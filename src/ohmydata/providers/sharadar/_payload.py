"""Internal streaming payload extraction and CSV line-counting helpers."""

from __future__ import annotations

import csv
import hashlib
import os
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from .endpoints import SharadarTable
from .errors import SharadarError, redact_secret

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class DownloadedPayload:
    """Internal result of a single atomic download + extraction attempt."""

    csv_path: Path
    csv_bytes: int
    csv_sha256: str
    zip_path: Path | None
    zip_bytes: int
    zip_sha256: str | None
    was_zip: bool
    columns: tuple[str, ...]
    row_count: int


def _parse_csv_header(first_line_bytes: bytes) -> tuple[str, ...]:
    if not first_line_bytes:
        return ()
    text = first_line_bytes.decode("utf-8", errors="replace").strip("\r\n")
    if not text:
        return ()
    parsed = next(csv.reader([text]), [])
    cleaned = tuple(col.strip() for col in parsed)
    if not cleaned or any(not col for col in cleaned) or len(set(cleaned)) != len(cleaned):
        return ()
    return cleaned


class CsvLineCounter:
    """Incrementally count CSV header columns and data rows during streaming."""

    def __init__(self) -> None:
        self._first_line_buf = bytearray()
        self._header_done = False
        self._newlines = 0
        self._last_byte: int | None = None
        self._total_bytes = 0

    def update(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._total_bytes += len(chunk)
        self._last_byte = chunk[-1]
        self._newlines += chunk.count(b"\n")
        if not self._header_done:
            nl_idx = chunk.find(b"\n")
            if nl_idx == -1:
                if len(self._first_line_buf) < 65536:
                    self._first_line_buf.extend(chunk[: 65536 - len(self._first_line_buf)])
            else:
                remaining = max(0, 65536 - len(self._first_line_buf))
                self._first_line_buf.extend(chunk[: min(nl_idx, remaining)])
                self._header_done = True

    def summary(self) -> tuple[tuple[str, ...], int]:
        if self._total_bytes == 0:
            return (), 0
        columns = _parse_csv_header(bytes(self._first_line_buf))
        total_lines = self._newlines + (0 if self._last_byte == 10 else 1)
        data_rows = max(0, total_lines - 1) if columns else total_lines
        return columns, data_rows


def extract_zip_payload(
    *,
    table: SharadarTable,
    raw_tmp: Path,
    csv_tmp: Path,
    csv_path: Path,
    zip_path: Path,
    keep_zip: bool,
    raw_bytes: int,
    raw_sha256: str,
    api_key: str,
) -> DownloadedPayload:
    """Validate and extract the primary CSV from a downloaded ZIP archive atomically."""
    csv_hash = hashlib.sha256()
    csv_bytes = 0
    counter = CsvLineCounter()
    try:
        with zipfile.ZipFile(raw_tmp, "r") as zf:
            members = [m for m in zf.namelist() if not m.endswith("/")]
            csv_members = sorted([m for m in members if m.lower().endswith(".csv")] or members)
            if not csv_members:
                raise SharadarError(
                    f"Sharadar {table.value} ZIP archive contained no extractable file"
                )
            with zf.open(csv_members[0], "r") as src, csv_tmp.open("wb") as dst:
                while chunk := src.read(CHUNK_SIZE):
                    dst.write(chunk)
                    csv_hash.update(chunk)
                    csv_bytes += len(chunk)
                    counter.update(chunk)
        os.replace(csv_tmp, csv_path)
    except SharadarError:
        csv_tmp.unlink(missing_ok=True)
        raw_tmp.unlink(missing_ok=True)
        raise
    except (zipfile.BadZipFile, EOFError, zlib.error, RuntimeError) as exc:
        csv_tmp.unlink(missing_ok=True)
        raw_tmp.unlink(missing_ok=True)
        safe_msg = redact_secret(str(exc), api_key)
        raise SharadarError(
            f"Sharadar {table.value} downloaded ZIP is corrupt or truncated: {safe_msg}"
        ) from None
    except Exception:
        csv_tmp.unlink(missing_ok=True)
        raw_tmp.unlink(missing_ok=True)
        raise

    if keep_zip:
        os.replace(raw_tmp, zip_path)
    else:
        raw_tmp.unlink(missing_ok=True)

    columns, row_count = counter.summary()
    return DownloadedPayload(
        csv_path=csv_path,
        csv_bytes=csv_bytes,
        csv_sha256=csv_hash.hexdigest(),
        zip_path=zip_path if keep_zip else None,
        zip_bytes=raw_bytes,
        zip_sha256=raw_sha256,
        was_zip=True,
        columns=columns,
        row_count=row_count,
    )
