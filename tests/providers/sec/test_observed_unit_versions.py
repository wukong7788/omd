"""Offline acceptance coverage for observed XBRL parser-unit versions."""

import json
import socket
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ohmydata.providers.sec.observed_xbrl_financials as observed_financials
from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.providers.sec import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    SecQualityStatus,
    load_sec_observed_financial_bundle,
    select_sec_observed_financial_productions,
    write_sec_observed_financial_bundle,
)
from ohmydata.providers.sec.observed_xbrl_financials import (
    _restore_sec_observed_financial_production,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_observed_xbrl_financials import _produce, _replay_policy, _setup
from test_sgml_financials import _raw, _request

V1 = "sec-observed-xbrl-financial-parser-v1-edgartools-5.56.0"
V2 = "sec-observed-xbrl-financial-parser-v2-edgartools-5.56.0"

pytest.importorskip("edgar")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _compound_raw() -> bytes:
    return _raw().replace(
        b'<unit id="usd"><measure>iso4217:USD</measure></unit>',
        b'<unit id="usd"><divide><unitNumerator><measure>iso4217:USD</measure>'
        b"</unitNumerator><unitDenominator><measure>shares</measure>"
        b"</unitDenominator></divide></unit>",
    )


def _produce_raw(root, raw, *, parser_version=V2):
    source, source_ref, packages, package_ref, package_bytes = _setup(
        root,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
        raw=raw,
    )
    output = SnapshotStore(root / "output")
    production = observed_financials.produce_sec_financials_from_observed_xbrl_package(
        source_store=source,
        source_observation=source_ref,
        package_store=packages,
        package_observation=package_ref,
        output_store=output,
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        parser_version=parser_version,
    )
    return production, source, source_ref, packages, package_ref, package_bytes, output


def _records(production):
    policy = _replay_policy(production, production.produced_at + timedelta(minutes=5))
    quality = SecObservedFinancialQualityRecord(
        production.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        production.produced_at,
    )
    commit = SecObservedFinancialConsumerCommit(
        production.production_identity,
        quality.quality_record_id,
        policy.consumer_dataset_identity,
        production.produced_at,
    )
    return policy, quality, commit


def _receipts(*closures):
    result = {}
    for production, source, source_ref, packages, package_ref, _, output in closures:
        for store, observation in (
            (source, source_ref),
            (packages, package_ref),
            (output, production.output_observation),
        ):
            result[observation.observation_identity] = (store, observation)
    return result


def test_default_v2_corrects_compound_unit_but_v1_retains_native_row_fields(tmp_path):
    v1, *_ = _produce_raw(tmp_path / "v1", _compound_raw(), parser_version=V1)
    v2, *_ = _produce_raw(tmp_path / "v2", _compound_raw())

    assert v2.parser_version == V2
    assert v1.parser_version == V1
    assert v2.vintage.rows[0].unit == (
        '{"denominator":["shares"],"numerator":["iso4217:USD"],"type":"divide"}'
    )
    assert v1.vintage.rows[0].unit != v2.vintage.rows[0].unit
    assert replace(v1.vintage.rows[0], unit=None) == replace(v2.vintage.rows[0], unit=None)
    assert v1.vintage.rows[0].currency == "USD"
    assert v2.vintage.rows[0].currency is None


def test_unknown_parser_version_fails_before_dependency_replay_or_output_write(
    tmp_path, monkeypatch
):
    source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "base")[1:]
    monkeypatch.setattr(SnapshotStore, "replay_observation", lambda *_: pytest.fail("replayed"))
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("wrote"))

    with pytest.raises(ValueError, match="parser version"):
        observed_financials.produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
            parser_version="not-supported",
        )


def test_replaced_and_tampered_parser_seals_are_rejected(tmp_path):
    production, *_ = _produce(tmp_path)
    with pytest.raises(ValueError, match="output receipt"):
        replace(production, _parser_version=V1)
    object.__setattr__(production, "_parser_version", V1)
    with pytest.raises(ValueError, match="output receipt"):
        production.__post_init__()


