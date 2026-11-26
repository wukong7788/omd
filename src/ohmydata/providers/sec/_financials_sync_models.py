"""Typed models, receipts, and error-redaction helpers for incremental SEC financials sync."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ._event_discovery_models import SecDiscoveryCursor

SEC_DISCOVERY_CURSOR_SCHEMA = "sec-discovery-cursor-v1"
SEC_FINANCIALS_SYNC_RECEIPT_SCHEMA = "sec-financials-sync-receipt-v1"
DEFAULT_DISCOVERY_POLICY_VERSION = "sec-financials-sync-v1"

DEFAULT_SYNC_FORMS: tuple[str, ...] = (
    "10-K",
    "10-Q",
    "20-F",
    "40-F",
    "6-K",
    "10-K/A",
    "10-Q/A",
    "20-F/A",
    "40-F/A",
)

SUPPORTED_SYNC_FORMS: frozenset[str] = frozenset(
    {
        "10-K",
        "10-Q",
        "20-F",
        "40-F",
        "6-K",
        "8-K",
        "10-K/A",
        "10-Q/A",
        "20-F/A",
        "40-F/A",
        "6-K/A",
        "8-K/A",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_USER_AGENT_HEADER_RE = re.compile(r"(?i)user-agent\s*[:=]\s*[^\r\n;,]+")
_BEARER_RE = re.compile(r"(?i)(authorization|token|api[_-]?key|secret)\s*[:=]\s*[^\r\n;,]+")
_MAX_ERROR_MESSAGE_LEN = 512


class SecSyncOutcomeStatus(StrEnum):
    """Explicit per-CIK outcome classification for incremental financials sync."""

    SYNCED = "SYNCED"
    EMPTY = "EMPTY"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


def utc_iso(value: datetime) -> str:
    """Format a timezone-aware datetime as canonical UTC ISO-8601 with trailing Z."""
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime value must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def sanitize_error_message(
    exc: BaseException,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    """Return a bounded, secret-free diagnostic string for sync receipts."""
    raw = str(exc)
    cleaned = raw
    for secret in sensitive_values:
        if secret and len(secret) >= 3:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = _USER_AGENT_HEADER_RE.sub("User-Agent: [REDACTED]", cleaned)
    cleaned = _BEARER_RE.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _EMAIL_RE.sub("[REDACTED]", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return type(exc).__name__
    if len(cleaned) > _MAX_ERROR_MESSAGE_LEN:
        return cleaned[: _MAX_ERROR_MESSAGE_LEN - 3] + "..."
    return cleaned


@dataclass(frozen=True)
class SecCursorRecord:
    """Persisted per-CIK discovery cursor record (schema: sec-discovery-cursor-v1)."""

    cik: str
    cursor: SecDiscoveryCursor
    updated_at: datetime
    last_batch_id: str | None = None
    processed_accessions: tuple[str, ...] | None = None
    schema_version: str = SEC_DISCOVERY_CURSOR_SCHEMA

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]{10}", self.cik) or int(self.cik) <= 0:
            raise ValueError("cursor record cik must be 10 ASCII digits")
        if type(self.cursor) is not SecDiscoveryCursor:
            raise TypeError("cursor must be a SecDiscoveryCursor")
        if (
            type(self.updated_at) is not datetime
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
        ):
            raise ValueError("updated_at must be timezone-aware")
        object.__setattr__(self, "updated_at", self.updated_at.astimezone(UTC))
        if self.schema_version != SEC_DISCOVERY_CURSOR_SCHEMA:
            raise ValueError(f"unsupported cursor schema_version: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "cik": self.cik,
            "acceptance_at": utc_iso(self.cursor.acceptance_at),
            "accession_number": self.cursor.accession,
            "updated_at": utc_iso(self.updated_at),
            "last_batch_id": self.last_batch_id,
        }
        if self.processed_accessions is not None:
            payload["processed_accessions"] = list(self.processed_accessions)
        return payload


@dataclass(frozen=True)
class SecSyncCikReceipt:
    """Typed per-CIK outcome recorded inside a sync run receipt."""

    cik: str
    status: SecSyncOutcomeStatus
    reconciled: bool
    discovery_mode: str
    discovered_event_count: int
    new_event_count: int
    vintage_count: int
    batch_id: str | None
    prior_cursor: SecDiscoveryCursor | None
    new_cursor: SecDiscoveryCursor | None
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cik": self.cik,
            "status": self.status.value,
            "reconciled": self.reconciled,
            "discovery_mode": self.discovery_mode,
            "discovered_event_count": self.discovered_event_count,
            "new_event_count": self.new_event_count,
            "vintage_count": self.vintage_count,
            "batch_id": self.batch_id,
            "prior_cursor": (
                None if self.prior_cursor is None else self.prior_cursor.canonical_payload()
            ),
            "new_cursor": (
                None if self.new_cursor is None else self.new_cursor.canonical_payload()
            ),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class SecFinancialsSyncReceipt:
    """Immutable run receipt persisted at ``<root>/receipts/sync-<run_id>.json``."""

    schema_version: str
    run_id: str
    batch_id: str
    started_at: datetime
    completed_at: datetime
    as_of: datetime
    forms: tuple[str, ...]
    overlap_seconds: float
    ciks_requested: tuple[str, ...]
    synced: int
    empty: int
    transient_failure: int
    permanent_failure: int
    reconciled_ciks: tuple[str, ...]
    failed_ciks: tuple[str, ...]
    transient_failed_ciks: tuple[str, ...]
    permanent_failed_ciks: tuple[str, ...]
    batch_ids: tuple[str, ...]
    outcomes: tuple[SecSyncCikReceipt, ...]
    receipt_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "started_at": utc_iso(self.started_at),
            "completed_at": utc_iso(self.completed_at),
            "as_of": utc_iso(self.as_of),
            "forms": list(self.forms),
            "overlap_seconds": self.overlap_seconds,
            "ciks_requested": list(self.ciks_requested),
            "synced": self.synced,
            "empty": self.empty,
            "transient_failure": self.transient_failure,
            "permanent_failure": self.permanent_failure,
            "reconciled_ciks": list(self.reconciled_ciks),
            "failed_ciks": list(self.failed_ciks),
            "transient_failed_ciks": list(self.transient_failed_ciks),
            "permanent_failed_ciks": list(self.permanent_failed_ciks),
            "batch_ids": list(self.batch_ids),
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


__all__ = [
    "DEFAULT_DISCOVERY_POLICY_VERSION",
    "DEFAULT_SYNC_FORMS",
    "SEC_DISCOVERY_CURSOR_SCHEMA",
    "SEC_FINANCIALS_SYNC_RECEIPT_SCHEMA",
    "SUPPORTED_SYNC_FORMS",
    "SecCursorRecord",
    "SecFinancialsSyncReceipt",
    "SecSyncCikReceipt",
    "SecSyncOutcomeStatus",
    "sanitize_error_message",
    "utc_iso",
]
