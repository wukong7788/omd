from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pyarrow.parquet as pq
import pytest

from ohmydata.cli import main as cli_main
from ohmydata.core.errors import PermanentProviderError, TransientProviderError
from ohmydata.providers.sec import (
    FRANKFURTER_FX_SOURCE,
    SecBulkFundamentalRow,
    SecFxDatePolicy,
    SecFxFlowMethod,
    SecFxRateObservation,
    SecFxRateTable,
    SecFxTranslationStatus,
    build_bulk_companyfacts_dataset,
    fetch_frankfurter_fx_rates,
    load_fx_rate_table,
    parse_frankfurter_timeseries_payload,
    translate_bulk_dataset_to_usd,
    translate_bulk_fundamentals_to_usd,
    validate_bulk_companyfacts_dataset,
    validate_frankfurter_url,
    validate_usd_translated_fundamentals_dataset,
    write_fx_rate_table,
)


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._buf = io.BytesIO(body)
        self.status = status
        msg = Message()
        if content_type:
            msg["Content-Type"] = content_type
        if headers:
            for k, v in headers.items():
                msg[k] = v
        self.headers = msg
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, steps: list[Any]) -> None:
        self._steps = list(steps)
        self.requests: list[tuple[str, dict[str, str]]] = []

    def open(self, req: Any, timeout: float | None = None) -> Any:
        _ = timeout
        self.requests.append((req.full_url, dict(req.header_items())))
        step = self._steps.pop(0)
        if callable(step):
            return step(req)
        if isinstance(step, BaseException):
            raise step
        return step


def _http_err(
    url: str, code: int, *, retry_after: str | None = None, location: str | None = None
) -> HTTPError:
    msg = Message()
    if retry_after is not None:
        msg["Retry-After"] = retry_after
    if location is not None:
        msg["Location"] = location
    return HTTPError(url, code, f"HTTP {code}", msg, io.BytesIO(b"error"))


def test_validate_frankfurter_url_allowlist() -> None:
    assert (
        validate_frankfurter_url("https://api.frankfurter.dev/v1/2024-01-01..2024-01-05?base=USD")
        == "https://api.frankfurter.dev/v1/2024-01-01..2024-01-05?base=USD"
    )
    assert (
        validate_frankfurter_url("https://api.frankfurter.dev:443/v1/2024-01-01..?")
        == "https://api.frankfurter.dev/v1/2024-01-01.."
    )

    bad_urls = [
        "",
        "http://api.frankfurter.dev/v1/2024-01-01..?base=USD",
        "https://evil.example.com/v1/2024-01-01..?base=USD",
        "https://www.sec.gov/v1/2024-01-01..",
        "https://api.frankfurter.dev:8443/v1/2024-01-01..",
        "https://user:pass@api.frankfurter.dev/v1/2024-01-01..",
        "https://api.frankfurter.dev/v1/2024-01-01..#frag",
        "https://api.frankfurter.dev/v2/latest",
        "https://api.frankfurter.dev/v1/../v2/latest",
        "https://api.frankfurter.dev/v1/2024-01-01..\r\n?base=USD",
        "https://api.frankfurter.dev/",
    ]
    for url in bad_urls:
        with pytest.raises(ValueError):
            validate_frankfurter_url(url)


def test_parse_frankfurter_timeseries_payload_inversion_and_usd_skip() -> None:
    raw = json.dumps(
        {
            "amount": 1.0,
            "base": "USD",
            "start_date": "2024-12-30",
            "end_date": "2024-12-31",
            "rates": {
                "2024-12-30": {"EUR": 0.96, "JPY": 156.25, "DKK": 7.15, "USD": 1.0},
                "2024-12-31": {"EUR": 0.8},
            },
        }
    ).encode("utf-8")

    table = parse_frankfurter_timeseries_payload(raw)
    assert table.currencies == ("DKK", "EUR", "JPY")
    assert table.observation_count == 4
    obs_eur = table.lookup_spot("EUR", date(2024, 12, 31))
    assert obs_eur is not None
    assert obs_eur.usd_per_unit == Decimal("1.25")
    assert obs_eur.source == FRANKFURTER_FX_SOURCE
    assert obs_eur.available_at is None

    obs_jpy = table.lookup_spot("JPY", date(2024, 12, 30))
    assert obs_jpy is not None
    assert obs_jpy.usd_per_unit == Decimal("0.0064")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"amount":1.0,"base":"EUR","rates":{"2024-01-02":{"USD":1.1}}}',
        b'{"amount":100,"base":"USD","rates":{"2024-01-02":{"EUR":90}}}',
        b'{"amount":1.0,"base":"USD","rates":[]}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"USD":1.01}}}',
        b'{"amount":1.0,"base":"USD","rates":{"20240102":{"EUR":0.9}}}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":0}}}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":-0.9}}}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":true}}}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EURO":0.9}}}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":NaN}}}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":0.9,"EUR":0.95}}}',
        b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":0.9},"2024-01-02":{"CAD":1.3}}}',
        b"{not-json",
        b"\xff\xfe",
    ],
)
def test_parse_frankfurter_timeseries_payload_rejects_malformed(payload: bytes) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_frankfurter_timeseries_payload(payload)


