# pyright: reportUnnecessaryIsInstance=false
"""Index-weight vintage audit over one captured exact-date index_weight result."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ..observations import TushareObservedResult

NATIVE_WEIGHT_UNIT = "percent"
DECIMAL_WEIGHT_UNIT = "decimal"
PERCENT_TO_DECIMAL_IDENTITY = "percent_to_decimal_divide_100"
_ONE_HUNDRED = Decimal(100)
_RANGE_MIN = Decimal(0)
_RANGE_MAX = Decimal(100)


class RetrievalCompletenessStatus(str, Enum):
    RETRIEVAL_COMPLETE = "RETRIEVAL_COMPLETE"
    RETRIEVAL_COMPLETENESS_UNPROVEN = "RETRIEVAL_COMPLETENESS_UNPROVEN"


class WeightTotalStatus(str, Enum):
    WEIGHT_TOTAL_PASS = "WEIGHT_TOTAL_PASS"
    WEIGHT_TOTAL_FAIL = "WEIGHT_TOTAL_FAIL"


class ExpectedCountStatus(str, Enum):
    EXPECTED_COUNT_MATCH = "EXPECTED_COUNT_MATCH"
    EXPECTED_COUNT_MISSING = "EXPECTED_COUNT_MISSING"
    EXPECTED_COUNT_MISMATCH = "EXPECTED_COUNT_MISMATCH"


class EconomicCompletenessStatus(str, Enum):
    ECONOMIC_COMPLETENESS_PROVEN = "ECONOMIC_COMPLETENESS_PROVEN"
    ECONOMIC_COMPLETENESS_UNPROVEN = "ECONOMIC_COMPLETENESS_UNPROVEN"


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise TypeError("total_weight_tolerance must be Decimal, int, or float")


@dataclass(frozen=True)
class IndexWeightVintageEvidence:
    """Deterministic audit of one captured index-weight vintage."""

    index_code: str
    trade_date: str
    native_weight_unit: str
    decimal_weight_unit: str
    conversion_identity: str
    observed_component_count: int
    component_codes: tuple[str, ...]
    row_count: int
    expected_component_count: int | None
    expected_count_basis: str | None
    duplicate_component_rows: int
    null_weight_count: int
    non_finite_weight_count: int
    out_of_range_weight_count: int
    native_weight_sum: Decimal
    decimal_weight_sum: Decimal
    total_weight_tolerance: Decimal
    weight_total_status: WeightTotalStatus
    expected_count_status: ExpectedCountStatus
    retrieval_status: RetrievalCompletenessStatus
    economic_completeness_status: EconomicCompletenessStatus
    vintage_identity: str
    request_identity: str
    response_sha256: str
    snapshot_identity: str
    observation_identity: str
    fact_version: str
    snapshot_fetched_at: datetime


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    try:
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return True
    except ImportError:  # pragma: no cover - pandas is a hard dependency here
        pass
    return False


def _vintage_identity(
    observation: TushareObservedResult,
    tolerance: Decimal,
    expected_count: int | None,
    basis: str | None,
) -> str:
    payload = json.dumps(
        {
            "request_identity": observation.request_identity,
            "response_sha256": observation.response_sha256,
            "observation_identity": observation.observation_identity,
            "total_weight_tolerance": str(tolerance),
            "expected_component_count": expected_count,
            "expected_count_basis": basis,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _retrieval_completeness(observation: TushareObservedResult) -> RetrievalCompletenessStatus:
    """Return the retrieval-completeness status for one observed vintage.

    The official ``index_weight`` contract (doc 96) documents no usable
    truncation or count guarantee, so retrieval completeness is always unproven
    in this release. The enum keeps the proven state for a future contract
    that supplies the missing authority.
    """
    return RetrievalCompletenessStatus.RETRIEVAL_COMPLETENESS_UNPROVEN


def audit_index_weight_vintage(
    observation: TushareObservedResult,
    *,
    total_weight_tolerance: float,
    expected_component_count: int | None = None,
    expected_count_basis: str | None = None,
) -> IndexWeightVintageEvidence:
    """Audit one captured exact-date ``index_weight`` vintage.

    The audit preserves native percent weights and exposes decimal weights only
    under the explicit conversion identity ``percent_to_decimal_divide_100``.
    Retrieval completeness is unproven for this provider contract: the official
    ``index_weight`` endpoint documents no usable truncation or count
    guarantee. Economic completeness is unproven unless retrieval completeness
    and an auditable caller-declared expected count both pass. Missing
    constituents are never treated as zero.
    """
    frame = observation.frame
    if observation.observation.endpoint != "index_weight":
        raise ValueError("weight observation must come from index_weight")
    if len(frame) == 0:
        raise ValueError("cannot audit an empty index weight observation")
    if "index_code" not in frame.columns or "trade_date" not in frame.columns:
        raise ValueError("index weight observation is missing identity columns")
    index_codes = {str(value) for value in frame["index_code"].tolist()}
    trade_dates = {str(value) for value in frame["trade_date"].tolist()}
    if len(index_codes) != 1 or len(trade_dates) != 1:
        raise ValueError("observation must contain exactly one index_code and trade_date")
    index_code = next(iter(index_codes))
    trade_date = next(iter(trade_dates))

    tolerance = _decimal(total_weight_tolerance)
    if tolerance < 0:
        raise ValueError("total_weight_tolerance must be non-negative")
    if expected_component_count is not None:
        if type(expected_component_count) is not int or expected_component_count < 0:
            raise ValueError("expected_component_count must be a non-negative int")
        if expected_count_basis is None or not expected_count_basis.strip():
            raise ValueError("expected_count_basis must be a non-blank string")
    elif expected_count_basis is not None:
        raise ValueError("expected_count_basis requires expected_component_count")

    con_codes = [str(value) for value in frame["con_code"].tolist()]
    raw_weights = frame["weight"].tolist()

    component_codes = tuple(sorted(set(con_codes)))
    observed_component_count = len(component_codes)
    duplicate_component_rows = len(con_codes) - observed_component_count
    null_weight_count = sum(1 for value in raw_weights if _is_null(value))
    non_finite = [
        value
        for value in raw_weights
        if not _is_null(value) and (isinstance(value, float) and not math.isfinite(value))
    ]
    non_finite_weight_count = len(non_finite)
    finite_weights = [
        Decimal(str(value))
        for value in raw_weights
        if not _is_null(value) and not (isinstance(value, float) and not math.isfinite(value))
    ]
    out_of_range_weight_count = sum(
        1 for value in finite_weights if value < _RANGE_MIN or value > _RANGE_MAX
    )
    native_weight_sum = sum(finite_weights, Decimal(0))
    decimal_weight_sum = native_weight_sum / _ONE_HUNDRED

    total_ok = (
        null_weight_count == 0
        and non_finite_weight_count == 0
        and abs(native_weight_sum - _ONE_HUNDRED) <= tolerance
    )
    weight_total_status = (
        WeightTotalStatus.WEIGHT_TOTAL_PASS if total_ok else WeightTotalStatus.WEIGHT_TOTAL_FAIL
    )
    if expected_component_count is None:
        expected_count_status = ExpectedCountStatus.EXPECTED_COUNT_MISSING
    elif expected_component_count == observed_component_count:
        expected_count_status = ExpectedCountStatus.EXPECTED_COUNT_MATCH
    else:
        expected_count_status = ExpectedCountStatus.EXPECTED_COUNT_MISMATCH

    retrieval_status = _retrieval_completeness(observation)
    economic = (
        EconomicCompletenessStatus.ECONOMIC_COMPLETENESS_PROVEN
        if retrieval_status is RetrievalCompletenessStatus.RETRIEVAL_COMPLETE
        and expected_count_status is ExpectedCountStatus.EXPECTED_COUNT_MATCH
        else EconomicCompletenessStatus.ECONOMIC_COMPLETENESS_UNPROVEN
    )

    return IndexWeightVintageEvidence(
        index_code=index_code,
        trade_date=trade_date,
        native_weight_unit=NATIVE_WEIGHT_UNIT,
        decimal_weight_unit=DECIMAL_WEIGHT_UNIT,
        conversion_identity=PERCENT_TO_DECIMAL_IDENTITY,
        observed_component_count=observed_component_count,
        component_codes=component_codes,
        row_count=len(frame),
        expected_component_count=expected_component_count,
        expected_count_basis=expected_count_basis,
        duplicate_component_rows=duplicate_component_rows,
        null_weight_count=null_weight_count,
        non_finite_weight_count=non_finite_weight_count,
        out_of_range_weight_count=out_of_range_weight_count,
        native_weight_sum=native_weight_sum,
        decimal_weight_sum=decimal_weight_sum,
        total_weight_tolerance=tolerance,
        weight_total_status=weight_total_status,
        expected_count_status=expected_count_status,
        retrieval_status=retrieval_status,
        economic_completeness_status=economic,
        vintage_identity=_vintage_identity(
            observation, tolerance, expected_component_count, expected_count_basis
        ),
        request_identity=observation.request_identity,
        response_sha256=observation.response_sha256,
        snapshot_identity=observation.snapshot_identity,
        observation_identity=observation.observation_identity,
        fact_version=observation.fact_version,
        snapshot_fetched_at=observation.snapshot_fetched_at,
    )


__all__ = [
    "DECIMAL_WEIGHT_UNIT",
    "NATIVE_WEIGHT_UNIT",
    "PERCENT_TO_DECIMAL_IDENTITY",
    "EconomicCompletenessStatus",
    "ExpectedCountStatus",
    "IndexWeightVintageEvidence",
    "RetrievalCompletenessStatus",
    "WeightTotalStatus",
    "audit_index_weight_vintage",
]
