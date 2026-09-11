"""Strict retained XBRL package codec tests."""

import base64
import json
from datetime import UTC, datetime

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.providers.sec import (
    SecXbrlPackageComponents,
    decode_sec_xbrl_package,
    serialize_sec_xbrl_package,
)


def _observation(tmp_path):
    store = SnapshotStore(tmp_path / "source")
    return store.observe(
        RequestSpec(
            "sec",
            "company-filing-sgml",
            {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"},
        ),
        b"synthetic SGML",
        datetime(2024, 5, 1, 22, tzinfo=UTC),
        "sec-filing-sgml-v1",
    )


def _components(*, instance: bytes = b"<xbrl/>"):
    return SecXbrlPackageComponents(b"<schema/>", b"<presentation/>", b"<labels/>", instance)


def _payload(tmp_path):
    return serialize_sec_xbrl_package(
        sgml_observation=_observation(tmp_path),
        cik="0000000001",
        accession_number="0000000001-24-000001",
        form="10-Q",
        source_available_at=datetime(2024, 5, 1, 22, tzinfo=UTC),
        components=_components(),
    )


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("payload", [b"{", b"\xff"])
def test_decoder_rejects_malformed_json_and_utf8(payload):
    with pytest.raises(ValueError, match="invalid SEC XBRL package"):
        decode_sec_xbrl_package(payload)


def test_decoder_rejects_duplicate_json_key(tmp_path):
    payload = _payload(tmp_path).decode()
    duplicated = payload.replace(
        '"schema":"sec-xbrl-package-v1"',
        '"schema":"sec-xbrl-package-v1","schema":"sec-xbrl-package-v1"',
    )
    with pytest.raises(ValueError, match="duplicate"):
        decode_sec_xbrl_package(duplicated.encode())


def test_decoder_rejects_noncanonical_base64_unknown_and_missing_components(tmp_path):
    envelope = json.loads(_payload(tmp_path))
    envelope["components"]["schema"] = "AB=="
    with pytest.raises(ValueError, match="base64"):
        decode_sec_xbrl_package(_canonical(envelope))

    envelope = json.loads(_payload(tmp_path))
    envelope["components"]["unexpected"] = base64.b64encode(b"<unexpected/>").decode()
    with pytest.raises(ValueError, match="components"):
        decode_sec_xbrl_package(_canonical(envelope))

    envelope = json.loads(_payload(tmp_path))
    del envelope["components"]["labels"]
    with pytest.raises(ValueError, match="components"):
        decode_sec_xbrl_package(_canonical(envelope))


@pytest.mark.parametrize("component", [b"<!DOCTYPE x><x/>", b"<x>"])
def test_decoder_rejects_unsafe_or_malformed_component_xml(tmp_path, component):
    envelope = json.loads(_payload(tmp_path))
    envelope["components"]["instance"] = base64.b64encode(component).decode()
    with pytest.raises(ValueError, match="(unsafe|malformed)"):
        decode_sec_xbrl_package(_canonical(envelope))


def test_decoder_enforces_component_and_aggregate_xml_limits(tmp_path):
    envelope = json.loads(_payload(tmp_path))
    envelope["components"]["instance"] = base64.b64encode(
        b"<x>" + b"a" * (2 * 1024 * 1024) + b"</x>"
    ).decode()
    with pytest.raises(ValueError, match="component limit"):
        decode_sec_xbrl_package(_canonical(envelope))

    component = b"<x>" + b"<a/>" * 50_000 + b"</x>"
    envelope = json.loads(_payload(tmp_path))
    encoded = base64.b64encode(component).decode()
    for name in ("schema", "presentation", "labels", "instance"):
        envelope["components"][name] = encoded
    with pytest.raises(ValueError, match="aggregate"):
        decode_sec_xbrl_package(_canonical(envelope))


def test_decoder_rejects_unknown_envelope_fields_and_noncanonical_json(tmp_path):
    envelope = json.loads(_payload(tmp_path))
    envelope["extra"] = "no"
    with pytest.raises(ValueError, match="envelope"):
        decode_sec_xbrl_package(_canonical(envelope))

    with pytest.raises(ValueError, match="noncanonical"):
        decode_sec_xbrl_package(_payload(tmp_path) + b" ")