def test_fetch_frankfurter_fx_rates_ranges_and_retries() -> None:
    payload = json.dumps(
        {
            "amount": 1.0,
            "base": "USD",
            "rates": {
                "2024-01-02": {"EUR": 0.9, "DKK": 6.7},
                "2024-01-03": {"EUR": 0.91, "DKK": 6.8},
            },
        }
    ).encode("utf-8")

    # 429 Retry-After -> 503 -> URLError (with max_attempts=4) -> 200 OK
    sleeps: list[float] = []
    url = "https://api.frankfurter.dev/v1/2024-01-02..2024-01-05?base=USD&symbols=DKK,EUR"
    opener = _FakeOpener(
        [
            _http_err(url, 429, retry_after="2"),
            _http_err(url, 503),
            URLError("temp reset"),
            _FakeResponse(payload),
        ]
    )
    table = fetch_frankfurter_fx_rates(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        currencies=[" eur ", "DKK", "EUR"],
        opener=opener,
        max_attempts=4,
        sleep=sleeps.append,
    )
    assert table.currencies == ("DKK", "EUR")
    assert table.observation_count == 4
    assert sleeps == [2.0, 2.0, 4.0]
    assert len(opener.requests) == 4
    assert opener.requests[0][0] == url
    headers_lower = {k.lower(): v for k, v in opener.requests[0][1].items()}
    assert headers_lower["accept"] == "application/json"

    # Open-ended range (end_date=None) without currency filter
    open_opener = _FakeOpener([_FakeResponse(payload)])
    open_table = fetch_frankfurter_fx_rates(start_date=date(2024, 1, 2), opener=open_opener)
    assert open_table.observation_count == 4
    assert open_opener.requests[0][0] == "https://api.frankfurter.dev/v1/2024-01-02..?base=USD"


def test_fetch_frankfurter_fx_rates_retry_exhaustion_preserves_cause() -> None:
    sleeps: list[float] = []
    url = "https://api.frankfurter.dev/v1/2024-01-02..?base=USD"
    opener = _FakeOpener([_http_err(url, 503), TimeoutError("read timeout")])
    with pytest.raises(TransientProviderError, match="retry exhausted") as exc_info:
        fetch_frankfurter_fx_rates(
            start_date=date(2024, 1, 2),
            opener=opener,
            max_attempts=2,
            sleep=sleeps.append,
        )
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert sleeps == [1.0]


def test_fetch_frankfurter_fx_rates_permanent_failures_do_not_retry() -> None:
    url = "https://api.frankfurter.dev/v1/2024-01-02..?base=USD"

    for bad_step in (
        _http_err(url, 404),
        _http_err(url, 400),
        _FakeResponse(b"<html>bad</html>", content_type="text/html"),
        _FakeResponse(b"{}", content_type="application/json-evil"),
        _FakeResponse(b'{"amount":1.0,"base":"USD","rates":{}}', headers={"Content-Length": "999"}),
        _FakeResponse(b'{"amount":1.0,"base":"USD","rates":{}}'),
        _FakeResponse(b"{bad json"),
        _FakeResponse(b"", status=302, headers={"Location": "https://evil.example.com/v1/x"}),
    ):
        sleeps: list[float] = []
        opener = _FakeOpener([bad_step])
        with pytest.raises(PermanentProviderError):
            fetch_frankfurter_fx_rates(
                start_date=date(2024, 1, 2),
                opener=opener,
                max_attempts=3,
                sleep=sleeps.append,
            )
        assert sleeps == []

    # Oversized body fails immediately
    sleeps = []
    opener_big = _FakeOpener(
        [_FakeResponse(b'{"amount":1.0,"base":"USD","rates":{}}' + b" " * 100)]
    )
    with pytest.raises(PermanentProviderError, match="max_response_bytes"):
        fetch_frankfurter_fx_rates(
            start_date=date(2024, 1, 2),
            opener=opener_big,
            max_response_bytes=32,
            sleep=sleeps.append,
        )
    assert sleeps == []

    # Missing requested currency fails permanently
    partial_payload = b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":0.9}}}'
    opener_missing = _FakeOpener([_FakeResponse(partial_payload)])
    with pytest.raises(PermanentProviderError, match="missing requested currencies"):
        fetch_frankfurter_fx_rates(
            start_date=date(2024, 1, 2),
            currencies=("EUR", "DKK"),
            opener=opener_missing,
        )


