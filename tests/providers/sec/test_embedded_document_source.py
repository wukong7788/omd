"""Offline checks for experimental SEC embedded-linkbase source closures."""

import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core import RequestSpec, SnapshotIntegrityError, SnapshotStore
from ohmydata.providers.sec.document_source import SecDocumentSource
from ohmydata.providers.sec.embedded_document_source import (
    produce_sec_embedded_document_source_package,
    restore_sec_embedded_document_source_package,
)

ACCEPTED = datetime(2024, 5, 1, 21, tzinfo=UTC)
CAPTURED = ACCEPTED + timedelta(hours=2)
REQUEST = {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"}
FILENAMES = {"primary": "fake.htm", "schema": "fake.xsd", "instance": "fake_htm.xml"}
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XS = "http://www.w3.org/2001/XMLSchema"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def payloads():
    submissions = {
        "cik": "1",
        "name": "Synthetic Filing Co.",
        "filings": {
            "recent": {
                "accessionNumber": [REQUEST["accession_number"]],
                "form": ["10-Q"],
                "filingDate": ["2024-05-01"],
                "reportDate": ["2024-03-31"],
                "primaryDocument": ["fake.htm"],
                "acceptanceDateTime": ["2024-05-01T21:00:00Z"],
            }
        },
    }
    index = {
        "directory": {
            "name": "/Archives/edgar/data/1/000000000124000001",
            "item": [{"name": name} for name in FILENAMES.values()],
        }
    }
    schema = f'''<xs:schema xmlns:xs="{XS}" xmlns:link="{LINK}" xmlns:xlink="{XLINK}">
      <xs:annotation><xs:appinfo><link:linkbase>
        <link:labelLink/><link:presentationLink/><link:calculationLink/><link:definitionLink/>
      </link:linkbase></xs:appinfo></xs:annotation>
      <xs:import namespace="http://fasb.org/us-gaap/2024" schemaLocation="https://xbrl.fasb.org/us-gaap.xsd"/>
    </xs:schema>'''.encode()
    instance = f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:link="{LINK}" xmlns:xlink="{XLINK}">
      <link:schemaRef xlink:href="fake.xsd"/>
      <xbrli:context id="c"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2024-03-31</xbrli:instant></xbrli:period></xbrli:context>
      <dei:EntityCentralIndexKey xmlns:dei="http://xbrl.sec.gov/dei/2024">1</dei:EntityCentralIndexKey>
    </xbrli:xbrl>'''.encode()
    return {
        "submissions": json.dumps(submissions).encode(),
        "index": json.dumps(index).encode(),
        "primary": f'<html xmlns:link="{LINK}" xmlns:xlink="{XLINK}"><head><meta charset="utf-8"></head><body><link:schemaRef xlink:href="fake.xsd"/>😀</body></html>'.encode(),
        "schema": schema,
        "instance": instance,
    }


def inputs(root, change=None, time_by_role=None):
    raw = payloads()
    if change:
        change(raw)
    store = SnapshotStore(root / "sources")
    result = []
    for role, payload in raw.items():
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
            spec,
            payload,
            (time_by_role or {}).get(role, ACCEPTED + timedelta(hours=1)),
            serialization,
        )
        result.append(SecDocumentSource(role, FILENAMES.get(role), store, observation))
    return result


def produce(root, sources):
    return produce_sec_embedded_document_source_package(
        store=SnapshotStore(root / "package"), **REQUEST, sources=sources, captured_at=CAPTURED
    )


def test_seals_exact_five_raw_receipts_and_restores_complete_closure(tmp_path, monkeypatch):
    sources = inputs(tmp_path)
    package = produce(tmp_path, sources)
    root = json.loads(package.manifest)
    assert root["schema"] == "sec-document-embedded-source-package-v1"
    assert root["view_policy"] == "sec-schema-embedded-linkbase-view-v1"
    assert [item["role"] for item in root["sources"]] == [
        "index",
        "instance",
        "primary",
        "schema",
        "submissions",
    ]
    assert package.known_by_at == CAPTURED
    mapping = {
        source.observation.observation_identity: (
            SnapshotStore(source.store.root),
            source.observation,
        )
        for source in sources
    }
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("restore writes"))
    restored = restore_sec_embedded_document_source_package(
        store=SnapshotStore(tmp_path / "package"),
        observation=package.observation,
        resolve_observation=mapping.__getitem__,
    )
    assert restored.manifest == package.manifest
    with pytest.raises(ValueError, match="binding"):
        replace(
            package,
            observation=replace(
                package.observation, snapshot_fetched_at=CAPTURED + timedelta(seconds=1)
            ),
        )


@pytest.mark.parametrize(
    "role,before,after,error",
    [
        ("primary", b"fake.xsd", b"other.xsd", "primary schemaRef"),
        ("instance", b"fake.xsd", b"other.xsd", "instance schemaRef"),
        ("schema", b"<link:labelLink/>", b"", "labelLink"),
        (
            "schema",
            b"<link:linkbase>",
            (
                b"<!-- <link:linkbase xmlns:link='http://www.xbrl.org/2003/linkbase'> -->"
                b"<lb:linkbase xmlns:lb='http://www.xbrl.org/2003/linkbase'>"
            ),
            "pinned link",
        ),
        (
            "schema",
            b"<link:linkbase>",
            b'<link:linkbaseRef xlink:href="outside.xml"/><link:linkbase>',
            "external",
        ),
        ("schema", b"</xs:schema>", b"<!DOCTYPE x></xs:schema>", "unsafe"),
        (
            "schema",
            b"</xs:schema>",
            b"<link:linkbase/></xs:schema>",
            "one direct",
        ),
        ("instance", b"<xbrli:xbrl ", b'<xbrli:xbrl xml:base="outside/" ', "base URI"),
    ],
)
def test_invalid_embedded_graph_fails_before_package_write(tmp_path, role, before, after, error):
    def change(raw):
        assert before in raw[role]
        raw[role] = raw[role].replace(before, after, 1)
        if error == "pinned link":
            raw[role] = raw[role].replace(b"</link:linkbase>", b"</lb:linkbase>", 1)

    with pytest.raises(ValueError, match=error):
        produce(tmp_path, inputs(tmp_path, change))
    assert not list((tmp_path / "package").rglob("response.bin"))


@pytest.mark.parametrize(
    "role,size",
    [
        ("primary", 8 * 1024 * 1024 + 1),
        ("instance", 12 * 1024 * 1024 + 1),
        ("schema", 2 * 1024 * 1024 + 1),
    ],
)
def test_role_byte_limits_apply_before_parse(tmp_path, role, size):
    with pytest.raises(SnapshotIntegrityError):
        produce(tmp_path, inputs(tmp_path, lambda raw: raw.__setitem__(role, b"x" * size)))


def test_requires_all_raw_receipts_and_causal_observation_order(tmp_path):
    sources = inputs(tmp_path)
    with pytest.raises(ValueError, match="count"):
        produce(tmp_path, sources[:-1])
    with pytest.raises(ValueError, match="causal"):
        produce(
            tmp_path / "early",
            inputs(tmp_path / "early", time_by_role={"schema": ACCEPTED - timedelta(seconds=1)}),
        )


def test_rejects_duplicate_raw_receipts_and_document_filenames(tmp_path):
    sources = inputs(tmp_path)
    duplicate = next(source for source in sources if source.role == "schema")
    primary = next(source for source in sources if source.role == "primary")
    sources[sources.index(primary)] = SecDocumentSource(
        "primary", "fake.xsd", duplicate.store, duplicate.observation
    )
    with pytest.raises(ValueError, match="distinct"):
        produce(tmp_path, sources)
