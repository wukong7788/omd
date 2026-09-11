from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    RequestSpec,
    SnapshotStore,
)
from ohmydata.core.errors import SnapshotIntegrityError
from ohmydata.providers.sec import (
    SecCompanyFinancialVintage,
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
    SecStatementRow,
    load_sec_pit_bundle,
    select_sec_financial_versions,
    serialize_sec_typed_rows_projection,
    write_sec_pit_bundle,
)

CONFIG = "c" * 64
ARTIFACT = "a" * 64


def _version(
    root: Path,
    *,
    row: SecStatementRow | None = None,
    normalization_version: str = "normalization-v1",
) -> SecNormalizedFinancialFactVersion:
    row = row or SecStatementRow(
        "income_statement",
        "Revenue",
        "us-gaap:Revenue",
        "Revenue",
        Decimal("1.2300"),
        "1.2300",
        "USD",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 3, 31),
        period_type="duration",
        dimension=None,
    )
    vintage = SecCompanyFinancialVintage(
        "FAKE",
        "0000000001",
        "Synthetic",
        "10-Q",
        "0000000001-24-000001",
        date(2024, 5, 1),
        accepted_at=datetime(2024, 5, 1, 21, tzinfo=UTC),
        rows=(row,),
    )
    store = SnapshotStore(root)
    source_at = datetime(2024, 5, 1, 21, tzinfo=UTC)
    observation = store.observe(
        RequestSpec("sec", "financial-typed-rows", {"accession": vintage.accession_number}),
        serialize_sec_typed_rows_projection(
            vintage, source_artifact_identity=ARTIFACT, source_available_at=source_at
        ),
        datetime(2024, 5, 2, 9, tzinfo=UTC),
        "sec-financial-typed-rows-projection-v1",
    )
    return SecNormalizedFinancialFactVersion.from_projection(
        store=store,
        observation=observation,
        availability=AvailabilityEvidence(
            source_at,
            observation.snapshot_fetched_at,
            observation.snapshot_fetched_at,
            AvailabilityBasis.SOURCE_DECLARED,
            AvailabilityPrecision.TIMESTAMP,
        ),
        vintage=vintage,
        row_ordinal=0,
        schema_version="sec-financial-normalized-v1",
        adapter_version="adapter-v1",
        normalization_version=normalization_version,
        configuration_identity=CONFIG,
        recorded_at=datetime(2024, 5, 2, 10, tzinfo=UTC),
    )


def _records(
    version: SecNormalizedFinancialFactVersion,
) -> tuple[SecQualityRecord, SecConsumerCommit]:
    quality = SecQualityRecord(
        version.normalized_version_id,
        "quality-v1",
        SecQualityStatus.PASS,
        datetime(2024, 5, 2, 11, tzinfo=UTC),
    )
    return quality, SecConsumerCommit(
        version.normalized_version_id,
        quality.quality_record_id,
        "d" * 64,
        datetime(2024, 5, 2, 12, tzinfo=UTC),
    )


def _write(tmp_path: Path):
    version = _version(tmp_path / "source")
    quality, commit = _records(version)
    bundle_store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_pit_bundle(
        store=bundle_store,
        batch_identity="batch-1",
        versions=[version],
        quality_records=[quality],
        consumer_commits=[commit],
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    return version, quality, commit, bundle_store, ref


def test_bundle_replay_preserves_row_and_selector_semantics(tmp_path: Path) -> None:
    version, quality, commit, bundle_store, ref = _write(tmp_path)
    calls: list[str] = []
    loaded = load_sec_pit_bundle(
        store=bundle_store,
        bundle_ref=ref,
        source_store=SnapshotStore(tmp_path / "source"),
        resolve_observation=lambda identity: calls.append(identity) or version.observation,
    )
    rebuilt = loaded.versions[0]
    assert calls == [version.observation.observation_identity]
    assert rebuilt.normalized_version_id == version.normalized_version_id
    assert rebuilt.row.value == Decimal("1.2300") and rebuilt.row.dimension is None
    policy = SecPitPolicy(
        "sec-financial-normalized-v1",
        "adapter-v1",
        "normalization-v1",
        CONFIG,
        "quality-v1",
        datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    assert (
        select_sec_financial_versions(
            loaded.versions,
            mode=SecPitMode.SYSTEM_REPLAY,
            knowledge_cutoff=policy.quality_cutoff,
            policy=policy,
            quality_records=loaded.quality_records,
            consumer_commits=loaded.consumer_commits,
        )[0].version.normalized_version_id
        == version.normalized_version_id
    )
    assert (
        commit.commit_id == loaded.consumer_commits[0].commit_id
        and quality.quality_record_id == loaded.quality_records[0].quality_record_id
    )


def test_bundle_rejects_mismatched_resolver_and_tampered_receipt(tmp_path: Path) -> None:
    version, _, _, bundle_store, ref = _write(tmp_path)
    with pytest.raises(ValueError, match="resolver"):
        load_sec_pit_bundle(
            store=bundle_store,
            bundle_ref=ref,
            source_store=SnapshotStore(tmp_path / "source"),
            resolve_observation=lambda _: replace(
                version.observation, observation_identity="b" * 64
            ),
        )
    path = ref.path / "response.bin"
    payload = path.read_bytes().replace(b'"adapter-v1"', b'"adapter-v2"')
    path.write_bytes(payload)
    with pytest.raises(SnapshotIntegrityError):
        load_sec_pit_bundle(
            store=bundle_store,
            bundle_ref=ref,
            source_store=SnapshotStore(tmp_path / "source"),
            resolve_observation=lambda _: version.observation,
        )


def test_bundle_rejects_causal_and_duplicate_quality_records(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    first = SecQualityRecord(
        version.normalized_version_id, "quality-v1", SecQualityStatus.PASS, version.recorded_at
    )
    conflict = SecQualityRecord(
        version.normalized_version_id, "quality-v1", SecQualityStatus.REVOKED, version.recorded_at
    )
    with pytest.raises(ValueError, match="revoked"):
        write_sec_pit_bundle(
            store=SnapshotStore(tmp_path / "bundle"),
            batch_identity="batch-1",
            versions=[version],
            quality_records=[first, conflict],
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        )
    early = SecQualityRecord(
        version.normalized_version_id,
        "quality-v1",
        SecQualityStatus.PASS,
        datetime(2024, 5, 2, 9, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="causally"):
        write_sec_pit_bundle(
            store=SnapshotStore(tmp_path / "bundle2"),
            batch_identity="batch-2",
            versions=[version],
            quality_records=[early],
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        )


def test_bundle_bounds_generators_and_canonicalizes_exact_duplicates(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    quality, _ = _records(version)
    store = SnapshotStore(tmp_path / "bundle")
    with pytest.raises(ValueError, match="limit"):
        write_sec_pit_bundle(
            store=store,
            batch_identity="bounded",
            versions=(version for _ in range(3)),
            quality_records=(),
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
            max_records=1,
        )
    ref = write_sec_pit_bundle(
        store=store,
        batch_identity="duplicates",
        versions=[version, version],
        quality_records=[quality, quality],
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    loaded = load_sec_pit_bundle(
        store=store,
        bundle_ref=ref,
        source_store=SnapshotStore(tmp_path / "source"),
        resolve_observation=lambda _: version.observation,
    )
    assert len(loaded.versions) == len(loaded.quality_records) == 1
