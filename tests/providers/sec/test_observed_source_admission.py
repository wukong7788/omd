"""Unchanged source admission, span validation and historical identity regressions."""

import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ohmydata.core import SnapshotIntegrityError, SnapshotStore
from ohmydata.providers.sec import (
    load_sec_observed_financial_bundle,
    produce_sec_financials_from_observed_xbrl_package,
    write_sec_observed_financial_bundle,
)
from ohmydata.providers.sec.sgml_financials import _documents, _validate_component_span

sys.path.insert(0, str(Path(__file__).parent))
from test_observed_xbrl_financials import _produce, _raw, _request, _setup

pytest.importorskip("edgar")
MIB = 1024 * 1024
SOURCE_TIME = datetime(2024, 5, 1, 22, tzinfo=UTC)
PACKAGE_TIME = datetime(2024, 5, 1, 22, 30, tzinfo=UTC)
PRODUCED = datetime(2024, 5, 1, 23, tzinfo=UTC)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def padded_source(size):
    prefix = _raw() + b"\n<DOCUMENT>\n<TYPE>EX-99\n<TEXT>" + "😀".encode()
    suffix = b"</TEXT>\n</DOCUMENT>\n"
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


@pytest.mark.parametrize(
    "size,limit,accepted",
    [
        (8 * MIB, 8 * MIB, True),
        (8 * MIB + 1, 8 * MIB, False),
    ],
)
def test_existing_source_boundaries_and_bundle_restore(tmp_path, size, limit, accepted):
    source, sr, packages, pr, _ = _setup(
        tmp_path, source_time=SOURCE_TIME, package_time=PACKAGE_TIME, raw=padded_source(size)
    )
    output = SnapshotStore(tmp_path / "output")
    args = {
        "source_store": source,
        "source_observation": sr,
        "package_store": packages,
        "package_observation": pr,
        "output_store": output,
        "request": _request(),
        "produced_at": PRODUCED,
        "max_raw_bytes": limit,
    }
    if not accepted:
        with pytest.raises(SnapshotIntegrityError):
            produce_sec_financials_from_observed_xbrl_package(**args)
        assert not list(output.root.rglob("response.bin"))
        return
    production = produce_sec_financials_from_observed_xbrl_package(**args)
    receipts = {
        ref.observation_identity: (store, ref)
        for store, ref in [(source, sr), (packages, pr), (output, production.output_observation)]
    }
    store = SnapshotStore(tmp_path / "bundle")
    bundle = write_sec_observed_financial_bundle(
        store=store,
        batch_identity="synthetic-large-source",
        productions=(production,),
        quality_records=(),
        consumer_commits=(),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
    )
    if size > 8 * MIB:
        with pytest.raises(SnapshotIntegrityError):
            load_sec_observed_financial_bundle(
                store=store, bundle_ref=bundle, resolve_observation=receipts.__getitem__
            )
    restored = load_sec_observed_financial_bundle(
        store=store,
        bundle_ref=bundle,
        resolve_observation=receipts.__getitem__,
    )
    assert restored.productions[0].production_identity == production.production_identity


