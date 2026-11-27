"""Per-row PIT-safe FX resolution and USD translation engine for SEC bulk fundamentals."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from ._bulk_zip_models import SecBulkFundamentalRow
from ._fx_translation_models import (
    FX_FILING_IMPLIED_SOURCE,
    SecFilingImpliedFxPair,
    SecFilingImpliedFxRate,
    SecFxDatePolicy,
    SecFxFlowMethod,
    SecFxRateTable,
    SecFxTranslationStatus,
    SecUsdTranslatedFundamentalRow,
    _validate_utc_datetime,
)
from .fx_rates import FRANKFURTER_FX_SOURCE, fetch_frankfurter_fx_rates

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_FLOW_FIELDS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "ocf",
    "capex",
    "eps_diluted",
)
_BS_FIELDS = ("total_assets", "total_debt")


@dataclass(frozen=True)
class _KindFxResolution:
    kind: str
    origin: str  # "FILING_IMPLIED" | "EXTERNAL_FX" | "AMBIGUOUS" | "MISSING"
    rate: Decimal | None = None
    source: str | None = None
    fx_date: date | None = None
    provenance: dict[str, Any] | None = None


def _implied_provenance(
    rate_info: SecFilingImpliedFxRate, *, cross_kind: bool = False
) -> dict[str, Any]:
    return {
        "basis": "filing_implied_cross_kind" if cross_kind else "filing_implied",
        "status": rate_info.status,
        "spread": str(rate_info.spread),
        "min_rate": str(rate_info.min_rate),
        "max_rate": str(rate_info.max_rate),
        "pair_count": rate_info.pair_count,
        "tags": list(rate_info.tags),
        "cross_kind_implied": cross_kind,
    }


def _resolve_external_spot(
    *,
    kind: str,
    currency: str,
    accepted_utc: datetime,
    fx_table: SecFxRateTable,
    date_policy: SecFxDatePolicy,
    anchor_date: date,
    max_staleness_days: int,
    basis_ok: str,
    basis_missing: str,
) -> _KindFxResolution:
    spot_obs = fx_table.lookup_spot(
        currency,
        anchor_date,
        accepted_at=accepted_utc,
        max_staleness_days=max_staleness_days,
    )
    if spot_obs is not None:
        return _KindFxResolution(
            kind=kind,
            origin="EXTERNAL_FX",
            rate=spot_obs.usd_per_unit,
            source=spot_obs.source,
            fx_date=spot_obs.rate_date,
            provenance={
                "basis": basis_ok,
                "date_policy": date_policy.value,
                "anchor_date": anchor_date.isoformat(),
                "rate_date": spot_obs.rate_date.isoformat(),
                "staleness_days": (anchor_date - spot_obs.rate_date).days,
            },
        )
    return _KindFxResolution(
        kind=kind,
        origin="MISSING",
        provenance={
            "basis": basis_missing,
            "date_policy": date_policy.value,
            "anchor_date": anchor_date.isoformat(),
            "max_staleness_days": max_staleness_days,
        },
    )


def _resolve_kind_rate(
    *,
    kind: str,
    currency: str,
    row: SecBulkFundamentalRow,
    accepted_utc: datetime,
    implied_pair: SecFilingImpliedFxPair | None,
    fx_table: SecFxRateTable | None,
    date_policy: SecFxDatePolicy,
    flow_method: SecFxFlowMethod,
    max_staleness_days: int,
    allow_spot_fallback: bool,
    allow_cross_kind_implied: bool,
) -> _KindFxResolution:
    pit_end_date = min(row.period_end, accepted_utc.date())
    anchor_date = (
        pit_end_date
        if date_policy is SecFxDatePolicy.PERIOD_END_SPOT
        else min(row.filing_date, accepted_utc.date())
    )

    if implied_pair is not None:
        primary = implied_pair.flow if kind == "flow" else implied_pair.bs
        other = implied_pair.bs if kind == "flow" else implied_pair.flow
        if primary is not None:
            if primary.status == "AMBIGUOUS":
                return _KindFxResolution(kind, "AMBIGUOUS", provenance=_implied_provenance(primary))
            return _KindFxResolution(
                kind=kind,
                origin="FILING_IMPLIED",
                rate=primary.rate,
                source=FX_FILING_IMPLIED_SOURCE,
                fx_date=pit_end_date,
                provenance=_implied_provenance(primary),
            )
        if allow_cross_kind_implied and other is not None:
            if other.status == "AMBIGUOUS":
                return _KindFxResolution(
                    kind, "AMBIGUOUS", provenance=_implied_provenance(other, cross_kind=True)
                )
            suffix = "CROSS_KIND_FROM_BS" if kind == "flow" else "CROSS_KIND_FROM_FLOW"
            return _KindFxResolution(
                kind=kind,
                origin="FILING_IMPLIED",
                rate=other.rate,
                source=f"{FX_FILING_IMPLIED_SOURCE}:{suffix}",
                fx_date=pit_end_date,
                provenance=_implied_provenance(other, cross_kind=True),
            )

    if fx_table is None:
        return _KindFxResolution(
            kind=kind,
            origin="MISSING",
            provenance={"basis": "missing_no_fx_table", "anchor_date": anchor_date.isoformat()},
        )

    if kind == "flow" and flow_method is SecFxFlowMethod.PERIOD_AVERAGE:
        if row.period_start is not None and row.period_start <= row.period_end:
            avg = fx_table.lookup_period_average(
                currency,
                row.period_start,
                row.period_end,
                accepted_at=accepted_utc,
            )
            if avg is not None:
                return _KindFxResolution(
                    kind=kind,
                    origin="EXTERNAL_FX",
                    rate=avg.usd_per_unit,
                    source=avg.source,
                    fx_date=avg.last_rate_date,
                    provenance={
                        "basis": "period_average",
                        "window_start": avg.window_start.isoformat(),
                        "window_end": avg.window_end.isoformat(),
                        "first_rate_date": avg.first_rate_date.isoformat(),
                        "last_rate_date": avg.last_rate_date.isoformat(),
                        "observation_count": avg.observation_count,
                    },
                )
        if not allow_spot_fallback:
            return _KindFxResolution(
                kind=kind,
                origin="MISSING",
                provenance={
                    "basis": "period_average_missing_no_spot_fallback",
                    "period_start": row.period_start.isoformat() if row.period_start else None,
                    "period_end": row.period_end.isoformat(),
                },
            )
        return _resolve_external_spot(
            kind=kind,
            currency=currency,
            accepted_utc=accepted_utc,
            fx_table=fx_table,
            date_policy=date_policy,
            anchor_date=anchor_date,
            max_staleness_days=max_staleness_days,
            basis_ok="period_average_fallback_spot",
            basis_missing="period_average_and_spot_missing_or_stale",
        )

    return _resolve_external_spot(
        kind=kind,
        currency=currency,
        accepted_utc=accepted_utc,
        fx_table=fx_table,
        date_policy=date_policy,
        anchor_date=anchor_date,
        max_staleness_days=max_staleness_days,
        basis_ok=f"spot_{date_policy.value.lower()}",
        basis_missing="spot_missing_or_stale",
    )


def _mul_opt(value: Decimal | None, rate: Decimal | None) -> Decimal | None:
    if value is None or rate is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 34
        return value * rate


def _identity_and_shares(row: SecBulkFundamentalRow, accepted_utc: datetime) -> dict[str, Any]:
    return {
        "cik": row.cik,
        "entity_name": row.entity_name,
        "symbols": row.symbols,
        "accession_number": row.accession_number,
        "form": row.form,
        "is_amendment": row.is_amendment,
        "filing_date": row.filing_date,
        "report_date": row.report_date,
        "accepted_at": accepted_utc,
        "fiscal_year": row.fiscal_year,
        "fiscal_period": row.fiscal_period,
        "period_kind": row.period_kind,
        "period_start": row.period_start,
        "period_end": row.period_end,
        "taxonomy": row.taxonomy,
        "diluted_shares": row.diluted_shares,
        "dei_shares_outstanding": row.dei_shares_outstanding,
        "dei_shares_outstanding_end": row.dei_shares_outstanding_end,
    }


def translate_single_row(
    row: SecBulkFundamentalRow,
    *,
    implied_map: Mapping[tuple[str, str, str], SecFilingImpliedFxPair],
    fx_table: SecFxRateTable | None,
    use_filing_implied: bool,
    date_policy: SecFxDatePolicy,
    flow_method: SecFxFlowMethod,
    max_staleness_days: int,
    allow_spot_fallback: bool,
    allow_cross_kind_implied: bool,
) -> SecUsdTranslatedFundamentalRow:
    """Translate one ``SecBulkFundamentalRow`` into USD with explicit provenance."""
    accepted_utc = _validate_utc_datetime(row.accepted_at, "accepted_at")
    base = _identity_and_shares(row, accepted_utc)
    raw_ccy = row.reporting_currency
    norm_ccy = raw_ccy.strip().upper() if isinstance(raw_ccy, str) else ""
    if not norm_ccy or norm_ccy == "MULTI" or _ISO_CURRENCY_RE.fullmatch(norm_ccy) is None:
        return SecUsdTranslatedFundamentalRow(
            **base,
            original_currency=raw_ccy,
            fx_status=SecFxTranslationStatus.MISSING_CURRENCY,
            fx_provenance_json=json.dumps(
                {"reason": "missing_or_multi_currency", "original_currency": raw_ccy},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    if norm_ccy == "USD":
        return SecUsdTranslatedFundamentalRow(
            **base,
            original_currency="USD",
            fx_status=SecFxTranslationStatus.NATIVE_USD,
            fx_flow_rate_to_usd=Decimal(1),
            fx_flow_source="NATIVE_USD",
            fx_flow_date=None,
            fx_bs_rate_to_usd=Decimal(1),
            fx_bs_source="NATIVE_USD",
            fx_bs_date=None,
            fx_provenance_json='{"status":"NATIVE_USD"}',
            revenue_usd=row.revenue,
            gross_profit_usd=row.gross_profit,
            operating_income_usd=row.operating_income,
            net_income_usd=row.net_income,
            ocf_usd=row.ocf,
            capex_usd=row.capex,
            total_assets_usd=row.total_assets,
            total_debt_usd=row.total_debt,
            eps_diluted_usd=row.eps_diluted,
        )

    implied_pair = (
        implied_map.get((row.cik, row.accession_number, norm_ccy)) if use_filing_implied else None
    )
    flow_res = _resolve_kind_rate(
        kind="flow",
        currency=norm_ccy,
        row=row,
        accepted_utc=accepted_utc,
        implied_pair=implied_pair,
        fx_table=fx_table,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        allow_cross_kind_implied=allow_cross_kind_implied,
    )
    bs_res = _resolve_kind_rate(
        kind="bs",
        currency=norm_ccy,
        row=row,
        accepted_utc=accepted_utc,
        implied_pair=implied_pair,
        fx_table=fx_table,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        allow_cross_kind_implied=allow_cross_kind_implied,
    )

    has_flow = any(getattr(row, fld) is not None for fld in _FLOW_FIELDS)
    has_bs = any(getattr(row, fld) is not None for fld in _BS_FIELDS)
    needed: list[_KindFxResolution] = []
    if has_flow:
        needed.append(flow_res)
    if has_bs:
        needed.append(bs_res)
    if not needed:
        needed = [flow_res, bs_res]

    origins = {res.origin for res in needed}
    if "AMBIGUOUS" in origins:
        status = SecFxTranslationStatus.AMBIGUOUS_FX_RATE
    elif "MISSING" in origins:
        status = SecFxTranslationStatus.MISSING_FX_RATE
    elif origins == {"FILING_IMPLIED"}:
        status = SecFxTranslationStatus.TRANSLATED_FILING_IMPLIED
    else:
        status = SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX

    flow_rate = flow_res.rate if flow_res.origin in {"FILING_IMPLIED", "EXTERNAL_FX"} else None
    bs_rate = bs_res.rate if bs_res.origin in {"FILING_IMPLIED", "EXTERNAL_FX"} else None
    provenance = {
        "currency": norm_ccy,
        "has_flow_values": has_flow,
        "has_bs_values": has_bs,
        "flow": flow_res.provenance,
        "bs": bs_res.provenance,
    }
    return SecUsdTranslatedFundamentalRow(
        **base,
        original_currency=norm_ccy,
        fx_status=status,
        fx_flow_rate_to_usd=flow_rate,
        fx_flow_source=flow_res.source if flow_rate is not None else None,
        fx_flow_date=flow_res.fx_date if flow_rate is not None else None,
        fx_bs_rate_to_usd=bs_rate,
        fx_bs_source=bs_res.source if bs_rate is not None else None,
        fx_bs_date=bs_res.fx_date if bs_rate is not None else None,
        fx_provenance_json=json.dumps(provenance, sort_keys=True, separators=(",", ":")),
        revenue_usd=_mul_opt(row.revenue, flow_rate),
        gross_profit_usd=_mul_opt(row.gross_profit, flow_rate),
        operating_income_usd=_mul_opt(row.operating_income, flow_rate),
        net_income_usd=_mul_opt(row.net_income, flow_rate),
        ocf_usd=_mul_opt(row.ocf, flow_rate),
        capex_usd=_mul_opt(row.capex, flow_rate),
        total_assets_usd=_mul_opt(row.total_assets, bs_rate),
        total_debt_usd=_mul_opt(row.total_debt, bs_rate),
        eps_diluted_usd=_mul_opt(row.eps_diluted, flow_rate),
    )


def _kind_covered_by_implied(
    implied_pair: SecFilingImpliedFxPair | None, kind: str, *, allow_cross_kind_implied: bool
) -> bool:
    if implied_pair is None:
        return False
    primary = implied_pair.flow if kind == "flow" else implied_pair.bs
    if primary is not None:
        return True
    other = implied_pair.bs if kind == "flow" else implied_pair.flow
    return allow_cross_kind_implied and other is not None


def resolve_auto_fx_table(
    rows: Sequence[SecBulkFundamentalRow],
    *,
    implied_map: Mapping[tuple[str, str, str], SecFilingImpliedFxPair],
    fx_table: SecFxRateTable | None,
    use_filing_implied: bool,
    date_policy: SecFxDatePolicy,
    flow_method: SecFxFlowMethod = SecFxFlowMethod.PERIOD_AVERAGE,
    max_staleness_days: int = 10,
    allow_cross_kind_implied: bool = False,
    auto_fetch_fx: bool = True,
    fx_fetcher: Callable[..., SecFxRateTable] | None = None,
) -> tuple[SecFxRateTable | None, dict[str, Any]]:
    """Fetch official Frankfurter FX rates only when unresolved non-USD rows remain."""
    accepted_list: list[datetime] = [
        _validate_utc_datetime(r.accepted_at, "accepted_at") for r in rows
    ]
    if fx_table is not None or not auto_fetch_fx:
        return fx_table, {"fx_auto_fetched": False}

    min_date: date | None = None
    max_date: date | None = None
    for row, accepted_utc in zip(rows, accepted_list, strict=True):
        raw_ccy = row.reporting_currency
        norm_ccy = raw_ccy.strip().upper() if isinstance(raw_ccy, str) else ""
        if (
            not norm_ccy
            or norm_ccy in {"MULTI", "USD"}
            or _ISO_CURRENCY_RE.fullmatch(norm_ccy) is None
        ):
            continue
        pair = (
            implied_map.get((row.cik, row.accession_number, norm_ccy))
            if use_filing_implied
            else None
        )
        has_flow = any(getattr(row, fld) is not None for fld in _FLOW_FIELDS)
        has_bs = any(getattr(row, fld) is not None for fld in _BS_FIELDS)
        need_flow = (has_flow or not has_bs) and not _kind_covered_by_implied(
            pair, "flow", allow_cross_kind_implied=allow_cross_kind_implied
        )
        need_bs = (has_bs or not has_flow) and not _kind_covered_by_implied(
            pair, "bs", allow_cross_kind_implied=allow_cross_kind_implied
        )
        if not need_flow and not need_bs:
            continue

        accepted_day = accepted_utc.date()
        pit_end = min(row.period_end, accepted_day)
        anchor = (
            pit_end
            if date_policy is SecFxDatePolicy.PERIOD_END_SPOT
            else min(row.filing_date, accepted_day)
        )
        p_low = (
            row.period_start
            if flow_method is SecFxFlowMethod.PERIOD_AVERAGE
            and row.period_start is not None
            and row.period_start <= row.period_end
            else anchor
        )
        row_min = min(anchor - timedelta(days=max_staleness_days), p_low)
        row_max = max(anchor, pit_end)
        min_date = row_min if min_date is None else min(min_date, row_min)
        max_date = row_max if max_date is None else max(max_date, row_max)

    if min_date is None or max_date is None:
        return None, {"fx_auto_fetched": False}

    today_utc = datetime.now(UTC).date()
    fetcher = fx_fetcher if fx_fetcher is not None else fetch_frankfurter_fx_rates
    fetch_end = (
        None if (fetcher is fetch_frankfurter_fx_rates and max_date >= today_utc) else max_date
    )
    fetched = fetcher(start_date=min_date, end_date=fetch_end)
    if not isinstance(fetched, SecFxRateTable):
        raise TypeError("fx_fetcher must return a SecFxRateTable")
    auto_sources = sorted({obs.source for obs in fetched.iter_observations()})
    source_label = (
        ",".join(auto_sources)
        if auto_sources
        else (
            FRANKFURTER_FX_SOURCE if fetcher is fetch_frankfurter_fx_rates else "custom_fx_fetcher"
        )
    )
    return fetched, {
        "fx_auto_fetched": True,
        "fx_auto_fetch_source": source_label,
        "fx_auto_fetch_start_date": min_date.isoformat(),
        "fx_auto_fetch_end_date": fetch_end.isoformat() if fetch_end is not None else None,
    }


__all__ = ["resolve_auto_fx_table", "translate_single_row"]
