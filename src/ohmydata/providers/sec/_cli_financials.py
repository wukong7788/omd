"""CLI subcommand execution for ``omd sec financials``."""

from __future__ import annotations

import importlib
import json
import math
import re
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ...core.snapshot import SnapshotStore
from ._cli_financials_args import (
    PARSER_VERSIONS,
    add_financials_commands,
    apply_config,
    parse_ciks,
    parse_str_list,
    parse_utc_iso,
    resolve_user_agent,
)
from .batch import ensure_safe_output_root
from .bulk_zip import (
    DEFAULT_BULK_FORMS,
    DEFAULT_MAX_ZIP_ENTRY_BYTES,
    DEFAULT_MAX_ZIP_TOTAL_BYTES,
    build_bulk_companyfacts_dataset,
)
from .financials import SecFinancialsRequest, normalize_sec_cik
from .financials_sync import (
    DEFAULT_SYNC_FORMS,
    SecRateLimiter,
    make_edgartools_filing_fetcher,
    sync_sec_financials_incremental,
)
from .fx_rates import (
    DEFAULT_FX_MAX_ATTEMPTS,
    FRANKFURTER_FX_SOURCE,
    fetch_frankfurter_fx_rates,
    write_fx_rate_table,
)
from .fx_translation import (
    DEFAULT_FX_MAX_STALENESS_DAYS,
    SecFxDatePolicy,
    SecFxFlowMethod,
    build_usd_translated_fundamentals_dataset,
    load_fx_rate_table,
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

__all__ = ["add_financials_commands", "run_financials"]


def _run_inspect(args: Any, payload: dict[str, Any]) -> None:
    from .financials_dataset import validate_cik_financials_partition, validate_financials_partition

    sym, cik = getattr(args, "symbol", None), getattr(args, "cik", None)
    if bool(sym) == bool(cik):
        raise ValueError("exactly one of --symbol or --cik is required for inspect")
    if sym:
        key = str(sym).strip().upper()
        p_dir = Path(args.root) / f"symbol={key}"
        label, validator = f"symbol: {key}", validate_financials_partition
    else:
        key = normalize_sec_cik(str(cik))
        p_dir = Path(args.root) / f"cik={key}"
        label, validator = f"cik: {key}", validate_cik_financials_partition
    if not p_dir.is_dir():
        raise ValueError(f"partition not found for {label}")
    if not (p_dir / "manifest.json").is_file():
        raise ValueError(f"manifest.json missing for {label}")
    payload.update(validator(p_dir))
    if getattr(args, "rows", False):
        pq: Any = importlib.import_module("pyarrow.parquet")
        s_file = p_dir / "financial_statements.parquet"
        if s_file.is_file():
            payload["rows"] = pq.read_table(s_file, partitioning=None).to_pylist()[:100]


def _run_validate(
    args: Any, payload: dict[str, Any], log_progress: Callable[[str, bool], None]
) -> None:
    from .financials_dataset import (
        list_financials_incremental_batches,
        validate_cik_financials_partition,
        validate_financials_incremental_batch,
        validate_financials_partition,
    )

    root_path = Path(args.root)
    req_symbols = parse_str_list(getattr(args, "symbols", None))
    req_ciks = parse_ciks(getattr(args, "ciks", None))
    verified = batches_verified = 0

    if req_symbols or req_ciks:
        for prefix, items, validator, label in (
            ("symbol=", req_symbols, validate_financials_partition, "symbol"),
            ("cik=", req_ciks, validate_cik_financials_partition, "cik"),
        ):
            for key in items:
                p_dir = root_path / f"{prefix}{key}"
                if not p_dir.is_dir():
                    raise ValueError(f"partition not found for {label}: {key}")
                validator(p_dir)
                verified += 1
    else:
        for pattern, validator in (
            ("symbol=*", validate_financials_partition),
            ("cik=*", validate_cik_financials_partition),
        ):
            for p in sorted(root_path.glob(pattern)):
                validator(p)
                verified += 1
        for batch_dir in list_financials_incremental_batches(root_path):
            validate_financials_incremental_batch(batch_dir)
            batches_verified += 1

    payload["status"] = "completed"
    payload["partitions_verified"] = verified
    if batches_verified:
        payload["batches_verified"] = batches_verified
    log_progress(
        f"verified {verified} company financials partition(s)", getattr(args, "quiet", False)
    )


def _run_sync(
    args: Any,
    payload: dict[str, Any],
    http_client_cls: Any,
    log_progress: Callable[[str, bool], None],
) -> None:
    from .edgartools_adapter import SecFinancialsClient
    from .financials_dataset import write_cik_financials_partition, write_financials_partition

    symbols_raw: Any = getattr(args, "symbols", None)
    ciks_raw: Any = getattr(args, "ciks", None)
    universe_val: Any = getattr(args, "universe", None)
    if not symbols_raw and isinstance(universe_val, dict) and "symbols" in universe_val:
        symbols_raw = universe_val["symbols"]
    elif not symbols_raw and isinstance(universe_val, list):
        symbols_raw = universe_val
    if not ciks_raw and isinstance(universe_val, dict) and "ciks" in universe_val:
        ciks_raw = universe_val["ciks"]

    symbols, ciks = parse_str_list(symbols_raw), parse_ciks(ciks_raw)
    if not symbols and not ciks:
        raise ValueError("--symbols or --ciks is required for sync")

    forms_list = parse_str_list(getattr(args, "forms", None))
    user_agent = resolve_user_agent(args)
    client = (
        SecFinancialsClient.from_config(args.config)
        if getattr(args, "config", None) and not user_agent
        else SecFinancialsClient(user_agent, http_client=http_client_cls(user_agent))
    )
    if client.http_client is None:
        client.http_client = http_client_cls(client.user_agent)

    req = SecFinancialsRequest(
        symbols=tuple(symbols),
        ciks=ciks,
        forms=tuple(forms_list) if forms_list else ("10-K", "10-Q"),
        start_year=getattr(args, "start_year", None),
        end_year=getattr(args, "end_year", None),
        availability_policy=getattr(args, "availability_policy", None) or "accepted-at-plus-lag",
        lag_days=0 if getattr(args, "lag_days", None) is None else args.lag_days,
        limit=1 if getattr(args, "latest", False) else getattr(args, "limit", None),
        parser_version=getattr(args, "parser_version", None) or PARSER_VERSIONS[1],
    )
    quiet = getattr(args, "quiet", False)
    log_progress(
        f"fetching company financials for {len(symbols)} symbol(s) and {len(ciks)} CIK(s)...",
        quiet,
    )
    vintages = client.fetch_company_financials(req)
    written_manifests = {
        sym: write_financials_partition(args.root, sym, [v for v in vintages if v.symbol == sym])
        for sym in symbols
    }
    cik_manifests = {
        cik: write_cik_financials_partition(
            args.root, cik, [v for v in vintages if normalize_sec_cik(v.cik) == cik]
        )
        for cik in ciks
    }
    payload.update(
        {
            "status": "completed",
            "symbols_processed": len(symbols),
            "vintages_written": len(vintages),
            "manifests": written_manifests,
        }
    )
    if ciks:
        payload["ciks_processed"] = len(ciks)
        payload["cik_manifests"] = cik_manifests
    log_progress(
        f"sync completed: {len(symbols)} symbol(s), {len(ciks)} CIK(s), {len(vintages)} vintage(s)",
        quiet,
    )


def _run_sync_incremental(
    args: Any,
    payload: dict[str, Any],
    http_client_cls: Any,
    log_progress: Callable[[str, bool], None],
    validate_policy: Callable[[str, int | None], None],
) -> int:
    from .edgartools_adapter import SecFinancialsClient, _load_user_agent_from_config

    ciks = parse_ciks(getattr(args, "ciks", None), getattr(args, "universe_ciks", None))
    if not ciks:
        raise ValueError("--universe-ciks or --ciks is required for sync-incremental")

    forms_list = parse_str_list(getattr(args, "forms", None))
    as_of_raw = getattr(args, "as_of", None)
    as_of_dt = parse_utc_iso(str(as_of_raw), "--as-of") if as_of_raw is not None else None
    overlap_raw = getattr(args, "overlap_hours", None)
    overlap_hours = 24.0 if overlap_raw is None else float(overlap_raw)
    if not math.isfinite(overlap_hours) or overlap_hours < 0.0:
        raise ValueError("--overlap-hours must be a finite non-negative number")
    max_rps_raw = getattr(args, "max_rps", None)
    max_rps = 6.0 if max_rps_raw is None else float(max_rps_raw)
    rate_limiter = SecRateLimiter(max_requests_per_second=max_rps)

    policy = getattr(args, "availability_policy", None) or "accepted-at-plus-lag"
    raw_lag = getattr(args, "lag_days", None)
    if policy == "accepted-at-plus-lag" and raw_lag is None:
        raw_lag = 0
    validate_policy(policy, raw_lag)

    user_agent = resolve_user_agent(args)
    if not user_agent:
        if getattr(args, "config", None):
            user_agent = _load_user_agent_from_config(args.config)
        else:
            raise ValueError("--user-agent-file, --user-agent, or --config is required")

    http_client = http_client_cls(user_agent, limiter=rate_limiter)
    financials_client = SecFinancialsClient(user_agent, http_client=http_client)
    filing_fetcher = make_edgartools_filing_fetcher(
        financials_client,
        availability_policy=policy,
        lag_days=0 if raw_lag is None else int(raw_lag),
    )
    quiet = getattr(args, "quiet", False)
    log_progress(
        f"running incremental financials sync for {len(ciks)} CIK(s) at <= {max_rps:g} rps...",
        quiet,
    )
    receipt = sync_sec_financials_incremental(
        root=args.root,
        ciks=ciks,
        http_client=http_client,
        snapshot_store=SnapshotStore(Path(args.root) / "snapshots"),
        filing_fetcher=filing_fetcher,
        financials_client=financials_client,
        forms=tuple(forms_list) if forms_list else DEFAULT_SYNC_FORMS,
        as_of=as_of_dt,
        overlap=timedelta(hours=overlap_hours),
        rate_limiter=rate_limiter,
        run_id=getattr(args, "run_id", None),
    )
    payload.update(receipt.to_dict())
    payload["receipt_path"] = str(receipt.receipt_path)
    payload["status"] = "completed" if not receipt.failed_ciks else "completed_with_failures"
    log_progress(
        f"incremental sync completed: synced={receipt.synced}, empty={receipt.empty}, "
        f"transient_failure={receipt.transient_failure}, "
        f"permanent_failure={receipt.permanent_failure}",
        quiet,
    )
    return 0 if not receipt.failed_ciks else 1


def _resolve_output_path(args: Any, command: str) -> str:
    out_arg, root_arg = getattr(args, "output", None), getattr(args, "root", None)
    if out_arg and root_arg and Path(out_arg).resolve() != Path(root_arg).resolve():
        raise ValueError("--output and --root must match when both are provided")
    output_path = out_arg or root_arg
    if not output_path:
        raise ValueError(f"--output or --root is required for {command}")
    return str(output_path)


def _run_bulk_project(
    args: Any, payload: dict[str, Any], log_progress: Callable[[str, bool], None]
) -> None:
    cf_zip = getattr(args, "companyfacts_zip", None)
    sub_zip = getattr(args, "submissions_zip", None)
    if not cf_zip:
        raise ValueError("--companyfacts-zip is required for bulk-project")
    if not sub_zip:
        raise ValueError("--submissions-zip is required for bulk-project")
    output_dir = _resolve_output_path(args, "bulk-project")
    ensure_safe_output_root(output_dir)

    raw_ciks, universe_ciks = getattr(args, "ciks", None), getattr(args, "universe_ciks", None)
    ciks = parse_ciks(raw_ciks, universe_ciks) if (raw_ciks or universe_ciks) else None
    forms_list = parse_str_list(getattr(args, "forms", None))
    acc_raw = getattr(args, "acceptance_upper", None)
    acc_upper = parse_utc_iso(str(acc_raw), "--acceptance-upper") if acc_raw is not None else None
    max_entry = getattr(args, "max_entry_bytes", None)
    max_total = getattr(args, "max_total_bytes", None)
    workers = _parse_optional_int(args, "workers", "--workers", 1)

    quiet = getattr(args, "quiet", False)
    log_progress("projecting SEC bulk ZIP archives offline...", quiet)
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
    )
    payload.update(manifest)
    payload.update({"root": str(output_dir), "output": str(output_dir), "status": "completed"})
    log_progress(
        f"bulk-project completed: total_ciks={manifest.get('counts', {}).get('total_ciks', 0)}",
        quiet,
    )


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


