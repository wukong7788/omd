"""Free, PIT-safe daily FX rate table fetcher and writer backed by Frankfurter (ECB)."""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import math
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ._fx_translation_models import (
    SecFxRateObservation,
    SecFxRateTable,
    _validate_currency,
    _validate_positive_decimal,
)
from .errors import PermanentProviderError, TransientProviderError

FRANKFURTER_API_BASE_URL = "https://api.frankfurter.dev/v1"
FRANKFURTER_FX_SOURCE = "frankfurter_ecb"
DEFAULT_FX_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_FX_TIMEOUT_SECONDS = 30.0
DEFAULT_FX_MAX_ATTEMPTS = 3

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_REDIRECT_HTTP_STATUSES = frozenset({301, 302, 303, 307, 308})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _reject_non_finite(token: str) -> Any:
    raise ValueError(f"invalid non-finite JSON constant: {token!r}")


def _validate_calendar_date(value: date, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.date (not datetime)")
    return value


def _parse_iso_date_str(raw: Any, field_name: str) -> date:
    if type(raw) is not str or _ISO_DATE_RE.fullmatch(raw) is None:
        raise ValueError(f"{field_name} must be an ISO YYYY-MM-DD string, got {raw!r}")
    return date.fromisoformat(raw)


def _parse_positive_decimal(raw: Any, field_name: str) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"{field_name} must be a positive finite number")
    if isinstance(raw, Decimal):
        val = raw
    elif isinstance(raw, int):
        val = Decimal(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            raise ValueError(f"{field_name} must be finite")
        val = Decimal(str(raw))
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(f"{field_name} cannot be empty")
        try:
            val = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"invalid decimal in {field_name}: {raw!r}") from exc
    else:
        raise ValueError(f"unsupported numeric type in {field_name}: {type(raw).__name__}")  # noqa: TRY004
    return _validate_positive_decimal(val, field_name)


def validate_frankfurter_url(url: str) -> str:
    """Validate that ``url`` targets the official Frankfurter v1 HTTPS API."""
    if type(url) is not str or not url.strip() or any(ord(c) < 32 or ord(c) > 126 for c in url):
        raise ValueError("Frankfurter URL must be a printable ASCII non-empty string")
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "api.frankfurter.dev"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.path.startswith("/v1/")
        or ".." in parsed.path.split("/")
    ):
        raise ValueError("URL is outside the Frankfurter HTTPS v1 allowlist")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"https://{parsed.hostname}{parsed.path}{query}"


def parse_frankfurter_timeseries_payload(
    payload: bytes | str | Mapping[str, Any],
    *,
    source: str = FRANKFURTER_FX_SOURCE,
) -> SecFxRateTable:
    """Parse a Frankfurter ``base=USD`` timeseries payload into ``SecFxRateTable``.

    Frankfurter quotes foreign units per ``1 USD`` (``units_per_usd``). This parser
    inverts every observation to OMD's canonical ``usd_per_unit = 1 / units_per_usd``
    using 34-digit ``Decimal`` precision.
    """
    if type(source) is not str or not source.strip():
        raise ValueError("source must be a non-empty string")
    clean_source = source.strip()

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Frankfurter payload is not valid UTF-8") from exc
        raw_obj = _decode_json_object(text)
    elif isinstance(payload, str):
        raw_obj = _decode_json_object(payload)
    elif isinstance(payload, Mapping):
        raw_obj = payload
    else:
        raise TypeError("payload must be bytes, str, or Mapping")

    if raw_obj.get("base") != "USD":
        raise ValueError(f"Frankfurter payload base must be 'USD', got {raw_obj.get('base')!r}")
    amount = _parse_positive_decimal(raw_obj.get("amount"), "amount")
    if amount != Decimal(1):
        raise ValueError(f"Frankfurter payload amount must equal 1, got {amount}")

    rates_obj = raw_obj.get("rates")
    if not isinstance(rates_obj, Mapping):
        raise TypeError("Frankfurter payload 'rates' must be a mapping")

    observations: list[SecFxRateObservation] = []
    with localcontext() as ctx:
        ctx.prec = 34
        for raw_date, ccy_map in rates_obj.items():
            rate_dt = _parse_iso_date_str(raw_date, "rate_date")
            if not isinstance(ccy_map, Mapping):
                raise TypeError(f"rates[{raw_date!r}] must be a currency mapping")
            for raw_ccy, raw_val in ccy_map.items():
                ccy = _validate_currency(raw_ccy if type(raw_ccy) is str else "", "currency")
                units_per_usd = _parse_positive_decimal(raw_val, f"rates[{raw_date}][{ccy}]")
                if ccy == "USD":
                    if units_per_usd != Decimal(1):
                        raise ValueError("USD rate in USD-base payload must equal 1")
                    continue
                usd_per_unit = Decimal(1) / units_per_usd
                observations.append(
                    SecFxRateObservation(
                        currency=ccy,
                        rate_date=rate_dt,
                        usd_per_unit=usd_per_unit,
                        source=clean_source,
                        available_at=None,
                    )
                )
    return SecFxRateTable.from_observations(observations)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key in Frankfurter payload: {key!r}")
        out[key] = value
    return out


