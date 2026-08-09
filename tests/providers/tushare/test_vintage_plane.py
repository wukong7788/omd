# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportArgumentType=false, reportPrivateUsage=false
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownMemberType=false
from ohmydata.core import SnapshotStore, SourceFactRegistry
from ohmydata.core.errors import PermanentProviderError
from ohmydata.providers.tushare import (
    EmptyPolicy,
    EtfBasicRequest,
    EtfBenchmarkConstituentScope,
    IndexWeightRequest,
    TushareClient,
    assemble_etf_benchmark_constituent_vintages,
    produce_etf_benchmark_constituent_vintages,
)
from ohmydata.providers.tushare.observations import capture_tushare_result
from ohmydata.providers.tushare.vintage_gates import recompute_artifact_gate_passes
from ohmydata.providers.tushare.vintage_plane import PARQUET_COLUMNS, EtfBenchmarkConstituentBundle

OBSERVED = datetime(2026, 1, 1, tzinfo=UTC)


class _FakePro:
    def __init__(self, frame):
        self.frame = frame

    def etf_basic(self, **kwargs):
        return self.frame.copy()

    def index_weight(self, **kwargs):
        return self.frame.copy()


def _capture_mapping(store: SnapshotStore):
    req = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH")
    frame = pd.DataFrame(
        [{**dict.fromkeys(req.fields), "ts_code": "510050.SH", "index_code": "000016.SH"}],
        columns=req.fields,
    )
    result = TushareClient(_FakePro(frame), clock=lambda: OBSERVED).fetch_etf_basic(req)
    return req, capture_tushare_result(store, req, result, observed_at=OBSERVED)


def test_empty_offline_bundle_is_deterministic_and_tamper_detected(tmp_path: Path):
    out = tmp_path / "bundle"
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "snapshots"),
        registry=SourceFactRegistry(tmp_path / "registry"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        output_dir=out,
    )
    assert bundle.verify()
    assert {p.name for p in out.iterdir()} == {
        "etf_benchmark_vintages.parquet",
        "index_weight_vintages.parquet",
        "index_weight_constituents.parquet",
        "source_resolution_manifest.parquet",
        "coverage_and_gap_report.json",
        "machine_gate.json",
    }
    try:
        assemble_etf_benchmark_constituent_vintages(
            [],
            store=SnapshotStore(tmp_path / "s2"),
            registry=SourceFactRegistry(tmp_path / "r2"),
            scope=EtfBenchmarkConstituentScope(),
            cutoff=datetime(2026, 1, 1, tzinfo=UTC),
            output_dir=out,
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("overwrite must fail")


def test_existing_empty_target_refused(tmp_path: Path):
    target = tmp_path / "bundle"
    target.mkdir()
    with pytest.raises(FileExistsError):
        assemble_etf_benchmark_constituent_vintages(
            [],
            store=SnapshotStore(tmp_path / "s"),
            registry=SourceFactRegistry(tmp_path / "r"),
            scope=EtfBenchmarkConstituentScope(),
            cutoff=datetime(2026, 1, 1, tzinfo=UTC),
            output_dir=target,
        )


def test_each_bundle_file_tamper_is_rejected(tmp_path: Path):
    target = tmp_path / "bundle"
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        output_dir=target,
    )
    for name in (
        "coverage_and_gap_report.json",
        "machine_gate.json",
        "etf_benchmark_vintages.parquet",
    ):
        original = (target / name).read_bytes()
        (target / name).write_bytes(original + b"x")
        with pytest.raises(ValueError):
            bundle.verify()
        (target / name).write_bytes(original)


def test_gate_flip_and_file_set_tamper(tmp_path: Path):
    target = tmp_path / "bundle"
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        output_dir=target,
    )
    gate = json.loads((target / "machine_gate.json").read_text())
    gate["passes"] = not gate["passes"]
    (target / "machine_gate.json").write_text(json.dumps(gate))
    with pytest.raises(ValueError):
        bundle.verify()


def test_extra_and_missing_file_rejected(tmp_path: Path):
    target = tmp_path / "bundle"
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        output_dir=target,
    )
    (target / "extra").write_text("x")
    with pytest.raises(ValueError):
        bundle.verify()
    (target / "extra").unlink()
    (target / "machine_gate.json").unlink()
    with pytest.raises(ValueError):
        bundle.verify()


