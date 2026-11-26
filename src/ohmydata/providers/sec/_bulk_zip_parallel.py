"""Deterministic multiprocessing helpers for SEC bulk ZIP projection."""

from __future__ import annotations

import atexit
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ._bulk_zip_extract import extract_cik_bulk_records
from ._bulk_zip_io import (
    BoundedZipReader,
    BulkZipBombError,
    BulkZipSecurityError,
    load_submissions_from_zip,
)
from ._bulk_zip_models import (
    SecBulkCikOutcome,
    SecBulkCikOutcomeStatus,
    SecBulkFundamentalRow,
    SecBulkJoinedFact,
)
from ._companyfacts_models import parse_sec_companyfacts_payload
from .errors import ResourceLimitError, SchemaMismatchError

MAX_BULK_WORKERS = 64
_CHUNKS_PER_WORKER = 8
_WORKER_CF_PATH: str | None = None
_WORKER_SUB_PATH: str | None = None
_WORKER_CF_ZF: zipfile.ZipFile | None = None
_WORKER_SUB_ZF: zipfile.ZipFile | None = None


def validate_bulk_workers(workers: int) -> int:
    """Validate explicit worker count for bulk ZIP projection."""
    if type(workers) is not int or not 1 <= workers <= MAX_BULK_WORKERS:
        raise ValueError(f"workers must be an integer in 1..{MAX_BULK_WORKERS}")
    return workers


def _close_worker_zips() -> None:
    global _WORKER_CF_PATH, _WORKER_SUB_PATH, _WORKER_CF_ZF, _WORKER_SUB_ZF
    if _WORKER_CF_ZF is not None:
        _WORKER_CF_ZF.close()
        _WORKER_CF_ZF = None
    if _WORKER_SUB_ZF is not None:
        _WORKER_SUB_ZF.close()
        _WORKER_SUB_ZF = None
    _WORKER_CF_PATH = None
    _WORKER_SUB_PATH = None


def _init_worker_zips(cf_path: str, sub_path: str) -> None:
    """Open read-only bulk ZIP handles once per worker process for dynamic chunk reuse."""
    global _WORKER_CF_PATH, _WORKER_SUB_PATH, _WORKER_CF_ZF, _WORKER_SUB_ZF
    _close_worker_zips()
    try:
        _WORKER_CF_ZF = zipfile.ZipFile(cf_path, "r")
        _WORKER_SUB_ZF = zipfile.ZipFile(sub_path, "r")
        _WORKER_CF_PATH = cf_path
        _WORKER_SUB_PATH = sub_path
    except zipfile.BadZipFile as exc:
        _close_worker_zips()
        raise SchemaMismatchError("invalid SEC bulk ZIP archive") from exc
    atexit.register(_close_worker_zips)


def process_cik_from_zips(
    cik: str,
    *,
    cf_zf: zipfile.ZipFile,
    cf_index: dict[str, zipfile.ZipInfo],
    cf_member: str,
    sub_zf: zipfile.ZipFile,
    sub_index: dict[str, zipfile.ZipInfo],
    reader: BoundedZipReader,
    allowed_forms: frozenset[str],
    start_year: int | None,
    end_year: int | None,
    acceptance_upper: datetime | None,
    max_history_pages_per_cik: int,
) -> tuple[SecBulkCikOutcome, list[SecBulkJoinedFact], list[SecBulkFundamentalRow]]:
    """Parse and project a single CIK from validated bulk ZIP archives."""
    try:
        cf_body = reader.read_entry(cf_zf, cf_index[cf_member])
        sub_meta = load_submissions_from_zip(
            sub_zf,
            sub_index,
            reader,
            cik,
            allowed_forms=allowed_forms,
            acceptance_upper=acceptance_upper,
            max_history_pages=max_history_pages_per_cik,
        )
        raw_facts = parse_sec_companyfacts_payload(cf_body, cik)
        joined_facts, fundamental_rows, taxonomy, currency = extract_cik_bulk_records(
            cik,
            raw_facts,
            sub_meta,
            start_year=start_year,
            end_year=end_year,
            acceptance_upper=acceptance_upper,
        )
    except (BulkZipSecurityError, BulkZipBombError):
        raise
    except (SchemaMismatchError, ResourceLimitError, ValueError) as exc:
        return (
            SecBulkCikOutcome(
                cik=cik,
                status=SecBulkCikOutcomeStatus.PARSE_FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
            ),
            [],
            [],
        )

    if not joined_facts and not fundamental_rows:
        return (
            SecBulkCikOutcome(
                cik=cik,
                status=SecBulkCikOutcomeStatus.EMPTY,
                entity_name=sub_meta.entity_name,
                symbols=sub_meta.symbols,
                taxonomy=taxonomy,
                reporting_currency=currency,
                filing_count=len(sub_meta.filings),
                history_pages_read=sub_meta.history_pages_read,
            ),
            [],
            [],
        )

    return (
        SecBulkCikOutcome(
            cik=cik,
            status=SecBulkCikOutcomeStatus.OK,
            entity_name=sub_meta.entity_name,
            symbols=sub_meta.symbols,
            taxonomy=taxonomy,
            reporting_currency=currency,
            filing_count=len(sub_meta.filings),
            history_pages_read=sub_meta.history_pages_read,
            fact_count=len(joined_facts),
            fundamental_row_count=len(fundamental_rows),
        ),
        joined_facts,
        fundamental_rows,
    )


