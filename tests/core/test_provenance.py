import json
from datetime import UTC, datetime

import pytest

from ohmydata.core.policy import AttemptRecord
from ohmydata.core.provenance import EmptyDisposition, FetchProvenance
from ohmydata.core.specs import RequestSpec


def test_construction_and_defensive_to_dict() -> None:
    spec = RequestSpec("p", "e", {"x": [1]}, ("a",))
    p = FetchProvenance.from_request(
        spec,
        retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
        attempts=(AttemptRecord(1, None, None),),
        row_count=1,
        columns=("a",),
        warnings=("safe",),
        snapshot_identities=("id",),
        empty_disposition=EmptyDisposition.NOT_EMPTY,
    )
    d = p.to_dict()
    d["effective_parameters"]["x"].append(2)
    assert p.effective_parameters["x"] == (1,) and d["attempts"][0]["attempt"] == 1
    json.dumps(d)


def test_empty_and_validation() -> None:
    spec = RequestSpec("p", "e", {}, ())
    kwargs = {
        "retrieved_at": datetime.now(UTC),
        "attempts": (),
        "row_count": 0,
        "columns": (),
        "warnings": (),
        "snapshot_identities": (),
        "empty_disposition": EmptyDisposition.ALLOWED_EMPTY,
    }
    assert (
        FetchProvenance.from_request(spec, **kwargs).to_dict()["empty_disposition"]
        == "ALLOWED_EMPTY"
    )
    with pytest.raises(ValueError):
        FetchProvenance.from_request(spec, **{**kwargs, "row_count": 1})
    with pytest.raises(ValueError):
        FetchProvenance.from_request(
            spec,
            **{
                **kwargs,
                "columns": ("a", "a"),
                "row_count": 2,
                "empty_disposition": EmptyDisposition.NOT_EMPTY,
            },
        )


def test_utc_attempt_count_and_sequence_isolation() -> None:
    spec = RequestSpec("p", "e", {"x": [1]}, ())
    attempts = [AttemptRecord(1, None, None)]
    p = FetchProvenance.from_request(
        spec,
        retrieved_at=datetime(
            2024,
            1,
            1,
            8,
            tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8)),
        ),
        attempts=attempts,
        row_count=1,
        columns=["x"],
        warnings=["w"],
        snapshot_identities=["s"],
        empty_disposition=EmptyDisposition.NOT_EMPTY,
    )
    attempts.append(AttemptRecord(2, None, None))
    offset = p.retrieved_at.utcoffset()
    assert offset is not None and offset.total_seconds() == 0 and p.attempt_count == 1
    first, second = p.to_dict(), p.to_dict()
    first["warnings"].append("x")
    assert second["warnings"] == ["w"]


def test_all_sequence_inputs_and_nested_parameters_are_isolated() -> None:
    spec = RequestSpec("p", "e", {"nested": {"x": [1]}}, ())
    attempts = [AttemptRecord(1, None, None)]
    columns = ["x"]
    warnings = ["w"]
    snapshots = ["s"]
    p = FetchProvenance.from_request(
        spec,
        retrieved_at=datetime.now(UTC),
        attempts=attempts,
        row_count=1,
        columns=columns,
        warnings=warnings,
        snapshot_identities=snapshots,
        empty_disposition=EmptyDisposition.NOT_EMPTY,
    )
    attempts.append(AttemptRecord(2, None, None))
    columns.append("y")
    warnings.append("z")
    snapshots.append("t")
    with pytest.raises(TypeError):
        p.effective_parameters["nested"]["x"] += (2,)
    assert (
        p.attempt_count == 1
        and p.columns == ("x",)
        and p.warnings == ("w",)
        and p.snapshot_identities == ("s",)
    )
    assert p.to_dict()["attempt_count"] == 1
