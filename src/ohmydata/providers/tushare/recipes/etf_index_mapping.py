# pyright: reportUnnecessaryIsInstance=false
"""ETF-to-index mapping observation recipe over captured etf_basic results."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from ..observations import TushareObservedResult
from .shared import canonical_frame_hash


class MappingObservationStatus(str, Enum):
    CURRENT_OBSERVATION_ONLY = "CURRENT_OBSERVATION_ONLY"
    UNCHANGED_FROM_PREVIOUS = "UNCHANGED_FROM_PREVIOUS"
    REVISED_FROM_PREVIOUS = "REVISED_FROM_PREVIOUS"
    MISSING_INDEX_CODE = "MISSING_INDEX_CODE"


_OUTPUT_COLUMNS = (
    "etf_symbol",
    "index_code",
    "list_status",
    "provider_first_observed_at",
    "snapshot_fetched_at",
    "request_identity",
    "response_sha256",
    "snapshot_identity",
    "observation_identity",
    "fact_version",
    "mapping_observation_status",
    "quality_flags",
)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, init=False)
class EtfIndexMappingObservationResult:
    """One observed exact mapping version per ``(etf_symbol, observation_identity)``."""

    _frame: Any
    observation_count: int
    content_sha256: str

    def __init__(self, frame: Any, observation_count: int, content_sha256: str) -> None:
        if type(observation_count) is not int or observation_count < 0:
            raise ValueError("observation_count must be a non-negative int")
        if type(content_sha256) is not str or len(content_sha256) != 64:
            raise ValueError("content_sha256 must be a SHA-256 hex digest")
        object.__setattr__(self, "_frame", frame.copy(deep=True))
        object.__setattr__(self, "observation_count", observation_count)
        object.__setattr__(self, "content_sha256", content_sha256)

    @property
    def frame(self) -> Any:
        return self._frame.copy(deep=True)

    @property
    def row_count(self) -> int:
        return len(self._frame)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        import math

        return math.isnan(value)
    if type(value) is str:
        return not value.strip()
    return False


def build_etf_index_mapping_observations(
    observations: Sequence[TushareObservedResult],
) -> EtfIndexMappingObservationResult:
    """Build observed ETF-to-index mapping versions from captured etf_basic results.

    The output grain is one row per ``(etf_symbol, observation_identity)``.
    Statuses distinguish the first observation, unchanged mappings, revised
    mappings, and missing index codes. No ``effective_date``, historical
    mapping interval, or ``first_usable_session`` is ever emitted: a newly
    observed old ETF record remains ``PIT_UNPROVEN`` for earlier decision
    dates.
    """
    ordered = sorted(
        observations,
        key=lambda item: (item.snapshot_fetched_at, item.observation_identity),
    )
    previous: dict[str, tuple[str | None, datetime]] = {}
    rows: list[list[Any]] = []
    for observation in ordered:
        if observation.observation.endpoint != "etf_basic":
            raise ValueError("mapping observations must come from etf_basic")
        frame = observation.frame
        fetched = observation.snapshot_fetched_at
        if "ts_code" not in frame.columns or "index_code" not in frame.columns:
            raise ValueError("etf_basic observation is missing ts_code or index_code")
        has_list_status = "list_status" in frame.columns
        for _, row in frame.iterrows():
            symbol = str(row["ts_code"])
            index_code = row["index_code"]
            missing = _is_missing(index_code)
            prior = previous.get(symbol)
            if missing:
                status = MappingObservationStatus.MISSING_INDEX_CODE.value
                first_observed = None
            elif prior is None:
                status = MappingObservationStatus.CURRENT_OBSERVATION_ONLY.value
                first_observed = fetched
            elif prior[0] == str(index_code):
                status = MappingObservationStatus.UNCHANGED_FROM_PREVIOUS.value
                first_observed = prior[1]
            else:
                status = MappingObservationStatus.REVISED_FROM_PREVIOUS.value
                first_observed = fetched
            previous[symbol] = (
                None if missing else str(index_code),
                first_observed if first_observed is not None else fetched,
            )
            rows.append(
                [
                    symbol,
                    None if missing else str(index_code),
                    str(row["list_status"]) if has_list_status else None,
                    _utc_iso(first_observed) if first_observed is not None else None,
                    _utc_iso(fetched),
                    observation.request_identity,
                    observation.response_sha256,
                    observation.snapshot_identity,
                    observation.observation_identity,
                    observation.fact_version,
                    status,
                    "PIT_UNPROVEN",
                ]
            )
    import pandas as pd

    frame = pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)
    return EtfIndexMappingObservationResult(frame, len(ordered), canonical_frame_hash(frame))


__all__ = [
    "EtfIndexMappingObservationResult",
    "MappingObservationStatus",
    "build_etf_index_mapping_observations",
]
