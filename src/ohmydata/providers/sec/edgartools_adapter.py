"""Injected-identity SEC client and financial statement parser exports."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ._statement_parser import SecStatementParseError, parse_statement_rows
from .financials import SecCompanyFinancialVintage, SecFinancialsRequest, SecStatementRow

logger = logging.getLogger(__name__)
_EASTERN_TZ = ZoneInfo("America/New_York")


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


class SecFinancialsClient:
    """Injected-credentials client for SEC company financials."""

    def __init__(
        self,
        user_agent: str,
        *,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.user_agent = validate_user_agent(user_agent)
        self.runner = runner

        # Set identity in edgartools if not using a custom runner
        if self.runner is None:
            ensure_edgar_available()
            from edgar import set_identity

            set_identity(self.user_agent)

    @classmethod
    def from_config(cls, config_path: str | Path, **kwargs: Any) -> SecFinancialsClient:
        """Create a client from an OMD configuration file (YAML, JSON, or TOML)."""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")

        text = path.read_text(encoding="utf-8")
        raw: dict[str, Any]
        if path.suffix in (".yaml", ".yml"):
            try:
                yaml: Any = importlib.import_module("yaml")
                raw = yaml.safe_load(text) or {}
            except ImportError:
                import json

                raw = json.loads(text)
        elif path.suffix == ".toml":
            import tomllib

            raw = tomllib.loads(text)
        else:
            import json

            raw = json.loads(text)

        user_agent = raw.get("user_agent")
        if not user_agent and raw.get("user_agent_file"):
            ua_file = Path(raw["user_agent_file"])
            if not ua_file.is_absolute():
                ua_file = path.parent / ua_file
            if ua_file.exists():
                user_agent = ua_file.read_text(encoding="utf-8").strip()

        if not user_agent:
            raise ValueError(
                "config must specify either 'user_agent' or 'user_agent_file' pointing to contact info"
            )

        return cls(user_agent, **kwargs)

    def fetch_company_financials(
        self, request: SecFinancialsRequest
    ) -> list[SecCompanyFinancialVintage]:
        """Fetch and parse financial statement vintages according to request."""
        if self.runner is not None:
            res: list[SecCompanyFinancialVintage] = self.runner(request)
            return res

        ensure_edgar_available()
        from edgar import Company

        vintages: list[SecCompanyFinancialVintage] = []
        for symbol in request.symbols:
            symbol = symbol.strip().upper()
            company: Any = Company(symbol)
            start = f"{request.start_year}-01-01" if request.start_year else ""
            end = f"{request.end_year}-12-31" if request.end_year else ""
            filings: Any = company.get_filings(
                form=list(request.forms),
                amendments=request.include_amendments,
                filing_date=f"{start}:{end}" if start or end else None,
            )
            if not filings:
                continue

            # Filter the complete collection before applying limit.  latest(1)
            # returns a Filing while latest(n>1) returns a Filings collection.
            candidates = list(filings)
            eligible: list[Any] = []
            requested_forms = {str(x).upper() for x in request.forms}
            for candidate in candidates:
                form = str(getattr(candidate, "form", "")).upper()
                base_form = form.removesuffix("/A")
                if form not in requested_forms and not (
                    request.include_amendments and base_form in requested_forms
                ):
                    continue
                if form.endswith("/A") and not request.include_amendments:
                    continue
                try:
                    candidate_date = date.fromisoformat(str(candidate.filing_date))
                except (TypeError, ValueError) as err:
                    raise ValueError(
                        f"invalid filing date for {symbol}: {getattr(candidate, 'filing_date', None)!r}"
                    ) from err
                if request.start_year and candidate_date.year < request.start_year:
                    continue
                if request.end_year and candidate_date.year > request.end_year:
                    continue
                eligible.append(candidate)
            eligible.sort(key=lambda f: (str(f.filing_date), str(f.accession_number)), reverse=True)
            filings_to_process = (
                eligible[: request.limit] if request.limit is not None else eligible
            )

            for filing in filings_to_process:
                form = str(filing.form).upper()
                is_amend = form.endswith("/A")
                f_date_str = str(filing.filing_date)
                try:
                    f_date = date.fromisoformat(f_date_str)
                except ValueError as err:
                    raise ValueError(f"invalid filing date for {symbol}: {f_date_str!r}") from err

                # Obtain acceptance timestamp
                accepted_at: datetime | None = None
                try:
                    header: Any = getattr(filing, "header", None)
                    if header and header.acceptance_datetime:
                        dt: datetime = header.acceptance_datetime
                        if dt.tzinfo is None:
                            # SEC acceptance datetimes in SGML are Eastern Time
                            dt = dt.replace(tzinfo=_EASTERN_TZ)
                        accepted_at = dt.astimezone(UTC)
                except (AttributeError, KeyError, ValueError, TypeError) as err:
                    logger.debug("Could not parse acceptance_datetime from filing header: %s", err)

                # Parse the report object (TenK, TenQ, etc.)
                quality_flags: list[str] = []
                parse_failures: list[Exception] = []
                if accepted_at is None:
                    quality_flags.append("ACCEPTED_AT_MISSING")
                try:
                    report: Any = filing.obj()
                except (AttributeError, KeyError, ValueError, TypeError, OSError) as err:
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
                ) as err:
                    logger.debug("Could not access filing financials: %s", type(err).__name__)
                    fin = None
                    quality_flags.append("FINANCIALS_PARSE_FAILED")
                    parse_failures.append(err)
                if fin is None:
                    quality_flags.append("NO_FINANCIALS_OBJECT")

                rows: list[SecStatementRow] = []
                if fin is not None:
                    for method_name, statement_type, flag in (
                        ("balance_sheet", "balance_sheet", "BALANCE_SHEET"),
                        ("income_statement", "income_statement", "INCOME_STATEMENT"),
                        ("cash_flow_statement", "cash_flow", "CASH_FLOW"),
                    ):
                        try:
                            statement = getattr(fin, method_name)()
                            if statement is None:
                                quality_flags.append(f"{flag}_MISSING")
                            else:
                                statement_rows = parse_statement_rows(
                                    statement,
                                    statement_type,
                                    include_dimensions=request.include_dimensions,
                                )
                                rows.extend(statement_rows)
                                if not statement_rows:
                                    quality_flags.append(f"{flag}_EMPTY")
                        except (AttributeError, KeyError, ValueError, TypeError) as err:
                            logger.debug(
                                "Could not extract %s: %s", statement_type, type(err).__name__
                            )
                            quality_flags.append(f"{flag}_PARSE_FAILED")
                            parse_failures.append(err)

                if not rows:
                    quality_flags.append("NO_FINANCIAL_STATEMENTS")
                if not request.include_dimensions:
                    quality_flags.append("DIMENSIONS_EXCLUDED_BY_REQUEST")

                # Period of report date
                p_end: date | None = None
                try:
                    if hasattr(filing, "period_of_report") and filing.period_of_report:
                        p_end = date.fromisoformat(str(filing.period_of_report))
                except (ValueError, TypeError) as err:
                    raise ValueError(
                        f"invalid period_of_report for {symbol}: {filing.period_of_report!r}"
                    ) from err

                vintage = SecCompanyFinancialVintage(
                    symbol=symbol,
                    cik=str(filing.cik).zfill(10),
                    company_name=str(filing.company),
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
                )
                if not rows and parse_failures:
                    raise SecFinancialsParseError(vintage) from parse_failures[0]
                vintages.append(vintage)

        return vintages


__all__ = [
    "SecFinancialsClient",
    "SecFinancialsParseError",
    "SecStatementParseError",
    "ensure_edgar_available",
    "parse_statement_rows",
    "validate_user_agent",
]
