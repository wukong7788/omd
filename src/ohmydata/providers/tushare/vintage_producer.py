"""Provider-fetch orchestration for the vintage plane."""
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ...core import SnapshotStore, SourceFactRegistry
from ...core.errors import ProviderError
from .observations import capture_tushare_result


def produce_etf_benchmark_constituent_vintages(
    client: Any,
    *,
    store: SnapshotStore,
    registry: SourceFactRegistry,
    observed_at: datetime,
    cutoff: datetime,
    scope: Any,
    output_dir: Path,
    audit_policy: dict[str, Any] | None = None,
) -> Any:
    if any(value.tzinfo is None or value.utcoffset() is None for value in (observed_at, cutoff)):
        raise ValueError("observed_at and cutoff must be timezone-aware")
    from .vintage_plane import (
        CoverageItemResult,
        CoverageItemStatus,
        assemble_etf_benchmark_constituent_vintages,
    )

    captured = []
    outcomes = []
    for req, method in [
        *((req, "fetch_etf_basic") for req in scope.etf_requests),
        *((req, "fetch_index_weight") for req in scope.weight_requests),
    ]:
        try:
            captured.append(
                capture_tushare_result(
                    store, req, getattr(client, method)(req), observed_at=observed_at
                )
            )
        except ProviderError:
            outcomes.append(
                CoverageItemResult(
                    req.spec.request_identity,
                    req.endpoint,
                    CoverageItemStatus.FAILURE,
                    reason="provider_failure",
                )
            )
    return assemble_etf_benchmark_constituent_vintages(
        captured,
        store=store,
        registry=registry,
        scope=scope,
        cutoff=cutoff,
        output_dir=output_dir,
        audit_policy=audit_policy,
        coverage_outcomes=outcomes,
    )