def test_fetch_frankfurter_fx_rates_redirect_within_allowlist_succeeds() -> None:
    url1 = "https://api.frankfurter.dev/v1/2024-01-02..?base=USD"
    payload = b'{"amount":1.0,"base":"USD","rates":{"2024-01-02":{"EUR":0.9}}}'
    opener = _FakeOpener(
        [
            _http_err(
                url1, 302, location="https://api.frankfurter.dev/v1/2024-01-02..2024-01-03?base=USD"
            ),
            _FakeResponse(payload),
        ]
    )
    table = fetch_frankfurter_fx_rates(start_date=date(2024, 1, 2), opener=opener)
    assert table.observation_count == 1
    assert len(opener.requests) == 2

    too_many = _FakeOpener(
        [
            _http_err(url1, 302, location=url1),
            _http_err(url1, 302, location=url1),
            _http_err(url1, 302, location=url1),
            _http_err(url1, 302, location=url1),
        ]
    )
    with pytest.raises(PermanentProviderError, match="redirect limit"):
        fetch_frankfurter_fx_rates(start_date=date(2024, 1, 2), opener=too_many)


def test_fetch_frankfurter_fx_rates_input_validation() -> None:
    with pytest.raises(TypeError):
        fetch_frankfurter_fx_rates(start_date=datetime(2024, 1, 1, tzinfo=UTC))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="end_date"):
        fetch_frankfurter_fx_rates(start_date=date(2024, 2, 1), end_date=date(2024, 1, 1))
    with pytest.raises(ValueError, match="USD"):
        fetch_frankfurter_fx_rates(start_date=date(2024, 1, 1), currencies=("EUR", "USD"))
    with pytest.raises(ValueError, match="empty"):
        fetch_frankfurter_fx_rates(start_date=date(2024, 1, 1), currencies=())
    with pytest.raises(TypeError):
        fetch_frankfurter_fx_rates(start_date=date(2024, 1, 1), currencies="EUR")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_attempts"):
        fetch_frankfurter_fx_rates(start_date=date(2024, 1, 1), max_attempts=0)


def test_write_and_load_fx_rate_table_roundtrip_parquet_and_csv(tmp_path: Path) -> None:
    avail = datetime(2024, 12, 31, 15, 30, tzinfo=UTC)
    obs = [
        SecFxRateObservation("EUR", date(2024, 12, 30), Decimal("1.0416666667"), "frankfurter_ecb"),
        SecFxRateObservation(
            "EUR", date(2024, 12, 31), Decimal("1.0355"), "frankfurter_ecb", available_at=avail
        ),
        SecFxRateObservation("DKK", date(2024, 12, 31), Decimal("0.1388888889"), "frankfurter_ecb"),
    ]
    table = SecFxRateTable.from_observations(obs)

    pq_path = tmp_path / "fx.parquet"
    csv_path = tmp_path / "fx.csv"
    pq_meta = write_fx_rate_table(pq_path, table)
    csv_meta = write_fx_rate_table(csv_path, table)

    assert pq_meta["format"] == "parquet"
    assert pq_meta["observation_count"] == 3
    assert pq_meta["currencies"] == ["DKK", "EUR"]
    assert pq_meta["min_date"] == "2024-12-30"
    assert pq_meta["max_date"] == "2024-12-31"
    assert pq.ParquetFile(pq_path).metadata.num_rows == 3

    # Deterministic re-write yields identical SHA-256
    pq_path_2 = tmp_path / "fx2.parquet"
    pq_meta_2 = write_fx_rate_table(pq_path_2, table)
    assert pq_meta_2["sha256"] == pq_meta["sha256"]

    reloaded_pq = load_fx_rate_table(pq_path)
    reloaded_csv = load_fx_rate_table(csv_path)
    assert reloaded_pq.observation_count == 3
    assert reloaded_csv.observation_count == 3
    assert reloaded_pq.currencies == ("DKK", "EUR")
    assert reloaded_csv.currencies == ("DKK", "EUR")

    got_pq = reloaded_pq.lookup_spot("EUR", date(2024, 12, 31), accepted_at=avail)
    got_csv = reloaded_csv.lookup_spot("EUR", date(2024, 12, 31), accepted_at=avail)
    assert got_pq is not None and got_csv is not None
    assert got_pq.usd_per_unit == Decimal("1.0355")
    assert got_csv.usd_per_unit == Decimal("1.0355")
    assert got_pq.available_at == avail
    assert got_csv.available_at == avail

    # CSV omits available_at header when no observation sets available_at
    no_avail_table = SecFxRateTable.from_observations(
        [SecFxRateObservation("EUR", date(2024, 12, 30), Decimal("1.04"), "frankfurter_ecb")]
    )
    csv_plain = tmp_path / "plain.csv"
    write_fx_rate_table(csv_plain, no_avail_table)
    with csv_plain.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["currency", "rate_date", "usd_per_unit", "source"]
    assert csv_meta["observation_count"] == 3

    with pytest.raises(ValueError, match="unsupported"):
        write_fx_rate_table(tmp_path / "bad.json", table)
    with pytest.raises(ValueError, match="empty"):
        write_fx_rate_table(tmp_path / "empty.csv", SecFxRateTable.from_observations([]))
    assert not list(tmp_path.glob(".fx-rates-*"))


