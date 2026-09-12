"""Complete retained document closure and lifecycle restart acceptance."""

import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotIntegrityError, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import (
    load_sec_document_financial_bundle as load,
)
from ohmydata.providers.sec import (
    select_sec_document_financial_productions as select,
)
from ohmydata.providers.sec import (
    write_sec_document_financial_bundle as write,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_document_financials import build
from test_document_source import CAPTURED
from test_observed_financial_bundle import _records


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network"))
    result, output, mapping, sources = build(tmp_path / "input")
    mapping[result.output_observation.observation_identity] = (output, result.output_observation)
    policy, quality, commit = _records(result)
    args = {
        "store": SnapshotStore(tmp_path / "bundle"),
        "batch_identity": "synthetic-batch",
        "productions": (result,),
        "quality_records": (quality,),
        "consumer_commits": (commit,),
        "captured_at": policy.knowledge_cutoff,
        "resolve_observation": mapping.__getitem__,
    }
    return args, policy, mapping, sources


def test_complete_readonly_restart_and_selection(case, monkeypatch):
    args, policy, mapping, _ = case
    ref = write(**args)
    monkeypatch.setattr(SnapshotStore, "write", lambda *_: pytest.fail("load write"))
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("load observe"))
    loaded = load(
        store=SnapshotStore(args["store"].root),
        bundle_ref=ref,
        resolve_observation=mapping.__getitem__,
    )
    assert loaded.productions == args["productions"]
    assert select(
        loaded.productions, loaded.quality_records, loaded.consumer_commits, policy
    ) == select(args["productions"], args["quality_records"], args["consumer_commits"], policy)


def test_concurrent_idempotence(case):
    args, _, _, _ = case
    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(lambda _: write(**args), range(2)))
    assert refs[0] == refs[1] == write(**args)
    assert len(list(args["store"].root.rglob("response.bin"))) == 1


@pytest.mark.parametrize("after", [False, True])
def test_interrupted_write_retry(case, monkeypatch, after):
    args, _, mapping, _ = case
    original = SnapshotStore.write

    def interrupted(self, *a, **kw):
        if after:
            original(self, *a, **kw)
        raise OSError("synthetic interruption")

    monkeypatch.setattr(SnapshotStore, "write", interrupted)
    with pytest.raises(OSError):
        write(**args)
    if not after:
        assert not list(args["store"].root.rglob("response.bin"))
    monkeypatch.setattr(SnapshotStore, "write", original)
    ref = write(**args)
    assert (
        load(
            store=args["store"], bundle_ref=ref, resolve_observation=mapping.__getitem__
        ).productions
        == args["productions"]
    )


def test_dependency_budget_counts_output_package_and_raw(case):
    args, _, mapping, _ = case
    total = sum(len(store.replay_observation(obs).payload) for store, obs in mapping.values())
    ref = write(**args, max_dependency_bytes=total)
    with pytest.raises((ValueError, SnapshotIntegrityError)):
        load(
            store=args["store"],
            bundle_ref=ref,
            resolve_observation=mapping.__getitem__,
            max_dependency_bytes=total - 1,
        )
    with pytest.raises((ValueError, SnapshotIntegrityError)):
        write(**args, max_dependency_bytes=1)


def test_missing_dependency_and_source_tampering(case):
    args, _, mapping, sources = case
    ref = write(**args)
    source = sources[-1]
    saved = mapping.pop(source.observation.observation_identity)
    with pytest.raises(KeyError):
        load(store=args["store"], bundle_ref=ref, resolve_observation=mapping.__getitem__)
    mapping[source.observation.observation_identity] = saved
    # observation.path belongs to observation ledger; locate the source snapshot
    payloads = list(source.store.root.rglob("response.bin"))
    assert payloads
    payloads[-1].write_bytes(b"synthetic-corruption")
    with pytest.raises(SnapshotIntegrityError):
        load(store=args["store"], bundle_ref=ref, resolve_observation=mapping.__getitem__)


