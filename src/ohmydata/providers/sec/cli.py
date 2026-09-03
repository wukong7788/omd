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


def run(args: Any) -> int:
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
