# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownLambdaType=false, reportArgumentType=false, reportUnnecessaryIsInstance=false
"""Capture-path tests: identities, replay, tamper, mismatch, revision lineage."""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from ohmydata.core import SnapshotStore
from ohmydata.core.errors import SnapshotIntegrityError
from ohmydata.providers.tushare import (
    EmptyPolicy,
    EtfBasicRequest,
    TushareClient,
    TushareFetchResult,
)
from ohmydata.providers.tushare.observations import (
    SERIALIZATION_IDENTIFIER,
    TushareObservedResult,
    capture_tushare_result,
    serialize_tushare_frame,
)

OBSERVED_AT = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


def etf_frame(rows):
    fields = EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH").fields
    return pd.DataFrame([{**dict.fromkeys(fields), **row} for row in rows], columns=fields)


class EndpointFake:
    def __init__(self, frame_factory):
        self.frame_factory = frame_factory
        self.calls = []

    def etf_basic(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame_factory()


def fetch_result(frame, request):
    client = TushareClient(EndpointFake(lambda: frame), clock=lambda: OBSERVED_AT)
    return client.fetch_etf_basic(request)


def make_request(ts_code="510050.SH"):
    return EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code=ts_code)


def make_observation(store, frame, request, when=OBSERVED_AT):
    result = fetch_result(frame, request)
    return capture_tushare_result(store, request, result, observed_at=when)


def test_capture_returns_defensive_observed_result(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    frame = etf_frame(
        [
            {
                "ts_code": "510050.SH",
                "index_code": "000016.SH",
                "list_status": "L",
                "csname": "上证50ETF",
            }
        ]
    )
    observed = make_observation(store, frame, request)
    assert isinstance(observed, TushareObservedResult)
    assert observed.row_count == 1
    assert observed.columns == tuple(frame.columns)
    assert observed.serialization_identifier == SERIALIZATION_IDENTIFIER
    assert len(observed.content_sha256) == 64
    assert observed.content_sha256 == observed.response_sha256
    assert observed.request_identity == request.spec.request_identity
    assert observed.snapshot_identity == observed.observation.snapshot_identity
    assert observed.fact_version == observed.observation.fact_version
    assert observed.snapshot_fetched_at == OBSERVED_AT
    assert observed.provenance.request_identity == request.spec.request_identity
    # defensive copy: mutating the returned frame does not affect internals
    mutated = observed.frame
    mutated.loc[0, "csname"] = "tampered"
    assert observed.frame.iloc[0]["csname"] == "上证50ETF"


def test_capture_requires_aware_observed_at(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    result = fetch_result(etf_frame([{"ts_code": "510050.SH"}]), request)
    with pytest.raises(TypeError):
        capture_tushare_result(
            store,
            request,
            result,
            observed_at=datetime(2024, 1, 2),  # noqa: DTZ001 - rejected input
        )
    with pytest.raises(TypeError):
        capture_tushare_result(store, request, result, observed_at=None)


def test_capture_rejects_provenance_request_mismatch(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    other = make_request(ts_code="510300.SH")
    result = fetch_result(etf_frame([{"ts_code": "510300.SH"}]), other)
    with pytest.raises(ValueError, match="does not match"):
        capture_tushare_result(store, request, result, observed_at=OBSERVED_AT)


def test_capture_is_append_only_and_replays(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    frame = etf_frame([{"ts_code": "510050.SH", "index_code": "000016.SH"}])
    observed = make_observation(store, frame, request)
    replay = store.replay_observation(observed.observation, request.spec)
    assert replay.payload == serialize_tushare_frame(frame)
    assert replay.manifest["mode"] == "append"
    # replay validation catches tampered snapshots
    response_bins = list(store.root.rglob("response.bin"))
    assert len(response_bins) == 1
    response_bins[0].write_bytes(b"tampered")
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(observed.observation, request.spec)


def test_capture_preserves_unchanged_and_revised_observations(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    original = etf_frame([{"ts_code": "510050.SH", "index_code": "000016.SH"}])
    revised = etf_frame([{"ts_code": "510050.SH", "index_code": "000010.SH"}])
    first = make_observation(store, original, request, when=OBSERVED_AT)
    later = make_observation(
        store, revised, request, when=datetime(2024, 1, 3, 3, 4, 5, tzinfo=UTC)
    )
    # same request identity, different payload -> different snapshots, both retained
    assert first.snapshot_identity != later.snapshot_identity
    assert first.observation_identity != later.observation_identity
    assert first.fact_version != later.fact_version
    # unchanged capture at a later time keeps both observations, no overwrite
    unchanged = make_observation(
        store, original, request, when=datetime(2024, 1, 4, 3, 4, 5, tzinfo=UTC)
    )
    assert unchanged.snapshot_identity == first.snapshot_identity
    assert unchanged.observation_identity != first.observation_identity
    assert unchanged.fact_version == first.fact_version


def test_capture_never_calls_provider_or_reads_credentials(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    frame = etf_frame([{"ts_code": "510050.SH"}])
    fake = EndpointFake(lambda: frame)
    client = TushareClient(fake, clock=lambda: OBSERVED_AT)
    result = client.fetch_etf_basic(request)
    assert fake.calls  # fetch happened before capture
    capture_tushare_result(store, request, result, observed_at=OBSERVED_AT)
    assert len(fake.calls) == 1  # capture itself made no provider call


def test_capture_rejects_mismatched_result_frame_shape(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    frame = etf_frame([{"ts_code": "510050.SH"}])
    result = fetch_result(frame, request)
    # provenance already built from the real frame; forge a result with a different frame
    forged = TushareFetchResult(
        frame.drop(columns=["csname"]),
        result.provenance,
        result.page_count,
    )
    with pytest.raises(ValueError, match="frame shape"):
        capture_tushare_result(store, request, forged, observed_at=OBSERVED_AT)


def test_capture_rejects_forged_provenance_identity(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    other_request = make_request(ts_code="510300.SH")
    other_result = fetch_result(etf_frame([{"ts_code": "510300.SH"}]), other_request)
    with pytest.raises(ValueError, match="does not match"):
        capture_tushare_result(store, request, other_result, observed_at=OBSERVED_AT)


def test_observed_result_rejects_inconsistent_inputs(tmp_path):
    store = SnapshotStore(Path(tmp_path))
    request = make_request()
    frame = etf_frame([{"ts_code": "510050.SH"}])
    observed = make_observation(store, frame, request)
    with pytest.raises(ValueError, match="content_sha256"):
        TushareObservedResult(
            frame,
            observed.provenance,
            observed.observation,
            observed.availability,
            SERIALIZATION_IDENTIFIER,
            "0" * 64,
        )