def _run_translate_usd(
    args: Any, payload: dict[str, Any], log_progress: Callable[[str, bool], None]
) -> None:
    bulk_dataset = getattr(args, "bulk_dataset", None)
    if not bulk_dataset:
        raise ValueError("--bulk-dataset is required for translate-usd")
    output_dir = _resolve_output_path(args, "translate-usd")
    ensure_safe_output_root(output_dir)

    fx_rates_path = getattr(args, "fx_rates", None)
    fx_table = load_fx_rate_table(fx_rates_path) if fx_rates_path else None
    date_policy = _parse_enum_flag(
        getattr(args, "date_policy", None),
        SecFxDatePolicy,
        SecFxDatePolicy.PERIOD_END_SPOT,
        "--date-policy",
    )
    flow_method = _parse_enum_flag(
        getattr(args, "flow_method", None),
        SecFxFlowMethod,
        SecFxFlowMethod.PERIOD_AVERAGE,
        "--flow-method",
    )
    max_staleness_days = _parse_optional_int(
        args, "max_staleness_days", "--max-staleness-days", DEFAULT_FX_MAX_STALENESS_DAYS
    )
    use_filing_implied = not bool(getattr(args, "no_filing_implied", False))
    allow_spot_fallback = not bool(getattr(args, "no_spot_fallback", False))

    quiet = getattr(args, "quiet", False)
    log_progress("translating SEC bulk fundamentals to USD offline...", quiet)
    manifest = build_usd_translated_fundamentals_dataset(
        bulk_dataset,
        output_dir,
        fx_table=fx_table,
        use_filing_implied=use_filing_implied,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
    )
    payload.update(manifest)
    payload.update({"root": str(output_dir), "output": str(output_dir), "status": "completed"})
    log_progress(
        f"translate-usd completed: total_rows={manifest.get('counts', {}).get('total_rows', 0)}",
        quiet,
    )


