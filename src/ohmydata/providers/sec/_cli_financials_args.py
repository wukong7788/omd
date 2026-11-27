"""Argument parser registration and input helpers for ``omd sec financials``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .financials import normalize_sec_cik
from .financials_sync import load_cik_universe

BOOL_CONFIG_KEYS = frozenset(
    {"json", "quiet", "rows", "latest", "no_amendments", "no_filing_implied", "no_spot_fallback"}
)
PARSER_VERSIONS = (
    "sec-live-financial-parser-v1-edgartools-6.4.0",
    "sec-live-financial-parser-v2-edgartools-6.4.0",
)


def _add_flags(
    parser: Any,
    str_flags: tuple[str, ...] = (),
    *,
    int_flags: tuple[str, ...] = (),
    float_flags: tuple[str, ...] = (),
    bool_flags: tuple[str, ...] = (),
    include_quiet: bool = True,
) -> None:
    parser.add_argument("--config")
    parser.add_argument("--root")
    parser.add_argument("--json", action="store_true")
    if include_quiet:
        parser.add_argument("--quiet", action="store_true")
    for flag in str_flags:
        parser.add_argument(flag)
    for flag in int_flags:
        parser.add_argument(flag, type=int)
    for flag in float_flags:
        parser.add_argument(flag, type=float)
    for flag in bool_flags:
        parser.add_argument(flag, action="store_true")


def add_financials_commands(sec_subparsers: Any) -> None:
    """Register ``omd sec financials ...`` subcommands."""
    fin = sec_subparsers.add_parser("financials").add_subparsers(
        dest="financials_command", required=True
    )

    for command in ("sync", "validate"):
        p_fin = fin.add_parser(command)
        _add_flags(
            p_fin,
            (
                "--symbols",
                "--ciks",
                "--forms",
                "--user-agent-file",
                "--user-agent",
                "--availability-policy",
            ),
            int_flags=("--start-year", "--end-year", "--lag-days", "--limit"),
            bool_flags=("--latest",),
        )
        p_fin.add_argument("--parser-version", choices=PARSER_VERSIONS)

    _add_flags(
        fin.add_parser("inspect"),
        ("--symbol", "--cik"),
        bool_flags=("--rows",),
        include_quiet=False,
    )
    _add_flags(
        fin.add_parser("sync-incremental"),
        (
            "--universe-ciks",
            "--ciks",
            "--forms",
            "--as-of",
            "--availability-policy",
            "--user-agent-file",
            "--user-agent",
            "--run-id",
        ),
        int_flags=("--lag-days",),
        float_flags=("--overlap-hours", "--max-rps"),
    )
    _add_flags(
        fin.add_parser("bulk-project"),
        (
            "--companyfacts-zip",
            "--submissions-zip",
            "--output",
            "--universe-ciks",
            "--ciks",
            "--forms",
            "--acceptance-upper",
        ),
        int_flags=(
            "--start-year",
            "--end-year",
            "--max-entry-bytes",
            "--max-total-bytes",
            "--workers",
        ),
        bool_flags=("--no-amendments",),
    )
    _add_flags(
        fin.add_parser("translate-usd"),
        (
            "--bulk-dataset",
            "--output",
            "--fx-rates",
            "--date-policy",
            "--flow-method",
        ),
        int_flags=("--max-staleness-days",),
        bool_flags=("--no-filing-implied", "--no-spot-fallback"),
    )
    _add_flags(
        fin.add_parser("fetch-fx-rates"),
        (
            "--output",
            "--start-date",
            "--end-date",
            "--currencies",
            "--source",
        ),
        int_flags=("--max-attempts",),
    )


def apply_config(args: Any, load_config: Callable[[str | Path], dict[str, Any]]) -> None:
    if not getattr(args, "config", None):
        return
    for key, val in load_config(args.config).items():
        if key in BOOL_CONFIG_KEYS:
            if not getattr(args, key, False) and bool(val):
                setattr(args, key, bool(val))
        elif getattr(args, key, None) is None:
            setattr(args, key, val)


def parse_str_list(raw: Any, *, uppercase: bool = True) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        pieces: list[object] = list(raw.split(","))
    elif isinstance(raw, (list, tuple)):
        pieces = list(cast(list[object], raw))
    else:
        raise TypeError("list option must be a comma-separated string or list")
    return [val.upper() if uppercase else val for item in pieces if (val := str(item).strip())]


def parse_ciks(raw_ciks: Any, universe_path: str | Path | None = None) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    if universe_path:
        for cik in load_cik_universe(universe_path):
            if cik not in seen:
                seen.add(cik)
                ordered.append(cik)
    if raw_ciks is not None:
        if isinstance(raw_ciks, str):
            raw_items: list[object] = [p.strip() for p in raw_ciks.split(",") if p.strip()]
        elif isinstance(raw_ciks, (list, tuple)):
            raw_items = list(cast(list[object], raw_ciks))
        else:
            raise TypeError("--ciks must be a comma-separated string or list")
        for item in raw_items:
            if not isinstance(item, (str, int)) or isinstance(item, bool):
                raise ValueError("invalid CIK entry")  # noqa: TRY004
            norm = normalize_sec_cik(item)
            if norm not in seen:
                seen.add(norm)
                ordered.append(norm)
    return tuple(ordered)


def parse_utc_iso(raw: str, flag_name: str) -> datetime:
    text = raw.strip()
    if not text or "T" not in text:
        raise ValueError(f"{flag_name} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {flag_name} ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{flag_name} must be timezone-aware (e.g. trailing Z or +00:00)")
    return parsed.astimezone(UTC)


def resolve_user_agent(args: Any) -> str:
    ua_direct, ua_file = getattr(args, "user_agent", None), getattr(args, "user_agent_file", None)
    if ua_direct and ua_file:
        raise ValueError("cannot specify both --user-agent and --user-agent-file")
    if ua_direct:
        value, err_msg = str(ua_direct).strip(), "invalid User-Agent"
    elif ua_file:
        ua_path = Path(ua_file)
        if not ua_path.is_file():
            raise ValueError("User-Agent file not found")
        lines = ua_path.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1 or not lines[0].strip():
            raise ValueError("invalid User-Agent file")
        value, err_msg = lines[0].strip(), "invalid User-Agent file"
    else:
        return ""
    if not value or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError(err_msg)
    return value
