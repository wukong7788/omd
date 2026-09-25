"""Offline, retained-snapshot discovery of SEC filing acceptance events."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, cast

from ...core.snapshot import SnapshotMode, SnapshotObservationRef, SnapshotStore
from .errors import SchemaMismatchError

_SCHEMA = 1
_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"})
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_ITEM = re.compile(r"^[0-9]+\.[0-9]+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_SOURCE_BYTES = 8 * 1024**2
_MAX_TOTAL_BYTES = 64 * 1024**2
_MAX_ROWS = 100_000
_MAX_EVENTS = 10_000
_MAX_HISTORICAL_SOURCES = 256
_MAX_STRING = 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 500_000


if TYPE_CHECKING:
    from .event_discovery import _DiscoveryReplayCache


class SecDiscoveryMode(str, Enum):
    INCREMENTAL = "INCREMENTAL"
    RECONCILE = "RECONCILE"


class SecRootCoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    NEEDS_RECONCILE = "NEEDS_RECONCILE"


class SecFilingEventKind(str, Enum):
    SEC_FILING_ACCEPTED = "SEC_FILING_ACCEPTED"
    SEC_8K_ITEM_2_02 = "SEC_8K_ITEM_2_02"


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or len(value) > _MAX_STRING or "T" not in value:
        raise SchemaMismatchError(f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SchemaMismatchError(f"invalid {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaMismatchError(f"invalid {name}")
    return parsed.astimezone(UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True)
class SecDiscoveryPolicy:
    cik: str
    forms: tuple[str, ...]
    acceptance_lower: datetime
    acceptance_upper: datetime
    overlap: timedelta
    mode: SecDiscoveryMode
    version: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[1-9][0-9]*", self.cik) or len(self.cik) > 10:
            raise ValueError("CIK must be canonical zero-stripped digits")
        if not isinstance(self.forms, tuple) or len(self.forms) > len(_FORMS):
            raise ValueError("invalid selected forms")
        forms = self.forms
        if not forms or len(set(forms)) != len(forms) or any(item not in _FORMS for item in forms):
            raise ValueError("invalid selected forms")
        if type(self.overlap) is not timedelta or self.overlap < timedelta(0):
            raise ValueError("overlap must be nonnegative")
        if type(self.mode) is not SecDiscoveryMode:
            raise TypeError("invalid discovery mode")
        if (
            not isinstance(self.version, str)
            or not self.version
            or len(self.version.encode()) > _MAX_STRING
        ):
            raise ValueError("invalid policy version")
        lower, upper = (
            _utc(self.acceptance_lower, "acceptance_lower"),
            _utc(self.acceptance_upper, "acceptance_upper"),
        )
        if lower > upper:
            raise ValueError("acceptance window is inverted")
        object.__setattr__(self, "forms", tuple(sorted(forms)))
        object.__setattr__(self, "acceptance_lower", lower)
        object.__setattr__(self, "acceptance_upper", upper)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "cik": self.cik,
            "forms": list(self.forms),
            "acceptance_lower": _stamp(self.acceptance_lower),
            "acceptance_upper": _stamp(self.acceptance_upper),
            "overlap_microseconds": self.overlap.days * 86_400_000_000
            + self.overlap.seconds * 1_000_000
            + self.overlap.microseconds,
            "mode": self.mode.value,
            "version": self.version,
        }

    @property
    def policy_identity(self) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "cik": self.cik,
                    "forms": list(self.forms),
                    "overlap_microseconds": self.overlap.days * 86_400_000_000
                    + self.overlap.seconds * 1_000_000
                    + self.overlap.microseconds,
                    "version": self.version,
                }
            )
        ).hexdigest()


@dataclass(frozen=True)
class SecDiscoveryCursor:
    acceptance_at: datetime
    accession: str

    def __post_init__(self) -> None:
        if not _ACCESSION.fullmatch(self.accession):
            raise ValueError("invalid accession")
        object.__setattr__(self, "acceptance_at", _utc(self.acceptance_at, "acceptance_at"))

    def canonical_payload(self) -> dict[str, str]:
        return {"acceptance_at": _stamp(self.acceptance_at), "accession": self.accession}


@dataclass(frozen=True)
class SecDiscoverySource:
    url: str
    observation: SnapshotObservationRef

    def canonical_payload(self) -> dict[str, object]:
        ref = self.observation
        return {
            "url": self.url,
            "observation": {
                "provider": ref.provider,
                "endpoint": ref.endpoint,
                "request_identity": ref.request_identity,
                "response_sha256": ref.response_sha256,
                "serialization_identifier": ref.serialization_identifier,
                "snapshot_identity": ref.snapshot_identity,
                "fact_version": ref.fact_version,
                "mode": ref.mode.value,
                "snapshot_fetched_at": _stamp(ref.snapshot_fetched_at),
                "observation_identity": ref.observation_identity,
            },
        }

    @classmethod
    def from_canonical_payload(
        cls,
        payload: Mapping[str, object],
        store: SnapshotStore,
        _replay_cache: _DiscoveryReplayCache | None = None,
    ) -> SecDiscoverySource:
        if (
            set(payload) != {"url", "observation"}
            or not isinstance(payload["url"], str)
            or not isinstance(payload["observation"], Mapping)
        ):
            raise SchemaMismatchError("invalid discovery source")
        raw = cast(Mapping[str, object], payload["observation"])
        keys = {
            "provider",
            "endpoint",
            "request_identity",
            "response_sha256",
            "serialization_identifier",
            "snapshot_identity",
            "fact_version",
            "mode",
            "snapshot_fetched_at",
            "observation_identity",
        }
        if set(raw) != keys or any(not isinstance(raw[key], str) for key in keys):
            raise SchemaMismatchError("invalid discovery observation")
        identifiers = (
            "request_identity",
            "response_sha256",
            "snapshot_identity",
            "fact_version",
            "observation_identity",
        )
        if (
            any(_HEX64.fullmatch(cast(str, raw[key])) is None for key in identifiers)
            or any(
                _IDENT.fullmatch(cast(str, raw[key])) is None
                for key in ("provider", "endpoint", "serialization_identifier")
            )
            or not cast(str, raw["snapshot_fetched_at"]).endswith("Z")
        ):
            raise SchemaMismatchError("invalid discovery observation")
        try:
            mode = SnapshotMode(cast(str, raw["mode"]))
            fetched = _parse_stamp(raw["snapshot_fetched_at"], "observation timestamp")
            path = (
                store.root
                / cast(str, raw["provider"])
                / cast(str, raw["endpoint"])
                / cast(str, raw["request_identity"])
                / "observations"
                / cast(str, raw["snapshot_identity"])
                / cast(str, raw["observation_identity"])
                / "observation.json"
            )
            ref = SnapshotObservationRef(
                path,
                cast(str, raw["observation_identity"]),
                cast(str, raw["snapshot_identity"]),
                cast(str, raw["fact_version"]),
                mode,
                cast(str, raw["provider"]),
                cast(str, raw["endpoint"]),
                cast(str, raw["request_identity"]),
                cast(str, raw["response_sha256"]),
                cast(str, raw["serialization_identifier"]),
                fetched,
            )
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError("invalid discovery observation") from exc
        return cls(payload["url"], ref)


@dataclass(frozen=True)
class SecFilingDiscoveryEvent:
    key: str
    cik: str
    accession: str
    kind: SecFilingEventKind
    form: str
    filing_date: str
    report_date: str | None
    acceptance_native: str
    acceptance_at: datetime
    primary_document: str
    items: str | None
    metadata_digest: str
    source_observation_identities: tuple[str, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "cik": self.cik,
            "accession": self.accession,
            "kind": self.kind.value,
            "form": self.form,
            "filing_date": self.filing_date,
            "report_date": self.report_date,
            "acceptance_native": self.acceptance_native,
            "acceptance_at": _stamp(self.acceptance_at),
            "primary_document": self.primary_document,
            "items": self.items,
            "metadata_digest": self.metadata_digest,
            "source_observation_identities": list(self.source_observation_identities),
        }

    canonical_event_payload = canonical_payload

    @property
    def event_identity(self) -> str:
        return self.key


@dataclass(frozen=True)
class SecDiscoveryBatch:
    policy: SecDiscoveryPolicy
    prior_cursor: SecDiscoveryCursor | None
    sources: tuple[SecDiscoverySource, ...]
    events: tuple[SecFilingDiscoveryEvent, ...]
    candidate_cursor: SecDiscoveryCursor | None
    batch_identity: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA,
            "policy": self.policy.canonical_payload(),
            "prior_cursor": None
            if self.prior_cursor is None
            else self.prior_cursor.canonical_payload(),
            "sources": [item.canonical_payload() for item in self.sources],
            "events": [item.canonical_payload() for item in self.events],
            "candidate_cursor": None
            if self.candidate_cursor is None
            else self.candidate_cursor.canonical_payload(),
        }

    to_canonical_payload = canonical_payload

    @classmethod
    def from_canonical_payload(
        cls,
        payload: Mapping[str, object],
        store: SnapshotStore,
        _replay_cache: _DiscoveryReplayCache | None = None,
    ) -> SecDiscoveryBatch:
        from .event_discovery import discover_sec_filing_events

        # Rebuild by re-running the factory; this deliberately rejects caller-complete flags.
        required = {
            "schema_version",
            "policy",
            "prior_cursor",
            "sources",
            "events",
            "candidate_cursor",
        }
        if (
            set(payload) != required
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != _SCHEMA
            or not isinstance(payload["policy"], Mapping)
            or not isinstance(payload["sources"], list)
            or len(payload["sources"]) > _MAX_HISTORICAL_SOURCES + 1
            or any(not isinstance(item, Mapping) for item in payload["sources"])
        ):
            raise SchemaMismatchError("invalid discovery batch")
        policy = _policy_from_payload(cast(Mapping[str, object], payload["policy"]))
        prior = _cursor_from_payload(payload["prior_cursor"])
        sources = tuple(
            SecDiscoverySource.from_canonical_payload(cast(Mapping[str, object], item), store)
            for item in cast(list[object], payload["sources"])
        )
        if not sources:
            raise SchemaMismatchError("missing discovery root")
        rebuilt = discover_sec_filing_events(
            store,
            sources[0],
            sources[1:],
            policy=policy,
            prior_cursor=prior,
            _replay_cache=_replay_cache,
        )
        if canonical_batch_bytes(rebuilt) != _canonical(dict(payload)):
            raise SchemaMismatchError("discovery batch revalidation mismatch")
        return rebuilt


@dataclass(frozen=True)
class SecRootDiscoveryResult:
    """Root-only incremental discovery with an explicit completeness boundary."""

    status: SecRootCoverageStatus
    root_source: SecDiscoverySource
    covered_from: datetime | None
    covered_through: datetime
    window: SecRootDiscoveryWindow | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not SecRootCoverageStatus:
            raise TypeError("invalid root coverage status")
        if type(self.root_source) is not SecDiscoverySource:
            raise TypeError("invalid root source")
        through = _utc(self.covered_through, "covered_through")
        object.__setattr__(self, "covered_through", through)
        if self.covered_from is not None:
            start = _utc(self.covered_from, "covered_from")
            object.__setattr__(self, "covered_from", start)
            if start > through:
                raise ValueError("root coverage interval is inverted")
        if self.status is SecRootCoverageStatus.COMPLETE:
            if self.window is None or self.reason is not None:
                raise ValueError("complete root discovery requires a root window and no reason")
        elif not self.reason:
            raise ValueError("incomplete root discovery requires a reason")


@dataclass(frozen=True)
class SecRootDiscoveryWindow:
    """Events observed in the root recent rows, explicitly outside full-closure batches."""

    policy: SecDiscoveryPolicy
    prior_cursor: SecDiscoveryCursor | None
    root_source: SecDiscoverySource
    events: tuple[SecFilingDiscoveryEvent, ...]
    candidate_cursor: SecDiscoveryCursor | None
    covered_from: datetime
    covered_through: datetime
    coverage_semantics: str = "SEC_RECENT_ROOT_WINDOW"

    def __post_init__(self) -> None:
        if self.coverage_semantics != "SEC_RECENT_ROOT_WINDOW":
            raise ValueError("invalid root window coverage semantics")
        start = _utc(self.covered_from, "covered_from")
        through = _utc(self.covered_through, "covered_through")
        if start > through:
            raise ValueError("root window interval is inverted")
        object.__setattr__(self, "covered_from", start)
        object.__setattr__(self, "covered_through", through)


def _policy_from_payload(raw: Mapping[str, object]) -> SecDiscoveryPolicy:
    required = {
        "cik",
        "forms",
        "acceptance_lower",
        "acceptance_upper",
        "overlap_microseconds",
        "mode",
        "version",
    }
    if (
        set(raw) != required
        or not isinstance(raw["forms"], list)
        or type(raw["overlap_microseconds"]) is not int
    ):
        raise SchemaMismatchError("invalid discovery policy")
    try:
        return SecDiscoveryPolicy(
            cast(str, raw["cik"]),
            tuple(cast(list[str], raw["forms"])),
            _parse_stamp(raw["acceptance_lower"], "acceptance lower"),
            _parse_stamp(raw["acceptance_upper"], "acceptance upper"),
            timedelta(microseconds=raw["overlap_microseconds"]),
            SecDiscoveryMode(cast(str, raw["mode"])),
            cast(str, raw["version"]),
        )
    except (TypeError, ValueError) as exc:
        raise SchemaMismatchError("invalid discovery policy") from exc


def _cursor_from_payload(raw: object) -> SecDiscoveryCursor | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"acceptance_at", "accession"}:
        raise SchemaMismatchError("invalid discovery cursor")
    try:
        return SecDiscoveryCursor(
            _parse_stamp(raw["acceptance_at"], "cursor timestamp"), cast(str, raw["accession"])
        )
    except (TypeError, ValueError) as exc:
        raise SchemaMismatchError("invalid discovery cursor") from exc


def canonical_batch_bytes(batch: SecDiscoveryBatch) -> bytes:
    """Return the strict persisted payload used to derive ``batch_identity``."""
    if type(batch) is not SecDiscoveryBatch:
        raise TypeError("invalid discovery batch")
    return _canonical(batch.canonical_payload())
