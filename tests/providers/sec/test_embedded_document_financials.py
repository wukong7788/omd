"""Experimental embedded-linkbase financial production contracts."""

import json
import re
import socket
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.providers.sec._observed_financial_bundle_codec import _receipt
from ohmydata.providers.sec.document_financial_bundle import (
    load_sec_document_financial_bundle,
    write_sec_document_financial_bundle,
)
from ohmydata.providers.sec.document_financial_replay import (
    select_sec_document_financial_productions,
)
from ohmydata.providers.sec.document_financials import (
    produce_sec_financials_from_document_source,
    produce_sec_financials_from_embedded_document_source,
    restore_sec_document_financial_production,
)
from ohmydata.providers.sec.document_source import SecDocumentSource, _canonical

sys.path.insert(0, str(Path(__file__).parent))
from test_document_financials import build as legacy_build
from test_document_source import CAPTURED, FILENAMES, REQUEST, source_payloads
from test_embedded_document_source import produce
from test_observed_financial_bundle import _records
from test_observed_xbrl_financials import _request


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _embedded_sources(root, change=None):
    payloads = source_payloads()
    presentation = payloads.pop("presentation")
    labels = payloads.pop("labels")
    inner = b"".join(
        item[item.find(b">") + 1 : item.rfind(b"</link:linkbase>")]
        for item in (presentation, labels)
    )
    payloads["schema"] = re.sub(
        rb"<xs:annotation><xs:appinfo>.*?</xs:appinfo></xs:annotation>",
        (
            b'<xs:annotation><xs:appinfo><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink">'
            + inner
            + b"</link:linkbase></xs:appinfo></xs:annotation>"
        ),
        payloads["schema"],
        flags=re.DOTALL,
    )
    index = json.loads(payloads["index"])
    index["directory"]["item"] = [
        item
        for item in index["directory"]["item"]
        if item["name"] not in {FILENAMES["presentation"], FILENAMES["labels"]}
    ]
    payloads["index"] = json.dumps(index).encode()
    if change is not None:
        change(payloads)
    store = SnapshotStore(root / "sources")
    sources = []
    for role in ("submissions", "index", "primary", "schema", "instance"):
        if role == "submissions":
            spec, serialization = (
                RequestSpec("sec", "edgar_submissions", {"cik": REQUEST["cik"]}),
                "sec-submissions-json-v1",
            )
        elif role == "index":
            spec, serialization = (
                RequestSpec(
                    "sec",
                    "company-filing-directory",
                    {key: REQUEST[key] for key in ("cik", "accession_number")},
                ),
                "sec-filing-directory-json-v1",
            )
        else:
            spec, serialization = (
                RequestSpec(
                    "sec",
                    "company-filing-document",
                    {key: REQUEST[key] for key in ("cik", "accession_number")}
                    | {"filename": FILENAMES[role]},
                ),
                "sec-filing-document-bytes-v1",
            )
        observation = store.observe(
            spec, payloads[role], CAPTURED - timedelta(hours=1), serialization
        )
        sources.append(SecDocumentSource(role, FILENAMES.get(role), store, observation))
    return sources


def build_embedded(root, change=None):
    """Build a real-parser embedded-linkbase financial production fixture."""
    sources = _embedded_sources(root, change=change)
    package = produce(root, sources)
    package_store = SnapshotStore(root / "package")
    mapping = {
        item.observation.observation_identity: (item.store, item.observation) for item in sources
    }
    mapping[package.observation.observation_identity] = (package_store, package.observation)
    output = SnapshotStore(root / "financial")
    result = produce_sec_financials_from_embedded_document_source(
        package_store=package_store,
        package_observation=package.observation,
        resolve_observation=mapping.__getitem__,
        output_store=output,
        request=_request(),
        produced_at=CAPTURED + timedelta(hours=1),
    )
    return result, output, mapping


