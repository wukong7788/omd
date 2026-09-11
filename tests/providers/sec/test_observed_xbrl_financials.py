"""Offline regressions for locally observed SEC XBRL package production."""

import json
import socket
import sys
from base64 import b64encode
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotIntegrityError, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import (
    SecCompanyFinancialVintage,
    SecObservedFinancialReplayPolicy,
    SecPitMode,
    SecPitPolicy,
    SecXbrlPackageComponents,
    decode_sec_observed_xbrl_package,
    produce_sec_financials_from_observed_xbrl_package,
    produce_sec_financials_from_sgml,
    select_sec_financial_versions,
    serialize_sec_observed_xbrl_package,
)
from ohmydata.providers.sec._observed_xbrl_units import decode_raw_units
from ohmydata.providers.sec._pit_projection import _decode_projection
from ohmydata.providers.sec.observed_xbrl_financials import (
    _restore_sec_observed_financial_production,
)
from ohmydata.providers.sec.sgml_financials import _documents

sys.path.insert(0, str(Path(__file__).parent))
from test_sgml_financials import _observation, _raw, _request

pytest.importorskip("edgar")


def test_raw_unit_decoder_preserves_compound_dimensions_and_namespace_scope():
    raw = b"""<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:i="http://www.xbrl.org/2003/iso4217"><unit id="usd"><measure>i:USD</measure></unit><unit id="eps"><divide><unitNumerator><measure>i:USD</measure><measure>i:USD</measure></unitNumerator><unitDenominator><measure>shares</measure></unitDenominator></divide></unit><unit id="product"><measure>pure</measure><measure>shares</measure></unit><unit id="other" xmlns:i="urn:other"><measure>i:USD</measure></unit></xbrl>"""
    assert decode_raw_units(raw, max_elements=100, max_depth=20) == {
        "usd": "iso4217:USD",
        "eps": '{"denominator":["shares"],"numerator":["iso4217:USD","iso4217:USD"],"type":"divide"}',
        "product": '{"measures":["pure","shares"],"type":"product"}',
        "other": "{urn:other}USD",
    }


@pytest.mark.parametrize(
    "raw",
    [
        b'<xbrl xmlns="http://www.xbrl.org/2003/instance"><unit id="x"><measure/></unit></xbrl>',
        b'<xbrl xmlns="http://www.xbrl.org/2003/instance"><unit id="x"><divide/></unit></xbrl>',
        b'<xbrl xmlns="http://www.xbrl.org/2003/instance"><unit id="x"><measure>shares</measure></unit><unit id="x"><measure>shares</measure></unit></xbrl>',
    ],
)
def test_raw_unit_decoder_rejects_invalid_definitions(raw):
    with pytest.raises(ValueError):
        decode_raw_units(raw, max_elements=100, max_depth=20)


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _components(raw=None):
    documents = _documents((raw or _raw()).decode())
    return SecXbrlPackageComponents(
        documents["EX-101.SCH"].encode(),
        documents["EX-101.PRE"].encode(),
        documents["EX-101.LAB"].encode(),
        documents["EX-101.INS"].encode(),
    )


