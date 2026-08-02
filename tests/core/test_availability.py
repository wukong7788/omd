import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    RequestSpec,
    SnapshotIntegrityError,
    SnapshotMode,
    SnapshotStore,
)

FIRST = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
FETCHED = datetime(2024, 1, 2, 8, 0, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize(
    ("basis", "precision", "source"),
    [
        (AvailabilityBasis.SOURCE_DECLARED, AvailabilityPrecision.TIMESTAMP, FIRST),
        (AvailabilityBasis.SOURCE_DECLARED, AvailabilityPrecision.DATE, date(2024, 1, 1)),
        (AvailabilityBasis.INFERRED_SCHEDULE, AvailabilityPrecision.TIMESTAMP, FIRST),
        (AvailabilityBasis.INFERRED_SCHEDULE, AvailabilityPrecision.DATE, date(2024, 1, 1)),
        (AvailabilityBasis.PROVIDER_FIRST_OBSERVED, AvailabilityPrecision.UNKNOWN, None),
        (AvailabilityBasis.UNKNOWN, AvailabilityPrecision.UNKNOWN, None),
    ],
)
def test_valid_matrix_and_properties(
    basis: AvailabilityBasis,
    precision: AvailabilityPrecision,
    source: datetime | date | None,
) -> None:
    evidence = AvailabilityEvidence(source, FIRST, FETCHED, basis, precision)
    assert evidence.provider_first_observed_at == FIRST
    assert evidence.snapshot_fetched_at == FETCHED
    assert evidence.availability_anchor == (
        source
        if source is not None
        else (FIRST if basis is AvailabilityBasis.PROVIDER_FIRST_OBSERVED else None)
    )
    assert evidence.pit_proven is (
        basis is AvailabilityBasis.SOURCE_DECLARED and precision is AvailabilityPrecision.TIMESTAMP
    )


@pytest.mark.parametrize(
    ("basis", "precision", "source"),
    [
        (AvailabilityBasis.SOURCE_DECLARED, AvailabilityPrecision.UNKNOWN, None),
        (AvailabilityBasis.SOURCE_DECLARED, AvailabilityPrecision.DATE, FIRST),
        (AvailabilityBasis.INFERRED_SCHEDULE, AvailabilityPrecision.TIMESTAMP, date(2024, 1, 1)),
        (AvailabilityBasis.PROVIDER_FIRST_OBSERVED, AvailabilityPrecision.UNKNOWN, FIRST),
        (AvailabilityBasis.UNKNOWN, AvailabilityPrecision.UNKNOWN, date(2024, 1, 1)),
    ],
)
def test_invalid_matrix(
    basis: AvailabilityBasis,
    precision: AvailabilityPrecision,
    source: datetime | date | None,
) -> None:
    with pytest.raises(ValueError):
        AvailabilityEvidence(source, FIRST, FETCHED, basis, precision)


def test_type_ordering_and_utc_serialization() -> None:
    local = datetime(2024, 1, 1, 16, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    evidence = AvailabilityEvidence(
        local, FIRST, FETCHED, AvailabilityBasis.SOURCE_DECLARED, AvailabilityPrecision.TIMESTAMP
    )
    assert evidence.source_available_at == datetime(2024, 1, 1, 8, 0, 0, 123456, tzinfo=UTC)
    data = evidence.to_dict()
    assert data == {
        "source_available_at": "2024-01-01T08:00:00.123456Z",
        "provider_first_observed_at": "2024-01-01T08:00:00Z",
        "snapshot_fetched_at": "2024-01-02T08:00:00.123456Z",
        "availability_basis": "SOURCE_DECLARED",
        "availability_precision": "TIMESTAMP",
        "pit_proven": True,
    }
    assert json.dumps(data)
    data["availability_basis"] = "changed"
    assert evidence.to_dict()["availability_basis"] == "SOURCE_DECLARED"
    with pytest.raises(TypeError):
        AvailabilityEvidence(
            None,
            FIRST.replace(tzinfo=None),
            FETCHED,
            AvailabilityBasis.UNKNOWN,
            AvailabilityPrecision.UNKNOWN,
        )
    with pytest.raises(ValueError):
        AvailabilityEvidence(
            None,
            FIRST,
            FIRST - timedelta(seconds=1),
            AvailabilityBasis.UNKNOWN,
            AvailabilityPrecision.UNKNOWN,
        )
    with pytest.raises(TypeError):
        AvailabilityEvidence(
            None, FIRST, FETCHED, cast(Any, "UNKNOWN"), AvailabilityPrecision.UNKNOWN
        )


def test_from_observation_uses_validated_manifest(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("synthetic", "bars", {"symbol": "FAKE"}, ("close",))
    observation = store.observe(spec, b"payload", FETCHED, "json", SnapshotMode.APPEND)
    evidence = AvailabilityEvidence.from_observation(store, observation)
    assert evidence.provider_first_observed_at == FETCHED
    assert evidence.snapshot_fetched_at == FETCHED
    assert evidence.availability_anchor == evidence.provider_first_observed_at


def test_observation_history_and_revised_payload(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("synthetic", "bars", {"symbol": "FAKE"}, ())
    first_time = datetime(2024, 1, 1, tzinfo=UTC)
    later_time = datetime(2024, 1, 2, tzinfo=UTC)
    first = store.observe(spec, b"raw-a", first_time, "json")
    later = store.observe(spec, b"raw-a", later_time, "json")
    first_evidence = AvailabilityEvidence.from_observation(store, first)
    later_evidence = AvailabilityEvidence.from_observation(store, later)
    assert later_evidence.provider_first_observed_at == first_evidence.provider_first_observed_at
    assert later_evidence.snapshot_fetched_at == later_time

    revised = store.observe(spec, b"raw-b", later_time + timedelta(days=1), "json")
    revised_evidence = AvailabilityEvidence.from_observation(store, revised)
    assert revised.fact_version != first.fact_version
    assert revised.snapshot_identity != first.snapshot_identity
    assert revised_evidence.provider_first_observed_at == later_time + timedelta(days=1)


def test_from_observation_rejects_tampered_receipts_and_files(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("synthetic", "bars", {}, ())
    ref = store.observe(spec, b"arbitrary non-json bytes\x00", FETCHED, "json")
    with pytest.raises(SnapshotIntegrityError):
        AvailabilityEvidence.from_observation(store, replace(ref, provider="forged"))

    manifest = json.loads(ref.path.read_text())
    manifest["observation_identity"] = "0" * 64
    ref.path.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        AvailabilityEvidence.from_observation(store, ref)

    # Recreate a valid receipt, then tamper its linked snapshot payload.
    ref = store.observe(spec, b"different arbitrary bytes", FETCHED, "json")
    snapshot = store.write(spec, b"different arbitrary bytes", FETCHED, "json")
    (snapshot.path / "response.bin").write_bytes(b"tampered")
    with pytest.raises(SnapshotIntegrityError):
        AvailabilityEvidence.from_observation(store, ref)


@pytest.mark.parametrize(
    ("source", "precision"),
    [
        ("2024-01-01", AvailabilityPrecision.DATE),
        (1, AvailabilityPrecision.TIMESTAMP),
        (datetime(2024, 1, 1, tzinfo=UTC), AvailabilityPrecision.DATE),
    ],
)
def test_source_type_rejections(source: Any, precision: AvailabilityPrecision) -> None:
    expected = (
        ValueError if isinstance(source, datetime) and source.tzinfo is not None else TypeError
    )
    with pytest.raises(expected):
        AvailabilityEvidence(
            source,
            FIRST,
            FETCHED,
            AvailabilityBasis.SOURCE_DECLARED,
            precision,
        )


def test_raw_precision_and_naive_datetime_rejections() -> None:
    with pytest.raises(TypeError):
        AvailabilityEvidence(None, FIRST, FETCHED, AvailabilityBasis.UNKNOWN, cast(Any, "UNKNOWN"))
    with pytest.raises(TypeError):
        AvailabilityEvidence(
            FIRST.replace(tzinfo=None),
            FIRST,
            FETCHED,
            AvailabilityBasis.SOURCE_DECLARED,
            AvailabilityPrecision.TIMESTAMP,
        )
    with pytest.raises(TypeError):
        AvailabilityEvidence(
            None,
            FIRST,
            FETCHED.replace(tzinfo=None),
            AvailabilityBasis.UNKNOWN,
            AvailabilityPrecision.UNKNOWN,
        )


def test_date_and_unknown_serialization() -> None:
    date_evidence = AvailabilityEvidence(
        date(2024, 1, 1),
        FIRST,
        FETCHED,
        AvailabilityBasis.INFERRED_SCHEDULE,
        AvailabilityPrecision.DATE,
    )
    assert date_evidence.to_dict()["source_available_at"] == "2024-01-01"
    unknown = AvailabilityEvidence(
        None, FIRST, FETCHED, AvailabilityBasis.UNKNOWN, AvailabilityPrecision.UNKNOWN
    )
    assert unknown.to_dict()["source_available_at"] is None
    assert unknown.to_dict()["pit_proven"] is False


def test_availability_module_is_provider_and_credential_free() -> None:
    source = Path(__file__).parents[2].joinpath("src/ohmydata/core/availability.py").read_text()
    assert all(
        token not in source.lower()
        for token in ("pandas", "polars", "tushare", "os.environ", "dotenv")
    )
