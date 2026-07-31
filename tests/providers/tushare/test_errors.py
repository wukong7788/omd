from ohmydata.core import (
    AuthenticationError,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimitError,
    TransientProviderError,
)
from ohmydata.providers.tushare import classify_tushare_exception


def test_exception_mapping_redacts_provider_message():
    error = classify_tushare_exception(TimeoutError("token=FAKE_SECRET"))
    assert isinstance(error, TransientProviderError)
    assert "FAKE_SECRET" not in str(error)
    auth = classify_tushare_exception(RuntimeError("invalid api token FAKE_SECRET"))
    assert isinstance(auth, AuthenticationError)
    assert "FAKE_SECRET" not in str(auth)


def test_connection_classes_transient_but_generic_oserror_permanent():
    assert isinstance(classify_tushare_exception(ConnectionError("x")), TransientProviderError)
    assert isinstance(classify_tushare_exception(TimeoutError("x")), TransientProviderError)
    assert isinstance(
        classify_tushare_exception(OSError("permission denied")), PermissionDeniedError
    )
    assert isinstance(classify_tushare_exception(OSError("disk failure")), PermanentProviderError)


def test_permission_precedes_auth_and_rate_signals_are_stable():
    assert isinstance(
        classify_tushare_exception(RuntimeError("permission denied; token invalid")),
        PermissionDeniedError,
    )
    assert isinstance(
        classify_tushare_exception(RuntimeError("rate limit exceeded token=FAKE")), RateLimitError
    )
    assert isinstance(
        classify_tushare_exception(RuntimeError("unexpected provider response")),
        PermanentProviderError,
    )
