from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    RequestSpec,
    SnapshotStore,
)
from ohmydata.providers.sec import (
    SecCompanyFinancialVintage,
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
    SecStatementRow,
    select_sec_financial_versions,
    serialize_sec_typed_rows_projection,
)

CONFIG = "c" * 64
ARTIFACT = "a" * 64


def _vintage(accession: str = "0000000001-24-000001", value: Decimal = Decimal("1.2300")):
    row = SecStatementRow(
        "income_statement",
        "Revenue",
        "us-gaap:Revenue",
        "Revenue",
        value,
        str(value),
        "USD",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 3, 31),
        period_type="duration",
    )
    return SecCompanyFinancialVintage(
        symbol="FAKE",
        cik="0000000001",
        company_name="Synthetic",
        form="10-Q",
        accession_number=accession,
        filing_date=date(2024, 5, 1),
        accepted_at=datetime(2024, 5, 1, 21, tzinfo=UTC),
        rows=(row,),
    )


def _version(
    root: Path,
    *,
    source_at: datetime = datetime(2024, 5, 1, 21, tzinfo=UTC),
    observed_at: datetime = datetime(2024, 5, 2, 9, tzinfo=UTC),
    recorded_at: datetime = datetime(2024, 5, 2, 10, tzinfo=UTC),
    accession: str = "0000000001-24-000001",
    value: Decimal = Decimal("1.2300"),
    adapter: str = "adapter-v1",
) -> SecNormalizedFinancialFactVersion:
    vintage = _vintage(accession, value)
    store = SnapshotStore(root)
    payload = serialize_sec_typed_rows_projection(
        vintage, source_artifact_identity=ARTIFACT, source_available_at=source_at
    )
    observation = store.observe(
        RequestSpec("sec", "financial-typed-rows", {"accession": accession}, ()),
        payload,
        observed_at,
        "sec-financial-typed-rows-projection-v1",
    )
    availability = AvailabilityEvidence(
        source_at,
        observed_at,
        observed_at,
        AvailabilityBasis.SOURCE_DECLARED,
        AvailabilityPrecision.TIMESTAMP,
    )
    return SecNormalizedFinancialFactVersion.from_projection(
        store=store,
        observation=observation,
        availability=availability,
        vintage=vintage,
        row_ordinal=0,
        schema_version="sec-financial-normalized-v1",
        adapter_version=adapter,
        normalization_version="normalization-v1",
        configuration_identity=CONFIG,
        recorded_at=recorded_at,
    )


def _policy(quality_cutoff: datetime, adapter: str = "adapter-v1") -> SecPitPolicy:
    return SecPitPolicy(
        schema_version="sec-financial-normalized-v1",
        adapter_version=adapter,
        normalization_version="normalization-v1",
        configuration_identity=CONFIG,
        quality_policy_version="quality-v1",
        quality_cutoff=quality_cutoff,
    )


def _pass(version: SecNormalizedFinancialFactVersion, at: datetime) -> SecQualityRecord:
    return SecQualityRecord(version.normalized_version_id, "quality-v1", SecQualityStatus.PASS, at)


def test_market_known_allows_late_observation_but_system_replay_fails_closed(
    tmp_path: Path,
) -> None:
    version = _version(
        tmp_path,
        observed_at=datetime(2024, 5, 3, 9, tzinfo=UTC),
        recorded_at=datetime(2024, 5, 3, 10, tzinfo=UTC),
    )
    quality = _pass(version, datetime(2024, 5, 3, 10, tzinfo=UTC))
    cutoff = datetime(2024, 5, 2, 12, tzinfo=UTC)
    market = select_sec_financial_versions(
        [version],
        mode=SecPitMode.MARKET_KNOWN,
        knowledge_cutoff=cutoff,
        policy=_policy(datetime(2024, 5, 3, 10, tzinfo=UTC)),
        quality_records=[quality],
    )
    system = select_sec_financial_versions(
        [version],
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=cutoff,
        policy=_policy(cutoff),
        quality_records=[quality],
    )
    assert [result.version.normalized_version_id for result in market] == [
        version.normalized_version_id
    ]
    assert system == ()


