"""Offline N-PORT table parsing and knowledge-time vintage resolution."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, cast

from ...core import EmptyDisposition, FetchProvenance
from .artifacts import SecArtifactRef, SecReplaySession
from .endpoints import SecNportQuarterRequest
from .errors import CoverageError, EmptyResponseError, SchemaMismatchError
from .http import SecTransportEvidence

REQUIRED = (
    "SUBMISSION",
    "REGISTRANT",
    "FUND_REPORTED_INFO",
    "FUND_REPORTED_HOLDING",
    "IDENTIFIERS",
)
REQUIRED_COLUMNS = {
    "SUBMISSION": (
        "ACCESSION_NUMBER",
        "FILING_DATE",
        "SUB_TYPE",
        "REPORT_ENDING_PERIOD",
        "REPORT_DATE",
        "IS_LAST_FILING",
    ),
    "REGISTRANT": ("ACCESSION_NUMBER", "CIK", "REGISTRANT_NAME", "FILE_NUM", "LEI"),
    "FUND_REPORTED_INFO": (
        "ACCESSION_NUMBER",
        "SERIES_ID",
        "SERIES_NAME",
        "SERIES_LEI",
        "TOTAL_ASSETS",
        "TOTAL_LIABILITIES",
        "NET_ASSETS",
    ),
    "FUND_REPORTED_HOLDING": (
        "ACCESSION_NUMBER",
        "HOLDING_ID",
        "ISSUER_NAME",
        "ISSUER_LEI",
        "ISSUER_TITLE",
        "ISSUER_CUSIP",
        "BALANCE",
        "UNIT",
        "OTHER_UNIT_DESC",
        "CURRENCY_CODE",
        "CURRENCY_VALUE",
        "EXCHANGE_RATE",
        "PERCENTAGE",
        "PAYOFF_PROFILE",
        "ASSET_CAT",
        "OTHER_ASSET",
        "ISSUER_TYPE",
        "OTHER_ISSUER",
        "INVESTMENT_COUNTRY",
        "IS_RESTRICTED_SECURITY",
        "FAIR_VALUE_LEVEL",
        "DERIVATIVE_CAT",
    ),
    "IDENTIFIERS": (
        "HOLDING_ID",
        "IDENTIFIERS_ID",
        "IDENTIFIER_ISIN",
        "IDENTIFIER_TICKER",
        "OTHER_IDENTIFIER",
        "OTHER_IDENTIFIER_DESC",
    ),
}
_DATE_RE = re.compile(r"^[0-9]{2}-(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)-[0-9]{4}$")
_FIXED_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}
_NUMERIC_FIELDS = {
    "FUND_REPORTED_INFO": frozenset({"TOTAL_ASSETS", "TOTAL_LIABILITIES", "NET_ASSETS"}),
    "FUND_REPORTED_HOLDING": frozenset(
        {"BALANCE", "CURRENCY_VALUE", "EXCHANGE_RATE", "PERCENTAGE"}
    ),
}


def _empty_native_assets() -> dict[str, str]:
    return {}


def parse_sec_date(value: str) -> date:
    """Parse SEC's ASCII DD-MON-YYYY date without locale-sensitive helpers."""
    if any(ord(c) > 127 for c in value):
        raise SchemaMismatchError("invalid SEC date")
    value = value.upper()
    if not _DATE_RE.fullmatch(value):
        raise SchemaMismatchError("invalid SEC date")
    try:
        return date(int(value[7:]), _MONTHS[value[3:6]], int(value[:2]))
    except ValueError as exc:
        raise SchemaMismatchError("invalid SEC date") from exc


