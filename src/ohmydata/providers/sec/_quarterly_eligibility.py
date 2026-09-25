"""SEC ticker and submissions evidence for quarterly eligibility."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from ...core import SnapshotStore
from .errors import TransientProviderError
from .submissions import fetch_sec_submissions_root
from .ticker_cik import (
    SecTickerCikAmbiguousError,
    SecTickerCikMapping,
    SecTickerCikNotFoundError,
    fetch_sec_ticker_cik_mapping,
)

_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_IDENTITY = re.compile(r"^[0-9a-f]{64}$")


class SecCompanyEligibilityStatus(str, Enum):
    SEC_COMPANY = "SEC_COMPANY"
    NON_SEC = "NON_SEC"
    NON_COMPANY = "NON_COMPANY"
    UNKNOWN = "UNKNOWN"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"


@dataclass(frozen=True)
class SecQuarterlyEligibility:
    status: SecCompanyEligibilityStatus
    ticker: str
    cik: str | None
    entity_type: str | None
    ticker_mapping_observation_id: str | None
    submissions_observation_id: str | None
    filing_accessions: tuple[str, ...]
    reason: str
    limitations: tuple[str, ...] = (
        "SEC entityType does not prove ETF status or the absence of an ETF security.",
        "The official ticker mapping may omit fund and series-level tickers.",
    )


def _classify_sec_company_eligibility_fields(
    ticker: str,
    mapping: SecTickerCikMapping,
    *,
    entity_type: str | None,
    submissions_cik: str | None,
    submissions_observation_id: str | None,
    filing_accessions: tuple[str, ...],
    filing_forms: tuple[str, ...],
) -> SecQuarterlyEligibility:
    """Fail-closed SEC company classification using SEC ticker and submissions evidence.

    NON_SEC means only that this ticker is absent from the supplied official mapping
    observation. Transport/source exceptions are expected to propagate to the caller.
    """
    if type(mapping) is not SecTickerCikMapping:
        raise TypeError("mapping must be SecTickerCikMapping")
    canonical = ticker.strip().upper() if type(ticker) is str else ""
    if not canonical:
        raise ValueError("ticker must be non-empty")
    mapping_id = mapping.observation.observation_identity
    try:
        resolution = mapping.resolve(canonical)
    except SecTickerCikNotFoundError:
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.NON_SEC,
            canonical,
            None,
            entity_type,
            mapping_id,
            submissions_observation_id,
            (),
            "ticker is not listed in this SEC ticker mapping observation",
        )
    except SecTickerCikAmbiguousError:
        # Ambiguous SEC ticker identity is explicitly unknown, never a company guess.
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.UNKNOWN,
            canonical,
            None,
            entity_type,
            mapping_id,
            submissions_observation_id,
            (),
            "SEC ticker mapping has conflicting CIK identities",
        )
    normalized_cik = f"{int(resolution.cik):010d}"
    normalized_entity = entity_type.strip().lower() if isinstance(entity_type, str) else None
    identity_matches = (
        isinstance(submissions_cik, str)
        and submissions_cik.isascii()
        and submissions_cik.isdecimal()
        and submissions_cik.zfill(10) == normalized_cik
    )
    filings_ok = (
        len(filing_accessions) == len(filing_forms)
        and bool(filing_accessions)
        and all(_ACCESSION.fullmatch(item) for item in filing_accessions)
        and any(form in {"10-K", "10-K/A", "10-Q", "10-Q/A"} for form in filing_forms)
    )
    if not identity_matches:
        status, reason = (
            SecCompanyEligibilityStatus.UNKNOWN,
            "SEC submissions CIK does not match the official ticker mapping",
        )
    elif normalized_entity == "investment":
        status, reason = SecCompanyEligibilityStatus.NON_COMPANY, "SEC entityType is investment"
    elif (
        normalized_entity == "operating"
        and filings_ok
        and submissions_observation_id is not None
        and _IDENTITY.fullmatch(submissions_observation_id)
    ):
        status, reason = (
            SecCompanyEligibilityStatus.SEC_COMPANY,
            "operating entity, ticker/CIK identity, and SEC 10-K/Q filing evidence match",
        )
    else:
        status, reason = (
            SecCompanyEligibilityStatus.UNKNOWN,
            "SEC evidence is missing, unsupported, or inconsistent",
        )
    return SecQuarterlyEligibility(
        status,
        canonical,
        normalized_cik,
        entity_type,
        mapping_id,
        submissions_observation_id,
        filing_accessions if filings_ok else (),
        reason,
    )


def classify_sec_company_eligibility_from_root(
    ticker: str,
    mapping: SecTickerCikMapping,
    store: SnapshotStore,
    root_source: object,
) -> SecQuarterlyEligibility:
    """Replay SEC root evidence and classify without consumer parsing of its fields."""
    from ._event_discovery_models import SecDiscoverySource
    from .event_discovery import _DiscoveryReplayCache, _submission_rows, _validate_source

    if type(root_source) is not SecDiscoverySource or not isinstance(store, SnapshotStore):
        raise TypeError("invalid SEC eligibility replay inputs")
    canonical = ticker.strip().upper() if type(ticker) is str else ""
    if not canonical or type(mapping) is not SecTickerCikMapping:
        raise ValueError("invalid ticker mapping input")
    mapping_id = mapping.observation.observation_identity
    try:
        resolution = mapping.resolve(canonical)
    except SecTickerCikNotFoundError:
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.NON_SEC,
            canonical,
            None,
            None,
            mapping_id,
            None,
            (),
            "ticker is not listed in this SEC ticker mapping observation",
        )
    except SecTickerCikAmbiguousError:
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.UNKNOWN,
            canonical,
            None,
            None,
            mapping_id,
            None,
            (),
            "SEC ticker mapping has conflicting CIK identities",
        )
    cik = f"{int(resolution.cik):010d}"
    root, _ = _validate_source(
        store,
        root_source,
        cik,
        root=True,
        _replay_cache=_DiscoveryReplayCache(store),
    )
    submissions_id = root_source.observation.observation_identity
    raw_entity_type = root.get("entityType")
    entity_type = raw_entity_type if isinstance(raw_entity_type, str) else None
    rows = _submission_rows(root, cik, child=False)
    accessions: tuple[str, ...] = tuple(
        str(item["accessionNumber"])
        for item in rows
        if isinstance(item.get("accessionNumber"), str)
    )
    forms: tuple[str, ...] = tuple(
        str(item["form"]) for item in rows if isinstance(item.get("form"), str)
    )
    if entity_type is None or len(accessions) != len(forms):
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.UNKNOWN,
            canonical,
            cik,
            entity_type,
            mapping_id,
            submissions_id,
            accessions,
            "SEC submissions root lacks valid company classification or filing metadata",
        )
    return _classify_sec_company_eligibility_fields(
        canonical,
        mapping,
        entity_type=entity_type,
        submissions_cik=cik,
        submissions_observation_id=submissions_id,
        filing_accessions=accessions,
        filing_forms=forms,
    )


def fetch_sec_company_eligibility(
    ticker: str,
    client: object,
    store: SnapshotStore,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SecQuarterlyEligibility:
    """Fetch retained SEC ticker/root evidence and return explicit availability status."""
    from .http import SecHttpClient

    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("invalid SEC eligibility fetch dependencies")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    try:
        mapping = fetch_sec_ticker_cik_mapping(client, store, utc_now=utc_now)
        try:
            resolution = mapping.resolve(ticker)
        except SecTickerCikNotFoundError:
            canonical = ticker.strip().upper()
            return SecQuarterlyEligibility(
                SecCompanyEligibilityStatus.NON_SEC,
                canonical,
                None,
                None,
                mapping.observation.observation_identity,
                None,
                (),
                "ticker is not listed in this SEC ticker mapping observation",
            )
        except SecTickerCikAmbiguousError:
            return SecQuarterlyEligibility(
                SecCompanyEligibilityStatus.UNKNOWN,
                ticker.strip().upper(),
                None,
                None,
                mapping.observation.observation_identity,
                None,
                (),
                "SEC ticker mapping has conflicting CIK identities",
            )
        root = fetch_sec_submissions_root(store, client, resolution.cik, utc_now=utc_now)
        return classify_sec_company_eligibility_from_root(ticker, mapping, store, root)
    except TransientProviderError:
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.TRANSIENT_FAILURE,
            ticker.strip().upper(),
            None,
            None,
            None,
            None,
            (),
            "SEC eligibility source request failed transiently",
        )