def test_date_only_or_unbound_source_evidence_is_rejected(tmp_path: Path) -> None:
    vintage = _vintage()
    store = SnapshotStore(tmp_path)
    source_at = datetime(2024, 5, 1, 21, tzinfo=UTC)
    observation = store.observe(
        RequestSpec("sec", "financial-typed-rows", {}, ()),
        serialize_sec_typed_rows_projection(
            vintage, source_artifact_identity=ARTIFACT, source_available_at=source_at
        ),
        datetime(2024, 5, 2, 9, tzinfo=UTC),
        "sec-financial-typed-rows-projection-v1",
    )
    evidence = AvailabilityEvidence(
        source_at.date(),
        observation.snapshot_fetched_at,
        observation.snapshot_fetched_at,
        AvailabilityBasis.SOURCE_DECLARED,
        AvailabilityPrecision.DATE,
    )
    with pytest.raises(ValueError, match="caller-declared and timestamped"):
        SecNormalizedFinancialFactVersion.from_projection(
            store=store,
            observation=observation,
            availability=evidence,
            vintage=vintage,
            row_ordinal=0,
            schema_version="sec-financial-normalized-v1",
            adapter_version="adapter-v1",
            normalization_version="normalization-v1",
            configuration_identity=CONFIG,
            recorded_at=datetime(2024, 5, 2, 10, tzinfo=UTC),
        )


def test_projection_replay_rejects_mismatched_typed_row(tmp_path: Path) -> None:
    version = _version(tmp_path)
    changed = _vintage(value=Decimal("1.2301"))
    store = SnapshotStore(tmp_path)
    observation = version.observation
    evidence = AvailabilityEvidence(
        version.source_available_at,
        observation.snapshot_fetched_at,
        observation.snapshot_fetched_at,
        AvailabilityBasis.SOURCE_DECLARED,
        AvailabilityPrecision.TIMESTAMP,
    )
    with pytest.raises(ValueError, match="projection vintage identity"):
        SecNormalizedFinancialFactVersion.from_projection(
            store=store,
            observation=observation,
            availability=evidence,
            vintage=changed,
            row_ordinal=0,
            schema_version="sec-financial-normalized-v1",
            adapter_version="adapter-v1",
            normalization_version="normalization-v1",
            configuration_identity=CONFIG,
            recorded_at=datetime(2024, 5, 2, 11, tzinfo=UTC),
        )


def test_parser_fix_and_decimal_precision_create_distinct_explicit_versions(tmp_path: Path) -> None:
    first = _version(tmp_path, adapter="adapter-v1")
    repaired = _version(
        tmp_path, adapter="adapter-v2", recorded_at=first.recorded_at + timedelta(hours=1)
    )
    assert first.observation.fact_version == repaired.observation.fact_version
    assert first.content_identity != repaired.content_identity
    assert first.normalized_version_id != repaired.normalized_version_id
    assert first.row.value == Decimal("1.2300")
    selected = select_sec_financial_versions(
        [first, repaired],
        mode=SecPitMode.MARKET_KNOWN,
        knowledge_cutoff=datetime(2024, 5, 3, tzinfo=UTC),
        policy=_policy(datetime(2024, 5, 3, tzinfo=UTC), adapter="adapter-v2"),
        quality_records=[_pass(first, first.recorded_at), _pass(repaired, repaired.recorded_at)],
    )
    assert [item.version.normalized_version_id for item in selected] == [
        repaired.normalized_version_id
    ]


