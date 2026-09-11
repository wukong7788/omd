"""Atomic publication guarantees for v2 finding receipt bundles."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.core import snapshot as snapshot_module
from ohmydata.providers.sec import (
    SecQualityEvidenceRef,
    SecQualityFinding,
    SecQualityFindingStatus,
    SecQualityIssueClass,
    load_sec_pit_bundle,
    write_sec_pit_bundle,
)
from tests.providers.sec.test_pit_bundle import _version


def _finding(version):
    return SecQualityFinding(
        version.normalized_version_id,
        ("concept",),
        "source",
        "rule",
        "v1",
        version.adapter_version,
        SecQualityIssueClass.UNKNOWN,
        None,
        "Synthetic assertion.",
        (
            SecQualityEvidenceRef(
                version.observation.observation_identity, version.observation.fact_version
            ),
        ),
        SecQualityFindingStatus.OPEN,
        datetime(2024, 5, 2, 10, tzinfo=UTC),
        datetime(2024, 5, 2, 11, tzinfo=UTC),
    )


def test_v2_concurrent_and_interrupted_publication_preserves_old_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    store = SnapshotStore(tmp_path / "bundle")
    finding = _finding(version)

    def write(batch: str = "v2"):
        return write_sec_pit_bundle(
            store=store,
            batch_identity=batch,
            versions=[version],
            quality_records=[],
            quality_findings=[finding],
            source_store=source,
            resolve_observation=lambda _: version.observation,
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda _: write(), range(4)))
    assert all(ref == refs[0] for ref in refs)
    old = store.replay(refs[0]).payload

    def interrupt(*args, **kwargs):
        raise OSError("synthetic interrupted atomic publication")

    with monkeypatch.context() as patch:
        patch.setattr(snapshot_module.os, "rename", interrupt)
        with pytest.raises(OSError, match="synthetic interrupted"):
            write("interrupted")
    assert store.replay(refs[0]).payload == old
    assert not list(store.root.rglob(".tmp-*"))
    recovered = write("interrupted")
    loaded = load_sec_pit_bundle(
        store=SnapshotStore(store.root),
        bundle_ref=recovered,
        source_store=SnapshotStore(source.root),
        resolve_observation=lambda _: version.observation,
    )
    assert loaded.quality_findings == (finding,)
