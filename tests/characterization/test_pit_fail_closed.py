"""Offline characterization fixtures for fail-closed PIT contracts."""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityPrecision,
    PaginationError,
    RawFactEnvelope,
    RawFactQualityFlag,
    RawFactRevisionStatus,
    RequestSpec,
    SnapshotIntegrityError,
    SnapshotStore,
)
from ohmydata.providers.tushare import EmptyPolicy, FundAdjustmentRequest, TushareClient


def _fact(
    root: Path,
    *,
    payload: bytes,
    fetched: datetime,
    row: dict[str, Any],
    source_available_at: datetime | date | None = None,
    basis: AvailabilityBasis = AvailabilityBasis.PROVIDER_FIRST_OBSERVED,
    precision: AvailabilityPrecision = AvailabilityPrecision.UNKNOWN,
) -> RawFactEnvelope:
    store = SnapshotStore(root)
    observation = store.observe(
        RequestSpec("synthetic", "facts", {"symbol": "FAKE"}, ()),
        payload,
        fetched,
        "json-v1",
    )
    return RawFactEnvelope.from_observation(
        store,
        observation,
        native_fields=row,
        primary_key_fields=("symbol", "event_date"),
        entity_fields=("symbol",),
        native_schema_version="row-v1",
        adapter_version="fixture-v1",
        source_available_at=source_available_at,
        availability_basis=basis,
        availability_precision=precision,
    )


def test_revision_and_late_arrival_never_backdates_observation(tmp_path: Path) -> None:
    first = _fact(
        tmp_path / "first",
        payload=b"v1",
        fetched=datetime(2024, 2, 1, 9, tzinfo=UTC),
        row={"symbol": "FAKE", "event_date": date(2020, 1, 1), "value": 1},
    )
    later = _fact(
        tmp_path / "later",
        payload=b"v2",
        fetched=datetime(2024, 2, 2, 9, tzinfo=UTC),
        row={"symbol": "FAKE", "event_date": date(2020, 1, 1), "value": 2},
    )
    assert first.revision_status is RawFactRevisionStatus.UNCLASSIFIED
    assert later.revision_status is RawFactRevisionStatus.UNCLASSIFIED
    assert RawFactQualityFlag.PIT_UNPROVEN.value in later.quality_flags
    revised = later.classify_against(first)
    assert revised.revision_status is RawFactRevisionStatus.REVISED_FROM_PREVIOUS
    assert revised.snapshot_fetched_at == datetime(2024, 2, 2, 9, tzinfo=UTC)
    assert revised.availability.availability_anchor == datetime(2024, 2, 2, 9, tzinfo=UTC)


def test_date_only_source_evidence_is_explicitly_unproven(tmp_path: Path) -> None:
    envelope = _fact(
        tmp_path,
        payload=b"date-only",
        fetched=datetime(2024, 1, 4, tzinfo=UTC),
        row={"symbol": "FAKE", "event_date": date(2020, 1, 1)},
        source_available_at=date(2024, 1, 3),
        basis=AvailabilityBasis.SOURCE_DECLARED,
        precision=AvailabilityPrecision.DATE,
    )
    serialized = envelope.to_dict()
    assert RawFactQualityFlag.DATE_ONLY_AVAILABILITY.value in envelope.quality_flags
    assert RawFactQualityFlag.PIT_UNPROVEN.value in envelope.quality_flags
    assert serialized["availability"]["source_available_at"] == "2024-01-03"
    assert "cutoff" not in serialized["availability"]
    assert "first_usable_session" not in serialized["availability"]


