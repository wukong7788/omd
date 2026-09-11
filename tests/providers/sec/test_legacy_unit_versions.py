"""Offline v2 raw-unit integration for legacy SEC financial producers."""

import socket
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import (
    SecConsumerCommit,
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
    load_sec_pit_bundle,
    produce_sec_financials_from_sgml,
    produce_sec_financials_from_xbrl_package,
    select_sec_financial_versions,
    write_sec_pit_bundle,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_sgml_financials import _observation, _raw, _request
from test_xbrl_package_financials import _inline_raw, _package_observation

from ohmydata.providers.sec.sgml_financials import _documents

SGML_V1 = "sec-sgml-financial-parser-v1-edgartools-5.56.0"
SGML_V2 = "sec-sgml-financial-parser-v2-edgartools-5.56.0"
PACKAGE_V1 = "sec-xbrl-package-financial-parser-v1-edgartools-5.56.0"
PACKAGE_V2 = "sec-xbrl-package-financial-parser-v2-edgartools-5.56.0"
PRODUCED = datetime(2024, 5, 1, 23, tzinfo=UTC)

pytest.importorskip("edgar")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _unit(raw: bytes, definition: bytes) -> bytes:
    return raw.replace(b'<unit id="usd"><measure>iso4217:USD</measure></unit>', definition)


def _produce_sgml(root, raw, *, parser_version=SGML_V2):
    source, observation = _observation(root, raw)
    output = SnapshotStore(root / "output")
    production = produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=observation,
        projection_store=output,
        request=_request(),
        produced_at=PRODUCED,
        parser_version=parser_version,
    )
    return production, output


def _produce_package(root, raw, *, parser_version=PACKAGE_V2):
    source, source_observation = _observation(root / "source", _inline_raw())
    packages, package_observation, availability = _package_observation(
        root / "package",
        source_observation,
        embedded=_documents(raw.decode()),
        declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    output = SnapshotStore(root / "output")
    production = produce_sec_financials_from_xbrl_package(
        source_store=source,
        source_observation=source_observation,
        package_store=packages,
        package_observation=package_observation,
        package_availability=availability,
        projection_store=output,
        request=_request(),
        produced_at=PRODUCED,
        parser_version=parser_version,
    )
    return production, output


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (b'<unit id="usd"><measure>iso4217:USD</measure></unit>', "iso4217:USD"),
        (
            b'<unit id="usd"><measure>pure</measure><measure>shares</measure></unit>',
            '{"measures":["pure","shares"],"type":"product"}',
        ),
        (
            (
                b'<unit id="usd"><divide><unitNumerator><measure>iso4217:USD</measure>'
                b"</unitNumerator><unitDenominator><measure>shares</measure>"
                b"</unitDenominator></divide></unit>"
            ),
            '{"denominator":["shares"],"numerator":["iso4217:USD"],"type":"divide"}',
        ),
    ],
)
@pytest.mark.parametrize("producer", [_produce_sgml, _produce_package])
def test_v2_producers_apply_raw_simple_product_and_divide_units(
    tmp_path, producer, definition, expected
):
    production, _ = producer(tmp_path, _unit(_raw(), definition))
    row = production.vintage.rows[0]
    assert row.unit == expected
    assert row.currency == ("USD" if expected == "iso4217:USD" else None)


@pytest.mark.parametrize(
    ("producer", "v1", "v2"),
    [(_produce_sgml, SGML_V1, SGML_V2), (_produce_package, PACKAGE_V1, PACKAGE_V2)],
)
def test_explicit_v1_retains_rows_while_default_v2_seals_own_adapter_and_configuration(
    tmp_path, producer, v1, v2
):
    raw = _unit(
        _raw(),
        b'<unit id="usd"><divide><unitNumerator><measure>iso4217:USD</measure>'
        b"</unitNumerator><unitDenominator><measure>shares</measure>"
        b"</unitDenominator></divide></unit>",
    )
    old, _ = producer(tmp_path / "v1", raw, parser_version=v1)
    new, _ = producer(tmp_path / "v2", raw)
    assert old.parser_version == v1
    assert new.parser_version == v2
    assert old.versions[0].adapter_version.endswith("adapter-v1")
    assert new.versions[0].adapter_version.endswith("adapter-v2")
    assert old.versions[0].configuration_identity != new.versions[0].configuration_identity
    assert old.vintage.rows[0].unit != new.vintage.rows[0].unit
    assert replace(old.vintage.rows[0], unit=None) == replace(new.vintage.rows[0], unit=None)


