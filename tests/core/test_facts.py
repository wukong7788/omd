from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityPrecision,
    RawFactEnvelope,
    RawFactQualityFlag,
    RawFactRevisionStatus,
    RequestSpec,
    SnapshotIntegrityError,
    SnapshotStore,
)


def _envelope(
    tmp_path: Path,
    payload: bytes = b"raw",
    *,
    fetched: datetime | None = None,
    row: dict[str, Any] | None = None,
) -> RawFactEnvelope:
    store = SnapshotStore(tmp_path)
    observation = store.observe(
        RequestSpec("synthetic", "bars", {}, ()),
        payload,
        fetched or datetime(2024, 1, 1, tzinfo=UTC),
        "json-v1",
    )
    return RawFactEnvelope.from_observation(
        store,
        observation,
        native_fields=row or {"symbol": "FAKE", "value": float("nan")},
        primary_key_fields=("symbol",),
        entity_fields=("symbol",),
        native_schema_version="row-v1",
        adapter_version="adapter-v1",
    )


def test_round_trip_hash_flags_and_defensive_serialization(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    assert envelope.revision_status is RawFactRevisionStatus.UNCLASSIFIED
    assert RawFactQualityFlag.PIT_UNPROVEN.value in envelope.quality_flags
    assert envelope.native_fields["symbol"] == "FAKE"
    with pytest.raises((TypeError, ValueError)):
        envelope.native_fields["x"] = 1  # type: ignore[index]
    data = envelope.to_dict()
    data["native_fields"]["symbol"] = "MUTATED"
    assert envelope.native_fields["symbol"] == "FAKE"
    assert data["row_payload_sha256"] == envelope.row_payload_sha256


def test_row_hash_is_order_independent_and_classification_is_relative(tmp_path: Path) -> None:
    first = _envelope(tmp_path / "first", row={"symbol": "FAKE", "value": 1})
    second = _envelope(
        tmp_path / "second",
        payload=b"new",
        fetched=datetime(2024, 1, 2, tzinfo=UTC),
        row={"value": 1, "symbol": "FAKE"},
    )
    assert first.row_payload_sha256 == second.row_payload_sha256
    classified = second.classify_against(first)
    assert classified.revision_status is RawFactRevisionStatus.UNCHANGED_FROM_PREVIOUS
    assert classified.previous_fact_version == first.fact_version


def test_date_only_and_additional_quality_flags(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    observation = store.observe(
        RequestSpec("synthetic", "bars", {}, ()),
        b"raw",
        datetime(2024, 1, 1, tzinfo=UTC),
        "json-v1",
    )
    envelope = RawFactEnvelope.from_observation(
        store,
        observation,
        native_fields={"symbol": "FAKE"},
        primary_key_fields=("symbol",),
        entity_fields=("symbol",),
        native_schema_version="row-v1",
        adapter_version="adapter-v1",
        source_available_at=date(2024, 1, 1),
        availability_basis=AvailabilityBasis.SOURCE_DECLARED,
        availability_precision=AvailabilityPrecision.DATE,
        additional_quality_flags=("CALLER_NOTE",),
    )
    assert RawFactQualityFlag.DATE_ONLY_AVAILABILITY.value in envelope.quality_flags
    assert "CALLER_NOTE" in envelope.quality_flags


def test_fund_nav_date_only_evidence_is_not_intraday_or_historical_vintage(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    observation = store.observe(
        RequestSpec("tushare", "fund_nav", {}, ()),
        b"raw",
        datetime(2024, 1, 4, tzinfo=UTC),
        "json-v1",
    )
    envelope = RawFactEnvelope.from_observation(
        store,
        observation,
        native_fields={
            "ts_code": "FAKE",
            "ann_date": date(2024, 1, 3),
            "nav_date": date(2000, 1, 1),
        },
        primary_key_fields=("ts_code", "nav_date", "ann_date"),
        entity_fields=("ts_code",),
        native_schema_version="row-v1",
        adapter_version="adapter-v1",
        source_available_at=date(2024, 1, 3),
        availability_basis=AvailabilityBasis.SOURCE_DECLARED,
        availability_precision=AvailabilityPrecision.DATE,
    )
    assert envelope.availability.availability_precision is AvailabilityPrecision.DATE
    assert RawFactQualityFlag.DATE_ONLY_AVAILABILITY.value in envelope.quality_flags
    assert RawFactQualityFlag.PIT_UNPROVEN.value in envelope.quality_flags
    revised = envelope.classify_against(envelope)
    assert revised.revision_status is RawFactRevisionStatus.UNCHANGED_FROM_PREVIOUS


def test_exact_derived_metadata_and_json_safe_values(tmp_path: Path) -> None:
    envelope = _envelope(
        tmp_path,
        row={
            "symbol": "FAKE",
            "when": datetime(2024, 1, 1, 8, tzinfo=UTC),
            "day": date(2024, 1, 1),
            "n": None,
            "nan": float("nan"),
            "pos": float("inf"),
            "neg": float("-inf"),
        },
    )
    assert envelope.provider == "synthetic"
    assert envelope.endpoint == "bars"
    assert envelope.payload_hash == envelope.observation.response_sha256
    assert envelope.snapshot_id == envelope.observation.snapshot_identity
    assert envelope.fact_version == envelope.observation.fact_version
    assert envelope.serialization_identifier == "json-v1"
    assert envelope.to_dict()["envelope_schema_version"] == 1
    assert envelope.to_dict()["native_fields"]["when"] == {"__datetime__": "2024-01-01T08:00:00Z"}
    assert envelope.to_dict()["native_fields"]["nan"] == {"__float__": "nan"}


def test_mapping_order_and_timezone_hash_equivalence(tmp_path: Path) -> None:
    a = _envelope(tmp_path / "a", row={"symbol": "FAKE", "x": 1})
    b = _envelope(tmp_path / "b", row={"x": 1, "symbol": "FAKE"})
    assert a.row_payload_sha256 == b.row_payload_sha256
    c = _envelope(
        tmp_path / "c", row={"symbol": "FAKE", "when": datetime(2024, 1, 1, 8, tzinfo=UTC)}
    )
    d = _envelope(
        tmp_path / "d",
        row={
            "symbol": "FAKE",
            "when": datetime(2024, 1, 1, 16, tzinfo=timezone(timedelta(hours=8))),
        },
    )
    assert c.row_payload_sha256 == d.row_payload_sha256


def test_pairwise_distinct_scalar_hashes(tmp_path: Path) -> None:
    values = [
        None,
        0,
        float("nan"),
        float("inf"),
        float("-inf"),
        date(2024, 1, 1),
        datetime(2024, 1, 1, tzinfo=UTC),
    ]
    hashes = [
        _envelope(tmp_path / str(i), row={"symbol": "FAKE", "value": value}).row_payload_sha256
        for i, value in enumerate(values)
    ]
    assert len(set(hashes)) == len(hashes)


def test_field_order_and_source_mapping_isolation(tmp_path: Path) -> None:
    source = {"symbol": "FAKE", "first": 1, "second": 2}
    envelope = _envelope(tmp_path, row=source)
    assert tuple(envelope.native_fields) == ("symbol", "first", "second")
    source["first"] = 99
    assert envelope.native_fields["first"] == 1


@pytest.mark.parametrize("entity", [(), ("symbol", "symbol")])
def test_entity_key_rejections(tmp_path: Path, entity: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        store = SnapshotStore(tmp_path)
        obs = store.observe(
            RequestSpec("synthetic", "bars", {}, ()),
            b"raw",
            datetime(2024, 1, 1, tzinfo=UTC),
            "json-v1",
        )
        RawFactEnvelope.from_observation(
            store,
            obs,
            native_fields={"symbol": "FAKE"},
            primary_key_fields=("symbol",),
            entity_fields=entity,
            native_schema_version="row-v1",
            adapter_version="adapter-v1",
        )


def test_additional_flag_validation(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    obs = store.observe(
        RequestSpec("synthetic", "bars", {}, ()),
        b"raw",
        datetime(2024, 1, 1, tzinfo=UTC),
        "json-v1",
    )
    for flags in (("bad-flag",), ("NOTE", "NOTE"), ("PIT_UNPROVEN",)):
        with pytest.raises(ValueError):
            RawFactEnvelope.from_observation(
                store,
                obs,
                native_fields={"symbol": "FAKE"},
                primary_key_fields=("symbol",),
                entity_fields=("symbol",),
                native_schema_version="row-v1",
                adapter_version="adapter-v1",
                additional_quality_flags=flags,
            )


def test_public_revision_fields_are_not_constructor_keywords(tmp_path: Path) -> None:
    e = _envelope(tmp_path)
    base = {
        "observation": e.observation,
        "availability": e.availability,
        "native_fields": e.native_fields,
        "primary_key_fields": ("symbol",),
        "entity_fields": ("symbol",),
        "native_schema_version": "row-v1",
        "adapter_version": "adapter-v1",
    }
    for key, value in (
        ("row_payload_sha256", "0" * 64),
        ("revision_status", RawFactRevisionStatus.REVISED_FROM_PREVIOUS),
        ("previous_fact_version", "0" * 64),
        ("previous_row_payload_sha256", "0" * 64),
    ):
        with pytest.raises(TypeError):
            RawFactEnvelope(**base, **{key: value})  # type: ignore[arg-type]


def test_serialization_is_json_strict_and_has_no_sensitive_fields(tmp_path: Path) -> None:
    import json

    data = _envelope(tmp_path, row={"symbol": "FAKE", "value": float("nan")}).to_dict()
    json.dumps(data, allow_nan=False)
    text = json.dumps(data)
    assert all(
        token not in text
        for token in ("response.bin", "first_usable_session", "credential", "payload_bytes")
    )


@pytest.mark.parametrize(
    "row",
    [
        {"symbol": []},
        {"symbol": {"nested": 1}},
        {"symbol": b"x"},
        {"symbol": datetime(2024, 1, 1, tzinfo=UTC).replace(tzinfo=None)},
    ],
)
def test_unsupported_nested_and_naive_values_rejected(tmp_path: Path, row: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _envelope(tmp_path, row=row)


@pytest.mark.parametrize(
    "field",
    [
        "token",
        "api_key",
        "secret",
        "password",
        "api_token",
        "access_token",
        "client_secret",
        "foo_password",
        "x_api_key",
        "proxy_authorization",
        "set_cookie",
        "X-API.KEY",
        "CLIENT.Secret",
    ],
)
def test_secret_like_exact_field_names_rejected_but_secretary_allowed(
    tmp_path: Path, field: str
) -> None:
    with pytest.raises(ValueError):
        _envelope(tmp_path / field, row={"symbol": "FAKE", field: "x"})
    allowed = _envelope(tmp_path / "allowed", row={"symbol": "FAKE", "secretary_name": "x"})
    assert "secretary_name" in allowed.native_fields


@pytest.mark.parametrize(
    "kwargs",
    [
        {"native_schema_version": ""},
        {"adapter_version": "bad value"},
        {"native_schema_version": ".."},
    ],
)
def test_unsafe_versions_rejected(tmp_path: Path, kwargs: dict[str, str]) -> None:
    store = SnapshotStore(tmp_path)
    observation = store.observe(
        RequestSpec("synthetic", "bars", {}, ()),
        b"raw",
        datetime(2024, 1, 1, tzinfo=UTC),
        "json-v1",
    )
    with pytest.raises(ValueError):
        RawFactEnvelope.from_observation(
            store,
            observation,
            native_fields={"symbol": "FAKE"},
            primary_key_fields=("symbol",),
            entity_fields=("symbol",),
            native_schema_version=kwargs.get("native_schema_version", "row-v1"),
            adapter_version=kwargs.get("adapter_version", "adapter-v1"),
        )


@pytest.mark.parametrize(
    "keys,entities,row",
    [
        ((), ("symbol",), {"symbol": "FAKE"}),
        (("symbol", "symbol"), ("symbol",), {"symbol": "FAKE"}),
        (("missing",), ("missing",), {"symbol": "FAKE"}),
        (("symbol",), ("other",), {"symbol": "FAKE", "other": 1}),
        (("symbol",), ("symbol",), {"symbol": None}),
        (("symbol",), ("symbol",), {"symbol": float("nan")}),
    ],
)
def test_identity_field_rejections(
    tmp_path: Path, keys: tuple[str, ...], entities: tuple[str, ...], row: dict[str, Any]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        store = SnapshotStore(tmp_path)
        observation = store.observe(
            RequestSpec("synthetic", "bars", {}, ()),
            b"raw",
            datetime(2024, 1, 1, tzinfo=UTC),
            "json-v1",
        )
        RawFactEnvelope.from_observation(
            store,
            observation,
            native_fields=row,
            primary_key_fields=keys,
            entity_fields=entities,
            native_schema_version="row-v1",
            adapter_version="adapter-v1",
        )


def test_timestamp_tamper_and_public_revision_fields_cannot_be_fabricated(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    assert envelope.revision_status is RawFactRevisionStatus.UNCLASSIFIED
    with pytest.raises((TypeError, ValueError)):
        RawFactEnvelope(
            envelope.observation,
            envelope.availability,
            {"symbol": "FAKE"},
            ("symbol",),
            ("symbol",),
            "row-v1",
            "adapter-v1",
            RawFactRevisionStatus.REVISED_FROM_PREVIOUS,  # type: ignore[arg-type]
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RawFactEnvelope(
            envelope.observation,
            replace(
                envelope.availability,
                snapshot_fetched_at=envelope.snapshot_fetched_at + timedelta(seconds=1),
            ),
            envelope.native_fields,
            ("symbol",),
            ("symbol",),
            "row-v1",
            "adapter-v1",
        )


def test_revised_lineage_and_original_immutability(tmp_path: Path) -> None:
    first = _envelope(tmp_path / "first", row={"symbol": "FAKE", "value": 1})
    second = _envelope(
        tmp_path / "second",
        payload=b"new",
        fetched=datetime(2024, 1, 2, tzinfo=UTC),
        row={"symbol": "FAKE", "value": 2},
    )
    revised = second.classify_against(first)
    assert revised.revision_status is RawFactRevisionStatus.REVISED_FROM_PREVIOUS
    assert revised.previous_fact_version == first.fact_version
    assert revised.previous_row_payload_sha256 == first.row_payload_sha256
    assert first.revision_status is RawFactRevisionStatus.UNCLASSIFIED


@pytest.mark.parametrize("change", ["key", "provider", "endpoint", "schema", "adapter", "entity"])
def test_classification_rejects_incompatible_prior(tmp_path: Path, change: str) -> None:
    first = _envelope(tmp_path / "first", row={"symbol": "FAKE", "value": 1})
    second = _envelope(
        tmp_path / "second",
        payload=b"new",
        fetched=datetime(2024, 1, 2, tzinfo=UTC),
        row={"symbol": "FAKE", "value": 2},
    )
    if change == "key":
        prior = _envelope(tmp_path / "key", row={"symbol": "OTHER", "value": 1})
    elif change == "provider":
        prior = replace(first.observation, provider="other")
        prior = RawFactEnvelope(
            prior,
            first.availability,
            first.native_fields,
            first.primary_key_fields,
            first.entity_fields,
            first.native_schema_version,
            first.adapter_version,
        )
    elif change == "endpoint":
        prior = replace(first.observation, endpoint="other")
        prior = RawFactEnvelope(
            prior,
            first.availability,
            first.native_fields,
            first.primary_key_fields,
            first.entity_fields,
            first.native_schema_version,
            first.adapter_version,
        )
    elif change == "schema":
        prior = RawFactEnvelope(
            first.observation,
            first.availability,
            first.native_fields,
            first.primary_key_fields,
            first.entity_fields,
            "row-v2",
            first.adapter_version,
        )
    elif change == "adapter":
        prior = RawFactEnvelope(
            first.observation,
            first.availability,
            first.native_fields,
            first.primary_key_fields,
            first.entity_fields,
            first.native_schema_version,
            "adapter-v2",
        )
    else:
        prior = RawFactEnvelope(
            first.observation,
            first.availability,
            {"symbol": "FAKE", "value": 1},
            ("symbol", "value"),
            ("symbol",),
            first.native_schema_version,
            first.adapter_version,
        )
        second = RawFactEnvelope(
            second.observation,
            second.availability,
            {"symbol": "FAKE", "value": 2},
            ("symbol", "value"),
            ("value",),
            second.native_schema_version,
            second.adapter_version,
        )
        with pytest.raises(ValueError):
            second.classify_against(prior)
        return
    with pytest.raises(ValueError):
        second.classify_against(prior)


def test_classification_rejects_future_prior_and_same_fact_changed_row(tmp_path: Path) -> None:
    current = _envelope(tmp_path / "current", fetched=datetime(2024, 1, 1, tzinfo=UTC))
    future = _envelope(tmp_path / "future", fetched=datetime(2024, 1, 2, tzinfo=UTC))
    with pytest.raises(ValueError):
        current.classify_against(future)
    changed = RawFactEnvelope(
        current.observation,
        current.availability,
        {"symbol": "FAKE", "value": 2},
        ("symbol",),
        ("symbol",),
        current.native_schema_version,
        current.adapter_version,
    )
    with pytest.raises(ValueError):
        changed.classify_against(current)


def test_classification_uses_exact_key_types(tmp_path: Path) -> None:
    first = _envelope(tmp_path / "first", row={"symbol": 1})
    second = _envelope(
        tmp_path / "second",
        payload=b"new",
        fetched=datetime(2024, 1, 2, tzinfo=UTC),
        row={"symbol": True},
    )
    with pytest.raises(ValueError):
        second.classify_against(first)


def test_from_observation_rejects_replay_identity_mismatch_and_tampered_files(
    tmp_path: Path,
) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("synthetic", "bars", {}, ())
    obs = store.observe(spec, b"raw", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    with pytest.raises(SnapshotIntegrityError):
        RawFactEnvelope.from_observation(
            SnapshotStore(tmp_path / "other"),
            obs,
            native_fields={"symbol": "FAKE"},
            primary_key_fields=("symbol",),
            entity_fields=("symbol",),
            native_schema_version="row-v1",
            adapter_version="adapter-v1",
        )
    path = obs.path
    payload = path.read_text()
    path.write_text(payload.replace(obs.observation_identity, "0" * 64))
    with pytest.raises(SnapshotIntegrityError):
        RawFactEnvelope.from_observation(
            store,
            obs,
            native_fields={"symbol": "FAKE"},
            primary_key_fields=("symbol",),
            entity_fields=("symbol",),
            native_schema_version="row-v1",
            adapter_version="adapter-v1",
        )


def test_old_native_date_does_not_prove_pit(tmp_path: Path) -> None:
    e = _envelope(tmp_path, row={"symbol": "FAKE", "published": date(2000, 1, 1)})
    assert RawFactQualityFlag.PIT_UNPROVEN.value in e.quality_flags


def test_direct_observation_requires_utc(tmp_path: Path) -> None:
    e = _envelope(tmp_path)
    with pytest.raises(ValueError):
        RawFactEnvelope(
            replace(
                e.observation,
                snapshot_fetched_at=e.snapshot_fetched_at.replace(
                    tzinfo=timezone(timedelta(hours=8))
                ),
            ),
            e.availability,
            e.native_fields,
            e.primary_key_fields,
            e.entity_fields,
            e.native_schema_version,
            e.adapter_version,
        )


def test_tampered_observation_rejected(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    for changes in ({"serialization_identifier": "other"},):
        with pytest.raises((ValueError, TypeError)):
            RawFactEnvelope(
                replace(envelope.observation, **changes),
                envelope.availability,
                envelope.native_fields,
                ("symbol",),
                ("symbol",),
                "row-v1",
                "adapter-v1",
            )