@pytest.mark.parametrize(
    ("knowledge_cutoff", "policy_adapter", "quality_records"),
    [
        (
            datetime(2024, 5, 1, 22, tzinfo=UTC),
            "sec-sgml-financial-adapter-v1",
            (),
        ),
        (
            datetime(2024, 6, 1, tzinfo=UTC),
            "other-adapter-v1",
            (),
        ),
    ],
)
def test_market_known_rejects_sgml_acceptance_proxy_before_eligibility_filters(
    tmp_path: Path,
    knowledge_cutoff: datetime,
    policy_adapter: str,
    quality_records: tuple[SecQualityRecord, ...],
) -> None:
    proxy = _version(tmp_path / "proxy", adapter="sec-sgml-financial-adapter-v1")
    normal = _version(tmp_path / "normal")
    with pytest.raises(ValueError, match="acceptance-proxy"):
        select_sec_financial_versions(
            [normal, proxy],
            mode=SecPitMode.MARKET_KNOWN,
            knowledge_cutoff=knowledge_cutoff,
            policy=_policy(knowledge_cutoff, adapter=policy_adapter),
            quality_records=quality_records,
        )


def test_system_replay_retains_sgml_acceptance_proxy_causal_gates(tmp_path: Path) -> None:
    proxy = _version(tmp_path, adapter="sec-sgml-financial-adapter-v1")
    quality = _pass(proxy, datetime(2024, 5, 2, 11, tzinfo=UTC))
    commit = SecConsumerCommit(
        proxy.normalized_version_id,
        quality.quality_record_id,
        "d" * 64,
        datetime(2024, 5, 2, 12, tzinfo=UTC),
    )
    policy = _policy(datetime(2024, 5, 2, 13, tzinfo=UTC), adapter=proxy.adapter_version)
    selected = select_sec_financial_versions(
        [proxy],
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=policy.quality_cutoff,
        policy=policy,
        quality_records=[quality],
        consumer_commits=[commit],
    )
    assert selected[0].version == proxy
    assert selected[0].quality_record == quality
    assert selected[0].consumer_commit == commit


def test_restated_accessions_remain_separate_and_are_not_combined(tmp_path: Path) -> None:
    original = _version(tmp_path / "original", source_at=datetime(2024, 5, 1, 21, tzinfo=UTC))
    restated = _version(
        tmp_path / "restated",
        accession="0000000001-24-000099",
        source_at=datetime(2024, 6, 1, 21, tzinfo=UTC),
        observed_at=datetime(2024, 6, 2, 9, tzinfo=UTC),
        recorded_at=datetime(2024, 6, 2, 10, tzinfo=UTC),
    )
    qualities = [_pass(original, original.recorded_at), _pass(restated, restated.recorded_at)]
    before = select_sec_financial_versions(
        [restated, original],
        mode=SecPitMode.MARKET_KNOWN,
        knowledge_cutoff=datetime(2024, 5, 15, tzinfo=UTC),
        policy=_policy(datetime(2024, 6, 3, tzinfo=UTC)),
        quality_records=qualities,
    )
    after = select_sec_financial_versions(
        [restated, original],
        mode=SecPitMode.MARKET_KNOWN,
        knowledge_cutoff=datetime(2024, 6, 3, tzinfo=UTC),
        policy=_policy(datetime(2024, 6, 3, tzinfo=UTC)),
        quality_records=qualities,
    )
    assert [item.version.accession_number for item in before] == [original.accession_number]
    assert [item.version.accession_number for item in after] == [
        original.accession_number,
        restated.accession_number,
    ]


def test_system_requires_exact_pass_commit_and_honors_later_revoke(tmp_path: Path) -> None:
    version = _version(tmp_path)
    passed = _pass(version, datetime(2024, 5, 2, 11, tzinfo=UTC))
    commit = SecConsumerCommit(
        version.normalized_version_id,
        passed.quality_record_id,
        "d" * 64,
        datetime(2024, 5, 2, 12, tzinfo=UTC),
    )
    revoked = SecQualityRecord(
        version.normalized_version_id,
        "quality-v1",
        SecQualityStatus.REVOKED,
        datetime(2024, 5, 3, tzinfo=UTC),
        passed.quality_record_id,
    )
    before = select_sec_financial_versions(
        [version],
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=datetime(2024, 5, 2, 13, tzinfo=UTC),
        policy=_policy(datetime(2024, 5, 2, 13, tzinfo=UTC)),
        quality_records=[passed, revoked],
        consumer_commits=[commit],
    )
    after = select_sec_financial_versions(
        [version],
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=datetime(2024, 5, 3, 1, tzinfo=UTC),
        policy=_policy(datetime(2024, 5, 3, 1, tzinfo=UTC)),
        quality_records=[passed, revoked],
        consumer_commits=[commit],
    )
    missing_commit = select_sec_financial_versions(
        [version],
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=datetime(2024, 5, 2, 13, tzinfo=UTC),
        policy=_policy(datetime(2024, 5, 2, 13, tzinfo=UTC)),
        quality_records=[passed],
    )
    assert before[0].consumer_commit == commit
    assert after == ()
    assert missing_commit == ()