@pytest.mark.parametrize("producer", [_produce_sgml, _produce_package])
@pytest.mark.parametrize(
    "raw",
    [
        _raw().replace(b"</xbrl>", b'<unit id="unused"><measure/></unit></xbrl>'),
        _raw().replace(b"</xbrl>", b'<unit id="usd"><measure>iso4217:USD</measure></unit></xbrl>'),
        _raw().replace(b'unitRef="usd"', b'unitRef="missing"'),
    ],
    ids=["malformed-unused", "duplicate", "missing-reference"],
)
def test_v2_invalid_or_unresolved_raw_units_fail_before_projection_write(tmp_path, producer, raw):
    output = SnapshotStore(tmp_path / "output")
    with pytest.raises(ValueError):
        if producer is _produce_sgml:
            source, observation = _observation(tmp_path / "source", raw)
            produce_sec_financials_from_sgml(
                source_store=source,
                source_observation=observation,
                projection_store=output,
                request=_request(),
                produced_at=PRODUCED,
            )
        else:
            source, source_observation = _observation(tmp_path / "source", _inline_raw())
            packages, package_observation, availability = _package_observation(
                tmp_path / "package",
                source_observation,
                embedded=_documents(raw.decode()),
                declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
                observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
            )
            produce_sec_financials_from_xbrl_package(
                source_store=source,
                source_observation=source_observation,
                package_store=packages,
                package_observation=package_observation,
                package_availability=availability,
                projection_store=output,
                request=_request(),
                produced_at=PRODUCED,
            )
    assert not list(output.root.rglob("response.bin"))


@pytest.mark.parametrize(
    ("producer", "version"),
    [
        (_produce_sgml, PACKAGE_V1),
        (_produce_sgml, None),
        (_produce_package, SGML_V1),
        (_produce_package, None),
    ],
)
def test_producers_reject_unknown_type_invalid_and_cross_family_versions_before_writes(
    tmp_path, producer, version, monkeypatch
):
    if version is None:
        version = 2
    if producer is _produce_sgml:
        source, observation = _observation(tmp_path / "source", _raw())
        output = SnapshotStore(tmp_path / "output")
        monkeypatch.setattr(
            SnapshotStore, "replay_observation", lambda *_: pytest.fail("dependency replayed")
        )
        with pytest.raises(ValueError, match="parser version"):
            produce_sec_financials_from_sgml(
                source_store=source,
                source_observation=observation,
                projection_store=output,
                request=_request(),
                produced_at=PRODUCED,
                parser_version=version,
            )
    else:
        source, source_observation = _observation(tmp_path / "source", _inline_raw())
        packages, package_observation, availability = _package_observation(
            tmp_path / "package",
            source_observation,
            embedded=_documents(_raw().decode()),
            declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
            observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
        )
        output = SnapshotStore(tmp_path / "output")
        monkeypatch.setattr(
            SnapshotStore, "replay_observation", lambda *_: pytest.fail("dependency replayed")
        )
        with pytest.raises(ValueError, match="parser version"):
            produce_sec_financials_from_xbrl_package(
                source_store=source,
                source_observation=source_observation,
                package_store=packages,
                package_observation=package_observation,
                package_availability=availability,
                projection_store=output,
                request=_request(),
                produced_at=PRODUCED,
                parser_version=version,
            )
    assert not list(output.root.rglob("response.bin"))


def test_mixed_sgml_versions_bundle_and_quality_commit_selection_are_isolated(tmp_path):
    raw = _unit(
        _raw(),
        b'<unit id="usd"><divide><unitNumerator><measure>iso4217:USD</measure>'
        b"</unitNumerator><unitDenominator><measure>shares</measure>"
        b"</unitDenominator></divide></unit>",
    )
    source, observation = _observation(tmp_path / "source", raw)
    projections = SnapshotStore(tmp_path / "projections")
    old = produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=observation,
        projection_store=projections,
        request=_request(),
        produced_at=PRODUCED,
        parser_version=SGML_V1,
    )
    new_time = datetime(2024, 5, 1, 23, 1, tzinfo=UTC)
    new = produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=observation,
        projection_store=projections,
        request=_request(),
        produced_at=new_time,
    )
    old_version, new_version = old.versions[0], new.versions[0]
    old_quality = SecQualityRecord(
        old_version.normalized_version_id, "quality-v1", SecQualityStatus.PASS, PRODUCED
    )
    old_commit = SecConsumerCommit(
        old_version.normalized_version_id,
        old_quality.quality_record_id,
        "a" * 64,
        PRODUCED,
    )
    new_quality = SecQualityRecord(
        new_version.normalized_version_id, "quality-v1", SecQualityStatus.PASS, new_time
    )
    new_commit = SecConsumerCommit(
        new_version.normalized_version_id,
        new_quality.quality_record_id,
        "a" * 64,
        new_time,
    )
    bundle_store = SnapshotStore(tmp_path / "bundle")
    bundle = write_sec_pit_bundle(
        store=bundle_store,
        batch_identity="mixed-sgml-unit-versions",
        versions=(old_version, new_version),
        quality_records=(old_quality, new_quality),
        consumer_commits=(old_commit, new_commit),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
    )
    resolved = {
        old_version.observation.observation_identity: old.projection_observation,
        new_version.observation.observation_identity: new.projection_observation,
    }
    loaded = load_sec_pit_bundle(
        store=bundle_store,
        bundle_ref=bundle,
        source_store=projections,
        resolve_observation=resolved.__getitem__,
    )
    policy = SecPitPolicy(
        new_version.schema_version,
        new_version.adapter_version,
        new_version.normalization_version,
        new_version.configuration_identity,
        "quality-v1",
        datetime(2024, 5, 2, tzinfo=UTC),
    )
    selected = select_sec_financial_versions(
        loaded.versions,
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=policy.quality_cutoff,
        policy=policy,
        quality_records=loaded.quality_records,
        consumer_commits=loaded.consumer_commits,
    )
    assert [item.version.normalized_version_id for item in selected] == [
        new_version.normalized_version_id
    ]
    assert not select_sec_financial_versions(
        (new_version,),
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=policy.quality_cutoff,
        policy=policy,
        quality_records=(old_quality,),
        consumer_commits=(old_commit,),
    )