def _setup(
    root, *, source_time, package_time, serialization="sec-observed-xbrl-package-v1", raw=None
):
    source, source_ref = _observation(root / "source", raw or _raw(), source_time)
    package_bytes = serialize_sec_observed_xbrl_package(
        sgml_observation=source_ref,
        cik="0000000001",
        accession_number="0000000001-24-000001",
        form="10-Q",
        components=_components(raw),
    )
    packages = SnapshotStore(root / "packages")
    package_ref = packages.observe(
        RequestSpec(
            "sec",
            "company-filing-observed-xbrl-package",
            {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"},
        ),
        package_bytes,
        package_time,
        serialization,
    )
    return source, source_ref, packages, package_ref, package_bytes


def _produce(
    root,
    *,
    source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
    package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    produced=datetime(2024, 5, 1, 23, tzinfo=UTC),
    **kwargs,
):
    source, source_ref, packages, package_ref, package_bytes = _setup(
        root, source_time=source_time, package_time=package_time
    )
    output = SnapshotStore(root / "output")
    result = produce_sec_financials_from_observed_xbrl_package(
        source_store=source,
        source_observation=source_ref,
        package_store=packages,
        package_observation=package_ref,
        output_store=output,
        request=_request(),
        produced_at=produced,
        **kwargs,
    )
    return result, source, source_ref, packages, package_ref, package_bytes, output


def _replay_policy(result, cutoff):
    return SecObservedFinancialReplayPolicy(
        result.output_schema_version,
        result.parser_version,
        result.configuration_version,
        result.configuration_identity,
        "observed-quality-v1",
        "a" * 64,
        cutoff,
    )


@pytest.mark.parametrize(
    ("source_time", "package_time"),
    [
        (datetime(2024, 5, 1, 22, tzinfo=UTC), datetime(2024, 5, 1, 22, 30, tzinfo=UTC)),
        (datetime(2024, 5, 1, 22, 30, tzinfo=UTC), datetime(2024, 5, 1, 22, tzinfo=UTC)),
    ],
)
def test_real_parser_binds_receipts_and_timing_orders(tmp_path, source_time, package_time):
    result, _, source_ref, packages, package_ref, package_bytes, output = _produce(
        tmp_path, source_time=source_time, package_time=package_time
    )
    assert packages.replay_observation(package_ref).payload == package_bytes
    assert (
        decode_sec_observed_xbrl_package(package_bytes).components.instance
        == _components().instance
    )
    assert result.evidence.known_by_at == max(source_time, package_time)
    assert result.vintage.accepted_at == datetime(2024, 5, 1, 21, tzinfo=UTC)
    assert result.vintage.rows[0].value_native == "123"
    assert result.evidence.source_observation == source_ref
    payload = output.replay_observation(result.output_observation).payload
    decoded = json.loads(payload)
    assert decoded["schema"] == "sec-financial-observed-rows-v1"
    assert decoded["source_receipt"]["fact_version"] == source_ref.fact_version
    assert "source_available_at" not in payload.decode()


def test_fresh_stores_rebuild_identical_result_bytes_and_identity(tmp_path):
    first, *_, first_output = _produce(tmp_path / "first")
    second, *_, second_output = _produce(tmp_path / "second")
    assert first.vintage.vintage_identity == second.vintage.vintage_identity
    assert first.output_observation.fact_version == second.output_observation.fact_version
    assert (
        first_output.replay_observation(first.output_observation).payload
        == second_output.replay_observation(second.output_observation).payload
    )


def test_parser_versions_are_explicit_and_isolated(tmp_path):
    v1, *_ = _produce(
        tmp_path / "v1", parser_version="sec-observed-xbrl-financial-parser-v1-edgartools-5.56.0"
    )
    v2, *_ = _produce(tmp_path / "v2")
    assert v1.parser_version.endswith("v1-edgartools-5.56.0")
    assert v2.parser_version.endswith("v2-edgartools-5.56.0")
    assert v1.configuration_identity != v2.configuration_identity
    assert v1.production_identity != v2.production_identity
    with pytest.raises(ValueError, match="parser version"):
        _produce(tmp_path / "bad", parser_version="unknown")


def test_restore_uses_retained_v1_selector_without_writes(tmp_path, monkeypatch):
    result, source, source_ref, packages, package_ref, _, output = _produce(
        tmp_path, parser_version="sec-observed-xbrl-financial-parser-v1-edgartools-5.56.0"
    )
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("loader wrote"))
    restored = _restore_sec_observed_financial_production(
        source_store=source,
        source_observation=source_ref,
        package_store=packages,
        package_observation=package_ref,
        output_store=output,
        output_observation=result.output_observation,
        request=_request(),
        produced_at=result.produced_at,
    )
    assert restored.parser_version == result.parser_version
    assert restored.production_identity == result.production_identity


def test_native_decimal_unit_period_and_dimension_match_sgml_parser(tmp_path):
    raw = _raw(dimension=True)
    source, source_ref, packages, package_ref, _ = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
        raw=raw,
    )
    request = replace(_request(), include_dimensions=True)
    observed = produce_sec_financials_from_observed_xbrl_package(
        source_store=source,
        source_observation=source_ref,
        package_store=packages,
        package_observation=package_ref,
        output_store=SnapshotStore(tmp_path / "observed-output"),
        request=request,
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )
    legacy = produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=source_ref,
        projection_store=SnapshotStore(tmp_path / "legacy-output"),
        request=request,
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )
    assert observed.vintage.rows == legacy.vintage.rows
    row = observed.vintage.rows[0]
    assert row.value is not None and row.value.as_tuple().digits
    assert row.unit == "iso4217:USD"
    assert row.period_start is not None and row.period_end is not None
    assert any(item.dimension is not None for item in observed.vintage.rows)


@pytest.mark.parametrize(
    "limit", [{"max_component_bytes": 1}, {"max_xml_elements": 1}, {"max_rows": 0}]
)
def test_stricter_limits_fail_without_output_write(tmp_path, limit):
    source, source_ref, packages, package_ref, _ = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    output = SnapshotStore(tmp_path / "output")
    with pytest.raises(ValueError):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
            **limit,
        )
    assert not list(output.root.rglob("response.bin"))


