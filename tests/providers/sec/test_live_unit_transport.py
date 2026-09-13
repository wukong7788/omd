"""Offline bounds and explicit-v2 runner boundary coverage."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from email.message import Message
from types import SimpleNamespace
from typing import Any

import pytest

from ohmydata.providers.sec._live_unit_corroboration import (
    SecUnitEvidenceError as RawUnitEvidenceError,
)
from ohmydata.providers.sec._live_unit_corroboration import (
    corroborate_rows,
    prepare_raw_instance,
)
from ohmydata.providers.sec._statement_parser import SecUnitEvidenceError, parse_statement_rows
from ohmydata.providers.sec.edgartools_adapter import (
    SecFinancialsClient,
    _instance_attachment,
    _instance_url,
    _read_instance,
)
from ohmydata.providers.sec.financials import (
    SecCompanyFinancialVintage,
    SecFinancialsRequest,
    SecStatementRow,
)
from ohmydata.providers.sec.http import SecHttpClient


class _Body:
    def __init__(self, value: bytes) -> None:
        self.value = io.BytesIO(value)
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.value.read(size)

    def close(self) -> None:
        self.closed = True


def test_v2_requires_raw_bytes_and_custom_runner_evidence() -> None:
    with pytest.raises(SecUnitEvidenceError, match="requires raw"):
        parse_statement_rows(
            None,
            "income_statement",
            parser_version="sec-live-financial-parser-v2-edgartools-5.56.0",
        )

    vintage = SecCompanyFinancialVintage(
        symbol="SYN",
        cik="0000000001",
        company_name="Synthetic",
        form="10-K",
        accession_number="0000000001-24-000001",
        filing_date=__import__("datetime").date(2024, 1, 1),
    )
    client = SecFinancialsClient("Synthetic test@example.invalid", runner=lambda request: [vintage])
    with pytest.raises(SecUnitEvidenceError, match="without unit evidence"):
        client.fetch_company_financials(SecFinancialsRequest(symbols=("SYN",)))


def test_instance_read_is_chunked_closed_and_redirect_is_filing_bound() -> None:
    body = _Body(b"x" * (64 * 1024 + 1))

    class Transport:
        def open(self, url: str, **kwargs: object) -> object:
            return type("Response", (), {"body": body})()

    raw = _read_instance(
        Transport(),
        "https://www.sec.gov:443/Archives/edgar/data/1/000000000124000001/a.xml",
        lambda value: value,
    )  # type: ignore[arg-type]
    assert raw == b"x" * (64 * 1024 + 1)
    assert body.read_sizes == [64 * 1024, 64 * 1024, 64 * 1024]
    assert body.closed

    filing = type("Filing", (), {"cik": 1, "accession_number": "0000000001-24-000001"})()
    _, validator = _instance_url(filing, "a.xml")
    with pytest.raises(SecUnitEvidenceError, match="directory"):
        validator("https://www.sec.gov:443/Archives/edgar/data/2/000000000124000001/a.xml")


def test_http_xml_accepts_xml_and_closes_bounded_response() -> None:
    @dataclass
    class Response:
        body: io.BytesIO
        headers: Message
        status: int = 200

        def read(self, size: int = -1) -> bytes:
            return self.body.read(size)

        def close(self) -> None:
            self.body.close()

    headers = Message()
    headers["Content-Type"] = "application/xml"
    headers["Content-Length"] = "4"
    response = Response(io.BytesIO(b"<x/>"), headers)
    client = SecHttpClient("test", opener=type("Open", (), {"open": lambda *_, **__: response})())
    opened = client.open("https://www.sec.gov/x", accept="application/xml", max_bytes=4)
    assert opened.body.read(64 * 1024) == b"<x/>"
    opened.body.close()


def _raw_instance() -> bytes:
    return (
        b'<xbrl xmlns="http://www.xbrl.org/2003/instance" '
        b'xmlns:dei="http://example.invalid/dei" '
        b'xmlns:iso4217="http://www.xbrl.org/2003/iso4217">'
        b'<context id="c"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier>'
        b"</entity><period><instant>2024-12-31</instant></period></context>"
        b'<unit id="usd"><measure>iso4217:USD</measure></unit>'
        b'<dei:Fact contextRef="c" unitRef="usd" decimals="0">1</dei:Fact></xbrl>'
    )


def _attachment(name: str, raw: bytes) -> Any:
    class Attachment:
        document = name
        document_type = "EX-101.INS"
        extension = ".xml"
        url = f"https://www.sec.gov:443/Archives/edgar/data/1/000000000124000001/{name}"

        @property
        def content(self) -> bytes:
            raise AssertionError("network-backed Attachment.content must not be read")

    attachment: Any = Attachment()
    attachment.sgml_document = SimpleNamespace(content=raw)
    return attachment


def _filing(*attachments: object) -> Any:
    return SimpleNamespace(
        cik=1,
        accession_number="0000000001-24-000001",
        attachments=SimpleNamespace(data_files=list(attachments)),
    )


def test_instance_acquisition_uses_pinned_classifier_on_bounded_sgml_without_content() -> None:
    attachment = _attachment("instance.xml", _raw_instance())
    document, raw, url = _instance_attachment(_filing(attachment), None)
    assert (document, raw) == ("instance.xml", _raw_instance())
    assert url.endswith("/instance.xml")


def test_instance_acquisition_rejects_multiple_classified_original_instances() -> None:
    with pytest.raises(SecUnitEvidenceError, match="exactly one"):
        _instance_attachment(
            _filing(
                _attachment("one.xml", _raw_instance()), _attachment("two.xml", _raw_instance())
            ),
            None,
        )


def test_instance_acquisition_rejects_conflicting_duplicate_source_bytes() -> None:
    changed = _raw_instance().replace(b">1</dei:Fact>", b">2</dei:Fact>")
    with pytest.raises(SecUnitEvidenceError, match="conflicting duplicate bytes"):
        _instance_attachment(
            _filing(_attachment("same.xml", _raw_instance()), _attachment("same.xml", changed)),
            None,
        )


def test_instance_acquisition_uses_homepage_only_after_original_has_no_instance() -> None:
    original = _attachment("schema.xml", b"<schema/>")
    selected = _attachment("instance.xml", _raw_instance())
    filing = _filing(original)
    filing.homepage = SimpleNamespace(data_files=[selected])
    document, raw, _ = _instance_attachment(filing, None)
    assert document == "instance.xml"
    assert raw == _raw_instance()


def test_instance_acquisition_validates_sgml_attachment_url_before_using_content() -> None:
    attachment = _attachment("instance.xml", _raw_instance())
    attachment.url = "https://www.sec.gov:443/Archives/edgar/data/2/000000000124000001/instance.xml"
    with pytest.raises(SecUnitEvidenceError, match="directory"):
        _instance_attachment(_filing(attachment), None)


def test_instance_acquisition_rejects_absence_after_one_homepage_fallback() -> None:
    filing = _filing()
    filing.homepage = SimpleNamespace(data_files=[])
    with pytest.raises(SecUnitEvidenceError, match="exactly one"):
        _instance_attachment(filing, None)


@pytest.mark.parametrize(
    "name,url,expected",
    [
        ("../unsafe.xml", None, "unsafe"),
        (
            "instance.xml",
            "https://www.sec.gov:443/Archives/edgar/data/1/000000000124000002/instance.xml",
            "directory",
        ),
    ],
)
def test_instance_acquisition_rejects_unbound_document_source(name, url, expected) -> None:
    attachment = _attachment(name, _raw_instance())
    if url is not None:
        attachment.url = url
    with pytest.raises(SecUnitEvidenceError, match=expected):
        _instance_attachment(_filing(attachment), None)


def test_sgml_instance_size_limit_is_exact_and_not_over() -> None:
    exact = _raw_instance() + b" " * (16 * 1024 * 1024 - len(_raw_instance()))
    assert _instance_attachment(_filing(_attachment("exact.xml", exact)), None)[1] == exact
    with pytest.raises(SecUnitEvidenceError, match="byte limit"):
        _instance_attachment(_filing(_attachment("over.xml", exact + b"x")), None)


def test_instance_acquisition_reads_injected_http_when_sgml_is_absent() -> None:
    attachment = _attachment("remote.xml", _raw_instance())
    attachment.sgml_document = None
    body = _Body(_raw_instance())

    class Transport:
        def open(self, url: str, **kwargs: object) -> object:
            assert url.endswith("/remote.xml")
            return SimpleNamespace(body=body)

    assert _instance_attachment(_filing(attachment), Transport())[1] == _raw_instance()  # type: ignore[arg-type]
    assert body.closed


def test_retained_context_cik_is_checked_after_preparing_raw_index() -> None:
    evidence = prepare_raw_instance(_raw_instance(), expected_cik="2")
    row = SecStatementRow(
        statement_type="income_statement",
        standard_concept="dei_Fact",
        concept="dei_Fact",
        label="Fact",
        value=Decimal(1),
        value_native="1",
        unit_ref="usd",
        decimals=0,
        decimals_native="0",
        period_end=date(2024, 12, 31),
        period_type="instant",
        period_key="instant_2024-12-31",
        context_ref="c",
        is_point_in_time=True,
        period_source="xbrl-context",
    )
    with pytest.raises(Exception, match="does not match"):
        corroborate_rows(evidence, [row])


class _StreamedChunkFake:
    def __init__(self, total_bytes: int, chunk_size: int = 64 * 1024) -> None:
        self.total_bytes = total_bytes
        self.chunk_size = chunk_size
        self.read_bytes = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self.read_bytes >= self.total_bytes:
            return b""
        remaining = self.total_bytes - self.read_bytes
        chunk_len = min(size if size > 0 else self.chunk_size, self.chunk_size, remaining)
        self.read_bytes += chunk_len
        return b"x" * chunk_len

    def close(self) -> None:
        self.closed = True


def test_read_instance_accepts_over_2_mib_and_up_to_16_mib_streamed() -> None:
    # 3 MiB instance (similar to GOOGL 3,046,085 bytes)
    target_len = 3 * 1024 * 1024
    body = _StreamedChunkFake(target_len)

    class Transport:
        def open(self, url: str, **kwargs: object) -> object:
            assert kwargs.get("max_bytes") == 16 * 1024 * 1024
            return type("Response", (), {"body": body})()

    raw = _read_instance(
        Transport(),  # type: ignore[arg-type]
        "https://www.sec.gov:443/Archives/edgar/data/1/000000000124000001/a.xml",
        lambda value: value,
    )
    assert len(raw) == target_len
    assert body.closed


def test_read_instance_rejects_over_16_mib_without_pathological_fixture() -> None:
    # Simulates 16 MiB + 1 byte in 64 KiB chunks without allocating a 16MB contiguous array
    over_limit = 16 * 1024 * 1024 + 1
    body = _StreamedChunkFake(over_limit)

    class Transport:
        def open(self, url: str, **kwargs: object) -> object:
            assert kwargs.get("max_bytes") == 16 * 1024 * 1024
            return type("Response", (), {"body": body})()

    with pytest.raises(SecUnitEvidenceError, match="byte limit"):
        _read_instance(
            Transport(),  # type: ignore[arg-type]
            "https://www.sec.gov:443/Archives/edgar/data/1/000000000124000001/a.xml",
            lambda value: value,
        )
    assert body.closed


def test_prepare_raw_instance_accepts_over_2_mib_and_rejects_over_16_mib() -> None:
    # Padded instance > 2 MiB (3 MiB) must be accepted by prepare_raw_instance
    three_mib_instance = (
        _raw_instance() + b"<!-- " + b"x" * (3 * 1024 * 1024 - len(_raw_instance()) - 7) + b" -->"
    )
    evidence = prepare_raw_instance(three_mib_instance)
    assert "usd" in evidence.units

    # > 16 MiB must be rejected
    with pytest.raises(RawUnitEvidenceError, match="byte limit"):
        prepare_raw_instance(b" " * (16 * 1024 * 1024 + 1))
