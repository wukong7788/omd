"""Bulk projection, USD translation, and FX rate CLI handlers for ``omd sec financials``."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ._cli_financials_args import parse_ciks, parse_str_list, parse_utc_iso
from .batch import ensure_safe_output_root
from .bulk_zip import (
    DEFAULT_BULK_FORMS,
    DEFAULT_MAX_ZIP_ENTRY_BYTES,
    DEFAULT_MAX_ZIP_TOTAL_BYTES,
    build_bulk_companyfacts_dataset,
)
from .fx_rates import DEFAULT_FX_MAX_ATTEMPTS, FRANKFURTER_FX_SOURCE, write_fx_rate_table
from .fx_translation import (
    DEFAULT_FX_MAX_STALENESS_DAYS,
    SecFxDatePolicy,
    SecFxFlowMethod,
    SecFxRateTable,
    build_usd_translated_fundamentals_dataset,
    load_fx_rate_table,
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_USD_ONLY_FLAGS = (
    "fx_rates",
    "date_policy",
    "flow_method",
    "max_staleness_days",
    "no_auto_fx",
    "no_filing_implied",
    "no_spot_fallback",
)

__all__ = ["run_bulk_project", "run_fetch_fx_rates", "run_translate_usd"]


def _resolve_output_path(args: Any, command: str) -> str:
    out_arg, root_arg = getattr(args, "output", None), getattr(args, "root", None)
    if out_arg and root_arg and Path(out_arg).resolve() != Path(root_arg).resolve():
        raise ValueError("--output and --root must match when both are provided")
    output_path = out_arg or root_arg
    if not output_path:
        raise ValueError(f"--output or --root is required for {command}")
    return str(output_path)


def _parse_enum_flag(raw: Any, enum_cls: Any, default: Any, flag_name: str) -> Any:
    if raw is None:
        return default
    if isinstance(raw, enum_cls):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"{flag_name} must be a string")  # noqa: TRY004
    token = raw.strip().upper().replace("-", "_")
    try:
        return enum_cls[token]
    except KeyError as exc:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"invalid {flag_name}: {raw!r} (expected one of: {allowed})") from exc


def _parse_optional_int(args: Any, attr: str, flag_name: str, default: int) -> int:
    raw = getattr(args, attr, None)
    if type(raw) is bool or (raw is not None and type(raw) is not int):
        raise ValueError(f"{flag_name} must be an integer")
    return default if raw is None else raw


def _parse_iso_date_arg(raw: Any, flag_name: str) -> date:
    if not isinstance(raw, str) or _ISO_DATE_RE.fullmatch(raw.strip()) is None:
        raise ValueError(f"{flag_name} must be an ISO YYYY-MM-DD date")
    return date.fromisoformat(raw.strip())


def _parse_usd_options(args: Any) -> dict[str, Any]:
    fx_rates_path = getattr(args, "fx_rates", None)
    fx_table = load_fx_rate_table(fx_rates_path) if fx_rates_path else None
    return {
        "fx_table": fx_table,
        "date_policy": _parse_enum_flag(
            getattr(args, "date_policy", None),
            SecFxDatePolicy,
            SecFxDatePolicy.PERIOD_END_SPOT,
            "--date-policy",
        ),
        "flow_method": _parse_enum_flag(
            getattr(args, "flow_method", None),
            SecFxFlowMethod,
            SecFxFlowMethod.PERIOD_AVERAGE,
            "--flow-method",
        ),
        "max_staleness_days": _parse_optional_int(
            args, "max_staleness_days", "--max-staleness-days", DEFAULT_FX_MAX_STALENESS_DAYS
        ),
        "use_filing_implied": not bool(getattr(args, "no_filing_implied", False)),
        "allow_spot_fallback": not bool(getattr(args, "no_spot_fallback", False)),
        "auto_fetch_fx": not bool(getattr(args, "no_auto_fx", False)),
    }


def run_bulk_project(
    args: Any,
    payload: dict[str, Any],
    log_progress: Callable[[str, bool], None],
    *,
    fx_fetcher: Callable[..., SecFxRateTable],
) -> None:
    cf_zip = getattr(args, "companyfacts_zip", None)
    sub_zip = getattr(args, "submissions_zip", None)
    if not cf_zip:
        raise ValueError("--companyfacts-zip is required for bulk-project")
    if not sub_zip:
        raise ValueError("--submissions-zip is required for bulk-project")
    output_dir = _resolve_output_path(args, "bulk-project")
    ensure_safe_output_root(output_dir)

    usd_output = getattr(args, "usd_output", None)
    usd_opts: dict[str, Any] = {}
    if usd_output is not None:
        if not isinstance(usd_output, str) or not usd_output.strip():
            raise ValueError("--usd-output cannot be empty")
        if Path(usd_output).expanduser().resolve() == Path(output_dir).expanduser().resolve():
            raise ValueError("--usd-output must differ from --output")
        ensure_safe_output_root(usd_output)
        usd_opts = _parse_usd_options(args)
        usd_opts["fx_fetcher"] = fx_fetcher
    else:
        has_usd_flag = any(
            bool(getattr(args, key, False))
            if key.startswith("no_")
            else getattr(args, key, None) is not None
            for key in _USD_ONLY_FLAGS
        )
        if has_usd_flag:
            raise ValueError("USD translation flags require --usd-output")

    raw_ciks, universe_ciks = getattr(args, "ciks", None), getattr(args, "universe_ciks", None)
    ciks = parse_ciks(raw_ciks, universe_ciks) if (raw_ciks or universe_ciks) else None
    forms_list = parse_str_list(getattr(args, "forms", None))
    acc_raw = getattr(args, "acceptance_upper", None)
    acc_upper = parse_utc_iso(str(acc_raw), "--acceptance-upper") if acc_raw is not None else None
    max_entry = getattr(args, "max_entry_bytes", None)
    max_total = getattr(args, "max_total_bytes", None)
    workers = _parse_optional_int(args, "workers", "--workers", 1)

    quiet = getattr(args, "quiet", False)
    will_auto_fetch = (
        usd_output is not None
        and usd_opts.get("auto_fetch_fx", True)
        and usd_opts.get("fx_table") is None
    )
    log_progress(
        "projecting SEC bulk ZIP archives and translating to USD (auto-FX enabled)..."
        if will_auto_fetch
        else "projecting SEC bulk ZIP archives offline...",
        quiet,
    )
    manifest = build_bulk_companyfacts_dataset(
        cf_zip,
        sub_zip,
        output_dir,
        ciks=ciks,
        start_year=getattr(args, "start_year", None),
        end_year=getattr(args, "end_year", None),
        forms=tuple(forms_list) if forms_list else DEFAULT_BULK_FORMS,
        include_amendments=not bool(getattr(args, "no_amendments", False)),
        acceptance_upper=acc_upper,
        max_entry_bytes=DEFAULT_MAX_ZIP_ENTRY_BYTES if max_entry is None else int(max_entry),
        max_total_bytes=DEFAULT_MAX_ZIP_TOTAL_BYTES if max_total is None else int(max_total),
        workers=workers,
        usd_output_dir=usd_output,
        **usd_opts,
    )
    payload.update(manifest)
    payload.update({"root": str(output_dir), "output": str(output_dir), "status": "completed"})
    if usd_output is not None:
        payload["usd_output"] = str(usd_output)
    log_progress(
        f"bulk-project completed: total_ciks={manifest.get('counts', {}).get('total_ciks', 0)}",
        quiet,
    )


def run_translate_usd(
    args: Any,
    payload: dict[str, Any],
    log_progress: Callable[[str, bool], None],
    *,
    fx_fetcher: Callable[..., SecFxRateTable],
) -> None:
    bulk_dataset = getattr(args, "bulk_dataset", None)
    if not bulk_dataset:
        raise ValueError("--bulk-dataset is required for translate-usd")
    output_dir = _resolve_output_path(args, "translate-usd")
    ensure_safe_output_root(output_dir)
    usd_opts = _parse_usd_options(args)

    quiet = getattr(args, "quiet", False)
    log_progress("translating SEC bulk fundamentals to USD...", quiet)
    manifest = build_usd_translated_fundamentals_dataset(
        bulk_dataset,
        output_dir,
        fx_fetcher=fx_fetcher,
        **usd_opts,
    )
    payload.update(manifest)
    payload.update({"root": str(output_dir), "output": str(output_dir), "status": "completed"})
    log_progress(
        f"translate-usd completed: total_rows={manifest.get('counts', {}).get('total_rows', 0)}",
        quiet,
    )


def run_fetch_fx_rates(
    args: Any,
    payload: dict[str, Any],
    log_progress: Callable[[str, bool], None],
    *,
    fx_fetcher: Callable[..., SecFxRateTable],
) -> None:
    out_file = Path(_resolve_output_path(args, "fetch-fx-rates"))
    if out_file.suffix.lower() not in {".parquet", ".csv"}:
        raise ValueError(f"unsupported FX rate table format: {out_file.suffix!r}")
    ensure_safe_output_root(out_file)

    raw_start, raw_end = getattr(args, "start_date", None), getattr(args, "end_date", None)
    if not raw_start:
        raise ValueError("--start-date is required for fetch-fx-rates")
    start_date = _parse_iso_date_arg(raw_start, "--start-date")
    end_date = _parse_iso_date_arg(raw_end, "--end-date") if raw_end is not None else None
    if end_date is not None and end_date < start_date:
        raise ValueError("--end-date must be >= --start-date")

    raw_source = getattr(args, "source", None)
    src = "frankfurter" if raw_source is None else str(raw_source).strip().lower()
    if src not in {"frankfurter", FRANKFURTER_FX_SOURCE}:
        raise ValueError(f"unsupported FX source: {raw_source!r} (expected 'frankfurter')")

    max_attempts = _parse_optional_int(
        args, "max_attempts", "--max-attempts", DEFAULT_FX_MAX_ATTEMPTS
    )
    raw_ccy = getattr(args, "currencies", None)
    currencies = tuple(parse_str_list(raw_ccy)) if raw_ccy is not None else None
    if raw_ccy is not None and not currencies:
        raise ValueError("--currencies cannot be empty when specified")
    quiet = getattr(args, "quiet", False)
    log_progress(f"fetching Frankfurter FX rates from {start_date.isoformat()}...", quiet)
    fx_table = fx_fetcher(
        start_date=start_date, end_date=end_date, currencies=currencies, max_attempts=max_attempts
    )
    meta = write_fx_rate_table(out_file, fx_table)
    if load_fx_rate_table(out_file).observation_count != meta["observation_count"]:
        raise RuntimeError("reloaded FX rate table observation count mismatch")
    payload.update({**meta, "status": "completed", "source": FRANKFURTER_FX_SOURCE})
    log_progress(f"fetch-fx-rates completed: observations={meta['observation_count']}", quiet)
