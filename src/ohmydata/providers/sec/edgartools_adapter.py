"""Injected-identity SEC client and financial statement parser exports."""

from __future__ import annotations

import hashlib
import importlib
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ._edgartools_instance import (
    _has_xbrl_attachment_candidates,
    _instance_attachment,
    _instance_url,
    _load_user_agent_from_config,
    _read_instance,
    _try_instance_attachment,
)
from ._financials_filing_selection import (
    amendment_base_accessions,
    select_financial_filing_candidates,
)
from ._statement_parser import (
    SecStatementParseError,
    SecUnitEvidenceError,
    parse_statement_rows,
)
from .adr_metadata import build_adr_share_metadata
from .financials import (
    FPI_FINANCIAL_FORMS,
    SecCompanyFinancialVintage,
    SecFinancialsRequest,
    SecStatementRow,
    normalize_sec_cik,
)
from .http import SecHttpClient
from .unit_evidence import SEC_LIVE_FINANCIAL_PARSER_V2, SecFinancialUnitEvidence

logger = logging.getLogger(__name__)
_EASTERN_TZ = ZoneInfo("America/New_York")
_LIVE_PARSER_V1 = "sec-live-financial-parser-v1-edgartools-6.4.0"


class SecFinancialsParseError(SecStatementParseError):
    """A selected filing produced no financial rows because parsing failed.

    ``vintage`` retains accession and coverage flags; ``__cause__`` retains
    the first parser failure. Neither is replaced by a different filing.
    """

    def __init__(self, vintage: SecCompanyFinancialVintage) -> None:
        super().__init__("selected SEC filing produced no financial rows because parsing failed")
        self.vintage = vintage


def ensure_edgar_available() -> None:
    """Require the optional Edgar financials dependency."""
    try:
        importlib.import_module("edgar")
    except ImportError as exc:
        raise ImportError(
            "edgartools is required for SEC company financials. "
            "Install it with: uv sync --extra sec-financials"
        ) from exc


def validate_user_agent(user_agent: str) -> str:
    """Validate injected SEC identity without echoing caller contact details."""
    if not user_agent:
        raise ValueError("User-Agent cannot be empty")
    cleaned = user_agent.strip()
    if not cleaned:
        raise ValueError("User-Agent cannot be whitespace only")
    if "@" not in cleaned and "." not in cleaned:
        raise ValueError("User-Agent should include an email or domain per SEC rules")
    return cleaned


def _resolve_company_symbol(company: Any, filing: Any) -> str:
    """Resolve uppercase ticker from entity metadata, or '' for delisted/tickerless companies."""
    for attr in ("get_ticker", "ticker", "tickers"):
        try:
            candidate = getattr(company, attr, None)
            value = candidate() if callable(candidate) else candidate
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip().upper()
    try:
        filing_ticker = getattr(filing, "ticker", None)
        if isinstance(filing_ticker, str) and filing_ticker.strip():
            return filing_ticker.strip().upper()
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return ""


def _parse_acceptance_datetime(filing: Any) -> datetime | None:
    try:
        header: Any = getattr(filing, "header", None)
        if header and header.acceptance_datetime:
            dt: datetime = header.acceptance_datetime
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_EASTERN_TZ)
            return dt.astimezone(UTC)
    except (AttributeError, KeyError, ValueError, TypeError) as err:
        logger.debug("Could not parse acceptance_datetime from filing header: %s", err)
    return None


def _parse_period_end(filing: Any, label: str) -> date | None:
    try:
        if hasattr(filing, "period_of_report") and filing.period_of_report:
            return date.fromisoformat(str(filing.period_of_report))
    except (ValueError, TypeError) as err:
        raise ValueError(
            f"invalid period_of_report for {label}: {filing.period_of_report!r}"
        ) from err
    return None


def _statement_getter(fin: Any, method_name: str) -> Callable[[], Any] | None:
    getter = getattr(fin, method_name, None)
    if callable(getter):
        return getter
    if method_name == "cash_flow_statement":
        alt = getattr(fin, "cashflow_statement", None)
        if callable(alt):
            return alt
    return None


