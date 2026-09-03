from __future__ import annotations

import io
import json
import zipfile
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from ohmydata.providers.sec.artifacts import SecArtifactStore, SecReplaySession
from ohmydata.providers.sec.errors import (
    PermanentProviderError,
    SnapshotIntegrityError,
    TransientProviderError,
)
from ohmydata.providers.sec.http import SecHttpClient, validate_sec_url


def _zip_bytes(member: str = "SUBMISSION.tsv", payload: bytes = b"a\tb\n") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload)
    return out.getvalue()


class _Response:
    status = 200

    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))
        self.body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self.body.read(size)

    def close(self) -> None:
        self.body.close()


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response: _Response = response
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float = 0) -> _Response:
        self.requests.append(request)
        return self.response


def test_url_allowlist_and_ua_redaction_boundary() -> None:
    assert validate_sec_url("https://www.sec.gov/files/x") == "https://www.sec.gov:443/files/x"
    for url in (
        "http://www.sec.gov/x",
        "https://evil.example/x",
        "https://www.sec.gov/x?q=1",
        "https://u:p@www.sec.gov/x",
    ):
        with pytest.raises(ValueError):
            validate_sec_url(url)
    with pytest.raises(ValueError):
        SecHttpClient("bad\ncontact")


def test_http_injected_opener_headers_and_content_type() -> None:
    opener = _Opener(_Response(b"{}"))
    response = SecHttpClient("omd-test contact@example.invalid", opener=opener).open(
        "https://data.sec.gov/submissions/x", accept="application/json"
    )
    assert response.status == 200
    assert opener.requests[0].get_header("User-agent") == "omd-test contact@example.invalid"
    with pytest.raises(PermanentProviderError):
        SecHttpClient("test", opener=_Opener(_Response(b"{}", "text/plain"))).open(
            "https://data.sec.gov/submissions/x", accept="application/json"
        )


def test_default_opener_has_empty_proxy_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9")
    client = SecHttpClient("test")
    opener: Any = client.opener
    proxy_handlers = [
        p for p in getattr(opener, "handlers", []) if p.__class__.__name__ == "ProxyHandler"
    ]
    assert proxy_handlers and proxy_handlers[0].proxies == {}


def test_retry_then_success_exposes_immutable_history() -> None:
    class Sequence:
        def __init__(self) -> None:
            self.calls = 0

        def open(self, request: Request, timeout: float = 0) -> _Response:
            self.calls += 1
            if self.calls == 1:
                headers = Message()
                headers["Retry-After"] = "1"
                raise HTTPError(request.full_url, 503, "busy", headers, io.BytesIO())
            return _Response(b"{}")

    sequence = Sequence()
    response = SecHttpClient("test", opener=sequence, sleep=lambda _: None).open(
        "https://data.sec.gov/x", accept="application/json"
    )
    assert len(response.attempts) == 2
    assert response.attempts[0].error_type == "HTTPError"
    assert response.attempts[0].retry_delay == 1
    assert response.attempts[1].error_type is None


def test_response_read_checks_deadline_and_cancellation() -> None:
    now = [0.0]
    cancelled = [False]
    response = SecHttpClient("test", opener=_Opener(_Response(b"{}")), clock=lambda: now[0]).open(
        "https://data.sec.gov/x",
        accept="application/json",
        deadline=20,
        cancelled=lambda: cancelled[0],
    )
    now[0] = 30.0
    with pytest.raises(TransientProviderError, match="deadline"):
        response.body.read()
    cancelled[0] = True
    with pytest.raises(TransientProviderError, match="cancelled"):
        response.body.read()


def test_artifact_publish_replay_stream_and_tamper(tmp_path: Path) -> None:
    store = SecArtifactStore(tmp_path, max_member_bytes=1024, max_total_bytes=2048)
    ref = store.publish(
        io.BytesIO(_zip_bytes()), year=2026, quarter=2, source_url="https://www.sec.gov/x"
    )
    assert b"a\tb" in b"".join(store.stream_member(ref, "submission.tsv"))
    assert store.replay(ref, required_members=("SUBMISSION",)) == ref
    manifest = json.loads(ref.manifest.read_text())
    manifest["sha256"] = "0" * 64
    ref.manifest.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        store.replay(ref)


def test_artifact_rejects_unsafe_zip_paths(tmp_path: Path) -> None:
    data = _zip_bytes("../escape", b"x")
    with pytest.raises(SnapshotIntegrityError):
        SecArtifactStore(tmp_path).publish(
            io.BytesIO(data), year=2026, quarter=2, source_url="https://www.sec.gov/x"
        )


def test_replay_session_validates_once_and_member_is_single_use(tmp_path: Path) -> None:
    store = SecArtifactStore(tmp_path)
    ref = store.publish(
        io.BytesIO(_zip_bytes()), year=2026, quarter=2, source_url="https://www.sec.gov/x"
    )
    session = SecReplaySession(store, ref, required_members=("SUBMISSION",))
    assert b"a\tb" in b"".join(session.open_member("SUBMISSION"))
    with pytest.raises(SnapshotIntegrityError, match="more than once"):
        list(session.open_member("SUBMISSION"))
