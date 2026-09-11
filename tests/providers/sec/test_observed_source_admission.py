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
            "0e8b0e409206f5b9579668529e5d36f44fc0c5f82e15e40a102eae108c04522d",
            "08a31b4a49b97d2dca3fc66a0beaa071ac5f3e010028d10e73768f049eb81aba",
            "386c7cae5da25d577a6879465ea75fdd508ef40bb5c0b0a3fc696b238753a860",
        ),
        (
            "1f5566f7ac6f0ad476f817f7aeaff16d6d884a723dc881b0f637fe0e6d554ecd",
            "7e845bcab735d17be9b0b690a5886ac416f8e62367581168479ecbc18f867b62",
            "ca1d3ad029a11c7e06f934c0ce31de405ca487841fe2be2a245aa91cf1f14f86",
        ),
    ]
    productions, receipts = [], {}
    for version, golden in enumerate(expected, 1):
        result, source, sr, packages, pr, _, output = _produce(
            tmp_path / str(version),
            parser_version=f"sec-observed-xbrl-financial-parser-v{version}-edgartools-5.56.0",
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
    assert ref.response_sha256 == "7afc6293800bd2941cc261110e13dcaa5d1707e4b0e7dfe5cb28fc094ec327d0"
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
