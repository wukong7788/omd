"""Argument parser and command helpers for ``omd sec nport``."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .batch import (
    Quarter,
    SecEquityEtfUniverse,
    SecNportBatch,
    ensure_safe_output_root,
    latest_completed_quarter,
    load_universe,
    quarter_range,
)
from .http import SecHttpClient

DEFAULT_MAX_SELECTED_ROWS = 5_000_000
DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024**3


def _log_progress(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def _make_download_progress(prefix: str, quiet: bool) -> Callable[[str, int], None]:
    last_mb = [0]

    def _download_progress(stage: str, count: int) -> None:
        if stage == "download":
            mb = count // (1024 * 1024)
            if mb - last_mb[0] >= 50:
                last_mb[0] = mb
                _log_progress(f"{prefix}: downloaded {mb} MB...", quiet)

    return _download_progress


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid config line: {line}")
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if " #" in val:
            val = val.split(" #", 1)[0].strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        elif val.lower() == "true":
            val = True
        elif val.lower() == "false":
            val = False
        elif val.lower() in ("null", "~"):
            val = None
        elif val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
            val = int(val)
        result[key] = val
    return result


def load_cli_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"config file not found: {path}")
    text = p.read_text(encoding="utf-8")
    ext = p.suffix.lower()
    data: Any = None
    if ext == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON config: {exc}") from exc
    elif ext in (".toml",):
        try:
            import tomllib

            data = tomllib.loads(text)
        except (ValueError, OSError) as exc:
            raise ValueError(f"invalid TOML config: {exc}") from exc
    elif ext in (".yaml", ".yml"):
        try:
            yaml = __import__("yaml")
            data = yaml.safe_load(text)
        except ImportError:
            data = _parse_simple_yaml(text)
        except (ValueError, OSError) as exc:
            raise ValueError(f"invalid YAML config: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            data = _parse_simple_yaml(text)
    if not isinstance(data, dict):
        raise ValueError("config file must contain a key-value mapping")  # noqa: TRY004
    mapping = cast(dict[object, object], data)
    return {str(k).replace("-", "_"): v for k, v in mapping.items()}


def add_nport_commands(subparsers: Any) -> None:
    sec = subparsers.add_parser("sec").add_subparsers(dest="sec_command", required=True)
    nport = sec.add_parser("nport").add_subparsers(dest="nport_command", required=True)
    for command in ("plan", "fetch", "build", "sync", "validate"):
        p = nport.add_parser(command)
        p.add_argument("--config")
        p.add_argument("--quarters")
        p.add_argument("--root")
        if command in ("plan", "fetch", "build", "sync"):
            p.add_argument("--universe")
        if command in ("fetch", "sync"):
            ua = p.add_mutually_exclusive_group()
            ua.add_argument("--user-agent-file")
            ua.add_argument("--user-agent-stdin", action="store_true")
            p.add_argument("--refresh", action="store_true")
        if command in ("build", "sync"):
            p.add_argument("--availability-policy")
            p.add_argument("--lag-days", type=int)
            p.add_argument("--max-selected-rows", type=int, default=DEFAULT_MAX_SELECTED_ROWS)
            p.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
        p.add_argument("--json", action="store_true")
        p.add_argument("--quiet", action="store_true")
    p = nport.add_parser("inspect")
    p.add_argument("--config")
    p.add_argument("--quarter")
    p.add_argument("--root")
    p.add_argument("--symbol")
    p.add_argument("--rows", action="store_true")
    p.add_argument("--json", action="store_true")

    p_qual = nport.add_parser("qualify")
    p_qual.add_argument("--config")
    p_qual.add_argument("--quarters")
    p_qual.add_argument("--root")
    p_qual.add_argument("--universe")
    p_qual.add_argument("--availability-policy")
    p_qual.add_argument("--lag-days", type=int)
    p_qual.add_argument("--partition-set")
    p_qual.add_argument("--output")
    p_qual.add_argument("--json", action="store_true")
    p_qual.add_argument("--quiet", action="store_true")

    fin = sec.add_parser("financials").add_subparsers(dest="financials_command", required=True)
    for command in ("sync", "validate"):
        p_fin = fin.add_parser(command)
        p_fin.add_argument("--config")
        p_fin.add_argument("--symbols")
        p_fin.add_argument("--forms")
        p_fin.add_argument("--root")
        p_fin.add_argument("--user-agent-file")
        p_fin.add_argument("--user-agent")
        p_fin.add_argument("--start-year", type=int)
        p_fin.add_argument("--end-year", type=int)
        p_fin.add_argument("--availability-policy")
        p_fin.add_argument("--lag-days", type=int)
        p_fin.add_argument("--limit", type=int)
        p_fin.add_argument("--latest", action="store_true")
        p_fin.add_argument("--json", action="store_true")
        p_fin.add_argument("--quiet", action="store_true")
    p_insp = fin.add_parser("inspect")
    p_insp.add_argument("--config")
    p_insp.add_argument("--root")
    p_insp.add_argument("--symbol")
    p_insp.add_argument("--rows", action="store_true")
    p_insp.add_argument("--json", action="store_true")


def run(args: Any) -> int:
    if getattr(args, "sec_command", None) == "financials":
        return run_financials(args)
    if getattr(args, "config", None):
        cfg = load_cli_config(args.config)
        for key, val in cfg.items():
            if key in ("json", "quiet", "refresh", "rows", "user_agent_stdin"):
                if not getattr(args, key, False) and bool(val):
                    setattr(args, key, bool(val))
            elif getattr(args, key, None) is None:
                setattr(args, key, val)

    if not getattr(args, "root", None):
        raise ValueError("--root is required")
    ensure_safe_output_root(args.root)

    if args.nport_command == "qualify":
        return run_qualify(args)

    if args.nport_command == "inspect":
        if not getattr(args, "quarter", None):
            raise ValueError("--quarter is required")
        if str(args.quarter).lower() == "latest":
            target_quarter = latest_completed_quarter()
        else:
            target_quarter = Quarter.parse(str(args.quarter))
        quarter_range(str(target_quarter), str(target_quarter))
        payload = SecNportBatch(args.root).inspect_local(
            target_quarter,
            args.symbol,
            args.rows,
        )
    else:
        quiet = getattr(args, "quiet", False)
        if not getattr(args, "quarters", None):
            raise ValueError("--quarters is required")
        quarters_str = str(args.quarters).strip()
        if quarters_str.lower() == "full":
            start = "2019q4"
            end = str(latest_completed_quarter())
        elif ":" in quarters_str:
            start, end = quarters_str.split(":", 1)
        else:
            start = end = quarters_str
        quarters = quarter_range(start, end)
        if args.nport_command in ("build", "sync"):
            if not getattr(args, "availability_policy", None):
                raise ValueError("--availability-policy is required")
            _validate_policy(args.availability_policy, args.lag_days)
            _validate_limits(args.max_selected_rows, args.max_output_bytes)
        selectors: tuple[Any, ...] = ()
        digest = ""
        if args.nport_command in ("plan", "fetch", "build", "sync"):
            if not getattr(args, "universe", None):
                raise ValueError("--universe is required")
            if not Path(args.universe).is_file():
                raise ValueError(f"universe file not found: {args.universe}")
            selectors, digest = load_universe(args.universe)
        payload = {
            "command": args.nport_command,
            "quarters": [str(q) for q in quarters],
            "count": len(quarters),
        }
        if args.nport_command == "plan":
            payload["universe_hash"] = digest
            payload["funds"] = sum(1 for x in selectors if x.active(quarters[0]))
        if args.nport_command in ("fetch", "sync"):
            if not (
                getattr(args, "user_agent_file", None) or getattr(args, "user_agent_stdin", False)
            ):
                raise ValueError("either --user-agent-file or --user-agent-stdin is required")
            if args.user_agent_file and getattr(args, "user_agent_stdin", False):
                raise ValueError("cannot specify both --user-agent-file and --user-agent-stdin")
            if args.user_agent_file:
                ua_path = Path(args.user_agent_file)
                if not ua_path.is_file():
                    raise ValueError(f"User-Agent file not found: {args.user_agent_file}")
                ua = ua_path.read_text(encoding="utf-8").splitlines()
            else:
                ua = sys.stdin.read().splitlines()
            if len(ua) != 1 or not ua[0] or any(ord(c) < 32 or ord(c) > 126 for c in ua[0]):
                raise ValueError("invalid User-Agent file")
            client = SecHttpClient(ua[0])
            if args.nport_command == "fetch":
                service = SecNportBatch(args.root, client)
            else:
                if (args.max_selected_rows, args.max_output_bytes) == (
                    DEFAULT_MAX_SELECTED_ROWS,
                    DEFAULT_MAX_OUTPUT_BYTES,
                ):
                    service = SecNportBatch(args.root, client)
                else:
                    service = SecNportBatch(
                        args.root,
                        client,
                        max_selected_rows=args.max_selected_rows,
                        max_output_bytes=args.max_output_bytes,
                    )
            total = len(quarters)
            for i, quarter in enumerate(quarters, 1):
                t0 = time.monotonic()
                prefix = f"[{i}/{total}] {quarter}"
                cached = (
                    getattr(service, "artifact_for_quarter", None) is not None
                    and service.artifact_for_quarter(quarter) is not None
                    and not args.refresh
                )
                if cached:
                    _log_progress(f"{prefix}: reusing cached N-PORT artifact...", quiet)
                else:
                    _log_progress(f"{prefix}: fetching N-PORT artifact & EDGAR metadata...", quiet)

                service.progress = _make_download_progress(prefix, quiet)

                service.fetch_receipt_for_quarter(quarter, selectors, digest, refresh=args.refresh)
                if args.nport_command == "sync":
                    policy = (
                        "observation-only"
                        if args.availability_policy == "observation-only"
                        else "accepted-at-plus-lag"
                    )
                    _log_progress(f"{prefix}: building core Parquet dataset ({policy})...", quiet)
                    if (args.max_selected_rows, args.max_output_bytes) == (
                        DEFAULT_MAX_SELECTED_ROWS,
                        DEFAULT_MAX_OUTPUT_BYTES,
                    ):
                        service.build_all_receipts(
                            quarter, digest, selectors, policy, args.lag_days
                        )
                    else:
                        service.build_all_receipts(
                            quarter,
                            digest,
                            selectors,
                            policy,
                            args.lag_days,
                            max_selected_rows=args.max_selected_rows,
                            max_output_bytes=args.max_output_bytes,
                        )
                elapsed = time.monotonic() - t0
                _log_progress(f"{prefix}: completed in {elapsed:.1f}s", quiet)
            payload["status"] = "completed"
        elif args.nport_command == "build":
            service = SecNportBatch(
                args.root,
                max_selected_rows=args.max_selected_rows,
                max_output_bytes=args.max_output_bytes,
            )
            total = len(quarters)
            for i, quarter in enumerate(quarters, 1):
                t0 = time.monotonic()
                prefix = f"[{i}/{total}] {quarter}"
                _log_progress(
                    f"{prefix}: building core Parquet dataset ({args.availability_policy})...",
                    quiet,
                )
                if (args.max_selected_rows, args.max_output_bytes) == (
                    DEFAULT_MAX_SELECTED_ROWS,
                    DEFAULT_MAX_OUTPUT_BYTES,
                ):
                    service.build_all_receipts(
                        quarter, digest, selectors, args.availability_policy, args.lag_days
                    )
                else:
                    service.build_all_receipts(
                        quarter,
                        digest,
                        selectors,
                        args.availability_policy,
                        args.lag_days,
                        max_selected_rows=args.max_selected_rows,
                        max_output_bytes=args.max_output_bytes,
                    )
                elapsed = time.monotonic() - t0
                _log_progress(f"{prefix}: completed in {elapsed:.1f}s", quiet)
            payload["status"] = "completed"
        elif args.nport_command == "validate":
            _log_progress(f"validating {len(quarters)} quarter(s)...", quiet)
            validated = SecNportBatch(args.root).validate_local(quarters)
            payload.update(validated)
            _log_progress(
                f"validation completed: {validated.get('artifacts_checked', 0)} artifact(s), "
                f"{validated.get('partitions_checked', 0)} partition(s) verified",
                quiet,
            )
            payload["status"] = "completed"
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":") if getattr(args, "json", False) else (", ", ": "),
        )
    )
    return 0


def _validate_policy(policy: str, lag_days: int | None) -> None:
    """Validate policy before reading contact details or constructing transport."""
    if policy not in ("observation-only", "accepted-at-plus-lag"):
        raise ValueError("invalid availability policy")
    if policy == "observation-only" and lag_days is not None:
        raise ValueError("lag-days is invalid for observation-only")
    if policy == "accepted-at-plus-lag" and (lag_days is None or not 0 <= lag_days <= 30):
        raise ValueError("lag-days must be 0..30")


def _validate_limits(max_selected_rows: int, max_output_bytes: int) -> None:
    if max_selected_rows <= 0:
        raise ValueError("max-selected-rows must be positive")
    if max_output_bytes <= 0:
        raise ValueError("max-output-bytes must be positive")


def run_qualify(args: Any) -> int:
    from .qualification import SecNportPartitionSet, qualify_sec_nport

    if not getattr(args, "quarters", None):
        raise ValueError("--quarters is required")
    quarters_str = str(args.quarters).strip()
    if quarters_str.lower() == "full":
        start = "2019q4"
        end = str(latest_completed_quarter())
    elif ":" in quarters_str:
        start, end = quarters_str.split(":", 1)
    else:
        start = end = quarters_str
    quarters = quarter_range(start, end)

    if not getattr(args, "universe", None):
        raise ValueError("--universe is required")
    if not Path(args.universe).is_file():
        raise ValueError(f"universe file not found: {args.universe}")
    universe = SecEquityEtfUniverse.load(args.universe)

    if not getattr(args, "availability_policy", None):
        raise ValueError("--availability-policy is required")
    _validate_policy(args.availability_policy, args.lag_days)

    if not getattr(args, "output", None):
        raise ValueError("--output is required")
    ensure_safe_output_root(args.output)
    output_path = Path(args.output)

    partition_set_obj = None
    if getattr(args, "partition_set", None):
        partition_set_obj = SecNportPartitionSet.load(args.partition_set)

    quiet = getattr(args, "quiet", False)

    class ConsoleProgress:
        def report(
            self,
            phase: str,
            quarter: str | None = None,
            partition_index: int = 0,
            partition_count: int = 0,
            rows_read: int = 0,
            **kwargs: Any,
        ) -> None:
            if not quiet:
                msg = f"[{phase}]"
                if quarter:
                    msg += f" {quarter} ({partition_index}/{partition_count})"
                _log_progress(msg, quiet)

    ref = qualify_sec_nport(
        root=Path(args.root),
        quarters=quarters,
        universe=universe,
        availability_policy=args.availability_policy,
        lag_days=args.lag_days,
        output=output_path,
        partition_set=partition_set_obj,
        progress=ConsoleProgress() if not quiet else None,
    )

    if getattr(args, "json", False):
        print(json.dumps(ref.receipt, indent=2, sort_keys=True))
    elif not quiet:
        summary = ref.receipt.get("coverage_summary", {})
        print(f"Qualification Status: {ref.status}")
        print(f"Receipt Identity:     {ref.receipt_identity}")
        print(f"Requested Quarters:   {summary.get('requested_quarters')}")
        print(f"Expected Funds:       {summary.get('expected_funds')}")
        print(f"Total Vintages:       {summary.get('total_vintages')}")
        print(f"Elapsed Seconds:      {ref.receipt.get('elapsed_seconds', 0.0):.2f}s")
        print(f"Artifacts Published:  {len(ref.receipt.get('output_artifacts', {}))}")

    return 0 if ref.status == "STRUCTURALLY_COMPLETE" else 1


def run_financials(args: Any) -> int:
    import hashlib
    import importlib

    if getattr(args, "config", None):
        cfg = load_cli_config(args.config)
        for key, val in cfg.items():
            if key in ("json", "quiet", "rows"):
                if not getattr(args, key, False) and bool(val):
                    setattr(args, key, bool(val))
            elif getattr(args, key, None) is None:
                setattr(args, key, val)

    if not getattr(args, "root", None):
        raise ValueError("--root is required")
    ensure_safe_output_root(args.root)

    cmd = args.financials_command
    quiet = getattr(args, "quiet", False)
    payload: dict[str, Any] = {
        "command": f"sec financials {cmd}",
        "root": str(args.root),
    }

    if cmd == "inspect":
        sym = getattr(args, "symbol", None)
        if not sym:
            raise ValueError("--symbol is required for inspect")
        sym_str = str(sym).upper()
        p_dir = Path(args.root) / f"symbol={sym_str}"
        if not p_dir.is_dir():
            raise ValueError(f"partition not found for symbol: {sym_str}")
        m_file = p_dir / "manifest.json"
        if not m_file.is_file():
            raise ValueError(f"manifest.json missing for symbol: {sym_str}")
        m_data: dict[str, Any] = json.loads(m_file.read_text(encoding="utf-8"))
        payload.update(m_data)

        if getattr(args, "rows", False):
            pq: Any = importlib.import_module("pyarrow.parquet")
            s_file = p_dir / "financial_statements.parquet"
            if s_file.is_file():
                tbl: Any = pq.read_table(s_file, partitioning=None)
                payload["rows"] = tbl.to_pylist()[:100]

    elif cmd == "validate":
        root_path = Path(args.root)
        partitions = list(root_path.glob("symbol=*"))
        verified_count = 0
        for p in partitions:
            m_file = p / "manifest.json"
            if not m_file.is_file():
                raise ValueError(f"manifest missing in {p}")
            m: dict[str, Any] = json.loads(m_file.read_text(encoding="utf-8"))
            files = m.get("files", {})
            for fname, meta in files.items():
                fpath = p / fname
                if not fpath.is_file():
                    raise ValueError(f"file missing: {fpath}")
                h = hashlib.sha256(fpath.read_bytes()).hexdigest()
                if h != meta.get("sha256"):
                    raise ValueError(
                        f"hash mismatch for {fpath}: expected {meta.get('sha256')}, got {h}"
                    )
            verified_count += 1
        payload["status"] = "completed"
        payload["partitions_verified"] = verified_count
        _log_progress(f"verified {verified_count} company financials partition(s)", quiet)

    elif cmd == "sync":
        symbols_raw: Any = getattr(args, "symbols", None)
        if not symbols_raw:
            universe_val: Any = getattr(args, "universe", None)
            if isinstance(universe_val, dict) and "symbols" in universe_val:
                symbols_raw = universe_val["symbols"]
            elif isinstance(universe_val, list):
                symbols_raw = universe_val
        if not symbols_raw:
            raise ValueError("--symbols is required for sync")
        symbols: list[str] = []
        if isinstance(symbols_raw, str):
            symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
        elif isinstance(symbols_raw, (list, tuple)):
            for item in cast(list[object], symbols_raw):
                s_str = str(item).strip().upper()
                if s_str:
                    symbols.append(s_str)
        if not symbols:
            raise ValueError("--symbols cannot be empty")

        forms_val = getattr(args, "forms", None)
        if forms_val:
            if isinstance(forms_val, str):
                forms = tuple(f.strip().upper() for f in forms_val.split(",") if f.strip())
            else:
                forms = tuple(str(f).strip().upper() for f in forms_val if str(f).strip())
        else:
            forms = ("10-K", "10-Q")

        from .edgartools_adapter import SecFinancialsClient
        from .financials import SecFinancialsRequest
        from .financials_dataset import write_financials_partition

        user_agent: str = ""
        if getattr(args, "user_agent", None):
            user_agent = str(args.user_agent).strip()
        elif getattr(args, "user_agent_file", None):
            ua_path = Path(args.user_agent_file)
            if not ua_path.is_file():
                raise ValueError(f"User-Agent file not found: {args.user_agent_file}")
            user_agent = ua_path.read_text(encoding="utf-8").strip()

        client = (
            SecFinancialsClient.from_config(args.config)
            if getattr(args, "config", None) and not user_agent
            else SecFinancialsClient(user_agent)
        )

        limit_val = getattr(args, "limit", None)
        if getattr(args, "latest", False):
            limit_val = 1

        req = SecFinancialsRequest(
            symbols=tuple(symbols),
            forms=forms,
            start_year=getattr(args, "start_year", None),
            end_year=getattr(args, "end_year", None),
            availability_policy=getattr(args, "availability_policy", "accepted-at-plus-lag")
            or "accepted-at-plus-lag",
            lag_days=getattr(args, "lag_days", 0)
            if getattr(args, "lag_days", None) is not None
            else 0,
            limit=limit_val,
        )

        _log_progress(f"fetching company financials for {len(symbols)} symbol(s)...", quiet)
        vintages = client.fetch_company_financials(req)

        written_manifests: dict[str, Any] = {}
        for sym in symbols:
            sym_vintages = [v for v in vintages if v.symbol == sym]
            manifest = write_financials_partition(args.root, sym, sym_vintages)
            written_manifests[sym] = manifest

        payload["status"] = "completed"
        payload["symbols_processed"] = len(symbols)
        payload["vintages_written"] = len(vintages)
        payload["manifests"] = written_manifests
        _log_progress(
            f"sync completed: {len(symbols)} symbol(s), {len(vintages)} vintage(s)", quiet
        )

    print(
        json.dumps(
            payload,
            default=str,
            sort_keys=True,
            separators=(",", ":") if getattr(args, "json", False) else (", ", ": "),
        )
    )
    return 0
