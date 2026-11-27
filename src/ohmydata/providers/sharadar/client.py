"""Offline-testable stdlib client for Sharadar Direct v1.0 bulk tables."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ohmydata.core.errors import AuthenticationError, PermanentProviderError, TransientProviderError
from ohmydata.core.policy import RetryPolicy, execute_with_retry
from ohmydata.core.provenance import EmptyDisposition, FetchProvenance
from ohmydata.core.specs import RequestSpec

from ._payload import CHUNK_SIZE, CsvLineCounter, DownloadedPayload, extract_zip_payload
from .endpoints import (
    SharadarBulkDownloadResult,
    SharadarBulkTableRequest,
    SharadarTable,
    SharadarTableStatusResult,
    normalize_table,
    normalize_years,
)
from .errors import (
    SharadarTruncatedDownloadError,
    classify_sharadar_http_error,
    redact_secret,
)

DEFAULT_BASE_URL = "https://api.sharadar.com/v1.0/data"
_USER_AGENT = "ohmydata-sharadar/0.1"
_MAX_STATUS_BYTES = 65536


def _validate_base_url(base_url: str) -> str:
    cleaned = base_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Sharadar base_url must be a clean https:// URL without credentials")
    return cleaned


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject non-HTTPS redirects and cross-host redirects that could leak api_key."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        src = urllib.parse.urlsplit(req.full_url)
        dst = urllib.parse.urlsplit(newurl)
        if dst.scheme.lower() != "https":
            raise PermanentProviderError("Sharadar non-HTTPS redirect rejected")
        if dst.hostname != src.hostname and (
            self._api_key in dst.query or urllib.parse.quote(self._api_key, safe="") in dst.query
        ):
            raise PermanentProviderError("Sharadar cross-host redirect rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SharadarClient:
    """Stdlib-only client for Sharadar Direct v1.0 bulk CSV/ZIP downloads."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        opener: Callable[..., Any] | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        random_value_fn: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise AuthenticationError("Sharadar api_key must be a non-empty string")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")

        self._api_key = api_key.strip()
        self._base_url = _validate_base_url(base_url)
        self._opener = (
            opener or urllib.request.build_opener(_HttpsOnlyRedirectHandler(self._api_key)).open
        )
        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=3,
            base_delay_seconds=1.0,
            backoff_multiplier=2.0,
            max_delay_seconds=8.0,
            jitter_ratio=0.0,
        )
        self._sleep_fn = sleep_fn or time.sleep
        self._random_value_fn = random_value_fn or random.random
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return f"SharadarClient(base_url={self._base_url!r}, api_key='***')"

    def _build_url(
        self,
        table: SharadarTable,
        *,
        years: str | None = None,
        status_only: bool = False,
    ) -> str:
        params: dict[str, str] = {"api_key": self._api_key}
        if status_only:
            params["status"] = "True"
        if years is not None:
            params["years"] = years
        encoded_table = urllib.parse.quote(table.value, safe="")
        return f"{self._base_url}/{encoded_table}?{urllib.parse.urlencode(params)}"

    def _open_response(self, url: str, *, timeout: float) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            return self._opener(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            reason = str(getattr(exc, "reason", "") or exc)
            code = int(exc.code)
            with contextlib.suppress(Exception):
                exc.close()
            raise classify_sharadar_http_error(code, reason, self._api_key) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            safe_msg = redact_secret(str(exc), self._api_key)
            raise TransientProviderError(f"Sharadar transport error: {safe_msg}") from None

    def fetch_status(
        self,
        table: SharadarTable | str,
        years: str | int | None = None,
        *,
        timeout: float = 30.0,
        retry_policy: RetryPolicy | None = None,
    ) -> SharadarTableStatusResult:
        """Probe Sharadar Direct table status (``status=True``)."""
        resolved_table = normalize_table(table)
        resolved_years = normalize_years(years)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")

        url = self._build_url(resolved_table, years=resolved_years, status_only=True)
        policy = retry_policy or self._retry_policy

        def _probe() -> tuple[int, str]:
            try:
                with self._open_response(url, timeout=float(timeout)) as resp:
                    raw = resp.read(_MAX_STATUS_BYTES + 1)
                    if len(raw) > _MAX_STATUS_BYTES:
                        raise PermanentProviderError(
                            f"Sharadar status response exceeded {_MAX_STATUS_BYTES} bytes"
                        )
                    status_code = getattr(resp, "status", None)
                    if status_code is None and hasattr(resp, "getcode"):
                        status_code = resp.getcode()
                    code_int = int(status_code) if status_code is not None else 200
                    body = raw.decode("utf-8", errors="replace").strip() if raw else ""
                    safe_body = redact_secret(body, self._api_key)
                    if code_int >= 400:
                        raise classify_sharadar_http_error(code_int, safe_body, self._api_key)
                    return code_int, safe_body
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                safe_msg = redact_secret(str(exc), self._api_key)
                raise TransientProviderError(f"Sharadar transport error: {safe_msg}") from None

        retry_result = execute_with_retry(
            _probe,
            policy=policy,
            sleep=self._sleep_fn,
            random_value=self._random_value_fn,
        )
        status_code, message = retry_result.value

        spec = RequestSpec(
            provider="sharadar",
            endpoint="status",
            parameters={
                "table": resolved_table.value,
                "years": resolved_years,
                "status": True,
            },
        )
        provenance = FetchProvenance.from_request(
            spec,
            retrieved_at=self._clock(),
            attempts=retry_result.attempts,
            row_count=1 if message else 0,
            columns=("status",) if message else (),
            warnings=(),
            snapshot_identities=(),
            empty_disposition=(
                EmptyDisposition.NOT_EMPTY if message else EmptyDisposition.ALLOWED_EMPTY
            ),
        )
        return SharadarTableStatusResult(
            table=resolved_table,
            years=resolved_years,
            available=status_code == 200,
            status_code=status_code,
            message=message,
            provenance=provenance,
        )

    def download_table(self, request: SharadarBulkTableRequest) -> SharadarBulkDownloadResult:
        """Download and validate a Sharadar Direct bulk CSV or ZIP table atomically."""
        table = normalize_table(request.table)
        years = normalize_years(request.years)
        output_dir = Path(request.output_dir)
        csv_path = output_dir / f"{table.value}.csv"
        zip_path = output_dir / f"{table.value}.csv.zip"
        if not request.overwrite and (
            csv_path.exists() or (request.keep_zip and zip_path.exists())
        ):
            raise FileExistsError(
                f"Refusing to overwrite existing Sharadar file in {output_dir} without overwrite=True"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        url = self._build_url(table, years=years, status_only=False)
        policy = request.retry_policy or self._retry_policy

        retry_result = execute_with_retry(
            lambda: self._download_once(
                url=url,
                table=table,
                output_dir=output_dir,
                keep_zip=request.keep_zip,
                timeout=request.timeout,
                max_extracted_bytes=request.max_extracted_bytes,
            ),
            policy=policy,
            sleep=self._sleep_fn,
            random_value=self._random_value_fn,
        )
        payload = retry_result.value

        snapshot_ids: list[str] = [f"csv:sha256:{payload.csv_sha256}"]
        if payload.zip_sha256 is not None:
            snapshot_ids.append(f"zip:sha256:{payload.zip_sha256}")

        spec = RequestSpec(
            provider="sharadar",
            endpoint=f"bulk.{table.value}",
            parameters={
                "table": table.value,
                "years": years,
                "keep_zip": request.keep_zip,
            },
        )
        provenance = FetchProvenance.from_request(
            spec,
            retrieved_at=self._clock(),
            attempts=retry_result.attempts,
            row_count=payload.row_count,
            columns=payload.columns,
            warnings=(),
            snapshot_identities=tuple(snapshot_ids),
            empty_disposition=(
                EmptyDisposition.NOT_EMPTY
                if payload.row_count > 0
                else EmptyDisposition.ALLOWED_EMPTY
            ),
        )
        return SharadarBulkDownloadResult(
            table=table,
            years=years,
            csv_path=payload.csv_path,
            csv_bytes=payload.csv_bytes,
            csv_sha256=payload.csv_sha256,
            zip_path=payload.zip_path,
            zip_bytes=payload.zip_bytes,
            zip_sha256=payload.zip_sha256,
            was_zip=payload.was_zip,
            provenance=provenance,
        )

    def _download_once(
        self,
        *,
        url: str,
        table: SharadarTable,
        output_dir: Path,
        keep_zip: bool,
        timeout: float,
        max_extracted_bytes: int,
    ) -> DownloadedPayload:
        nonce = uuid.uuid4().hex[:8]
        raw_tmp = output_dir / f".{table.value}.{nonce}.download.tmp"
        csv_tmp = output_dir / f".{table.value}.{nonce}.csv.tmp"
        zip_path = output_dir / f"{table.value}.csv.zip"
        csv_path = output_dir / f"{table.value}.csv"

        raw_hash = hashlib.sha256()
        raw_bytes = 0
        expected_len: int | None = None
        is_zip_hint = False
        raw_csv_counter = CsvLineCounter()

        try:
            with self._open_response(url, timeout=timeout) as resp, raw_tmp.open("wb") as fh:
                headers = getattr(resp, "headers", None)
                if headers is not None:
                    cl = headers.get("Content-Length")
                    if cl is not None and str(cl).strip().isdigit():
                        expected_len = int(str(cl).strip())
                    ctype = str(headers.get("Content-Type") or "").lower()
                    resp_url = str(getattr(resp, "url", "") or "").lower()
                    is_zip_hint = "zip" in ctype or ".zip" in resp_url

                while chunk := resp.read(CHUNK_SIZE):
                    fh.write(chunk)
                    raw_hash.update(chunk)
                    raw_bytes += len(chunk)
                    raw_csv_counter.update(chunk)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            raw_tmp.unlink(missing_ok=True)
            safe_msg = redact_secret(str(exc), self._api_key)
            raise TransientProviderError(f"Sharadar transport error: {safe_msg}") from None
        except Exception:
            raw_tmp.unlink(missing_ok=True)
            raise

        if expected_len is not None and raw_bytes != expected_len:
            raw_tmp.unlink(missing_ok=True)
            raise SharadarTruncatedDownloadError(
                f"Sharadar {table.value} truncated download: "
                f"got {raw_bytes} bytes, expected {expected_len}"
            )

        with raw_tmp.open("rb") as fh:
            head = fh.read(4)

        is_zip = zipfile.is_zipfile(raw_tmp)
        if (head.startswith(b"PK\x03\x04") or is_zip_hint) and not is_zip:
            raw_tmp.unlink(missing_ok=True)
            raise SharadarTruncatedDownloadError(
                f"Sharadar {table.value} downloaded ZIP is corrupt or truncated"
            )

        if is_zip:
            return extract_zip_payload(
                table=table,
                raw_tmp=raw_tmp,
                csv_tmp=csv_tmp,
                csv_path=csv_path,
                zip_path=zip_path,
                keep_zip=keep_zip,
                raw_bytes=raw_bytes,
                raw_sha256=raw_hash.hexdigest(),
                api_key=self._api_key,
                max_extracted_bytes=max_extracted_bytes,
            )

        columns, row_count = raw_csv_counter.summary()
        os.replace(raw_tmp, csv_path)
        return DownloadedPayload(
            csv_path=csv_path,
            csv_bytes=raw_bytes,
            csv_sha256=raw_hash.hexdigest(),
            zip_path=None,
            zip_bytes=0,
            zip_sha256=None,
            was_zip=False,
            columns=columns,
            row_count=row_count,
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "SharadarClient",
]