def test_system_rejects_stale_quality_cutoff_and_market_bounds_production_time(
    tmp_path: Path,
) -> None:
    version = _version(tmp_path, recorded_at=datetime(2024, 5, 3, 10, tzinfo=UTC))
    quality = _pass(version, datetime(2024, 5, 3, 11, tzinfo=UTC))
    with pytest.raises(ValueError, match="must equal"):
        select_sec_financial_versions(
            [version],
            mode=SecPitMode.SYSTEM_REPLAY,
            knowledge_cutoff=datetime(2024, 5, 4, tzinfo=UTC),
            policy=_policy(datetime(2024, 5, 3, 12, tzinfo=UTC)),
            quality_records=[quality],
        )
    market = select_sec_financial_versions(
        [version],
        mode=SecPitMode.MARKET_KNOWN,
        knowledge_cutoff=datetime(2024, 5, 4, tzinfo=UTC),
        policy=_policy(datetime(2024, 5, 3, 9, tzinfo=UTC)),
        quality_records=[quality],
    )
    assert market == ()


def test_same_time_quality_conflict_fails_instead_of_using_input_order(tmp_path: Path) -> None:
    version = _version(tmp_path)
    at = datetime(2024, 5, 3, tzinfo=UTC)
    passed = _pass(version, at)
    with pytest.raises(ValueError, match="conflicting quality"):
        select_sec_financial_versions(
            [version],
            mode=SecPitMode.MARKET_KNOWN,
            knowledge_cutoff=at,
            policy=_policy(at),
            quality_records=[
                passed,
                SecQualityRecord(
                    version.normalized_version_id,
                    "quality-v1",
                    SecQualityStatus.REVOKED,
                    at,
                    passed.quality_record_id,
                ),
            ],
        )


def test_factory_binding_rejects_dataclass_replace_and_invalid_quality_chain(
    tmp_path: Path,
) -> None:
    version = _version(tmp_path)
    with pytest.raises(ValueError, match="projection binding"):
        replace(version, source_available_at=datetime(2024, 1, 1, tzinfo=UTC))
    revoked = SecQualityRecord(
        version.normalized_version_id,
        "quality-v1",
        SecQualityStatus.REVOKED,
        datetime(2024, 5, 3, tzinfo=UTC),
        "f" * 64,
    )
    with pytest.raises(ValueError, match="supersedes target"):
        select_sec_financial_versions(
            [version, version],
            mode=SecPitMode.MARKET_KNOWN,
            knowledge_cutoff=datetime(2024, 5, 4, tzinfo=UTC),
            policy=_policy(datetime(2024, 5, 4, tzinfo=UTC)),
            quality_records=[revoked],
        )


def test_selector_rejects_factory_bypass_object(tmp_path: Path) -> None:
    version = _version(tmp_path)
    forged = SimpleNamespace(**vars(version))
    forged.source_available_at = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(TypeError, match="SecNormalizedFinancialFactVersion"):
        select_sec_financial_versions(
            [forged],
            mode=SecPitMode.MARKET_KNOWN,
            knowledge_cutoff=datetime(2024, 1, 2, tzinfo=UTC),
            policy=_policy(datetime(2024, 5, 3, tzinfo=UTC)),
            quality_records=[_pass(version, version.recorded_at)],
        )