def partition_ciks_balanced(
    items: list[tuple[str, str, int]],
    num_buckets: int,
) -> list[tuple[tuple[str, str], ...]]:
    """Deterministically pack (cik, member, byte_size) items into size-ordered work chunks."""
    if not items:
        return []
    bucket_count = min(num_buckets, len(items))
    ordered = sorted(items, key=lambda item: (-item[2], item[0]))
    buckets: list[list[tuple[str, str]]] = [[] for _ in range(bucket_count)]
    totals = [0] * bucket_count
    for cik, member, size in ordered:
        best = min(range(bucket_count), key=lambda idx: (totals[idx], idx))
        buckets[best].append((cik, member))
        totals[best] += size
    ranked = sorted(
        ((totals[idx], idx, tuple(bucket)) for idx, bucket in enumerate(buckets) if bucket),
        key=lambda entry: (-entry[0], entry[1]),
    )
    return [entry[2] for entry in ranked]


@dataclass(frozen=True)
class BulkWorkerChunkTask:
    cf_path: str
    sub_path: str
    cik_members: tuple[tuple[str, str], ...]
    allowed_forms: frozenset[str]
    start_year: int | None
    end_year: int | None
    acceptance_upper: datetime | None
    max_entry_bytes: int
    max_total_bytes: int
    max_history_pages_per_cik: int


@dataclass(frozen=True)
class BulkWorkerChunkResult:
    outcomes: list[SecBulkCikOutcome]
    fundamentals: list[SecBulkFundamentalRow]
    facts: list[SecBulkJoinedFact]
    bytes_read: int


