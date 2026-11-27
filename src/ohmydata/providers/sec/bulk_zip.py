"""Offline SEC Bulk ZIP (companyfacts.zip + submissions.zip) PIT projection API."""

from __future__ import annotations

import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._bulk_zip_dataset import (
    BULK_COMPANYFACTS_DATASET_SCHEMA,
    BULK_COMPANYFACTS_WRITER_PROFILE,
    validate_bulk_companyfacts_dataset,
    write_bulk_companyfacts_dataset,
)
from ._bulk_zip_io import (
    DEFAULT_MAX_HISTORY_PAGES_PER_CIK,
    DEFAULT_MAX_ZIP_ENTRY_BYTES,
    DEFAULT_MAX_ZIP_MEMBERS,
    DEFAULT_MAX_ZIP_TOTAL_BYTES,
    BoundedZipReader,
    BulkZipBombError,
    BulkZipSecurityError,
    SecBulkArchiveProvenance,
    index_cik_members,
    sha256_file_streaming,
    validate_zip_archive_safety,
)
from ._bulk_zip_models import (
    SecBulkCikOutcome,
    SecBulkCikOutcomeStatus,
    SecBulkFundamentalRow,
    SecBulkJoinedFact,
    SecBulkProjectionResult,
)
from ._bulk_zip_parallel import (
    process_cik_from_zips,
    project_ciks_parallel,
    validate_bulk_workers,
)
from ._companyfacts_models import normalize_sec_cik
from .errors import SchemaMismatchError
from .fx_translation import (
    DEFAULT_FX_MAX_STALENESS_DAYS,
    SecFxDatePolicy,
    SecFxFlowMethod,
    SecFxRateTable,
    build_usd_translated_from_projection,
    validate_translation_params,
)

DEFAULT_BULK_FORMS: tuple[str, ...] = ("10-K", "10-Q", "20-F", "40-F")
_SUPPORTED_BASE_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F"})


def _resolve_allowed_forms(forms: Iterable[str], include_amendments: bool) -> frozenset[str]:
    if type(include_amendments) is not bool:
        raise TypeError("include_amendments must be a bool")
    resolved: set[str] = set()
    for raw in forms:
        if type(raw) is not str:
            raise TypeError("form entries must be strings")
        form = raw.strip().upper()
        base = form.removesuffix("/A")
        if base not in _SUPPORTED_BASE_FORMS:
            raise ValueError(f"unsupported SEC periodic form: {raw!r}")
        if form.endswith("/A"):
            if include_amendments:
                resolved.add(form)
        else:
            resolved.add(base)
            if include_amendments:
                resolved.add(f"{base}/A")
    if not resolved:
        raise ValueError("at least one SEC periodic form must be selected")
    return frozenset(resolved)


