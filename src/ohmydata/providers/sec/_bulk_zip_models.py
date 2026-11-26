"""Multi-taxonomy fundamental concept maps and SEC bulk ZIP data models."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any

_SUPPORTED_NAMESPACES = ("us-gaap", "ifrs-full", "dei")
_CURRENCY_UNIT = re.compile(r"^(?:iso4217:)?([A-Z]{3})$")
_PER_SHARE_UNIT = re.compile(r"^(?:iso4217:)?([A-Za-z]{3})/shares$", re.IGNORECASE)


@dataclass(frozen=True)
class SecBulkArchiveProvenance:
    filename: str
    sha256: str
    bytes: int


SEC_MULTI_TAXONOMY_CONCEPTS_VERSION = "sec-multi-taxonomy-fundamentals-v1"
SEC_CANONICAL_IFRS_REVENUE_PRIORITY = ("Revenue",)
SEC_CANONICAL_IFRS_CONCEPTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "REVENUE": (
            "Revenue",
            "RevenueFromContractsWithCustomers",
            "RevenueFromSaleOfGoods",
        ),
        "GROSS_PROFIT": ("GrossProfit",),
        "OPERATING_INCOME": ("ProfitLossFromOperatingActivities",),
        "GAAP_DILUTED_EPS": ("DilutedEarningsLossPerShare",),
    }
)
SEC_FUNDAMENTAL_CONCEPTS: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "revenue": MappingProxyType(
            {
                "us-gaap": (
                    "Revenues",
                    "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "RevenueFromContractWithCustomerIncludingAssessedTax",
                    "SalesRevenueNet",
                ),
                "ifrs-full": (
                    "Revenue",
                    "RevenueFromContractsWithCustomers",
                    "RevenueFromSaleOfGoods",
                ),
            }
        ),
        "gross_profit": MappingProxyType(
            {"us-gaap": ("GrossProfit",), "ifrs-full": ("GrossProfit",)}
        ),
        "operating_income": MappingProxyType(
            {
                "us-gaap": ("OperatingIncomeLoss",),
                "ifrs-full": ("ProfitLossFromOperatingActivities",),
            }
        ),
        "net_income": MappingProxyType(
            {
                "us-gaap": (
                    "NetIncomeLoss",
                    "ProfitLoss",
                    "NetIncomeLossAvailableToCommonStockholdersBasic",
                ),
                "ifrs-full": ("ProfitLoss", "ProfitLossAttributableToOwnersOfParent"),
            }
        ),
        "ocf": MappingProxyType(
            {
                "us-gaap": (
                    "NetCashProvidedByUsedInOperatingActivities",
                    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                ),
                "ifrs-full": ("CashFlowsFromUsedInOperatingActivities",),
            }
        ),
        "capex": MappingProxyType(
            {
                "us-gaap": (
                    "PaymentsToAcquirePropertyPlantAndEquipment",
                    "PaymentsToAcquireProductiveAssets",
                ),
                "ifrs-full": (
                    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                    "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsClassifiedAsInvestingActivities",
                ),
            }
        ),
        "total_assets": MappingProxyType({"us-gaap": ("Assets",), "ifrs-full": ("Assets",)}),
        "total_debt": MappingProxyType(
            {
                "us-gaap": (
                    "DebtAndCapitalLeaseObligations",
                    "LongTermDebtAndCapitalLeaseObligations",
                    "LongTermDebt",
                    "LongTermDebtNoncurrent",
                    "DebtCurrent",
                    "ShortTermBorrowings",
                    "LongTermDebtCurrent",
                ),
                "ifrs-full": (
                    "Borrowings",
                    "NoncurrentPortionOfNoncurrentBorrowings",
                    "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
                ),
            }
        ),
        "diluted_shares": MappingProxyType(
            {
                "us-gaap": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
                "ifrs-full": ("AdjustedWeightedAverageShares", "WeightedAverageShares"),
            }
        ),
        "eps_diluted": MappingProxyType(
            {
                "us-gaap": ("EarningsPerShareDiluted",),
                "ifrs-full": ("DilutedEarningsLossPerShare",),
            }
        ),
        "dei_common_shares_outstanding": MappingProxyType(
            {"dei": ("EntityCommonStockSharesOutstanding",)}
        ),
    }
)


class SecFundamentalMetric(str, Enum):
    REVENUE = "revenue"
    GROSS_PROFIT = "gross_profit"
    OPERATING_INCOME = "operating_income"
    NET_INCOME = "net_income"
    OCF = "ocf"
    CAPEX = "capex"
    TOTAL_ASSETS = "total_assets"
    TOTAL_DEBT = "total_debt"
    DILUTED_SHARES = "diluted_shares"
    EPS_DILUTED = "eps_diluted"
    DEI_COMMON_SHARES_OUTSTANDING = "dei_common_shares_outstanding"


class SecBulkCikOutcomeStatus(str, Enum):
    OK = "OK"
    EMPTY = "EMPTY"
    MISSING_SUBMISSIONS = "MISSING_SUBMISSIONS"
    PARSE_FAILED = "PARSE_FAILED"


@dataclass(frozen=True)
class SecBulkCikOutcome:
    cik: str
    status: SecBulkCikOutcomeStatus
    entity_name: str | None = None
    symbols: tuple[str, ...] = ()
    taxonomy: str | None = None
    reporting_currency: str | None = None
    filing_count: int = 0
    history_pages_read: int = 0
    fact_count: int = 0
    fundamental_row_count: int = 0
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class SecBulkJoinedFact:
    cik: str
    entity_name: str | None
    symbols: tuple[str, ...]
    accession_number: str
    form: str
    is_amendment: bool
    filing_date: date
    report_date: date
    accepted_at: datetime
    namespace: str
    tag: str
    unit: str
    currency: str | None
    value: Decimal
    period_start: date | None
    period_end: date
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None
    taxonomy: str | None


@dataclass(frozen=True)
class SecBulkFundamentalRow:
    cik: str
    entity_name: str | None
    symbols: tuple[str, ...]
    accession_number: str
    form: str
    is_amendment: bool
    filing_date: date
    report_date: date
    accepted_at: datetime
    fiscal_year: int
    fiscal_period: str
    period_kind: str
    period_start: date | None
    period_end: date
    taxonomy: str
    reporting_currency: str | None
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    ocf: Decimal | None = None
    capex: Decimal | None = None
    total_assets: Decimal | None = None
    total_debt: Decimal | None = None
    diluted_shares: Decimal | None = None
    eps_diluted: Decimal | None = None
    dei_shares_outstanding: Decimal | None = None
    dei_shares_outstanding_end: date | None = None
    source_tags_json: str = "{}"


@dataclass(frozen=True)
class SecBulkProjectionResult:
    companyfacts_archive: SecBulkArchiveProvenance
    submissions_archive: SecBulkArchiveProvenance
    outcomes: tuple[SecBulkCikOutcome, ...]
    fundamental_rows: tuple[SecBulkFundamentalRow, ...]
    joined_facts: tuple[SecBulkJoinedFact, ...]
    filters: dict[str, Any]


_INSTANT_FUNDAMENTAL_METRICS = frozenset(
    {"total_assets", "total_debt", "dei_common_shares_outstanding"}
)
_INSTANT_TAGS_BY_NS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        ns: frozenset(
            tag
            for metric in _INSTANT_FUNDAMENTAL_METRICS
            for tag in SEC_FUNDAMENTAL_CONCEPTS[metric].get(ns, ())
        )
        for ns in _SUPPORTED_NAMESPACES
    }
)
_PARSED_TAGS_BY_NS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        ns: tuple(
            sorted(
                {tag for ns_map in SEC_FUNDAMENTAL_CONCEPTS.values() for tag in ns_map.get(ns, ())}
            )
        )
        for ns in _SUPPORTED_NAMESPACES
    }
)


def _currency_from_unit(unit: str) -> str | None:
    match = _CURRENCY_UNIT.fullmatch(unit)
    if match is not None:
        return match.group(1)
    per_share = _PER_SHARE_UNIT.fullmatch(unit)
    if per_share is not None:
        return per_share.group(1).upper()
    return None


def _is_valid_canonical_unit(metric_name: str, unit: str) -> bool:
    if metric_name == "GAAP_DILUTED_EPS":
        return _PER_SHARE_UNIT.fullmatch(unit) is not None
    return _CURRENCY_UNIT.fullmatch(unit) is not None


def _is_valid_fundamental_unit(metric_name: str, unit: str) -> bool:
    if metric_name == "eps_diluted":
        return _PER_SHARE_UNIT.fullmatch(unit) is not None
    if metric_name in {"diluted_shares", "dei_common_shares_outstanding"}:
        return unit.lower() == "shares"
    return _CURRENCY_UNIT.fullmatch(unit) is not None


@dataclass(frozen=True)
class SecPrimaryCurrencySelection:
    selected: str | None
    alternate_currencies: tuple[str, ...]
    basis: str

    def to_provenance_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "alternate_currencies": list(self.alternate_currencies),
            "basis": self.basis,
        }


_SHARE_COUNT_METRICS = frozenset({"diluted_shares", "dei_common_shares_outstanding"})
_FPI_CONVENIENCE_FORMS = frozenset({"20-F", "40-F"})


def _score_currencies(
    facts: Sequence[Any],
    currencies: Sequence[str],
) -> dict[str, tuple[int, int, int]]:
    allowed = set(currencies)
    mon_ends: dict[str, set[date]] = {c: set() for c in currencies}
    mon_keys: dict[str, set[tuple[Any, ...]]] = {c: set() for c in currencies}
    all_keys: dict[str, set[tuple[Any, ...]]] = {c: set() for c in currencies}
    for fact in facts:
        if fact.namespace == "dei":
            continue
        ccy = _currency_from_unit(fact.unit)
        if ccy not in allowed:
            continue
        key = (fact.namespace, fact.tag, fact.unit, fact.start, fact.end)
        all_keys[ccy].add(key)
        if _CURRENCY_UNIT.fullmatch(fact.unit) is not None:
            mon_ends[ccy].add(fact.end)
            mon_keys[ccy].add(key)
    return {c: (len(mon_ends[c]), len(mon_keys[c]), len(all_keys[c])) for c in currencies}


def select_primary_currency(
    facts: Sequence[Any],
    *,
    form: str | None = None,
    filing_facts: Sequence[Any] | None = None,
    issuer_facts: Sequence[Any] | None = None,
) -> SecPrimaryCurrencySelection:
    """Deterministically resolve one primary reporting currency without implicit conversion."""
    scope_currencies = sorted(
        {
            ccy
            for fact in facts
            if fact.namespace != "dei"
            for ccy in (_currency_from_unit(fact.unit),)
            if ccy is not None
        }
    )
    if not scope_currencies:
        return SecPrimaryCurrencySelection(None, (), "no_currency_facts")
    if len(scope_currencies) == 1:
        return SecPrimaryCurrencySelection(scope_currencies[0], (), "single_currency")

    filing_scores = _score_currencies(
        filing_facts if filing_facts is not None else facts,
        scope_currencies,
    )
    max_filing = max(filing_scores.values())
    leaders = [c for c in scope_currencies if filing_scores[c] == max_filing]
    if len(leaders) == 1:
        winner = leaders[0]
        runner_up = max(score for c, score in filing_scores.items() if c != winner)
        if filing_scores[winner][0] > runner_up[0]:
            basis = "filing_comparative_periods"
        elif filing_scores[winner][1] > runner_up[1]:
            basis = "filing_monetary_fact_count"
        else:
            basis = "filing_currency_fact_count"
        alts = tuple(c for c in scope_currencies if c != winner)
        return SecPrimaryCurrencySelection(winner, alts, basis)

    if issuer_facts:
        issuer_scores = _score_currencies(issuer_facts, leaders)
        max_issuer = max(issuer_scores.values())
        issuer_leaders = [c for c in leaders if issuer_scores[c] == max_issuer]
        if len(issuer_leaders) == 1:
            winner = issuer_leaders[0]
            alts = tuple(c for c in scope_currencies if c != winner)
            return SecPrimaryCurrencySelection(winner, alts, "issuer_pit_evidence")
        leaders = issuer_leaders

    base_form = form.strip().upper().removesuffix("/A") if form else ""
    if base_form in _FPI_CONVENIENCE_FORMS and len(leaders) == 2 and "USD" in leaders:
        winner = next(c for c in leaders if c != "USD")
        alts = tuple(c for c in scope_currencies if c != winner)
        return SecPrimaryCurrencySelection(winner, alts, "fpi_convenience_translation_tiebreak")

    return SecPrimaryCurrencySelection(None, tuple(scope_currencies), "ambiguous_tie")


def select_metric_fact(
    metric: str,
    candidates: list[Any],
    *,
    currency: str | None = None,
) -> Any | None:
    """Select one unambiguous fact by versioned concept precedence without guessing."""
    target_currency = (
        currency.upper() if currency is not None and metric not in _SHARE_COUNT_METRICS else None
    )
    eligible = [
        fact
        for fact in candidates
        if fact.tag in SEC_FUNDAMENTAL_CONCEPTS[metric].get(fact.namespace, ())
        and _is_valid_fundamental_unit(metric, fact.unit)
        and (target_currency is None or _currency_from_unit(fact.unit) == target_currency)
    ]
    if not eligible:
        return None
    for namespace in _SUPPORTED_NAMESPACES:
        for tag in SEC_FUNDAMENTAL_CONCEPTS[metric].get(namespace, ()):
            matched = [f for f in eligible if f.namespace == namespace and f.tag == tag]
            if not matched:
                continue
            unique_vals = {(f.value, f.unit, f.start, f.end) for f in matched}
            if len(unique_vals) == 1:
                return matched[0]
            return None
    return None


def select_dei_shares(
    facts: list[Any],
) -> tuple[Decimal | None, date | None, dict[str, str] | None]:
    eligible = [
        f
        for f in facts
        if f.namespace == "dei"
        and f.tag == "EntityCommonStockSharesOutstanding"
        and f.unit.lower() == "shares"
    ]
    if not eligible:
        return None, None, None
    latest_end = max(f.end for f in eligible)
    at_latest = [f for f in eligible if f.end == latest_end]
    if len({f.value for f in at_latest}) != 1:
        return None, None, None
    chosen = at_latest[0]
    return (
        chosen.value,
        chosen.end,
        {
            "namespace": chosen.namespace,
            "tag": chosen.tag,
            "unit": chosen.unit,
            "value": str(chosen.value),
            "end": chosen.end.isoformat(),
        },
    )


def summarize_currency(facts: list[Any]) -> str | None:
    currencies = {
        ccy
        for fact in facts
        if fact.namespace != "dei"
        for ccy in (_currency_from_unit(fact.unit),)
        if ccy is not None
    }
    if not currencies:
        return None
    return next(iter(currencies)) if len(currencies) == 1 else "MULTI"


def summarize_taxonomy(facts: list[Any]) -> str:
    namespaces = {fact.namespace for fact in facts if fact.namespace != "dei"}
    if not namespaces:
        return "unknown"
    return next(iter(namespaces)) if len(namespaces) == 1 else "mixed"


__all__ = [
    "SEC_CANONICAL_IFRS_CONCEPTS",
    "SEC_CANONICAL_IFRS_REVENUE_PRIORITY",
    "SEC_FUNDAMENTAL_CONCEPTS",
    "SEC_MULTI_TAXONOMY_CONCEPTS_VERSION",
    "_CURRENCY_UNIT",
    "_INSTANT_TAGS_BY_NS",
    "_PARSED_TAGS_BY_NS",
    "_PER_SHARE_UNIT",
    "_SUPPORTED_NAMESPACES",
    "SecBulkArchiveProvenance",
    "SecBulkCikOutcome",
    "SecBulkCikOutcomeStatus",
    "SecBulkFundamentalRow",
    "SecBulkJoinedFact",
    "SecBulkProjectionResult",
    "SecFundamentalMetric",
    "SecPrimaryCurrencySelection",
    "_currency_from_unit",
    "_is_valid_canonical_unit",
    "_is_valid_fundamental_unit",
    "select_dei_shares",
    "select_metric_fact",
    "select_primary_currency",
    "summarize_currency",
    "summarize_taxonomy",
]
