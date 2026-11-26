"""CIK universe loader, injectable SEC rate limiter, and atomic cursor store."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

from ._event_discovery_models import SecDiscoveryCursor
from ._financials_sync_models import SEC_DISCOVERY_CURSOR_SCHEMA, SecCursorRecord, utc_iso
from .batch import atomic_json, canonical_json
from .errors import SchemaMismatchError

P = ParamSpec("P")
R = TypeVar("R")

_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURSOR_FILE_RE = re.compile(r"^cik=([0-9]{10})\.json$")
_MAX_SEC_RPS = 10.0
_MAX_PROCESSED_ACCESSIONS = 20_000


def normalize_cik(value: object) -> str:
    """Validate and normalize a CIK to a 10-digit zero-padded ASCII string."""
    if isinstance(value, bool):
        raise ValueError("invalid CIK: boolean is not allowed")  # noqa: TRY004
    if isinstance(value, int):
        if value <= 0 or value > 9_999_999_999:
            raise ValueError(f"invalid CIK integer out of range: {value}")
        return str(value).zfill(10)
    if not isinstance(value, str):
        raise ValueError(f"invalid CIK type: {type(value).__name__}")  # noqa: TRY004
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > 10
        or not cleaned.isascii()
        or not cleaned.isdigit()
        or int(cleaned) <= 0
    ):
        raise ValueError(f"invalid CIK value: {value!r}")
    return cleaned.zfill(10)


def _deduplicate_ciks(raw_items: Iterable[object]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw_items:
        norm = normalize_cik(item)
        if norm not in seen:
            seen.add(norm)
            ordered.append(norm)
    if not ordered:
        raise ValueError("CIK universe cannot be empty")
    return tuple(ordered)


def _parse_txt_universe(text: str) -> tuple[str, ...]:
    raw_items: list[str] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        without_comment = raw_line.split("#", 1)[0].strip()
        if not without_comment:
            continue
        try:
            raw_items.append(normalize_cik(without_comment))
        except ValueError as exc:
            raise ValueError(f"malformed CIK at line {line_no}: {raw_line.strip()!r}") from exc
    return _deduplicate_ciks(raw_items)


def _parse_json_universe(text: str) -> tuple[str, ...]:
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON CIK universe: {exc}") from exc

    items: list[object]
    if isinstance(data, list):
        items = cast(list[object], data)
    elif isinstance(data, dict):
        if "ciks" not in data or not isinstance(data["ciks"], list):
            raise ValueError("JSON CIK universe object must contain a 'ciks' list")
        items = cast(list[object], data["ciks"])
    else:
        raise ValueError(  # noqa: TRY004
            "JSON CIK universe must be a list or an object with a 'ciks' list"
        )

    raw_items: list[object] = []
    for idx, entry in enumerate(items):
        if isinstance(entry, dict):
            if "cik" not in entry:
                raise ValueError(f"JSON CIK universe entry {idx} is missing 'cik' key")
            raw_items.append(entry["cik"])
        else:
            raw_items.append(entry)
    return _deduplicate_ciks(raw_items)


def _parse_csv_universe(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    if not lines:
        raise ValueError("CIK universe cannot be empty")
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        raise ValueError("CSV CIK universe is missing header row")
    cik_columns = [
        name for name in reader.fieldnames if name is not None and name.strip().lower() == "cik"
    ]
    if len(cik_columns) != 1:
        raise ValueError("CSV CIK universe must contain exactly one 'cik' column")
    cik_col = cik_columns[0]

    raw_items: list[str] = []
    for row_no, row in enumerate(reader, start=2):
        values = [str(v).strip() for v in row.values() if v is not None]
        if all(not v for v in values):
            continue
        cell = row.get(cik_col)
        if cell is None or not str(cell).strip():
            raise ValueError(f"missing CIK value at CSV row {row_no}")
        try:
            raw_items.append(normalize_cik(cell))
        except ValueError as exc:
            raise ValueError(f"malformed CIK at CSV row {row_no}: {cell!r}") from exc
    return _deduplicate_ciks(raw_items)


def load_cik_universe(path: str | Path) -> tuple[str, ...]:
    """Load, validate, and normalize a CIK universe from ``.txt``, ``.json``, or ``.csv``."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"CIK universe file not found: {p}")
    text = p.read_text(encoding="utf-8-sig")
    suffix = p.suffix.lower()
    if suffix == ".txt":
        return _parse_txt_universe(text)
    if suffix == ".json":
        return _parse_json_universe(text)
    if suffix == ".csv":
        return _parse_csv_universe(text)
    raise ValueError(f"unsupported CIK universe file extension: {p.suffix!r}")


