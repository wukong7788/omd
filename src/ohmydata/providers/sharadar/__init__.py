"""Sharadar Direct v1.0 bulk ingestion provider and local file readers."""

from .client import DEFAULT_BASE_URL, SharadarClient
from .endpoints import (
    SHARADAR_CAPABILITIES,
    STANDARD_COLUMNS,
    SharadarAdjustmentMode,
    SharadarBulkDownloadResult,
    SharadarBulkTableRequest,
    SharadarCapabilities,
    SharadarTable,
    SharadarTableStatusResult,
)
from .errors import SharadarError, classify_sharadar_http_error, redact_secret
from .readers import read_local_actions, read_local_bars
from .readers_polars import (
    read_local_actions_pl,
    read_local_bars_pl,
    scan_local_actions_pl,
    scan_local_bars_pl,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "SHARADAR_CAPABILITIES",
    "STANDARD_COLUMNS",
    "SharadarAdjustmentMode",
    "SharadarBulkDownloadResult",
    "SharadarBulkTableRequest",
    "SharadarCapabilities",
    "SharadarClient",
    "SharadarError",
    "SharadarTable",
    "SharadarTableStatusResult",
    "classify_sharadar_http_error",
    "read_local_actions",
    "read_local_actions_pl",
    "read_local_bars",
    "read_local_bars_pl",
    "redact_secret",
    "scan_local_actions_pl",
    "scan_local_bars_pl",
]
