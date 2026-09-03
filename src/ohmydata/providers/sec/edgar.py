"""Strict parsing of SEC EDGAR submissions JSON."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from .errors import CoverageError, SchemaMismatchError

_HIST = re.compile(r"^CIK[0-9]{10}-submissions-[0-9]{3}\.json$")
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_COLUMNS = (
    "accessionNumber",
    "form",
    "filingDate",
    "reportDate",
    "primaryDocument",
    "acceptanceDateTime",
)


@dataclass(frozen=True, init=False)
class SecPayloadReceipt:
    """Exact response bytes plus decoded JSON, for bounded replay accounting."""

    payload: dict[str, Any]
    observed_bytes: int
    _token: object = field(repr=False, compare=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("use SecPayloadReceipt.from_bytes")

    @classmethod
    def from_bytes(cls, body: bytes) -> SecPayloadReceipt:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaMismatchError("invalid submissions JSON") from exc
        if not isinstance(value, dict):
            raise SchemaMismatchError("submissions payload must be an object")
        obj = object.__new__(cls)
        object.__setattr__(obj, "payload", cast(dict[str, Any], value))
        object.__setattr__(obj, "observed_bytes", len(body))
        object.__setattr__(obj, "_token", _RECEIPT_TOKEN)
        return obj

    def is_authentic(self) -> bool:
        return self._token is _RECEIPT_TOKEN


_RECEIPT_TOKEN = object()


def _validate_cik(payload: dict[str, Any], cik: str) -> None:
    if not re.fullmatch(r"[0-9]{10}", cik) or str(payload.get("cik", "")).zfill(10) != cik:
        raise SchemaMismatchError("CIK mismatch")


def _rows(payload: dict[str, Any], cik: str) -> Iterable[dict[str, Any]]:
    _validate_cik(payload, cik)
    filings_raw = payload.get("filings")
    if type(filings_raw) is not dict:
        raise SchemaMismatchError("filings must be an object")
    filings = cast(dict[str, Any], filings_raw)
    recent_raw = filings.get("recent")
    if type(recent_raw) is not dict:
        raise SchemaMismatchError("recent filings must be an object")
    recent = cast(dict[str, Any], recent_raw)
    if any(k not in recent for k in _COLUMNS):
        raise SchemaMismatchError("submission columns missing")
    if any(type(recent[k]) is not list for k in _COLUMNS):
        raise SchemaMismatchError("submission columns must be lists")
    columns = {k: cast(list[Any], recent[k]) for k in _COLUMNS}
    n = len(columns["accessionNumber"])
    if any(not isinstance(recent[k], list) or len(columns[k]) != n for k in _COLUMNS):
        raise SchemaMismatchError("submission column length mismatch")
    for i in range(n):
        row: dict[str, Any] = {k: columns[k][i] for k in _COLUMNS}
        if not isinstance(row["accessionNumber"], str) or not _ACCESSION.fullmatch(
            row["accessionNumber"]
        ):
            raise SchemaMismatchError("invalid accession")
        yield row


def _child_rows(child: dict[str, Any], cik: str) -> Iterable[dict[str, Any]]:
    if "filings" in child:
        yield from _rows(child, cik)
        return
    if "cik" in child:
        _validate_cik(child, cik)
    if any(k not in child for k in _COLUMNS):
        raise SchemaMismatchError("submission columns missing")
    if any(type(child[k]) is not list for k in _COLUMNS):
        raise SchemaMismatchError("submission columns must be lists")
    columns = {k: cast(list[Any], child[k]) for k in _COLUMNS}
    n = len(columns["accessionNumber"])
    if any(not isinstance(child[k], list) or len(columns[k]) != n for k in _COLUMNS):
        raise SchemaMismatchError("submission column length mismatch")
    for i in range(n):
        row: dict[str, Any] = {k: columns[k][i] for k in _COLUMNS}
        if not isinstance(row["accessionNumber"], str) or not _ACCESSION.fullmatch(
            row["accessionNumber"]
        ):
            raise SchemaMismatchError("invalid accession")
        yield row


def parse_submissions(
    payload: dict[str, Any],
    cik: str,
    required_accessions: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Return exact required filing metadata from the recent payload only."""
    required = tuple(required_accessions)
    if not required or any(not _ACCESSION.fullmatch(a) for a in required):
        raise ValueError("invalid required accessions")
    result: dict[str, dict[str, Any]] = {}
    for row in _rows(payload, cik):
        if row["accessionNumber"] in required:
            result[row["accessionNumber"]] = row
    if set(result) != set(required):
        raise CoverageError("required accession missing")
    return result