def _decode_json_object(text: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(
            text, parse_constant=_reject_non_finite, object_pairs_hook=_no_duplicate_keys
        )
    except json.JSONDecodeError as exc:
        raise ValueError("Frankfurter payload is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise TypeError("Frankfurter payload root must be a JSON object")
    return parsed


def _normalize_requested_currencies(currencies: Sequence[str] | None) -> tuple[str, ...] | None:
    if currencies is None:
        return None
    if isinstance(currencies, str):
        raise TypeError("currencies must be a sequence of ISO currency strings, not str")
    seen: set[str] = set()
    for item in currencies:
        if type(item) is not str:
            raise ValueError("each currency must be a 3-letter uppercase ISO 4217 string")
        norm = _validate_currency(item.strip().upper(), "currency")
        if norm == "USD":
            raise ValueError("currencies cannot include 'USD' (base currency)")
        seen.add(norm)
    if not seen:
        raise ValueError("currencies cannot be empty when specified")
    return tuple(sorted(seen))


def _read_bounded_json_body(response: Any, max_response_bytes: int) -> bytes:
    body = response.read(max_response_bytes + 1)
    if not isinstance(body, (bytes, bytearray)):
        raise PermanentProviderError("Frankfurter response body must be bytes")
    if len(body) > max_response_bytes:
        raise PermanentProviderError("Frankfurter FX response exceeded max_response_bytes")
    return bytes(body)


def _retry_delay_for_http_error(exc: HTTPError, attempt: int) -> float:
    delay = min(30.0, float(2 ** (attempt - 1)))
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after is not None and retry_after.isdigit():
        parsed = int(retry_after)
        if parsed <= 30:
            return float(parsed)
    return delay


def _next_redirect_url(current: str, headers: Any, redirects: int) -> str:
    if redirects > 3:
        raise PermanentProviderError("Frankfurter redirect limit exceeded")
    location = headers.get("Location") if headers else None
    if not location:
        raise PermanentProviderError("Frankfurter redirect missing location")
    try:
        return validate_frankfurter_url(urljoin(current, location))
    except ValueError as err:
        raise PermanentProviderError("Frankfurter redirect outside allowlist") from err


def _fetch_frankfurter_bytes(
    *,
    url: str,
    opener: Any,
    timeout: float,
    max_attempts: int,
    max_response_bytes: int,
    sleep: Callable[[float], None],
    user_agent: str,
) -> bytes:
    current = validate_frankfurter_url(url)
    active_opener = opener if opener is not None else build_opener(_NoRedirect())
    redirects = 0
    attempt = 1
    last: BaseException | None = None

    while attempt <= max_attempts:
        response: Any = None
        try:
            req = Request(
                current,
                headers={"User-Agent": user_agent, "Accept": "application/json"},
                method="GET",
            )
            response = active_opener.open(req, timeout=timeout)
            status = int(
                getattr(
                    response,
                    "status",
                    response.getcode() if hasattr(response, "getcode") else 200,
                )
            )
            if status in _REDIRECT_HTTP_STATUSES:
                redirects += 1
                current = _next_redirect_url(current, response.headers, redirects)
                continue
            if status != 200:
                raise PermanentProviderError(f"Frankfurter HTTP status {status}")
            ctype = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise PermanentProviderError(
                    f"Frankfurter response content type rejected: {ctype!r}"
                )
            content_length = response.headers.get("Content-Length") if response.headers else None
            if content_length is not None and (
                not content_length.isdigit() or int(content_length) > max_response_bytes
            ):
                raise PermanentProviderError("Frankfurter FX response exceeded max_response_bytes")
            return _read_bounded_json_body(response, max_response_bytes)
        except HTTPError as exc:
            exc.close()
            last = exc
            if exc.code in _REDIRECT_HTTP_STATUSES:
                redirects += 1
                current = _next_redirect_url(current, exc.headers, redirects)
                continue
            if exc.code not in _TRANSIENT_HTTP_STATUSES:
                raise PermanentProviderError(f"Frankfurter HTTP status {exc.code}") from None
            delay = _retry_delay_for_http_error(exc, attempt)
        except (TimeoutError, ConnectionError, URLError, OSError) as exc:
            last = exc
            delay = min(30.0, float(2 ** (attempt - 1)))
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()

        if attempt >= max_attempts:
            break
        attempt += 1
        sleep(delay)

    raise TransientProviderError("Frankfurter FX request retry exhausted") from last


def fetch_frankfurter_fx_rates(
    *,
    start_date: date,
    end_date: date | None = None,
    currencies: Sequence[str] | None = None,
    source: str = FRANKFURTER_FX_SOURCE,
    opener: Any = None,
    timeout: float = DEFAULT_FX_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_FX_MAX_ATTEMPTS,
    max_response_bytes: int = DEFAULT_FX_MAX_RESPONSE_BYTES,
    sleep: Callable[[float], None] = time.sleep,
    user_agent: str = "ohmydata-fx/1.0",
) -> SecFxRateTable:
    """Fetch PIT-safe daily FX rates (``usd_per_unit``) from the Frankfurter (ECB) API."""
    start = _validate_calendar_date(start_date, "start_date")
    end = _validate_calendar_date(end_date, "end_date") if end_date is not None else None
    if end is not None and end < start:
        raise ValueError("end_date must be >= start_date")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be an integer in 1..5")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    if type(max_response_bytes) is not int or max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be a positive integer")
    if (
        type(user_agent) is not str
        or not 1 <= len(user_agent) <= 256
        or any(ord(c) < 32 or ord(c) > 126 for c in user_agent)
    ):
        raise ValueError("user_agent must be 1..256 printable ASCII characters")
    if type(source) is not str or not source.strip():
        raise ValueError("source must be a non-empty string")

    requested = _normalize_requested_currencies(currencies)
    range_part = (
        f"{start.isoformat()}..{end.isoformat()}" if end is not None else f"{start.isoformat()}.."
    )
    url = f"{FRANKFURTER_API_BASE_URL}/{range_part}?base=USD"
    if requested is not None:
        url = f"{url}&symbols={','.join(requested)}"

    body = _fetch_frankfurter_bytes(
        url=url,
        opener=opener,
        timeout=float(timeout),
        max_attempts=max_attempts,
        max_response_bytes=max_response_bytes,
        sleep=sleep,
        user_agent=user_agent,
    )
    try:
        table = parse_frankfurter_timeseries_payload(body, source=source)
    except (ValueError, TypeError, KeyError) as exc:
        raise PermanentProviderError(f"invalid Frankfurter FX payload: {exc}") from exc

    if table.observation_count == 0:
        raise PermanentProviderError("Frankfurter FX response returned zero observations")
    if requested is not None:
        missing = sorted(set(requested) - set(table.currencies))
        if missing:
            raise PermanentProviderError(
                f"Frankfurter FX response missing requested currencies: {missing}"
            )
    return table


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_fx_rate_table(path: str | Path, fx_table: SecFxRateTable) -> dict[str, Any]:
    """Atomically write ``SecFxRateTable`` to ``.parquet`` or ``.csv`` for ``load_fx_rate_table``."""
    if not isinstance(fx_table, SecFxRateTable):
        raise TypeError("fx_table must be a SecFxRateTable")
    if fx_table.observation_count == 0:
        raise ValueError("fx_table cannot be empty")

    dest = Path(path)
    suffix = dest.suffix.lower()
    if suffix not in {".parquet", ".csv"}:
        raise ValueError(f"unsupported FX rate table format: {dest.suffix!r}")

    observations = fx_table.iter_observations()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dest.parent,
            prefix=".fx-rates-",
            suffix=suffix,
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)

        if suffix == ".csv":
            has_avail = any(obs.available_at is not None for obs in observations)
            cols = ["currency", "rate_date", "usd_per_unit", "source"] + (
                ["available_at"] if has_avail else []
            )
            with tmp_path.open("w", encoding="utf-8", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=cols)
                writer.writeheader()
                for obs in observations:
                    row = {
                        "currency": obs.currency,
                        "rate_date": obs.rate_date.isoformat(),
                        "usd_per_unit": str(obs.usd_per_unit),
                        "source": obs.source,
                    }
                    if has_avail:
                        row["available_at"] = (
                            obs.available_at.isoformat() if obs.available_at else ""
                        )
                    writer.writerow(row)
        else:
            try:
                pa: Any = importlib.import_module("pyarrow")
                pq: Any = importlib.import_module("pyarrow.parquet")
            except ImportError as exc:
                raise RuntimeError(
                    "install an optional pyarrow extra (e.g. 'sec-cli') to write Parquet FX tables"
                ) from exc
            schema = pa.schema(
                [
                    pa.field("currency", pa.string(), nullable=False),
                    pa.field("rate_date", pa.date32(), nullable=False),
                    pa.field("usd_per_unit", pa.string(), nullable=False),
                    pa.field("source", pa.string(), nullable=False),
                    pa.field("available_at", pa.timestamp("us", tz="UTC"), nullable=True),
                ]
            )
            records = [
                {
                    "currency": o.currency,
                    "rate_date": o.rate_date,
                    "usd_per_unit": str(o.usd_per_unit),
                    "source": o.source,
                    "available_at": o.available_at,
                }
                for o in observations
            ]
            pq.write_table(
                pa.Table.from_pylist(records, schema=schema), tmp_path, compression="zstd"
            )

        tmp_path.replace(dest)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    dates = [obs.rate_date for obs in observations]
    return {
        "path": str(dest),
        "format": suffix.lstrip("."),
        "observation_count": len(observations),
        "currencies": list(fx_table.currencies),
        "min_date": min(dates).isoformat(),
        "max_date": max(dates).isoformat(),
        "sha256": _sha256_file(dest),
        "bytes": dest.stat().st_size,
    }


__all__ = [
    "DEFAULT_FX_MAX_ATTEMPTS",
    "DEFAULT_FX_MAX_RESPONSE_BYTES",
    "DEFAULT_FX_TIMEOUT_SECONDS",
    "FRANKFURTER_API_BASE_URL",
    "FRANKFURTER_FX_SOURCE",
    "fetch_frankfurter_fx_rates",
    "parse_frankfurter_timeseries_payload",
    "validate_frankfurter_url",
    "write_fx_rate_table",
]
