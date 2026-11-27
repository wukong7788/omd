"""Sharadar Direct v1.0 bulk ingestion provider and local file readers."""

from .client import DEFAULT_BASE_URL, SharadarClient
from .endpoints import (
    DEFAULT_MAX_EXTRACTED_BYTES,
    SHARADAR_CAPABILITIES,
    STANDARD_COLUMNS,
    SharadarAdjustmentMode,
    SharadarBulkDownloadResult,
    SharadarBulkTableRequest,
    SharadarCapabilities,
    SharadarTable,
    SharadarTableStatusResult,
)
from .errors import (
    SharadarError,
    SharadarTruncatedDownloadError,
    classify_sharadar_http_error,
    redact_secret,
)
from .readers import read_local_actions, read_local_bars
from .readers_polars import (
    read_local_actions_pl,
    read_local_bars_pl,
    scan_local_actions_pl,
    scan_local_bars_pl,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_EXTRACTED_BYTES",
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
    "SharadarTruncatedDownloadError",
    "classify_sharadar_http_error",
    "read_local_actions",
    "read_local_actions_pl",
    "read_local_bars",
    "read_local_bars_pl",
    "redact_secret",
    "scan_local_actions_pl",
    "scan_local_bars_pl",
]