def test_deterministic_empty_bundle_identity(tmp_path: Path):
    kwargs = {
        "store": SnapshotStore(tmp_path / "s"),
        "registry": SourceFactRegistry(tmp_path / "r"),
        "scope": EtfBenchmarkConstituentScope(),
        "cutoff": datetime(2026, 1, 1, tzinfo=UTC),
    }
    first = assemble_etf_benchmark_constituent_vintages([], output_dir=tmp_path / "a", **kwargs)
    second = assemble_etf_benchmark_constituent_vintages([], output_dir=tmp_path / "b", **kwargs)
    assert first.bundle_identity == second.bundle_identity


def test_assembler_captures_mapping_and_registry(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req, obs = _capture_mapping(store)
    bundle = assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=registry,
        scope=EtfBenchmarkConstituentScope(etf_requests=(req,)),
        cutoff=OBSERVED,
        output_dir=tmp_path / "out",
    )
    assert bundle.verify() and registry.observations


def test_identical_reassembly_is_idempotent(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req, obs = _capture_mapping(store)
    scope = EtfBenchmarkConstituentScope(etf_requests=(req,))
    bundle = assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "a",
    )
    gate = json.loads((bundle.output_dir / "machine_gate.json").read_text())
    assert gate["passes"] is True
    assert all(entry["passes"] is True for entry in gate["gates"].values())
    mapping_rows = pd.read_parquet(bundle.output_dir / "etf_benchmark_vintages.parquet")
    assert len(mapping_rows.iloc[0]["payload_sha256"]) == 64
    assert mapping_rows.iloc[0]["payload_sha256"] == obs.response_sha256
    count = len(registry.observations)
    generations = len(list((tmp_path / "r" / "manifests").glob("*.json")))
    assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    assert (
        len(registry.observations) == count
        and len(list((tmp_path / "r" / "manifests").glob("*.json"))) == generations
    )


def test_mapping_opt_in_schema_has_canonical_aliases(tmp_path: Path):
    _, obs = _capture_mapping(SnapshotStore(tmp_path / "s"))
    from ohmydata.providers.tushare.recipes import build_etf_index_mapping_observations

    frame = build_etf_index_mapping_observations([obs], include_vintage_fields=True).frame
    assert {"etf_code", "benchmark_index_code", "revision_status", "snapshot_identity"}.issubset(
        frame.columns
    )