def test_embedded_producer_has_distinct_output_domain_and_readonly_restore(tmp_path, monkeypatch):
    result, output, mapping = build_embedded(tmp_path)
    assert result.output_schema_version == "sec-embedded-document-financial-rows-v1"
    assert result.parser_version == "sec-schema-embedded-financial-parser-v1-edgartools-5.56.0"
    assert result.output_observation.endpoint == "financial-embedded-document-observed-rows"
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("restore wrote"))
    restored = restore_sec_document_financial_production(
        output_store=SnapshotStore(output.root),
        output_observation=result.output_observation,
        resolve_observation=mapping.__getitem__,
    )
    assert restored.production_identity == result.production_identity


def test_embedded_instance_above_legacy_document_cap_reaches_real_parser(tmp_path):
    def expand(payloads):
        payloads["instance"] = b"<!--" + b"x" * (2 * 1024 * 1024) + b"-->" + payloads["instance"]

    result, _, _ = build_embedded(tmp_path, change=expand)
    assert result.vintage.rows


def test_old_producer_rejects_embedded_package_before_output(tmp_path):
    sources = _embedded_sources(tmp_path)
    package = produce(tmp_path, sources)
    mapping = {
        item.observation.observation_identity: (item.store, item.observation) for item in sources
    }
    output = SnapshotStore(tmp_path / "financial")
    with pytest.raises(ValueError):
        produce_sec_financials_from_document_source(
            package_store=SnapshotStore(tmp_path / "package"),
            package_observation=package.observation,
            resolve_observation=mapping.__getitem__,
            output_store=output,
            request=_request(),
            produced_at=CAPTURED + timedelta(hours=1),
        )
    assert not list(output.root.rglob("response.bin"))


def test_embedded_producer_rejects_external_linkbase_before_output(tmp_path):
    def external(payloads):
        payloads["schema"] = payloads["schema"].replace(
            b"<link:linkbase",
            b'<link:linkbaseRef xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="outside.xml"/><link:linkbase',
            1,
        )

    sources = _embedded_sources(tmp_path, change=external)
    valid = produce(tmp_path / "valid", _embedded_sources(tmp_path / "valid"))
    manifest = json.loads(valid.manifest)
    receipts = {item.role: _receipt(item.observation) for item in sources}
    for claim in manifest["sources"]:
        claim["receipt"] = receipts[claim["role"]]
    package_store = SnapshotStore(tmp_path / "package")
    package_ref = package_store.observe(
        RequestSpec("sec", "company-filing-embedded-document-source-package", REQUEST),
        _canonical(manifest),
        CAPTURED,
        "sec-document-embedded-source-package-v1",
    )
    mapping = {
        item.observation.observation_identity: (item.store, item.observation) for item in sources
    }
    output = SnapshotStore(tmp_path / "financial")
    with pytest.raises(ValueError, match="external"):
        produce_sec_financials_from_embedded_document_source(
            package_store=package_store,
            package_observation=package_ref,
            resolve_observation=mapping.__getitem__,
            output_store=output,
            request=_request(),
            produced_at=CAPTURED + timedelta(hours=1),
        )
    assert not list(output.root.rglob("response.bin"))


