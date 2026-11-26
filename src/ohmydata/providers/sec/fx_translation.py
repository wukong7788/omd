"""Explicit, opt-in, PIT-safe FX-to-USD translation layer for SEC bulk fundamentals."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from ._bulk_zip_dataset import validate_bulk_companyfacts_dataset
from ._bulk_zip_models import SecBulkFundamentalRow, SecBulkJoinedFact
from ._fx_translation_dataset import (
    validate_usd_translated_fundamentals_dataset,
    write_usd_translated_fundamentals_dataset,
)
from ._fx_translation_engine import translate_single_row
from ._fx_translation_io import (
    extract_filing_implied_fx_rates,
    extract_implied_from_facts_parquet,
    load_bulk_fundamental_rows,
    load_fx_rate_table,
)
from ._fx_translation_models import (
    BULK_FUNDAMENTALS_USD_DATASET_SCHEMA,
    BULK_FUNDAMENTALS_USD_WRITER_PROFILE,
    DEFAULT_FX_MAX_IMPLIED_SPREAD,
    DEFAULT_FX_MAX_STALENESS_DAYS,
    FX_FILING_IMPLIED_SOURCE,
    SecFilingImpliedFxPair,
    SecFilingImpliedFxRate,
    SecFxDatePolicy,
    SecFxFlowMethod,
    SecFxPeriodAverage,
    SecFxRateObservation,
    SecFxRateTable,
    SecFxTranslationStatus,
    SecUsdTranslatedFundamentalRow,
    SecUsdTranslationResult,
)


def _validate_translation_params(
    *,
    fx_table: SecFxRateTable | None,
    use_filing_implied: bool,
    date_policy: SecFxDatePolicy,
    flow_method: SecFxFlowMethod,
    max_staleness_days: int,
    allow_spot_fallback: bool,
    max_implied_spread: Decimal,
    allow_cross_kind_implied: bool,
) -> None:
    if fx_table is not None and not isinstance(fx_table, SecFxRateTable):
        raise TypeError("fx_table must be a SecFxRateTable or None")
    if type(use_filing_implied) is not bool:
        raise TypeError("use_filing_implied must be a bool")
    if not isinstance(date_policy, SecFxDatePolicy):
        raise TypeError("date_policy must be a SecFxDatePolicy")
    if not isinstance(flow_method, SecFxFlowMethod):
        raise TypeError("flow_method must be a SecFxFlowMethod")
    if type(max_staleness_days) is not int or max_staleness_days < 0:
        raise ValueError("max_staleness_days must be a non-negative integer")
    if type(allow_spot_fallback) is not bool:
        raise TypeError("allow_spot_fallback must be a bool")
    if (
        not isinstance(max_implied_spread, Decimal)
        or not max_implied_spread.is_finite()
        or max_implied_spread < 0
    ):
        raise ValueError("max_implied_spread must be a non-negative finite Decimal")
    if type(allow_cross_kind_implied) is not bool:
        raise TypeError("allow_cross_kind_implied must be a bool")


def _translate_rows_with_implied_map(
    rows: Sequence[SecBulkFundamentalRow],
    *,
    implied_map: Mapping[tuple[str, str, str], SecFilingImpliedFxPair],
    fx_table: SecFxRateTable | None,
    use_filing_implied: bool,
    date_policy: SecFxDatePolicy,
    flow_method: SecFxFlowMethod,
    max_staleness_days: int,
    allow_spot_fallback: bool,
    max_implied_spread: Decimal,
    allow_cross_kind_implied: bool,
    extra_policy: Mapping[str, Any] | None = None,
) -> SecUsdTranslationResult:
    counts = {status.value: 0 for status in SecFxTranslationStatus}
    translated: list[SecUsdTranslatedFundamentalRow] = []
    for row in rows:
        out_row = translate_single_row(
            row,
            implied_map=implied_map,
            fx_table=fx_table,
            use_filing_implied=use_filing_implied,
            date_policy=date_policy,
            flow_method=flow_method,
            max_staleness_days=max_staleness_days,
            allow_spot_fallback=allow_spot_fallback,
            allow_cross_kind_implied=allow_cross_kind_implied,
        )
        counts[out_row.fx_status.value] += 1
        translated.append(out_row)

    policy: dict[str, Any] = {
        "use_filing_implied": use_filing_implied,
        "date_policy": date_policy.value,
        "flow_method": flow_method.value,
        "max_staleness_days": max_staleness_days,
        "allow_spot_fallback": allow_spot_fallback,
        "max_implied_spread": str(max_implied_spread),
        "allow_cross_kind_implied": allow_cross_kind_implied,
        "fx_table_observation_count": fx_table.observation_count if fx_table is not None else 0,
        "fx_table_currencies": list(fx_table.currencies) if fx_table is not None else [],
    }
    if extra_policy:
        policy.update(extra_policy)
    return SecUsdTranslationResult(rows=tuple(translated), counts=counts, policy=policy)


def translate_bulk_fundamentals_to_usd(
    rows: Sequence[SecBulkFundamentalRow],
    *,
    joined_facts: Sequence[SecBulkJoinedFact] | None = None,
    fx_table: SecFxRateTable | None = None,
    use_filing_implied: bool = True,
    date_policy: SecFxDatePolicy = SecFxDatePolicy.PERIOD_END_SPOT,
    flow_method: SecFxFlowMethod = SecFxFlowMethod.PERIOD_AVERAGE,
    max_staleness_days: int = DEFAULT_FX_MAX_STALENESS_DAYS,
    allow_spot_fallback: bool = True,
    max_implied_spread: Decimal = DEFAULT_FX_MAX_IMPLIED_SPREAD,
    allow_cross_kind_implied: bool = False,
) -> SecUsdTranslationResult:
    """Translate SEC bulk fundamental rows into USD with full PIT provenance."""
    _validate_translation_params(
        fx_table=fx_table,
        use_filing_implied=use_filing_implied,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        max_implied_spread=max_implied_spread,
        allow_cross_kind_implied=allow_cross_kind_implied,
    )
    implied_map = (
        extract_filing_implied_fx_rates(joined_facts, max_implied_spread=max_implied_spread)
        if use_filing_implied and joined_facts
        else {}
    )
    return _translate_rows_with_implied_map(
        rows,
        implied_map=implied_map,
        fx_table=fx_table,
        use_filing_implied=use_filing_implied,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        max_implied_spread=max_implied_spread,
        allow_cross_kind_implied=allow_cross_kind_implied,
    )


def translate_bulk_dataset_to_usd(
    bulk_dataset_dir: str | Path,
    *,
    fx_table: SecFxRateTable | None = None,
    use_filing_implied: bool = True,
    date_policy: SecFxDatePolicy = SecFxDatePolicy.PERIOD_END_SPOT,
    flow_method: SecFxFlowMethod = SecFxFlowMethod.PERIOD_AVERAGE,
    max_staleness_days: int = DEFAULT_FX_MAX_STALENESS_DAYS,
    allow_spot_fallback: bool = True,
    max_implied_spread: Decimal = DEFAULT_FX_MAX_IMPLIED_SPREAD,
    allow_cross_kind_implied: bool = False,
) -> SecUsdTranslationResult:
    """Validate and translate a ``sec-bulk-companyfacts-v1`` dataset into USD offline."""
    _validate_translation_params(
        fx_table=fx_table,
        use_filing_implied=use_filing_implied,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        max_implied_spread=max_implied_spread,
        allow_cross_kind_implied=allow_cross_kind_implied,
    )
    src_dir = Path(bulk_dataset_dir)
    source_manifest = validate_bulk_companyfacts_dataset(src_dir)
    rows = load_bulk_fundamental_rows(src_dir / "fundamentals.parquet")
    non_usd_accns = {
        row.accession_number
        for row in rows
        if row.reporting_currency is not None and row.reporting_currency.strip().upper() != "USD"
    }
    implied_map = (
        extract_implied_from_facts_parquet(
            src_dir / "facts.parquet",
            non_usd_accns,
            max_implied_spread=max_implied_spread,
        )
        if use_filing_implied
        else {}
    )
    source_lineage = {
        "source_dataset_schema": source_manifest["dataset_schema"],
        "source_fundamentals_sha256": source_manifest["files"]["fundamentals.parquet"]["sha256"],
        "source_facts_sha256": source_manifest["files"]["facts.parquet"]["sha256"],
    }
    return _translate_rows_with_implied_map(
        rows,
        implied_map=implied_map,
        fx_table=fx_table,
        use_filing_implied=use_filing_implied,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        max_implied_spread=max_implied_spread,
        allow_cross_kind_implied=allow_cross_kind_implied,
        extra_policy=source_lineage,
    )


def build_usd_translated_fundamentals_dataset(
    bulk_dataset_dir: str | Path,
    output_dir: str | Path,
    *,
    fx_table: SecFxRateTable | None = None,
    use_filing_implied: bool = True,
    date_policy: SecFxDatePolicy = SecFxDatePolicy.PERIOD_END_SPOT,
    flow_method: SecFxFlowMethod = SecFxFlowMethod.PERIOD_AVERAGE,
    max_staleness_days: int = DEFAULT_FX_MAX_STALENESS_DAYS,
    allow_spot_fallback: bool = True,
    max_implied_spread: Decimal = DEFAULT_FX_MAX_IMPLIED_SPREAD,
    allow_cross_kind_implied: bool = False,
) -> dict[str, Any]:
    """One-shot CLI/SDK helper: translate bulk dataset to USD, write immutable Parquet, validate."""
    result = translate_bulk_dataset_to_usd(
        bulk_dataset_dir,
        fx_table=fx_table,
        use_filing_implied=use_filing_implied,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        max_implied_spread=max_implied_spread,
        allow_cross_kind_implied=allow_cross_kind_implied,
    )
    write_usd_translated_fundamentals_dataset(output_dir, result)
    return validate_usd_translated_fundamentals_dataset(output_dir)


__all__ = [
    "BULK_FUNDAMENTALS_USD_DATASET_SCHEMA",
    "BULK_FUNDAMENTALS_USD_WRITER_PROFILE",
    "DEFAULT_FX_MAX_IMPLIED_SPREAD",
    "DEFAULT_FX_MAX_STALENESS_DAYS",
    "FX_FILING_IMPLIED_SOURCE",
    "SecFilingImpliedFxPair",
    "SecFilingImpliedFxRate",
    "SecFxDatePolicy",
    "SecFxFlowMethod",
    "SecFxPeriodAverage",
    "SecFxRateObservation",
    "SecFxRateTable",
    "SecFxTranslationStatus",
    "SecUsdTranslatedFundamentalRow",
    "SecUsdTranslationResult",
    "build_usd_translated_fundamentals_dataset",
    "extract_filing_implied_fx_rates",
    "load_fx_rate_table",
    "translate_bulk_dataset_to_usd",
    "translate_bulk_fundamentals_to_usd",
    "validate_usd_translated_fundamentals_dataset",
    "write_usd_translated_fundamentals_dataset",
]
