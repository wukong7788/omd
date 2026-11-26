"""Bounded, path-safe offline readers for SEC bulk companyfacts.zip and submissions.zip."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ._bulk_zip_models import SecBulkArchiveProvenance
from ._companyfacts_models import (
    _ACCESSION,
    SecCompanyFactsFiling,
    _accepted,
    _date,
)
from .edgar import _child_rows, _rows, historical_basenames
from .errors import ResourceLimitError, SchemaMismatchError

DEFAULT_MAX_ZIP_ENTRY_BYTES = 128 * 1024**2
DEFAULT_MAX_ZIP_TOTAL_BYTES = 4 * 1024**3
DEFAULT_MAX_HISTORY_PAGES_PER_CIK = 256
DEFAULT_MAX_ZIP_MEMBERS = 2_000_000
_CHUNK_BYTES = 65_536
_HASH_CHUNK_BYTES = 1024 * 1024
_COMPANYFACTS_MEMBER = re.compile(r"^CIK([0-9]{10})\.json$")
_SUBMISSIONS_ROOT_MEMBER = re.compile(r"^CIK([0-9]{10})\.json$")
_SUBMISSIONS_HIST_MEMBER = re.compile(r"^CIK([0-9]{10})-submissions-[0-9]{3}\.json$")
_SAFE_DOC_BASENAME = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


class BulkZipSecurityError(SchemaMismatchError):
    """Archive-level ZIP structural or path-traversal violation."""


class BulkZipBombError(ResourceLimitError):
    """Archive-level decompressed byte or member-count limit exceeded."""


@dataclass(frozen=True)
class SecBulkSubmissionMeta:
    cik: str
    entity_name: str | None
    entity_type: str | None
    symbols: tuple[str, ...]
    filings: dict[str, SecCompanyFactsFiling]
    history_pages_read: int


def sha256_file_streaming(path: Path) -> SecBulkArchiveProvenance:
    """Compute SHA-256 digest and byte size of an archive without loading it into RAM."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return SecBulkArchiveProvenance(path.name, digest.hexdigest(), total)


def validate_zip_member_name(name: str) -> str:
    """Reject path traversal, absolute paths, backslashes, drive prefixes, and control chars."""
    if type(name) is not str or not name:
        raise BulkZipSecurityError("ZIP member name is empty or non-string")
    if name.startswith("/") or "\\" in name or ":" in name or "\x00" in name:
        raise BulkZipSecurityError(f"unsafe ZIP member path: {name!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise BulkZipSecurityError(f"ZIP member contains control characters: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts if not (name.endswith("/") and part == "")):
        raise BulkZipSecurityError(f"unsafe ZIP member path traversal: {name!r}")
    if ".." in parts:
        raise BulkZipSecurityError(f"unsafe ZIP member path traversal: {name!r}")
    return name