@pytest.mark.parametrize(
    "limit",
    [
        {"max_raw_bytes": 1},
        {"max_package_bytes": 1},
        {"max_result_bytes": 1},
        {"max_xml_depth": 1},
    ],
)
def test_positive_byte_and_depth_limits_fail_without_output_write(tmp_path, limit):
    source, source_ref, packages, package_ref, _ = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    output = SnapshotStore(tmp_path / "output")
    with pytest.raises((SnapshotIntegrityError, ValueError)):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
            **limit,
        )
    assert not list(output.root.rglob("response.bin"))


def test_positive_row_limit_on_real_dimension_fixture_fails_without_output_write(tmp_path):
    raw = _raw(dimension=True)
    raw = (
        raw.replace(
            b'<xs:element name="Revenues" id="Revenues" type="xs:decimal" xbrli:periodType="duration"/>',
            b'<xs:element name="Revenues" id="Revenues" type="xs:decimal" xbrli:periodType="duration"/>'
            b'<xs:element name="GrossProfit" id="GrossProfit" type="xs:decimal" xbrli:periodType="duration"/>',
        )
        .replace(
            b'<link:loc xlink:label="revenue" xlink:href="fake.xsd#us-gaap_Revenues"/>',
            b'<link:loc xlink:label="revenue" xlink:href="fake.xsd#us-gaap_Revenues"/>'
            b'<link:loc xlink:label="gross" xlink:href="fake.xsd#us-gaap_GrossProfit"/>',
        )
        .replace(
            b'<link:presentationArc xlink:from="root" xlink:to="revenue" order="1"/>',
            b'<link:presentationArc xlink:from="root" xlink:to="revenue" order="1"/>'
            b'<link:presentationArc xlink:from="root" xlink:to="gross" order="2"/>',
        )
        .replace(
            b'<us-gaap:Revenues contextRef="c1" unitRef="usd" decimals="0">123</us-gaap:Revenues>',
            b'<us-gaap:Revenues contextRef="c1" unitRef="usd" decimals="0">123</us-gaap:Revenues>'
            b'<us-gaap:GrossProfit contextRef="c1" unitRef="usd" decimals="0">100</us-gaap:GrossProfit>',
        )
    )
    source, source_ref, packages, package_ref, _ = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
        raw=raw,
    )
    output = SnapshotStore(tmp_path / "output")
    with pytest.raises(ValueError, match="row limit"):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=replace(_request(), include_dimensions=True),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
            max_rows=1,
        )
    assert not list(output.root.rglob("response.bin"))


def test_reopened_retained_stores_reuse_exact_existing_output(tmp_path):
    first, _, source_ref, _, package_ref, _, output = _produce(tmp_path)
    before = {
        path.relative_to(output.root): path.read_bytes()
        for path in output.root.rglob("*")
        if path.is_file()
    }
    second = produce_sec_financials_from_observed_xbrl_package(
        source_store=SnapshotStore(tmp_path / "source" / "raw"),
        source_observation=source_ref,
        package_store=SnapshotStore(tmp_path / "packages"),
        package_observation=package_ref,
        output_store=SnapshotStore(tmp_path / "output"),
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )
    after = {
        path.relative_to(output.root): path.read_bytes()
        for path in output.root.rglob("*")
        if path.is_file()
    }
    assert second.output_observation == first.output_observation
    assert after == before
    with pytest.raises(ValueError, match="result exceeds limit"):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=SnapshotStore(tmp_path / "source" / "raw"),
            source_observation=source_ref,
            package_store=SnapshotStore(tmp_path / "packages"),
            package_observation=package_ref,
            output_store=SnapshotStore(tmp_path / "output"),
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
            max_result_bytes=1,
        )
    unchanged_after_failure = {
        path.relative_to(output.root): path.read_bytes()
        for path in output.root.rglob("*")
        if path.is_file()
    }
    assert unchanged_after_failure == before


def test_legacy_projection_decoder_and_vintage_type_reject_observed_result(tmp_path):
    result, *_, output = _produce(tmp_path)
    payload = output.replay_observation(result.output_observation).payload
    assert not isinstance(result.vintage, SecCompanyFinancialVintage)
    with pytest.raises(ValueError, match="typed-row projection"):
        _decode_projection(payload)
    with pytest.raises(TypeError, match="SecNormalizedFinancialFactVersion"):
        select_sec_financial_versions(
            (result.vintage,),
            mode=SecPitMode.MARKET_KNOWN,
            knowledge_cutoff=datetime(2024, 5, 2, tzinfo=UTC),
            policy=SecPitPolicy("v1", "v1", "v1", "0" * 64, "v1", datetime(2024, 5, 2, tzinfo=UTC)),
            quality_records=(),
        )


def test_rejects_wrong_serialization_and_causal_time_without_output(tmp_path):
    source, source_ref, packages, package_ref, _ = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
        serialization="wrong-v1",
    )
    output = SnapshotStore(tmp_path / "output")
    with pytest.raises(ValueError, match="observed XBRL package"):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )
    assert not list(output.root.rglob("response.bin"))