def test_changed_mapping_revision_is_preserved(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req, first = _capture_mapping(store)
    scope = EtfBenchmarkConstituentScope(etf_requests=(req,))
    assemble_etf_benchmark_constituent_vintages(
        [first],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "a",
    )
    fields = req.fields
    changed = pd.DataFrame(
        [{**dict.fromkeys(fields), "ts_code": "510050.SH", "index_code": "000300.SH"}],
        columns=fields,
    )
    second = (
        _capture_mapping(
            store,
        )[1]
        if False
        else capture_tushare_result(
            store,
            req,
            TushareClient(_FakePro(changed), clock=lambda: OBSERVED).fetch_etf_basic(req),
            observed_at=OBSERVED,
        )
    )
    assemble_etf_benchmark_constituent_vintages(
        [second],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    records = [
        x
        for x in SourceFactRegistry(tmp_path / "r").observations
        if x.source_key == "etf_mapping:510050.SH"
    ]
    assert len(records) == 2 and any(r.supersedes_fact_version for r in records)
    rows = pd.read_parquet(tmp_path / "b" / "etf_benchmark_vintages.parquet")
    assert "revision_status" in rows.columns


def test_changed_weight_revision_is_preserved(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH", trade_date="20240102"
    )
    fields = req.fields
    frame = pd.DataFrame(
        [
            {
                **dict.fromkeys(fields),
                "index_code": "000016.SH",
                "trade_date": "20240102",
                "con_code": "000001.SZ",
                "weight": 50.0,
            }
        ],
        columns=fields,
    )
    first = capture_tushare_result(
        store,
        req,
        TushareClient(_FakePro(frame), clock=lambda: OBSERVED).fetch_index_weight(req),
        observed_at=OBSERVED,
    )
    scope = EtfBenchmarkConstituentScope(weight_requests=(req,))
    assemble_etf_benchmark_constituent_vintages(
        [first],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "a",
    )
    changed = frame.copy()
    changed.loc[0, "weight"] = 60.0
    second = capture_tushare_result(
        store,
        req,
        TushareClient(_FakePro(changed), clock=lambda: OBSERVED).fetch_index_weight(req),
        observed_at=OBSERVED,
    )
    assemble_etf_benchmark_constituent_vintages(
        [second],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    records = [
        x
        for x in SourceFactRegistry(tmp_path / "r").observations
        if x.source_key == "index_weight:000016.SH:20240102"
    ]
    assert len(records) == 2 and records[-1].supersedes_fact_version == records[0].fact_version
    rows = pd.read_parquet(tmp_path / "b" / "index_weight_vintages.parquet")
    assert {"revision_status", "supersedes_fact_version", "provider_first_observed_at"}.issubset(
        rows.columns
    )


def test_machine_gate_matrix_is_explicit_and_fail_closed(tmp_path: Path):
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=OBSERVED,
        output_dir=tmp_path / "g",
    )
    gate = json.loads((bundle.output_dir / "machine_gate.json").read_text())
    assert set(gate["gates"]) == {
        "APPEND_ONLY",
        "NO_FUTURE_VISIBILITY",
        "EXACT_LINEAGE",
        "UNKNOWN_FAILS_CLOSED",
        "REVISION_PRESERVED",
        "WEIGHT_INTEGRITY",
        "DETERMINISTIC_REPLAY",
        "NO_ECONOMIC_LABEL_DEPENDENCY",
        "PUBLIC_SAFETY",
    }
    assert all(set(v) == {"passes", "evidence"} for v in gate["gates"].values())
    assert gate["passes"] is False


def test_machine_gate_predicates_fail_on_forbidden_or_missing_lineage(tmp_path: Path):
    from ohmydata.providers.tushare.vintage_plane import _compute_machine_gates

    registry = SourceFactRegistry(tmp_path / "r")
    rows = [
        {
            "request_identity": "req",
            "observation_identity": None,
            "fact_version": None,
            "coverage_status": "captured",
            "resolution_status": "CURRENT_ONLY_HISTORICAL_UNPROVEN",
            "action": "forbidden",
        }
    ]
    gates = _compute_machine_gates(registry, [], rows, [], OBSERVED)
    assert gates["NO_ECONOMIC_LABEL_DEPENDENCY"]["passes"] is False
    assert gates["PUBLIC_SAFETY"]["passes"] is False
    assert gates["DETERMINISTIC_REPLAY"]["passes"] is False


def test_empty_bundle_has_explicit_parquet_schemas_and_load(tmp_path: Path):
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=OBSERVED,
        output_dir=tmp_path / "empty",
    )
    for filename, columns in PARQUET_COLUMNS.items():
        assert set(pd.read_parquet(bundle.output_dir / filename).columns) >= set(columns)
    loaded = EtfBenchmarkConstituentBundle.load(bundle.output_dir)
    assert loaded.verify()


def test_reverse_observation_order_is_byte_identical(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    req, obs = _capture_mapping(store)
    scope = EtfBenchmarkConstituentScope(etf_requests=(req,))
    first = assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=SourceFactRegistry(tmp_path / "r1"),
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "a",
    )
    second = assemble_etf_benchmark_constituent_vintages(
        list(reversed([obs])),
        store=store,
        registry=SourceFactRegistry(tmp_path / "r2"),
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    names = [
        "etf_benchmark_vintages.parquet",
        "index_weight_vintages.parquet",
        "index_weight_constituents.parquet",
        "source_resolution_manifest.parquet",
        "coverage_and_gap_report.json",
    ]
    assert first.bundle_identity == second.bundle_identity
    assert all(
        (first.output_dir / n).read_bytes() == (second.output_dir / n).read_bytes() for n in names
    )


def test_cutoff_matrix_and_missing_scope_parity(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req, obs = _capture_mapping(store)
    from ohmydata.providers.tushare.vintage_plane import (
        CoverageItemResult,
        CoverageItemStatus,
        derive_cutoff_status,
    )

    assert (
        derive_cutoff_status(
            CoverageItemResult(req.spec.request_identity, "etf_basic", CoverageItemStatus.CAPTURED),
            obs,
            OBSERVED.replace(year=2025),
        ).value
        == "NOT_YET_OBSERVED"
    )
    assert (
        derive_cutoff_status(
            CoverageItemResult(req.spec.request_identity, "etf_basic", CoverageItemStatus.CAPTURED),
            obs,
            OBSERVED,
        ).value
        == "CURRENT_ONLY_HISTORICAL_UNPROVEN"
    )
    assert (
        derive_cutoff_status(
            CoverageItemResult(req.spec.request_identity, "etf_basic", CoverageItemStatus.CAPTURED),
            obs,
            OBSERVED.replace(year=2027),
        ).value
        == "CURRENT_ONLY_HISTORICAL_UNPROVEN"
    )
    assert (
        derive_cutoff_status(
            CoverageItemResult("x", "etf_basic", CoverageItemStatus.UNSUPPORTED), None, OBSERVED
        ).value
        == "UNKNOWN_FAIL_CLOSED"
    )
    assert (
        derive_cutoff_status(
            CoverageItemResult("x", "etf_basic", CoverageItemStatus.FAILURE), None, OBSERVED
        ).value
        == "UNKNOWN_FAIL_CLOSED"
    )
    missing_scope = EtfBenchmarkConstituentScope(
        etf_requests=(req, EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="159001.SZ"))
    )
    bundle = assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=registry,
        scope=missing_scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "m",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    assert any(x["resolution_status"] == "UNKNOWN_FAIL_CLOSED" for x in report["items"])


def test_weight_incomplete_status_parity(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH", trade_date="20240102"
    )
    fields = req.fields
    frame = pd.DataFrame(
        [
            {
                **dict.fromkeys(fields),
                "index_code": "000016.SH",
                "trade_date": "20240102",
                "con_code": "000001.SZ",
                "weight": 100.0,
            }
        ],
        columns=fields,
    )
    obs = capture_tushare_result(
        store,
        req,
        TushareClient(_FakePro(frame), clock=lambda: OBSERVED).fetch_index_weight(req),
        observed_at=OBSERVED,
    )
    bundle = assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=registry,
        scope=EtfBenchmarkConstituentScope(weight_requests=(req,)),
        cutoff=OBSERVED,
        output_dir=tmp_path / "w",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    assert report["items"][0]["resolution_status"] == "INCOMPLETE_SOURCE_OBSERVATION"


def test_producer_naive_observed_at_makes_zero_calls(tmp_path: Path):
    class Recorder:
        def __init__(self):
            self.calls = []

        def fetch_etf_basic(self, req):
            self.calls.append(req)
            raise AssertionError("must not call")

    recorder = Recorder()
    req = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH")
    with pytest.raises(ValueError):
        produce_etf_benchmark_constituent_vintages(
            recorder,
            store=SnapshotStore(tmp_path / "s"),
            registry=SourceFactRegistry(tmp_path / "r"),
            observed_at=datetime(2026, 1, 1),  # noqa: DTZ001
            cutoff=OBSERVED,
            scope=EtfBenchmarkConstituentScope(etf_requests=(req,)),
            output_dir=tmp_path / "p",
        )
    assert recorder.calls == []


def test_producer_success_persists_declared_mapping(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req, _ = _capture_mapping(store)
    fields = req.fields
    frame = pd.DataFrame(
        [{**dict.fromkeys(fields), "ts_code": "510050.SH", "index_code": "000016.SH"}],
        columns=fields,
    )
    client = TushareClient(_FakePro(frame), clock=lambda: OBSERVED)
    bundle = produce_etf_benchmark_constituent_vintages(
        client,
        store=store,
        registry=registry,
        observed_at=OBSERVED,
        cutoff=OBSERVED,
        scope=EtfBenchmarkConstituentScope(etf_requests=(req,)),
        output_dir=tmp_path / "p",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    assert report["items"][0]["coverage_status"] == "captured"


def test_producer_empty_persists_missing(tmp_path: Path):
    req = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH")
    frame = pd.DataFrame(columns=req.fields)
    client = TushareClient(_FakePro(frame), clock=lambda: OBSERVED)
    bundle = produce_etf_benchmark_constituent_vintages(
        client,
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        observed_at=OBSERVED,
        cutoff=OBSERVED,
        scope=EtfBenchmarkConstituentScope(etf_requests=(req,)),
        output_dir=tmp_path / "p",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    assert report["items"][0]["coverage_status"] == "missing"


def test_producer_malformed_is_failure(tmp_path: Path):
    req = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH")
    frame = pd.DataFrame([{"bad": "x"}])
    client = TushareClient(_FakePro(frame), clock=lambda: OBSERVED)
    bundle = produce_etf_benchmark_constituent_vintages(
        client,
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        observed_at=OBSERVED,
        cutoff=OBSERVED,
        scope=EtfBenchmarkConstituentScope(etf_requests=(req,)),
        output_dir=tmp_path / "p",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    assert report["items"][0]["coverage_status"] == "failure"


def test_producer_failure_continues_to_second_request(tmp_path: Path):
    req1 = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH")
    req2 = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="159001.SZ")
    fields = req2.fields
    frame = pd.DataFrame(
        [{**dict.fromkeys(fields), "ts_code": "159001.SZ", "index_code": "000300.SH"}],
        columns=fields,
    )
    calls = []

    class Client:
        def fetch_etf_basic(self, req):
            calls.append(req.ts_code)
            if req.ts_code == "510050.SH":
                raise PermanentProviderError("boom")
            return TushareClient(_FakePro(frame), clock=lambda: OBSERVED).fetch_etf_basic(req)

    bundle = produce_etf_benchmark_constituent_vintages(
        Client(),
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        observed_at=OBSERVED,
        cutoff=OBSERVED,
        scope=EtfBenchmarkConstituentScope(etf_requests=(req1, req2)),
        output_dir=tmp_path / "p",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    assert calls == ["510050.SH", "159001.SZ"] and {
        x["coverage_status"] for x in report["items"]
    } == {"failure", "captured"}


@pytest.mark.parametrize(
    "gate_name",
    [
        "APPEND_ONLY",
        "NO_FUTURE_VISIBILITY",
        "EXACT_LINEAGE",
        "UNKNOWN_FAILS_CLOSED",
        "REVISION_PRESERVED",
        "WEIGHT_INTEGRITY",
        "DETERMINISTIC_REPLAY",
        "NO_ECONOMIC_LABEL_DEPENDENCY",
        "PUBLIC_SAFETY",
    ],
)
def test_gate_forgery_is_rejected(tmp_path: Path, gate_name: str):
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    gate = json.loads((bundle.output_dir / "machine_gate.json").read_text())
    gate["gates"][gate_name]["passes"] = not gate["gates"][gate_name]["passes"]
    (bundle.output_dir / "machine_gate.json").write_text(json.dumps(gate))
    with pytest.raises(ValueError):
        EtfBenchmarkConstituentBundle.load(bundle.output_dir).verify()


def test_consistent_resolution_tamper_rejected(tmp_path: Path):
    bundle = assemble_etf_benchmark_constituent_vintages(
        [],
        store=SnapshotStore(tmp_path / "s"),
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(),
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    report["items"].append(
        {
            "request_identity": "x",
            "endpoint": "etf_basic",
            "coverage_status": "captured",
            "resolution_status": "VERIFIED_SOURCE_OBSERVATION",
        }
    )
    (bundle.output_dir / "coverage_and_gap_report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        EtfBenchmarkConstituentBundle.load(bundle.output_dir).verify()


def test_two_etf_unrelated_revision_preserves_statuses(tmp_path: Path):
    store = SnapshotStore(tmp_path / "s")
    registry = SourceFactRegistry(tmp_path / "r")
    req = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH")
    fields = req.fields
    first = pd.DataFrame(
        [
            {
                **dict.fromkeys(fields),
                "ts_code": "510050.SH",
                "index_code": "000016.SH",
                "list_status": "L",
            },
            {
                **dict.fromkeys(fields),
                "ts_code": "510300.SH",
                "index_code": "000016.SH",
                "list_status": "L",
            },
        ],
        columns=fields,
    )
    first_obs = capture_tushare_result(
        store,
        req,
        TushareClient(_FakePro(first), clock=lambda: OBSERVED).fetch_etf_basic(req),
        observed_at=OBSERVED,
    )
    scope = EtfBenchmarkConstituentScope(etf_requests=(req,))
    assemble_etf_benchmark_constituent_vintages(
        [first_obs],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=OBSERVED,
        output_dir=tmp_path / "a",
    )
    changed = first.copy()
    changed.loc[changed.ts_code == "510050.SH", "list_status"] = "D"
    later = datetime(2026, 1, 2, tzinfo=UTC)
    second_obs = capture_tushare_result(
        store,
        req,
        TushareClient(_FakePro(changed), clock=lambda: later).fetch_etf_basic(req),
        observed_at=later,
    )
    bundle = assemble_etf_benchmark_constituent_vintages(
        [second_obs],
        store=store,
        registry=registry,
        scope=scope,
        cutoff=later,
        output_dir=tmp_path / "b",
    )
    grouped = {}
    for record in registry.observations:
        grouped.setdefault(record.source_key, []).append(record)
    assert all(len(rows) == 2 for rows in grouped.values())
    assert grouped["etf_mapping:510050.SH"][-1].revision_status == "REVISED_FROM_PREVIOUS"
    assert (
        grouped["etf_mapping:510050.SH"][-1].supersedes_fact_version
        == grouped["etf_mapping:510050.SH"][0].fact_version
    )
    assert grouped["etf_mapping:510300.SH"][-1].revision_status == "UNCHANGED_FROM_PREVIOUS"
    assert grouped["etf_mapping:510300.SH"][-1].supersedes_fact_version is None
    rows = pd.read_parquet(bundle.output_dir / "etf_benchmark_vintages.parquet")
    assert set(rows["revision_status"]) == {"REVISED_FROM_PREVIOUS", "UNCHANGED_FROM_PREVIOUS"}
    assert bundle.verify()


def test_recompute_gate_rejects_future_anchor_tamper():
    cutoff = OBSERVED
    report = {
        "items": [
            {
                "request_identity": "r",
                "coverage_status": "captured",
                "resolution_status": "CURRENT_ONLY_HISTORICAL_UNPROVEN",
                "provider_first_observed_at": (cutoff + timedelta(days=1)).isoformat(),
                "cutoff": cutoff.isoformat(),
            }
        ]
    }
    gates = recompute_artifact_gate_passes(
        {"source_resolution_manifest": report["items"]}, report, {"gates": {}}
    )
    assert gates["NO_FUTURE_VISIBILITY"]["passes"] is False


def test_recompute_gate_rejects_invalid_weight_diagnostics():
    row = {
        "request_identity": "r",
        "retrieval_status": "RETRIEVAL_COMPLETENESS_UNPROVEN",
        "economic_completeness_status": "ECONOMIC_COMPLETENESS_UNPROVEN",
        "observed_component_count": -1,
        "expected_component_count": None,
        "duplicate_component_rows": 0,
        "null_weight_count": 0,
        "non_finite_weight_count": 0,
        "out_of_range_weight_count": 0,
    }
    report = {
        "items": [
            {
                "request_identity": "r",
                "coverage_status": "captured",
                "resolution_status": "INCOMPLETE_SOURCE_OBSERVATION",
            }
        ]
    }
    gates = recompute_artifact_gate_passes({"index_weight_vintages": [row]}, report, {"gates": {}})
    assert gates["WEIGHT_INTEGRITY"]["passes"] is False


def test_weight_after_cutoff_is_not_yet_observed(tmp_path: Path):
    req = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH", trade_date="20240102"
    )
    frame = pd.DataFrame(
        [
            {
                **dict.fromkeys(req.fields),
                "index_code": "000016.SH",
                "trade_date": "20240102",
                "con_code": "000001.SZ",
                "weight": 100.0,
            }
        ],
        columns=req.fields,
    )
    store = SnapshotStore(tmp_path / "s")
    obs = capture_tushare_result(
        store,
        req,
        TushareClient(
            _FakePro(frame), clock=lambda: datetime(2026, 2, 1, tzinfo=UTC)
        ).fetch_index_weight(req),
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    bundle = assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(weight_requests=(req,)),
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    report = json.loads((bundle.output_dir / "coverage_and_gap_report.json").read_text())
    assert report["items"][0]["resolution_status"] == "NOT_YET_OBSERVED"
    rows = pd.read_parquet(bundle.output_dir / "source_resolution_manifest.parquet")
    assert "NOT_YET_OBSERVED" in str(rows.iloc[0].get("quality_flags"))


def test_valid_incomplete_weight_passes_weight_integrity(tmp_path: Path):
    req = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="000016.SH", trade_date="20240102"
    )
    frame = pd.DataFrame(
        [
            {
                **dict.fromkeys(req.fields),
                "index_code": "000016.SH",
                "trade_date": "20240102",
                "con_code": "000001.SZ",
                "weight": 100.0,
            }
        ],
        columns=req.fields,
    )
    store = SnapshotStore(tmp_path / "s")
    obs = capture_tushare_result(
        store,
        req,
        TushareClient(_FakePro(frame), clock=lambda: OBSERVED).fetch_index_weight(req),
        observed_at=OBSERVED,
    )
    bundle = assemble_etf_benchmark_constituent_vintages(
        [obs],
        store=store,
        registry=SourceFactRegistry(tmp_path / "r"),
        scope=EtfBenchmarkConstituentScope(weight_requests=(req,)),
        cutoff=OBSERVED,
        output_dir=tmp_path / "b",
    )
    gate = json.loads((bundle.output_dir / "machine_gate.json").read_text())
    assert gate["gates"]["WEIGHT_INTEGRITY"]["passes"] is True
