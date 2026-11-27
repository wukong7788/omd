"""Atomic CSV/Parquet writer for SEC FX rate tables."""

from __future__ import annotations

import csv
import hashlib
import importlib
import tempfile
from pathlib import Path
from typing import Any

from ._fx_translation_models import SecFxRateTable


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


__all__ = ["write_fx_rate_table"]
