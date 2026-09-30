import pytest

from ohmydata.core import (
    AuthenticationError,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderError,
    ProviderErrorCategory,
    RateLimitError,
    TransientProviderError,
    UnknownProviderError,
    UnsupportedProviderError,
)
from ohmydata.providers.tushare import classify_tushare_exception


class ProviderFailure(Exception):
    def __init__(self, message="opaque", *, code=None, status_code=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class CodedTimeout(TimeoutError):
    code = "TMO_7"


class UnprintableFailure(Exception):
    code = "E_29"

    def __str__(self):
        raise RuntimeError("private provider details")


def test_classified_messages_are_redacted_and_categories_are_typed():
    auth = classify_tushare_exception(RuntimeError("invalid api token"))
    assert isinstance(auth, AuthenticationError)
    assert auth.category is ProviderErrorCategory.AUTHENTICATION
    assert "invalid api token" not in str(auth)
    assert isinstance(
        classify_tushare_exception(RuntimeError("permission denied")), PermissionDeniedError
    )
    assert isinstance(
        classify_tushare_exception(RuntimeError("rate limit exceeded")), RateLimitError
    )
    assert isinstance(
        classify_tushare_exception(RuntimeError("unsupported endpoint")), UnsupportedProviderError
    )
    permanent = classify_tushare_exception(RuntimeError("invalid parameter"))
    assert isinstance(permanent, PermanentProviderError)
    assert permanent.category is ProviderErrorCategory.PERMANENT


def test_unrecognized_message_is_unknown_and_does_not_match_substrings():
    for message in (
        "opaque token value FAKE_SECRET",
        "permission denied; token invalid",
        "provider says too many things",
        "unexpected provider response",
    ):
        mapped = classify_tushare_exception(RuntimeError(message))
        assert isinstance(mapped, UnknownProviderError)
        assert mapped.category is ProviderErrorCategory.UNKNOWN
        assert message not in repr(mapped)


def test_network_errors_are_transient_and_keep_only_safe_structured_code():
    error = classify_tushare_exception(ProviderFailure("token=FAKE_SECRET", code=429))
    assert isinstance(error, UnknownProviderError)
    assert error.provider_code == 429
    assert "FAKE_SECRET" not in str(error)

    timeout = classify_tushare_exception(CodedTimeout("secret payload"))
    assert isinstance(timeout, TransientProviderError)
    assert timeout.category is ProviderErrorCategory.TRANSIENT
    assert timeout.provider_code == "TMO_7"
    assert "secret payload" not in repr(timeout)


@pytest.mark.parametrize(
    "code", [True, "contains whitespace", "api_key=FAKE", "FAKE_SECRET_TOKEN_LONG_LONG_LONG_LONG"]
)
def test_unsafe_structured_codes_are_rejected(code):
    mapped = classify_tushare_exception(ProviderFailure("opaque", code=code))
    assert isinstance(mapped, ProviderError)
    assert mapped.provider_code is None


def test_safe_status_code_fallback_and_code_precedence():
    fallback = classify_tushare_exception(ProviderFailure(code="bad value", status_code="E_17"))
    assert isinstance(fallback, ProviderError)
    assert fallback.provider_code == "E_17"
    preferred = classify_tushare_exception(ProviderFailure(code=401, status_code=403))
    assert isinstance(preferred, ProviderError)
    assert preferred.provider_code == 401


def test_unknown_tushare_failure_is_permanent_non_retry_compatible():
    mapped = classify_tushare_exception(RuntimeError("unclassified"))
    assert isinstance(mapped, PermanentProviderError)
    assert not isinstance(mapped, TransientProviderError)


def test_unprintable_exception_falls_back_to_redacted_unknown_with_code():
    mapped = classify_tushare_exception(UnprintableFailure())

    assert isinstance(mapped, UnknownProviderError)
    assert mapped.category is ProviderErrorCategory.UNKNOWN
    assert mapped.provider_code == "E_29"
    assert "private provider details" not in repr(mapped)
