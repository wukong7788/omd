"""XML transport bounds, cleanup and retry policy at the injected opener boundary."""

from __future__ import annotations

import io
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from ohmydata.providers.sec.edgartools_adapter import _instance_url
from ohmydata.providers.sec.errors import PermanentProviderError, TransientProviderError
from ohmydata.providers.sec.http import SecHttpClient

URL = "https://www.sec.gov:443/Archives/edgar/data/1/000000000124000001/fake.xml"
LIMIT = 2 * 1024 * 1024


class Response:
    def __init__(self, body=b"<x/>", *, mime="application/xml", status=200, location=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = mime
        if location is not None:
            self.headers["Location"] = location
        self.reads = []

    def read(self, size=-1):
        self.reads.append(size)
        return self.body.read(size)

    def close(self):
        self.body.close()


class Opener:
    def __init__(self, *items):
        self.items = iter(items)
        self.urls = []

    def open(self, request, **kwargs):
        self.urls.append(request.full_url)
        item = next(self.items)
        if isinstance(item, Exception):
            raise item
        return item


def client(opener, sleeps=None):
    return SecHttpClient(
        "Synthetic contact@example.invalid",
        opener=opener,
        limiter=lambda: None,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


@pytest.mark.parametrize(
    "mime", ["application/xml", "text/xml; charset=utf-8", "application/octet-stream"]
)
def test_xml_exact_byte_limit_and_chunked_read(mime):
    raw = Response(b"x" * LIMIT, mime=mime)
    opened = client(Opener(raw)).open(URL, accept="application/xml", max_bytes=LIMIT)
    try:
        chunks = list(iter(lambda: opened.body.read(65536), b""))
        assert sum(map(len, chunks)) == LIMIT
        assert set(raw.reads) == {65536}
    finally:
        opened.body.close()
    assert raw.body.closed


def test_xml_over_limit_without_length_closes_body():
    raw = Response(b"x" * (LIMIT + 1))
    opened = client(Opener(raw)).open(URL, accept="application/xml", max_bytes=LIMIT)
    with pytest.raises(PermanentProviderError, match="too large"):
        while opened.body.read(65536):
            pass
    assert raw.body.closed


@pytest.mark.parametrize("case", ["mime", "length"])
def test_xml_header_rejection_closes_without_retry(case):
    raw = Response(mime="application/json" if case == "mime" else "application/xml")
    if case == "length":
        raw.headers["Content-Length"] = str(LIMIT + 1)
    opener = Opener(raw)
    with pytest.raises(PermanentProviderError):
        client(opener).open(URL, accept="application/xml", max_bytes=LIMIT)
    assert raw.body.closed and not raw.reads and len(opener.urls) == 1


@pytest.mark.parametrize("as_error", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_xml_redirect_checks_filing_directory_and_closes(as_error, valid):
    location = "next.xml" if valid else "/Archives/edgar/data/2/000000000124000001/next.xml"
    redirect = Response(status=302, location=location)
    item = (
        HTTPError(URL, 302, "redirect", redirect.headers, redirect.body) if as_error else redirect
    )
    final = Response()
    opener = Opener(item, final)
    _, validator = _instance_url(
        SimpleNamespace(cik=1, accession_number="0000000001-24-000001"), "fake.xml"
    )
    if valid:
        opened = client(opener).open(URL, accept="application/xml", redirect_validator=validator)
        opened.body.close()
        assert len(opener.urls) == 2
    else:
        with pytest.raises(ValueError, match="directory"):
            client(opener).open(URL, accept="application/xml", redirect_validator=validator)
        assert len(opener.urls) == 1
    assert redirect.body.closed


@pytest.mark.parametrize("code", [403, 503])
def test_xml_http_error_classification_and_error_body_cleanup(code):
    bodies = [io.BytesIO(b"synthetic error") for _ in range(3)]
    headers = Message()
    headers["Retry-After"] = "2"
    errors = [HTTPError(URL, code, "synthetic", headers, body) for body in bodies]
    opener = Opener(*errors)
    sleeps = []
    with pytest.raises(PermanentProviderError if code == 403 else TransientProviderError):
        client(opener, sleeps).open(URL, accept="application/xml")
    attempts = 1 if code == 403 else 3
    assert len(opener.urls) == attempts
    assert all(body.closed for body in bodies[:attempts])
    assert sleeps == ([] if code == 403 else [2.0, 2.0])


@pytest.mark.parametrize("accept", ["application/json", "application/zip"])
def test_xml_mime_does_not_widen_existing_json_or_zip(accept):
    raw = Response()
    with pytest.raises(PermanentProviderError, match="content type"):
        client(Opener(raw)).open(URL, accept=accept)
    assert raw.body.closed


def test_injected_validator_cannot_bypass_sec_host_allowlist():
    opener = Opener()
    with pytest.raises(ValueError, match="allowlist"):
        client(opener).open(
            URL, accept="application/xml", redirect_validator=lambda _: "https://example.invalid/x"
        )
    assert opener.urls == []