def parse_sec_acceptance_timestamp(value: str) -> datetime:
    if any(ord(c) > 127 for c in value):
        raise SchemaMismatchError("invalid EDGAR acceptance timestamp")
    try:
        if re.fullmatch(r"[0-9]{14}(?:\.[0-9]+)?", value):
            parsed = datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        elif re.fullmatch(r"[0-9]{8}[0-9]{2}:[0-9]{2}:[0-9]{2}", value):
            parsed = datetime.strptime(value, "%Y%m%d%H:%M:%S").replace(tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            parsed = parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SchemaMismatchError("invalid EDGAR acceptance timestamp") from exc
    return parsed


def _value(value: str | None, field: str, table: str | None = None) -> Any:
    if value is None or value == "":
        return None
    upper = field.upper()
    if "DATE" in upper or upper in {"REPORT_ENDING_PERIOD", "REPORT_DATE", "FILING_DATE"}:
        return parse_sec_date(value)
    if upper.startswith(("IS_", "HAS_")):
        return value
    if table is not None and upper in _NUMERIC_FIELDS.get(table, ()):
        if not _FIXED_RE.fullmatch(value):
            raise SchemaMismatchError(f"invalid SEC number in {table}.{upper}")
        try:
            parsed = Decimal(value)
            if not parsed.is_finite():
                raise SchemaMismatchError(f"invalid SEC number in {table}.{upper}")
            return parsed
        except InvalidOperation as exc:
            raise SchemaMismatchError("invalid SEC number") from exc
    return value


def _member(z: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    matches = [
        i for i in z.infolist() if i.filename.rsplit("/", 1)[-1].split(".", 1)[0].upper() == name
    ]
    if len(matches) != 1:
        raise SchemaMismatchError(f"required member {name} missing or duplicated")
    return matches[0]


def read_member(
    z: Any,
    name: str | None = None,
    *,
    required_columns: tuple[str, ...] = (),
    table: str | None = None,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    scan_counts: dict[str, int] | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield typed rows from one member; the member is never materialized."""
    raw = z.open(_member(z, name)) if name is not None and isinstance(z, zipfile.ZipFile) else z
    if not hasattr(raw, "read"):

        class _IteratorReader(io.RawIOBase):
            def __init__(self, chunks: Iterable[bytes]) -> None:
                self._chunks, self._buf = iter(chunks), bytearray()

            def readable(self) -> bool:
                return True

            def readinto(self, b: Any) -> int:
                while not self._buf:
                    try:
                        self._buf.extend(next(self._chunks))
                    except StopIteration:
                        return 0
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                del self._buf[:n]
                return n

        raw = io.BufferedReader(_IteratorReader(raw))
    with raw if hasattr(raw, "__enter__") else nullcontext(raw):
        try:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            reader = csv.DictReader(text, delimiter="\t")
            if not reader.fieldnames:
                raise SchemaMismatchError("missing table header")
            columns = tuple(str(x).upper() for x in reader.fieldnames)
            if len(set(columns)) != len(columns):
                raise SchemaMismatchError("duplicate table column")
            missing = set(required_columns) - set(columns)
            if missing:
                raise SchemaMismatchError("required table column missing")
            for row in reader:
                if scan_counts is not None and table is not None:
                    scan_counts[table] = scan_counts.get(table, 0) + 1
                if predicate is not None and not predicate(row):
                    continue
                native = {k.upper(): v for k, v in row.items() if k is not None}
                accession = native.get("ACCESSION_NUMBER")
                if accession and not _ACCESSION_RE.fullmatch(accession):
                    raise SchemaMismatchError("invalid SEC accession number")
                typed = {k: _value(v, k, table) for k, v in native.items()}
                typed["__native__"] = MappingProxyType(native)
                yield typed
        except UnicodeError as exc:
            raise SchemaMismatchError("invalid UTF-8") from exc


class SecAvailabilityPolicy(str, Enum):
    OBSERVATION_ONLY_V1 = "OBSERVATION_ONLY_V1"
    ACCEPTED_AT_PLUS_LAG_V1 = "ACCEPTED_AT_PLUS_LAG_V1"


@dataclass(frozen=True)
class SecNportScanCounts:
    """Safe row/member counters emitted by a bounded extraction."""

    member_rows: Mapping[str, int]
    retained_rows: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "member_rows", MappingProxyType(dict(self.member_rows)))
        object.__setattr__(self, "retained_rows", MappingProxyType(dict(self.retained_rows)))


@dataclass(frozen=True)
class SecNportQA:
    percentage_sum: Decimal | None = None
    identifier_coverage_count: int = 0
    identifier_coverage_weight: Decimal | None = None
    duplicate_issuer_count: int = 0
    net_assets_delta: Decimal | None = None
    quality_flags: tuple[str, ...] = ()
    duplicate_cusip_count: int = 0
    duplicate_ticker_count: int = 0
    currency_value_net_assets_delta: Decimal | None = None
    accounting_delta: Decimal | None = None
    currency_all_complete = True
    accounting_all_complete = True


@dataclass(frozen=True)
class SecNportQuarterResult:
    """Immutable extraction result; transport/artifact objects are caller-owned."""

    artifact_ref: SecArtifactRef
    vintages: tuple[SecFundHoldingVintage, ...]
    scan_counts: SecNportScanCounts
    qa: SecNportQA
    transport_evidence: SecTransportEvidence
    provenance: FetchProvenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "vintages", tuple(self.vintages))


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, Any], value)
        return {str(k): _canonical(v) for k, v in sorted(mapping.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (tuple, list)):
        sequence = cast(tuple[Any, ...] | list[Any], value)
        return [_canonical(v) for v in sequence]
    return value


@dataclass(frozen=True)
class SecFundHoldingVintage:
    accession_number: str
    cik: str
    series_id: str | None
    series_name: str | None
    report_date: date
    filing_date: date | None
    submission_type: str
    holdings: tuple[Mapping[str, Any], ...]
    identifiers: tuple[Mapping[str, Any], ...] = ()
    accepted_at: datetime | None = None
    artifact_sha256: str = ""
    source_url: str = ""
    observed_at: datetime | None = None
    availability_anchor: datetime | None = None
    availability_basis: str = "PROVIDER_FIRST_OBSERVED"
    availability_precision: str = "TIMESTAMP"
    availability_policy: str = SecAvailabilityPolicy.OBSERVATION_ONLY_V1.value
    availability_lag_days: int = 0
    quality_flags: tuple[str, ...] = ()
    total_assets: Decimal | None = None
    total_liabilities: Decimal | None = None
    net_assets: Decimal | None = None
    report_ending_period: date | None = None
    registrant_name: str | None = None
    registrant_file_number: str | None = None
    registrant_lei: str | None = None
    series_lei: str | None = None
    native_assets: dict[str, str] = field(default_factory=_empty_native_assets)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "holdings", tuple(MappingProxyType(dict(x)) for x in self.holdings)
        )
        object.__setattr__(
            self, "identifiers", tuple(MappingProxyType(dict(x)) for x in self.identifiers)
        )
        observed = self.observed_at or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        observed = observed.astimezone(UTC)
        object.__setattr__(self, "observed_at", observed)
        if self.accepted_at is not None:
            if self.accepted_at.tzinfo is None:
                raise ValueError("accepted_at must be timezone-aware")
            object.__setattr__(self, "accepted_at", self.accepted_at.astimezone(UTC))
        if (
            self.availability_anchor is None
            and self.availability_basis == "PROVIDER_FIRST_OBSERVED"
        ):
            object.__setattr__(self, "availability_anchor", observed)
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))
        assets: dict[str, str] = dict(self.native_assets)
        object.__setattr__(self, "native_assets", MappingProxyType(assets))

    @property
    def payload_hash(self) -> str:
        cached = getattr(self, "_cached_payload_hash", None)
        if cached is not None:
            return cached
        payload = _canonical({"holdings": self.holdings, "identifiers": self.identifiers})
        h = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        object.__setattr__(self, "_cached_payload_hash", h)
        return h

    @property
    def vintage_identity(self) -> str:
        cached = getattr(self, "_cached_vintage_identity", None)
        if cached is not None:
            return cached
        value = {
            "artifact_sha256": self.artifact_sha256,
            "cik": self.cik,
            "series_id": self.series_id,
            "report_date": self.report_date,
            "accession": self.accession_number,
            "submission_type": self.submission_type,
            "accepted_at": self.accepted_at,
            "payload_hash": self.payload_hash,
            "parser_schema_version": "sec-nport-v1",
        }
        h = hashlib.sha256(
            json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        object.__setattr__(self, "_cached_vintage_identity", h)
        return h


class SecUnavailableResult:
    def __init__(
        self,
        reason: str = "not-yet-available",
        *,
        policy: SecAvailabilityPolicy = SecAvailabilityPolicy.OBSERVATION_ONLY_V1,
        lag_days: int = 0,
    ) -> None:
        self.reason = reason
        self.policy = policy
        self.lag_days = lag_days


class SecHoldingVintageSet:
    def __init__(
        self, vintages: tuple[SecFundHoldingVintage, ...], *, explicit_cik_query: bool = False
    ) -> None:
        self.vintages = tuple(vintages)
        identities: set[str] = set()
        filings: dict[tuple[str, str], SecFundHoldingVintage] = {}
        series_ciks: dict[str | None, set[str]] = {}
        for vintage in self.vintages:
            if vintage.vintage_identity in identities:
                raise ValueError("duplicate vintage identity")
            key = (vintage.cik, vintage.accession_number)
            series_ciks.setdefault(vintage.series_id, set()).add(vintage.cik)
            if not explicit_cik_query and len(series_ciks[vintage.series_id]) > 1:
                raise ValueError("series mapped to multiple CIKs")
            old = filings.get(key)
            if old is not None:
                if (
                    old.payload_hash != vintage.payload_hash
                    or old.report_date != vintage.report_date
                    or old.availability_anchor != vintage.availability_anchor
                    or old.submission_type != vintage.submission_type
                ):
                    raise ValueError("filing collision")
                raise ValueError("duplicate filing identity")
            identities.add(vintage.vintage_identity)
            filings[key] = vintage

    def resolve(
        self,
        cik: str,
        series_id: str | None,
        knowledge_at: datetime,
        *,
        policy: SecAvailabilityPolicy = SecAvailabilityPolicy.OBSERVATION_ONLY_V1,
        lag_days: int = 0,
    ) -> SecFundHoldingVintage | SecUnavailableResult:
        if knowledge_at.tzinfo is None:
            raise ValueError("knowledge_at must be timezone-aware")
        if lag_days < 0 or lag_days > 30:
            raise ValueError("lag_days must be 0..30")
        k = knowledge_at.astimezone(UTC)
        eligible: list[SecFundHoldingVintage] = []
        active_anchors: dict[str, datetime] = {}
        for vintage in self.vintages:
            if (
                vintage.cik != cik
                or vintage.series_id != series_id
                or vintage.report_date > k.date()
            ):
                continue
            anchor = vintage.availability_anchor
            if policy is SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1:
                anchor = (
                    vintage.accepted_at + timedelta(days=lag_days) if vintage.accepted_at else None
                )
            if anchor is not None:
                active_anchors[vintage.vintage_identity] = anchor
            if anchor is not None and anchor <= k:
                eligible.append(vintage)
        if not eligible:
            return SecUnavailableResult(policy=policy, lag_days=lag_days)
        winners: dict[date, SecFundHoldingVintage] = {}
        for vintage in eligible:
            old = winners.get(vintage.report_date)
            key = (
                active_anchors.get(vintage.vintage_identity, datetime.min.replace(tzinfo=UTC)),
                vintage.accepted_at or datetime.min.replace(tzinfo=UTC),
                vintage.filing_date or date.min,
                vintage.accession_number,
            )
            if old is None:
                winners[vintage.report_date] = vintage
                continue
            old_key = (
                active_anchors.get(old.vintage_identity, datetime.min.replace(tzinfo=UTC)),
                old.accepted_at or datetime.min.replace(tzinfo=UTC),
                old.filing_date or date.min,
                old.accession_number,
            )
            if key > old_key:
                winners[vintage.report_date] = vintage
        winner = winners[max(winners)]
        active_anchor = active_anchors.get(winner.vintage_identity)
        if (
            winner.availability_policy != policy.value
            or winner.availability_lag_days != lag_days
            or winner.availability_anchor != active_anchor
        ):
            winner = replace(
                winner,
                availability_anchor=active_anchor,
                availability_basis=(
                    "INFERRED_SCHEDULE"
                    if policy is SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1
                    else winner.availability_basis
                ),
                availability_policy=policy.value,
                availability_lag_days=lag_days,
                quality_flags=(
                    tuple(winner.quality_flags)
                    + (
                        ("PIT_INFERRED",)
                        if policy is SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1
                        and "PIT_INFERRED" not in winner.quality_flags
                        else ()
                    )
                ),
            )
        return winner


def _extract_quarter(
    path: str,
    request: SecNportQuarterRequest,
    *,
    artifact_sha256: str = "",
    source_url: str = "",
    observed_at: datetime | None = None,
    replay_session: SecReplaySession | None = None,
    scan_counts: dict[str, int] | None = None,
    max_selected_rows: int | None = None,
) -> tuple[SecFundHoldingVintage, ...]:
    """Extract selected series using one pass over each required member."""
    with zipfile.ZipFile(path) as archive:
        source: Callable[[str], Any] = (
            replay_session.open_member
            if replay_session is not None
            else lambda n: archive.open(_member(archive, n))
        )
        small: dict[str, dict[Any, dict[str, Any]]] = {name: {} for name in REQUIRED[:3]}
        reqcols = REQUIRED_COLUMNS
        exact_mode = bool(request.selected_pairs or request.single_series_ciks)
        target_ciks = {c for c, _ in request.selected_pairs} | set(request.single_series_ciks)
        if exact_mode:
            for row in read_member(
                source("REGISTRANT"), required_columns=reqcols["REGISTRANT"], table="REGISTRANT"
            ):
                if scan_counts is not None:
                    scan_counts["REGISTRANT"] = scan_counts.get("REGISTRANT", 0) + 1
                cik = str(row.get("CIK") or "")
                accession = str(row.get("ACCESSION_NUMBER") or "")
                if cik in target_ciks:
                    if accession in small["REGISTRANT"]:
                        raise SchemaMismatchError("duplicate registrant row")
                    small["REGISTRANT"][accession] = row
        target_accessions = set(small["REGISTRANT"]) if exact_mode else None
        # Discover requested accessions first; unrelated parent rows are never retained.
        for row in read_member(
            source("FUND_REPORTED_INFO"),
            required_columns=reqcols["FUND_REPORTED_INFO"],
            table="FUND_REPORTED_INFO",
        ):
            if scan_counts is not None:
                scan_counts["FUND_REPORTED_INFO"] = scan_counts.get("FUND_REPORTED_INFO", 0) + 1
            series = str(row.get("SERIES_ID") or "")
            accession = str(row.get("ACCESSION_NUMBER") or "")
            if (
                exact_mode and target_accessions is not None and accession in target_accessions
            ) or (not exact_mode and series in request.series_ids):
                if not accession:
                    raise SchemaMismatchError("missing parent key")
                key = (accession, series)
                if key in small["FUND_REPORTED_INFO"]:
                    raise SchemaMismatchError("duplicate parent row")
                small["FUND_REPORTED_INFO"][key] = row
        accession_set = {str(k[0]) for k in small["FUND_REPORTED_INFO"]}
        for name in ("SUBMISSION",) if exact_mode else ("SUBMISSION", "REGISTRANT"):
            for row in read_member(source(name), required_columns=reqcols[name], table=name):
                if scan_counts is not None:
                    scan_counts[name] = scan_counts.get(name, 0) + 1
                accession = str(row.get("ACCESSION_NUMBER") or "")
                if accession in accession_set:
                    if accession in small[name]:
                        raise SchemaMismatchError("duplicate parent row")
                    small[name][accession] = row
        selected: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        retained_output_rows = 0
        parent_counts: dict[tuple[str, str], int] = {}
        for fund in small["FUND_REPORTED_INFO"].values():
            accession = str(fund.get("ACCESSION_NUMBER") or "")
            submission = small["SUBMISSION"].get(accession)
            registrant = small["REGISTRANT"].get(accession)
            if submission is not None and not registrant:
                raise SchemaMismatchError("missing registrant parent")
            cik = str(registrant.get("CIK") if registrant else fund.get("CIK") or "")
            if cik in request.single_series_ciks:
                parent_counts[(cik, accession)] = parent_counts.get((cik, accession), 0) + 1
            if submission is not None and submission.get("SUB_TYPE") in ("NPORT-P", "NPORT-P/A"):
                if registrant is None:
                    raise SchemaMismatchError("missing registrant parent")
                selected_pair = (cik, str(fund.get("SERIES_ID") or "")) in request.selected_pairs
                legacy_series = (
                    not request.selected_pairs
                    and str(fund.get("SERIES_ID") or "") in request.series_ids
                )
                single = cik in request.single_series_ciks
                if selected_pair or legacy_series or single:
                    # A vintage is one output row in fund_vintages. Count it at
                    # selection time so the cap is enforced before any child
                    # rows are retained.
                    retained_output_rows += 1
                    if max_selected_rows is not None and retained_output_rows > max_selected_rows:
                        raise CoverageError("selected output row limit exceeded")
                    selected.append((submission, fund, cik))
        for (cik, _accession), count in parent_counts.items():
            if cik in request.single_series_ciks and count != 1:
                raise CoverageError("single-series CIK is ambiguous")
        found_singles = {cik for _, _, cik in selected if cik in request.single_series_ciks}
        missing_singles = set(request.single_series_ciks) - found_singles
        if missing_singles:
            raise CoverageError(f"required single-series CIK missing: {sorted(missing_singles)}")
        found = {str(f.get("SERIES_ID") or "") for _, f, _ in selected}
        found_pairs = {(cik, str(f.get("SERIES_ID") or "")) for _, f, cik in selected}
        missing_pairs = set(request.required_pairs) - found_pairs
        if missing_pairs:
            raise CoverageError(f"required exact selector missing: {sorted(missing_pairs)}")
        missing = set(request.required_series_ids) - found
        if missing:
            raise CoverageError(f"required series missing: {sorted(missing)}")
        if not selected and request.empty_policy.value == "REQUIRE_ROWS":
            raise EmptyResponseError("no selected holdings")
        accession_set = {str(s["ACCESSION_NUMBER"]) for s, _, _ in selected}
        holdings: dict[str, list[dict[str, Any]]] = {a: [] for a in accession_set}
        owner: dict[str, str] = {}
        accession_hids: dict[str, set[str]] = {a: set() for a in accession_set}
        seen_other_hids: set[str] = set()

        def _holding_predicate(r: dict[str, Any]) -> bool:
            acc = str(r.get("ACCESSION_NUMBER") or "")
            hid = str(r.get("HOLDING_ID") or "")
            if not hid or not acc:
                raise CoverageError("holding id collision")
            if acc in accession_set:
                if hid in seen_other_hids or hid in owner:
                    raise CoverageError("holding id collision")
                return True
            if hid in owner:
                raise CoverageError("holding id collision")
            seen_other_hids.add(hid)
            return False

        for row in read_member(
            source("FUND_REPORTED_HOLDING"),
            required_columns=REQUIRED_COLUMNS["FUND_REPORTED_HOLDING"],
            table="FUND_REPORTED_HOLDING",
            predicate=_holding_predicate,
            scan_counts=scan_counts,
        ):
            acc = str(row.get("ACCESSION_NUMBER") or "")
            hid = str(row.get("HOLDING_ID") or "")
            if hid in accession_hids[acc]:
                raise CoverageError("holding id collision")
            retained_output_rows += 1
            if max_selected_rows is not None and retained_output_rows > max_selected_rows:
                raise CoverageError("selected output row limit exceeded")
            owner[hid] = acc
            accession_hids[acc].add(hid)
            holdings[acc].append(row)

        identifiers: dict[str, list[dict[str, Any]]] = {a: [] for a in accession_set}
        idkeys: set[tuple[str, str]] = set()
        for row in read_member(
            source("IDENTIFIERS"),
            required_columns=REQUIRED_COLUMNS["IDENTIFIERS"],
            table="IDENTIFIERS",
            predicate=lambda r: str(r.get("HOLDING_ID") or "") in owner,
            scan_counts=scan_counts,
        ):
            acc = owner.get(str(row.get("HOLDING_ID") or ""))
            if acc is None:
                continue
            key = (str(row.get("HOLDING_ID") or ""), str(row.get("IDENTIFIERS_ID") or ""))
            if key in idkeys:
                raise CoverageError("duplicate identifier key")
            retained_output_rows += 1
            if max_selected_rows is not None and retained_output_rows > max_selected_rows:
                raise CoverageError("selected output row limit exceeded")
            idkeys.add(key)
            identifiers[acc].append(row)
        out: list[SecFundHoldingVintage] = []
        for submission, fund, cik in selected:
            acc = str(submission["ACCESSION_NUMBER"])
            registrant_row = small["REGISTRANT"].get(acc)
            if registrant_row is None:
                raise CoverageError("missing registrant parent")
            if request.empty_policy.value == "REQUIRE_ROWS" and not holdings[acc]:
                raise EmptyResponseError("selected accession has no holdings")
            report_date = submission.get("REPORT_DATE")
            filing_date = submission.get("FILING_DATE")
            if not isinstance(report_date, date) or (
                filing_date is not None and not isinstance(filing_date, date)
            ):
                raise SchemaMismatchError("missing or invalid submission dates")
            out.append(
                SecFundHoldingVintage(
                    acc,
                    cik,
                    (str(fund["SERIES_ID"]) if fund.get("SERIES_ID") not in (None, "") else None),
                    fund.get("SERIES_NAME"),
                    report_date,
                    filing_date,
                    str(submission["SUB_TYPE"]),
                    tuple(sorted(holdings[acc], key=lambda r: str(r.get("HOLDING_ID") or ""))),
                    tuple(
                        sorted(
                            identifiers[acc],
                            key=lambda r: (
                                str(r.get("HOLDING_ID") or ""),
                                str(r.get("IDENTIFIERS_ID") or ""),
                            ),
                        )
                    ),
                    artifact_sha256=artifact_sha256,
                    source_url=source_url,
                    observed_at=observed_at,
                    total_assets=cast(Decimal | None, fund.get("TOTAL_ASSETS")),
                    total_liabilities=cast(Decimal | None, fund.get("TOTAL_LIABILITIES")),
                    net_assets=cast(Decimal | None, fund.get("NET_ASSETS")),
                    report_ending_period=cast(date | None, submission.get("REPORT_ENDING_PERIOD")),
                    registrant_name=cast(str | None, registrant_row.get("REGISTRANT_NAME")),
                    registrant_file_number=cast(str | None, registrant_row.get("FILE_NUM")),
                    registrant_lei=cast(str | None, registrant_row.get("LEI")),
                    series_lei=cast(str | None, fund.get("SERIES_LEI")),
                    native_assets={
                        key: cast(str, fund.get("__native__", {}).get(key))
                        for key in ("TOTAL_ASSETS", "TOTAL_LIABILITIES", "NET_ASSETS")
                    },
                )
            )
        return tuple(
            sorted(out, key=lambda v: (v.cik, v.series_id or "", v.report_date, v.accession_number))
        )


def enrich_vintages(
    vintages: tuple[SecFundHoldingVintage, ...],
    payload: dict[str, Any],
    cik: str,
    *,
    load: Callable[[str], Any] | None = None,
) -> tuple[SecFundHoldingVintage, ...]:
    """Join caller-fetched EDGAR metadata without performing network I/O."""
    from .edgar import parse_submissions, resolve_submissions

    accessions = tuple(v.accession_number for v in vintages)
    if load is None:
        metadata = parse_submissions(payload, cik, accessions)
    else:
        metadata = resolve_submissions(payload, cik, accessions, load=load)
    enriched: list[SecFundHoldingVintage] = []
    for vintage in vintages:
        row = metadata[vintage.accession_number]
        if row["form"] != vintage.submission_type:
            raise SchemaMismatchError("EDGAR form disagrees with N-PORT")
        edgar_report = row["reportDate"]
        if edgar_report is not None and edgar_report != vintage.report_date.isoformat():
            raise SchemaMismatchError("EDGAR report date disagrees with N-PORT")
        accepted = row.get("acceptanceDateTime")
        accepted_at = None
        if isinstance(accepted, str) and accepted:
            accepted_at = parse_sec_acceptance_timestamp(accepted)
        flags = tuple(vintage.quality_flags) + (
            ("ACCEPTANCE_TIME_MISSING",) if accepted_at is None else ()
        )
        enriched.append(replace(vintage, accepted_at=accepted_at, quality_flags=flags))
    return tuple(enriched)


def _extract_quarter_result(
    path: str,
    request: SecNportQuarterRequest,
    *,
    artifact_ref: Any = None,
    transport_evidence: Any = None,
    artifact_sha256: str = "",
    source_url: str = "",
    observed_at: datetime | None = None,
    replay_session: SecReplaySession | None = None,
    max_selected_rows: int | None = None,
) -> SecNportQuarterResult:
    """Convenience assembly of typed output around the streaming extractor."""
    member_rows: dict[str, int] = {}
    vintages = _extract_quarter(
        path,
        request,
        artifact_sha256=artifact_sha256,
        source_url=source_url,
        observed_at=observed_at,
        replay_session=replay_session,
        scan_counts=member_rows,
        max_selected_rows=max_selected_rows,
    )
    retained_rows = {name: 0 for name in REQUIRED}
    retained_rows["FUND_REPORTED_HOLDING"] = sum(len(v.holdings) for v in vintages)
    retained_rows["IDENTIFIERS"] = sum(len(v.identifiers) for v in vintages)
    member_rows.update({name: 0 for name in REQUIRED if name not in member_rows})
    percentage_sum: Decimal | None = None
    id_count = 0
    id_weight: Decimal | None = None
    issuers: set[str] = set()
    cusips: set[str] = set()
    issuer_dups = cusip_dups = ticker_dups = 0
    currency_delta: Decimal | None = None
    accounting_delta: Decimal | None = None
    currency_all_complete = True
    accounting_all_complete = True
    for vintage in vintages:
        currency_total = Decimal(0)
        currency_complete = True
        for row in vintage.holdings:
            value = row.get("PERCENTAGE")
            if isinstance(value, Decimal):
                percentage_sum = value if percentage_sum is None else percentage_sum + value
            cv = row.get("CURRENCY_VALUE")
            if isinstance(cv, Decimal):
                currency_total += cv
            else:
                currency_complete = False
            for field_name, seen in (
                ("ISSUER_NAME", issuers),
                ("ISSUER_CUSIP", cusips),
            ):
                val = row.get(field_name)
                if val:
                    if val in seen:
                        if "CUSIP" in field_name:
                            cusip_dups += 1
                        elif "TICKER" in field_name:
                            ticker_dups += 1
                        else:
                            issuer_dups += 1
                    seen.add(str(val))
        identified = {
            str(ident.get("HOLDING_ID"))
            for ident in vintage.identifiers
            if ident.get("HOLDING_ID")
            and (
                ident.get("IDENTIFIER_ISIN")
                or ident.get("IDENTIFIER_TICKER")
                or ident.get("OTHER_IDENTIFIER")
            )
        }
        child_tickers: set[str] = set()
        for ident in vintage.identifiers:
            ticker = ident.get("IDENTIFIER_TICKER")
            if ticker:
                if str(ticker) in child_tickers:
                    ticker_dups += 1
                child_tickers.add(str(ticker))
        id_count += len(identified)
        weights = [
            cast(Decimal, row["PERCENTAGE"])
            for row in vintage.holdings
            if str(row.get("HOLDING_ID")) in identified
            and isinstance(row.get("PERCENTAGE"), Decimal)
        ]
        if weights:
            weight = sum(weights, Decimal(0))
            id_weight = weight if id_weight is None else id_weight + weight
        if currency_complete and vintage.net_assets is not None:
            delta = currency_total - vintage.net_assets
            currency_delta = delta if currency_delta is None else currency_delta + delta
        else:
            currency_all_complete = False
        if (
            vintage.total_assets is not None
            and vintage.total_liabilities is not None
            and vintage.net_assets is not None
        ):
            delta = vintage.total_assets - vintage.total_liabilities - vintage.net_assets
            accounting_delta = delta if accounting_delta is None else accounting_delta + delta
        else:
            accounting_all_complete = False
    quality_flags: tuple[str, ...] = ()
    if not currency_all_complete or not accounting_all_complete:
        currency_delta = None if not currency_all_complete else currency_delta
        accounting_delta = None if not accounting_all_complete else accounting_delta
        quality_flags = ("RECONCILIATION_INCOMPLETE",)
    total_rows = sum(retained_rows.values())
    retrieved = observed_at or datetime.now(UTC)
    provenance = FetchProvenance.from_request(
        request.spec,
        retrieved_at=retrieved,
        attempts=(),
        row_count=total_rows,
        columns=(),
        warnings=(),
        snapshot_identities=(artifact_sha256,) if artifact_sha256 else (),
        empty_disposition=EmptyDisposition.ALLOWED_EMPTY
        if total_rows == 0
        else EmptyDisposition.NOT_EMPTY,
    )
    return SecNportQuarterResult(
        artifact_ref,
        vintages,
        SecNportScanCounts(member_rows, retained_rows),
        SecNportQA(
            percentage_sum=percentage_sum,
            identifier_coverage_count=id_count,
            identifier_coverage_weight=id_weight,
            duplicate_issuer_count=issuer_dups,
            duplicate_cusip_count=cusip_dups,
            duplicate_ticker_count=ticker_dups,
            currency_value_net_assets_delta=currency_delta,
            accounting_delta=accounting_delta,
            quality_flags=quality_flags,
        ),
        transport_evidence or SecTransportEvidence(),
        provenance,
    )


def extract_from_artifact(
    store: Any,
    artifact_ref: Any,
    request: SecNportQuarterRequest,
    *,
    transport_evidence: Any = None,
    observed_at: datetime | None = None,
    max_selected_rows: int | None = None,
) -> SecNportQuarterResult:
    """Replay-validate an artifact before parsing its source ZIP."""
    if max_selected_rows is not None and max_selected_rows <= 0:
        raise ValueError("max_selected_rows must be positive")
    session = SecReplaySession(store, artifact_ref, parser_version="1", required_members=REQUIRED)
    manifest = json.loads(artifact_ref.manifest.read_text(encoding="utf-8"))
    if observed_at is None:
        raw_retrieved = manifest.get("retrieved_at")
        if not isinstance(raw_retrieved, str):
            raise SchemaMismatchError("artifact retrieval timestamp missing")
        try:
            observed_at = datetime.fromisoformat(raw_retrieved)
        except ValueError as exc:
            raise SchemaMismatchError("invalid artifact retrieval timestamp") from exc
        if observed_at.tzinfo is None:
            raise SchemaMismatchError("artifact retrieval timestamp must be aware")
    result = _extract_quarter_result(
        str(artifact_ref.source_zip),
        request,
        artifact_ref=artifact_ref,
        transport_evidence=transport_evidence,
        artifact_sha256=artifact_ref.sha256,
        source_url=str(manifest.get("source_url", "")),
        observed_at=observed_at,
        replay_session=session,
        max_selected_rows=max_selected_rows,
    )
    session.assert_complete()
    return result


__all__ = [
    "REQUIRED",
    "SecAvailabilityPolicy",
    "SecFundHoldingVintage",
    "SecHoldingVintageSet",
    "SecNportQA",
    "SecNportQuarterResult",
    "SecNportScanCounts",
    "SecUnavailableResult",
    "enrich_vintages",
    "extract_from_artifact",
    "parse_sec_acceptance_timestamp",
    "parse_sec_date",
    "read_member",
]