def test_asml_style_end_to_end_offline_fx_translation(tmp_path: Path) -> None:
    # Two Q1/FY days inside period + one future post-accepted_at day that must NOT leak
    payload = json.dumps(
        {
            "amount": 1.0,
            "base": "USD",
            "rates": {
                "2024-06-28": {"EUR": 0.8},
                "2024-12-31": {"EUR": 1.0},
                "2025-03-10": {"EUR": 0.5},  # after accepted_at 2025-03-05 -> forbidden look-ahead
            },
        }
    ).encode("utf-8")
    opener = _FakeOpener([_FakeResponse(payload)])
    fetched = fetch_frankfurter_fx_rates(
        start_date=date(2024, 1, 1),
        end_date=date(2025, 3, 10),
        currencies=("EUR",),
        opener=opener,
    )
    fx_file = tmp_path / "frankfurter.parquet"
    write_fx_rate_table(fx_file, fetched)
    reloaded = load_fx_rate_table(fx_file)

    asml_row = SecBulkFundamentalRow(
        cik="0000937966",
        entity_name="ASML HOLDING NV",
        symbols=("ASML",),
        accession_number="0000937966-25-000010",
        form="20-F",
        is_amendment=False,
        filing_date=date(2025, 3, 5),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 3, 5, 11, 0, tzinfo=UTC),
        fiscal_year=2024,
        fiscal_period="FY",
        period_kind="FY",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        taxonomy="us-gaap",
        reporting_currency="EUR",
        revenue=Decimal(28263000000),
        total_assets=Decimal(40000000000),
    )
    res = translate_bulk_fundamentals_to_usd([asml_row], fx_table=reloaded)
    out = res.rows[0]
    assert out.fx_status is SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX
    # Flow average over (2024-06-28 -> 1/0.8=1.25, 2024-12-31 -> 1/1.0=1.0) = 1.125
    assert out.fx_flow_rate_to_usd == Decimal("1.125")
    assert out.revenue_usd == Decimal("31795875000.000")
    # BS spot at period_end 2024-12-31 = 1.0
    assert out.fx_bs_rate_to_usd == Decimal(1)
    assert out.total_assets_usd == Decimal(40000000000)
    assert out.fx_flow_date == date(2024, 12, 31)
    assert out.fx_bs_date == date(2024, 12, 31)
    assert out.fx_flow_source == FRANKFURTER_FX_SOURCE


def test_cli_fetch_fx_rates_roundtrip_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ohmydata.providers.sec._cli_financials as cli_fin

    sample_table = SecFxRateTable.from_observations(
        [
            SecFxRateObservation(
                "CAD", date(2024, 12, 30), Decimal("0.695"), FRANKFURTER_FX_SOURCE
            ),
            SecFxRateObservation(
                "EUR", date(2024, 12, 30), Decimal("1.041"), FRANKFURTER_FX_SOURCE
            ),
        ]
    )
    captured_calls: list[dict[str, Any]] = []

    def fake_fetch(**kwargs: Any) -> SecFxRateTable:
        captured_calls.append(kwargs)
        return sample_table

    monkeypatch.setattr(cli_fin, "fetch_frankfurter_fx_rates", fake_fetch)

    out_pq = tmp_path / "fx.parquet"
    rc = cli_main(
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-12-31",
            "--currencies",
            "eur,cad",
            "--output",
            str(out_pq),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "sec financials fetch-fx-rates"
    assert payload["status"] == "completed"
    assert payload["source"] == FRANKFURTER_FX_SOURCE
    assert payload["format"] == "parquet"
    assert payload["observation_count"] == 2
    assert payload["currencies"] == ["CAD", "EUR"]
    assert Path(out_pq).is_file()
    assert load_fx_rate_table(out_pq).observation_count == 2
    assert captured_calls[0]["start_date"] == date(2024, 1, 1)
    assert captured_calls[0]["end_date"] == date(2024, 12, 31)
    assert captured_calls[0]["currencies"] == ("EUR", "CAD")

    out_csv = tmp_path / "fx.csv"
    rc_csv = cli_main(
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "2024-01-01",
            "--output",
            str(out_csv),
            "--json",
            "--quiet",
        ]
    )
    assert rc_csv == 0
    payload_csv = json.loads(capsys.readouterr().out)
    assert payload_csv["format"] == "csv"
    assert Path(out_csv).is_file()

    # Invalid flag cases exit with code 2 cleanly
    for bad_argv in (
        ["sec", "financials", "fetch-fx-rates", "--start-date", "2024-01-01"],
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "2024-01-01",
            "--output",
            str(tmp_path / "bad.json"),
        ],
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--output",
            str(tmp_path / "ok.parquet"),
        ],
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "20240101",
            "--output",
            str(tmp_path / "ok.parquet"),
        ],
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "2024-02-01",
            "--end-date",
            "2024-01-01",
            "--output",
            str(tmp_path / "ok.parquet"),
        ],
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "2024-01-01",
            "--source",
            "unknown_fx",
            "--output",
            str(tmp_path / "ok.parquet"),
        ],
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "2024-01-01",
            "--currencies",
            "",
            "--output",
            str(tmp_path / "ok.parquet"),
        ],
        [
            "sec",
            "financials",
            "fetch-fx-rates",
            "--start-date",
            "2024-01-01",
            "--currencies",
            " , ",
            "--output",
            str(tmp_path / "ok.parquet"),
        ],
    ):
        assert cli_main(bad_argv) == 2
        capsys.readouterr()


