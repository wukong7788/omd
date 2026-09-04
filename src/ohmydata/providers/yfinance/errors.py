"""Exceptions for the yfinance provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ohmydata.core.errors import (
    CoverageError,
    OhMyDataError,
    PermanentProviderError,
    ProviderError,
    SchemaMismatchError,
    TransientProviderError,
)


class YFinanceError(ProviderError):
    """Base error for yfinance provider operations."""


class YFinanceVersionMismatchError(PermanentProviderError):
    """Raised when the runtime yfinance version differs from the pinned 1.5.1 baseline."""


class YFinanceEmptyBatchError(PermanentProviderError):
    """Raised when a batch fetch returns completely empty data."""


class YFinanceRepairError(YFinanceError):
    """Raised when bounded per-symbol repair fails to resolve missing data."""


@dataclass(frozen=True)
class YFinanceRepairReceipt:
    """Audit record for a single-symbol recovery attempt."""

    symbol: str
    original_outcome: str
    repair_parameters: dict[str, Any]
    attempts: int
    returned_rows: int
    final_status: str


__all__ = [
    "CoverageError",
    "OhMyDataError",
    "PermanentProviderError",
    "ProviderError",
    "SchemaMismatchError",
    "TransientProviderError",
    "YFinanceEmptyBatchError",
    "YFinanceError",
    "YFinanceRepairError",
    "YFinanceRepairReceipt",
    "YFinanceVersionMismatchError",
]