def _validate_filters(
    ciks: Iterable[str | int] | None,
    start_year: int | None,
    end_year: int | None,
    acceptance_upper: datetime | None,
) -> tuple[tuple[str, ...] | None, datetime | None]:
    if start_year is not None and (type(start_year) is not int or not 1 <= start_year <= 9999):
        raise ValueError("start_year must be an integer in 1..9999")
    if end_year is not None and (type(end_year) is not int or not 1 <= end_year <= 9999):
        raise ValueError("end_year must be an integer in 1..9999")
    if start_year is not None and end_year is not None and start_year > end_year:
        raise ValueError("start_year must be <= end_year")
    if acceptance_upper is not None and (
        not isinstance(acceptance_upper, datetime)
        or acceptance_upper.tzinfo is None
        or acceptance_upper.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    bound = acceptance_upper.astimezone(UTC) if acceptance_upper is not None else None
    if ciks is None:
        return None, bound
    normalized = tuple(sorted({normalize_sec_cik(cik) for cik in ciks}))
    return normalized, bound


def project_bulk_companyfacts_zips(
    companyfacts_zip: str | Path,
    submissions_zip: str | Path,
    *,
    ciks: Iterable[str | int] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    forms: Iterable[str] = DEFAULT_BULK_FORMS,
    include_amendments: bool = True,
    acceptance_upper: datetime | None = None,
    max_entry_bytes: int = DEFAULT_MAX_ZIP_ENTRY_BYTES,
    max_total_bytes: int = DEFAULT_MAX_ZIP_TOTAL_BYTES,
    max_history_pages_per_cik: int = DEFAULT_MAX_HISTORY_PAGES_PER_CIK,
    max_zip_members: int = DEFAULT_MAX_ZIP_MEMBERS,
    workers: int = 1,
) -> SecBulkProjectionResult:
    """Parse SEC bulk companyfacts.zip + submissions.zip offline into PIT-joined records."""
    cf_path = Path(companyfacts_zip)
    sub_path = Path(submissions_zip)
    if not cf_path.is_file():
        raise FileNotFoundError(f"companyfacts zip not found: {cf_path.name}")
    if not sub_path.is_file():
        raise FileNotFoundError(f"submissions zip not found: {sub_path.name}")
    normalized_ciks, bound = _validate_filters(ciks, start_year, end_year, acceptance_upper)
    allowed_forms = _resolve_allowed_forms(forms, include_amendments)
    if type(max_history_pages_per_cik) is not int or max_history_pages_per_cik < 1:
        raise ValueError("max_history_pages_per_cik must be a positive integer")
    worker_count = validate_bulk_workers(workers)
    reader = BoundedZipReader(
        max_entry_bytes=max_entry_bytes,
        max_total_bytes=max_total_bytes,
    )

    outcomes: list[SecBulkCikOutcome] = []
    all_fundamentals: list[SecBulkFundamentalRow] = []
    all_facts: list[SecBulkJoinedFact] = []
    work_items: list[tuple[str, str, int]] = []

    with ThreadPoolExecutor(max_workers=2) as hash_pool:
        cf_hash_future = hash_pool.submit(sha256_file_streaming, cf_path)
        sub_hash_future = hash_pool.submit(sha256_file_streaming, sub_path)
        try:
            with (
                zipfile.ZipFile(cf_path, "r") as cf_zf,
                zipfile.ZipFile(sub_path, "r") as sub_zf,
            ):
                cf_index = validate_zip_archive_safety(cf_zf, max_members=max_zip_members)
                sub_index = validate_zip_archive_safety(sub_zf, max_members=max_zip_members)
                cf_by_cik = index_cik_members(cf_index)
                sub_by_cik = index_cik_members(sub_index)

                target_ciks = (
                    normalized_ciks
                    if normalized_ciks is not None
                    else tuple(sorted(cf_by_cik.keys()))
                )
                for cik in target_ciks:
                    cf_member = cf_by_cik.get(cik)
                    if cf_member is None:
                        outcomes.append(
                            SecBulkCikOutcome(
                                cik=cik,
                                status=SecBulkCikOutcomeStatus.EMPTY,
                                error_message="CIK not present in companyfacts.zip",
                            )
                        )
                        continue
                    if cik not in sub_by_cik:
                        outcomes.append(
                            SecBulkCikOutcome(
                                cik=cik,
                                status=SecBulkCikOutcomeStatus.MISSING_SUBMISSIONS,
                                error_message="CIK not present in submissions.zip",
                            )
                        )
                        continue
                    work_items.append((cik, cf_member, cf_index[cf_member].file_size))

                if worker_count == 1 or len(work_items) <= 1:
                    for cik, cf_member, _ in work_items:
                        outcome, joined_facts, fundamental_rows = process_cik_from_zips(
                            cik,
                            cf_zf=cf_zf,
                            cf_index=cf_index,
                            cf_member=cf_member,
                            sub_zf=sub_zf,
                            sub_index=sub_index,
                            reader=reader,
                            allowed_forms=allowed_forms,
                            start_year=start_year,
                            end_year=end_year,
                            acceptance_upper=bound,
                            max_history_pages_per_cik=max_history_pages_per_cik,
                        )
                        outcomes.append(outcome)
                        if joined_facts:
                            all_facts.extend(joined_facts)
                        if fundamental_rows:
                            all_fundamentals.extend(fundamental_rows)
                    work_items.clear()
        except zipfile.BadZipFile as exc:
            raise SchemaMismatchError("invalid SEC bulk ZIP archive") from exc
        cf_prov = cf_hash_future.result()
        sub_prov = sub_hash_future.result()

    if work_items:
        p_outcomes, p_fundamentals, p_facts = project_ciks_parallel(
            cf_path,
            sub_path,
            work_items,
            workers=worker_count,
            allowed_forms=allowed_forms,
            start_year=start_year,
            end_year=end_year,
            acceptance_upper=bound,
            max_entry_bytes=max_entry_bytes,
            max_total_bytes=max_total_bytes,
            max_history_pages_per_cik=max_history_pages_per_cik,
        )
        outcomes.extend(p_outcomes)
        all_fundamentals.extend(p_fundamentals)
        all_facts.extend(p_facts)

    outcomes.sort(key=lambda o: o.cik)
    all_fundamentals.sort(
        key=lambda r: (
            r.cik,
            r.accepted_at,
            r.accession_number,
            r.fiscal_year,
            r.fiscal_period,
            r.period_kind,
        )
    )
    all_facts.sort(
        key=lambda f: (
            f.cik,
            f.accepted_at,
            f.accession_number,
            f.namespace,
            f.tag,
            f.unit,
            f.period_end,
            f.period_start or f.period_end,
        )
    )
    filters = {
        "ciks": list(normalized_ciks) if normalized_ciks is not None else None,
        "start_year": start_year,
        "end_year": end_year,
        "forms": sorted(allowed_forms),
        "include_amendments": include_amendments,
        "acceptance_upper": bound.isoformat() if bound is not None else None,
    }
    return SecBulkProjectionResult(
        companyfacts_archive=cf_prov,
        submissions_archive=sub_prov,
        outcomes=tuple(outcomes),
        fundamental_rows=tuple(all_fundamentals),
        joined_facts=tuple(all_facts),
        filters=filters,
    )


def build_bulk_companyfacts_dataset(
    companyfacts_zip: str | Path,
    submissions_zip: str | Path,
    output_dir: str | Path,
    *,
    ciks: Iterable[str | int] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    forms: Iterable[str] = DEFAULT_BULK_FORMS,
    include_amendments: bool = True,
    acceptance_upper: datetime | None = None,
    max_entry_bytes: int = DEFAULT_MAX_ZIP_ENTRY_BYTES,
    max_total_bytes: int = DEFAULT_MAX_ZIP_TOTAL_BYTES,
    max_history_pages_per_cik: int = DEFAULT_MAX_HISTORY_PAGES_PER_CIK,
    workers: int = 1,
    usd_output_dir: str | Path | None = None,
    fx_table: SecFxRateTable | None = None,
    auto_fetch_fx: bool = True,
    fx_fetcher: Callable[..., SecFxRateTable] | None = None,
    use_filing_implied: bool = True,
    date_policy: SecFxDatePolicy = SecFxDatePolicy.PERIOD_END_SPOT,
    flow_method: SecFxFlowMethod = SecFxFlowMethod.PERIOD_AVERAGE,
    max_staleness_days: int = DEFAULT_FX_MAX_STALENESS_DAYS,
    allow_spot_fallback: bool = True,
) -> dict[str, Any]:
    """One-shot CLI/SDK helper: project bulk ZIPs, write immutable dataset, and validate."""
    if usd_output_dir is not None:
        if not str(usd_output_dir).strip():
            raise ValueError("usd_output_dir cannot be empty")
        if Path(usd_output_dir).expanduser().resolve() == Path(output_dir).expanduser().resolve():
            raise ValueError("usd_output_dir must differ from output_dir")
        validate_translation_params(
            fx_table=fx_table,
            use_filing_implied=use_filing_implied,
            date_policy=date_policy,
            flow_method=flow_method,
            max_staleness_days=max_staleness_days,
            allow_spot_fallback=allow_spot_fallback,
            auto_fetch_fx=auto_fetch_fx,
            fx_fetcher=fx_fetcher,
        )
    projection = project_bulk_companyfacts_zips(
        companyfacts_zip,
        submissions_zip,
        ciks=ciks,
        start_year=start_year,
        end_year=end_year,
        forms=forms,
        include_amendments=include_amendments,
        acceptance_upper=acceptance_upper,
        max_entry_bytes=max_entry_bytes,
        max_total_bytes=max_total_bytes,
        max_history_pages_per_cik=max_history_pages_per_cik,
        workers=workers,
    )
    write_bulk_companyfacts_dataset(output_dir, projection)
    manifest = validate_bulk_companyfacts_dataset(output_dir)
    if usd_output_dir is None:
        return manifest
    usd_manifest = build_usd_translated_from_projection(
        projection,
        usd_output_dir,
        source_manifest=manifest,
        fx_table=fx_table,
        use_filing_implied=use_filing_implied,
        date_policy=date_policy,
        flow_method=flow_method,
        max_staleness_days=max_staleness_days,
        allow_spot_fallback=allow_spot_fallback,
        auto_fetch_fx=auto_fetch_fx,
        fx_fetcher=fx_fetcher,
    )
    return {**manifest, "usd_dataset": usd_manifest}


__all__ = [
    "BULK_COMPANYFACTS_DATASET_SCHEMA",
    "BULK_COMPANYFACTS_WRITER_PROFILE",
    "DEFAULT_BULK_FORMS",
    "DEFAULT_MAX_HISTORY_PAGES_PER_CIK",
    "DEFAULT_MAX_ZIP_ENTRY_BYTES",
    "DEFAULT_MAX_ZIP_MEMBERS",
    "DEFAULT_MAX_ZIP_TOTAL_BYTES",
    "BulkZipBombError",
    "BulkZipSecurityError",
    "SecBulkArchiveProvenance",
    "SecBulkCikOutcome",
    "SecBulkCikOutcomeStatus",
    "SecBulkFundamentalRow",
    "SecBulkJoinedFact",
    "SecBulkProjectionResult",
    "build_bulk_companyfacts_dataset",
    "project_bulk_companyfacts_zips",
    "validate_bulk_companyfacts_dataset",
    "write_bulk_companyfacts_dataset",
]