@pytest.mark.parametrize("selector", ["absent", "unknown", "duplicate"])
def test_retained_output_selector_is_rejected_before_source_parsing(
    tmp_path, monkeypatch, selector
):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "base")
    payload = output.replay_observation(production.output_observation).payload
    if selector == "absent":
        decoded = json.loads(payload)
        del decoded["parser_version"]
        altered = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    elif selector == "unknown":
        decoded = json.loads(payload)
        decoded["parser_version"] = "unknown"
        altered = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    else:
        altered = payload.replace(
            b'"parser_version":"' + V2.encode() + b'"',
            b'"parser_version":"' + V2.encode() + b'","parser_version":"' + V1.encode() + b'"',
        )
    altered_store = SnapshotStore(tmp_path / selector / "output")
    altered_ref = altered_store.observe(
        RequestSpec(
            "sec",
            "financial-observed-rows",
            {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"},
        ),
        altered,
        production.produced_at,
        "sec-financial-observed-rows-v1",
    )
    monkeypatch.setattr(source, "replay_observation", lambda *_: pytest.fail("source parsed"))

    with pytest.raises(ValueError, match="retained parser version"):
        _restore_sec_observed_financial_production(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=altered_store,
            output_observation=altered_ref,
            request=_request(),
            produced_at=production.produced_at,
        )


def test_mixed_version_bundle_reloads_without_writes_and_isolates_lifecycle(tmp_path, monkeypatch):
    v1_closure = _produce_raw(tmp_path / "v1", _compound_raw(), parser_version=V1)
    v2_closure = _produce_raw(tmp_path / "v2", _compound_raw())
    v1, v2 = v1_closure[0], v2_closure[0]
    v1_policy, v1_quality, v1_commit = _records(v1)
    v2_policy, v2_quality, v2_commit = _records(v2)
    receipts = _receipts(v1_closure, v2_closure)
    bundle_store = SnapshotStore(tmp_path / "bundle")
    bundle_ref = write_sec_observed_financial_bundle(
        store=bundle_store,
        batch_identity="mixed-unit-versions",
        productions=(v1, v2),
        quality_records=(v1_quality, v2_quality),
        consumer_commits=(v1_commit, v2_commit),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
    )
    monkeypatch.setattr(SnapshotStore, "write", lambda *_: pytest.fail("loader wrote"))
    loaded = load_sec_observed_financial_bundle(
        store=bundle_store, bundle_ref=bundle_ref, resolve_observation=receipts.__getitem__
    )

    assert {item.parser_version for item in loaded.productions} == {V1, V2}
    assert (
        select_sec_observed_financial_productions(
            loaded.productions, loaded.quality_records, loaded.consumer_commits, v1_policy
        )[0].production.production_identity
        == v1.production_identity
    )
    assert (
        select_sec_observed_financial_productions(
            loaded.productions, loaded.quality_records, loaded.consumer_commits, v2_policy
        )[0].production.production_identity
        == v2.production_identity
    )
    assert not select_sec_observed_financial_productions(
        (v2,), (v1_quality,), (v1_commit,), v2_policy
    )


@pytest.mark.parametrize("case", ["duplicate", "unused-malformed", "missing-reference"])
def test_invalid_unit_production_never_writes_output(tmp_path, case):
    raw = _raw()
    if case == "duplicate":
        raw = raw.replace(
            b"</xbrl>", b'<unit id="usd"><measure>iso4217:USD</measure></unit></xbrl>'
        )
    elif case == "unused-malformed":
        raw = raw.replace(b"</xbrl>", b'<unit id="unused"><divide/></unit></xbrl>')
    else:
        raw = raw.replace(b'unitRef="usd"', b'unitRef="missing"')
    assert raw != _raw()
    with pytest.raises(ValueError):
        _produce_raw(tmp_path, raw)
    assert not list((tmp_path / "output").rglob("response.bin"))
    assert not list((tmp_path / "output").rglob("observation.json"))
