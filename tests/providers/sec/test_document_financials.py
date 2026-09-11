"""Real-parser regressions for the distinct document financial production domain."""

import json
import socket
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotIntegrityError, SnapshotStore
from ohmydata.providers.sec.document_financials import (
    produce_sec_financials_from_document_source,
    restore_sec_document_financial_production,
)
from ohmydata.providers.sec.observed_xbrl_financials import SecObservedFinancialProduction

sys.path.insert(0, str(Path(__file__).parent))
from test_document_source import CAPTURED, inputs, produce
from test_observed_xbrl_financials import _produce, _request


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def setup(root, *, change=None):
    sources = inputs(root, change=change)
    package = produce(root, sources)
    package_store = SnapshotStore(root / "package")
    mapping = {s.observation.observation_identity: (s.store, s.observation) for s in sources}
    mapping[package.observation.observation_identity] = (package_store, package.observation)
    return sources, package, mapping


def build(root, *, change=None, request=None, produced_at=None):
    sources, package, mapping = setup(root, change=change)
    output = SnapshotStore(root / "financial")
    result = produce_sec_financials_from_document_source(
        package_store=SnapshotStore(root / "package"),
        package_observation=package.observation,
        resolve_observation=mapping.__getitem__,
        output_store=output,
        request=request or _request(),
        produced_at=produced_at or CAPTURED + timedelta(hours=1),
    )
    return result, output, mapping, sources


def test_real_parser_parity_and_read_only_restoration(tmp_path, monkeypatch):
    result, output, mapping, _ = build(tmp_path / "document")
    legacy, *_ = _produce(tmp_path / "sgml")
    assert result.vintage.rows == legacy.vintage.rows
    assert not isinstance(result, SecObservedFinancialProduction)
    assert result.production_identity != legacy.production_identity
    assert result.configuration_identity != legacy.configuration_identity
    assert result.vintage.rows[0].unit == "iso4217:USD"
    assert result.vintage.rows[0].value_native == "123"
    assert result.vintage.known_by_at == CAPTURED
    payload = output.replay_observation(result.output_observation).payload
    assert b"source_available_at" not in payload and b"sgml" not in payload
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("restore wrote"))
    restored = restore_sec_document_financial_production(
        output_store=SnapshotStore(output.root),
        output_observation=result.output_observation,
        resolve_observation=mapping.__getitem__,
    )
    assert restored.production_identity == result.production_identity
    assert restored.vintage == result.vintage


def test_fresh_store_roots_are_identity_independent(tmp_path):
    first, *_ = build(tmp_path / "first")
    second, *_ = build(tmp_path / "second")
    assert first.production_identity == second.production_identity
    assert first.output_observation.response_sha256 == second.output_observation.response_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"cik": "0000000002"},
        {"form": "10-K"},
        {"accession_number": "0000000001-24-000002"},
        {"statement_types": ("cash_flow",)},
    ],
)
def test_source_identity_and_required_coverage_fail_before_output(tmp_path, changes):
    with pytest.raises(ValueError):
        build(tmp_path, request=replace(_request(), **changes))
    assert not list((tmp_path / "financial").rglob("response.bin"))


def test_production_before_source_package_fails_without_output(tmp_path):
    with pytest.raises(ValueError, match="precedes"):
        build(tmp_path, produced_at=CAPTURED - timedelta(seconds=1))
    assert not list((tmp_path / "financial").rglob("response.bin"))


def test_unresolvable_raw_unit_is_not_returned_as_opaque_id(tmp_path):
    def change(payloads):
        payloads["instance"] = payloads["instance"].replace(b'unitRef="usd"', b'unitRef="missing"')

    with pytest.raises(ValueError, match="unit"):
        build(tmp_path, change=change)
    assert not list((tmp_path / "financial").rglob("response.bin"))