def validate_zip_archive_safety(
    zf: zipfile.ZipFile,
    *,
    max_members: int = DEFAULT_MAX_ZIP_MEMBERS,
) -> dict[str, zipfile.ZipInfo]:
    """Validate every member of a ZIP archive against traversal, symlinks, and duplicates."""
    infos = zf.infolist()
    if len(infos) > max_members:
        raise BulkZipBombError("ZIP archive member count exceeds limit")
    index: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        validate_zip_member_name(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise BulkZipSecurityError(f"ZIP archive contains symlink member: {info.filename!r}")
        if info.filename in index:
            raise BulkZipSecurityError(f"ZIP archive contains duplicate member: {info.filename!r}")
        index[info.filename] = info
    return index


def index_cik_members(index: dict[str, zipfile.ZipInfo]) -> dict[str, str]:
    """Map 10-digit CIK strings to validated root ``CIK##########.json`` member names."""
    mapping: dict[str, str] = {}
    for name in index:
        match = _COMPANYFACTS_MEMBER.fullmatch(name)
        if match is not None:
            mapping[match.group(1)] = name
    return mapping


class BoundedZipReader:
    """Stream-reads ZIP entries with strict per-entry and cumulative byte limits."""

    def __init__(
        self,
        *,
        max_entry_bytes: int = DEFAULT_MAX_ZIP_ENTRY_BYTES,
        max_total_bytes: int = DEFAULT_MAX_ZIP_TOTAL_BYTES,
    ) -> None:
        if type(max_entry_bytes) is not int or max_entry_bytes < 1:
            raise ValueError("max_entry_bytes must be a positive integer")
        if type(max_total_bytes) is not int or max_total_bytes < 1:
            raise ValueError("max_total_bytes must be a positive integer")
        self.max_entry_bytes = max_entry_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes_read = 0

    def read_entry(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        validate_zip_member_name(info.filename)
        if info.file_size > self.max_entry_bytes:
            raise BulkZipBombError(
                f"ZIP entry {info.filename} declared size exceeds per-entry byte limit"
            )
        if self.total_bytes_read + info.file_size > self.max_total_bytes:
            raise BulkZipBombError("ZIP archive cumulative decompressed bytes exceed limit")
        chunks: list[bytes] = []
        entry_bytes = 0
        with zf.open(info, "r") as stream:
            while True:
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                entry_bytes += len(chunk)
                self.total_bytes_read += len(chunk)
                if entry_bytes > self.max_entry_bytes:
                    raise BulkZipBombError(
                        f"ZIP entry {info.filename} decompressed size exceeds per-entry byte limit"
                    )
                if self.total_bytes_read > self.max_total_bytes:
                    raise BulkZipBombError("ZIP archive cumulative decompressed bytes exceed limit")
                chunks.append(chunk)
        return b"".join(chunks)


def decode_strict_json(body: bytes, *, context: str) -> dict[str, Any]:
    """Decode UTF-8 JSON object rejecting duplicate keys and non-finite constants."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SchemaMismatchError(f"duplicate key in {context} JSON")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        if isinstance(exc, SchemaMismatchError):
            raise
        raise SchemaMismatchError(f"invalid {context} JSON") from exc
    if type(parsed) is not dict:
        raise SchemaMismatchError(f"{context} payload must be a JSON object")
    return parsed


def _clean_symbols(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        if isinstance(item, str):
            sym = item.strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                cleaned.append(sym)
    return tuple(cleaned)


def _ingest_submission_rows(
    rows: Iterable[dict[str, Any]],
    *,
    cik: str,
    allowed_forms: frozenset[str],
    acceptance_upper: datetime | None,
    by_accession: dict[str, SecCompanyFactsFiling],
) -> None:
    for row in rows:
        accession = row.get("accessionNumber")
        form = row.get("form")
        if (
            type(accession) is not str
            or not _ACCESSION.fullmatch(accession)
            or type(form) is not str
            or form not in allowed_forms
        ):
            continue
        raw_report = row.get("reportDate")
        raw_accepted = row.get("acceptanceDateTime")
        raw_filed = row.get("filingDate")
        if not raw_report or not raw_accepted or not raw_filed:
            # Never fabricate PIT timestamps or report dates when missing on legacy rows.
            continue
        report_date = _date(raw_report, "reportDate")
        accepted_at = _accepted(raw_accepted)
        if acceptance_upper is not None and accepted_at > acceptance_upper:
            continue
        filing_date = _date(raw_filed, "filingDate")
        document = row.get("primaryDocument")
        if document == "":
            document = None
        if document is not None and (
            type(document) is not str
            or not _SAFE_DOC_BASENAME.fullmatch(document)
            or document in {".", ".."}
        ):
            raise SchemaMismatchError("SEC primary document is not a safe basename")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{document}"
            if document is not None
            else None
        )
        filing = SecCompanyFactsFiling(
            accession, form, report_date, accepted_at, document, url, filing_date
        )
        old = by_accession.get(accession)
        if old is not None and old != filing:
            raise SchemaMismatchError("conflicting SEC accession metadata across submissions pages")
        by_accession[accession] = filing


def load_submissions_from_zip(
    zf: zipfile.ZipFile,
    index: dict[str, zipfile.ZipInfo],
    reader: BoundedZipReader,
    cik: str,
    *,
    allowed_forms: frozenset[str],
    acceptance_upper: datetime | None,
    max_history_pages: int = DEFAULT_MAX_HISTORY_PAGES_PER_CIK,
) -> SecBulkSubmissionMeta:
    """Load root and all historical paginated submissions JSON files for one CIK from ZIP."""
    root_name = f"CIK{cik}.json"
    root_info = index[root_name]
    root_payload = decode_strict_json(
        reader.read_entry(zf, root_info), context=f"SEC submissions {root_name}"
    )
    by_accession: dict[str, SecCompanyFactsFiling] = {}
    _ingest_submission_rows(
        _rows(root_payload, cik),
        cik=cik,
        allowed_forms=allowed_forms,
        acceptance_upper=acceptance_upper,
        by_accession=by_accession,
    )
    refs = historical_basenames(root_payload, cik)
    if len(refs) > max_history_pages:
        raise ResourceLimitError(f"SEC submissions history page count exceeds limit for CIK {cik}")
    pages_read = 0
    for basename in refs:
        if _SUBMISSIONS_HIST_MEMBER.fullmatch(basename) is None:
            raise SchemaMismatchError(f"invalid historical submissions basename {basename!r}")
        child_info = index.get(basename)
        if child_info is None:
            raise SchemaMismatchError(
                f"referenced historical submissions page {basename} missing from submissions.zip"
            )
        child_payload = decode_strict_json(
            reader.read_entry(zf, child_info), context=f"SEC submissions history {basename}"
        )
        _ingest_submission_rows(
            _child_rows(child_payload, cik),
            cik=cik,
            allowed_forms=allowed_forms,
            acceptance_upper=acceptance_upper,
            by_accession=by_accession,
        )
        pages_read += 1
    raw_name = root_payload.get("name")
    entity_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
    raw_type = root_payload.get("entityType")
    entity_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else None
    symbols = _clean_symbols(root_payload.get("tickers"))
    return SecBulkSubmissionMeta(
        cik=cik,
        entity_name=entity_name,
        entity_type=entity_type,
        symbols=symbols,
        filings=by_accession,
        history_pages_read=pages_read,
    )


__all__ = [
    "DEFAULT_MAX_HISTORY_PAGES_PER_CIK",
    "DEFAULT_MAX_ZIP_ENTRY_BYTES",
    "DEFAULT_MAX_ZIP_MEMBERS",
    "DEFAULT_MAX_ZIP_TOTAL_BYTES",
    "BoundedZipReader",
    "BulkZipBombError",
    "BulkZipSecurityError",
    "SecBulkArchiveProvenance",
    "SecBulkSubmissionMeta",
    "decode_strict_json",
    "index_cik_members",
    "load_submissions_from_zip",
    "sha256_file_streaming",
    "validate_zip_archive_safety",
    "validate_zip_member_name",
]