def _filing_cik_str(filing: Any, fallback_cik: str | None) -> str:
    raw = getattr(filing, "cik", None)
    if raw is not None and str(raw).strip():
        return normalize_sec_cik(str(raw))
    if fallback_cik is not None:
        return normalize_sec_cik(fallback_cik)
    raise ValueError("filing CIK is unavailable")


class SecFinancialsClient:
    """Injected-credentials client for SEC company financials."""

    def __init__(
        self,
        user_agent: str,
        *,
        runner: Callable[..., Any] | None = None,
        http_client: SecHttpClient | None = None,
    ) -> None:
        self.user_agent = validate_user_agent(user_agent)
        self.runner = runner
        self.http_client = http_client

        if self.runner is None:
            ensure_edgar_available()
            from edgar import set_identity

            set_identity(self.user_agent)

    @classmethod
    def from_config(cls, config_path: str | Path, **kwargs: Any) -> SecFinancialsClient:
        """Create a client from an OMD configuration file (YAML, JSON, or TOML)."""
        return cls(_load_user_agent_from_config(config_path), **kwargs)

    def fetch_company_financials(
        self, request: SecFinancialsRequest
    ) -> list[SecCompanyFinancialVintage]:
        """Fetch and parse financial statement vintages according to request."""
        if self.runner is not None:
            res: list[SecCompanyFinancialVintage] = self.runner(request)
            if request.parser_version == SEC_LIVE_FINANCIAL_PARSER_V2:
                for item in res:
                    is_non_xbrl_6k = (
                        item.form == "6-K" and "NON_XBRL_6K" in item.quality_flags and not item.rows
                    )
                    if item.unit_evidence is None and not is_non_xbrl_6k:
                        raise SecUnitEvidenceError(
                            "custom v2 runner returned a vintage without unit evidence"
                        )
                    if (
                        item.unit_evidence is not None
                        and item.unit_evidence.parser_version != SEC_LIVE_FINANCIAL_PARSER_V2
                    ):
                        raise SecUnitEvidenceError(
                            "custom v2 runner returned a vintage without unit evidence"
                        )
            return res

        ensure_edgar_available()
        from edgar import Company
        from edgar.exceptions import DataObjectError, ParsingError, StatementNotFoundError

        targets: list[tuple[str, str]] = [
            ("symbol", symbol.strip().upper()) for symbol in request.symbols
        ] + [("cik", cik) for cik in request.ciks]

        vintages: list[SecCompanyFinancialVintage] = []
        for target_kind, target_value in targets:
            company: Any = Company(target_value)
            start = f"{request.start_year}-01-01" if request.start_year else ""
            end = f"{request.end_year}-12-31" if request.end_year else ""
            filings: Any = company.get_filings(
                form=list(request.forms),
                amendments=request.include_amendments,
                filing_date=f"{start}:{end}" if start or end else None,
            )
            if not filings:
                continue

            target_label = target_value if target_kind == "symbol" else f"CIK:{target_value}"
            eligible = select_financial_filing_candidates(filings, request, symbol=target_label)
            amendment_bases = amendment_base_accessions(eligible)
            filings_to_process = (
                eligible[: request.limit] if request.limit is not None else eligible
            )

            for filing in filings_to_process:
                form = str(filing.form).strip().upper()
                is_amend = form.endswith("/A")
                f_date_str = str(filing.filing_date)
                try:
                    f_date = date.fromisoformat(f_date_str)
                except ValueError as err:
                    raise ValueError(
                        f"invalid filing date for {target_label}: {f_date_str!r}"
                    ) from err

                resolved_symbol = (
                    target_value
                    if target_kind == "symbol"
                    else _resolve_company_symbol(company, filing)
                )
                resolved_cik = _filing_cik_str(
                    filing, target_value if target_kind == "cik" else None
                )
                if target_kind == "cik" and resolved_cik != target_value:
                    raise ValueError(
                        f"filing CIK {resolved_cik} does not match requested CIK {target_value}"
                    )
                company_name = str(
                    getattr(filing, "company", None) or getattr(company, "name", "") or ""
                )
                accepted_at = _parse_acceptance_datetime(filing)
                p_end = _parse_period_end(filing, target_label)

                quality_flags: list[str] = []
                if accepted_at is None:
                    quality_flags.append("ACCEPTED_AT_MISSING")

                raw_instance: bytes | None = None
                evidence: SecFinancialUnitEvidence | None = None
                prepared_evidence: Any | None = None
                if request.parser_version == SEC_LIVE_FINANCIAL_PARSER_V2:
                    if form == "6-K":
                        attached = _try_instance_attachment(filing, self.http_client)
                        if attached is None:
                            quality_flags.extend(
                                ["NON_XBRL_6K", "NO_XBRL_ATTACHMENT", "NO_FINANCIAL_STATEMENTS"]
                            )
                            if not request.include_dimensions:
                                quality_flags.append("DIMENSIONS_EXCLUDED_BY_REQUEST")
                            vintages.append(
                                SecCompanyFinancialVintage(
                                    symbol=resolved_symbol,
                                    cik=resolved_cik,
                                    company_name=company_name,
                                    form=form,
                                    accession_number=str(filing.accession_number),
                                    filing_date=f_date,
                                    period_end=p_end,
                                    accepted_at=accepted_at,
                                    availability_policy=request.availability_policy,
                                    availability_lag_days=request.lag_days,
                                    is_amendment=is_amend,
                                    quality_flags=tuple(quality_flags),
                                    rows=(),
                                    unit_evidence=None,
                                    adr_metadata=None,
                                )
                            )
                            continue
                        document, raw_instance, _ = attached
                    else:
                        document, raw_instance, _ = _instance_attachment(filing, self.http_client)

                    from ._live_unit_corroboration import (
                        SecUnitEvidenceError as _RawUnitEvidenceError,
                    )
                    from ._live_unit_corroboration import prepare_raw_instance

                    try:
                        prepared_evidence = prepare_raw_instance(
                            raw_instance, expected_cik=str(int(resolved_cik))
                        )
                    except _RawUnitEvidenceError as exc:
                        raise SecUnitEvidenceError(str(exc)) from exc
                    evidence = SecFinancialUnitEvidence(
                        parser_version=request.parser_version,
                        cik=str(int(resolved_cik)),
                        accession_number=str(filing.accession_number),
                        instance_document=document,
                        instance_sha256=hashlib.sha256(raw_instance).hexdigest(),
                    )

                parse_failures: list[Exception] = []
                filing_xbrl: Any | None = None
                try:
                    report: Any = filing.obj()
                except (
                    AttributeError,
                    KeyError,
                    ValueError,
                    TypeError,
                    OSError,
                    ParsingError,
                    DataObjectError,
                ) as err:
                    logger.debug("Could not parse filing obj: %s", type(err).__name__)
                    report = None
                    quality_flags.append("FILING_PARSE_FAILED")
                    parse_failures.append(err)

                try:
                    fin: Any = getattr(report, "financials", None) if report is not None else None
                except (
                    AttributeError,
                    KeyError,
                    ValueError,
                    TypeError,
                    RuntimeError,
                    OSError,
                    ParsingError,
                    DataObjectError,
                ) as err:
                    logger.debug("Could not access filing financials: %s", type(err).__name__)
                    fin = None
                    quality_flags.append("FINANCIALS_PARSE_FAILED")
                    parse_failures.append(err)

                if fin is None and form in FPI_FINANCIAL_FORMS:
                    xbrl_getter = getattr(filing, "xbrl", None)
                    if callable(xbrl_getter):
                        try:
                            filing_xbrl = xbrl_getter()
                            fin = getattr(filing_xbrl, "statements", None)
                        except (
                            AttributeError,
                            KeyError,
                            ValueError,
                            TypeError,
                            RuntimeError,
                            OSError,
                            ParsingError,
                            DataObjectError,
                        ) as err:
                            logger.debug("Could not access FPI filing xbrl: %s", type(err).__name__)

                if fin is None:
                    if form == "6-K" and not _has_xbrl_attachment_candidates(filing):
                        quality_flags.extend(["NON_XBRL_6K", "NO_XBRL_ATTACHMENT"])
                    quality_flags.append("NO_FINANCIALS_OBJECT")

                rows: list[SecStatementRow] = []
                if fin is not None:
                    for method_name, statement_type, flag in (
                        ("balance_sheet", "balance_sheet", "BALANCE_SHEET"),
                        ("income_statement", "income_statement", "INCOME_STATEMENT"),
                        ("cash_flow_statement", "cash_flow", "CASH_FLOW"),
                    ):
                        getter = _statement_getter(fin, method_name)
                        if getter is None:
                            quality_flags.append(f"{flag}_MISSING")
                            continue
                        try:
                            statement = getter()
                            if statement is None:
                                quality_flags.append(f"{flag}_MISSING")
                            else:
                                if filing_xbrl is None:
                                    filing_xbrl = getattr(statement, "xbrl", None)
                                statement_rows = parse_statement_rows(
                                    statement,
                                    statement_type,
                                    include_dimensions=request.include_dimensions,
                                    parser_version=request.parser_version,
                                    raw_instance=raw_instance,
                                    _prepared_evidence=prepared_evidence,
                                )
                                rows.extend(statement_rows)
                                if not statement_rows:
                                    quality_flags.append(f"{flag}_EMPTY")
                        except SecUnitEvidenceError:
                            raise
                        except StatementNotFoundError:
                            quality_flags.append(f"{flag}_MISSING")
                        except (
                            AttributeError,
                            KeyError,
                            ValueError,
                            TypeError,
                            ParsingError,
                            DataObjectError,
                        ) as err:
                            logger.debug(
                                "Could not extract %s: %s", statement_type, type(err).__name__
                            )
                            quality_flags.append(f"{flag}_PARSE_FAILED")
                            parse_failures.append(err)

                if not rows:
                    quality_flags.append("NO_FINANCIAL_STATEMENTS")
                    if is_amend:
                        quality_flags.append("AMENDMENT_WITHOUT_FINANCIAL_STATEMENTS")
                base_accession = amendment_bases.get(str(filing.accession_number))
                if is_amend and base_accession is not None:
                    quality_flags.append(f"AMENDMENT_BASE_ACCESSION:{base_accession}")
                if not request.include_dimensions:
                    quality_flags.append("DIMENSIONS_EXCLUDED_BY_REQUEST")

                adr_metadata = build_adr_share_metadata(
                    rows,
                    xbrl=filing_xbrl,
                    raw_instance=raw_instance,
                    form=form,
                )

                vintage = SecCompanyFinancialVintage(
                    symbol=resolved_symbol,
                    cik=resolved_cik,
                    company_name=company_name,
                    form=form,
                    accession_number=str(filing.accession_number),
                    filing_date=f_date,
                    period_end=p_end,
                    accepted_at=accepted_at,
                    availability_policy=request.availability_policy,
                    availability_lag_days=request.lag_days,
                    is_amendment=is_amend,
                    quality_flags=tuple(quality_flags),
                    rows=tuple(rows),
                    unit_evidence=evidence,
                    adr_metadata=adr_metadata,
                )
                if not rows and parse_failures:
                    raise SecFinancialsParseError(vintage) from parse_failures[0]
                vintages.append(vintage)

        return vintages


__all__ = [
    "SecFinancialsClient",
    "SecFinancialsParseError",
    "SecStatementParseError",
    "_instance_attachment",
    "_instance_url",
    "_read_instance",
    "ensure_edgar_available",
    "parse_statement_rows",
    "validate_user_agent",
]
