"""Endpoint models, capabilities, requests, and results for Sharadar Direct."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ohmydata.core.policy import RetryPolicy
from ohmydata.core.provenance import FetchProvenance

STANDARD_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
)


class SharadarTable(StrEnum):
    """Supported Sharadar Direct v1.0 bulk tables."""

    STOCKS = "stocks"
    FUNDS = "funds"
    TICKERS = "tickers"
    ACTIONS = "actions"
    SP500 = "sp500"
    FUNDAMENTALS = "fundamentals"
    DAILY = "daily"


class SharadarAdjustmentMode(StrEnum):
    """Price adjustment modes supported for Sharadar EOD price bars."""

    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    UNADJUSTED = "UNADJUSTED"
    TOTAL_RETURN = "TOTAL_RETURN"


@dataclass(frozen=True)
class SharadarCapabilities:
    """Explicit provider capability flags and known dataset caveats."""

    supports_delisted: bool = True
    has_permaticker: bool = True
    eod_only: bool = True
    single_venue_caveat_exchanges: tuple[str, ...] = ("BATS", "CBOE_BZX")


SHARADAR_CAPABILITIES = SharadarCapabilities()


def normalize_table(table: SharadarTable | str) -> SharadarTable:
    """Normalize and validate a Sharadar table name."""
    if isinstance(table, SharadarTable):
        return table
    if not isinstance(table, str):
        raise TypeError(f"Unsupported Sharadar table type: {type(table).__name__}")
    cleaned = table.strip().lower()
    return SharadarTable(cleaned)


def normalize_years(years: str | int | None) -> str | None:
    """Validate and normalize the optional ``years`` query parameter."""
    if years is None:
        return None
    if isinstance(years, bool):
        raise TypeError("years cannot be a boolean")
    cleaned = str(years).strip().lower()
    if not cleaned:
        raise ValueError("years cannot be empty or whitespace")
    if cleaned == "full":
        return "full"
    if cleaned.isdigit() and int(cleaned) > 0:
        return str(int(cleaned))
    raise ValueError(f"Invalid Sharadar years parameter: {years!r}")


@dataclass(frozen=True)
class SharadarBulkTableRequest:
    """Typed request contract for downloading a Sharadar Direct bulk CSV/ZIP table."""

    table: SharadarTable | str
    years: str | int | None = None
    output_dir: Path | str = Path("data/raw/us/sharadar")
    keep_zip: bool = True
    timeout: float = 300.0
    retry_policy: RetryPolicy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "table", normalize_table(self.table))
        object.__setattr__(self, "years", normalize_years(self.years))

        if isinstance(self.output_dir, str):
            if not self.output_dir.strip():
                raise ValueError("output_dir cannot be empty")
            object.__setattr__(self, "output_dir", Path(self.output_dir))
        elif isinstance(self.output_dir, Path):
            object.__setattr__(self, "output_dir", self.output_dir)
        else:
            raise TypeError(
                f"output_dir must be a Path or str, got {type(self.output_dir).__name__}"
            )

        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(float(self.timeout))
            or float(self.timeout) <= 0
        ):
            raise ValueError(f"timeout must be a positive finite number, got {self.timeout!r}")
        object.__setattr__(self, "timeout", float(self.timeout))
        object.__setattr__(self, "keep_zip", bool(self.keep_zip))


@dataclass(frozen=True)
class SharadarTableStatusResult:
    """Result of a Sharadar Direct table status probe (``status=True``)."""

    table: SharadarTable
    years: str | None
    available: bool
    status_code: int
    message: str
    provenance: FetchProvenance | None = None


@dataclass(frozen=True)
class SharadarBulkDownloadResult:
    """Result of a Sharadar Direct bulk table download with SHA-256 integrity checksums."""

    table: SharadarTable
    years: str | None
    csv_path: Path
    csv_bytes: int
    csv_sha256: str
    zip_path: Path | None = None
    zip_bytes: int = 0
    zip_sha256: str | None = None
    was_zip: bool = False
    provenance: FetchProvenance | None = None


__all__ = [
    "SHARADAR_CAPABILITIES",
    "STANDARD_COLUMNS",
    "SharadarAdjustmentMode",
    "SharadarBulkDownloadResult",
    "SharadarBulkTableRequest",
    "SharadarCapabilities",
    "SharadarTable",
    "SharadarTableStatusResult",
    "normalize_table",
    "normalize_years",
]
