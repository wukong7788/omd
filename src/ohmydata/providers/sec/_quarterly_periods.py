"""Typed SEC fiscal-period evidence and source-backed derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from .financials import SecCompanyFinancialVintage

_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_MAX_SOURCE_LABEL_BYTES = 1_024
_MAX_CONTEXT_REF_BYTES = 256
_MAX_DURATION_LABELS = 1_000


class SecQuarterPeriodKind(str, Enum):
    INDEPENDENT_3M = "INDEPENDENT_3M"
    FY = "FY"
    YTD_9M = "YTD_9M"
    INSTANT = "INSTANT"


class SecQuarterPeriodSourceKind(str, Enum):
    DECLARED_LABEL = "DECLARED_LABEL"
    SEC_COMPANYFACTS_FRAME = "SEC_COMPANYFACTS_FRAME"
    DEI_FISCAL_FOCUS = "DEI_FISCAL_FOCUS"


class SecQuarterPeriodResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class SecQuarterlyPeriodEvidence:
    accession_number: str
    fiscal_year: int
    fiscal_quarter: int
    period_kind: SecQuarterPeriodKind
    period_start: date | None
    period_end: date
    source_label: str
    source_kind: SecQuarterPeriodSourceKind = SecQuarterPeriodSourceKind.DECLARED_LABEL

    def __post_init__(self) -> None:
        if type(self.accession_number) is not str or not _ACCESSION.fullmatch(
            self.accession_number
        ):
            raise ValueError("invalid SEC accession number")
        if type(self.fiscal_year) is not int or not 1 <= self.fiscal_year <= 9999:
            raise ValueError("fiscal_year must be in 1..9999")
        if type(self.fiscal_quarter) is not int or self.fiscal_quarter not in range(1, 5):
            raise ValueError("fiscal_quarter must be in 1..4")
        if type(self.period_kind) is not SecQuarterPeriodKind:
            raise TypeError("invalid period kind")
        if type(self.period_end) is not date or isinstance(self.period_end, datetime):
            raise TypeError("period_end must be a date")
        if self.period_start is not None and (
            type(self.period_start) is not date
            or isinstance(self.period_start, datetime)
            or self.period_start > self.period_end
        ):
            raise ValueError("invalid period_start")
        if (
            type(self.source_label) is not str
            or not self.source_label.strip()
            or len(self.source_label.encode("utf-8")) > _MAX_SOURCE_LABEL_BYTES
        ):
            raise ValueError("source_label is required")
        if type(self.source_kind) is not SecQuarterPeriodSourceKind:
            raise TypeError("invalid period source kind")
        label = self.source_label.lower()
        if self.period_kind is SecQuarterPeriodKind.INDEPENDENT_3M:
            if self.source_kind is SecQuarterPeriodSourceKind.SEC_COMPANYFACTS_FRAME:
                match = re.fullmatch(r"CY([0-9]{4})Q([1-4])", self.source_label)
                if match is None or self.fiscal_quarter == 4:
                    raise ValueError("companyfacts frame must be quarterly for a 10-Q duration")
                return
            forbidden = (
                "six months",
                "6 months",
                "nine months",
                "9 months",
                "twelve months",
                "12 months",
                "year ended",
                "year to date",
                "ytd",
                "annual",
                "nine-month",
                "six-month",
                "full year",
            )
            if (
                self.fiscal_quarter == 4
                or any(token in label for token in forbidden)
                or not any(
                    token in label
                    for token in (
                        "three months",
                        "3 months",
                        "three-month",
                        "quarter ended",
                        "13 weeks",
                    )
                )
            ):
                raise ValueError("10-Q independent quarter requires explicit 3-month source label")
        elif self.period_kind is SecQuarterPeriodKind.YTD_9M:
            if self.source_kind is not SecQuarterPeriodSourceKind.DECLARED_LABEL:
                raise ValueError("YTD duration requires explicit nine-month source label")
            if self.fiscal_quarter != 3 or not any(
                token in label for token in ("nine months", "9 months", "nine-month")
            ):
                raise ValueError("YTD evidence requires explicit nine-month label for fiscal Q3")
        elif self.period_kind is SecQuarterPeriodKind.FY:
            if self.source_kind is SecQuarterPeriodSourceKind.SEC_COMPANYFACTS_FRAME:
                raise ValueError("quarterly companyfacts frame cannot establish annual period")
            if self.source_kind is SecQuarterPeriodSourceKind.DEI_FISCAL_FOCUS:
                if self.fiscal_quarter != 4 or self.source_label != "FY":
                    raise ValueError("DEI annual evidence must explicitly declare FY")
                return
            if self.fiscal_quarter != 4 or not any(
                token in label for token in ("year ended", "full year", "fiscal year", "annual")
            ):
                raise ValueError("FY evidence requires explicit annual source label")
        else:
            if self.period_start is not None:
                raise ValueError("instant evidence cannot have period_start")
            if self.source_kind is SecQuarterPeriodSourceKind.SEC_COMPANYFACTS_FRAME:
                raise ValueError("companyfacts duration frame cannot establish instant evidence")


@dataclass(frozen=True)
class SecQuarterlyNativeDurationEvidence:
    """Retained source label bound to one exact native XBRL context interval."""

    context_ref: str
    period_start: date
    period_end: date
    source_label: str
    source_observation_id: str
    source_kind: SecQuarterPeriodSourceKind = SecQuarterPeriodSourceKind.DECLARED_LABEL

    def __post_init__(self) -> None:
        if (
            type(self.context_ref) is not str
            or not self.context_ref
            or len(self.context_ref.encode("utf-8")) > _MAX_CONTEXT_REF_BYTES
        ):
            raise ValueError("context_ref is required")
        if type(self.period_start) is not date or type(self.period_end) is not date:
            raise TypeError("native duration dates must be dates")
        if self.period_start > self.period_end:
            raise ValueError("native duration dates are reversed")
        if type(self.source_observation_id) is not str or not _IDENTITY.fullmatch(
            self.source_observation_id
        ):
            raise ValueError("invalid duration-label source observation identity")
        if type(self.source_kind) is not SecQuarterPeriodSourceKind:
            raise TypeError("invalid native duration source kind")


@dataclass(frozen=True)
class SecQuarterlyFilingPeriodEvidence:
    accession_number: str
    source_observation_id: str
    document_fiscal_year_focus: int | None
    document_fiscal_period_focus: str | None
    document_period_end: date | None
    native_duration_labels: tuple[SecQuarterlyNativeDurationEvidence, ...] = ()

    def __post_init__(self) -> None:
        if type(self.accession_number) is not str or not _ACCESSION.fullmatch(
            self.accession_number
        ):
            raise ValueError("invalid period evidence accession")
        if type(self.source_observation_id) is not str or not _IDENTITY.fullmatch(
            self.source_observation_id
        ):
            raise ValueError("invalid period evidence observation identity")
        if (
            type(self.native_duration_labels) is not tuple
            or len(self.native_duration_labels) > _MAX_DURATION_LABELS
        ):
            raise ValueError("invalid native duration label collection")
        if any(
            type(item) is not SecQuarterlyNativeDurationEvidence
            for item in self.native_duration_labels
        ):
            raise TypeError("invalid native duration label evidence")


@dataclass(frozen=True)
class SecQuarterlyPeriodResolution:
    status: SecQuarterPeriodResolutionStatus
    evidence: tuple[SecQuarterlyPeriodEvidence, ...]
    source_observation_id: str
    reason: str | None = None


def derive_sec_quarterly_period_evidence(
    vintage: SecCompanyFinancialVintage,
    source: SecQuarterlyFilingPeriodEvidence,
) -> SecQuarterlyPeriodResolution:
    """Derive quarter declarations only from DEI filing focus and labeled contexts.

    Date spans alone do not establish independent-quarter or YTD semantics. When the
    retained source has no explicit duration label for a matching XBRL context, the
    result is UNRESOLVED; callers must not infer a label from elapsed days.
    """
    if (
        type(vintage) is not SecCompanyFinancialVintage
        or type(source) is not SecQuarterlyFilingPeriodEvidence
    ):
        raise TypeError("invalid quarterly source evidence")
    if source.accession_number != vintage.accession_number:
        raise ValueError("filing period source accession does not match vintage")
    if type(source.source_observation_id) is not str or not _IDENTITY.fullmatch(
        source.source_observation_id
    ):
        raise ValueError("invalid filing period source observation identity")
    year, focus = source.document_fiscal_year_focus, source.document_fiscal_period_focus
    form = vintage.form.upper()
    if (
        type(year) is not int
        or not 1 <= year <= 9999
        or type(focus) is not str
        or source.document_period_end is None
        or type(source.document_period_end) is not date
        or source.document_period_end != vintage.period_end
    ):
        return SecQuarterlyPeriodResolution(
            SecQuarterPeriodResolutionStatus.UNRESOLVED,
            (),
            source.source_observation_id,
            "DEI fiscal focus or document period end is missing or inconsistent",
        )
    focus = focus.upper()
    if form in {"10-K", "10-K/A"} and focus == "FY":
        quarter, expected_kind = 4, SecQuarterPeriodKind.FY
    elif form in {"10-Q", "10-Q/A"} and focus in {"Q1", "Q2", "Q3"}:
        quarter = int(focus[1])
        expected_kind = SecQuarterPeriodKind.INDEPENDENT_3M
    else:
        return SecQuarterlyPeriodResolution(
            SecQuarterPeriodResolutionStatus.UNRESOLVED,
            (),
            source.source_observation_id,
            "DEI fiscal period focus does not resolve to a supported 10-K/Q period",
        )
    resolved: dict[tuple[object, ...], SecQuarterlyPeriodEvidence] = {}
    for row in vintage.rows:
        if row.period_type == "instant" and row.period_end == source.document_period_end:
            item = SecQuarterlyPeriodEvidence(
                vintage.accession_number,
                year,
                quarter,
                SecQuarterPeriodKind.INSTANT,
                None,
                source.document_period_end,
                f"DEI DocumentFiscalPeriodFocus {focus}",
            )
            resolved[(item.fiscal_year, item.fiscal_quarter, item.period_kind, item.period_end)] = (
                item
            )
            continue
        if (
            row.period_type != "duration"
            or row.period_source != "xbrl-context"
            or row.period_start is None
            or row.period_end != source.document_period_end
            or not row.context_ref
        ):
            continue
        if row.period_end is None:
            continue
        for label in source.native_duration_labels:
            if (
                label.source_observation_id == source.source_observation_id
                and label.context_ref == row.context_ref
                and label.period_start == row.period_start
                and label.period_end == row.period_end
            ):
                kind = expected_kind
                if expected_kind is SecQuarterPeriodKind.INDEPENDENT_3M:
                    normalized = label.source_label.lower()
                    if any(
                        token in normalized
                        for token in ("nine months", "9 months", "year to date", "ytd")
                    ):
                        if quarter != 3 or not any(
                            t in normalized for t in ("nine months", "9 months")
                        ):
                            continue
                        kind = SecQuarterPeriodKind.YTD_9M
                item = SecQuarterlyPeriodEvidence(
                    vintage.accession_number,
                    year,
                    quarter,
                    kind,
                    row.period_start,
                    row.period_end,
                    label.source_label,
                    label.source_kind,
                )
                resolved[
                    (
                        item.fiscal_year,
                        item.fiscal_quarter,
                        item.period_kind,
                        item.period_start,
                        item.period_end,
                        item.source_label,
                    )
                ] = item
    evidence = tuple(resolved[key] for key in sorted(resolved, key=str))
    return SecQuarterlyPeriodResolution(
        SecQuarterPeriodResolutionStatus.RESOLVED
        if evidence
        else SecQuarterPeriodResolutionStatus.UNRESOLVED,
        evidence,
        source.source_observation_id,
        None if evidence else "no exact native period label matches a filing XBRL context",
    )
