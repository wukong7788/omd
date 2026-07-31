from datetime import UTC, date, datetime
from typing import cast

import pytest

from ohmydata.core.specs import RequestSpec


def test_canonical_determinism_dates_and_mutation() -> None:
    params: dict[str, object] = {
        "z": [2, 1],
        "a": date(2024, 1, 1),
        "when": datetime(2024, 1, 1, tzinfo=UTC),
    }
    spec = RequestSpec("p", "e", params, ("b", "a"))
    cast(list[int], params["z"]).append(3)
    assert spec.effective_parameters["z"] == [2, 1]
    assert (
        spec.request_identity
        == RequestSpec(
            "p",
            "e",
            {"when": datetime(2024, 1, 1, tzinfo=UTC), "a": date(2024, 1, 1), "z": [2, 1]},
            ("b", "a"),
        ).request_identity
    )
    assert "__date__" in spec.canonical_json.decode()


@pytest.mark.parametrize(
    "value",
    [datetime(2024, 1, 1), float("nan"), float("inf"), {1: "x"}, {"x": {1, 2}}, b"x"],  # noqa: DTZ001
)
def test_invalid_values(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        RequestSpec("p", "e", {"x": value})


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "api_token",
        "access_token",
        "client_secret",
        "foo_password",
        "x_api_key",
        "authorization",
        "cookie",
    ],
)
def test_nested_secret_rejection_without_value(key: str) -> None:
    with pytest.raises(ValueError) as exc:
        RequestSpec("p", "e", {"nested": {key: "REAL_SECRET_VALUE"}})
    assert "REAL_SECRET_VALUE" not in str(exc.value)


@pytest.mark.parametrize("key", ["proxy_authorization", "set_cookie", "X-API.KEY", "CLIENT.Secret"])
def test_all_secret_key_variants(key: str) -> None:
    with pytest.raises(ValueError):
        RequestSpec("p", "e", {key: "hidden"})


@pytest.mark.parametrize("identifier", [".", "..", "a/b", "a\\b", "a b", ""])
def test_unsafe_identifiers(identifier: str) -> None:
    with pytest.raises(ValueError):
        RequestSpec(identifier, "e", {})


def test_direct_nested_mutation_and_field_normalization() -> None:
    spec = RequestSpec("p", "e", {"nested": {"x": [1]}}, ["a"])  # type: ignore[arg-type]
    assert spec.fields == ("a",)
    with pytest.raises((TypeError, ValueError)):
        spec.parameters["nested"]["x"] += (2,)
    with pytest.raises((TypeError, ValueError)):
        RequestSpec("p", "e", {}, (1,))  # type: ignore[arg-type]