def test_mixed_package_versions_bundle_and_quality_commit_selection_are_isolated(tmp_path):
    raw = _unit(
        _raw(),
        b'<unit id="usd"><divide><unitNumerator><measure>iso4217:USD</measure>'
        b"</unitNumerator><unitDenominator><measure>shares</measure>"
        b"</unitDenominator></divide></unit>",
    )
    source, source_observation = _observation(tmp_path / "source", _inline_raw())
    packages, package_observation, availability = _package_observation(
        tmp_path / "package",
        source_observation,
        embedded=_documents(raw.decode()),
        declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    projections = SnapshotStore(tmp_path / "projections")
    old = produce_sec_financials_from_xbrl_package(
        source_store=source,
        source_observation=source_observation,
        package_store=packages,
        package_observation=package_observation,
        package_availability=availability,
        projection_store=projections,
        request=_request(),
        produced_at=PRODUCED,
        parser_version=PACKAGE_V1,
    )
    new_time = datetime(2024, 5, 1, 23, 1, tzinfo=UTC)
    new = produce_sec_financials_from_xbrl_package(
        source_store=source,
        source_observation=source_observation,
        package_store=packages,
        package_observation=package_observation,
        package_availability=availability,
        projection_store=projections,
        request=_request(),
        produced_at=new_time,
    )
    old_version, new_version = old.versions[0], new.versions[0]
    old_quality = SecQualityRecord(
        old_version.normalized_version_id, "quality-v1", SecQualityStatus.PASS, PRODUCED
    )
    old_commit = SecConsumerCommit(
        old_version.normalized_version_id,
        old_quality.quality_record_id,
        "a" * 64,
        PRODUCED,
    )
    new_quality = SecQualityRecord(
        new_version.normalized_version_id, "quality-v1", SecQualityStatus.PASS, new_time
    )
    new_commit = SecConsumerCommit(
        new_version.normalized_version_id,
        new_quality.quality_record_id,
        "a" * 64,
        new_time,
    )
    bundle_store = SnapshotStore(tmp_path / "bundle")
    bundle = write_sec_pit_bundle(
        store=bundle_store,
        batch_identity="mixed-package-unit-versions",
        versions=(old_version, new_version),
        quality_records=(old_quality, new_quality),
        consumer_commits=(old_commit, new_commit),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
    )
    resolved = {
        old_version.observation.observation_identity: old.projection_observation,
        new_version.observation.observation_identity: new.projection_observation,
    }
    loaded = load_sec_pit_bundle(
        store=bundle_store,
        bundle_ref=bundle,
        source_store=projections,
        resolve_observation=resolved.__getitem__,
    )
    policy = SecPitPolicy(
        new_version.schema_version,
        new_version.adapter_version,
        new_version.normalization_version,
        new_version.configuration_identity,
        "quality-v1",
        datetime(2024, 5, 2, tzinfo=UTC),
    )
    selected = select_sec_financial_versions(
        loaded.versions,
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=policy.quality_cutoff,
        policy=policy,
        quality_records=loaded.quality_records,
        consumer_commits=loaded.consumer_commits,
    )
    assert [item.version.normalized_version_id for item in selected] == [
        new_version.normalized_version_id
    ]
    assert not select_sec_financial_versions(
        (new_version,),
        mode=SecPitMode.SYSTEM_REPLAY,
        knowledge_cutoff=policy.quality_cutoff,
        policy=policy,
        quality_records=(old_quality,),
        consumer_commits=(old_commit,),
    )