def test_caller_symbol_is_label_but_binds_output_identity(tmp_path):
    first, *_ = build(tmp_path / "first")
    renamed, *_ = build(tmp_path / "renamed", request=replace(_request(), symbol="OTHER_LABEL"))
    assert first.vintage.company_name == renamed.vintage.company_name
    assert first.vintage.rows == renamed.vintage.rows
    assert first.configuration_identity == renamed.configuration_identity
    assert first.production_identity != renamed.production_identity


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other"),
        ("endpoint", "other"),
        ("request_identity", "1" * 64),
        ("response_sha256", "2" * 64),
        ("fact_version", "3" * 64),
        ("snapshot_identity", "4" * 64),
        ("observation_identity", "5" * 64),
        ("serialization_identifier", "other-v1"),
        ("path", Path("/tmp/synthetic-changed.json")),
        ("snapshot_fetched_at", CAPTURED + timedelta(hours=2)),
    ],
)
def test_seal_covers_output_receipt_fields(tmp_path, field, value):
    result, *_ = build(tmp_path)
    with pytest.raises(ValueError):
        replace(result, output_observation=replace(result.output_observation, **{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_name", "Other"),
        ("symbol", "OTHER"),
        ("is_amendment", True),
        ("known_by_at", CAPTURED + timedelta(seconds=1)),
    ],
)
def test_seal_revalidates_native_metadata(tmp_path, field, value):
    result, *_ = build(tmp_path)
    with pytest.raises(ValueError):
        replace(result, vintage=replace(result.vintage, **{field: value}))


@pytest.mark.parametrize("mutation", ["unknown", "row", "parser", "future", "source-receipt"])
def test_output_claims_are_rebuilt_from_raw_sources(tmp_path, mutation):
    result, output, mapping, _ = build(tmp_path)
    data = json.loads(output.replay_observation(result.output_observation).payload)
    if mutation == "unknown":
        data["unexpected"] = True
    elif mutation == "row":
        data["rows"][0]["value_native"] = "999"
    elif mutation == "parser":
        data["parser_version"] = "other-parser-v1"
    elif mutation == "future":
        data["produced_at"] = "2024-05-03T00:00:00Z"
    else:
        data["source_package_receipt"]["fact_version"] = "0" * 64
    changed = SnapshotStore(tmp_path / "changed")
    ref = changed.observe(
        RequestSpec(
            "sec",
            "financial-document-observed-rows",
            {
                "cik": result.request.cik,
                "accession_number": result.request.accession_number,
                "form": result.request.form,
            },
        ),
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
        result.produced_at,
        "sec-document-financial-rows-v1",
    )
    with pytest.raises(ValueError):
        restore_sec_document_financial_production(
            output_store=changed, output_observation=ref, resolve_observation=mapping.__getitem__
        )


def test_restore_checks_original_instance_bytes(tmp_path):
    result, output, mapping, sources = build(tmp_path)
    instance = next(s for s in sources if s.role == "instance")
    raw_path = next(
        instance.store.root.glob(f"**/{instance.observation.response_sha256}/response.bin")
    )
    raw_path.write_bytes(b"tampered")
    with pytest.raises(SnapshotIntegrityError):
        restore_sec_document_financial_production(
            output_store=output,
            output_observation=result.output_observation,
            resolve_observation=mapping.__getitem__,
        )


@pytest.mark.parametrize(
    "nested,field", [("source_package", "package_identity"), ("vintage", "vintage_identity")]
)
def test_forged_nested_identity_is_rejected_without_repair(tmp_path, nested, field):
    result, *_ = build(tmp_path)
    target = getattr(result, nested)
    forged = "0" * 64
    object.__setattr__(target, field, forged)
    with pytest.raises(ValueError, match="nested identity"):
        replace(result)
    assert getattr(target, field) == forged


def test_compound_raw_units_and_dimensions_remain_explicit(tmp_path):
    def change(payloads):
        payloads["instance"] = (
            payloads["instance"]
            .replace(
                b"<measure>iso4217:USD</measure>",
                b"<divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator>"
                b"<unitDenominator><measure>shares</measure></unitDenominator></divide>",
            )
            .replace(
                b"</entity>",
                b'<segment><xbrldi:explicitMember xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
                b'dimension="us-gaap:ProductOrServiceAxis">us-gaap:ProductMember'
                b"</xbrldi:explicitMember></segment></entity>",
            )
        )

    result, *_ = build(
        tmp_path, change=change, request=replace(_request(), include_dimensions=True)
    )
    row = result.vintage.rows[0]
    assert json.loads(row.unit) == {
        "type": "divide",
        "numerator": ["iso4217:USD"],
        "denominator": ["shares"],
    }
    assert row.dimension is not None
    assert row.period_start.isoformat() == "2024-01-01"
    assert row.period_end.isoformat() == "2024-03-31"
