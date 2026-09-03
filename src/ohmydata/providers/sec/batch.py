"""Pure, offline helpers for the SEC N-PORT quarterly batch workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO, Self, cast

from .artifacts import SecArtifactRef, SecArtifactStore
from .endpoints import SecNportQuarterRequest
from .errors import CoverageError, SchemaMismatchError, SnapshotIntegrityError
from .nport import extract_from_artifact

_QUARTER = re.compile(r"^(\d{4})q([1-4])$", re.ASCII)


@dataclass(frozen=True, order=True)
class Quarter:
    year: int
    number: int

    def __post_init__(self) -> None:
        if (
            self.year < 2019
            or self.number not in (1, 2, 3, 4)
            or (self.year == 2019 and self.number < 4)
        ):
            raise ValueError("invalid quarter")

    @classmethod
    def parse(cls, value: str) -> Quarter:
        if not _QUARTER.fullmatch(value.lower()):
            raise ValueError("quarter must be YYYYq[1-4]")
        m = _QUARTER.fullmatch(value.lower())
        assert m is not None
        return cls(int(m.group(1)), int(m.group(2)))

    def __str__(self) -> str:
        return f"{self.year}q{self.number}"


def quarter_range(
    start: str, end: str, *, now: Callable[[], datetime] | None = None
) -> tuple[Quarter, ...]:
    a, b = Quarter.parse(start), Quarter.parse(end)
    if a > b:
        raise ValueError("quarter range is reversed")
    clock = now or (lambda: datetime.now(UTC))
    current = clock()
    if current.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    maximum = Quarter(current.year, (current.month - 1) // 3 + 1)
    if b > maximum:
        raise ValueError("quarter is not yet current")
    out: list[Quarter] = []
    q = a
    while q <= b:
        out.append(q)
        q = Quarter(q.year + (q.number == 4), 1 if q.number == 4 else q.number + 1)
    return tuple(out)


def latest_completed_quarter(*, now: Callable[[], datetime] | None = None) -> Quarter:
    clock = now or (lambda: datetime.now(UTC))
    current = clock()
    if current.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    if current.month <= 3:
        return Quarter(current.year - 1, 4)
    if current.month <= 6:
        return Quarter(current.year, 1)
    if current.month <= 9:
        return Quarter(current.year, 2)
    return Quarter(current.year, 3)


@dataclass(frozen=True)
class SecFundSelector:
    symbol: str
    cik: str
    selection_mode: str
    series_id: str | None


@dataclass(frozen=True)
class SecScheduledFundSelector:
    selector: SecFundSelector
    valid_from_quarter: str | None = None
    valid_to_quarter: str | None = None

    def active(self, quarter: Quarter) -> bool:
        return not (
            (self.valid_from_quarter and quarter < Quarter.parse(self.valid_from_quarter))
            or (self.valid_to_quarter and quarter > Quarter.parse(self.valid_to_quarter))
        )


def _read_json_object(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SchemaMismatchError("JSON object required")
    return cast(dict[str, object], raw)


def canonical_json(value: Any) -> bytes:
    def convert(item: Any) -> Any:
        from decimal import Decimal

        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            if item.tzinfo is None:
                raise ValueError("naive datetime is not canonicalizable")
            return item.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, dict):
            mapping = cast(dict[Any, Any], item)
            pairs = sorted(mapping.items(), key=lambda x: str(x[0]))
            return {str(k): convert(v) for k, v in pairs}
        if isinstance(item, (list, tuple)):
            values = list(cast(Iterable[Any], item))
            return [convert(v) for v in values]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise TypeError("unsupported canonical JSON value")

    return json.dumps(
        convert(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def receipt_identity(value: dict[str, Any]) -> dict[str, Any]:
    """Canonical non-circular receipt identity payload."""
    return {
        "schema_version": value["schema_version"],
        "source_quarter": value["source_quarter"],
        "nport_artifact_sha256": value["nport_artifact_sha256"],
        "nport_manifest_sha256": value["nport_manifest_sha256"],
        "universe_hash": value["universe_hash"],
        "payloads": value["payloads"],
        "accessions": value["accessions"],
    }


def receipt_hashes(value: dict[str, Any]) -> tuple[str, str]:
    """Return the closure and receipt hashes for one receipt identity."""
    identity = receipt_identity(value)
    closure = canonical_hash(identity)
    receipt = canonical_hash({**identity, "edgar_closure_hash": closure})
    return closure, receipt


def ensure_safe_output_root(root: str | Path) -> Path:
    """Reject repository paths unless Git explicitly ignores the exact root."""
    resolved = Path(root).expanduser().resolve()
    probe = resolved
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        top = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return resolved
    ignored = subprocess.run(
        ["git", "-C", top, "check-ignore", "--quiet", "--no-index", "--", str(resolved)],
        check=False,
    )
    if ignored.returncode != 0:
        raise ValueError("output root must be outside the repository or Git-ignored")
    return resolved


class AdvisoryLock:
    """Bounded process lock used for state/index updates."""

    def __init__(self, path: str | Path, timeout: float = 30.0) -> None:
        self.path, self.timeout = Path(path), timeout
        self._file: Any = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + self.timeout
                while True:
                    try:
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                        return self
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("SEC state lock timeout") from None
                        time.sleep(0.01)
            import fcntl

            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("SEC state lock timeout") from None
                    time.sleep(0.01)
        except BaseException:
            self._file.close()
            self._file = None
            raise

    def __exit__(self, *_: object) -> None:
        if self._file is not None:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def atomic_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(value))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class AppendOnlyIndex:
    """Revisioned canonical JSON index; conflicting keys fail closed."""

    def __init__(
        self, path: str | Path, schema_version: str, allowed_keys: tuple[str, ...] | None = None
    ) -> None:
        self.path, self.schema_version, self.allowed_keys = Path(path), schema_version, allowed_keys
        self.lock = AdvisoryLock(self.path.parent / "sec-state.lock")

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": self.schema_version, "revision": 0, "entries": []}
        try:
            value = _read_json_object(self.path)
        except (OSError, ValueError) as exc:
            raise SchemaMismatchError("invalid state index") from exc
        if (
            value.get("schema_version") != self.schema_version
            or type(value.get("revision")) is not int
        ):
            raise SchemaMismatchError("invalid state index envelope")
        entries_obj = value.get("entries")
        if not isinstance(entries_obj, list):
            raise SchemaMismatchError("invalid state index entries")
        entries_raw = cast(list[object], entries_obj)
        entries: list[dict[str, object]] = []
        for entry_obj in entries_raw:
            if not isinstance(entry_obj, dict):
                raise SchemaMismatchError("invalid state index entry")
            entries.append(cast(dict[str, object], entry_obj))
        if self.allowed_keys is not None:
            if any(set(x) != set(self.allowed_keys) for x in entries):
                raise SchemaMismatchError("invalid state index entry keys")
            keys = [tuple(x.get(k) for k in self.allowed_keys) for x in entries]
            if len(set(keys)) != len(keys) or keys != sorted(
                keys, key=lambda x: tuple(str(y) for y in x)
            ):
                raise SchemaMismatchError("invalid state index ordering")
        return {
            "schema_version": self.schema_version,
            "revision": value["revision"],
            "entries": entries,
        }

    def append(self, entry: dict[str, Any], key_fields: tuple[str, ...]) -> dict[str, Any]:
        if self.allowed_keys is not None and not set(key_fields).issubset(self.allowed_keys):
            raise SchemaMismatchError("invalid state identity key")
        with self.lock:
            current = self.read()
            entries = cast(list[dict[str, object]], current["entries"])
            key = tuple(entry.get(k) for k in key_fields)
            for old in entries:
                if tuple(old.get(k) for k in key_fields) == key:
                    if old != entry:
                        raise SchemaMismatchError("state index key collision")
                    return current
            entries.append(dict(entry))
            entries.sort(key=lambda x: tuple(str(x.get(k, "")) for k in key_fields))
            value = {
                "schema_version": self.schema_version,
                "revision": current["revision"] + 1,
                "entries": entries,
            }
            atomic_json(self.path, value)
            return value


class SecEdgarReceiptIndex:
    """Request-key index retaining immutable EDGAR observations."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.index = AppendOnlyIndex(
            self.root / "state" / "sec-edgar-receipt-index-v1.json", "sec-edgar-receipt-index-v1"
        )

    def append(self, manifest: dict[str, object]) -> None:
        key_fields = ("cik", "request_kind", "historical_basename")
        if set(manifest) - {
            "schema_version",
            "request_kind",
            "cik",
            "historical_basename",
            "canonical_url",
            "payload_sha256",
            "byte_count",
            "content_type",
            "retrieved_at",
            "parser_version",
            "manifest_sha256",
        }:
            raise SchemaMismatchError("invalid EDGAR manifest")
        key = (manifest["cik"], manifest["request_kind"], manifest.get("historical_basename"))
        obs = {k: manifest[k] for k in ("payload_sha256", "manifest_sha256", "retrieved_at")}
        with self.index.lock:
            current = self.index.read()
            entries = cast(list[dict[str, object]], current["entries"])
            for entry in entries:
                if tuple(entry.get(k) for k in key_fields) != key:
                    continue
                raw = entry.get("observations")
                if not isinstance(raw, list):
                    raise SchemaMismatchError("invalid EDGAR observations")
                raw_objects = cast(list[object], raw)
                observations = [
                    cast(dict[str, object], x) for x in raw_objects if isinstance(x, dict)
                ]
                if len(observations) != len(raw_objects):
                    raise SchemaMismatchError("invalid EDGAR observation")
                if obs in observations:
                    return
                observations.append(obs)
                observations.sort(key=lambda x: (str(x["retrieved_at"]), str(x["payload_sha256"])))
                entry["observations"] = observations
                break
            else:
                entries.append(
                    {
                        "cik": key[0],
                        "request_kind": key[1],
                        "historical_basename": key[2],
                        "observations": [obs],
                    }
                )
            entries.sort(key=lambda x: tuple(str(x.get(k, "")) for k in key_fields))
            atomic_json(
                self.index.path,
                {
                    "schema_version": self.index.schema_version,
                    "revision": current["revision"] + 1,
                    "entries": entries,
                },
            )

    def observations(
        self, cik: str, request_kind: str, basename: str | None = None
    ) -> tuple[dict[str, object], ...]:
        out: list[dict[str, object]] = []
        for entry in cast(list[dict[str, object]], self.index.read()["entries"]):
            if entry.get("cik") != cik or entry.get("request_kind") != request_kind:
                continue
            if basename is not None and entry.get("historical_basename") != basename:
                continue
            raw = entry.get("observations")
            if not isinstance(raw, list):
                raise SchemaMismatchError("invalid EDGAR observations")
            for obs_obj in cast(list[object], raw):
                if not isinstance(obs_obj, dict):
                    raise SchemaMismatchError("invalid EDGAR observation")
                obs = cast(dict[str, object], obs_obj)
                path = (
                    self.root
                    / "raw"
                    / "sec"
                    / "edgar"
                    / "submissions"
                    / cik
                    / str(obs["payload_sha256"])
                )
                manifest = _read_json_object(path / "manifest.json")
                body = (path / "source.json").read_bytes()
                from .edgar import historical_submission_url

                entry_basename = entry.get("historical_basename")
                expected_url = (
                    f"https://data.sec.gov/submissions/CIK{cik}.json"
                    if request_kind == "current"
                    else historical_submission_url(cik, str(entry_basename))
                )
                if (
                    manifest.get("cik") != cik
                    or manifest.get("request_kind") != request_kind
                    or manifest.get("historical_basename") != entry_basename
                    or manifest.get("canonical_url") != expected_url
                    or manifest.get("payload_sha256") != obs.get("payload_sha256")
                    or manifest.get("manifest_sha256") != obs.get("manifest_sha256")
                    or manifest.get("byte_count") != len(body)
                    or hashlib.sha256(body).hexdigest() != manifest.get("payload_sha256")
                    or canonical_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
                    != manifest.get("manifest_sha256")
                ):
                    continue
                out.append(manifest)
        return tuple(
            sorted(
                out,
                key=lambda x: (str(x.get("retrieved_at")), str(x.get("payload_sha256"))),
                reverse=True,
            )
        )


