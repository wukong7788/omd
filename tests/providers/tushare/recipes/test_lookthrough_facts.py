# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownLambdaType=false, reportArgumentType=false, reportUnnecessaryIsInstance=false
"""Look-through recipe tests: mapping observations, weight vintage audit, bundle."""

import math
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ohmydata.core import EmptyDisposition, FetchProvenance, SnapshotStore
from ohmydata.providers.tushare import (
    EmptyPolicy,
    EtfBasicRequest,
    IndexMemberAllRequest,
    IndexWeightRequest,
    TushareClient,
    TushareFetchResult,
)
from ohmydata.providers.tushare.observations import capture_tushare_result
from ohmydata.providers.tushare.recipes import (
    EconomicCompletenessStatus,
    EtfIndexMappingObservationResult,
    ExpectedCountStatus,
    IndexWeightVintageEvidence,
    LookthroughReadiness,
    LookthroughSourceBundle,
    MappingObservationStatus,
    RetrievalCompletenessStatus,
    WeightTotalStatus,
    audit_index_weight_vintage,
    build_etf_index_mapping_observations,
    build_lookthrough_source_bundle,
)

OBSERVED_AT = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
SECOND_AT = datetime(2024, 1, 3, 3, 4, 5, tzinfo=UTC)
THIRD_AT = datetime(2024, 1, 4, 3, 4, 5, tzinfo=UTC)


def etf_frame(rows):
    fields = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH").fields
    return pd.DataFrame([{**dict.fromkeys(fields), **row} for row in rows], columns=fields)


def index_weight_frame(rows):
    fields = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH", trade_date="20240102"
    ).fields
    defaults = {"index_code": "000016.SH", "trade_date": "20240102"}
    return pd.DataFrame(
        [{**dict.fromkeys(fields), **defaults, **row} for row in rows], columns=fields
    )


def index_member_frame(rows):
    fields = IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ").fields
    defaults = {
        "ts_code": "000001.SZ",
        "l1_code": "801010.SI",
        "l2_code": "801011.SI",
        "l3_code": "801013.SI",
        "in_date": "20200101",
        "is_new": "Y",
    }
    return pd.DataFrame(
        [{**dict.fromkeys(fields), **defaults, **row} for row in rows], columns=fields
    )


class EndpointFake:
    def __init__(self, frame_factory, endpoint):
        self.frame_factory = frame_factory
        self.endpoint = endpoint

    def __getattr__(self, name):
        def call(**kwargs):
            return self.frame_factory()

        return call


def capture(store, request, frame, when=OBSERVED_AT):
    client = TushareClient(EndpointFake(lambda: frame, request.endpoint), clock=lambda: when)
    result = getattr(client, f"fetch_{request.endpoint}")(request)
    return capture_tushare_result(store, request, result, observed_at=when)


def etf_observation(store, rows, when=OBSERVED_AT):
    request = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH")
    return capture(store, request, etf_frame(rows), when)


def weight_observation(store, rows, when=OBSERVED_AT):
    request = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH", trade_date="20240102"
    )
    return capture(store, request, index_weight_frame(rows), when)


def raw_weight_observation(store, rows, when=OBSERVED_AT):
    """Capture an index-weight observation bypassing client-side validation.

    Needed for audit diagnostics (duplicate constituents, foreign index rows)
    that the endpoint client rejects before capture.
    """
    request = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH", trade_date="20240102"
    )
    frame = index_weight_frame(rows)
    provenance = FetchProvenance.from_request(
        request.spec,
        retrieved_at=when,
        attempts=(),
        row_count=len(frame),
        columns=tuple(frame.columns),
        warnings=(),
        snapshot_identities=(),
        empty_disposition=(
            EmptyDisposition.ALLOWED_EMPTY if frame.empty else EmptyDisposition.NOT_EMPTY
        ),
    )
    return capture_tushare_result(
        store, request, TushareFetchResult(frame, provenance, 1), observed_at=when
    )


def member_observation(store, rows, when=OBSERVED_AT):
    request = IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ")
    return capture(store, request, index_member_frame(rows), when)


# ---- ETF mapping observations ----