def _make_mini_zips(tmp_path: Path, *, include_asml: bool = True) -> tuple[Path, Path]:
    cf_zip = tmp_path / "companyfacts.zip"
    sub_zip = tmp_path / "submissions.zip"
    baba_cik = "0001577552"
    asml_cik = "0000937966"
    baba_accn = f"{baba_cik}-25-000001"
    asml_accn = f"{asml_cik}-25-000001"

    baba_cf = {
        "cik": int(baba_cik),
        "entityName": "Synthetic Alibaba Group",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "CNY": [
                            {
                                "val": 1_000_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                        "USD": [
                            {
                                "val": 140_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                    }
                },
                "Assets": {
                    "units": {
                        "CNY": [
                            {
                                "val": 2_000_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                        "USD": [
                            {
                                "val": 276_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                    }
                },
            }
        },
    }
    baba_sub = {
        "cik": baba_cik,
        "name": "Synthetic Alibaba Group",
        "tickers": ["BABA"],
        "filings": {
            "recent": {
                "accessionNumber": [baba_accn],
                "form": ["20-F"],
                "filingDate": ["2025-06-20"],
                "reportDate": ["2024-12-31"],
                "primaryDocument": ["baba-20f.htm"],
                "acceptanceDateTime": ["2025-06-20T12:00:00.000Z"],
            },
            "files": [],
        },
    }
    asml_cf = {
        "cik": int(asml_cik),
        "entityName": "Synthetic ASML NV",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "EUR": [
                            {
                                "val": 500_000,
                                "accn": asml_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-02-20",
                            }
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "EUR": [
                            {
                                "val": 1_000_000,
                                "accn": asml_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-12-31",
                                "filed": "2025-02-20",
                            }
                        ]
                    }
                },
            }
        },
    }
    asml_sub = {
        "cik": asml_cik,
        "name": "Synthetic ASML NV",
        "tickers": ["ASML"],
        "filings": {
            "recent": {
                "accessionNumber": [asml_accn],
                "form": ["20-F"],
                "filingDate": ["2025-02-20"],
                "reportDate": ["2024-12-31"],
                "primaryDocument": ["asml-20f.htm"],
                "acceptanceDateTime": ["2025-02-20T12:00:00.000Z"],
            },
            "files": [],
        },
    }
    with zipfile.ZipFile(cf_zip, "w") as zf:
        zf.writestr(f"CIK{baba_cik}.json", json.dumps(baba_cf))
        if include_asml:
            zf.writestr(f"CIK{asml_cik}.json", json.dumps(asml_cf))
    with zipfile.ZipFile(sub_zip, "w") as zf:
        zf.writestr(f"CIK{baba_cik}.json", json.dumps(baba_sub))
        if include_asml:
            zf.writestr(f"CIK{asml_cik}.json", json.dumps(asml_sub))
    return cf_zip, sub_zip


def test_auto_fx_translate_dataset_and_skips_when_unneeded(tmp_path: Path) -> None:
    cf_zip, sub_zip = _make_mini_zips(tmp_path, include_asml=True)
    bulk_dir = tmp_path / "bulk-src"
    build_bulk_companyfacts_dataset(cf_zip, sub_zip, bulk_dir)

    fx_table = SecFxRateTable.from_observations(
        [
            SecFxRateObservation("EUR", date(2024, 6, 30), Decimal("1.08"), FRANKFURTER_FX_SOURCE),
            SecFxRateObservation("EUR", date(2024, 12, 31), Decimal("1.04"), FRANKFURTER_FX_SOURCE),
        ]
    )
    calls: list[dict[str, Any]] = []

    def fake_fetcher(**kwargs: Any) -> SecFxRateTable:
        calls.append(kwargs)
        return fx_table

    res = translate_bulk_dataset_to_usd(bulk_dir, fx_fetcher=fake_fetcher)
    assert len(calls) == 1
    assert calls[0]["start_date"] == date(2024, 1, 1)
    assert calls[0]["end_date"] == date(2024, 12, 31)
    assert res.policy["fx_auto_fetched"] is True
    assert res.policy["fx_auto_fetch_source"] == FRANKFURTER_FX_SOURCE
    by_sym = {r.symbols[0]: r for r in res.rows}
    assert by_sym["ASML"].fx_status is SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX
    assert by_sym["ASML"].revenue_usd == Decimal("530000.00")
    assert by_sym["ASML"].total_assets_usd == Decimal("1040000.00")
    assert by_sym["BABA"].fx_status is SecFxTranslationStatus.TRANSLATED_FILING_IMPLIED

    # Explicit fx_table -> zero fetcher calls
    calls.clear()
    res_explicit = translate_bulk_dataset_to_usd(
        bulk_dir, fx_table=fx_table, fx_fetcher=fake_fetcher
    )
    assert calls == []
    assert res_explicit.policy["fx_auto_fetched"] is False

    # Opt-out auto_fetch_fx=False -> zero fetcher calls
    res_off = translate_bulk_dataset_to_usd(bulk_dir, auto_fetch_fx=False, fx_fetcher=fake_fetcher)
    assert calls == []
    assert res_off.policy["fx_auto_fetched"] is False

    # Dataset fully covered by USD / filing-implied FX -> zero fetcher calls even when auto_fetch_fx=True
    baba_only_dir = tmp_path / "baba-only"
    baba_only_dir.mkdir(parents=True, exist_ok=True)
    cf_only_baba, sub_only_baba = _make_mini_zips(baba_only_dir, include_asml=False)
    bulk_baba = tmp_path / "bulk-baba"
    build_bulk_companyfacts_dataset(cf_only_baba, sub_only_baba, bulk_baba)
    res_baba = translate_bulk_dataset_to_usd(bulk_baba, fx_fetcher=fake_fetcher)
    assert calls == []
    assert res_baba.policy["fx_auto_fetched"] is False

    # Explicit error propagation when auto-fetch fails
    def failing_fetcher(**kwargs: Any) -> SecFxRateTable:
        _ = kwargs
        raise TransientProviderError("network down")

    with pytest.raises(TransientProviderError, match="network down"):
        translate_bulk_dataset_to_usd(bulk_dir, fx_fetcher=failing_fetcher)


def test_oneshot_bulk_project_and_cli_auto_fx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ohmydata.providers.sec._cli_financials as cli_fin

    cf_zip, sub_zip = _make_mini_zips(tmp_path, include_asml=True)
    fx_table = SecFxRateTable.from_observations(
        [
            SecFxRateObservation("EUR", date(2024, 6, 30), Decimal("1.10"), FRANKFURTER_FX_SOURCE),
            SecFxRateObservation("EUR", date(2024, 12, 31), Decimal("1.00"), FRANKFURTER_FX_SOURCE),
        ]
    )
    fetch_calls: list[dict[str, Any]] = []

    def fake_fetch(**kwargs: Any) -> SecFxRateTable:
        fetch_calls.append(kwargs)
        return fx_table

    # 1. SDK one-shot build_bulk_companyfacts_dataset(..., usd_output_dir=...)
    raw_dir = tmp_path / "raw-sdk"
    usd_dir = tmp_path / "usd-sdk"
    manifest = build_bulk_companyfacts_dataset(
        cf_zip,
        sub_zip,
        raw_dir,
        usd_output_dir=usd_dir,
        fx_fetcher=fake_fetch,
    )
    assert len(fetch_calls) == 1
    assert validate_bulk_companyfacts_dataset(raw_dir)["counts"]["total_ciks"] == 2
    assert (
        validate_usd_translated_fundamentals_dataset(usd_dir)["status_counts"][
            "TRANSLATED_EXTERNAL_FX"
        ]
        == 1
    )
    assert manifest["usd_dataset"]["policy"]["fx_auto_fetched"] is True

    with pytest.raises(ValueError, match="must differ"):
        build_bulk_companyfacts_dataset(cf_zip, sub_zip, raw_dir, usd_output_dir=raw_dir)

    # 2. CLI translate-usd WITHOUT --fx-rates uses auto-FX by default; --no-auto-fx skips
    monkeypatch.setattr(cli_fin, "fetch_frankfurter_fx_rates", fake_fetch)
    fetch_calls.clear()
    cli_usd_auto = tmp_path / "cli-usd-auto"
    assert (
        cli_main(
            [
                "sec",
                "financials",
                "translate-usd",
                "--bulk-dataset",
                str(raw_dir),
                "--output",
                str(cli_usd_auto),
                "--json",
                "--quiet",
            ]
        )
        == 0
    )
    out_auto = json.loads(capsys.readouterr().out)
    assert len(fetch_calls) == 1
    assert out_auto["status_counts"]["TRANSLATED_EXTERNAL_FX"] == 1
    assert out_auto["policy"]["fx_auto_fetched"] is True

    fetch_calls.clear()
    cli_usd_off = tmp_path / "cli-usd-off"
    assert (
        cli_main(
            [
                "sec",
                "financials",
                "translate-usd",
                "--bulk-dataset",
                str(raw_dir),
                "--output",
                str(cli_usd_off),
                "--no-auto-fx",
                "--json",
                "--quiet",
            ]
        )
        == 0
    )
    out_off = json.loads(capsys.readouterr().out)
    assert fetch_calls == []
    assert out_off["status_counts"]["MISSING_FX_RATE"] == 1
    assert out_off["policy"]["fx_auto_fetched"] is False

    # 3. CLI bulk-project with --usd-output produces both datasets in one call
    raw_cli = tmp_path / "raw-cli"
    usd_cli = tmp_path / "usd-cli"
    assert (
        cli_main(
            [
                "sec",
                "financials",
                "bulk-project",
                "--companyfacts-zip",
                str(cf_zip),
                "--submissions-zip",
                str(sub_zip),
                "--output",
                str(raw_cli),
                "--usd-output",
                str(usd_cli),
                "--json",
                "--quiet",
            ]
        )
        == 0
    )
    out_bp = json.loads(capsys.readouterr().out)
    assert out_bp["status"] == "completed"
    assert out_bp["usd_output"] == str(usd_cli)
    assert out_bp["usd_dataset"]["status_counts"]["TRANSLATED_EXTERNAL_FX"] == 1
    assert len(fetch_calls) == 1

    # Reject --usd-output == --output
    assert (
        cli_main(
            [
                "sec",
                "financials",
                "bulk-project",
                "--companyfacts-zip",
                str(cf_zip),
                "--submissions-zip",
                str(sub_zip),
                "--output",
                str(raw_cli),
                "--usd-output",
                str(raw_cli),
                "--quiet",
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_review_regressions_spot_window_today_bound_fail_fast_and_cli_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ohmydata.providers.sec.fx_rates as fx_rates_mod
    from ohmydata.providers.sec._fx_translation_engine import resolve_auto_fx_table

    cf_zip, sub_zip = _make_mini_zips(tmp_path, include_asml=True)
    bulk_dir = tmp_path / "bulk-spot"
    build_bulk_companyfacts_dataset(cf_zip, sub_zip, bulk_dir)

    # P3: SPOT flow_method requests tight [period_end - max_staleness_days, period_end] window
    spot_calls: list[dict[str, Any]] = []
    spot_table = SecFxRateTable.from_observations(
        [SecFxRateObservation("EUR", date(2024, 12, 31), Decimal("1.04"), FRANKFURTER_FX_SOURCE)]
    )

    def spot_fetcher(**kwargs: Any) -> SecFxRateTable:
        spot_calls.append(kwargs)
        return spot_table

    res_spot = translate_bulk_dataset_to_usd(
        bulk_dir,
        flow_method=SecFxFlowMethod.SPOT,
        max_staleness_days=10,
        fx_fetcher=spot_fetcher,
    )
    assert spot_calls == [{"start_date": date(2024, 12, 21), "end_date": date(2024, 12, 31)}]
    assert res_spot.counts["TRANSLATED_EXTERNAL_FX"] == 1

    # P1: CLI default fx_fetcher=fetch_frankfurter_fx_rates uses open end_date=None when max_date >= today
    captured_urls: list[str] = []
    today_utc = datetime.now(UTC).date()
    yesterday_utc = today_utc - timedelta(days=1)

    def fake_bytes(**kwargs: Any) -> bytes:
        captured_urls.append(kwargs["url"])
        return json.dumps(
            {
                "amount": 1.0,
                "base": "USD",
                "rates": {yesterday_utc.isoformat(): {"EUR": 0.9}},
            }
        ).encode("utf-8")

    monkeypatch.setattr(fx_rates_mod, "_fetch_frankfurter_bytes", fake_bytes)
    today_row = SecBulkFundamentalRow(
        cik="0000937966",
        entity_name="ASML",
        symbols=("ASML",),
        accession_number="0000937966-26-000099",
        form="20-F",
        is_amendment=False,
        filing_date=today_utc,
        report_date=today_utc,
        accepted_at=datetime.now(UTC),
        fiscal_year=today_utc.year,
        fiscal_period="FY",
        period_kind="FY",
        period_start=yesterday_utc,
        period_end=today_utc,
        taxonomy="us-gaap",
        reporting_currency="EUR",
        revenue=Decimal(1000),
    )
    resolved, auto_meta = resolve_auto_fx_table(
        [today_row],
        implied_map={},
        fx_table=None,
        use_filing_implied=True,
        date_policy=SecFxDatePolicy.PERIOD_END_SPOT,
        flow_method=SecFxFlowMethod.PERIOD_AVERAGE,
        max_staleness_days=10,
        allow_cross_kind_implied=False,
        auto_fetch_fx=True,
        fx_fetcher=fetch_frankfurter_fx_rates,
    )
    assert resolved is not None
    assert auto_meta["fx_auto_fetch_end_date"] is None
    assert captured_urls and "..?" in captured_urls[0]

    # Custom mock fx_fetcher with today's date still receives exact end_date=today_utc
    mock_calls: list[dict[str, Any]] = []

    def mock_today_fetcher(**kwargs: Any) -> SecFxRateTable:
        mock_calls.append(kwargs)
        return spot_table

    resolve_auto_fx_table(
        [today_row],
        implied_map={},
        fx_table=None,
        use_filing_implied=True,
        date_policy=SecFxDatePolicy.PERIOD_END_SPOT,
        flow_method=SecFxFlowMethod.SPOT,
        max_staleness_days=10,
        allow_cross_kind_implied=False,
        auto_fetch_fx=True,
        fx_fetcher=mock_today_fetcher,
    )
    assert mock_calls[0]["end_date"] == today_utc

    # P2 SDK fail-fast: invalid FX params fail before running ZIP projection or creating output_dir
    uncreated_raw = tmp_path / "should-not-exist-raw"
    uncreated_usd = tmp_path / "should-not-exist-usd"
    with pytest.raises(ValueError, match="max_staleness_days"):
        build_bulk_companyfacts_dataset(
            cf_zip,
            sub_zip,
            uncreated_raw,
            usd_output_dir=uncreated_usd,
            max_staleness_days=-1,
        )
    assert not uncreated_raw.exists()
    assert not uncreated_usd.exists()

    # P2 & P3 CLI: USD flags without --usd-output and blank --usd-output fail cleanly
    for bad_argv in (
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(tmp_path / "out-flag-1"),
            "--no-auto-fx",
        ],
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(tmp_path / "out-flag-2"),
            "--date-policy",
            "PERIOD_END_SPOT",
        ],
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(tmp_path / "out-flag-3"),
            "--usd-output",
            "",
        ],
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(tmp_path / "out-flag-4"),
            "--usd-output",
            "   ",
        ],
    ):
        assert cli_main(bad_argv) == 2
        err_text = capsys.readouterr().err
        assert "usd-output" in err_text or "--usd-output" in err_text