def _parse_optional_int(args: Any, attr: str, flag_name: str, default: int) -> int:
    raw = getattr(args, attr, None)
    if type(raw) is bool or (raw is not None and type(raw) is not int):
        raise ValueError(f"{flag_name} must be an integer")
    return default if raw is None else raw


def _parse_iso_date_arg(raw: Any, flag_name: str) -> date:
    if not isinstance(raw, str) or _ISO_DATE_RE.fullmatch(raw.strip()) is None:
        raise ValueError(f"{flag_name} must be an ISO YYYY-MM-DD date")
    return date.fromisoformat(raw.strip())


def _run_fetch_fx_rates(
    args: Any, payload: dict[str, Any], log_progress: Callable[[str, bool], None]
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
    fx_table = fetch_frankfurter_fx_rates(
        start_date=start_date, end_date=end_date, currencies=currencies, max_attempts=max_attempts
    )
    meta = write_fx_rate_table(out_file, fx_table)
    if load_fx_rate_table(out_file).observation_count != meta["observation_count"]:
        raise RuntimeError("reloaded FX rate table observation count mismatch")
    payload.update({**meta, "status": "completed", "source": FRANKFURTER_FX_SOURCE})
    log_progress(f"fetch-fx-rates completed: observations={meta['observation_count']}", quiet)


def run_financials(
    args: Any,
    *,
    load_config: Callable[[str | Path], dict[str, Any]],
    http_client_cls: Any,
    log_progress: Callable[[str, bool], None],
    validate_policy: Callable[[str, int | None], None],
) -> int:
    """Dispatch ``omd sec financials`` subcommands."""
    apply_config(args, load_config)
    cmd = args.financials_command
    exit_code = 0
    payload: dict[str, Any] = {"command": f"sec financials {cmd}"}
    if cmd == "bulk-project":
        _run_bulk_project(args, payload, log_progress)
    elif cmd == "translate-usd":
        _run_translate_usd(args, payload, log_progress)
    elif cmd == "fetch-fx-rates":
        _run_fetch_fx_rates(args, payload, log_progress)
    else:
        if not getattr(args, "root", None):
            raise ValueError("--root is required")
        ensure_safe_output_root(args.root)
        payload["root"] = str(args.root)
        if cmd == "inspect":
            _run_inspect(args, payload)
        elif cmd == "validate":
            _run_validate(args, payload, log_progress)
        elif cmd == "sync":
            _run_sync(args, payload, http_client_cls, log_progress)
        elif cmd == "sync-incremental":
            exit_code = _run_sync_incremental(
                args, payload, http_client_cls, log_progress, validate_policy
            )
        else:
            raise ValueError(f"unsupported financials command: {cmd}")
    print(
        json.dumps(
            payload,
            default=str,
            sort_keys=True,
            separators=(",", ":") if getattr(args, "json", False) else (", ", ": "),
        )
    )
    return exit_code