@pytest.mark.parametrize(
    "mutation",
    ["extra", "identity", "receipt", "capture", "batch", "duplicate", "quality", "commit"],
)
def test_valid_hash_does_not_admit_semantic_tamper(case, tmp_path, mutation):
    args, _, mapping, _ = case
    ref = write(**args)
    data = json.loads(args["store"].replay(ref).payload)
    if mutation == "extra":
        data["extra"] = True
    elif mutation == "identity":
        data["productions"][0]["production_identity"] = "f" * 64
    elif mutation == "receipt":
        data["productions"][0]["output_receipt"]["fact_version"] = "f" * 64
    elif mutation == "capture":
        data["captured_at"] = "2024-01-01T00:00:00Z"
    elif mutation == "batch":
        data["batch_identity"] = "different"
    elif mutation == "duplicate":
        data["productions"] *= 2
    elif mutation == "quality":
        data["quality_records"][0]["quality_record_id"] = "f" * 64
    elif mutation == "commit":
        data["consumer_commits"][0]["production_identity"] = "f" * 64
    forged = SnapshotStore(tmp_path / "forged")
    other = forged.write(
        RequestSpec("sec", "document-financial-bundle", {"batch_identity": args["batch_identity"]}),
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
        args["captured_at"],
        "sec-document-financial-bundle-v1",
        SnapshotMode.FROZEN,
    )
    with pytest.raises(ValueError):
        load(store=forged, bundle_ref=other, resolve_observation=mapping.__getitem__)


@pytest.mark.parametrize("failure", ["quality", "commit", "capture", "count", "bytes", "seal"])
def test_invalid_writer_input_never_writes(case, failure):
    args, _, _, _ = case
    args = dict(args)
    if failure == "quality":
        args["quality_records"] = (
            replace(args["quality_records"][0], production_identity="f" * 64),
        )
    elif failure == "commit":
        args["consumer_commits"] = (
            replace(args["consumer_commits"][0], quality_record_id="f" * 64),
        )
    elif failure == "capture":
        args["captured_at"] = args["productions"][0].produced_at - timedelta(seconds=1)
    elif failure == "count":
        args["productions"] *= 11
    elif failure == "bytes":
        args["max_bundle_bytes"] = 1
    elif failure == "seal":
        object.__setattr__(args["productions"][0], "production_identity", "f" * 64)
    with pytest.raises(ValueError):
        write(**args)
    assert not list(args["store"].root.rglob("response.bin"))


def test_resolver_must_not_change_receipt_or_path(case):
    from ohmydata.providers.sec._document_bundle_codec import Dependencies

    args, _, mapping, _ = case
    result = args["productions"][0]
    identity = result.output_observation.observation_identity
    dependencies = Dependencies(mapping.__getitem__, 32 * 1024 * 1024)
    store, observation = dependencies.resolve(identity)
    assert dependencies.used == len(store.replay_observation(observation).payload)
    assert dependencies.resolve(identity) == (store, observation)
    mapping[identity] = (
        store,
        replace(observation, path=observation.path.parent / "synthetic-other.json"),
    )
    with pytest.raises(ValueError, match="changed receipt or path"):
        dependencies.resolve(identity)


def test_empty_bundle_and_bounded_generator(case):
    args, _, mapping, _ = case
    result = args["productions"][0]
    consumed = []

    def stream():
        for number in range(100):
            consumed.append(number)
            yield result

    with pytest.raises(ValueError, match="limit"):
        write(**(args | {"productions": stream()}))
    assert len(consumed) == 11
    empty = args | {"productions": (), "quality_records": (), "consumer_commits": ()}
    ref = write(**empty)
    loaded = load(store=args["store"], bundle_ref=ref, resolve_observation=mapping.__getitem__)
    assert loaded.productions == loaded.quality_records == loaded.consumer_commits == ()


def test_exact_4mib_instance_roundtrip_and_4mib_plus_one_rejection(tmp_path):
    def change_exact(payloads):
        target = 4 * 1024 * 1024
        padding = b"<!--" + b"x" * (target - len(payloads["instance"]) - 7) + b"-->"
        payloads["instance"] = padding + payloads["instance"]
        assert len(payloads["instance"]) == target

    result, output, mapping, _sources = build(tmp_path / "exact_input", change=change_exact)
    mapping[result.output_observation.observation_identity] = (output, result.output_observation)
    bundle_store = SnapshotStore(tmp_path / "exact_bundle")
    ref = write(
        store=bundle_store,
        batch_identity="exact-4mib-batch",
        productions=(result,),
        quality_records=(),
        consumer_commits=(),
        captured_at=CAPTURED + timedelta(hours=2),
        resolve_observation=mapping.__getitem__,
    )
    loaded = load(
        store=bundle_store,
        bundle_ref=ref,
        resolve_observation=mapping.__getitem__,
    )
    assert loaded.productions[0].production_identity == result.production_identity
    assert loaded.productions[0].vintage.rows == result.vintage.rows

    def change_over(payloads):
        target = 4 * 1024 * 1024 + 1
        padding = b"<!--" + b"x" * (target - len(payloads["instance"]) - 7) + b"-->"
        payloads["instance"] = padding + payloads["instance"]
        assert len(payloads["instance"]) == 4194305

    with pytest.raises(SnapshotIntegrityError):
        build(tmp_path / "over_input", change=change_over)