def test_old_v1_v2_golden_identities_ignore_admission_controls(tmp_path):
    expected = [
        (
            "a3200adee61015b50aae4ab87b2a94a363e5b297acb5f9d4a2226ff269884a2b",
            "846bd84f45a39017101911f24d2a52d9550cf354d5042dce80c99b280bbf5b17",
            "878a7b60e8f6c0e62f90e7cbb4f4bf18b0b948a45fa7d0e2d5213bceaf0992f0",
        ),
        (
            "d5b0de9de4cb9a38d6aa549576d2cd2258ead5624a89c8191c3fb1cf30657dbd",
            "bd05b790ac83e8288fb3d58e182fd301b3deb0f312e23a1a70d49a7fc6a2ce6f",
            "7cfc76f950acc27514ee8e89e2a12b78b0636a9ac12d734eda4d80c948fca43f",
        ),
    ]
    productions, receipts = [], {}
    for version, golden in enumerate(expected, 1):
        result, source, sr, packages, pr, _, output = _produce(
            tmp_path / str(version),
            parser_version=f"sec-observed-xbrl-financial-parser-v{version}-edgartools-6.4.0",
            max_raw_bytes=8 * MIB,
        )
        assert (
            result.output_observation.response_sha256,
            result.output_observation.fact_version,
            result.production_identity,
        ) == golden
        productions.append(result)
        receipts.update(
            {
                r.observation_identity: (s, r)
                for s, r in [(source, sr), (packages, pr), (output, result.output_observation)]
            }
        )
    store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_observed_financial_bundle(
        store=store,
        batch_identity="synthetic-admission-golden",
        productions=productions,
        quality_records=(),
        consumer_commits=(),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
    )
    assert ref.response_sha256 == "af14cb203336de9539a7da6527c5f645c64121f7e8f2a165ee96d3bf12d2f385"
    assert (
        len(
            load_sec_observed_financial_bundle(
                store=store, bundle_ref=ref, resolve_observation=receipts.__getitem__
            ).productions
        )
        == 2
    )


@pytest.mark.parametrize("separator", ["", "\n", "\r\n", "\u2003"])
def test_document_scanner_preserves_start_anchor_and_unicode_whitespace(separator):
    raw = f"<DOCUMENT>{separator}<TYPE>EX-99\n<TEXT>😀</TEXT></DOCUMENT>"
    assert _documents(raw, require_traditional=False) == {}


@pytest.mark.parametrize(
    "body",
    [
        "<TYPE>EX-99\n<TEXT>x</TEXT><TEXT>y</TEXT>",
        "<TYPE>EX-99\n<TYPE>EX-99\n<TEXT>x</TEXT>",
        "<TEXT>x</TEXT>",
        "<TYPE>EX-99\nx",
        "<TYPE>EX-99\n<TEXT>x",
        "<TYPE>EX-99\n<TEXT><DOCUMENT>x</DOCUMENT></TEXT>",
        "<TYPE>EX-101.CAL\n<TEXT><XBRL>x</TEXT>",
    ],
)
def test_unused_malformed_document_still_rejected(body):
    with pytest.raises(ValueError):
        _documents(f"<DOCUMENT>\n{body}</DOCUMENT>", require_traditional=False, validate_only=True)


def test_document_count_and_duplicate_unused_components():
    block = "<DOCUMENT>\n<TYPE>EX-99\n<TEXT>x</TEXT></DOCUMENT>"
    assert _documents(block * 64, require_traditional=False, validate_only=True) == {}
    with pytest.raises(ValueError):
        _documents(block * 65, require_traditional=False, validate_only=True)
    with pytest.raises(ValueError, match="duplicate"):
        _documents(
            block.replace("EX-99", "EX-101.CAL") * 2, require_traditional=False, validate_only=True
        )


@pytest.mark.parametrize(
    "text,valid",
    [
        ("", True),
        (" \u2003", True),
        ("😀", True),
        (" \u2003<XBRL>😀</XBRL>\r\n", True),
        ("<XBRL></XBRL>", False),
        ("<XBRL>\u2003 \n</XBRL>", False),
        ("<XBRL>x", False),
        ("x</XBRL>", False),
        ("<XBRL><XBRL>x</XBRL></XBRL>", False),
        ("prefix<XBRL>x</XBRL>suffix", True),
    ],
)
def test_wrapper_span_preserves_empty_unwrapped_and_wrapped_semantics(text, valid):
    raw = "ignored<XBRL>" + text + "</XBRL>ignored"
    start = len("ignored<XBRL>")
    if valid:
        _validate_component_span(raw, start, start + len(text))
    else:
        with pytest.raises(ValueError, match="wrapper"):
            _validate_component_span(raw, start, start + len(text))