def test_restore_rejects_mispaired_output_and_source_domains_before_parse(tmp_path, monkeypatch):
    result, output, mapping = build_embedded(tmp_path / "embedded")
    legacy, _, legacy_mapping, _ = legacy_build(tmp_path / "legacy")
    data = json.loads(output.replay_observation(result.output_observation).payload)
    data["source_package_receipt"] = _receipt(legacy.source_package.observation)
    forged = SnapshotStore(tmp_path / "forged")
    observation = forged.observe(
        RequestSpec("sec", "financial-embedded-document-observed-rows", REQUEST),
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
        result.produced_at,
        "sec-embedded-document-financial-rows-v1",
    )
    mapping.update(legacy_mapping)
    monkeypatch.setattr(
        "ohmydata.providers.sec.document_financials._build",
        lambda **_: pytest.fail("parser was reached"),
    )
    with pytest.raises(ValueError, match="domain mismatch"):
        restore_sec_document_financial_production(
            output_store=forged,
            output_observation=observation,
            resolve_observation=mapping.__getitem__,
        )


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("schema", "sec-document-financial-rows-v1", "output schema mismatch"),
        ("parser_version", "other-parser-v1", "parser or configuration mismatch"),
        ("configuration_version", "other-config-v1", "parser or configuration mismatch"),
        ("configuration_identity", "0" * 64, "parser or configuration mismatch"),
    ],
)
def test_forged_embedded_output_contract_fails_before_parser(
    tmp_path, monkeypatch, field, value, error
):
    result, output, mapping = build_embedded(tmp_path)
    data = json.loads(output.replay_observation(result.output_observation).payload)
    data[field] = value
    forged = SnapshotStore(tmp_path / "forged")
    observation = forged.observe(
        RequestSpec("sec", "financial-embedded-document-observed-rows", REQUEST),
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
        result.produced_at,
        "sec-embedded-document-financial-rows-v1",
    )
    monkeypatch.setattr(
        "ohmydata.providers.sec.document_financials._build",
        lambda **_: pytest.fail("parser was reached"),
    )
    with pytest.raises(ValueError, match=error):
        restore_sec_document_financial_production(
            output_store=forged,
            output_observation=observation,
            resolve_observation=mapping.__getitem__,
        )


def test_mixed_legacy_and_embedded_bundle_restores_each_sealed_domain(tmp_path):
    embedded, embedded_store, mapping = build_embedded(tmp_path / "embedded")
    legacy, legacy_store, legacy_mapping, _ = legacy_build(tmp_path / "legacy")
    mapping[embedded.output_observation.observation_identity] = (
        embedded_store,
        embedded.output_observation,
    )
    mapping[legacy.output_observation.observation_identity] = (
        legacy_store,
        legacy.output_observation,
    )
    mapping.update(legacy_mapping)
    _, embedded_quality, embedded_commit = _records(embedded)
    _, legacy_quality, legacy_commit = _records(legacy)
    store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_document_financial_bundle(
        store=store,
        batch_identity="mixed-synthetic",
        productions=(legacy, embedded),
        quality_records=(legacy_quality, embedded_quality),
        consumer_commits=(legacy_commit, embedded_commit),
        captured_at=embedded.produced_at + timedelta(minutes=5),
        resolve_observation=mapping.__getitem__,
    )
    loaded = load_sec_document_financial_bundle(
        store=SnapshotStore(store.root), bundle_ref=ref, resolve_observation=mapping.__getitem__
    )
    assert {item.output_schema_version for item in loaded.productions} == {
        "sec-document-financial-rows-v1",
        "sec-embedded-document-financial-rows-v1",
    }


def test_mixed_variants_do_not_share_quality_or_commit_identities(tmp_path):
    embedded, _, _ = build_embedded(tmp_path / "embedded")
    legacy, *_ = legacy_build(tmp_path / "legacy")
    policy, embedded_quality, embedded_commit = _records(embedded)
    _, legacy_quality, legacy_commit = _records(legacy)
    selected = select_sec_document_financial_productions(
        (embedded, legacy),
        (embedded_quality, legacy_quality),
        (embedded_commit, legacy_commit),
        policy,
    )
    assert {item.production.production_identity for item in selected} == {
        embedded.production_identity
    }
    assert (
        select_sec_document_financial_productions(
            (embedded,),
            (legacy_quality,),
            (legacy_commit,),
            policy,
        )
        == ()
    )


def test_embedded_producer_rejects_legacy_source_before_output(tmp_path):
    legacy, _, mapping, _ = legacy_build(tmp_path / "legacy")
    output = SnapshotStore(tmp_path / "embedded-output")
    package = legacy.source_package
    package_store, _ = mapping[package.observation.observation_identity]
    with pytest.raises(ValueError, match="serialization mismatch"):
        produce_sec_financials_from_embedded_document_source(
            package_store=package_store,
            package_observation=package.observation,
            resolve_observation=mapping.__getitem__,
            output_store=output,
            request=_request(),
            produced_at=CAPTURED + timedelta(hours=1),
        )
    assert not list(output.root.rglob("response.bin"))