def test_consumer_cutoff_boundary_is_not_a_core_policy(tmp_path: Path) -> None:
    before = _fact(
        tmp_path / "before",
        payload=b"before",
        fetched=datetime(2024, 1, 3, 10, tzinfo=UTC),
        row={"symbol": "FAKE", "event_date": date(2024, 1, 2)},
        source_available_at=datetime(2024, 1, 3, 8, 59, 59, tzinfo=UTC),
        basis=AvailabilityBasis.SOURCE_DECLARED,
        precision=AvailabilityPrecision.TIMESTAMP,
    )
    after = _fact(
        tmp_path / "after",
        payload=b"after",
        fetched=datetime(2024, 1, 3, 10, tzinfo=UTC),
        row={"symbol": "FAKE", "event_date": date(2024, 1, 2)},
        source_available_at=datetime(2024, 1, 3, 9, 0, 1, tzinfo=UTC),
        basis=AvailabilityBasis.SOURCE_DECLARED,
        precision=AvailabilityPrecision.TIMESTAMP,
    )
    for envelope in (before, after):
        serialized = envelope.to_dict()
        assert envelope.availability.pit_proven
        assert serialized["availability"]["source_available_at"].endswith("Z")
        assert all(
            key not in serialized
            for key in ("cutoff", "calendar", "dataset_commit", "first_usable_session")
        )


def test_replay_requires_exact_request_identity(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("synthetic", "facts", {"symbol": "FAKE"}, ())
    observation = store.observe(spec, b"payload", datetime(2024, 1, 2, tzinfo=UTC), "json-v1")
    replay = store.replay_observation(observation, spec)
    envelope = RawFactEnvelope.from_observation(
        store,
        observation,
        native_fields={"symbol": "FAKE", "event_date": date(2024, 1, 2)},
        primary_key_fields=("symbol", "event_date"),
        entity_fields=("symbol",),
        native_schema_version="row-v1",
        adapter_version="fixture-v1",
    )
    assert replay.payload == b"payload"
    assert envelope.fact_version == observation.fact_version
    assert envelope.payload_hash == observation.response_sha256
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(
            observation, RequestSpec("synthetic", "facts", {"symbol": "OTHER"}, ())
        )


class _SyntheticFundAdj:
    def __init__(self, pages: list[pd.DataFrame]) -> None:
        self.pages = pages
        self.calls = 0

    def fund_adj(self, **_: Any) -> pd.DataFrame:
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


def _adj_row(day: str) -> pd.DataFrame:
    return pd.DataFrame({"ts_code": ["FAKE"], "trade_date": [day], "adj_factor": [1.0]})


def test_fund_adj_pagination_is_complete_and_fail_closed() -> None:
    empty = pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])
    request = FundAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE", page_size=1)
    complete = TushareClient(
        _SyntheticFundAdj([_adj_row("20240102"), empty])
    ).fetch_fund_adjustment(request)
    assert complete.page_count == 2
    assert complete.frame["trade_date"].tolist() == ["20240102"]

    with pytest.raises(PaginationError):
        TushareClient(
            _SyntheticFundAdj([_adj_row("20240102"), _adj_row("20240101")])
        ).fetch_fund_adjustment(
            FundAdjustmentRequest(
                empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE", page_size=1, max_pages=2
            )
        )
    with pytest.raises(PaginationError):
        TushareClient(
            _SyntheticFundAdj([_adj_row("20240102"), _adj_row("20240102"), empty])
        ).fetch_fund_adjustment(
            FundAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE", page_size=1)
        )


def test_current_observation_does_not_prove_historical_vintage(tmp_path: Path) -> None:
    envelope = _fact(
        tmp_path,
        payload=b"current",
        fetched=datetime(2024, 2, 1, tzinfo=UTC),
        row={"symbol": "FAKE", "event_date": date(2000, 1, 1), "value": 7},
        basis=AvailabilityBasis.PROVIDER_FIRST_OBSERVED,
        precision=AvailabilityPrecision.UNKNOWN,
    )
    serialized = envelope.to_dict()
    assert serialized["snapshot_fetched_at"] == "2024-02-01T00:00:00Z"
    assert serialized["availability"]["source_available_at"] is None
    assert serialized["availability"]["pit_proven"] is False
    assert RawFactQualityFlag.PIT_UNPROVEN.value in envelope.quality_flags


# Exact-cap coverage for non-pageable daily, adj_factor, daily_basic, and
# fund_share remains owned by ``test_nonpageable_caps`` in
# tests/providers/tushare/test_client.py (referenced, not duplicated here).
