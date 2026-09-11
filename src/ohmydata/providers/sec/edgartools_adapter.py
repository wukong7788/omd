"""Injected-identity SEC client and financial statement parser exports."""

from __future__ import annotations

import hashlib
import importlib
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ._statement_parser import (
    SecStatementParseError,
    SecUnitEvidenceError,
    parse_statement_rows,
)
from .financials import SecCompanyFinancialVintage, SecFinancialsRequest, SecStatementRow
from .http import SecHttpClient, validate_sec_url
from .unit_evidence import SEC_LIVE_FINANCIAL_PARSER_V2, SecFinancialUnitEvidence

logger = logging.getLogger(__name__)
_EASTERN_TZ = ZoneInfo("America/New_York")
_LIVE_PARSER_V1 = "sec-live-financial-parser-v1-edgartools-5.56.0"
_SAFE_DOCUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_RAW_INSTANCE_MAX_BYTES = 2 * 1024 * 1024


def _filing_archive_prefix(filing: Any) -> str:
    """Return the one SEC archive directory that may supply this filing's instance."""
    try:
        cik = str(int(str(filing.cik)))
        accession = str(filing.accession_number)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SecUnitEvidenceError("filing identity is unavailable for raw XBRL evidence") from exc
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise SecUnitEvidenceError("filing accession is invalid for raw XBRL evidence")
    return f"https://www.sec.gov:443/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"


def _attachment_document(item: Any) -> str:
    document = getattr(item, "document", None)
    if not isinstance(document, str) or not _SAFE_DOCUMENT.fullmatch(document):
        raise SecUnitEvidenceError("classified XBRL instance has an unsafe document name")
    return document


def _attachment_sources(container: Any) -> list[Any]:
    """Return the pinned classifier's source set without inspecting names or content."""
    if container is None:
        return []
    data_files = getattr(container, "data_files", None)
    if data_files is None:
        data_files = getattr(container, "datafiles", None)
    if data_files is None:
        return []
    if not isinstance(data_files, list):
        raise SecUnitEvidenceError("filing XBRL attachment source list is invalid")
    return data_files


def _classifier_eligible(attachment: Any) -> bool:
    """Mirror the pinned classifier's pre-content filter exactly."""
    document_type = getattr(attachment, "document_type", None)
    extension = getattr(attachment, "extension", None)
    return (
        document_type in {"XML", "EX-101.INS"}
        and isinstance(extension, str)
        and extension.endswith((".xml", ".XML"))
    )


def _materialize_attachment(
    filing: Any, attachment: Any, client: SecHttpClient | None
) -> tuple[str, bytes, str]:
    document = _attachment_document(attachment)
    supplied_url = getattr(attachment, "url", None)
    if supplied_url is not None and not isinstance(supplied_url, str):
        raise SecUnitEvidenceError("classified XBRL instance URL is invalid")
    url, validator = _instance_url(filing, document, supplied_url)
    sgml = getattr(attachment, "sgml_document", None)
    content = getattr(sgml, "content", None) if sgml is not None else None
    if isinstance(content, str):
        content = content.encode("utf-8")
    if content is not None and type(content) is not bytes:
        raise SecUnitEvidenceError("classified XBRL instance content is not bytes")
    if content is None:
        if client is None:
            raise SecUnitEvidenceError("v2 raw XBRL evidence requires an injected SecHttpClient")
        content = _read_instance(client, url, validator)
    if len(content) > _RAW_INSTANCE_MAX_BYTES:
        raise SecUnitEvidenceError("raw XBRL instance exceeds byte limit")
    return document, content, url


def _classify_instance(document: str, attachment: Any, raw: bytes) -> bool:
    """Run edgartools' exact classifier over an in-memory one-document proxy."""
    try:
        from edgar.xbrl.xbrl import XBRLAttachments
    except ImportError as exc:
        raise SecUnitEvidenceError("pinned edgartools XBRL classifier is unavailable") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    proxy = type(
        "_BoundedAttachment",
        (),
        {
            "document": document,
            "document_type": getattr(attachment, "document_type", None),
            "extension": getattr(attachment, "extension", Path(document).suffix),
            "content": content,
        },
    )()
    attachments: Any = type("_OneAttachment", (), {"data_files": [proxy]})()
    return XBRLAttachments(attachments).get("instance") is not None