def run_bulk_worker_chunk(task: BulkWorkerChunkTask) -> BulkWorkerChunkResult:
    """Top-level spawn-safe worker entry point processing one chunk of CIKs."""
    reader = BoundedZipReader(
        max_entry_bytes=task.max_entry_bytes,
        max_total_bytes=task.max_total_bytes,
    )
    outcomes: list[SecBulkCikOutcome] = []
    all_fundamentals: list[SecBulkFundamentalRow] = []
    all_facts: list[SecBulkJoinedFact] = []
    if (
        _WORKER_CF_ZF is not None
        and _WORKER_SUB_ZF is not None
        and _WORKER_CF_PATH == task.cf_path
        and _WORKER_SUB_PATH == task.sub_path
    ):
        cf_zf = _WORKER_CF_ZF
        sub_zf = _WORKER_SUB_ZF
        cf_index = cf_zf.NameToInfo
        sub_index = sub_zf.NameToInfo
        for cik, cf_member in task.cik_members:
            outcome, joined_facts, fundamental_rows = process_cik_from_zips(
                cik,
                cf_zf=cf_zf,
                cf_index=cf_index,
                cf_member=cf_member,
                sub_zf=sub_zf,
                sub_index=sub_index,
                reader=reader,
                allowed_forms=task.allowed_forms,
                start_year=task.start_year,
                end_year=task.end_year,
                acceptance_upper=task.acceptance_upper,
                max_history_pages_per_cik=task.max_history_pages_per_cik,
            )
            outcomes.append(outcome)
            if joined_facts:
                all_facts.extend(joined_facts)
            if fundamental_rows:
                all_fundamentals.extend(fundamental_rows)
    else:
        try:
            with (
                zipfile.ZipFile(task.cf_path, "r") as cf_zf,
                zipfile.ZipFile(task.sub_path, "r") as sub_zf,
            ):
                cf_index = cf_zf.NameToInfo
                sub_index = sub_zf.NameToInfo
                for cik, cf_member in task.cik_members:
                    outcome, joined_facts, fundamental_rows = process_cik_from_zips(
                        cik,
                        cf_zf=cf_zf,
                        cf_index=cf_index,
                        cf_member=cf_member,
                        sub_zf=sub_zf,
                        sub_index=sub_index,
                        reader=reader,
                        allowed_forms=task.allowed_forms,
                        start_year=task.start_year,
                        end_year=task.end_year,
                        acceptance_upper=task.acceptance_upper,
                        max_history_pages_per_cik=task.max_history_pages_per_cik,
                    )
                    outcomes.append(outcome)
                    if joined_facts:
                        all_facts.extend(joined_facts)
                    if fundamental_rows:
                        all_fundamentals.extend(fundamental_rows)
        except zipfile.BadZipFile as exc:
            raise SchemaMismatchError("invalid SEC bulk ZIP archive") from exc
    return BulkWorkerChunkResult(
        outcomes=outcomes,
        fundamentals=all_fundamentals,
        facts=all_facts,
        bytes_read=reader.total_bytes_read,
    )


def project_ciks_parallel(
    cf_path: Path,
    sub_path: Path,
    work_items: list[tuple[str, str, int]],
    *,
    workers: int,
    allowed_forms: frozenset[str],
    start_year: int | None,
    end_year: int | None,
    acceptance_upper: datetime | None,
    max_entry_bytes: int,
    max_total_bytes: int,
    max_history_pages_per_cik: int,
) -> tuple[list[SecBulkCikOutcome], list[SecBulkFundamentalRow], list[SecBulkJoinedFact]]:
    """Run dynamic work-stealing CIK projection across persistent worker processes."""
    worker_count = min(workers, len(work_items))
    target_chunks = min(len(work_items), worker_count * _CHUNKS_PER_WORKER)
    chunks = partition_ciks_balanced(work_items, target_chunks)
    tasks = [
        BulkWorkerChunkTask(
            cf_path=str(cf_path),
            sub_path=str(sub_path),
            cik_members=chunk,
            allowed_forms=allowed_forms,
            start_year=start_year,
            end_year=end_year,
            acceptance_upper=acceptance_upper,
            max_entry_bytes=max_entry_bytes,
            max_total_bytes=max_total_bytes,
            max_history_pages_per_cik=max_history_pages_per_cik,
        )
        for chunk in chunks
    ]
    outcomes: list[SecBulkCikOutcome] = []
    all_fundamentals: list[SecBulkFundamentalRow] = []
    all_facts: list[SecBulkJoinedFact] = []
    total_bytes_read = 0
    with ProcessPoolExecutor(
        max_workers=worker_count,
        initializer=_init_worker_zips,
        initargs=(str(cf_path), str(sub_path)),
    ) as pool:
        futures = [pool.submit(run_bulk_worker_chunk, task) for task in tasks]
        try:
            for future in as_completed(futures):
                chunk_result = future.result()
                total_bytes_read += chunk_result.bytes_read
                if total_bytes_read > max_total_bytes:
                    raise BulkZipBombError("ZIP archive cumulative decompressed bytes exceed limit")
                outcomes.extend(chunk_result.outcomes)
                all_fundamentals.extend(chunk_result.fundamentals)
                all_facts.extend(chunk_result.facts)
        except BaseException:
            for pending in futures:
                pending.cancel()
            raise
    return outcomes, all_fundamentals, all_facts


__all__ = [
    "MAX_BULK_WORKERS",
    "BulkWorkerChunkResult",
    "BulkWorkerChunkTask",
    "partition_ciks_balanced",
    "process_cik_from_zips",
    "project_ciks_parallel",
    "run_bulk_worker_chunk",
    "validate_bulk_workers",
]