def test_mapping_observations_emit_one_row_per_symbol_observation(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    first = etf_observation(
        store,
        [
            {"ts_code": "510050.SH", "index_code": "000016.SH", "list_status": "L"},
            {"ts_code": "510300.SH", "index_code": "000300.SH", "list_status": "L"},
        ],
    )
    second = etf_observation(
        store,
        [{"ts_code": "510050.SH", "index_code": "000016.SH", "list_status": "L"}],
        when=SECOND_AT,
    )
    result = build_etf_index_mapping_observations([first, second])
    assert isinstance(result, EtfIndexMappingObservationResult)
    assert result.row_count == 3
    assert result.observation_count == 2
    assert list(result.frame.columns) == [
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
    ]
    rows_510050 = result.frame[result.frame["etf_symbol"] == "510050.SH"]
    assert set(rows_510050["mapping_observation_status"]) == {
        MappingObservationStatus.CURRENT_OBSERVATION_ONLY.value,
        MappingObservationStatus.UNCHANGED_FROM_PREVIOUS.value,
    }
    unchanged = rows_510050[
        rows_510050["mapping_observation_status"]
        == MappingObservationStatus.UNCHANGED_FROM_PREVIOUS.value
    ].iloc[0]
    assert unchanged["provider_first_observed_at"] == OBSERVED_AT.isoformat().replace("+00:00", "Z")
    assert unchanged["snapshot_fetched_at"] == SECOND_AT.isoformat().replace("+00:00", "Z")


def test_mapping_observations_revised_and_missing_index_codes(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    first = etf_observation(store, [{"ts_code": "510050.SH", "index_code": "000016.SH"}])
    revised = etf_observation(
        store, [{"ts_code": "510050.SH", "index_code": "000010.SH"}], when=SECOND_AT
    )
    result = build_etf_index_mapping_observations([first, revised])
    assert result.frame["mapping_observation_status"].tolist() == [
        MappingObservationStatus.CURRENT_OBSERVATION_ONLY.value,
        MappingObservationStatus.REVISED_FROM_PREVIOUS.value,
    ]
    assert result.frame["index_code"].tolist() == ["000016.SH", "000010.SH"]

    missing = etf_observation(
        store, [{"ts_code": "510050.SH", "index_code": math.nan}], when=SECOND_AT
    )
    result = build_etf_index_mapping_observations([first, missing])
    assert result.frame.iloc[1]["mapping_observation_status"] == (
        MappingObservationStatus.MISSING_INDEX_CODE.value
    )
    assert result.frame.iloc[1]["index_code"] is None
    assert result.frame.iloc[1]["provider_first_observed_at"] is None


def test_mapping_observations_preserve_first_observed_across_three_unchanged_versions(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    rows = [{"ts_code": "510050.SH", "index_code": "000016.SH"}]
    result = build_etf_index_mapping_observations(
        [
            etf_observation(store, rows, when=OBSERVED_AT),
            etf_observation(store, rows, when=SECOND_AT),
            etf_observation(store, rows, when=THIRD_AT),
        ]
    )
    expected = OBSERVED_AT.isoformat().replace("+00:00", "Z")
    assert result.frame["provider_first_observed_at"].tolist() == [expected, expected, expected]


def test_mapping_observations_reject_non_etf_basic_sources(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    with pytest.raises(ValueError, match="etf_basic"):
        build_etf_index_mapping_observations([member_observation(store, [])])


def test_mapping_observations_never_emit_historical_intervals(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    observation = etf_observation(store, [{"ts_code": "510050.SH", "index_code": "000016.SH"}])
    result = build_etf_index_mapping_observations([observation])
    for forbidden in ("effective_date", "first_usable_session", "interval_end", "style"):
        assert forbidden not in result.frame.columns
    assert result.frame.iloc[0]["quality_flags"] == "PIT_UNPROVEN"


def test_mapping_observations_empty_input():
    result = build_etf_index_mapping_observations([])
    assert result.row_count == 0
    assert result.observation_count == 0


# ---- index weight vintage audit ----


def test_vintage_audit_preserves_percent_and_converts_decimal(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    observation = weight_observation(
        store,
        [
            {"con_code": "000001.SZ", "weight": 60.0},
            {"con_code": "600000.SH", "weight": 40.0},
        ],
    )
    evidence = audit_index_weight_vintage(observation, total_weight_tolerance=Decimal("0.01"))
    assert isinstance(evidence, IndexWeightVintageEvidence)
    assert evidence.index_code == "000016.SH"
    assert evidence.trade_date == "20240102"
    assert evidence.native_weight_unit == "percent"
    assert evidence.decimal_weight_unit == "decimal"
    assert evidence.conversion_identity == "percent_to_decimal_divide_100"
    assert evidence.observed_component_count == 2
    assert evidence.row_count == 2
    assert evidence.native_weight_sum == Decimal("100.0")
    assert evidence.decimal_weight_sum == Decimal("1.000")
    assert evidence.weight_total_status is WeightTotalStatus.WEIGHT_TOTAL_PASS
    assert evidence.retrieval_status is RetrievalCompletenessStatus.RETRIEVAL_COMPLETENESS_UNPROVEN
    assert (
        evidence.economic_completeness_status
        is EconomicCompletenessStatus.ECONOMIC_COMPLETENESS_UNPROVEN
    )
    assert len(evidence.vintage_identity) == 64
    assert evidence.request_identity == observation.request_identity
    assert evidence.snapshot_identity == observation.snapshot_identity


def test_vintage_audit_deterministic_identity_and_diagnostics(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    observation = raw_weight_observation(
        store,
        [
            {"con_code": "000001.SZ", "weight": 60.0},
            {"con_code": "000001.SZ", "weight": 60.0},
            {"con_code": "600000.SH", "weight": math.nan},
            {"con_code": "600036.SH", "weight": math.inf},
            {"con_code": "601398.SH", "weight": -5.0},
            {"con_code": "601988.SH", "weight": 45.0},
        ],
    )
    evidence = audit_index_weight_vintage(
        observation,
        total_weight_tolerance=Decimal("0.01"),
        expected_component_count=5,
        expected_count_basis="official-index-announcement-20240102",
    )
    assert evidence.observed_component_count == 5
    assert evidence.duplicate_component_rows == 1
    assert evidence.null_weight_count == 1
    assert evidence.non_finite_weight_count == 1
    assert evidence.out_of_range_weight_count == 1
    assert evidence.weight_total_status is WeightTotalStatus.WEIGHT_TOTAL_FAIL
    assert evidence.expected_count_status is ExpectedCountStatus.EXPECTED_COUNT_MATCH
    again = audit_index_weight_vintage(
        observation,
        total_weight_tolerance=Decimal("0.01"),
        expected_component_count=5,
        expected_count_basis="official-index-announcement-20240102",
    )
    assert again.vintage_identity == evidence.vintage_identity
    different_tolerance = audit_index_weight_vintage(observation, total_weight_tolerance=Decimal(1))
    assert different_tolerance.vintage_identity != evidence.vintage_identity


def test_vintage_audit_expected_count_missing_and_mismatch(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    observation = weight_observation(
        store,
        [{"con_code": "000001.SZ", "weight": 60.0}, {"con_code": "600000.SH", "weight": 40.0}],
    )
    missing = audit_index_weight_vintage(observation, total_weight_tolerance=0.01)
    assert missing.expected_count_status is ExpectedCountStatus.EXPECTED_COUNT_MISSING
    assert missing.expected_component_count is None
    assert missing.expected_count_basis is None
    mismatch = audit_index_weight_vintage(
        observation,
        total_weight_tolerance=0.01,
        expected_component_count=3,
        expected_count_basis="official",
    )
    assert mismatch.expected_count_status is ExpectedCountStatus.EXPECTED_COUNT_MISMATCH
    with pytest.raises(ValueError, match="basis"):
        audit_index_weight_vintage(
            observation, total_weight_tolerance=0.01, expected_count_basis="official"
        )


def test_vintage_audit_rejects_invalid_inputs(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    with pytest.raises(ValueError, match="empty"):
        audit_index_weight_vintage(weight_observation(store, []), total_weight_tolerance=0.01)
    observation = raw_weight_observation(
        store,
        [
            {"index_code": "000016.SH", "con_code": "a", "weight": 1.0},
            {"index_code": "000300.SH", "con_code": "b", "weight": 1.0},
        ],
    )
    with pytest.raises(ValueError, match="exactly one"):
        audit_index_weight_vintage(observation, total_weight_tolerance=0.01)
    with pytest.raises(ValueError, match="index_weight"):
        audit_index_weight_vintage(
            etf_observation(store, [{"ts_code": "510050.SH", "index_code": "000016.SH"}]),
            total_weight_tolerance=0.01,
        )


# ---- look-through bundle ----


def make_bundle_inputs(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    mapping = build_etf_index_mapping_observations(
        [etf_observation(store, [{"ts_code": "510050.SH", "index_code": "000016.SH"}])]
    )
    vintage = audit_index_weight_vintage(
        weight_observation(
            store,
            [{"con_code": "000001.SZ", "weight": 60.0}, {"con_code": "600000.SH", "weight": 40.0}],
        ),
        total_weight_tolerance=Decimal("0.01"),
    )
    industry = member_observation(
        store,
        [
            {
                "ts_code": "000001.SZ",
                "l1_code": "801010.SI",
                "l2_code": "801011.SI",
                "l3_code": "801013.SI",
                "in_date": "20200101",
                "is_new": "Y",
            }
        ],
    )
    return mapping, [vintage], [industry]


def test_bundle_builds_manifest_only_and_verifies(tmp_path):
    output = Path(tmp_path) / "bundle"
    mapping, vintages, industry = make_bundle_inputs(tmp_path)
    bundle = build_lookthrough_source_bundle(mapping, vintages, industry, output_dir=output)
    assert isinstance(bundle, LookthroughSourceBundle)
    assert bundle.output_dir == output
    assert len(bundle.bundle_identity) == 64
    assert (output / "manifest.json").is_file()
    assert len(list(output.iterdir())) == 1
    assert bundle.verify()
    loaded = LookthroughSourceBundle.load(output)
    assert loaded.bundle_identity == bundle.bundle_identity
    manifest = dict(bundle.manifest)
    assert manifest["bundle_schema_version"] == 1
    assert manifest["readiness"] == LookthroughReadiness.BLOCKED_UNPROVEN_COMPLETENESS.value
    assert manifest["inputs"]["mapping_observations"]["row_count"] == 1
    assert manifest["inputs"]["weight_vintages"] == [vintages[0].vintage_identity]
    assert manifest["inputs"]["industry_observations"] == [industry[0].observation_identity]
    assert manifest["missing_index_codes"] == []
    assert manifest["missing_industry_components"] == ["600000.SH"]
    assert {row["industry_observation_status"] for row in manifest["component_evidence"]} == {
        "MISSING",
        "OBSERVED",
    }


def test_bundle_deterministic_identity_and_no_overwrite(tmp_path):
    output = Path(tmp_path) / "bundle"
    mapping, vintages, industry = make_bundle_inputs(tmp_path)
    first = build_lookthrough_source_bundle(mapping, vintages, industry, output_dir=output)
    output2 = Path(tmp_path) / "bundle2"
    second = build_lookthrough_source_bundle(mapping, vintages, industry, output_dir=output2)
    assert second.bundle_identity == first.bundle_identity
    with pytest.raises(FileExistsError, match="new or empty"):
        build_lookthrough_source_bundle(mapping, vintages, industry, output_dir=output)


def test_bundle_tamper_rejection(tmp_path):
    output = Path(tmp_path) / "bundle"
    mapping, vintages, industry = make_bundle_inputs(tmp_path)
    bundle = build_lookthrough_source_bundle(mapping, vintages, industry, output_dir=output)
    manifest_path = output / "manifest.json"
    tampered = manifest_path.read_text().replace('"readiness"', '"readiness_x"')
    manifest_path.write_text(tampered)
    assert not bundle.verify()
    assert not LookthroughSourceBundle.load(output).verify()


def test_bundle_unsupported_and_missing_index_codes_explicit(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    mapping = build_etf_index_mapping_observations(
        [etf_observation(store, [{"ts_code": "510050.SH", "index_code": math.nan}])]
    )
    vintage = audit_index_weight_vintage(
        weight_observation(
            store,
            [{"con_code": "000001.SZ", "weight": 100.0}],
        ),
        total_weight_tolerance=Decimal("0.01"),
    )
    industry = member_observation(store, [])
    output = Path(tmp_path) / "bundle"
    bundle = build_lookthrough_source_bundle(mapping, [vintage], [industry], output_dir=output)
    manifest = dict(bundle.manifest)
    assert manifest["missing_index_codes"] == ["510050.SH"]
    assert manifest["unsupported_index_codes"] == []
    assert manifest["readiness"] == LookthroughReadiness.BLOCKED_UNPROVEN_COMPLETENESS.value

    mapped = build_etf_index_mapping_observations(
        [etf_observation(store, [{"ts_code": "510050.SH", "index_code": "000300.SH"}])]
    )
    without_vintage = build_lookthrough_source_bundle(
        mapped, [], [], output_dir=Path(tmp_path) / "bundle-without-vintage"
    )
    assert dict(without_vintage.manifest)["unsupported_index_codes"] == ["000300.SH"]


def test_bundle_never_claims_complete_when_component_industry_is_missing(tmp_path):
    mapping, vintages, industry = make_bundle_inputs(tmp_path)
    proven = replace(
        vintages[0],
        retrieval_status=RetrievalCompletenessStatus.RETRIEVAL_COMPLETE,
        economic_completeness_status=EconomicCompletenessStatus.ECONOMIC_COMPLETENESS_PROVEN,
    )
    bundle = build_lookthrough_source_bundle(
        mapping,
        [proven],
        industry,
        output_dir=Path(tmp_path) / "bundle-missing-industry",
    )
    manifest = dict(bundle.manifest)
    assert manifest["missing_industry_components"] == ["600000.SH"]
    assert manifest["readiness"] == LookthroughReadiness.SOURCE_FACTS_PARTIAL.value


def test_bundle_readiness_partial_and_current_only(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    mapping = build_etf_index_mapping_observations(
        [etf_observation(store, [{"ts_code": "510050.SH", "index_code": "000016.SH"}])]
    )
    industry = member_observation(store, [])
    output = Path(tmp_path) / "bundle"
    bundle = build_lookthrough_source_bundle(mapping, [], [industry], output_dir=output)
    assert dict(bundle.manifest)["readiness"] == (
        LookthroughReadiness.CURRENT_OBSERVATION_ONLY.value
    )
    mapping_only = build_lookthrough_source_bundle(
        mapping, [], [], output_dir=Path(tmp_path) / "bundle2"
    )
    assert dict(mapping_only.manifest)["readiness"] == (
        LookthroughReadiness.CURRENT_OBSERVATION_ONLY.value
    )


def test_bundle_forbidden_keys_and_input_validation(tmp_path):
    output = Path(tmp_path) / "bundle"
    mapping, vintages, industry = make_bundle_inputs(tmp_path)
    bundle = build_lookthrough_source_bundle(mapping, vintages, industry, output_dir=output)
    forbidden = {
        "first_usable_session",
        "style",
        "cluster",
        "target",
        "score",
        "rank",
        "order",
        "action",
        "live_signal",
    }
    assert not forbidden.intersection(bundle.manifest)
    with pytest.raises(TypeError, match="mapping_observations"):
        build_lookthrough_source_bundle(
            "not-a-mapping", vintages, industry, output_dir=Path(tmp_path) / "b2"
        )
    with pytest.raises(ValueError, match="index_member_all"):
        other_store = SnapshotStore(Path(tmp_path) / "s2")
        build_lookthrough_source_bundle(
            mapping,
            vintages,
            [etf_observation(other_store, [{"ts_code": "x", "index_code": "y"}])],
            output_dir=Path(tmp_path) / "b3",
        )
    with pytest.raises(ValueError, match="at least one"):
        build_lookthrough_source_bundle(
            build_etf_index_mapping_observations([]),
            [],
            [],
            output_dir=Path(tmp_path) / "b4",
        )
