"""Offline provenance closure for bounded SEC filing documents."""

import json
import socket
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SchemaMismatchError, SnapshotIntegrityError, SnapshotStore
from ohmydata.providers.sec._document_source_links import _PrimaryReferences
from ohmydata.providers.sec.document_source import (
    SecDocumentSource,
    produce_sec_document_source_package,
    restore_sec_document_source_package,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_observed_xbrl_financials import _components

ACCEPTED = datetime(2024, 5, 1, 21, tzinfo=UTC)
CAPTURED = ACCEPTED + timedelta(hours=2)
REQUEST = {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"}
FILENAMES = {
    "primary": "fake.htm",
    "schema": "fake.xsd",
    "presentation": "fake_pre.xml",
    "labels": "fake_lab.xml",
    "instance": "fake_htm.xml",
}
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def source_payloads():
    components = _components().to_mapping()
    components["instance"] = components["instance"].replace(
        b"</xbrl>",
        f'<link:schemaRef xmlns:link="{LINK}" xmlns:xlink="{XLINK}" xlink:href="fake.xsd"/></xbrl>'.encode(),
    )
    links = "".join(
        f'<link:linkbaseRef xmlns:link="{LINK}" xmlns:xlink="{XLINK}" xlink:href="fake_{suffix}.xml" xlink:role="http://www.xbrl.org/2003/role/{role}LinkbaseRef"/>'
        for suffix, role in [("pre", "presentation"), ("lab", "label")]
    )
    components["schema"] = components["schema"].replace(
        b"</xs:schema>",
        f"<xs:annotation><xs:appinfo>{links}</xs:appinfo></xs:annotation></xs:schema>".encode(),
    )
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
    return {
        "submissions": json.dumps(submissions).encode(),
        "index": json.dumps(index).encode(),
        "primary": f'<html xmlns:link="{LINK}" xmlns:xlink="{XLINK}"><head><meta charset="utf-8"></head><body><link:schemaRef xlink:href="fake.xsd"/>😀</body></html>'.encode(),
        **components,
    }


def inputs(root, *, change=None, time_by_role=None):
    payloads = source_payloads()
    if change:
        change(payloads)
    store = SnapshotStore(root / "sources")
    result = []
    for role, payload in payloads.items():
        if role == "submissions":
            spec, version = (
                RequestSpec("sec", "edgar_submissions", {"cik": "0000000001"}),
                "sec-submissions-json-v1",
            )
        elif role == "index":
            spec = RequestSpec(
                "sec",
                "company-filing-directory",
                {k: REQUEST[k] for k in ("cik", "accession_number")},
            )
            version = "sec-filing-directory-json-v1"
        else:
            spec = RequestSpec(
                "sec",
                "company-filing-document",
                {k: REQUEST[k] for k in ("cik", "accession_number")}
                | {"filename": FILENAMES[role]},
            )
            version = "sec-filing-document-bytes-v1"
        observation = store.observe(
            spec, payload, (time_by_role or {}).get(role, ACCEPTED + timedelta(hours=1)), version
        )
        result.append(SecDocumentSource(role, FILENAMES.get(role), store, observation))
    return result


def produce(root, sources):
    return produce_sec_document_source_package(
        store=SnapshotStore(root / "package"), **REQUEST, sources=sources, captured_at=CAPTURED
    )


def test_full_closure_cold_and_warm_rebuild_with_no_writes(tmp_path, monkeypatch):
    sources = inputs(tmp_path)
    package = produce(tmp_path, sources)
    second_sources = inputs(tmp_path / "other")
    second = produce(tmp_path / "other", reversed(second_sources))
    assert package.manifest == second.manifest
    assert package.package_identity == second.package_identity
    assert package.known_by_at == CAPTURED
    root = json.loads(package.manifest)
    assert len(root["sources"]) == 7
    assert root["filing"]["accepted_at"] == "2024-05-01T21:00:00Z"
    assert b"sgml" not in package.manifest and b"source_available_at" not in package.manifest
    mapping = {
        s.observation.observation_identity: (SnapshotStore(s.store.root), s.observation)
        for s in sources
    }
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("restore writes"))
    restored = restore_sec_document_source_package(
        store=SnapshotStore(tmp_path / "package"),
        observation=package.observation,
        resolve_observation=mapping.__getitem__,
    )
    assert restored.manifest == package.manifest
    assert restored.package_identity == package.package_identity
    with pytest.raises(ValueError, match="binding"):
        replace(
            package,
            observation=replace(
                package.observation, snapshot_fetched_at=CAPTURED + timedelta(seconds=1)
            ),
        )


@pytest.mark.parametrize(
    "role,before,after",
    [
        ("primary", b"fake.xsd", b"other.xsd"),
        ("primary", b"fake.xsd", b"../fake.xsd"),
        ("primary", b"fake.xsd", b"fake.xsd#fragment"),
        ("primary", b"fake.xsd", b"fake%2exsd"),
        ("primary", b'xmlns:link="http://www.xbrl.org/2003/linkbase"', b'xmlns:link="urn:wrong"'),
        ("primary", b"</body>", b'<link:schemaRef xlink:href="fake.xsd"/></body>'),
        ("primary", b"</body>", b"</head>"),
        ("primary", b'xlink:href="fake.xsd"', b'xlink:href="fake.xsd" xlink:href="fake.xsd"'),
        ("instance", b"fake.xsd", b"wrong.xsd"),
        ("instance", b">0000000001<", b">0000000002<"),
        ("schema", b"fake_pre.xml", b"wrong_pre.xml"),
        ("schema", b"presentationLinkbaseRef", b"unknownLinkbaseRef"),
        ("schema", b"labelLinkbaseRef", b"calculationLinkbaseRef"),
        ("schema", b"</xs:schema>", b"<!DOCTYPE x></xs:schema>"),
        ("index", b"/1/000000000124000001", b"/2/000000000124000001"),
        ("index", b"fake_pre.xml", b"missing_pre.xml"),
        ("submissions", b'"cik": "1"', b'"cik": "2"'),
        ("submissions", b'"10-Q"', b'"10-K"'),
        ("submissions", b"2024-03-31", b"20240331"),
        ("submissions", b"2024-05-01T21:00:00Z", b"2024-05-01T21:00:00"),
    ],
)
def test_invalid_source_graph_fails_without_package(tmp_path, role, before, after):
    def change(payloads):
        assert before in payloads[role]
        payloads[role] = payloads[role].replace(before, after)

    sources = inputs(tmp_path, change=change)
    with pytest.raises((ValueError, SchemaMismatchError, SnapshotIntegrityError)):
        produce(tmp_path, sources)
    assert not list((tmp_path / "package").rglob("response.bin"))


@pytest.mark.parametrize(
    "role,time",
    [
        ("submissions", ACCEPTED - timedelta(seconds=1)),
        ("primary", ACCEPTED - timedelta(seconds=1)),
        ("index", CAPTURED + timedelta(seconds=1)),
        ("instance", CAPTURED + timedelta(seconds=1)),
    ],
)
def test_causal_times_fail_before_package_write(tmp_path, role, time):
    with pytest.raises(ValueError, match="causal"):
        produce(tmp_path, inputs(tmp_path, time_by_role={role: time}))
    assert not list((tmp_path / "package").rglob("response.bin"))


def test_missing_duplicate_and_overlong_generator_fail(tmp_path):
    sources = inputs(tmp_path)
    for bad in (sources[:-1], sources + [sources[-1]]):
        with pytest.raises(ValueError):
            produce(tmp_path, bad)
    count = 0

    def unbounded():
        nonlocal count
        while True:
            count += 1
            yield sources[0]

    with pytest.raises(ValueError):
        produce(tmp_path, unbounded())
    assert count == 10


def test_restoration_replays_tampered_raw_dependencies(tmp_path):
    sources = inputs(tmp_path)
    package = produce(tmp_path, sources)
    mapping = {s.observation.observation_identity: (s.store, s.observation) for s in sources}
    primary = next(s for s in sources if s.role == "primary")
    payload_path = next(
        primary.store.root.glob(f"**/{primary.observation.response_sha256}/response.bin")
    )
    payload_path.write_bytes(b"tampered")
    with pytest.raises(SnapshotIntegrityError):
        restore_sec_document_source_package(
            store=SnapshotStore(tmp_path / "package"),
            observation=package.observation,
            resolve_observation=mapping.__getitem__,
        )


@pytest.mark.parametrize(
    "role,size",
    [
        ("primary", 4 * 1024 * 1024 + 1),
        ("instance", 4 * 1024 * 1024 + 1),
        ("schema", 2 * 1024 * 1024 + 1),
        ("submissions", 2 * 1024 * 1024 + 1),
    ],
)
def test_source_size_limit_before_unbounded_parse(tmp_path, role, size):
    def change(payloads):
        payloads[role] = b"x" * size

    with pytest.raises(SnapshotIntegrityError):
        produce(tmp_path, inputs(tmp_path, change=change))
    assert not list((tmp_path / "package").rglob("response.bin"))


def test_separate_document_instance_admitted_above_2mib(tmp_path):
    def change(payloads):
        target_size = 3_046_086
        padding = b"<!--" + b"x" * (target_size - len(payloads["instance"]) - 7) + b"-->"
        payloads["instance"] = padding + payloads["instance"]
        assert len(payloads["instance"]) == target_size

    sources = inputs(tmp_path, change=change)
    package = produce(tmp_path, sources)
    assert package.package_identity
    mapping = {s.observation.observation_identity: (s.store, s.observation) for s in sources}
    restored = restore_sec_document_source_package(
        store=SnapshotStore(tmp_path / "package"),
        observation=package.observation,
        resolve_observation=mapping.__getitem__,
    )
    assert restored.package_identity == package.package_identity


@pytest.mark.parametrize(
    "role,before,after",
    [
        ("primary", b"<head>", b'<head><base href="https://outside.invalid/">'),
        ("primary", b"<body>", b'<body xml:base="https://outside.invalid/">'),
        ("instance", b"<xbrl ", b'<xbrl xml:base="https://outside.invalid/" '),
        ("schema", b"<xs:schema ", b'<xs:schema xml:base="https://outside.invalid/" '),
        ("presentation", b"<link:linkbase ", b'<link:linkbase xml:base="other/" '),
    ],
)
def test_base_uri_overrides_rejected(tmp_path, role, before, after):
    def change(payloads):
        assert before in payloads[role]
        payloads[role] = payloads[role].replace(before, after, 1)

    with pytest.raises(ValueError, match="base URI"):
        produce(tmp_path, inputs(tmp_path, change=change))


def test_supported_namespace_root_does_not_expand_each_scope():
    parser = _PrimaryReferences()
    attributes = " ".join(f'xmlns:unused{i}="urn:ignored:{i}"' for i in range(254))
    parser.feed(f'<html {attributes} xmlns:link="{LINK}" xmlns:xlink="{XLINK}">' + "<div>" * 120)
    assert len(parser.stack) == 121
    assert all(len(scope) == 2 for _, scope in parser.stack)
    parser.feed('<link:schemaRef xlink:href="fake.xsd"/>' + "</div>" * 120 + "</html>")
    parser.close()
    assert parser.references == ["fake.xsd"]
    assert not parser.stack


def test_namespace_shadow_is_not_resolved_from_global_declarations(tmp_path):
    def change(payloads):
        payloads["primary"] = payloads["primary"].replace(
            b"<body>", b'<body xmlns:link="urn:wrong">'
        )

    with pytest.raises(ValueError, match="namespace"):
        produce(tmp_path, inputs(tmp_path, change=change))


def test_declared_optional_role_must_be_retained(tmp_path, monkeypatch):
    monkeypatch.setitem(FILENAMES, "calculation", "fake_cal.xml")

    def change(payloads):
        ref = f'<link:linkbaseRef xmlns:link="{LINK}" xmlns:xlink="{XLINK}" xlink:href="fake_cal.xml" xlink:role="http://www.xbrl.org/2003/role/calculationLinkbaseRef"/>'
        payloads["schema"] = payloads["schema"].replace(
            b"</xs:appinfo>", ref.encode() + b"</xs:appinfo>"
        )
        payloads["calculation"] = f'<link:linkbase xmlns:link="{LINK}"/>'.encode()

    sources = inputs(tmp_path, change=change)
    package = produce(tmp_path, sources)
    assert len(json.loads(package.manifest)["sources"]) == 8
    with pytest.raises(ValueError, match="complete selected roles"):
        produce(tmp_path / "omitted", [s for s in sources if s.role != "calculation"])


@pytest.mark.parametrize(
    "role,extra",
    [
        ("primary", b"<br>" * 200_001),
        ("labels", b"<x/>" * 200_001),
    ],
)
def test_structure_count_limits_before_package_write(tmp_path, role, extra):
    def change(payloads):
        if role == "primary":
            payloads[role] = payloads[role].replace(b"</body>", extra + b"</body>")
        else:
            payloads[role] = payloads[role].replace(
                b"</link:linkbase>", extra + b"</link:linkbase>"
            )

    with pytest.raises(ValueError, match="limit"):
        produce(tmp_path, inputs(tmp_path, change=change))


@pytest.mark.parametrize(
    "kind", ["duplicate-index", "duplicate-filing", "unknown-field", "future-capture"]
)
def test_metadata_and_retained_manifest_claims_fail_closed(tmp_path, kind):
    def change(payloads):
        if kind == "duplicate-index":
            value = json.loads(payloads["index"])
            value["directory"]["item"].append(value["directory"]["item"][0])
            payloads["index"] = json.dumps(value).encode()
        elif kind == "duplicate-filing":
            value = json.loads(payloads["submissions"])
            for column in value["filings"]["recent"].values():
                column.append(column[0])
            payloads["submissions"] = json.dumps(value).encode()

    sources = inputs(tmp_path, change=change)
    if kind.startswith("duplicate"):
        with pytest.raises(ValueError):
            produce(tmp_path, sources)
        return
    package = produce(tmp_path, sources)
    value = json.loads(package.manifest)
    if kind == "unknown-field":
        value["unexpected"] = True
    else:
        value["captured_at"] = "2024-05-02T00:00:00Z"
    store = SnapshotStore(tmp_path / "changed")
    obs = store.observe(
        RequestSpec("sec", "company-filing-document-source-package", REQUEST),
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        CAPTURED,
        "sec-document-source-package-v1",
    )
    with pytest.raises(ValueError):
        restore_sec_document_source_package(
            store=store,
            observation=obs,
            resolve_observation=lambda _: pytest.fail("invalid envelope reached resolver"),
        )


@pytest.mark.parametrize("count,closed", [(256, False), (257, False), (256, True), (257, True)])
def test_per_element_attribute_boundary(count, closed):
    parser = _PrimaryReferences()
    attributes = " ".join(f'a{i}="x"' for i in range(count))
    raw = f"<div {attributes}" + ("/>" if closed else "></div>")
    if count == 256:
        parser.feed(raw)
        parser.close()
    else:
        with pytest.raises(ValueError, match="attribute limit"):
            parser.feed(raw)


def test_excessive_attributes_fail_before_package_retention(tmp_path):
    def change(payloads):
        attrs = " ".join(f'xmlns:unused{i}="urn:x"' for i in range(10_000)).encode()
        payloads["primary"] = payloads["primary"].replace(b"<html ", b"<html " + attrs + b" ")

    with pytest.raises(ValueError, match="attribute limit"):
        produce(tmp_path, inputs(tmp_path, change=change))
    assert not list((tmp_path / "package").rglob("response.bin"))


@pytest.mark.parametrize("divs", [126, 127])
def test_primary_depth_includes_self_closed_reference(divs):
    parser = _PrimaryReferences()
    raw = (
        f'<html xmlns:link="{LINK}" xmlns:xlink="{XLINK}">'
        + "<div>" * divs
        + '<link:schemaRef xlink:href="fake.xsd"/>'
        + "</div>" * divs
        + "</html>"
    )
    if divs == 126:
        parser.feed(raw)
        parser.close()
        assert parser.references == ["fake.xsd"]
    else:
        with pytest.raises(ValueError, match="structure limit"):
            parser.feed(raw)