def store_immutable_payload(
    root: str | Path, cik: str, body: bytes, manifest: dict[str, Any]
) -> tuple[Path, str]:
    digest = hashlib.sha256(body).hexdigest()
    if manifest.get("payload_sha256") != digest or manifest.get("cik") != cik:
        raise SchemaMismatchError("EDGAR payload manifest mismatch")
    target = Path(root) / "raw" / "sec" / "edgar" / "submissions" / cik / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    source = target / "source.json"
    if "manifest_sha256" in manifest:
        expected = manifest["manifest_sha256"]
        actual = canonical_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
        if expected != actual:
            raise SchemaMismatchError("EDGAR manifest hash mismatch")
    if target.exists():
        if source.read_bytes() != body:
            raise SchemaMismatchError("payload collision")
        return target, digest
    temp = Path(tempfile.mkdtemp(prefix=f".{digest}-", dir=target.parent))
    try:
        staged = temp / "source.json"
        staged.write_bytes(body)
        fd = os.open(staged, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
        atomic_json(temp / "manifest.json", manifest)
        os.replace(temp, target)
        fd = os.open(target.parent, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    finally:
        if temp.exists():
            import shutil

            shutil.rmtree(temp)
    return target, digest


def enumerate_receipts(
    root: str | Path, quarter: str, universe_hash: str
) -> tuple[tuple[Path, dict[str, Any]], ...]:
    folder = Path(root) / "state" / "fetch-receipts" / quarter
    found: list[tuple[Path, dict[str, object]]] = []
    if folder.exists():
        for path in sorted(folder.glob("*.json")):
            value = _read_json_object(path)
            if value.get("universe_hash") != universe_hash:
                continue
            _, expected_receipt = receipt_hashes(value)
            if path.stem != expected_receipt:
                raise SchemaMismatchError("receipt filename hash mismatch")
            refs_obj = value.get("payloads", [])
            if not isinstance(refs_obj, list):
                raise SchemaMismatchError("invalid receipt payloads")
            refs = cast(list[object], refs_obj)
            for ref_obj in refs:
                if not isinstance(ref_obj, dict):
                    raise SchemaMismatchError("invalid receipt payload reference")
                ref = cast(dict[str, object], ref_obj)
                payload = (
                    Path(root)
                    / "raw"
                    / "sec"
                    / "edgar"
                    / "submissions"
                    / str(ref.get("cik"))
                    / str(ref.get("payload_sha256"))
                )
                manifest = payload / "manifest.json"
                if (
                    not payload.is_dir()
                    or not (payload / "source.json").is_file()
                    or not manifest.is_file()
                ):
                    raise CoverageError("receipt payload is missing")
                if hashlib.sha256((payload / "source.json").read_bytes()).hexdigest() != ref.get(
                    "payload_sha256"
                ):
                    raise SchemaMismatchError("receipt payload tampered")
                manifest_value = _read_json_object(manifest)
                if canonical_hash(
                    {k: v for k, v in manifest_value.items() if k != "manifest_sha256"}
                ) != ref.get("manifest_sha256"):
                    raise SchemaMismatchError("receipt manifest tampered")
            found.append((path, value))
    return tuple(
        sorted(
            found,
            key=lambda x: (
                str(x[1].get("nport_artifact_sha256", "")),
                str(x[1].get("nport_manifest_sha256", "")),
                x[0].name,
            ),
        )
    )


def partition_identity(**dimensions: Any) -> str:
    return canonical_hash({"schema_version": "sec-partition-identity-v1", **dimensions})


def publish_directory(temp_dir: str | Path, final_dir: str | Path) -> None:
    """Publish a completed immutable directory, rejecting non-idempotent reuse."""
    source, target = Path(temp_dir), Path(final_dir)
    if not source.is_dir() or target.exists() and not target.is_dir():
        raise ValueError("invalid partition directory")
    if target.exists():
        old = {
            p.relative_to(target): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in target.rglob("*")
            if p.is_file()
        }
        new = {
            p.relative_to(source): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob("*")
            if p.is_file()
        }
        if old != new:
            raise SchemaMismatchError("partition publication collision")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_universe(path: str | Path) -> tuple[tuple[SecScheduledFundSelector, ...], str]:
    raw = _read_json_object(Path(path))
    if raw.get("schema_version") != "sec-equity-etf-universe-v1":
        raise SchemaMismatchError("invalid SEC universe schema")
    funds = raw.get("funds")
    if not isinstance(funds, list):
        raise SchemaMismatchError("universe funds must be a list")
    funds = cast(list[object], funds)
    selectors: list[SecFundSelector] = []
    scheduled: list[SecScheduledFundSelector] = []
    seen_symbols: set[str] = set()
    seen_source: set[tuple[str, str, str | None]] = set()
    for item in funds:
        if not isinstance(item, dict):
            raise SchemaMismatchError("invalid universe fund")
        item_obj = cast(dict[str, object], item)
        symbol_obj, cik_obj = item_obj.get("symbol"), item_obj.get("cik")
        mode_obj, series_obj = item_obj.get("selection_mode"), item_obj.get("series_id")
        if not isinstance(symbol_obj, str) or not re.fullmatch(r"[A-Z0-9.\-]+", symbol_obj):
            raise SchemaMismatchError("invalid universe symbol")
        if not isinstance(cik_obj, str) or not re.fullmatch(r"\d{10}", cik_obj):
            raise SchemaMismatchError("invalid universe CIK")
        if not isinstance(mode_obj, str):
            raise SchemaMismatchError("invalid selection mode")
        if series_obj is not None and not isinstance(series_obj, str):
            raise SchemaMismatchError("invalid series id")
        symbol, cik, mode, series = symbol_obj, cik_obj, mode_obj, series_obj
        if mode not in ("series", "single_series_cik"):
            raise SchemaMismatchError("invalid selection mode")
        if mode == "series" and not series:
            raise SchemaMismatchError("series selection requires series_id")
        if mode == "single_series_cik" and series is not None:
            raise SchemaMismatchError("single-series selection requires null series_id")
        start, finish = (
            cast(str | None, item_obj.get("valid_from_quarter")),
            cast(str | None, item_obj.get("valid_to_quarter")),
        )
        for bound in (start, finish):
            if bound is not None:
                Quarter.parse(bound)
        if start and finish and Quarter.parse(start) > Quarter.parse(finish):
            raise SchemaMismatchError("invalid universe validity bounds")
        key = (cik, mode, series)
        if symbol in seen_symbols or key in seen_source:
            raise SchemaMismatchError("duplicate universe identity")
        seen_symbols.add(symbol)
        seen_source.add(key)
        selector = SecFundSelector(symbol, cik, mode, series)
        selectors.append(selector)
        scheduled.append(SecScheduledFundSelector(selector, start, finish))
    selectors.sort(key=lambda x: (x.symbol, x.cik, x.series_id or ""))
    scheduled.sort(
        key=lambda x: (
            x.selector.symbol,
            x.selector.cik,
            x.selector.series_id or "",
            x.valid_from_quarter or "",
            x.valid_to_quarter or "",
        )
    )
    canonical = {
        "schema_version": raw["schema_version"],
        "funds": [
            {
                "symbol": x.selector.symbol,
                "cik": x.selector.cik,
                "selection_mode": x.selector.selection_mode,
                "series_id": x.selector.series_id,
                "valid_from_quarter": x.valid_from_quarter,
                "valid_to_quarter": x.valid_to_quarter,
            }
            for x in scheduled
        ],
    }
    digest = canonical_hash(canonical)
    return tuple(scheduled), digest


@dataclass(frozen=True)
class SecEquityEtfUniverse:
    funds: tuple[SecScheduledFundSelector, ...]
    universe_hash: str
    raw_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> SecEquityEtfUniverse:
        p = Path(path)
        funds, uhash = load_universe(p)
        return cls(funds=funds, universe_hash=uhash, raw_path=p)

    def active_for(self, quarter: Quarter) -> tuple[SecScheduledFundSelector, ...]:
        return tuple(f for f in self.funds if f.active(quarter))


def resolve_single_series_cik(parent_rows: list[dict[str, Any]], cik: str) -> str | None:
    """Return the sole empty native series identity, or fail on ambiguity."""
    rows = [r for r in parent_rows if str(r.get("CIK", "")) == cik]
    if len(rows) != 1 or str(rows[0].get("SERIES_ID") or ""):
        raise CoverageError("single-series CIK is ambiguous")
    return None


class SecNportBatch:
    """Small, sequential quarter orchestrator; transport is always injected."""

    DEFAULT_MAX_SELECTED_ROWS = 5_000_000
    DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024**3

    def __init__(
        self,
        root: str | Path,
        client: Any = None,
        *,
        max_selected_rows: int = DEFAULT_MAX_SELECTED_ROWS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        progress: Callable[[str, int], None] | None = None,
    ) -> None:
        if max_selected_rows <= 0:
            raise ValueError("max-selected-rows must be positive")
        if max_output_bytes <= 0:
            raise ValueError("max-output-bytes must be positive")
        self.root = ensure_safe_output_root(root)
        self.store = SecArtifactStore(self.root / "raw")
        self.client = client
        self.max_selected_rows = max_selected_rows
        self.max_output_bytes = max_output_bytes
        self.progress = progress

    def _quarter_index(self) -> AppendOnlyIndex:
        return AppendOnlyIndex(
            self.root / "state" / "sec-nport-quarter-index-v1.json",
            "sec-nport-quarter-index-v1",
            ("year", "quarter", "artifact_sha256", "manifest_sha256", "source_url", "retrieved_at"),
        )

    def artifact_for_quarter(self, quarter: Quarter) -> SecArtifactRef | None:
        entries = [
            x
            for x in self._quarter_index().read()["entries"]
            if x.get("year") == quarter.year and x.get("quarter") == quarter.number
        ]
        valid: list[SecArtifactRef] = []
        for x in entries:
            ref = SecArtifactRef(
                self.root / "raw",
                quarter.year,
                quarter.number,
                str(x["artifact_sha256"]),
                str(x["manifest_sha256"]),
            )
            try:
                self.store.replay(
                    ref,
                    required_members=(
                        "SUBMISSION",
                        "REGISTRANT",
                        "FUND_REPORTED_INFO",
                        "FUND_REPORTED_HOLDING",
                        "IDENTIFIERS",
                    ),
                )
            except SnapshotIntegrityError:
                continue
            valid.append(ref)
        return valid[-1] if valid else None

    def fetch_quarter(self, quarter: Quarter, *, refresh: bool = False) -> Any:
        if self.client is None:
            raise ValueError("a SecHttpClient is required for fetch")
        if not refresh:
            cached = self.artifact_for_quarter(quarter)
            if cached is not None:
                return cached
        from .endpoints import SecEmptyPolicy

        request = SecNportQuarterRequest(
            quarter.year,
            quarter.number,
            ("__batch__",),
            required_series_ids=(),
            empty_policy=SecEmptyPolicy.ALLOW_EMPTY,
        )
        response = self.client.open(request.source_url, accept="application/zip")

        class _DigestStream:
            def __init__(self, stream: Any) -> None:
                self.stream = stream
                self.digest = hashlib.sha256()

            def read(self, size: int = -1) -> bytes:
                chunk = self.stream.read(size)
                if chunk:
                    self.digest.update(chunk)
                return chunk

        source = _DigestStream(response.body)
        try:
            ref = self.store.publish(
                cast(BinaryIO, source),
                year=quarter.year,
                quarter=quarter.number,
                source_url=request.source_url,
                content_type="application/zip",
                progress=self.progress,
            )
        except SnapshotIntegrityError as exc:
            # A refresh of identical bytes must reuse the immutable artifact;
            # the store quite correctly rejects a new manifest timestamp.
            if "manifest collision" not in str(exc):
                raise
            digest = source.digest.hexdigest()
            for entry in self._quarter_index().read()["entries"]:
                if (
                    entry.get("year") == quarter.year
                    and entry.get("quarter") == quarter.number
                    and entry.get("artifact_sha256") == digest
                ):
                    return SecArtifactRef(
                        self.root / "raw",
                        quarter.year,
                        quarter.number,
                        digest,
                        str(entry["manifest_sha256"]),
                    )
            raise
        manifest = json.loads(ref.manifest.read_text(encoding="utf-8"))
        self._quarter_index().append(
            {
                "year": quarter.year,
                "quarter": quarter.number,
                "artifact_sha256": ref.sha256,
                "manifest_sha256": ref.manifest_sha256,
                "source_url": request.source_url,
                "retrieved_at": manifest["retrieved_at"],
            },
            ("year", "quarter", "artifact_sha256", "manifest_sha256", "source_url", "retrieved_at"),
        )
        return ref

    def fetch_edgar(
        self, cik: str, accessions: tuple[str, ...], *, refresh: bool = False
    ) -> dict[str, Any]:
        """Fetch and persist the exact EDGAR closure needed by accessions."""
        from .edgar import SecPayloadReceipt, resolve_submissions

        if self.client is None:
            raise ValueError("a SecHttpClient is required for fetch")
        index = SecEdgarReceiptIndex(self.root)
        if not refresh:
            cached_refs: dict[str, object] = {}
            current_manifests = index.observations(cik, "current")
            historical_manifests = index.observations(cik, "historical")

            def cached_history(url: str) -> bytes:
                basename = url.rsplit("/", 1)[-1]
                for item in historical_manifests:
                    if item.get("historical_basename") != basename:
                        continue
                    path = (
                        self.root
                        / "raw"
                        / "sec"
                        / "edgar"
                        / "submissions"
                        / cik
                        / str(item["payload_sha256"])
                        / "source.json"
                    )
                    try:
                        body = path.read_bytes()
                    except OSError:
                        continue
                    if hashlib.sha256(body).hexdigest() == item.get("payload_sha256"):
                        cached_refs[basename] = item
                        return body
                raise CoverageError("historical cache missing")

            for candidate in current_manifests:
                try:
                    payload_path = (
                        self.root
                        / "raw"
                        / "sec"
                        / "edgar"
                        / "submissions"
                        / cik
                        / str(candidate["payload_sha256"])
                        / "source.json"
                    )
                    payload = SecPayloadReceipt.from_bytes(payload_path.read_bytes()).payload
                    metadata = resolve_submissions(
                        payload,
                        cik,
                        tuple(accessions),
                        load=cached_history,
                    )
                    return {"metadata": metadata, "refs": {"current": candidate, **cached_refs}}
                except (OSError, CoverageError, SchemaMismatchError):
                    continue
        request_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        response = self.client.open(request_url, accept="application/json")
        body = response.body.read()
        receipt = SecPayloadReceipt.from_bytes(body)
        payload_hash = hashlib.sha256(body).hexdigest()
        base = {
            "schema_version": "sec-edgar-payload-v1",
            "request_kind": "current",
            "cik": cik,
            "historical_basename": None,
            "canonical_url": request_url,
            "payload_sha256": payload_hash,
            "byte_count": len(body),
            "content_type": "application/json",
            "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "parser_version": "1",
        }
        base["manifest_sha256"] = canonical_hash(base)
        stored_path, _ = store_immutable_payload(self.root, cik, body, base)
        base = _read_json_object(stored_path / "manifest.json")
        edgar_index = SecEdgarReceiptIndex(self.root)
        edgar_index.append(base)
        refs: dict[str, Any] = {"current": base}

        def load(url: str) -> bytes:
            res = self.client.open(url, accept="application/json")
            data = res.body.read()
            basename = url.rsplit("/", 1)[-1]
            digest = hashlib.sha256(data).hexdigest()
            mf = {
                "schema_version": "sec-edgar-payload-v1",
                "request_kind": "historical",
                "cik": cik,
                "historical_basename": basename,
                "canonical_url": url,
                "payload_sha256": digest,
                "byte_count": len(data),
                "content_type": "application/json",
                "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "parser_version": "1",
            }
            mf["manifest_sha256"] = canonical_hash(mf)
            stored_path, _ = store_immutable_payload(self.root, cik, data, mf)
            mf = _read_json_object(stored_path / "manifest.json")
            edgar_index.append(mf)
            refs[basename] = mf
            return data

        metadata = resolve_submissions(receipt.payload, cik, tuple(accessions), load=load)
        return {"metadata": metadata, "refs": refs}

    def fetch_receipt_for_quarter(
        self,
        quarter: Quarter,
        scheduled: tuple[SecScheduledFundSelector, ...],
        universe_hash: str,
        *,
        refresh: bool = False,
    ) -> Path:
        if not refresh:
            receipts = enumerate_receipts(self.root, str(quarter), universe_hash)
            if receipts:
                return receipts[0][0]
        active = tuple(x.selector for x in scheduled if x.active(quarter))
        artifact = self.fetch_quarter(quarter, refresh=refresh)
        ids = tuple(x.series_id for x in active if x.selection_mode == "series" and x.series_id)
        singles = tuple(x.cik for x in active if x.selection_mode == "single_series_cik")
        pairs = tuple(
            (x.cik, x.series_id) for x in active if x.selection_mode == "series" and x.series_id
        )
        result = extract_from_artifact(
            self.store,
            artifact,
            SecNportQuarterRequest(
                quarter.year,
                quarter.number,
                ids,
                required_series_ids=ids,
                single_series_ciks=singles,
                selected_pairs=pairs,
                required_pairs=pairs,
            ),
            max_selected_rows=self.max_selected_rows,
        )
        by_cik: dict[str, tuple[str, ...]] = {}
        for vintage in result.vintages:
            by_cik[vintage.cik] = tuple(
                sorted(set(by_cik.get(vintage.cik, ()) + (vintage.accession_number,)))
            )
        closures = {
            cik: self.fetch_edgar(cik, accessions, refresh=refresh)
            for cik, accessions in by_cik.items()
        }
        return self.publish_receipt(quarter, artifact, universe_hash, closures, by_cik)

    def publish_receipt(
        self,
        quarter: Quarter,
        artifact: Any,
        universe_hash: str,
        closures: dict[str, Any],
        accessions: dict[str, tuple[str, ...]],
    ) -> Path:
        payloads = [ref for closure in closures.values() for ref in closure["refs"].values()]
        value = {
            "schema_version": "sec-fetch-receipt-v1",
            "source_quarter": str(quarter),
            "nport_artifact_sha256": artifact.sha256,
            "nport_manifest_sha256": artifact.manifest_sha256,
            "universe_hash": universe_hash,
            "payloads": payloads,
            "accessions": accessions,
        }
        value["edgar_closure_hash"], digest = receipt_hashes(value)
        value["receipt_sha256"] = digest
        path = self.root / "state" / "fetch-receipts" / str(quarter) / f"{digest}.json"
        if path.exists():
            existing = _read_json_object(path)
            closure, expected = receipt_hashes(existing)
            if (
                existing.get("edgar_closure_hash") != closure
                or existing.get("receipt_sha256") != expected
                or expected != digest
            ):
                raise SchemaMismatchError("receipt collision")
            return path
        if not path.exists():
            atomic_json(path, value)
        return path

    def build_receipt(
        self,
        receipt_path: str | Path,
        scheduled_selectors: tuple[SecScheduledFundSelector, ...],
        availability_policy: str,
        lag_days: int | None = None,
        max_selected_rows: int | None = None,
        max_output_bytes: int | None = None,
    ) -> dict[str, Any]:
        selected_row_limit = (
            self.max_selected_rows if max_selected_rows is None else max_selected_rows
        )
        output_byte_limit = self.max_output_bytes if max_output_bytes is None else max_output_bytes
        if selected_row_limit <= 0 or output_byte_limit <= 0:
            raise ValueError("output limits must be positive")
        from dataclasses import replace

        from .core_dataset import rows_from_result, write_partition
        from .edgar import SecPayloadReceipt

        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        closure, expected_receipt = receipt_hashes(cast(dict[str, Any], receipt))
        if (
            receipt.get("edgar_closure_hash") != closure
            or receipt.get("receipt_sha256") != expected_receipt
        ):
            raise SchemaMismatchError("receipt hash mismatch")
        q = Quarter.parse(str(receipt["source_quarter"]))
        artifact = SecArtifactRef(
            self.root / "raw",
            q.year,
            q.number,
            receipt["nport_artifact_sha256"],
            receipt["nport_manifest_sha256"],
        )
        self.store.replay(
            artifact,
            required_members=(
                "SUBMISSION",
                "REGISTRANT",
                "FUND_REPORTED_INFO",
                "FUND_REPORTED_HOLDING",
                "IDENTIFIERS",
            ),
        )
        active = tuple(x.selector for x in scheduled_selectors if x.active(q))
        ids = tuple(x.series_id for x in active if x.selection_mode == "series" and x.series_id)
        singles = tuple(x.cik for x in active if x.selection_mode == "single_series_cik")
        pairs = tuple(
            (x.cik, x.series_id) for x in active if x.selection_mode == "series" and x.series_id
        )
        request = SecNportQuarterRequest(
            q.year,
            q.number,
            ids,
            required_series_ids=ids,
            single_series_ciks=singles,
            selected_pairs=pairs,
            required_pairs=pairs,
        )
        result = extract_from_artifact(
            self.store,
            artifact,
            request,
            max_selected_rows=selected_row_limit,
        )
        refs = {
            (x.get("cik"), x.get("historical_basename")): x for x in receipt.get("payloads", [])
        }
        grouped: list[list[dict[str, Any]]] = [[], [], []]
        selected_row_count = 0
        final_vintages: list[Any] = []
        for selector in active:
            vintages = tuple(
                v
                for v in result.vintages
                if v.cik == selector.cik
                and (
                    selector.selection_mode == "single_series_cik"
                    or selector.series_id == v.series_id
                )
            )
            for v in vintages:
                current = refs.get((v.cik, None))
                if current is None:
                    raise CoverageError("receipt current payload missing")
                current_path = (
                    self.root
                    / "raw"
                    / "sec"
                    / "edgar"
                    / "submissions"
                    / v.cik
                    / current["payload_sha256"]
                    / "source.json"
                )
                payload = SecPayloadReceipt.from_bytes(current_path.read_bytes()).payload
                from .nport import SecAvailabilityPolicy, enrich_vintages

                def loader(url: str, cik: str = v.cik) -> bytes:
                    basename = url.rsplit("/", 1)[-1]
                    ref = refs.get((cik, basename))
                    if ref is None:
                        raise CoverageError("receipt historical payload missing")
                    return (
                        self.root
                        / "raw"
                        / "sec"
                        / "edgar"
                        / "submissions"
                        / cik
                        / ref["payload_sha256"]
                        / "source.json"
                    ).read_bytes()

                v = enrich_vintages((v,), payload, v.cik, load=loader)[0]
                accepted = v.accepted_at
                if availability_policy == "observation-only" and lag_days is not None:
                    raise ValueError("lag-days is invalid for observation-only")
                policy_value = (
                    SecAvailabilityPolicy.OBSERVATION_ONLY_V1.value
                    if availability_policy == "observation-only"
                    else SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1.value
                )
                if availability_policy == "accepted-at-plus-lag":
                    if lag_days is None or not 0 <= lag_days <= 30:
                        raise ValueError("lag-days must be 0..30")
                    anchor = accepted + timedelta(days=lag_days) if accepted else None
                    v = replace(
                        v,
                        availability_anchor=anchor,
                        availability_basis="PIT_INFERRED" if accepted else "UNKNOWN",
                        availability_policy=policy_value,
                        availability_lag_days=lag_days,
                        quality_flags=tuple(v.quality_flags)
                        + (("PIT_INFERRED",) if accepted else ()),
                    )
                else:
                    v = replace(
                        v,
                        availability_policy=policy_value,
                        availability_lag_days=0,
                    )
                final_vintages.append(v)
                tables = rows_from_result(
                    replace(result, vintages=(v,)),
                    selector.symbol,
                    receipt["universe_hash"],
                    receipt["nport_manifest_sha256"],
                )
                for i in range(3):
                    grouped[i].extend(tables[i])
                    selected_row_count += len(tables[i])
                    if selected_row_count > selected_row_limit:
                        raise CoverageError("selected output row limit exceeded")
        merged: tuple[
            Iterable[dict[str, Any]], Iterable[dict[str, Any]], Iterable[dict[str, Any]]
        ] = (grouped[0], grouped[1], grouped[2])
        from dataclasses import asdict

        quality_flags = sorted(
            {flag for vintage in final_vintages for flag in vintage.quality_flags}
            | set(result.qa.quality_flags)
        )
        quality = {
            "scan_counts": {
                "member_rows": dict(result.scan_counts.member_rows),
                "retained_rows": dict(result.scan_counts.retained_rows),
            },
            "qa": asdict(result.qa),
            "quality_flags": quality_flags,
        }
        return write_partition(
            self.root,
            source_quarter=str(q),
            artifact_sha256=artifact.sha256,
            artifact_manifest_sha256=artifact.manifest_sha256,
            universe_hash=receipt["universe_hash"],
            edgar_closure_hash=receipt["edgar_closure_hash"],
            parser_version="1",
            availability_policy=availability_policy,
            lag_days=lag_days,
            tables=merged,
            quality=quality,
            max_selected_rows=selected_row_limit,
            max_output_bytes=output_byte_limit,
        )

    def build_all_receipts(
        self,
        quarter: Quarter,
        universe_hash: str,
        scheduled_selectors: tuple[SecScheduledFundSelector, ...],
        availability_policy: str,
        lag_days: int | None = None,
        max_selected_rows: int | None = None,
        max_output_bytes: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        receipts = enumerate_receipts(self.root, str(quarter), universe_hash)
        if not receipts:
            raise CoverageError("no matching fetch receipts")
        return tuple(
            self.build_receipt(
                path,
                scheduled_selectors,
                availability_policy,
                lag_days,
                max_selected_rows=max_selected_rows,
                max_output_bytes=max_output_bytes,
            )
            for path, _ in receipts
        )

    def validate_local(self, quarters: tuple[Quarter, ...]) -> dict[str, Any]:
        checked = 0
        partitions = 0
        receipt_dimensions: dict[str, set[tuple[object, ...]]] = {}
        from .core_dataset import validate_tables

        quarter_index = self._quarter_index().read()
        requested = set(quarters)
        for quarter in quarters:
            entries = [
                x
                for x in quarter_index["entries"]
                if x.get("year") == quarter.year and x.get("quarter") == quarter.number
            ]
            if not entries:
                raise CoverageError("quarter artifact missing")
            for entry in entries:
                ref = SecArtifactRef(
                    self.root / "raw",
                    quarter.year,
                    quarter.number,
                    str(entry["artifact_sha256"]),
                    str(entry["manifest_sha256"]),
                )
                try:
                    self.store.replay(
                        ref,
                        required_members=(
                            "SUBMISSION",
                            "REGISTRANT",
                            "FUND_REPORTED_INFO",
                            "FUND_REPORTED_HOLDING",
                            "IDENTIFIERS",
                        ),
                    )
                except SnapshotIntegrityError as exc:
                    raise SnapshotIntegrityError("quarter artifact tampered") from exc
                manifest = _read_json_object(ref.manifest)
                if (
                    manifest.get("sha256") != ref.sha256
                    or manifest.get("manifest_sha256") != ref.manifest_sha256
                ):
                    raise SnapshotIntegrityError("quarter manifest identity mismatch")
                checked += 1
            part_root = (
                self.root / "core" / "sec-fund-holdings-pit-v1" / f"source_quarter={quarter}"
            )
            for partition in sorted(part_root.glob("artifact=*/partition=*")):
                if not partition.is_dir():
                    raise CoverageError("invalid partition path")
                validate_tables(partition)
                partitions += 1
            receipt_folder = self.root / "state" / "fetch-receipts" / str(quarter)
            for receipt_path in sorted(receipt_folder.glob("*.json")):
                receipt = _read_json_object(receipt_path)
                if (
                    set(receipt)
                    != {
                        "schema_version",
                        "source_quarter",
                        "nport_artifact_sha256",
                        "nport_manifest_sha256",
                        "universe_hash",
                        "payloads",
                        "accessions",
                        "edgar_closure_hash",
                        "receipt_sha256",
                    }
                    or receipt.get("schema_version") != "sec-fetch-receipt-v1"
                ):
                    raise SchemaMismatchError("invalid receipt schema")
                closure, expected_receipt = receipt_hashes(receipt)
                if (
                    receipt_path.stem != expected_receipt
                    or receipt.get("receipt_sha256") != expected_receipt
                ):
                    raise SchemaMismatchError("receipt filename hash mismatch")
                if receipt.get("source_quarter") != str(quarter):
                    raise SchemaMismatchError("receipt quarter mismatch")
                if not any(
                    x.get("artifact_sha256") == receipt.get("nport_artifact_sha256")
                    and x.get("manifest_sha256") == receipt.get("nport_manifest_sha256")
                    for x in entries
                ):
                    raise CoverageError("receipt NPORT artifact is not indexed")
                enumerate_receipts(self.root, str(quarter), str(receipt.get("universe_hash", "")))
                refs = receipt.get("payloads")
                if not isinstance(refs, list):
                    raise SchemaMismatchError("invalid receipt payloads")
                if receipt.get("edgar_closure_hash") != closure:
                    raise SnapshotIntegrityError("receipt EDGAR closure hash mismatch")
                accessions = receipt.get("accessions")
                accession_map = (
                    cast(dict[object, object], accessions) if isinstance(accessions, dict) else {}
                )
                invalid_accessions = False
                for key, value in accession_map.items():
                    if not isinstance(key, str) or not isinstance(value, list):
                        invalid_accessions = True
                        break
                    if any(
                        not isinstance(accession, str) for accession in cast(list[object], value)
                    ):
                        invalid_accessions = True
                        break
                if not isinstance(accessions, dict) or invalid_accessions:
                    raise SchemaMismatchError("invalid receipt accessions")
                receipt_dimensions.setdefault(str(quarter), set()).add(
                    (
                        receipt["nport_artifact_sha256"],
                        receipt["nport_manifest_sha256"],
                        receipt["universe_hash"],
                        receipt["edgar_closure_hash"],
                    )
                )
                for ref_obj in cast(list[object], refs):
                    if not isinstance(ref_obj, dict):
                        raise SchemaMismatchError("invalid receipt reference")
                    ref = cast(dict[str, object], ref_obj)
                    payload_dir = (
                        self.root
                        / "raw"
                        / "sec"
                        / "edgar"
                        / "submissions"
                        / str(ref["cik"])
                        / str(ref["payload_sha256"])
                    )
                    if not payload_dir.is_dir():
                        raise CoverageError("receipt payload missing")
                    body = payload_dir / "source.json"
                    manifest = _read_json_object(payload_dir / "manifest.json")
                    if hashlib.sha256(body.read_bytes()).hexdigest() != ref.get(
                        "payload_sha256"
                    ) or canonical_hash(
                        {k: v for k, v in manifest.items() if k != "manifest_sha256"}
                    ) != ref.get("manifest_sha256"):
                        raise SnapshotIntegrityError("receipt payload tampered")
                    observations = SecEdgarReceiptIndex(self.root).observations(
                        str(ref["cik"]),
                        str(manifest.get("request_kind")),
                        cast(str | None, manifest.get("historical_basename")),
                    )
                    if not any(
                        x.get("payload_sha256") == ref.get("payload_sha256")
                        and x.get("manifest_sha256") == ref.get("manifest_sha256")
                        for x in observations
                    ):
                        raise CoverageError("receipt EDGAR closure is not indexed")
        catalog = self.root / "core" / "sec-fund-holdings-pit-v1" / "catalog.json"
        if catalog.exists():
            entries = AppendOnlyIndex(catalog, "sec-core-catalog-v1").read()["entries"]
            allowed = {
                "source_quarter",
                "artifact_sha256",
                "artifact_manifest_sha256",
                "universe_hash",
                "edgar_closure_hash",
                "parser_version",
                "dataset_schema_version",
                "availability_policy",
                "lag_days",
                "writer_profile_version",
                "pyarrow_version",
                "partition_identity",
                "partition_path",
                "manifest_hash",
                "file_hashes",
            }
            indexed: set[Path] = set()
            for entry_obj in cast(list[object], entries):
                entry = cast(dict[str, object], entry_obj)
                if set(entry) != allowed:
                    raise SchemaMismatchError("invalid core catalog entry keys")
                if entry.get("source_quarter") not in {str(q) for q in requested}:
                    continue
                relative = Path(str(entry.get("partition_path", "")))
                if relative.is_absolute() or ".." in relative.parts:
                    raise CoverageError("catalog partition path escapes root")
                target = (self.root / relative).resolve()
                if self.root.resolve() not in target.parents or not target.is_dir():
                    raise CoverageError("catalog partition path escapes root")
                manifest = _read_json_object(target / "manifest.json")
                if str(target.relative_to(self.root.resolve())) != str(relative):
                    raise CoverageError("noncanonical catalog partition path")
                expected_relative = Path(
                    "core/sec-fund-holdings-pit-v1"
                    f"/source_quarter={manifest.get('source_quarter')}"
                    f"/artifact={manifest.get('artifact_sha256')}"
                    f"/partition={manifest.get('partition_identity')}"
                )
                if relative != expected_relative:
                    raise CoverageError("noncanonical catalog partition path")
                if canonical_hash(manifest) != entry.get("manifest_hash") or manifest.get(
                    "partition_identity"
                ) != entry.get("partition_identity"):
                    raise SnapshotIntegrityError("catalog manifest mismatch")
                dimensions = {
                    k: manifest.get(k)
                    for k in (
                        "source_quarter",
                        "artifact_sha256",
                        "artifact_manifest_sha256",
                        "universe_hash",
                        "edgar_closure_hash",
                        "parser_version",
                        "dataset_schema_version",
                        "availability_policy",
                        "lag_days",
                        "writer_profile_version",
                        "pyarrow_version",
                    )
                }
                dimensions["dataset_schema_version"] = manifest.get("dataset_schema")
                dimensions["writer_profile_version"] = manifest.get("writer_profile")
                for key, manifest_key in (
                    ("dataset_schema_version", "dataset_schema"),
                    ("writer_profile_version", "writer_profile"),
                ):
                    if entry.get(key) != manifest.get(manifest_key):
                        raise SnapshotIntegrityError("catalog dimension mismatch")
                for key in (
                    "source_quarter",
                    "artifact_sha256",
                    "artifact_manifest_sha256",
                    "universe_hash",
                    "edgar_closure_hash",
                    "parser_version",
                    "availability_policy",
                    "lag_days",
                    "pyarrow_version",
                ):
                    if entry.get(key) != manifest.get(key):
                        raise SnapshotIntegrityError("catalog dimension mismatch")
                if partition_identity(**dimensions) != manifest.get("partition_identity"):
                    raise SnapshotIntegrityError("partition identity mismatch")
                validated = validate_tables(target)
                if validated["files"] != entry.get("file_hashes"):
                    raise SnapshotIntegrityError("catalog file hash mismatch")
                pq = __import__("pyarrow.parquet", fromlist=["ParquetFile"])
                actual_counts = {
                    n: pq.ParquetFile(target / f"{n}.parquet").metadata.num_rows
                    for n in ("fund_vintages", "holdings", "identifiers")
                }
                if manifest.get("row_counts") != actual_counts:
                    raise SnapshotIntegrityError("row count mismatch")
                receipt_key = (
                    manifest.get("artifact_sha256"),
                    manifest.get("artifact_manifest_sha256"),
                    manifest.get("universe_hash"),
                    manifest.get("edgar_closure_hash"),
                )
                if receipt_key not in receipt_dimensions.get(
                    str(manifest.get("source_quarter")), set()
                ):
                    raise CoverageError("partition has no matching fetch receipt")
                quality_path = target / "quality.json"
                if not quality_path.is_file() or hashlib.sha256(
                    quality_path.read_bytes()
                ).hexdigest() != manifest.get("quality_hash"):
                    raise SnapshotIntegrityError("quality artifact mismatch")
                indexed.add(target)
            disk = {
                p.resolve()
                for q in quarters
                for p in (
                    self.root / "core" / "sec-fund-holdings-pit-v1" / f"source_quarter={q}"
                ).glob("artifact=*/partition=*")
                if p.is_dir()
            }
            if disk != indexed & disk:
                raise CoverageError("unindexed partition")
        elif any(
            (self.root / "core" / "sec-fund-holdings-pit-v1").glob(
                "source_quarter=*/artifact=*/partition=*"
            )
        ):
            raise CoverageError("core catalog missing")
        return {
            "quarters": len(quarters),
            "artifacts_checked": checked,
            "partitions_checked": partitions,
        }

    def iter_validated_partitions(self, quarters: tuple[Quarter, ...]) -> list[dict[str, Any]]:
        self.validate_local(quarters)
        catalog_path = self.root / "core" / "sec-fund-holdings-pit-v1" / "catalog.json"
        if not catalog_path.is_file():
            return []
        entries = AppendOnlyIndex(catalog_path, "sec-core-catalog-v1").read()["entries"]
        requested_q = {str(q) for q in quarters}
        matched: list[dict[str, Any]] = [
            cast(dict[str, Any], e) for e in entries if str(e.get("source_quarter")) in requested_q
        ]
        matched.sort(
            key=lambda x: (
                str(x.get("source_quarter", "")),
                str(x.get("partition_identity", "")),
            )
        )
        return matched

    def inspect_local(
        self, quarter: Quarter, symbol: str | None = None, rows: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"quarter": str(quarter), "partitions": 0}
        root = self.root / "core" / "sec-fund-holdings-pit-v1" / f"source_quarter={quarter}"
        partitions = sorted(root.glob("artifact=*/partition=*")) if root.is_dir() else []
        result["partitions"] = len(partitions)
        # Inspection is deliberately fail-closed: a report must never be
        # presented from a partially validated local closure.
        self.validate_local((quarter,))
        from .core_dataset import validate_tables

        facts: list[dict[str, Any]] = []
        holdings: list[dict[str, Any]] = []
        identifiers: list[dict[str, Any]] = []
        counts: dict[str, int] = {"fund_vintages": 0, "holdings": 0, "identifiers": 0}
        metrics: list[dict[str, Any]] = []
        for partition in partitions:
            validated = validate_tables(partition)
            manifest = _read_json_object(partition / "manifest.json")
            quality = _read_json_object(partition / "quality.json")
            pq = __import__("pyarrow.parquet", fromlist=["ParquetFile"])
            fund_rows = pq.ParquetFile(partition / "fund_vintages.parquet").read().to_pylist()
            holding_rows = pq.ParquetFile(partition / "holdings.parquet").read().to_pylist()
            identifier_rows = pq.ParquetFile(partition / "identifiers.parquet").read().to_pylist()
            counts["fund_vintages"] += len(fund_rows)
            counts["holdings"] += len(holding_rows)
            counts["identifiers"] += len(identifier_rows)
            selected: Callable[[dict[str, Any]], bool] = lambda r: (
                symbol is None or r.get("fund_symbol") == symbol
            )
            facts.extend(
                {
                    k: r.get(k)
                    for k in (
                        "fund_symbol",
                        "cik",
                        "series_id",
                        "accession_number",
                        "report_date",
                        "filing_date",
                        "accepted_at",
                        "availability_anchor",
                        "availability_basis",
                        "availability_policy",
                        "availability_lag_days",
                        "quality_flags",
                    )
                }
                for r in fund_rows
                if selected(r)
            )
            if rows:
                holdings.extend(r for r in holding_rows if selected(r))
                identifiers.extend(r for r in identifier_rows if selected(r))
            qa = cast(dict[str, Any], quality.get("qa", {}))
            selected_holdings = [r for r in holding_rows if selected(r)]
            selected_identifiers = [r for r in identifier_rows if selected(r)]
            selected_funds = [r for r in fund_rows if selected(r)]
            selected_pct = sum(
                (r["percentage"] for r in selected_holdings if r.get("percentage") is not None),
                Decimal(0),
            )
            selected_covered = len(
                {r.get("holding_id") for r in selected_identifiers if r.get("holding_id")}
            )
            metrics.append(
                {
                    "partition_identity": manifest.get("partition_identity"),
                    "artifact_sha256": manifest.get("artifact_sha256"),
                    "manifest_hash": canonical_hash(manifest),
                    "file_hashes": validated["files"],
                    "logical_hashes": validated["logical_hashes"],
                    "quality_flags": quality.get("quality_flags", []),
                    "percentage_sum": qa.get("percentage_sum"),
                    "identifier_coverage_count": qa.get("identifier_coverage_count", 0),
                    "selected_percentage_sum": selected_pct,
                    "selected_identifier_coverage_count": selected_covered,
                    "selected_vintage_count": len(selected_funds),
                    "reconciliation_deltas": {
                        k: qa.get(k)
                        for k in (
                            "net_assets_delta",
                            "currency_value_net_assets_delta",
                            "accounting_delta",
                        )
                    },
                }
            )
        result["facts"] = json.loads(canonical_json(facts))
        result["table_counts"] = counts
        result["metrics"] = json.loads(canonical_json(metrics))
        if rows:
            result["rows"] = {
                "holdings": json.loads(canonical_json(holdings)),
                "identifiers": json.loads(canonical_json(identifiers)),
            }
        if symbol is not None:
            result["symbol"] = symbol
        if rows and "rows" not in result:
            result["rows"] = {"holdings": [], "identifiers": []}
        return result
