"""Offline unit tests for the Sharadar Direct provider and local readers."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import zipfile
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path
from typing import Any, Self

import pandas as pd
import polars as pl
import pytest

from ohmydata.core.errors import (
    AuthenticationError,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimitError,
    RetryExhaustedError,
    SchemaMismatchError,
    TransientProviderError,
)
from ohmydata.core.policy import RetryPolicy
from ohmydata.core.provenance import EmptyDisposition
from ohmydata.providers.sharadar import (
    SHARADAR_CAPABILITIES,
    STANDARD_COLUMNS,
    SharadarAdjustmentMode,
    SharadarBulkTableRequest,
    SharadarClient,
    SharadarError,
    SharadarTable,
    classify_sharadar_http_error,
    read_local_actions,
    read_local_actions_pl,
    read_local_bars,
    read_local_bars_pl,
    redact_secret,
    scan_local_actions_pl,
    scan_local_bars_pl,
)


class _FakeResponse:
    """Minimal context-managed HTTP response for offline opener tests."""

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "https://api.sharadar.com/v1.0/data/stocks",
        chunk_size: int | None = None,
    ) -> None:
        self._stream = io.BytesIO(payload)
        self.status = status
        msg = Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        self.headers = msg
        self.url = url
        self._chunk_size = chunk_size

    def read(self, size: int = -1) -> bytes:
        if self._chunk_size is not None and (size < 0 or size > self._chunk_size):
            return self._stream.read(self._chunk_size)
        return self._stream.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stream.close()


def _make_zip_bytes(filename: str, content: bytes, *, corrupt_crc: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    raw = bytearray(buf.getvalue())
    if corrupt_crc:
        # Flip a byte inside the compressed stream near the beginning of the local entry payload
        raw[32] ^= 0xFF
    return bytes(raw)


def _http_error(
    url: str,
    code: int,
    reason: str,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, reason, Message(), None)


class TestErrorsAndRedaction:
    def test_redact_secret_plain_and_url_encoded(self) -> None:
        secret = "sk+test/123=abc def"
        plain_msg = f"Failed fetching https://api.sharadar.com/v1.0/data/stocks?api_key={secret}"
        quoted_msg = (
            "Failed fetching https://api.sharadar.com/v1.0/data/stocks?api_key="
            "sk%2Btest%2F123%3Dabc%20def"
        )
        plus_msg = (
            "Failed fetching https://api.sharadar.com/v1.0/data/stocks?api_key="
            "sk%2Btest%2F123%3Dabc+def"
        )

        for msg in (plain_msg, quoted_msg, plus_msg):
            redacted = redact_secret(msg, secret)
            assert secret not in redacted
            assert "sk%2Btest" not in redacted
            assert "api_key=***" in redacted

        assert redact_secret("no secret here", "") == "no secret here"

    @pytest.mark.parametrize(
        ("code", "expected_cls"),
        [
            (401, AuthenticationError),
            (403, PermissionDeniedError),
            (404, PermanentProviderError),
            (429, RateLimitError),
            (500, TransientProviderError),
            (502, TransientProviderError),
            (503, TransientProviderError),
            (504, TransientProviderError),
            (400, PermanentProviderError),
        ],
    )
    def test_classify_http_error_mapping_and_redaction(
        self, code: int, expected_cls: type[Exception]
    ) -> None:
        secret = "my+secret/key=99"
        err = classify_sharadar_http_error(
            code,
            f"denied for api_key={secret} encoded=my%2Bsecret%2Fkey%3D99",
            secret,
        )
        assert isinstance(err, expected_cls)
        assert err.provider_code == code
        assert secret not in str(err)
        assert "my%2Bsecret" not in str(err)
        assert "***" in str(err)


class TestEndpointsAndCapabilities:
    def test_capabilities_and_enums(self) -> None:
        assert SHARADAR_CAPABILITIES.supports_delisted is True
        assert SHARADAR_CAPABILITIES.has_permaticker is True
        assert SHARADAR_CAPABILITIES.eod_only is True
        assert SHARADAR_CAPABILITIES.single_venue_caveat_exchanges == ("BATS", "CBOE_BZX")
        assert [t.value for t in SharadarTable] == [
            "stocks",
            "funds",
            "tickers",
            "actions",
            "sp500",
            "fundamentals",
            "daily",
        ]
        assert [m.value for m in SharadarAdjustmentMode] == [
            "SPLIT_ADJUSTED",
            "UNADJUSTED",
            "TOTAL_RETURN",
        ]

    def test_request_validation(self, tmp_path: Path) -> None:
        req = SharadarBulkTableRequest(
            table="stocks",
            years=5,
            output_dir=str(tmp_path),
            keep_zip=False,
            timeout=120,
        )
        assert req.table is SharadarTable.STOCKS
        assert req.years == "5"
        assert req.output_dir == tmp_path
        assert req.keep_zip is False
        assert req.timeout == 120.0

        with pytest.raises(ValueError):
            SharadarBulkTableRequest(table="unknown_table", output_dir=tmp_path)
        with pytest.raises(ValueError):
            SharadarBulkTableRequest(table="stocks", years="invalid", output_dir=tmp_path)
        with pytest.raises(TypeError):
            SharadarBulkTableRequest(table="stocks", years=True, output_dir=tmp_path)
        with pytest.raises(ValueError):
            SharadarBulkTableRequest(table="stocks", timeout=0.0, output_dir=tmp_path)
        with pytest.raises(ValueError):
            SharadarBulkTableRequest(table="stocks", output_dir="   ")


class TestSharadarClient:
    def test_client_requires_explicit_api_key_never_reads_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHARADAR_API_KEY", "env-should-never-be-read")
        with pytest.raises(AuthenticationError):
            SharadarClient("")
        with pytest.raises(AuthenticationError):
            SharadarClient("   ")

        client = SharadarClient("explicit+key/1")
        assert "explicit+key/1" not in repr(client)
        assert "***" in repr(client)

    def test_fetch_status_success_and_secret_redaction(self) -> None:
        secret = "secret+status/42"
        captured_urls: list[str] = []
        fixed_now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

        def fake_opener(req: Any, timeout: float = 30.0) -> _FakeResponse:
            captured_urls.append(req.full_url)
            return _FakeResponse(
                f"available for api_key={secret}".encode(),
                status=200,
            )

        client = SharadarClient(
            secret,
            opener=fake_opener,
            clock=lambda: fixed_now,
        )
        res = client.fetch_status(SharadarTable.STOCKS, years="full")

        assert len(captured_urls) == 1
        assert "status=True" in captured_urls[0]
        assert "years=full" in captured_urls[0]
        assert res.table is SharadarTable.STOCKS
        assert res.years == "full"
        assert res.available is True
        assert res.status_code == 200
        assert secret not in res.message
        assert "***" in res.message
        assert res.provenance is not None
        assert res.provenance.retrieved_at == fixed_now
        prov_json = json.dumps(res.provenance.to_dict())
        assert secret not in prov_json

    def test_download_zip_keep_and_discard_zip(self, tmp_path: Path) -> None:
        secret = "zip-secret+key=1"
        csv_bytes = (
            b"ticker,date,open,high,low,close,volume,closeadj,closeunadj\n"
            b"AAPL,2026-01-02,100.0,105.0,99.0,104.0,1000,102.0,208.0\n"
            b"MSFT,2026-01-02,200.0,205.0,198.0,202.0,2000,200.0,202.0\n"
        )
        zip_bytes = _make_zip_bytes("SHARADAR_SEP.csv", csv_bytes)
        expected_csv_sha = hashlib.sha256(csv_bytes).hexdigest()
        expected_zip_sha = hashlib.sha256(zip_bytes).hexdigest()

        def fake_opener(req: Any, timeout: float = 300.0) -> _FakeResponse:
            return _FakeResponse(
                zip_bytes,
                headers={
                    "Content-Length": str(len(zip_bytes)),
                    "Content-Type": "application/zip",
                },
                chunk_size=19,
            )

        client_a = SharadarClient(
            secret,
            opener=fake_opener,
            clock=lambda: datetime(2026, 5, 2, tzinfo=UTC),
        )
        out_keep = tmp_path / "keep"
        res_keep = client_a.download_table(
            SharadarBulkTableRequest(
                table=SharadarTable.STOCKS,
                years="10",
                output_dir=out_keep,
                keep_zip=True,
            )
        )
        assert res_keep.was_zip is True
        assert res_keep.csv_path == out_keep / "stocks.csv"
        assert res_keep.csv_path.read_bytes() == csv_bytes
        assert res_keep.csv_bytes == len(csv_bytes)
        assert res_keep.csv_sha256 == expected_csv_sha
        assert res_keep.zip_path == out_keep / "stocks.csv.zip"
        assert res_keep.zip_path.read_bytes() == zip_bytes
        assert res_keep.zip_bytes == len(zip_bytes)
        assert res_keep.zip_sha256 == expected_zip_sha
        assert not [p for p in out_keep.iterdir() if p.name.endswith(".tmp")]

        assert res_keep.provenance is not None
        assert res_keep.provenance.row_count == 2
        assert res_keep.provenance.columns == (
            "ticker",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "closeadj",
            "closeunadj",
        )
        assert res_keep.provenance.empty_disposition is EmptyDisposition.NOT_EMPTY
        assert f"csv:sha256:{expected_csv_sha}" in res_keep.provenance.snapshot_identities
        assert f"zip:sha256:{expected_zip_sha}" in res_keep.provenance.snapshot_identities
        assert secret not in json.dumps(res_keep.provenance.to_dict())

        # Verify request_identity is independent of api_key
        client_b = SharadarClient("completely-different-secret", opener=fake_opener)
        res_b = client_b.download_table(
            SharadarBulkTableRequest(
                table=SharadarTable.STOCKS,
                years="10",
                output_dir=tmp_path / "other",
                keep_zip=True,
            )
        )
        assert res_b.provenance is not None
        assert res_b.provenance.request_identity == res_keep.provenance.request_identity

        # Verify keep_zip=False removes ZIP while preserving extracted CSV
        out_no_zip = tmp_path / "nozip"
        res_no_zip = client_a.download_table(
            SharadarBulkTableRequest(
                table=SharadarTable.STOCKS,
                years="10",
                output_dir=out_no_zip,
                keep_zip=False,
            )
        )
        assert res_no_zip.was_zip is True
        assert res_no_zip.zip_path is None
        assert res_no_zip.zip_bytes == len(zip_bytes)
        assert res_no_zip.zip_sha256 == expected_zip_sha
        assert (out_no_zip / "stocks.csv").read_bytes() == csv_bytes
        assert not (out_no_zip / "stocks.csv.zip").exists()

    def test_download_plain_csv_and_empty_header_only_csv(self, tmp_path: Path) -> None:
        csv_bytes = b"ticker,date,action,value\nAAPL,2026-01-05,split,4.0\n"
        client = SharadarClient(
            "plain-secret",
            opener=lambda req, timeout=300.0: _FakeResponse(
                csv_bytes,
                headers={"Content-Length": str(len(csv_bytes)), "Content-Type": "text/csv"},
            ),
        )
        res = client.download_table(
            SharadarBulkTableRequest(table=SharadarTable.ACTIONS, output_dir=tmp_path)
        )
        assert res.was_zip is False
        assert res.zip_path is None
        assert res.csv_path.read_bytes() == csv_bytes
        assert res.csv_sha256 == hashlib.sha256(csv_bytes).hexdigest()
        assert res.provenance is not None
        assert res.provenance.row_count == 1
        assert res.provenance.empty_disposition is EmptyDisposition.NOT_EMPTY

        header_only = b"ticker,date,action,value\n"
        empty_client = SharadarClient(
            "plain-secret",
            opener=lambda req, timeout=300.0: _FakeResponse(
                header_only,
                headers={"Content-Length": str(len(header_only)), "Content-Type": "text/csv"},
            ),
        )
        empty_res = empty_client.download_table(
            SharadarBulkTableRequest(table=SharadarTable.ACTIONS, output_dir=tmp_path / "empty")
        )
        assert empty_res.provenance is not None
        assert empty_res.provenance.row_count == 0
        assert empty_res.provenance.empty_disposition is EmptyDisposition.ALLOWED_EMPTY

    def test_truncated_content_length_raises_and_preserves_existing_file(
        self, tmp_path: Path
    ) -> None:
        target_csv = tmp_path / "stocks.csv"
        target_csv.write_text("existing-valid-data", encoding="utf-8")

        client = SharadarClient(
            "trunc-secret",
            opener=lambda req, timeout=300.0: _FakeResponse(
                b"ticker,date\nAAPL,2026",
                headers={"Content-Length": "100", "Content-Type": "text/csv"},
            ),
        )
        with pytest.raises(SharadarError, match="truncated download"):
            client.download_table(
                SharadarBulkTableRequest(table=SharadarTable.STOCKS, output_dir=tmp_path)
            )
        assert target_csv.read_text(encoding="utf-8") == "existing-valid-data"
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    def test_corrupt_zip_raises_and_cleans_temp_files(self, tmp_path: Path) -> None:
        # Case 1: ZIP magic / Content-Type but truncated non-zip body
        bad_zip_bytes = b"PK\x03\x04corrupt-zip-payload"
        client_bad_header = SharadarClient(
            "zip-err-secret",
            opener=lambda req, timeout=300.0: _FakeResponse(
                bad_zip_bytes,
                headers={
                    "Content-Length": str(len(bad_zip_bytes)),
                    "Content-Type": "application/zip",
                },
            ),
        )
        with pytest.raises(SharadarError, match="corrupt or truncated"):
            client_bad_header.download_table(
                SharadarBulkTableRequest(table=SharadarTable.STOCKS, output_dir=tmp_path)
            )
        assert not (tmp_path / "stocks.csv").exists()
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

        # Case 2: Valid central directory but corrupted compressed stream
        corrupt_stream_zip = _make_zip_bytes(
            "SHARADAR_SEP.csv",
            b"ticker,date,close\n" + b"AAPL,2026-01-02,100.0\n" * 20,
            corrupt_crc=True,
        )
        client_bad_crc = SharadarClient(
            "zip-err-secret",
            opener=lambda req, timeout=300.0: _FakeResponse(
                corrupt_stream_zip,
                headers={
                    "Content-Length": str(len(corrupt_stream_zip)),
                    "Content-Type": "application/zip",
                },
            ),
        )
        with pytest.raises(SharadarError, match="corrupt or truncated"):
            client_bad_crc.download_table(
                SharadarBulkTableRequest(table=SharadarTable.STOCKS, output_dir=tmp_path)
            )
        assert not (tmp_path / "stocks.csv").exists()
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    @pytest.mark.parametrize(
        ("status_code", "expected_exc"),
        [
            (401, AuthenticationError),
            (403, PermissionDeniedError),
            (404, PermanentProviderError),
        ],
    )
    def test_http_401_403_404_fail_fast_and_redact_secret(
        self,
        tmp_path: Path,
        status_code: int,
        expected_exc: type[Exception],
    ) -> None:
        secret = "failfast+secret/99"
        calls = 0
        sleeps: list[float] = []

        def fake_opener(req: Any, timeout: float = 300.0) -> Any:
            nonlocal calls
            calls += 1
            raise _http_error(
                req.full_url,
                status_code,
                f"rejected url={req.full_url}",
            )

        client = SharadarClient(
            secret,
            opener=fake_opener,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=1.0),
            sleep_fn=sleeps.append,
        )
        with pytest.raises(expected_exc) as exc_info:
            client.download_table(
                SharadarBulkTableRequest(table=SharadarTable.STOCKS, output_dir=tmp_path)
            )

        assert calls == 1
        assert sleeps == []
        assert secret not in str(exc_info.value)
        assert "failfast%2Bsecret" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None

        # Also fail-fast on fetch_status
        calls = 0
        with pytest.raises(expected_exc) as status_exc:
            client.fetch_status(SharadarTable.STOCKS)
        assert calls == 1
        assert sleeps == []
        assert secret not in str(status_exc.value)

    def test_429_and_503_retry_then_succeed_and_exhaustion(self, tmp_path: Path) -> None:
        secret = "retry+secret=7"
        csv_bytes = b"ticker,date,open,high,low,close,volume,closeadj,closeunadj\nAAPL,2026-01-02,10,11,9,10,100,10,10\n"
        sleeps: list[float] = []
        attempt = 0

        def flaky_opener(req: Any, timeout: float = 300.0) -> _FakeResponse:
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise _http_error(req.full_url, 429, f"rate limited api_key={secret}")
            if attempt == 2:
                raise _http_error(req.full_url, 503, f"unavailable api_key={secret}")
            return _FakeResponse(csv_bytes, headers={"Content-Length": str(len(csv_bytes))})

        client = SharadarClient(
            secret,
            opener=flaky_opener,
            retry_policy=RetryPolicy(
                max_attempts=3,
                base_delay_seconds=0.5,
                backoff_multiplier=2.0,
                max_delay_seconds=4.0,
                jitter_ratio=0.0,
            ),
            sleep_fn=sleeps.append,
            random_value_fn=lambda: 0.5,
        )
        res = client.download_table(
            SharadarBulkTableRequest(table=SharadarTable.STOCKS, output_dir=tmp_path)
        )
        assert attempt == 3
        assert sleeps == [0.5, 1.0]
        assert res.provenance is not None
        assert res.provenance.attempt_count == 3
        assert res.provenance.attempts[0].exception_type == "RateLimitError"
        assert res.provenance.attempts[1].exception_type == "TransientProviderError"
        assert res.provenance.attempts[2].exception_type is None

        # Exhaustion must not leak secret in RetryExhaustedError or its __cause__
        exhaust_sleeps: list[float] = []

        def always_429(req: Any, timeout: float = 300.0) -> Any:
            raise _http_error(req.full_url, 429, f"still limited {req.full_url}")

        exhaust_client = SharadarClient(
            secret,
            opener=always_429,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.25),
            sleep_fn=exhaust_sleeps.append,
        )
        with pytest.raises(RetryExhaustedError) as exc_info:
            exhaust_client.fetch_status(SharadarTable.STOCKS)
        assert exhaust_sleeps == [0.25]
        assert secret not in str(exc_info.value)
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, RateLimitError)
        assert secret not in str(exc_info.value.__cause__)
        assert "retry%2Bsecret" not in str(exc_info.value.__cause__)


class TestLocalReaders:
    def _sample_bars_df(self) -> pd.DataFrame:
        # Sharadar native bars: OHLC is split-adjusted; closeadj is total return; closeunadj is raw.
        return pd.DataFrame(
            {
                "ticker": ["MSFT", "AAPL", "AAPL"],
                "date": ["2026-01-03", "2026-01-03", "2026-01-02"],
                "open": [200.0, 100.0, 95.0],
                "high": [210.0, 105.0, 100.0],
                "low": [190.0, 95.0, 90.0],
                "close": [205.0, 100.0, 98.0],
                "volume": [2000, 1000, None],
                "closeadj": [205.0, 90.0, 88.2],
                "closeunadj": [205.0, 200.0, 196.0],
            }
        )

    @pytest.mark.parametrize("fmt", ["csv", "parquet"])
    def test_read_local_bars_all_adjustment_modes(self, tmp_path: Path, fmt: str) -> None:
        raw_df = self._sample_bars_df()
        file_path = tmp_path / f"stocks.{fmt}"
        if fmt == "csv":
            raw_df.to_csv(file_path, index=False)
        else:
            raw_df.to_parquet(file_path, index=False)

        # 1. SPLIT_ADJUSTED: native OHLC + adj_close=closeadj, sorted by (symbol, date)
        split_df = read_local_bars(
            file_path,
            adjustment_mode=SharadarAdjustmentMode.SPLIT_ADJUSTED,
        )
        assert tuple(split_df.columns) == STANDARD_COLUMNS
        assert split_df["symbol"].tolist() == ["AAPL", "AAPL", "MSFT"]
        assert split_df["date"].tolist() == [
            date(2026, 1, 2),
            date(2026, 1, 3),
            date(2026, 1, 3),
        ]
        # Missing volume preserved as NaN (never silently zeroed)
        assert pd.isna(split_df.loc[0, "volume"])
        assert split_df.loc[1, "open"] == pytest.approx(100.0)
        assert split_df.loc[1, "close"] == pytest.approx(100.0)
        assert split_df.loc[1, "adj_close"] == pytest.approx(90.0)

        # 2. UNADJUSTED: rescaled via closeunadj / close (200 / 100 = 2.0 for AAPL)
        unadj_df = read_local_bars(
            file_path,
            symbols=["AAPL"],
            start_date="2026-01-03",
            end_date="2026-01-03",
            adjustment_mode=SharadarAdjustmentMode.UNADJUSTED,
        )
        assert len(unadj_df) == 1
        assert unadj_df.loc[0, "open"] == pytest.approx(200.0)
        assert unadj_df.loc[0, "high"] == pytest.approx(210.0)
        assert unadj_df.loc[0, "low"] == pytest.approx(190.0)
        assert unadj_df.loc[0, "close"] == pytest.approx(200.0)
        assert unadj_df.loc[0, "adj_close"] == pytest.approx(90.0)
        assert unadj_df.loc[0, "volume"] == pytest.approx(1000.0)

        # 3. TOTAL_RETURN: rescaled via closeadj / close (90 / 100 = 0.9 for AAPL)
        tr_df = read_local_bars(
            file_path,
            symbols=["AAPL"],
            start_date=date(2026, 1, 3),
            end_date=date(2026, 1, 3),
            adjustment_mode=SharadarAdjustmentMode.TOTAL_RETURN,
        )
        assert len(tr_df) == 1
        assert tr_df.loc[0, "open"] == pytest.approx(90.0)
        assert tr_df.loc[0, "high"] == pytest.approx(94.5)
        assert tr_df.loc[0, "low"] == pytest.approx(85.5)
        assert tr_df.loc[0, "close"] == pytest.approx(90.0)
        assert tr_df.loc[0, "adj_close"] == pytest.approx(90.0)

    def test_read_local_bars_schema_error_and_zero_close_guard(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "bad.csv"
        pd.DataFrame({"ticker": ["AAPL"], "date": ["2026-01-02"], "close": [10.0]}).to_csv(
            bad_path, index=False
        )
        with pytest.raises(SchemaMismatchError):
            read_local_bars(bad_path)

        zero_path = tmp_path / "zero.csv"
        pd.DataFrame(
            {
                "ticker": ["AAPL"],
                "date": ["2026-01-02"],
                "open": [10.0],
                "high": [10.0],
                "low": [10.0],
                "close": [0.0],
                "volume": [100],
                "closeadj": [9.0],
                "closeunadj": [10.0],
            }
        ).to_csv(zero_path, index=False)
        res = read_local_bars(zero_path, adjustment_mode=SharadarAdjustmentMode.TOTAL_RETURN)
        assert pd.isna(res.loc[0, "open"])
        assert res.loc[0, "close"] == pytest.approx(9.0)

    @pytest.mark.parametrize("fmt", ["csv", "parquet"])
    def test_read_local_actions(self, tmp_path: Path, fmt: str) -> None:
        actions_df = pd.DataFrame(
            {
                "date": ["2026-01-05", "2026-01-02", "2026-01-03"],
                "action": ["Split", "dividend", "spinoff"],
                "ticker": ["AAPL", "MSFT", "AAPL"],
                "name": ["Apple Inc", "Microsoft Corp", "Apple Inc"],
                "value": [4.0, 0.75, None],
            }
        )
        file_path = tmp_path / f"actions.{fmt}"
        if fmt == "csv":
            actions_df.to_csv(file_path, index=False)
        else:
            actions_df.to_parquet(file_path, index=False)

        filtered = read_local_actions(
            file_path,
            symbols=["AAPL"],
            start_date="2026-01-03",
            end_date="2026-01-05",
        )
        assert filtered["symbol"].tolist() == ["AAPL", "AAPL"]
        assert filtered["date"].tolist() == [date(2026, 1, 3), date(2026, 1, 5)]
        assert filtered["action"].tolist() == ["spinoff", "split"]
        assert pd.isna(filtered.loc[0, "value"])
        assert filtered.loc[1, "value"] == pytest.approx(4.0)

    def test_na_nan_null_tickers_preserved_in_pandas_and_polars(self, tmp_path: Path) -> None:
        bars_csv = tmp_path / "na_stocks.csv"
        bars_csv.write_text(
            "ticker,date,open,high,low,close,volume,closeadj,closeunadj\n"
            "NA,2026-01-02,10.0,11.0,9.5,10.5,,10.5,10.5\n"
            "NAN,2026-01-02,20.0,21.0,19.5,20.5,500,20.5,20.5\n"
            "NULL,2026-01-02,30.0,31.0,29.5,30.5,600,30.5,30.5\n"
            "N/A,2026-01-02,40.0,41.0,39.5,40.5,700,40.5,40.5\n",
            encoding="utf-8",
        )
        pd_bars = read_local_bars(bars_csv, symbols=["NA", "NAN", "NULL", "N/A"])
        pl_bars = read_local_bars_pl(bars_csv, symbols=["NA", "NAN", "NULL", "N/A"])
        assert pd_bars["symbol"].tolist() == ["N/A", "NA", "NAN", "NULL"]
        assert pl_bars["symbol"].to_list() == ["N/A", "NA", "NAN", "NULL"]
        assert pd.isna(pd_bars.loc[pd_bars["symbol"] == "NA", "volume"].iloc[0])
        assert pl_bars.filter(pl.col("symbol") == "NA")["volume"].to_list() == [None]

        actions_csv = tmp_path / "na_actions.csv"
        actions_csv.write_text(
            "date,action,ticker,name,value\n"
            "2026-01-05,dividend,NA,Nano Alpha Inc,0.25\n"
            "2026-01-05,split,NAN,North Am Corp,2.0\n",
            encoding="utf-8",
        )
        pd_act = read_local_actions(actions_csv, symbols=["NA", "NAN"])
        pl_act = read_local_actions_pl(actions_csv, symbols=["NA", "NAN"])
        assert pd_act["symbol"].tolist() == ["NA", "NAN"]
        assert pl_act["symbol"].to_list() == ["NA", "NAN"]

    @staticmethod
    def _assert_bars_cell_parity(pd_df: pd.DataFrame, pl_df: pl.DataFrame) -> None:
        assert tuple(pl_df.columns) == tuple(pd_df.columns)
        assert pl_df.height == len(pd_df)
        assert pl_df["symbol"].to_list() == pd_df["symbol"].tolist()
        assert pl_df["date"].to_list() == pd_df["date"].tolist()
        for col in ("open", "high", "low", "close", "adj_close", "volume"):
            for pv, lv in zip(pd_df[col].tolist(), pl_df[col].to_list(), strict=True):
                if pd.isna(pv):
                    assert lv is None
                else:
                    assert lv == pytest.approx(float(pv), rel=1e-12, abs=1e-12)

    @pytest.mark.parametrize("fmt", ["csv", "parquet"])
    def test_polars_scan_and_read_cell_parity_with_pandas(self, tmp_path: Path, fmt: str) -> None:
        raw_df = self._sample_bars_df()
        file_path = tmp_path / f"stocks_parity.{fmt}"
        if fmt == "csv":
            raw_df.to_csv(file_path, index=False)
        else:
            raw_df.to_parquet(file_path, index=False)

        for mode in SharadarAdjustmentMode:
            lf = scan_local_bars_pl(file_path, adjustment_mode=mode)
            assert isinstance(lf, pl.LazyFrame)
            pl_full = read_local_bars_pl(file_path, adjustment_mode=mode)
            assert isinstance(pl_full, pl.DataFrame)
            pd_full = read_local_bars(file_path, adjustment_mode=mode)
            self._assert_bars_cell_parity(pd_full, lf.collect())
            self._assert_bars_cell_parity(pd_full, pl_full)

            # Filtered query + empty match
            pd_sub = read_local_bars(
                file_path,
                symbols=["AAPL"],
                start_date="2026-01-03",
                end_date="2026-01-03",
                adjustment_mode=mode,
            )
            pl_sub = read_local_bars_pl(
                file_path,
                symbols=["AAPL"],
                start_date="2026-01-03",
                end_date="2026-01-03",
                adjustment_mode=mode,
            )
            self._assert_bars_cell_parity(pd_sub, pl_sub)

        pl_empty = read_local_bars_pl(file_path, symbols=["MISSING"])
        assert pl_empty.height == 0
        assert tuple(pl_empty.columns) == STANDARD_COLUMNS

        # Zero close guard parity
        zero_path = tmp_path / f"zero_parity.{fmt}"
        zero_df = pd.DataFrame(
            {
                "ticker": ["AAPL"],
                "date": ["2026-01-02"],
                "open": [10.0],
                "high": [10.0],
                "low": [10.0],
                "close": [0.0],
                "volume": [100],
                "closeadj": [9.0],
                "closeunadj": [10.0],
            }
        )
        if fmt == "csv":
            zero_df.to_csv(zero_path, index=False)
        else:
            zero_df.to_parquet(zero_path, index=False)
        self._assert_bars_cell_parity(
            read_local_bars(zero_path, adjustment_mode=SharadarAdjustmentMode.TOTAL_RETURN),
            read_local_bars_pl(zero_path, adjustment_mode=SharadarAdjustmentMode.TOTAL_RETURN),
        )

        # Actions parity
        act_df = pd.DataFrame(
            {
                "date": ["2026-01-05", "2026-01-02", "2026-01-03"],
                "action": ["Split", "dividend", "spinoff"],
                "ticker": ["AAPL", "MSFT", "AAPL"],
                "name": ["Apple Inc", "Microsoft Corp", "Apple Inc"],
                "value": [4.0, 0.75, None],
            }
        )
        act_path = tmp_path / f"actions_parity.{fmt}"
        if fmt == "csv":
            act_df.to_csv(act_path, index=False)
        else:
            act_df.to_parquet(act_path, index=False)

        act_lf = scan_local_actions_pl(act_path, symbols=["AAPL"], start_date="2026-01-03")
        assert isinstance(act_lf, pl.LazyFrame)
        pl_act = read_local_actions_pl(act_path, symbols=["AAPL"], start_date="2026-01-03")
        pd_act = read_local_actions(act_path, symbols=["AAPL"], start_date="2026-01-03")
        assert tuple(pl_act.columns) == tuple(pd_act.columns)
        assert pl_act["symbol"].to_list() == pd_act["symbol"].tolist()
        assert pl_act["date"].to_list() == pd_act["date"].tolist()
        assert pl_act["action"].to_list() == pd_act["action"].tolist()
        assert pl_act["name"].to_list() == pd_act["name"].tolist()
        for pv, lv in zip(pd_act["value"].tolist(), pl_act["value"].to_list(), strict=True):
            if pd.isna(pv):
                assert lv is None
            else:
                assert lv == pytest.approx(float(pv))

        bad_path = tmp_path / f"bad_parity.{fmt}"
        bad_df = pd.DataFrame({"ticker": ["AAPL"], "date": ["2026-01-02"], "close": [10.0]})
        if fmt == "csv":
            bad_df.to_csv(bad_path, index=False)
        else:
            bad_df.to_parquet(bad_path, index=False)
        with pytest.raises(SchemaMismatchError):
            scan_local_bars_pl(bad_path)

    def test_polars_csv_late_float_na_and_date_formats(self, tmp_path: Path) -> None:
        # >150 rows starting as integers before late decimal and "NA" values in numeric columns
        bars_csv = tmp_path / "late_float_bars.csv"
        bar_lines = ["ticker,date,open,high,low,close,volume,closeadj"]
        for _ in range(150):
            bar_lines.append("AAPL,2026-01-02,10,11,9,10,100,10")
        bar_lines.append("AAPL,2026-01-05,10.5,11.5,9.5,10.5,100,NA")
        bars_csv.write_text("\n".join(bar_lines) + "\n", encoding="utf-8")

        pd_bars = read_local_bars(bars_csv)
        pl_bars = read_local_bars_pl(bars_csv)
        self._assert_bars_cell_parity(pd_bars, pl_bars)

        actions_csv = tmp_path / "late_float_actions.csv"
        act_lines = ["ticker,date,action,value"]
        for _ in range(150):
            act_lines.append("AAPL,2026-01-02,split,2")
        act_lines.append("AAPL,2026-01-05,dividend,0.75")
        act_lines.append("AAPL,2026-01-06,spinoff,NA")
        actions_csv.write_text("\n".join(act_lines) + "\n", encoding="utf-8")

        pd_act = read_local_actions(actions_csv)
        pl_act = read_local_actions_pl(actions_csv)
        assert pl_act.height == pd_act.shape[0] == 152
        for pv, lv in zip(pd_act["value"].tolist(), pl_act["value"].to_list(), strict=True):
            if pd.isna(pv):
                assert lv is None
            else:
                assert lv == pytest.approx(float(pv))

        # Date format parity (YYYY/MM/DD CSV, YYYYMMDD Parquet/CSV) and fail-closed invalid dates
        slash_csv = tmp_path / "date_slash.csv"
        slash_csv.write_text(
            "ticker,date,open,high,low,close,volume,closeadj\nAAPL,2026/01/02,10,11,9,10,100,10\n",
            encoding="utf-8",
        )
        self._assert_bars_cell_parity(read_local_bars(slash_csv), read_local_bars_pl(slash_csv))

        ymd_pq = tmp_path / "date_ymd.parquet"
        pd.DataFrame(
            {
                "ticker": ["AAPL"],
                "date": ["20260105"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.0],
                "volume": [100],
                "closeadj": [10.0],
            }
        ).to_parquet(ymd_pq, index=False)
        self._assert_bars_cell_parity(read_local_bars(ymd_pq), read_local_bars_pl(ymd_pq))

        ymd_csv = tmp_path / "date_ymd.csv"
        ymd_csv.write_text(
            "ticker,date,open,high,low,close,volume,closeadj\nAAPL,20260105,10,11,9,10,100,10\n",
            encoding="utf-8",
        )
        assert read_local_bars_pl(ymd_csv)["date"].to_list() == [date(2026, 1, 5)]

        bad_date_csv = tmp_path / "bad_date.csv"
        bad_date_csv.write_text(
            "ticker,date,open,high,low,close,volume,closeadj\nAAPL,not-a-date,10,11,9,10,100,10\n",
            encoding="utf-8",
        )
        with pytest.raises((pl.exceptions.InvalidOperationError, ValueError)):
            read_local_bars_pl(bad_date_csv)
