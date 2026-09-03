"""Immutable, bounded SEC artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .errors import SnapshotIntegrityError
from .http import validate_sec_url

_SAFE_VALIDATORS = frozenset({"etag", "last-modified"})


@dataclass(frozen=True)
class SecArtifactRef:
    root: Path
    year: int
    quarter: int
    sha256: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256):
            raise SnapshotIntegrityError("invalid manifest digest")

    @property
    def directory(self) -> Path:
        return self.root / "sec" / "nport" / f"{self.year}q{self.quarter}" / self.sha256

    @property
    def source_zip(self) -> Path:
        return self.directory / "source.zip"

    @property
    def manifest(self) -> Path:
        return self.directory / "manifest.json"


class SecReplaySession:
    """A validated, single-use view of an immutable artifact.

    Validation is deliberately performed once.  Each required member may then
    be opened once; the returned stream accounts for every decompressed byte
    and verifies the ZIP CRC when exhausted.
    """

    def __init__(
        self,
        store: SecArtifactStore,
        ref: SecArtifactRef,
        *,
        parser_version: str = "1",
        required_members: tuple[str, ...] = (),
    ) -> None:
        self.store, self.ref = store, ref
        store.replay(ref, parser_version=parser_version, required_members=required_members)
        self._opened: set[str] = set()

    def open_member(
        self,
        basename: str,
        *,
        chunk_size: int = 1024 * 1024,
        cancelled: Callable[[], bool] | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] | None = None,
        progress: Callable[[str, int], None] | None = None,
    ) -> Iterator[bytes]:
        key = basename.rsplit(".", 1)[0].casefold()
        if key in self._opened:
            raise SnapshotIntegrityError("artifact member opened more than once")
        self._opened.add(key)
        clock_fn: Callable[[], float] = time.monotonic if clock is None else clock
        with zipfile.ZipFile(self.ref.source_zip) as archive:
            matches = [
                i
                for i in archive.infolist()
                if i.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold() == key
            ]
            if len(matches) != 1:
                raise SnapshotIntegrityError("required archive member missing or duplicated")
            info = matches[0]
            observed = 0
            try:
                with archive.open(info) as stream:
                    while True:
                        if cancelled is not None and cancelled():
                            raise SnapshotIntegrityError("artifact replay cancelled")
                        if deadline is not None and clock_fn() > deadline:
                            raise SnapshotIntegrityError("artifact replay deadline exceeded")
                        chunk = stream.read(chunk_size)
                        if not chunk:
                            break
                        observed += len(chunk)
                        if observed > self.store.max_member_bytes:
                            raise SnapshotIntegrityError("member expanded size exceeded")
                        if progress is not None:
                            progress(basename, observed)
                        yield chunk
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise SnapshotIntegrityError("invalid artifact member") from exc
            if observed != info.file_size:
                raise SnapshotIntegrityError("artifact member size mismatch")

    def assert_complete(self) -> None:
        expected = {n.rsplit(".", 1)[0].casefold() for n in REQUIRED_MEMBER_NAMES}
        if expected - self._opened:
            raise SnapshotIntegrityError("required artifact member was not consumed")


REQUIRED_MEMBER_NAMES = (
    "SUBMISSION",
    "REGISTRANT",
    "FUND_REPORTED_INFO",
    "FUND_REPORTED_HOLDING",
    "IDENTIFIERS",
)


class SecArtifactStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_members: int = 64,
        max_member_bytes: int = 16 * 1024**3,
        max_total_bytes: int = 32 * 1024**3,
        max_ratio: int = 250,
        max_download_bytes: int = 2 * 1024**3,
    ) -> None:
        self.root = Path(root)
        self.max_members, self.max_member_bytes = max_members, max_member_bytes
        self.max_total_bytes, self.max_ratio = max_total_bytes, max_ratio
        self.max_download_bytes = max_download_bytes

    def _mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._check_components(path)

    def _check_components(self, path: Path) -> None:
        current = Path(path.anchor) if path.is_absolute() else Path(".")
        for part in path.parts[1:] if path.is_absolute() else path.parts:
            current /= part
            try:
                current.lstat()
            except FileNotFoundError:
                continue
            if current.is_symlink() or not current.is_dir():
                raise SnapshotIntegrityError("artifact path component is unsafe")

    def publish(
        self,
        source: BinaryIO,
        *,
        year: int,
        quarter: int,
        source_url: str,
        content_type: str = "application/zip",
        retrieved_at: datetime | None = None,
        validators: dict[str, str] | None = None,
        parser_version: str = "1",
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[str, int], None] | None = None,
    ) -> SecArtifactRef:
        source_url = validate_sec_url(source_url)
        safe_validators: dict[str, str] = {}
        for key, value in (validators or {}).items():
            if (
                key.casefold() not in _SAFE_VALIDATORS
                or len(key) > 64
                or len(value) > 8192
                or any(ord(c) < 32 for c in value)
            ):
                raise SnapshotIntegrityError("unsafe HTTP validator")
            safe_validators[key.casefold()] = value
        self._mkdir(self.root)
        fd, tmp_name = tempfile.mkstemp(prefix=".sec-", dir=self.root)
        digest, count = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := source.read(1024 * 1024):
                    if cancelled is not None and cancelled():
                        raise SnapshotIntegrityError("artifact publication cancelled")
                    count += len(chunk)
                    if count > self.max_download_bytes:
                        raise SnapshotIntegrityError("compressed artifact exceeds limit")
                    digest.update(chunk)
                    out.write(chunk)
                    if progress is not None:
                        progress("download", count)
                out.flush()
                os.fsync(out.fileno())
            sha = digest.hexdigest()
            self._validate_zip(Path(tmp_name))
            ref = SecArtifactRef(self.root, year, quarter, sha, "0" * 64)
            self._mkdir(ref.directory)
            if ref.source_zip.exists():
                if not ref.source_zip.is_file() or ref.source_zip.is_symlink():
                    raise SnapshotIntegrityError("existing artifact is unsafe")
                os.unlink(tmp_name)
            else:
                os.replace(tmp_name, ref.source_zip)
                self._fsync_dir(ref.directory)
            manifest = {
                "schema_version": "sec-artifact-v1",
                "source_url": source_url,
                "year": year,
                "quarter": quarter,
                "byte_count": count,
                "sha256": sha,
                "content_type": content_type,
                "retrieved_at": (retrieved_at or datetime.now(UTC))
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "parser_version": parser_version,
                "validators": safe_validators,
            }
            manifest_digest = hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            manifest["manifest_sha256"] = manifest_digest
            if ref.manifest.exists():
                if not ref.manifest.is_file() or ref.manifest.is_symlink():
                    raise SnapshotIntegrityError("existing manifest is unsafe")
                if json.loads(ref.manifest.read_text()) != manifest:
                    raise SnapshotIntegrityError("artifact manifest collision")
            else:
                mf, mname = tempfile.mkstemp(prefix=".manifest-", dir=ref.directory)
                try:
                    with os.fdopen(mf, "w", encoding="utf-8", newline="\n") as out:
                        json.dump(manifest, out, sort_keys=True, separators=(",", ":"))
                        out.write("\n")
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(mname, ref.manifest)
                    self._fsync_dir(ref.directory)
                finally:
                    if os.path.exists(mname):
                        os.unlink(mname)
            return SecArtifactRef(self.root, year, quarter, sha, manifest_digest)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _validate_zip(self, path: Path, *, required_members: tuple[str, ...] = ()) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > self.max_members:
                    raise SnapshotIntegrityError("too many archive members")
                names: dict[str, zipfile.ZipInfo] = {}
                total = 0
                for info in infos:
                    base = info.filename.rsplit("/", 1)[-1]
                    parts = info.filename.replace("\\", "/").split("/")
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if (
                        info.is_dir()
                        or not base
                        or "\x00" in info.filename
                        or info.filename.startswith(("/", "\\"))
                        or any(x in {"..", ""} for x in parts[:-1])
                        or (mode and (mode & 0o170000) not in {0, 0o100000})
                        or info.flag_bits & 1
                        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or info.file_size > self.max_member_bytes
                        or (
                            info.compress_size
                            and info.file_size / info.compress_size > self.max_ratio
                        )
                    ):
                        raise SnapshotIntegrityError("unsafe archive member")
                    key = base.rsplit(".", 1)[0].casefold()
                    if key in names:
                        raise SnapshotIntegrityError("duplicate archive member basename")
                    names[key] = info
                    total += info.file_size
                if total > self.max_total_bytes:
                    raise SnapshotIntegrityError("archive expanded size exceeded")
                missing = [n for n in required_members if n.casefold() not in names]
                if missing:
                    raise SnapshotIntegrityError("required archive member missing")
        except (OSError, zipfile.BadZipFile) as exc:
            raise SnapshotIntegrityError("invalid ZIP") from exc

    def stream_member(
        self, ref: SecArtifactRef, basename: str, *, chunk_size: int = 1024 * 1024
    ) -> Iterator[bytes]:
        yield from SecReplaySession(self, ref).open_member(basename, chunk_size=chunk_size)

    def replay(
        self,
        ref: SecArtifactRef,
        *,
        parser_version: str = "1",
        required_members: tuple[str, ...] = (),
    ) -> SecArtifactRef:
        if (
            ref.root != self.root
            or not ref.source_zip.is_file()
            or ref.source_zip.is_symlink()
            or not ref.manifest.is_file()
            or ref.manifest.is_symlink()
        ):
            raise SnapshotIntegrityError("artifact missing or unsafe")
        self._check_components(ref.directory)
        try:
            manifest = json.loads(ref.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotIntegrityError("invalid artifact manifest") from exc
        digest, count = hashlib.sha256(), 0
        with ref.source_zip.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                digest.update(chunk)
        if (
            manifest.get("schema_version") != "sec-artifact-v1"
            or manifest.get("year") != ref.year
            or manifest.get("quarter") != ref.quarter
            or manifest.get("sha256") != ref.sha256
            or manifest.get("byte_count") != count
            or digest.hexdigest() != ref.sha256
            or manifest.get("parser_version") != parser_version
            or manifest.get("manifest_sha256")
            != hashlib.sha256(
                json.dumps(
                    {k: v for k, v in manifest.items() if k != "manifest_sha256"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            or ref.manifest_sha256 != manifest.get("manifest_sha256")
        ):
            raise SnapshotIntegrityError("artifact integrity mismatch")
        self._validate_zip(ref.source_zip, required_members=required_members)
        return ref

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = ["SecArtifactRef", "SecArtifactStore", "SecReplaySession"]