def _instance_attachment(filing: Any, client: SecHttpClient | None) -> tuple[str, bytes, str]:
    """Classify every original candidate separately, then one same-filing homepage set."""
    original = _attachment_sources(getattr(filing, "attachments", None))
    if not original:
        original = _attachment_sources(getattr(filing, "xbrl_attachments", None))
    sources = original
    homepage_used = False
    while True:
        found: dict[tuple[str, str], bytes] = {}
        for attachment in sources:
            if not _classifier_eligible(attachment):
                continue
            document, raw, url = _materialize_attachment(filing, attachment, client)
            if _classify_instance(document, attachment, raw):
                key = (document, url)
                existing = found.get(key)
                if existing is not None and existing != raw:
                    raise SecUnitEvidenceError(
                        "classified XBRL instance source has conflicting duplicate bytes"
                    )
                found[key] = raw
        if found or homepage_used:
            break
        homepage_used = True
        homepage = getattr(filing, "homepage", None)
        sources = _attachment_sources(getattr(homepage, "attachments", homepage))
    if len(found) != 1:
        raise SecUnitEvidenceError("filing must have exactly one classified original XBRL instance")
    (document, url), raw = next(iter(found.items()))
    return document, raw, url


def _instance_url(
    filing: Any, document: str, supplied_url: str | None = None
) -> tuple[str, Callable[[str], str]]:
    prefix = _filing_archive_prefix(filing)
    url = prefix + document

    def _same_directory(candidate: str) -> str:
        normalized = validate_sec_url(candidate)
        if not normalized.startswith(prefix):
            raise SecUnitEvidenceError("XBRL instance URL does not match filing archive directory")
        tail = normalized.removeprefix(prefix)
        if not _SAFE_DOCUMENT.fullmatch(tail):
            raise SecUnitEvidenceError("XBRL instance URL is not a safe filing attachment")
        return normalized

    selected = _same_directory(url if supplied_url is None else supplied_url)
    if not selected.endswith("/" + document):
        raise SecUnitEvidenceError("XBRL instance URL does not match classified attachment")
    return selected, _same_directory


def _read_instance(client: SecHttpClient, url: str, validator: Callable[[str], str]) -> bytes:
    response = client.open(
        url,
        accept="application/xml, text/xml, application/octet-stream",
        max_bytes=2 * 1024 * 1024,
        redirect_validator=validator,
    )
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.body.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 2 * 1024 * 1024:
                raise SecUnitEvidenceError("raw XBRL instance exceeds byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.body.close()


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
        http_client: SecHttpClient | None = None,
    ) -> None:
        self.user_agent = validate_user_agent(user_agent)
        self.runner = runner
        self.http_client = http_client

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
            if request.parser_version == SEC_LIVE_FINANCIAL_PARSER_V2 and any(
                item.unit_evidence is None
                or item.unit_evidence.parser_version != SEC_LIVE_FINANCIAL_PARSER_V2
                for item in res
            ):
                raise SecUnitEvidenceError(
                    "custom v2 runner returned a vintage without unit evidence"
                )
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

                raw_instance: bytes | None = None
                evidence: SecFinancialUnitEvidence | None = None
                prepared_evidence: Any | None = None
                if request.parser_version == SEC_LIVE_FINANCIAL_PARSER_V2:
                    document, raw_instance, _ = _instance_attachment(filing, self.http_client)
                    from ._live_unit_corroboration import (
                        SecUnitEvidenceError as _RawUnitEvidenceError,
                    )
                    from ._live_unit_corroboration import prepare_raw_instance

                    try:
                        # Validate the complete unit table before native extraction, once per filing.
                        prepared_evidence = prepare_raw_instance(
                            raw_instance, expected_cik=str(int(str(filing.cik)))
                        )
                    except _RawUnitEvidenceError as exc:
                        raise SecUnitEvidenceError(str(exc)) from exc
                    evidence = SecFinancialUnitEvidence(
                        parser_version=request.parser_version,
                        cik=str(int(str(filing.cik))),
                        accession_number=str(filing.accession_number),
                        instance_document=document,
                        instance_sha256=hashlib.sha256(raw_instance).hexdigest(),
                    )

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
                                    parser_version=request.parser_version,
                                    raw_instance=raw_instance,
                                    _prepared_evidence=prepared_evidence,
                                )
                                rows.extend(statement_rows)
                                if not statement_rows:
                                    quality_flags.append(f"{flag}_EMPTY")
                        except SecUnitEvidenceError:
                            raise
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
                    unit_evidence=evidence,
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
