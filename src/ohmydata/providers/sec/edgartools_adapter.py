"""Adapter bridging edgartools with OMD's Point-in-Time and credential-injected architecture."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from .financials import (
    SecCompanyFinancialVintage,
    SecFinancialsRequest,
    SecStatementRow,
    StatementType,
)

logger = logging.getLogger(__name__)

_EASTERN_TZ = ZoneInfo("America/New_York")
_DATE_COL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


import importlib


def ensure_edgar_available() -> None:
    """Ensure that edgartools is installed, or raise a friendly error."""
    try:
        importlib.import_module("edgar")
    except ImportError as exc:
        raise ImportError(
            "edgartools is required for SEC company financials. "
            "Install it with: uv sync --extra sec-financials (or pip install 'ohmydata[sec-financials]')"
        ) from exc


def validate_user_agent(user_agent: str) -> str:
    """Validate that user_agent is compliant with SEC requirements."""
    if not user_agent:
        raise ValueError("User-Agent cannot be empty")
    cleaned = user_agent.strip()
    if not cleaned:
        raise ValueError("User-Agent cannot be whitespace only")
    if "@" not in cleaned and "." not in cleaned:
        raise ValueError(
            f"User-Agent should include an email or domain per SEC rules, got: {cleaned!r}"
        )
    return cleaned


def _to_decimal(val: Any) -> Decimal | None:
    if val is None or val == "" or str(val).lower() in ("nan", "none", "null"):
        return None
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_period_date(col_name: str) -> date | None:
    if _DATE_COL_PATTERN.match(col_name):
        try:
            return date.fromisoformat(col_name)
        except ValueError:
            return None
    return None


def parse_statement_rows(
    statement: Any,
    statement_type: StatementType,
    *,
    include_dimensions: bool = False,
) -> list[SecStatementRow]:
    """Parse an edgartools Statement object into structured SecStatementRow records."""
    if statement is None:
        return []

    ensure_edgar_available()
    try:
        df = statement.to_dataframe(
            standard=True,
            include_unit=True,
            include_point_in_time=True,
            include_standardization=True,
        )
    except (AttributeError, TypeError, ValueError, KeyError) as err:
        logger.debug("Failed to extract dataframe from statement: %s", err)
        return []

    if df is None or getattr(df, "empty", True):
        return []

    # Identify date/period columns (e.g. "2023-09-30")
    period_cols: list[str] = [c for c in df.columns if _parse_period_date(str(c)) is not None]

    rows: list[SecStatementRow] = []
    for _, item in df.iterrows():
        # Skip purely abstract header labels (e.g. "Operating expenses:")
        if item.get("abstract") is True:
            continue
        # Skip dimensional breakdown segments unless requested
        if not include_dimensions and item.get("dimension") is True:
            continue

        label = str(item.get("label") or "").strip()
        concept = str(item.get("concept") or "").strip()
        standard_concept = str(item.get("standard_concept") or concept).strip()
        unit = str(item.get("unit") or "USD").strip() if item.get("unit") else "USD"
        is_pit = bool(item.get("point_in_time", False))

        for col in period_cols:
            raw_val = item.get(col)
            if raw_val is None or str(raw_val).lower() in ("nan", "none", ""):
                continue

            dec_val = _to_decimal(raw_val)
            p_end = _parse_period_date(col)

            rows.append(
                SecStatementRow(
                    statement_type=statement_type,
                    standard_concept=standard_concept,
                    concept=concept,
                    label=label,
                    value=dec_val,
                    value_native=str(raw_val) if raw_val is not None else None,
                    unit=unit,
                    period_end=p_end,
                    is_point_in_time=is_pit,
                )
            )

    return rows


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
            company: Any = Company(symbol)
            filings: Any = company.get_filings(form=list(request.forms))
            if not filings:
                continue

            filings_to_process: list[Any]
            if request.limit is not None and hasattr(filings, "latest"):
                latest_res: Any = filings.latest(request.limit)
                if isinstance(latest_res, (list, tuple)):
                    filings_to_process = list(cast(list[Any], latest_res))
                else:
                    filings_to_process = [latest_res] if latest_res else []
            else:
                filings_to_process = list(filings)

            for filing in filings_to_process:
                form = str(filing.form).upper()
                is_amend = form.endswith("/A")
                if is_amend and not request.include_amendments:
                    continue

                f_date_str = str(filing.filing_date)
                try:
                    f_date = date.fromisoformat(f_date_str)
                except ValueError:
                    continue

                if request.start_year and f_date.year < request.start_year:
                    continue
                if request.end_year and f_date.year > request.end_year:
                    continue

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
                try:
                    report: Any = filing.obj()
                except (AttributeError, KeyError, ValueError, TypeError, OSError) as err:
                    logger.debug("Could not parse filing obj: %s", err)
                    continue

                if report is None or not hasattr(report, "financials"):
                    continue

                fin: Any = getattr(report, "financials", None)
                if fin is None:
                    continue

                rows: list[SecStatementRow] = []
                # 1. Balance sheet
                try:
                    bs: Any = fin.balance_sheet()
                    rows.extend(parse_statement_rows(bs, "balance_sheet"))
                except (AttributeError, KeyError, ValueError, TypeError) as err:
                    logger.debug("Could not extract balance_sheet: %s", err)

                # 2. Income statement
                try:
                    inc: Any = fin.income_statement()
                    rows.extend(parse_statement_rows(inc, "income_statement"))
                except (AttributeError, KeyError, ValueError, TypeError) as err:
                    logger.debug("Could not extract income_statement: %s", err)

                # 3. Cash flow statement
                try:
                    cf: Any = fin.cash_flow_statement()
                    rows.extend(parse_statement_rows(cf, "cash_flow"))
                except (AttributeError, KeyError, ValueError, TypeError) as err:
                    logger.debug("Could not extract cash_flow: %s", err)

                if not rows:
                    continue

                # Period of report date
                p_end: date | None = None
                try:
                    if hasattr(filing, "period_of_report") and filing.period_of_report:
                        p_end = date.fromisoformat(str(filing.period_of_report))
                except (ValueError, TypeError) as err:
                    logger.debug("Could not parse period_of_report: %s", err)

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
                    rows=tuple(rows),
                )
                vintages.append(vintage)

        return vintages