@pytest.mark.parametrize(
    ("raw", "produced", "message"),
    [
        (_raw(acceptance="20240501190000"), datetime(2024, 5, 2, tzinfo=UTC), "not causal"),
        (_raw(), datetime(2024, 5, 1, 22, 15, tzinfo=UTC), "not causal"),
    ],
)
def test_real_causal_failures_leave_output_empty(tmp_path, raw, produced, message):
    source, source_ref, packages, package_ref, _ = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
        raw=raw,
    )
    output = SnapshotStore(tmp_path / "output")
    with pytest.raises(ValueError, match=message):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=_request(),
            produced_at=produced,
        )
    assert not list(output.root.rglob("response.bin"))


def test_rejects_wrong_request_and_observation_binding_without_output(tmp_path):
    source, source_ref, packages, package_ref, _ = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    alternate = source.observe(
        RequestSpec(
            "sec",
            "company-filing-sgml",
            {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"},
        ),
        _raw(),
        datetime(2024, 5, 1, 22, 15, tzinfo=UTC),
        "sec-filing-sgml-v1",
    )
    output = SnapshotStore(tmp_path / "output")
    with pytest.raises(ValueError, match="does not bind"):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=alternate,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )
    with pytest.raises(Exception, match="request mismatch"):
        produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=source_ref,
            package_store=packages,
            package_observation=package_ref,
            output_store=output,
            request=replace(_request(), form="10-K"),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )
    assert not list(output.root.rglob("response.bin"))


def test_evidence_replace_cannot_bypass_replay_binding(tmp_path):
    result, *_ = _produce(tmp_path)
    with pytest.raises(ValueError, match="binding mismatch"):
        replace(result.evidence, accepted_at=datetime(2024, 5, 1, 20, tzinfo=UTC))
    with pytest.raises(ValueError, match="binding mismatch"):
        replace(result.evidence, source_observation=result.evidence.package_observation)
    replaced_receipt = replace(
        result.evidence.source_observation,
        snapshot_fetched_at=datetime(2024, 5, 1, 21, 30, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="binding mismatch"):
        replace(result.evidence, source_observation=replaced_receipt)
    with pytest.raises(ValueError, match="binding mismatch"):
        replace(
            result.evidence,
            source_observation=replace(result.evidence.source_observation, provider="other"),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", Path("/tmp/changed-observation.json")),
        ("observation_identity", "1" * 64),
        ("snapshot_identity", "2" * 64),
        ("fact_version", "3" * 64),
        ("mode", SnapshotMode.FROZEN),
        ("provider", "other"),
        ("endpoint", "other-endpoint"),
        ("request_identity", "4" * 64),
        ("response_sha256", "5" * 64),
        ("serialization_identifier", "other-v1"),
        ("snapshot_fetched_at", datetime(2024, 5, 1, 21, 30, tzinfo=UTC)),
    ],
)
@pytest.mark.parametrize("receipt_name", ["source_observation", "package_observation"])
def test_evidence_binding_covers_every_receipt_field(tmp_path, field, value, receipt_name):
    result, *_ = _produce(tmp_path)
    changed = replace(getattr(result.evidence, receipt_name), **{field: value})
    with pytest.raises(ValueError):
        replace(result.evidence, **{receipt_name: changed})


def test_package_codec_rejects_malformed_base64_and_unknown_schema(tmp_path):
    _, _, _, _, package_bytes = _setup(
        tmp_path,
        source_time=datetime(2024, 5, 1, 22, tzinfo=UTC),
        package_time=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    payload = json.loads(package_bytes)
    payload["components"]["instance"] = "AB=="
    with pytest.raises(ValueError, match="base64"):
        decode_sec_observed_xbrl_package(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
    payload = json.loads(package_bytes)
    payload["schema"] = "old-v1"
    with pytest.raises(ValueError, match="envelope"):
        decode_sec_observed_xbrl_package(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
    payload = json.loads(package_bytes)
    payload["components"]["instance"] = "PHhicmw+"
    with pytest.raises(ValueError, match="malformed"):
        decode_sec_observed_xbrl_package(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
    payload = json.loads(package_bytes)
    del payload["components"]["labels"]
    with pytest.raises(ValueError, match="components"):
        decode_sec_observed_xbrl_package(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
    payload = json.loads(package_bytes)
    payload["components"]["instance"] = b64encode(b"<!DOCTYPE x><xbrl/>").decode()
    with pytest.raises(ValueError, match="unsafe"):
        decode_sec_observed_xbrl_package(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
    duplicate = package_bytes.decode().replace('"schema":', '"schema":"old-v1","schema":', 1)
    with pytest.raises(ValueError, match="duplicate"):
        decode_sec_observed_xbrl_package(duplicate.encode())
