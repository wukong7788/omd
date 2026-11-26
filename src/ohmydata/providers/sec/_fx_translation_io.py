"""CSV/Parquet FX rate loader, bulk Parquet reader, and filing-implied FX extractor."""

from __future__ import annotations

import csv
import importlib
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from ._bulk_zip_models import _CURRENCY_UNIT, SecBulkFundamentalRow, SecBulkJoinedFact
from ._fx_translation_models import (
    DEFAULT_FX_MAX_IMPLIED_SPREAD,
    SecFilingImpliedFxPair,
    SecFilingImpliedFxRate,
    SecFxRateObservation,
    SecFxRateTable,
    _validate_currency,
    _validate_positive_decimal,
    _validate_utc_datetime,
)


def _parse_date_cell(raw: Any, column: str) -> date:
    if isinstance(raw, datetime):
        raise ValueError(f"{column} must be a calendar date, not datetime")  # noqa: TRY004
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(f"{column} cannot be empty")
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO date in {column}: {raw!r}") from exc
    raise ValueError(f"invalid date value in {column}: {raw!r}")


def _parse_decimal_cell(raw: Any, column: str) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"{column} must be a positive finite number")
    if isinstance(raw, Decimal):
        val = raw
    elif isinstance(raw, int):
        val = Decimal(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            raise ValueError(f"{column} must be finite")
        val = Decimal(str(raw))
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(f"{column} cannot be empty")
        try:
            val = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"invalid decimal value in {column}: {raw!r}") from exc
    else:
        raise ValueError(f"unsupported numeric type in {column}: {type(raw).__name__}")  # noqa: TRY004
    return _validate_positive_decimal(val, column)