def resolve_submissions(
    payload: dict[str, Any],
    cik: str,
    required_accessions: tuple[str, ...],
    *,
    load: Callable[[str], dict[str, Any] | bytes | SecPayloadReceipt],
    max_files: int = 16,
    max_bytes: int = 128 * 1024**2,
) -> dict[str, dict[str, Any]]:
    """Resolve required filings through bounded official history files.

    ``load`` receives only canonical URLs generated from validated basenames.
    The callback is deliberately injected so the parser remains offline-testable.
    """
    required = tuple(required_accessions)
    found: dict[str, dict[str, Any]] = {}
    for row in _rows(payload, cik):
        if row["accessionNumber"] in required:
            found[row["accessionNumber"]] = row
    if set(found) == set(required):
        return found
    refs = historical_basenames(payload, cik)
    if len(refs) > max_files:
        raise SchemaMismatchError("historical file limit exceeded")
    seen: set[str] = set()
    observed = 0
    for basename in refs:
        if basename in seen:
            raise SchemaMismatchError("historical reference cycle")
        seen.add(basename)
        url = historical_submission_url(cik, basename)
        loaded = load(url)
        if isinstance(loaded, SecPayloadReceipt):
            if not loaded.is_authentic():
                raise SchemaMismatchError("unauthentic submissions receipt")
            child, size = loaded.payload, loaded.observed_bytes
        elif isinstance(loaded, bytes):
            receipt = SecPayloadReceipt.from_bytes(loaded)
            child, size = receipt.payload, receipt.observed_bytes
        elif type(loaded) is dict:
            child, size = loaded, None
        else:
            raise SchemaMismatchError("invalid historical submissions payload")
        if size is None:
            raise SchemaMismatchError("historical loader must provide observed bytes")
        observed += size
        if observed > max_bytes:
            raise SchemaMismatchError("historical submissions bytes exceeded")
        for row in _child_rows(child, cik):
            if row["accessionNumber"] in required:
                old = found.get(row["accessionNumber"])
                if old is not None and old != row:
                    raise SchemaMismatchError("conflicting submission metadata")
                found[row["accessionNumber"]] = row
        if set(found) == set(required):
            break
    if set(found) != set(required):
        raise CoverageError("required accession missing")
    return found


def historical_submission_url(cik: str, basename: str) -> str:
    if (
        not re.fullmatch(r"[0-9]{10}", cik)
        or not _HIST.fullmatch(basename)
        or not basename.startswith(f"CIK{cik}-")
    ):
        raise SchemaMismatchError("invalid historical submissions reference")
    return f"https://data.sec.gov/submissions/{basename}"


def historical_basenames(payload: dict[str, Any], cik: str) -> tuple[str, ...]:
    _validate_cik(payload, cik)
    filings = cast(dict[str, Any], payload.get("filings", {}))
    refs = cast(list[Any], filings.get("files", []))
    if len(refs) > 16:
        raise SchemaMismatchError("historical file limit exceeded")
    names: list[str] = []
    for raw_ref in refs:
        if not isinstance(raw_ref, dict):
            raise SchemaMismatchError("invalid historical reference")
        ref = cast(dict[str, Any], raw_ref)
        if not isinstance(ref.get("name"), str):
            raise SchemaMismatchError("invalid historical reference")
        name = ref["name"]
        historical_submission_url(cik, name)
        if name in names:
            raise SchemaMismatchError("duplicate historical reference")
        names.append(name)
    return tuple(names)


__all__ = [
    "SecPayloadReceipt",
    "historical_basenames",
    "historical_submission_url",
    "parse_submissions",
    "resolve_submissions",
]