class SecRateLimiter:
    """Thread-safe, injectable minimum-interval rate limiter respecting SEC fair-access limits."""

    def __init__(
        self,
        max_requests_per_second: float = 6.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(max_requests_per_second, bool) or not isinstance(
            max_requests_per_second, (int, float)
        ):
            raise ValueError("max_requests_per_second must be a finite number")  # noqa: TRY004
        rate = float(max_requests_per_second)
        if not math.isfinite(rate) or rate <= 0.0 or rate > _MAX_SEC_RPS:
            raise ValueError(f"max_requests_per_second must be in (0, {_MAX_SEC_RPS}]")
        if not callable(clock) or not callable(sleep):
            raise TypeError("clock and sleep must be callable")
        self.max_requests_per_second = rate
        self.min_interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed_at: float | None = None
        self._acquired_count = 0
        self._total_sleep_seconds = 0.0

    @property
    def acquired_count(self) -> int:
        with self._lock:
            return self._acquired_count

    @property
    def total_sleep_seconds(self) -> float:
        with self._lock:
            return self._total_sleep_seconds

    def acquire(self) -> float:
        """Reserve the next rate-limit slot and sleep outside the lock if needed."""
        with self._lock:
            now = float(self._clock())
            if self._next_allowed_at is None or now >= self._next_allowed_at:
                delay = 0.0
                slot = now
            else:
                slot = self._next_allowed_at
                delay = slot - now
            self._next_allowed_at = slot + self.min_interval
            self._acquired_count += 1
            self._total_sleep_seconds += delay
        if delay > 0.0:
            self._sleep(delay)
        return delay

    def __call__(self) -> float:
        """Callable hook compatible with ``SecHttpClient(..., limiter=rate_limiter)``."""
        return self.acquire()

    def call(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        """Acquire a rate-limit slot and invoke ``fn(*args, **kwargs)``."""
        self.acquire()
        return fn(*args, **kwargs)

    def wrap(self, fn: Callable[P, R]) -> Callable[P, R]:
        """Return a rate-limited wrapper around ``fn``."""

        def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            self.acquire()
            return fn(*args, **kwargs)

        return _wrapped


def _parse_utc_iso(raw: object, field_name: str) -> datetime:
    if not isinstance(raw, str) or "T" not in raw or len(raw) > 64:
        raise SchemaMismatchError(f"invalid cursor {field_name}")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SchemaMismatchError(f"invalid cursor {field_name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaMismatchError(f"cursor {field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _strict_cursor_json(text: str) -> dict[str, Any]:
    def _no_dups(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise ValueError(f"duplicate JSON key: {k}")
            out[k] = v
        return out

    try:
        parsed: Any = json.loads(
            text,
            object_pairs_hook=_no_dups,
            parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise SchemaMismatchError("corrupted cursor JSON") from exc
    if not isinstance(parsed, dict):
        raise SchemaMismatchError("cursor JSON must be an object")
    return cast(dict[str, Any], parsed)


class SecCursorStore:
    """Atomic, fail-closed per-CIK persistence for ``SecDiscoveryCursor``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.cursors_dir = self.root / "cursors"

    def cursor_path(self, cik: str) -> Path:
        return self.cursors_dir / f"cik={normalize_cik(cik)}.json"

    def load_record(self, cik: str) -> SecCursorRecord | None:
        norm = normalize_cik(cik)
        path = self.cursor_path(norm)
        if not path.exists():
            return None
        if not path.is_file():
            raise SchemaMismatchError(f"cursor path is not a regular file for CIK {norm}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SchemaMismatchError(f"unable to read cursor for CIK {norm}") from exc

        payload = _strict_cursor_json(text)
        required_keys = {
            "schema_version",
            "cik",
            "acceptance_at",
            "accession_number",
            "updated_at",
            "last_batch_id",
        }
        allowed_keys = required_keys | {"processed_accessions"}
        if not required_keys.issubset(payload) or not set(payload).issubset(allowed_keys):
            raise SchemaMismatchError(f"invalid cursor schema keys for CIK {norm}")
        if payload["schema_version"] != SEC_DISCOVERY_CURSOR_SCHEMA:
            raise SchemaMismatchError(f"unsupported cursor schema version for CIK {norm}")
        if payload["cik"] != norm:
            raise SchemaMismatchError(
                f"cursor CIK mismatch: expected {norm}, found {payload['cik']!r}"
            )

        accession = payload["accession_number"]
        if not isinstance(accession, str) or _ACCESSION_RE.fullmatch(accession) is None:
            raise SchemaMismatchError(f"invalid cursor accession_number for CIK {norm}")
        acceptance_at = _parse_utc_iso(payload["acceptance_at"], "acceptance_at")
        updated_at = _parse_utc_iso(payload["updated_at"], "updated_at")

        last_batch_id = payload["last_batch_id"]
        if last_batch_id is not None and (
            not isinstance(last_batch_id, str) or _SAFE_ID_RE.fullmatch(last_batch_id) is None
        ):
            raise SchemaMismatchError(f"invalid cursor last_batch_id for CIK {norm}")

        processed_accessions: tuple[str, ...] | None = None
        if "processed_accessions" in payload and payload["processed_accessions"] is not None:
            raw_acc = payload["processed_accessions"]
            if not isinstance(raw_acc, list) or len(raw_acc) > _MAX_PROCESSED_ACCESSIONS:
                raise SchemaMismatchError(f"invalid cursor processed_accessions for CIK {norm}")
            checked: list[str] = []
            for item in cast(list[object], raw_acc):
                if not isinstance(item, str) or _ACCESSION_RE.fullmatch(item) is None:
                    raise SchemaMismatchError(f"invalid accession in cursor for CIK {norm}")
                checked.append(item)
            if checked != sorted(set(checked)):
                raise SchemaMismatchError(f"unsorted or duplicate processed_accessions for {norm}")
            processed_accessions = tuple(checked)

        cursor = SecDiscoveryCursor(acceptance_at=acceptance_at, accession=accession)
        return SecCursorRecord(
            cik=norm,
            cursor=cursor,
            updated_at=updated_at,
            last_batch_id=last_batch_id,
            processed_accessions=processed_accessions,
        )

    def load(self, cik: str) -> SecDiscoveryCursor | None:
        record = self.load_record(cik)
        return None if record is None else record.cursor

    def save(
        self,
        cik: str,
        cursor: SecDiscoveryCursor,
        *,
        updated_at: datetime,
        last_batch_id: str | None = None,
        processed_accessions: Iterable[str] | None = None,
    ) -> SecCursorRecord:
        norm = normalize_cik(cik)
        if type(cursor) is not SecDiscoveryCursor:
            raise TypeError("cursor must be a SecDiscoveryCursor")
        if (
            type(updated_at) is not datetime
            or updated_at.tzinfo is None
            or updated_at.utcoffset() is None
        ):
            raise ValueError("updated_at must be timezone-aware")
        if last_batch_id is not None and (
            not isinstance(last_batch_id, str) or _SAFE_ID_RE.fullmatch(last_batch_id) is None
        ):
            raise ValueError(f"invalid last_batch_id: {last_batch_id!r}")

        normalized_processed: tuple[str, ...] | None = None
        if processed_accessions is not None:
            validated: set[str] = set()
            for acc in processed_accessions:
                if not isinstance(acc, str) or _ACCESSION_RE.fullmatch(acc) is None:
                    raise ValueError(f"invalid processed accession: {acc!r}")
                validated.add(acc)
            if len(validated) > _MAX_PROCESSED_ACCESSIONS:
                raise ValueError("processed_accessions limit exceeded")
            normalized_processed = tuple(sorted(validated))

        record = SecCursorRecord(
            cik=norm,
            cursor=cursor,
            updated_at=updated_at.astimezone(UTC),
            last_batch_id=last_batch_id,
            processed_accessions=normalized_processed,
        )
        atomic_json(self.cursor_path(norm), record.to_dict())
        return record

    def list_ciks(self) -> tuple[str, ...]:
        if not self.cursors_dir.is_dir():
            return ()
        ciks: list[str] = []
        for entry in sorted(self.cursors_dir.iterdir()):
            if not entry.is_file():
                continue
            match = _CURSOR_FILE_RE.fullmatch(entry.name)
            if match is None:
                raise SchemaMismatchError(f"unexpected file in cursor directory: {entry.name}")
            ciks.append(match.group(1))
        return tuple(ciks)


def write_immutable_json_exclusive(path: str | Path, value: Any) -> Path:
    """Write canonical JSON atomically, failing closed if the target already exists."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SchemaMismatchError(f"immutable artifact already exists: {target.name}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(value))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp_name, target)
        except FileExistsError as exc:
            raise SchemaMismatchError(f"immutable artifact already exists: {target.name}") from exc
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


__all__ = [
    "SecCursorRecord",
    "SecCursorStore",
    "SecRateLimiter",
    "load_cik_universe",
    "normalize_cik",
    "utc_iso",
    "write_immutable_json_exclusive",
]
