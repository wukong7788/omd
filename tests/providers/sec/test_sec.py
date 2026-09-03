from datetime import UTC, date, datetime

import pytest

from ohmydata.providers.sec import (
    SecAvailabilityPolicy,
    SecEmptyPolicy,
    SecFundHoldingVintage,
    SecHoldingVintageSet,
    SecNportQuarterRequest,
    SecUnavailableResult,
    validate_sec_url,
)


def test_request_identity_and_exact_url() -> None:
    req = SecNportQuarterRequest(2025, 1, ("B", "A"), ("A",), SecEmptyPolicy.ALLOW_EMPTY)
    assert req.series_ids == ("A", "B")
    assert req.source_url.endswith("/form-n-port-data-sets/2025q1_nport.zip")
    assert "User-Agent" not in req.spec.canonical_json.decode()


def test_future_quarter_rejected() -> None:
    with pytest.raises(ValueError):
        SecNportQuarterRequest(2099, 1, ("A",))


def test_resolver_is_knowledge_time_bounded() -> None:
    v = SecFundHoldingVintage(
        "0000000000-000001-000001",
        "0000000000",
        "A",
        "A",
        date(2024, 12, 31),
        date(2025, 1, 1),
        "NPORT-P",
        (),
        observed_at=datetime(2025, 2, 1, tzinfo=UTC),
        accepted_at=datetime(2025, 1, 31, tzinfo=UTC),
    )
    s = SecHoldingVintageSet((v,))
    assert isinstance(
        s.resolve("0000000000", "A", datetime(2025, 1, 1, tzinfo=UTC)), SecUnavailableResult
    )
    assert s.resolve("0000000000", "A", datetime(2025, 2, 2, tzinfo=UTC)) == v
    resolved = s.resolve(
        "0000000000",
        "A",
        datetime(2025, 2, 1, tzinfo=UTC),
        policy=SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1,
    )
    assert isinstance(resolved, SecFundHoldingVintage)
    assert resolved.availability_policy == SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1.value


def test_url_allowlist() -> None:
    with pytest.raises(ValueError):
        validate_sec_url("https://example.test/x")
