"""Versioned IFRS-to-standard concept normalization for SEC financial statements."""

from __future__ import annotations

from types import MappingProxyType

IFRS_STANDARD_CONCEPTS_VERSION = "sec-ifrs-standard-concepts-v1"

IFRS_STANDARD_CONCEPTS: MappingProxyType[str, str] = MappingProxyType(
    {
        # Income statement
        "Revenue": "Revenues",
        "RevenueFromContractsWithCustomers": "Revenues",
        "RevenueFromSaleOfGoods": "Revenues",
        "RevenueFromRenderingOfServices": "Revenues",
        "CostOfSales": "CostOfRevenue",
        "GrossProfit": "GrossProfit",
        "ResearchAndDevelopmentExpense": "ResearchAndDevelopmentExpense",
        "SellingGeneralAndAdministrativeExpense": "SellingGeneralAndAdministrativeExpense",
        "AdministrativeExpense": "GeneralAndAdministrativeExpense",
        "DistributionCosts": "SellingAndMarketingExpense",
        "OperatingExpense": "OperatingExpenses",
        "ProfitLossFromOperatingActivities": "OperatingIncomeLoss",
        "FinanceIncome": "InterestIncome",
        "FinanceCosts": "InterestExpense",
        "ProfitLossBeforeTax": "IncomeBeforeTax",
        "IncomeTaxExpenseContinuingOperations": "IncomeTaxExpenseBenefit",
        "ProfitLoss": "NetIncomeLoss",
        "ProfitLossAttributableToOwnersOfParent": "NetIncomeLoss",
        "BasicEarningsLossPerShare": "EarningsPerShareBasic",
        "BasicEarningsLossPerShareFromContinuingOperations": "EarningsPerShareBasic",
        "DilutedEarningsLossPerShare": "EarningsPerShareDiluted",
        "DilutedEarningsLossPerShareFromContinuingOperations": "EarningsPerShareDiluted",
        "WeightedAverageShares": "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfOrdinarySharesIssued": (
            "WeightedAverageNumberOfSharesOutstandingBasic"
        ),
        "AdjustedWeightedAverageShares": "WeightedAverageNumberOfDilutedSharesOutstanding",
        # Balance sheet
        "CashAndCashEquivalents": "CashAndCashEquivalents",
        "ShorttermDepositsNotClassifiedAsCashEquivalents": "ShortTermInvestments",
        "CurrentFinancialAssets": "ShortTermInvestments",
        "TradeAndOtherCurrentReceivables": "AccountsReceivable",
        "Inventories": "Inventory",
        "CurrentAssets": "AssetsCurrent",
        "PropertyPlantAndEquipment": "PropertyPlantAndEquipmentNet",
        "Goodwill": "Goodwill",
        "IntangibleAssetsOtherThanGoodwill": "IntangibleAssetsNetExcludingGoodwill",
        "NoncurrentAssets": "AssetsNoncurrent",
        "Assets": "Assets",
        "TradeAndOtherCurrentPayables": "AccountsPayable",
        "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings": "ShortTermDebt",
        "ShorttermBorrowings": "ShortTermDebt",
        "CurrentLiabilities": "LiabilitiesCurrent",
        "LongtermBorrowings": "LongTermDebt",
        "NoncurrentPortionOfNoncurrentBorrowings": "LongTermDebt",
        "Borrowings": "TotalDebt",
        "NoncurrentLiabilities": "LiabilitiesNoncurrent",
        "Liabilities": "Liabilities",
        "IssuedCapital": "CommonStockValue",
        "RetainedEarnings": "RetainedEarningsAccumulatedDeficit",
        "Equity": "StockholdersEquity",
        "EquityAttributableToOwnersOfParent": "StockholdersEquity",
        "EquityAndLiabilities": "LiabilitiesAndStockholdersEquity",
        # Cash flow statement
        "CashFlowsFromUsedInOperatingActivities": "OperatingCashFlow",
        "CashFlowsFromUsedInInvestingActivities": "InvestingCashFlow",
        "CashFlowsFromUsedInFinancingActivities": "FinancingCashFlow",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "CapitalExpenditures",
        "AcquisitionsOfPropertyPlantAndEquipment": "CapitalExpenditures",
        "DepreciationAndAmortisationExpense": "DepreciationAndAmortization",
        "AdjustmentsForDepreciationAndAmortisationExpense": "DepreciationAndAmortization",
        "DividendsPaidClassifiedAsFinancingActivities": "DividendsPaid",
        "DividendsPaid": "DividendsPaid",
        "PurchaseOfTreasuryShares": "ShareRepurchases",
    }
)


def normalize_ifrs_standard_concept(concept: str) -> str | None:
    """Map an ``ifrs-full:*`` or ``ifrs-full_*`` concept to a clean standard concept name."""
    if concept.startswith("ifrs-full:"):
        local = concept.removeprefix("ifrs-full:")
    elif concept.startswith("ifrs-full_"):
        local = concept.removeprefix("ifrs-full_")
    else:
        return None
    if not local:
        return None
    return IFRS_STANDARD_CONCEPTS.get(local, local)


def resolve_standard_concept(explicit_standard_concept: str | None, concept: str) -> str:
    """Resolve standard_concept without altering native concept or non-IFRS behavior."""
    if explicit_standard_concept and not explicit_standard_concept.startswith(
        ("ifrs-full:", "ifrs-full_")
    ):
        return explicit_standard_concept
    normalized_ifrs = normalize_ifrs_standard_concept(concept)
    if normalized_ifrs is not None:
        return normalized_ifrs
    return explicit_standard_concept or concept


__all__ = [
    "IFRS_STANDARD_CONCEPTS",
    "IFRS_STANDARD_CONCEPTS_VERSION",
    "normalize_ifrs_standard_concept",
    "resolve_standard_concept",
]