def _parse_available_at_cell(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _validate_utc_datetime(raw, "available_at")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO timestamp in available_at: {raw!r}") from exc
        return _validate_utc_datetime(parsed, "available_at")
    raise ValueError(f"invalid available_at value: {raw!r}")


def _resolve_table_columns(field_names: Iterable[str]) -> tuple[str, bool, bool]:
    cols = set(field_names)
    missing = {"currency", "usd_per_unit"} - cols
    if missing:
        raise ValueError(f"FX rate table missing required columns: {sorted(missing)}")
    has_rate_date = "rate_date" in cols
    has_date = "date" in cols
    if has_rate_date and has_date:
        raise ValueError("FX rate table cannot contain both 'rate_date' and 'date' columns")
    if not has_rate_date and not has_date:
        raise ValueError("FX rate table missing required date column ('rate_date' or 'date')")
    date_col = "rate_date" if has_rate_date else "date"
    return date_col, "source" in cols, "available_at" in cols


def load_fx_rate_table(path: str | Path) -> SecFxRateTable:
    """Load an immutable PIT FX rate table from a ``.parquet`` or ``.csv`` file."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"FX rate table file not found: {file_path.name}")
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        with file_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError("FX rate CSV file is missing header row")
            date_col, has_source, has_available = _resolve_table_columns(reader.fieldnames)
            raw_rows: list[Mapping[str, Any]] = list(reader)
        default_source = "external_csv"
    elif suffix == ".parquet":
        try:
            pq: Any = importlib.import_module("pyarrow.parquet")
        except ImportError as exc:
            raise RuntimeError(
                "install an optional pyarrow extra (e.g. 'sec-cli') to load Parquet FX tables"
            ) from exc
        table = pq.read_table(file_path, partitioning=None)
        date_col, has_source, has_available = _resolve_table_columns(table.column_names)
        raw_rows = table.to_pylist()
        default_source = "external_parquet"
    else:
        raise ValueError(f"unsupported FX rate table format: {file_path.suffix!r}")

    observations: list[SecFxRateObservation] = []
    for row in raw_rows:
        raw_ccy = row.get("currency")
        if type(raw_ccy) is not str:
            raise ValueError("currency column must contain ISO 4217 strings")
        ccy = _validate_currency(raw_ccy.strip(), "currency")
        rate_dt = _parse_date_cell(row.get(date_col), date_col)
        rate_val = _parse_decimal_cell(row.get("usd_per_unit"), "usd_per_unit")
        if has_source:
            raw_src = row.get("source")
            if type(raw_src) is not str or not raw_src.strip():
                raise ValueError("source column must contain non-empty strings when present")
            source = raw_src.strip()
        else:
            source = default_source
        avail = _parse_available_at_cell(row.get("available_at")) if has_available else None
        observations.append(
            SecFxRateObservation(
                currency=ccy,
                rate_date=rate_dt,
                usd_per_unit=rate_val,
                source=source,
                available_at=avail,
            )
        )
    return SecFxRateTable.from_observations(observations)


def _summarize_implied_rates(
    pairs: Sequence[tuple[str, Decimal]],
    max_implied_spread: Decimal,
) -> SecFilingImpliedFxRate:
    ordered_rates = sorted(rate for _, rate in pairs)
    lo = ordered_rates[0]
    hi = ordered_rates[-1]
    tags = tuple(sorted({tag for tag, _ in pairs}))
    with localcontext() as ctx:
        ctx.prec = 34
        spread = (hi - lo) / lo
        if spread > max_implied_spread:
            return SecFilingImpliedFxRate(
                status="AMBIGUOUS",
                rate=None,
                spread=spread,
                tags=tags,
                pair_count=len(pairs),
                min_rate=lo,
                max_rate=hi,
            )
        mid = len(ordered_rates) // 2
        if len(ordered_rates) % 2 == 1:
            median_rate = ordered_rates[mid]
        else:
            median_rate = (ordered_rates[mid - 1] + ordered_rates[mid]) / Decimal(2)
    return SecFilingImpliedFxRate(
        status="OK",
        rate=median_rate,
        spread=spread,
        tags=tags,
        pair_count=len(pairs),
        min_rate=lo,
        max_rate=hi,
    )


def _extract_implied_fx_from_fact_tuples(
    records: Sequence[tuple[str, str, str, str, str, str | None, Decimal, date | None, date]],
    *,
    max_implied_spread: Decimal = DEFAULT_FX_MAX_IMPLIED_SPREAD,
) -> dict[tuple[str, str, str], SecFilingImpliedFxPair]:
    """Core dual-currency implied rate extractor over compact fact tuples.

    Each tuple is ``(cik, accn, namespace, tag, unit, currency, value, period_start, period_end)``.
    Only pure monetary units (not per-share units) with matching same-sign non-zero
    native and USD values are used to avoid per-share rounding and ADR ratio distortion.
    """
    if (
        not isinstance(max_implied_spread, Decimal)
        or not max_implied_spread.is_finite()
        or max_implied_spread < 0
    ):
        raise ValueError("max_implied_spread must be a non-negative finite Decimal")

    grouped: dict[
        tuple[str, str, str, str, date | None, date],
        dict[str, set[Decimal]],
    ] = {}
    for cik, accn, ns, tag, unit, currency, value, p_start, p_end in records:
        if ns == "dei" or currency is None:
            continue
        if _CURRENCY_UNIT.fullmatch(unit) is None:
            continue
        key = (cik, accn, ns, tag, p_start, p_end)
        grouped.setdefault(key, {}).setdefault(currency, set()).add(value)

    flow_candidates: dict[tuple[str, str, str], list[tuple[str, Decimal]]] = {}
    bs_candidates: dict[tuple[str, str, str], list[tuple[str, Decimal]]] = {}
    all_keys: set[tuple[str, str, str]] = set()

    with localcontext() as ctx:
        ctx.prec = 34
        for (cik, accn, ns, tag, p_start, _p_end), by_ccy in grouped.items():
            if len(by_ccy) != 2:
                continue
            usd_vals = by_ccy.get("USD")
            if usd_vals is None or len(usd_vals) != 1:
                continue
            usd_val = next(iter(usd_vals))
            if usd_val == 0:
                continue
            tag_label = f"{ns}:{tag}"
            for native_ccy, native_vals in by_ccy.items():
                if native_ccy == "USD" or len(native_vals) != 1:
                    continue
                native_val = next(iter(native_vals))
                if native_val == 0 or (usd_val > 0) != (native_val > 0):
                    continue
                rate = usd_val / native_val
                if not rate.is_finite() or rate <= 0:
                    continue
                pair_key = (cik, accn, native_ccy)
                all_keys.add(pair_key)
                if p_start is None:
                    bs_candidates.setdefault(pair_key, []).append((tag_label, rate))
                else:
                    flow_candidates.setdefault(pair_key, []).append((tag_label, rate))

    result: dict[tuple[str, str, str], SecFilingImpliedFxPair] = {}
    for pair_key in sorted(all_keys):
        cik, accn, native_ccy = pair_key
        f_list = flow_candidates.get(pair_key)
        b_list = bs_candidates.get(pair_key)
        result[pair_key] = SecFilingImpliedFxPair(
            cik=cik,
            accession_number=accn,
            native_currency=native_ccy,
            flow=_summarize_implied_rates(f_list, max_implied_spread) if f_list else None,
            bs=_summarize_implied_rates(b_list, max_implied_spread) if b_list else None,
        )
    return result


def extract_filing_implied_fx_rates(
    joined_facts: Sequence[SecBulkJoinedFact],
    *,
    max_implied_spread: Decimal = DEFAULT_FX_MAX_IMPLIED_SPREAD,
) -> dict[tuple[str, str, str], SecFilingImpliedFxPair]:
    """Extract issuer-disclosed convenience translation rates from dual-currency SEC facts."""
    tuples = [
        (
            f.cik,
            f.accession_number,
            f.namespace,
            f.tag,
            f.unit,
            f.currency,
            f.value,
            f.period_start,
            f.period_end,
        )
        for f in joined_facts
    ]
    return _extract_implied_fx_from_fact_tuples(tuples, max_implied_spread=max_implied_spread)


def _opt_dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def load_bulk_fundamental_rows(fundamentals_path: Path) -> list[SecBulkFundamentalRow]:
    """Read ``fundamentals.parquet`` from a validated bulk dataset into typed rows."""
    pq: Any = importlib.import_module("pyarrow.parquet")
    table = pq.read_table(fundamentals_path, partitioning=None)
    rows: list[SecBulkFundamentalRow] = []
    for rec in table.to_pylist():
        accepted = rec["accepted_at"]
        accepted_utc = accepted.astimezone(UTC) if isinstance(accepted, datetime) else accepted
        rows.append(
            SecBulkFundamentalRow(
                cik=rec["cik"],
                entity_name=rec["entity_name"],
                symbols=tuple(rec["symbols"]),
                accession_number=rec["accession_number"],
                form=rec["form"],
                is_amendment=rec["is_amendment"],
                filing_date=rec["filing_date"],
                report_date=rec["report_date"],
                accepted_at=accepted_utc,
                fiscal_year=rec["fiscal_year"],
                fiscal_period=rec["fiscal_period"],
                period_kind=rec["period_kind"],
                period_start=rec["period_start"],
                period_end=rec["period_end"],
                taxonomy=rec["taxonomy"],
                reporting_currency=rec["reporting_currency"],
                revenue=_opt_dec(rec["revenue"]),
                gross_profit=_opt_dec(rec["gross_profit"]),
                operating_income=_opt_dec(rec["operating_income"]),
                net_income=_opt_dec(rec["net_income"]),
                ocf=_opt_dec(rec["ocf"]),
                capex=_opt_dec(rec["capex"]),
                total_assets=_opt_dec(rec["total_assets"]),
                total_debt=_opt_dec(rec["total_debt"]),
                diluted_shares=_opt_dec(rec["diluted_shares"]),
                eps_diluted=_opt_dec(rec["eps_diluted"]),
                dei_shares_outstanding=_opt_dec(rec["dei_shares_outstanding"]),
                dei_shares_outstanding_end=rec["dei_shares_outstanding_end"],
                source_tags_json=rec["source_tags_json"],
            )
        )
    return rows


def extract_implied_from_facts_parquet(
    facts_path: Path,
    target_accns: set[str],
    *,
    max_implied_spread: Decimal,
) -> dict[tuple[str, str, str], SecFilingImpliedFxPair]:
    """Extract filing-implied FX rates efficiently from ``facts.parquet`` for non-USD accessions."""
    if not target_accns:
        return {}
    pa: Any = importlib.import_module("pyarrow")
    pc: Any = importlib.import_module("pyarrow.compute")
    pq: Any = importlib.import_module("pyarrow.parquet")
    cols = [
        "cik",
        "accession_number",
        "namespace",
        "tag",
        "unit",
        "currency",
        "value",
        "period_start",
        "period_end",
    ]
    table = pq.read_table(facts_path, columns=cols, partitioning=None)
    mask = pc.and_(
        pc.is_in(table["accession_number"], value_set=pa.array(sorted(target_accns))),
        pc.not_equal(table["namespace"], "dei"),
    )
    filtered = table.filter(mask)
    if filtered.num_rows == 0:
        return {}
    tuples = [
        (
            rec["cik"],
            rec["accession_number"],
            rec["namespace"],
            rec["tag"],
            rec["unit"],
            rec["currency"],
            Decimal(rec["value"]),
            rec["period_start"],
            rec["period_end"],
        )
        for rec in filtered.to_pylist()
    ]
    return _extract_implied_fx_from_fact_tuples(tuples, max_implied_spread=max_implied_spread)


__all__ = [
    "extract_filing_implied_fx_rates",
    "extract_implied_from_facts_parquet",
    "load_bulk_fundamental_rows",
    "load_fx_rate_table",
]
