"""Typed models, parser protocol, and registry for foreign private issuer Form 6-K extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from ._tsm_6k_parse import SecTsm6KValues, parse_sec_tsm_6k_release
from .errors import ResourceLimitError, SchemaMismatchError

_TSM_CIK = "0001046179"
_MAX_TSM_EX99_BYTES = 128 * 1024
_TSM_EARNINGS_HEADLINE = re.compile(r"TSMC Reports (?:First|Second|Third|Fourth) Quarter EPS")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class SecFpi6KSourceKind(str, Enum):
    """Source extraction mechanism used for one Form 6-K filing."""

    XBRL_ATTACHMENT = "XBRL_ATTACHMENT"
    EX99_PLUGIN = "EX99_PLUGIN"
    UNSUPPORTED = "UNSUPPORTED"
    EMPTY = "EMPTY"


class SecFpi6KShareBasis(str, Enum):
    """Explicit share basis classification; never equates ADR units with ordinary shares."""

    ORDINARY_SHARES = "ordinary_shares"
    ADR_EQUIVALENT_SHARES = "adr_equivalent_shares"
    UNKNOWN = "unknown"


def normalize_fpi_cik(cik: str) -> str:
    """Validate and normalize an SEC CIK into a 10-digit zero-padded string."""
    if type(cik) is not str:
        raise TypeError("cik must be a string")
    cleaned = cik.strip()
    if not cleaned or not cleaned.isascii() or not cleaned.isdecimal() or len(cleaned) > 10:
        raise ValueError("cik must contain 1 to 10 ASCII digits")
    if int(cleaned) <= 0:
        raise ValueError("cik must be positive")
    return cleaned.zfill(10)


@dataclass(frozen=True)
class SecFpi6KMetricRow:
    """One financial metric extracted from a Form 6-K with original currency and share basis."""

    metric: str
    native_tag: str
    value: Decimal
    unit: str
    reporting_currency: str | None = None
    share_basis: SecFpi6KShareBasis = SecFpi6KShareBasis.UNKNOWN
    taxonomy: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_type: str = "duration"
    native_label: str | None = None

    def __post_init__(self) -> None:
        if type(self.metric) is not str or not self.metric.strip():
            raise ValueError("metric must be a non-empty string")
        if type(self.native_tag) is not str or not self.native_tag.strip():
            raise ValueError("native_tag must be a non-empty string")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError("value must be a finite Decimal")
        if type(self.unit) is not str or not self.unit.strip():
            raise ValueError("unit must be a non-empty string")
        if self.reporting_currency is not None and (
            type(self.reporting_currency) is not str
            or _CURRENCY.fullmatch(self.reporting_currency) is None
        ):
            raise ValueError("reporting_currency must be a 3-letter uppercase ISO-4217 code")
        if not isinstance(self.share_basis, SecFpi6KShareBasis):
            raise TypeError("share_basis must be a SecFpi6KShareBasis")
        if self.period_type not in {"duration", "instant"}:
            raise ValueError("period_type must be 'duration' or 'instant'")


@dataclass(frozen=True)
class SecFpi6KQuarterRecord:
    """One Form 6-K filing outcome with retained SnapshotStore provenance."""

    cik: str
    accession_number: str
    form: str
    filing_date: date
    accepted_at: datetime
    period_end: date | None
    source_kind: SecFpi6KSourceKind
    parser_id: str | None
    reporting_currency: str | None
    share_basis: SecFpi6KShareBasis
    metrics: tuple[SecFpi6KMetricRow, ...]
    index_observation_id: str
    source_observation_id: str | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    index_url: str | None = None
    source_url: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SecFpi6KResult:
    """Chronological Form 6-K extraction results for one foreign private issuer CIK."""

    cik: str
    records: tuple[SecFpi6KQuarterRecord, ...]
    submissions_observation_ids: tuple[str, ...]
    coverage_complete: bool
    unsupported_accessions: tuple[str, ...] = ()
    empty_accessions: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecFpi6KExhibitContext:
    """Filing metadata passed to a registered EX-99.1 plugin parser."""

    cik: str
    accession_number: str
    form: str
    filing_date: date
    report_date: date | None
    accepted_at: datetime
    primary_document: str | None
    exhibit_document: str
    exhibit_type: str


@dataclass(frozen=True)
class SecFpi6KPluginParseOutput:
    """Structured metrics returned by an issuer-specific Form 6-K EX-99.1 plugin."""

    period_end: date
    metrics: tuple[SecFpi6KMetricRow, ...]
    reporting_currency: str | None = None
    share_basis: SecFpi6KShareBasis = SecFpi6KShareBasis.UNKNOWN
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None


@runtime_checkable
class SecFpi6KExhibitParser(Protocol):
    """Protocol for issuer-specific Form 6-K EX-99.1 HTML exhibit parsers."""

    @property
    def parser_id(self) -> str:
        """Stable versioned identifier for provenance tracking."""

    def parse(
        self,
        body: bytes,
        *,
        context: SecFpi6KExhibitContext,
    ) -> SecFpi6KPluginParseOutput | None:
        """Parse one EX-99.1 exhibit body.

        Return ``None`` only when the exhibit is a non-earnings 6-K filing that
        this plugin intentionally declines. Malformed or inconsistent earnings
        releases must fail closed by raising ``SchemaMismatchError``.
        """


class SecFpi6KParserRegistry:
    """Registry mapping normalized 10-digit SEC CIKs to EX-99.1 exhibit parsers."""

    def __init__(self) -> None:
        self._parsers: dict[str, SecFpi6KExhibitParser] = {}

    def register(
        self,
        cik: str,
        parser: SecFpi6KExhibitParser,
        *,
        overwrite: bool = False,
    ) -> None:
        normalized = normalize_fpi_cik(cik)
        if not isinstance(parser, SecFpi6KExhibitParser):
            raise TypeError("parser must implement SecFpi6KExhibitParser")
        if type(parser.parser_id) is not str or not parser.parser_id.strip():
            raise ValueError("parser.parser_id must be a non-empty string")
        if normalized in self._parsers and not overwrite:
            raise ValueError(f"Form 6-K exhibit parser already registered for CIK {normalized}")
        self._parsers[normalized] = parser

    def get(self, cik: str) -> SecFpi6KExhibitParser | None:
        return self._parsers.get(normalize_fpi_cik(cik))

    def has(self, cik: str) -> bool:
        return normalize_fpi_cik(cik) in self._parsers

    def registered_ciks(self) -> tuple[str, ...]:
        return tuple(sorted(self._parsers))

    def copy(self) -> SecFpi6KParserRegistry:
        cloned = SecFpi6KParserRegistry()
        cloned._parsers.update(self._parsers)
        return cloned


def _tsm_values_to_output(values: SecTsm6KValues) -> SecFpi6KPluginParseOutput:
    million = Decimal(1_000_000)
    billion = Decimal(1_000_000_000)
    rows = (
        SecFpi6KMetricRow(
            metric="revenue",
            native_tag="Net sales",
            value=values.revenue_twd_million * million,
            unit="TWD",
            reporting_currency="TWD",
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Net sales (NT$ million)",
        ),
        SecFpi6KMetricRow(
            metric="gross_profit",
            native_tag="Gross profit",
            value=values.gross_profit_twd_million * million,
            unit="TWD",
            reporting_currency="TWD",
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Gross profit (NT$ million)",
        ),
        SecFpi6KMetricRow(
            metric="operating_income",
            native_tag="Income from operations",
            value=values.operating_income_twd_million * million,
            unit="TWD",
            reporting_currency="TWD",
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Income from operations (NT$ million)",
        ),
        SecFpi6KMetricRow(
            metric="income_before_tax",
            native_tag="Income before tax",
            value=values.income_before_tax_twd_million * million,
            unit="TWD",
            reporting_currency="TWD",
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Income before tax (NT$ million)",
        ),
        SecFpi6KMetricRow(
            metric="net_income",
            native_tag="Net income",
            value=values.net_income_twd_million * million,
            unit="TWD",
            reporting_currency="TWD",
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Net income (NT$ million)",
        ),
        SecFpi6KMetricRow(
            metric="diluted_eps",
            native_tag="EPS (NT$)",
            value=values.diluted_eps_twd_per_ordinary_share,
            unit="TWD/shares",
            reporting_currency="TWD",
            share_basis=SecFpi6KShareBasis.ORDINARY_SHARES,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="EPS (NT$ per ordinary share)",
        ),
        SecFpi6KMetricRow(
            metric="revenue_usd_reported",
            native_tag="USD revenue",
            value=values.revenue_usd_billion * billion,
            unit="USD",
            reporting_currency="USD",
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="In US dollars, quarter revenue ($ billion)",
        ),
        SecFpi6KMetricRow(
            metric="diluted_eps_adr",
            native_tag="USD per ADR",
            value=values.diluted_eps_usd_per_adr,
            unit="USD/ADR",
            reporting_currency="USD",
            share_basis=SecFpi6KShareBasis.ADR_EQUIVALENT_SHARES,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Diluted EPS (US$ per ADR unit)",
        ),
        SecFpi6KMetricRow(
            metric="gross_margin_pct",
            native_tag="Gross margin",
            value=values.gross_margin_pct,
            unit="%",
            reporting_currency=None,
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Gross margin (%)",
        ),
        SecFpi6KMetricRow(
            metric="operating_margin_pct",
            native_tag="Operating margin",
            value=values.operating_margin_pct,
            unit="%",
            reporting_currency=None,
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy="ex99_plugin",
            period_end=values.period_end,
            native_label="Operating margin (%)",
        ),
    )
    return SecFpi6KPluginParseOutput(
        period_end=values.period_end,
        metrics=rows,
        reporting_currency="TWD",
        share_basis=SecFpi6KShareBasis.ORDINARY_SHARES,
        fiscal_year=values.fiscal_year,
        fiscal_quarter=values.fiscal_quarter,
    )


class SecTsm6KExhibitParser:
    """Built-in Form 6-K EX-99.1 plugin for TSMC delegating to ``parse_sec_tsm_6k_release``."""

    @property
    def parser_id(self) -> str:
        return "tsmc-6k-ex99-v1"

    def parse(
        self,
        body: bytes,
        *,
        context: SecFpi6KExhibitContext,
    ) -> SecFpi6KPluginParseOutput | None:
        if type(body) is not bytes or not 0 < len(body) <= _MAX_TSM_EX99_BYTES:
            raise ResourceLimitError("TSM 6-K exhibit exceeds byte limit")
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SchemaMismatchError("TSM 6-K exhibit must be UTF-8") from exc
        if _TSM_EARNINGS_HEADLINE.search(html) is None:
            return None
        if context.report_date is None:
            raise SchemaMismatchError("TSM 6-K earnings release is missing report_date")
        values = parse_sec_tsm_6k_release(body, expected_period_end=context.report_date)
        return _tsm_values_to_output(values)


def default_fpi_6k_registry() -> SecFpi6KParserRegistry:
    """Return a fresh registry with built-in FPI EX-99.1 parsers (including TSMC)."""
    registry = SecFpi6KParserRegistry()
    registry.register(_TSM_CIK, SecTsm6KExhibitParser())
    return registry


__all__ = [
    "SecFpi6KExhibitContext",
    "SecFpi6KExhibitParser",
    "SecFpi6KMetricRow",
    "SecFpi6KParserRegistry",
    "SecFpi6KPluginParseOutput",
    "SecFpi6KQuarterRecord",
    "SecFpi6KResult",
    "SecFpi6KShareBasis",
    "SecFpi6KSourceKind",
    "SecTsm6KExhibitParser",
    "default_fpi_6k_registry",
    "normalize_fpi_cik",
]