def test_sec_fx_review_regressions(tmp_path: Path) -> None:
    from types import SimpleNamespace

    cf_zip, sub_zip = _make_mini_zips(tmp_path, include_asml=True)
    raw_dir = tmp_path / "raw-only"
    fx_tbl = SecFxRateTable.from_observations(
        [SecFxRateObservation("EUR", date(2024, 12, 31), Decimal("1.04"), "custom_ecb")]
    )
    with pytest.raises(ValueError, match="usd_output_dir"):
        build_bulk_companyfacts_dataset(cf_zip, sub_zip, raw_dir, fx_table=fx_tbl)
    assert not raw_dir.exists()

    # headers=None on Frankfurter response raises PermanentProviderError, not AttributeError
    class _NoneHeadersOpener:
        def open(self, req: Any, timeout: float = 30.0) -> Any:
            return SimpleNamespace(
                status=200,
                headers=None,
                read=lambda n=-1: b'{"base":"USD","amount":1,"rates":{}}',
                close=lambda: None,
            )

    with pytest.raises(PermanentProviderError, match="content type"):
        fetch_frankfurter_fx_rates(
            start_date=date(2024, 12, 30),
            end_date=date(2024, 12, 31),
            opener=_NoneHeadersOpener(),
        )

    # Custom fx_fetcher records actual observation source instead of hardcoded frankfurter_ecb
    bulk_dir = tmp_path / "bulk-src"
    build_bulk_companyfacts_dataset(cf_zip, sub_zip, bulk_dir)
    custom_res = translate_bulk_dataset_to_usd(
        bulk_dir,
        fx_fetcher=lambda **kwargs: SecFxRateTable.from_observations(
            [
                SecFxRateObservation("EUR", date(2024, 6, 30), Decimal("1.08"), "custom_ecb"),
                SecFxRateObservation("EUR", date(2024, 12, 31), Decimal("1.04"), "custom_ecb"),
            ]
        ),
    )
    assert custom_res.policy["fx_auto_fetch_source"] == "custom_ecb"
