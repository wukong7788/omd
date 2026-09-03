"""Small, deliberately boring HTTP boundary for SEC public endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import PermanentProviderError, TransientProviderError


@dataclass(frozen=True)
class SecTransportEvidence:
    proxy_in_use: bool = False


@dataclass(frozen=True)
class SecHttpResponse:
    status: int
    headers: Message
    body: Any
    url: str
    attempts: tuple[SecAttemptRecord, ...] = ()


@dataclass(frozen=True)
class SecAttemptRecord:
    attempt: int
    error_type: str | None
    retry_delay: float


class _BoundedBody:
    def __init__(
        self,
        raw: Any,
        limit: int,
        *,
        clock: Callable[[], float],
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
        progress: Callable[[str, int], None] | None,
    ) -> None:
        self.raw, self.limit, self.observed = raw, limit, 0
        self.clock, self.deadline, self.cancelled, self.progress = (
            clock,
            deadline,
            cancelled,
            progress,
        )

    def read(self, size: int | None = None) -> bytes:
        if self.cancelled is not None and self.cancelled():
            self.close()
            raise TransientProviderError("SEC response read cancelled")
        if self.deadline is not None and self.clock() > self.deadline:
            self.close()
            raise TransientProviderError("SEC response read deadline exceeded")
        chunk = self.raw.read() if size is None or size < 0 else self.raw.read(size)
        self.observed += len(chunk)
        if self.observed > self.limit:
            try:
                self.raw.close()
            finally:
                raise PermanentProviderError("SEC response too large")
        if self.progress is not None:
            self.progress("response_body", self.observed)
        return chunk

    def close(self) -> None:
        self.raw.close()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Message, newurl: str
    ) -> None:
        return None


def validate_sec_url(url: str) -> str:
    p = urlsplit(url)
    if (
        p.scheme.lower() != "https"
        or p.hostname not in {"www.sec.gov", "data.sec.gov"}
        or p.port not in (None, 443)
        or p.username
        or p.password
        or p.fragment
        or p.query
    ):
        raise ValueError("URL is outside the SEC HTTPS allowlist")
    return f"https://{p.hostname}:443{p.path or '/'}"


def _content_type(headers: Message) -> str:
    return headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


class SecHttpClient:
    def __init__(
        self,
        user_agent: object,
        *,
        opener: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        limiter: Any = None,
        max_attempts: int = 3,
        max_total_delay: float = 120.0,
        proxy_in_use: bool = False,
        max_body_bytes: int = 2 * 1024**3,
    ) -> None:
        if (
            not isinstance(user_agent, str)
            or not 1 <= len(user_agent) <= 256
            or any(ord(c) < 32 or ord(c) > 126 for c in user_agent)
        ):
            raise ValueError("invalid User-Agent")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be 1..3")
        self._ua = user_agent
        if opener is None:
            direct_proxy = ProxyHandler({})
            direct_opener: Any = build_opener(_NoRedirect())
            # build_opener omits an empty ProxyHandler; retain it explicitly so
            # the no-proxy policy is inspectable and cannot fall back silently.
            direct_opener.handlers.insert(0, direct_proxy)
            self._opener = direct_opener
        else:
            self._opener = opener
        self._clock, self._sleep, self._limiter = clock, sleep, limiter
        self.max_attempts, self.max_total_delay = max_attempts, max_total_delay
        self.max_body_bytes = max_body_bytes
        self._last_request_at: float | None = None
        self.attempts: tuple[SecAttemptRecord, ...] = ()
        self.evidence = SecTransportEvidence(proxy_in_use)

    @property
    def opener(self) -> Any:
        """The caller-supplied or no-proxy opener, for safe diagnostics."""
        return self._opener

    def _limit(self) -> None:
        if self._limiter is None:
            now = self._clock()
            if self._last_request_at is not None and now - self._last_request_at < 0.2:
                self._sleep(0.2 - (now - self._last_request_at))
            self._last_request_at = self._clock()
            return
        if callable(self._limiter):
            self._limiter()
        elif hasattr(self._limiter, "acquire"):
            self._limiter.acquire()

    def open(
        self,
        url: str,
        *,
        accept: str,
        max_bytes: int | None = None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[str, int], None] | None = None,
    ) -> SecHttpResponse:
        max_bytes = (
            self.max_body_bytes if max_bytes is None else min(max_bytes, self.max_body_bytes)
        )
        current = validate_sec_url(url)
        allowed = (
            {"application/zip", "application/octet-stream"}
            if "zip" in accept.lower()
            else {"application/json"}
        )
        total_delay = 0.0
        last: BaseException | None = None
        redirects = 0
        records: list[SecAttemptRecord] = []
        for attempt in range(1, self.max_attempts + 1):
            try:
                if cancelled is not None and cancelled():
                    raise TransientProviderError("SEC request cancelled")
                if deadline is not None and self._clock() > deadline:
                    raise TransientProviderError("SEC request deadline exceeded")
                self._limit()
                req = Request(
                    current, headers={"User-Agent": self._ua, "Accept": accept}, method="GET"
                )
                response = self._opener.open(req, timeout=3600)
                status = int(
                    getattr(
                        response,
                        "status",
                        response.getcode() if hasattr(response, "getcode") else 200,
                    )
                )
                if status in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > 3:
                        raise PermanentProviderError("SEC redirect limit exceeded")
                    location = response.headers.get("Location")
                    if not location:
                        raise PermanentProviderError("SEC redirect missing location")
                    current = validate_sec_url(urljoin(current, location))
                    continue
                if status != 200:
                    raise PermanentProviderError(f"SEC HTTP status {status}")
                headers = response.headers
                if len(headers.items()) > 128 or any(
                    len(k) > 8192 or len(v) > 8192 for k, v in headers.items()
                ):
                    raise PermanentProviderError("SEC response headers exceed limit")
                if _content_type(headers) not in allowed:
                    raise PermanentProviderError("SEC response content type rejected")
                length = headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise PermanentProviderError("SEC response too large")
                body = _BoundedBody(
                    response,
                    max_bytes,
                    clock=self._clock,
                    deadline=deadline,
                    cancelled=cancelled,
                    progress=progress,
                )
                if progress is not None:
                    progress("response_headers", 0)
                records.append(SecAttemptRecord(attempt, None, 0.0))
                self.attempts = tuple(records)
                return SecHttpResponse(status, headers, body, current, tuple(records))
            except HTTPError as exc:
                last = exc
                if exc.code in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > 3:
                        raise PermanentProviderError("SEC redirect limit exceeded") from None
                    location = exc.headers.get("Location") if exc.headers else None
                    if not location:
                        raise PermanentProviderError("SEC redirect missing location") from None
                    current = validate_sec_url(urljoin(current, location))
                    continue
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise PermanentProviderError(f"SEC HTTP status {exc.code}") from None
                delay = min(60.0, 2.0 ** (attempt - 1))
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after is not None and retry_after.isdigit() and int(retry_after) <= 60:
                    delay = float(retry_after)
            except (TimeoutError, ConnectionError, URLError, OSError) as exc:
                last = exc
                delay = min(60.0, 2.0 ** (attempt - 1))
            else:
                continue
            if attempt >= self.max_attempts:
                break
            delay = min(delay, max(0.0, self.max_total_delay - total_delay))
            total_delay += delay
            records.append(SecAttemptRecord(attempt, type(last).__name__ if last else None, delay))
            self._sleep(delay)
        self.attempts = tuple(records)
        raise TransientProviderError("SEC request retry exhausted") from last


__all__ = [
    "SecAttemptRecord",
    "SecHttpClient",
    "SecHttpResponse",
    "SecTransportEvidence",
    "validate_sec_url",
]
