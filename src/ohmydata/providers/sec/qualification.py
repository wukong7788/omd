"""SEC N-PORT structural qualification pipeline and independent replay."""

from __future__ import annotations

import importlib
import json
import resource
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from ...core.errors import (
    AmbiguousPartitionError,
    CoverageError,
    ResourceLimitError,
    SchemaMismatchError,
    SnapshotIntegrityError,
)
from .batch import (
    Quarter,
    SecEquityEtfUniverse,
    SecNportBatch,
    canonical_hash,
)
from .core_dataset import validate_tables
from .qualification_dataset import (
    QUALIFICATION_RECEIPT_SCHEMA,
    _sha256_file,
    get_amendment_facts_schema,
    get_coverage_schema,
    get_identifier_quality_schema,
    get_vintage_quality_schema,
    logical_table_hash,
    publish_qualification_receipt_and_rename,
    schema_fingerprint,
    write_candidate_qualification_tables,
)


@dataclass(frozen=True)
class SecNportPartitionSetEntry:
    source_quarter: str
    partition_identity: str
    manifest_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_quarter": self.source_quarter,
            "partition_identity": self.partition_identity,
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True)
class SecNportPartitionSet:
    entries: tuple[SecNportPartitionSetEntry, ...]

    def __post_init__(self) -> None:
        seen_quarters: set[str] = set()
        for e in self.entries:
            if e.source_quarter in seen_quarters:
                raise SchemaMismatchError(f"duplicate quarter in partition set: {e.source_quarter}")
            seen_quarters.add(e.source_quarter)

    @classmethod
    def load(cls, path: str | Path) -> SecNportPartitionSet:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("schema_version") != "sec-nport-partition-set-v1":
            raise SchemaMismatchError("invalid partition set schema version")
        entries = tuple(
            SecNportPartitionSetEntry(
                source_quarter=str(e["source_quarter"]),
                partition_identity=str(e["partition_identity"]),
                manifest_hash=str(e["manifest_hash"]),
            )
            for e in data.get("entries", [])
        )
        return cls(entries=entries)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": "sec-nport-partition-set-v1",
            "entries": [e.to_dict() for e in self.entries],
        }
        d["manifest_hash"] = canonical_hash(d)
        return d


@dataclass
class Deadline:
    timeout_seconds: float | None = None
    start_time: float = field(default_factory=time.monotonic)

    def check(self) -> None:
        if self.timeout_seconds is not None and (
            time.monotonic() - self.start_time > self.timeout_seconds
        ):
            raise ResourceLimitError("qualification execution deadline exceeded")


class QualificationProgress(Protocol):
    def report(self, phase: str, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class SecNportQualificationRequest:
    root: str
    quarters: tuple[str, ...]
    universe_hash: str
    availability_policy: str
    lag_days: int | None
    output: str
    partition_set_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "root": self.root,
            "quarters": list(self.quarters),
            "universe_hash": self.universe_hash,
            "availability_policy": self.availability_policy,
            "lag_days": self.lag_days,
            "output": self.output,
            "partition_set_hash": self.partition_set_hash,
        }
        d["request_hash"] = canonical_hash(d)
        return d


@dataclass(frozen=True)
class SecNportQualificationRef:
    output_dir: Path
    receipt_identity: str
    receipt: dict[str, Any]
    counters: dict[str, Any]
    status: str


def select_exact_partitions(
    *,
    batch: SecNportBatch,
    quarters: tuple[Quarter, ...],
    universe: SecEquityEtfUniverse,
    availability_policy: str,
    lag_days: int | None,
    partition_set: SecNportPartitionSet | None = None,
) -> list[dict[str, Any]]:
    """Resolves exactly one validated partition per quarter, failing on ambiguity."""
    validated = batch.iter_validated_partitions(quarters)
    selected: list[dict[str, Any]] = []

    if partition_set is not None:
        set_quarters = {Quarter.parse(e.source_quarter) for e in partition_set.entries}
        req_quarters = set(quarters)
        if set_quarters != req_quarters:
            raise CoverageError("partition set quarters do not match requested quarters")

    for q in quarters:
        candidates = [
            e
            for e in validated
            if str(e.get("source_quarter")) == str(q)
            and str(e.get("universe_hash")) == universe.universe_hash
            and str(e.get("availability_policy")) == availability_policy
            and (e.get("lag_days") == lag_days or (lag_days is None and e.get("lag_days") == 0))
        ]

        if not candidates:
            raise CoverageError(f"no matching partition found for quarter {q}")

        if partition_set is not None:
            entry_map = {
                e.source_quarter: (e.partition_identity, e.manifest_hash)
                for e in partition_set.entries
            }
            target_identity, target_hash = entry_map[str(q)]
            exact = [
                c
                for c in candidates
                if str(c.get("partition_identity")) == target_identity
                and str(c.get("manifest_hash")) == target_hash
            ]
            if not exact:
                raise CoverageError(f"partition set target not found for quarter {q}")
            if len(exact) > 1:
                raise AmbiguousPartitionError(
                    f"duplicate matching partition in catalog for quarter {q}"
                )
            selected.append(exact[0])
        else:
            if len(candidates) > 1:
                distinct_identities = {str(c.get("partition_identity")) for c in candidates}
                if len(distinct_identities) > 1:
                    raise AmbiguousPartitionError(
                        f"ambiguous candidate partitions for quarter {q}; explicit partition_set required"
                    )
            selected.append(candidates[0])

    selected.sort(key=lambda x: str(x.get("source_quarter", "")))
    return selected


def _safe_rss_bytes() -> int:
    try:
        import sys

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage if sys.platform == "darwin" else usage * 1024
    except (OSError, AttributeError):
        return 0


def extract_qualification_facts(
    *,
    root: Path,
    selected_catalog_entries: list[dict[str, Any]],
    universe: SecEquityEtfUniverse,
    counters: dict[str, Any],
    is_replay: bool = False,
    deadline: Deadline | None = None,
    progress: QualificationProgress | None = None,
) -> tuple[
    list[dict[str, Any]],  # coverage_rows
    list[dict[str, Any]],  # vintage_quality_rows
    list[dict[str, Any]],  # amendment_facts_rows
    list[dict[str, Any]],  # identifier_quality_rows
    bool,  # has_ambiguity
    bool,  # has_partial_coverage
    set[str],  # all_vintage_identities
]:
    """Pure fact extraction from validated input partitions."""
    pq: Any = importlib.import_module("pyarrow.parquet")

    coverage_rows: list[dict[str, Any]] = []
    vintage_quality_rows: list[dict[str, Any]] = []
    amendment_facts_rows: list[dict[str, Any]] = []
    identifier_quality_rows: list[dict[str, Any]] = []

    has_ambiguity = False
    has_partial_coverage = False
    all_vintage_identities: set[str] = set()

    for idx, entry in enumerate(selected_catalog_entries):
        if deadline:
            deadline.check()

        q_str = str(entry["source_quarter"])
        quarter = Quarter.parse(q_str)
        part_path = root / str(entry["partition_path"])

        if progress and not is_replay:
            progress.report(
                "PROCESSING_PARTITION",
                quarter=q_str,
                partition_index=idx + 1,
                partition_count=len(selected_catalog_entries),
            )

        # 1. Read fund_vintages
        v_file = pq.ParquetFile(part_path / "fund_vintages.parquet")
        if not is_replay:
            counters["table_scans"] += 1
            counters["row_groups_read"] += v_file.num_row_groups
            v_table = v_file.read()
            v_rows: list[dict[str, Any]] = v_table.to_pylist()
            counters["fund_vintage_rows_read"] += len(v_rows)
        else:
            counters["replay_table_scans"] += 1
            v_table = v_file.read()
            v_rows = v_table.to_pylist()
            counters["replay_rows_read"] += len(v_rows)
        # 2. Read holdings
        h_file = pq.ParquetFile(part_path / "holdings.parquet")
        if not is_replay:
            counters["table_scans"] += 1
            counters["row_groups_read"] += h_file.num_row_groups
            h_table = h_file.read()
            h_rows: list[dict[str, Any]] = h_table.to_pylist()
            counters["holding_rows_read"] += len(h_rows)
        else:
            counters["replay_table_scans"] += 1
            h_table = h_file.read()
            h_rows = h_table.to_pylist()
            counters["replay_rows_read"] += len(h_rows)
        # 3. Read identifiers
        id_file = pq.ParquetFile(part_path / "identifiers.parquet")
        if not is_replay:
            counters["table_scans"] += 1
            counters["row_groups_read"] += id_file.num_row_groups
            id_table = id_file.read()
            id_rows: list[dict[str, Any]] = id_table.to_pylist()
            counters["identifier_rows_read"] += len(id_rows)
        else:
            counters["replay_table_scans"] += 1
            id_table = id_file.read()
            id_rows = id_table.to_pylist()
            counters["replay_rows_read"] += len(id_rows)
        # Check unique vintage identities
        for v in v_rows:
            v_id = str(v["vintage_identity"])
            if v_id in all_vintage_identities:
                raise SnapshotIntegrityError(f"duplicate vintage_identity across dataset: {v_id}")
            all_vintage_identities.add(v_id)

        # --- A. Quarter Fund Coverage ---
        active_selectors = universe.active_for(quarter)
        active_symbols = {s.selector.symbol for s in active_selectors}
        vintages_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for v in v_rows:
            sym = str(v.get("fund_symbol", ""))
            if sym not in active_symbols:
                raise SnapshotIntegrityError(
                    f"fund symbol {sym} in partition is outside reviewed active universe"
                )
            vintages_by_symbol.setdefault(sym, []).append(v)

        for scheduled in active_selectors:
            sel = scheduled.selector
            sym = sel.symbol
            sym_vintages = vintages_by_symbol.get(sym, [])
            v_count = len(sym_vintages)
            if v_count == 0:
                has_partial_coverage = True

            orig_count = sum(
                1
                for v in sym_vintages
                if v.get("submission_type") == "NPORT-P"
                or not str(v.get("submission_type", "")).endswith("/A")
            )
            amend_count = sum(
                1 for v in sym_vintages if str(v.get("submission_type", "")).endswith("/A")
            )

            rep_dates = [v["report_date"] for v in sym_vintages if v.get("report_date")]
            anchors = [
                v["availability_anchor"] for v in sym_vintages if v.get("availability_anchor")
            ]

            coverage_rows.append(
                {
                    "source_quarter": q_str,
                    "fund_symbol": sym,
                    "cik": sel.cik,
                    "series_id": sel.series_id,
                    "selection_mode": sel.selection_mode,
                    "expected": True,
                    "vintage_count": v_count,
                    "original_count": orig_count,
                    "amendment_count": amend_count,
                    "earliest_report_date": min(rep_dates) if rep_dates else None,
                    "latest_report_date": max(rep_dates) if rep_dates else None,
                    "earliest_availability_anchor": min(anchors) if anchors else None,
                    "latest_availability_anchor": max(anchors) if anchors else None,
                    "unknown_availability_count": sum(
                        1 for v in sym_vintages if v.get("availability_anchor") is None
                    ),
                    "partition_identity": str(entry["partition_identity"]),
                }
            )

        # --- B. Vintage and Amendment Facts ---
        # Group vintages by economic key: (cik, series_id, report_date)
        economic_families: dict[tuple[str, str | None, date], list[dict[str, Any]]] = {}
        for v in v_rows:
            ekey = (str(v["cik"]), v.get("series_id"), v["report_date"])
            economic_families.setdefault(ekey, []).append(v)

        max_dt = datetime.max.replace(tzinfo=UTC)
        for (cik_k, series_k, report_date_k), family in economic_families.items():
            # Sort family filings by:
            # 1. availability_anchor (nulls last)
            # 2. accepted_at (nulls last)
            # 3. filing_date
            # 4. accession_number
            sorted_family = sorted(
                family,
                key=lambda x: (
                    x.get("availability_anchor") or max_dt,
                    x.get("accepted_at") or max_dt,
                    x.get("filing_date") or date.max,
                    str(x.get("accession_number")),
                ),
            )

            family_size = len(sorted_family)
            for f_order, f_vintage in enumerate(sorted_family):
                pred_acc = sorted_family[f_order - 1]["accession_number"] if f_order > 0 else None
                if family_size == 1:
                    basis = "ORIGINAL_ONLY"
                elif f_order == 0:
                    basis = "ORIGINAL_IN_FAMILY"
                else:
                    prev_v = sorted_family[f_order - 1]
                    # Check if tie in timestamps and dates
                    if (
                        f_vintage.get("availability_anchor") == prev_v.get("availability_anchor")
                        and f_vintage.get("accepted_at") == prev_v.get("accepted_at")
                        and f_vintage.get("filing_date") == prev_v.get("filing_date")
                    ):
                        basis = "AMBIGUOUS"
                        has_ambiguity = True
                    else:
                        basis = "FORM_AND_ORDER_INFERRED"

                amendment_facts_rows.append(
                    {
                        "cik": cik_k,
                        "series_id": series_k,
                        "report_date": report_date_k,
                        "accession_number": f_vintage["accession_number"],
                        "submission_type": f_vintage["submission_type"],
                        "vintage_identity": f_vintage["vintage_identity"],
                        "family_size": family_size,
                        "family_order": f_order,
                        "predecessor_accession_number": pred_acc,
                        "relation_basis": basis,
                        "availability_anchor": f_vintage.get("availability_anchor"),
                        "payload_hash": f_vintage["payload_hash"],
                    }
                )

        # Index holdings by vintage_identity
        holdings_by_v: dict[str, list[dict[str, Any]]] = {}
        for h in h_rows:
            holdings_by_v.setdefault(str(h["vintage_identity"]), []).append(h)

        # Index identifiers by vintage_identity, then by holding_id
        ids_by_v_and_hid: dict[str, dict[str, list[dict[str, Any]]]] = {}
        ids_by_v_all: dict[str, list[dict[str, Any]]] = {}
        for id_r in id_rows:
            v_id_str = str(id_r["vintage_identity"])
            hid_str = str(id_r.get("holding_id", ""))
            ids_by_v_all.setdefault(v_id_str, []).append(id_r)
            ids_by_v_and_hid.setdefault(v_id_str, {}).setdefault(hid_str, []).append(id_r)

        # --- C. Weight Facts & D. Identifier Facts ---
        for v in v_rows:
            v_id_str = str(v["vintage_identity"])
            v_holdings = holdings_by_v.get(v_id_str, [])
            v_id_rows = ids_by_v_all.get(v_id_str, [])
            v_hid_map = ids_by_v_and_hid.get(v_id_str, {})

            # Weight facts calculations
            h_count = len(v_holdings)
            unique_hids = {
                str(r.get("holding_id")) for r in v_holdings if r.get("holding_id") is not None
            }
            nonnull_pct = sum(1 for r in v_holdings if r.get("percentage") is not None)
            null_pct = sum(1 for r in v_holdings if r.get("percentage") is None)
            zero_pct = sum(
                1 for r in v_holdings if r.get("percentage") is not None and r["percentage"] == 0
            )
            pos_pct = sum(
                1 for r in v_holdings if r.get("percentage") is not None and r["percentage"] > 0
            )
            neg_pct = sum(
                1 for r in v_holdings if r.get("percentage") is not None and r["percentage"] < 0
            )
            pct_sum = sum(
                (r["percentage"] for r in v_holdings if r.get("percentage") is not None),
                Decimal(0),
            )

            # Category aggregations
            deriv_rows = sum(1 for r in v_holdings if r.get("derivative_cat") not in (None, ""))
            deriv_weight = sum(
                (
                    r["percentage"]
                    for r in v_holdings
                    if r.get("derivative_cat") not in (None, "") and r.get("percentage") is not None
                ),
                Decimal(0),
            )

            restr_rows = sum(
                1 for r in v_holdings if r.get("is_restricted_security") in ("Y", True, "true")
            )
            restr_weight = sum(
                (
                    r["percentage"]
                    for r in v_holdings
                    if r.get("is_restricted_security") in ("Y", True, "true")
                    and r.get("percentage") is not None
                ),
                Decimal(0),
            )

            debt_rows = sum(
                1 for r in v_holdings if str(r.get("asset_cat", "")).upper() in ("DBT", "DEBT")
            )
            debt_weight = sum(
                (
                    r["percentage"]
                    for r in v_holdings
                    if str(r.get("asset_cat", "")).upper() in ("DBT", "DEBT")
                    and r.get("percentage") is not None
                ),
                Decimal(0),
            )

            cash_rows = sum(
                1
                for r in v_holdings
                if str(r.get("asset_cat", "")).upper() in ("CSH", "CASH", "STIV")
            )
            cash_weight = sum(
                (
                    r["percentage"]
                    for r in v_holdings
                    if str(r.get("asset_cat", "")).upper() in ("CSH", "CASH", "STIV")
                    and r.get("percentage") is not None
                ),
                Decimal(0),
            )

            unknown_cat_rows = sum(
                1
                for r in v_holdings
                if r.get("asset_cat") in (None, "", "OTHER")
                and r.get("issuer_type") in (None, "")
                and r.get("payoff_profile") in (None, "")
            )
            unknown_cat_weight = sum(
                (
                    r["percentage"]
                    for r in v_holdings
                    if r.get("asset_cat") in (None, "", "OTHER")
                    and r.get("issuer_type") in (None, "")
                    and r.get("payoff_profile") in (None, "")
                    and r.get("percentage") is not None
                ),
                Decimal(0),
            )

            missing_curr = sum(1 for r in v_holdings if r.get("currency_code") in (None, ""))
            missing_fx = sum(1 for r in v_holdings if r.get("exchange_rate") is None)
            missing_bal = sum(1 for r in v_holdings if r.get("balance") is None)
            missing_val = sum(1 for r in v_holdings if r.get("currency_value") is None)

            vintage_quality_rows.append(
                {
                    "source_quarter": q_str,
                    "fund_symbol": v["fund_symbol"],
                    "cik": v["cik"],
                    "series_id": v.get("series_id"),
                    "accession_number": v["accession_number"],
                    "report_date": v["report_date"],
                    "filing_date": v["filing_date"],
                    "accepted_at": v.get("accepted_at"),
                    "observed_at": v["observed_at"],
                    "availability_anchor": v.get("availability_anchor"),
                    "availability_basis": v["availability_basis"],
                    "availability_precision": v["availability_precision"],
                    "availability_policy": v["availability_policy"],
                    "availability_lag_days": v["availability_lag_days"],
                    "submission_type": v["submission_type"],
                    "vintage_identity": v["vintage_identity"],
                    "holding_row_count": h_count,
                    "unique_holding_id_count": len(unique_hids),
                    "nonnull_percentage_count": nonnull_pct,
                    "null_percentage_count": null_pct,
                    "zero_percentage_count": zero_pct,
                    "positive_percentage_count": pos_pct,
                    "negative_percentage_count": neg_pct,
                    "percentage_sum": pct_sum,
                    "derivative_rows_count": deriv_rows,
                    "derivative_weight": deriv_weight,
                    "restricted_rows_count": restr_rows,
                    "restricted_weight": restr_weight,
                    "debt_like_rows_count": debt_rows,
                    "debt_like_weight": debt_weight,
                    "cash_like_rows_count": cash_rows,
                    "cash_like_weight": cash_weight,
                    "unknown_cat_rows_count": unknown_cat_rows,
                    "unknown_cat_weight": unknown_cat_weight,
                    "missing_currency_code_count": missing_curr,
                    "missing_exchange_rate_count": missing_fx,
                    "missing_balance_count": missing_bal,
                    "missing_currency_value_count": missing_val,
                    "total_assets": v.get("total_assets"),
                    "total_liabilities": v.get("total_liabilities"),
                    "net_assets": v.get("net_assets"),
                    "quality_flags": list(v.get("quality_flags") or []),
                }
            )

            # Identifier facts calculations
            tot_hids = len(unique_hids)
            zero_id = sum(
                1 for hid in unique_hids if hid not in v_hid_map or len(v_hid_map[hid]) == 0
            )
            single_id = sum(1 for hid in unique_hids if len(v_hid_map.get(hid, [])) == 1)
            multi_id = sum(1 for hid in unique_hids if len(v_hid_map.get(hid, [])) > 1)
            any_id = sum(1 for hid in unique_hids if len(v_hid_map.get(hid, [])) >= 1)
            any_id_pct = sum(
                (
                    r["percentage"]
                    for r in v_holdings
                    if str(r.get("holding_id")) in v_hid_map
                    and len(v_hid_map[str(r.get("holding_id"))]) >= 1
                    and r.get("percentage") is not None
                ),
                Decimal(0),
            )

            cusip_cnt = sum(1 for r in v_holdings if r.get("issuer_cusip") not in (None, ""))
            isin_cnt = sum(1 for r in v_id_rows if r.get("identifier_isin") not in (None, ""))
            ticker_cnt = sum(1 for r in v_id_rows if r.get("identifier_ticker") not in (None, ""))
            other_cnt = sum(1 for r in v_id_rows if r.get("other_identifier") not in (None, ""))

            # Duplicate identifier values across different holding_ids
            val_to_hids: dict[str, set[str]] = {}
            for r in v_holdings:
                c = r.get("issuer_cusip")
                if c:
                    val_to_hids.setdefault(f"CUSIP:{c}", set()).add(str(r.get("holding_id")))
            for r in v_id_rows:
                hid = str(r.get("holding_id"))
                isin = r.get("identifier_isin")
                ticker = r.get("identifier_ticker")
                if isin:
                    val_to_hids.setdefault(f"ISIN:{isin}", set()).add(hid)
                if ticker:
                    val_to_hids.setdefault(f"TICKER:{ticker}", set()).add(hid)

            dup_id_cnt = sum(1 for val, h_set in val_to_hids.items() if len(h_set) > 1)

            # Multi-value of same type per holding_id
            multi_val_cnt = 0
            for hid, hid_rows in v_hid_map.items():
                isins = {r.get("identifier_isin") for r in hid_rows if r.get("identifier_isin")}
                tickers = {
                    r.get("identifier_ticker") for r in hid_rows if r.get("identifier_ticker")
                }
                if len(isins) > 1 or len(tickers) > 1:
                    multi_val_cnt += 1

            # Identifier associated with multiple issuer names
            val_to_issuers: dict[str, set[str]] = {}
            hid_to_issuer = {
                str(r.get("holding_id")): str(r.get("issuer_name", "")).strip().upper()
                for r in v_holdings
                if r.get("holding_id") is not None and r.get("issuer_name")
            }
            for val, h_set in val_to_hids.items():
                issuers = {hid_to_issuer[h] for h in h_set if h in hid_to_issuer}
                if len(issuers) > 1:
                    val_to_issuers[val] = issuers
            multi_issuer_cnt = len(val_to_issuers)

            null_empty_id_cnt = sum(
                1
                for r in v_id_rows
                if r.get("identifier_isin") in (None, "")
                and r.get("identifier_ticker") in (None, "")
                and r.get("other_identifier") in (None, "")
            )

            identifier_quality_rows.append(
                {
                    "source_quarter": q_str,
                    "fund_symbol": v["fund_symbol"],
                    "cik": v["cik"],
                    "series_id": v.get("series_id"),
                    "accession_number": v["accession_number"],
                    "vintage_identity": v["vintage_identity"],
                    "total_holding_count": tot_hids,
                    "zero_identifier_holding_count": zero_id,
                    "single_identifier_holding_count": single_id,
                    "multi_identifier_holding_count": multi_id,
                    "any_identifier_holding_count": any_id,
                    "any_identifier_percentage_sum": any_id_pct,
                    "cusip_present_count": cusip_cnt,
                    "isin_present_count": isin_cnt,
                    "ticker_present_count": ticker_cnt,
                    "other_present_count": other_cnt,
                    "duplicate_identifier_value_count": dup_id_cnt,
                    "multi_value_same_type_holding_count": multi_val_cnt,
                    "multi_issuer_name_identifier_count": multi_issuer_cnt,
                    "null_or_empty_identifier_count": null_empty_id_cnt,
                }
            )

    return (
        coverage_rows,
        vintage_quality_rows,
        amendment_facts_rows,
        identifier_quality_rows,
        has_ambiguity,
        has_partial_coverage,
        all_vintage_identities,
    )


def reconstruct_and_verify_candidate_bundle(
    *,
    root: Path,
    stage_dir: Path,
    selected_catalog_entries: list[dict[str, Any]],
    universe: SecEquityEtfUniverse,
    request_obj: SecNportQualificationRequest,
    output_artifacts: dict[str, Any],
    counters: dict[str, Any],
    deadline: Deadline | None = None,
) -> None:
    """Independently reconstructs facts from validated source partitions and verifies against candidate tables in staging."""
    pa = importlib.import_module("pyarrow")
    pq: Any = importlib.import_module("pyarrow.parquet")

    if deadline:
        deadline.check()

    # 1. Reopen request JSON and verify
    req_path = stage_dir / "qualification_request.json"
    if not req_path.is_file():
        raise SnapshotIntegrityError("qualification_request.json missing in staging")
    staged_req = json.loads(req_path.read_text(encoding="utf-8"))
    if staged_req != request_obj.to_dict():
        raise SnapshotIntegrityError("staged request does not match canonical request")
    if _sha256_file(req_path) != output_artifacts["qualification_request.json"]["sha256"]:
        raise SnapshotIntegrityError("qualification_request.json sha256 mismatch")
    if req_path.stat().st_size != output_artifacts["qualification_request.json"]["bytes"]:
        raise SnapshotIntegrityError("qualification_request.json bytes mismatch")

    # 2. Rerun production source validation on input partitions
    for entry in selected_catalog_entries:
        if deadline:
            deadline.check()
        part_path = root / str(entry["partition_path"])
        manifest_path = part_path / "manifest.json"
        if not manifest_path.is_file():
            raise CoverageError(f"partition manifest missing for {entry.get('source_quarter')}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if canonical_hash(manifest) != entry.get("manifest_hash"):
            raise SnapshotIntegrityError("catalog manifest mismatch during replay")
        try:
            validated = validate_tables(part_path)
        except Exception as exc:
            raise SnapshotIntegrityError(
                f"source partition validation failed during replay: {exc}"
            ) from exc
        if validated["files"] != entry.get("file_hashes"):
            raise SnapshotIntegrityError("source partition file hash mismatch during replay")
        actual_counts = {
            n: pq.ParquetFile(part_path / f"{n}.parquet").metadata.num_rows
            for n in ("fund_vintages", "holdings", "identifiers")
        }
        if manifest.get("row_counts") != actual_counts:
            raise SnapshotIntegrityError("source row count mismatch during replay")

    # 3. Independently reconstruct all 4 fact tables from validated source partitions
    (
        recon_cov,
        recon_vint,
        recon_amend,
        recon_id,
        _,
        _,
        _,
    ) = extract_qualification_facts(
        root=root,
        selected_catalog_entries=selected_catalog_entries,
        universe=universe,
        counters=counters,
        is_replay=True,
        deadline=deadline,
        progress=None,
    )

    # 4. Lexically sort reconstructed rows
    sorted_recon_cov = sorted(
        recon_cov,
        key=lambda r: (str(r.get("source_quarter")), str(r.get("fund_symbol"))),
    )
    sorted_recon_vint = sorted(
        recon_vint,
        key=lambda r: (
            str(r.get("source_quarter")),
            str(r.get("fund_symbol")),
            str(r.get("report_date")),
            str(r.get("accession_number")),
            str(r.get("vintage_identity")),
        ),
    )
    sorted_recon_amend = sorted(
        recon_amend,
        key=lambda r: (
            str(r.get("cik")),
            str(r.get("series_id") or ""),
            str(r.get("report_date")),
            int(r.get("family_order", 0)),
            str(r.get("accession_number")),
        ),
    )
    sorted_recon_id = sorted(
        recon_id,
        key=lambda r: (
            str(r.get("source_quarter")),
            str(r.get("fund_symbol")),
            str(r.get("accession_number")),
            str(r.get("vintage_identity")),
        ),
    )

    # 5. Reopen candidate tables from staging and compare
    tables_to_check = (
        (
            "quarter_fund_coverage.parquet",
            sorted_recon_cov,
            get_coverage_schema(pa),
            "quarter_fund_coverage",
        ),
        (
            "vintage_quality.parquet",
            sorted_recon_vint,
            get_vintage_quality_schema(pa),
            "vintage_quality",
        ),
        (
            "amendment_facts.parquet",
            sorted_recon_amend,
            get_amendment_facts_schema(pa),
            "amendment_facts",
        ),
        (
            "identifier_quality.parquet",
            sorted_recon_id,
            get_identifier_quality_schema(pa),
            "identifier_quality",
        ),
    )

    for file_name, sorted_recon, schema, table_key in tables_to_check:
        if deadline:
            deadline.check()
        file_path = stage_dir / file_name
        if not file_path.is_file():
            raise CoverageError(f"qualification artifact {file_name} missing on disk")
        art_desc = output_artifacts.get(file_name)
        if not art_desc:
            raise SnapshotIntegrityError(f"artifact descriptor missing for {file_name}")

        actual_sha = _sha256_file(file_path)
        actual_bytes = file_path.stat().st_size
        if actual_sha != art_desc.get("sha256") or actual_bytes != art_desc.get("bytes"):
            raise SnapshotIntegrityError(f"hash or byte mismatch for artifact {file_name}")

        counters["replay_table_scans"] += 1
        tbl = pq.read_table(file_path)
        counters["replay_rows_read"] += tbl.num_rows

        if tbl.num_rows != len(sorted_recon) or tbl.num_rows != art_desc.get("row_count"):
            raise SnapshotIntegrityError(f"row count mismatch for {file_name}")

        expected_fp = schema_fingerprint(schema)
        actual_fp = schema_fingerprint(tbl.schema)
        if actual_fp != expected_fp or actual_fp != art_desc.get("schema_fingerprint"):
            raise SchemaMismatchError(f"schema fingerprint mismatch for {file_name}")

        recon_tbl = pa.Table.from_pylist(sorted_recon, schema=schema)
        recon_rows = recon_tbl.to_pylist()

        actual_rows = tbl.to_pylist()
        if actual_rows != recon_rows:
            raise SnapshotIntegrityError(
                f"reconstructed rows mismatch candidate rows for {file_name}"
            )

        actual_log_hash = logical_table_hash(table_key, actual_rows)
        if actual_log_hash != art_desc.get("logical_hash"):
            raise SnapshotIntegrityError(f"logical table hash mismatch for {file_name}")
        recon_log_hash = logical_table_hash(table_key, recon_rows)
        if recon_log_hash != art_desc.get("logical_hash"):
            raise SnapshotIntegrityError(f"reconstructed logical hash mismatch for {file_name}")


def qualify_sec_nport(
    *,
    root: Path,
    quarters: tuple[Quarter, ...],
    universe: SecEquityEtfUniverse,
    availability_policy: str,
    lag_days: int | None,
    output: Path,
    partition_set: SecNportPartitionSet | None = None,
    progress: QualificationProgress | None = None,
    deadline: Deadline | None = None,
) -> SecNportQualificationRef:
    """Core entrypoint for SEC N-PORT structural qualification."""
    start_time = time.monotonic()
    started_at_iso = datetime.now(UTC).isoformat()

    if not quarters:
        raise ValueError("quarters cannot be empty")
    if len(quarters) != len(set(quarters)):
        raise ValueError("quarters must not contain duplicate entries")
    if sorted(quarters) != list(quarters):
        raise ValueError("quarters must be in canonical sorted order")
    if availability_policy not in ("observation-only", "accepted-at-plus-lag"):
        raise ValueError(f"unsupported availability policy: {availability_policy}")
    if availability_policy == "observation-only" and lag_days not in (None, 0):
        raise ValueError("lag_days is invalid for observation-only policy")

    if output.resolve().exists():
        raise FileExistsError(f"output directory already exists: {output}")
    if output.is_symlink():
        raise SnapshotIntegrityError(f"output path is a symlink: {output}")

    if deadline:
        deadline.check()

    if progress:
        progress.report("VALIDATING_SOURCES", partition_count=len(quarters))

    batch = SecNportBatch(root)
    # Step 1: Select exact partitions with integrity validation
    selected_catalog_entries = select_exact_partitions(
        batch=batch,
        quarters=quarters,
        universe=universe,
        availability_policy=availability_policy,
        lag_days=lag_days,
        partition_set=partition_set,
    )

    if progress:
        progress.report("SOURCES_VALIDATED", partition_count=len(selected_catalog_entries))

    # Counters
    counters: dict[str, Any] = {
        "partitions_selected": len(selected_catalog_entries),
        "fund_vintage_rows_read": 0,
        "holding_rows_read": 0,
        "identifier_rows_read": 0,
        "table_scans": 0,
        "row_groups_read": 0,
        "qualification_rows_written": 0,
        "replay_rows_read": 0,
        "replay_table_scans": 0,
        "phase_elapsed_seconds": 0.0,
        "peak_rss_bytes": _safe_rss_bytes(),
    }

    # Prepare private sibling staging directory
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage_dir = parent / f".tmp-qualify-{uuid.uuid4().hex}"
    stage_dir.mkdir(parents=True, exist_ok=False)

    try:
        if progress:
            progress.report(
                "CONSTRUCTING_CANDIDATE_FACTS",
                partition_count=len(selected_catalog_entries),
            )

        # Production extraction pass
        (
            coverage_rows,
            vintage_quality_rows,
            amendment_facts_rows,
            identifier_quality_rows,
            has_ambiguity,
            has_partial_coverage,
            all_vintage_identities,
        ) = extract_qualification_facts(
            root=root,
            selected_catalog_entries=selected_catalog_entries,
            universe=universe,
            counters=counters,
            is_replay=False,
            deadline=deadline,
            progress=progress,
        )

        counters["qualification_rows_written"] = (
            len(coverage_rows)
            + len(vintage_quality_rows)
            + len(amendment_facts_rows)
            + len(identifier_quality_rows)
        )

        # Construct partition set document for receipt
        pset_dict = (
            partition_set.to_dict()
            if partition_set is not None
            else SecNportPartitionSet(
                tuple(
                    SecNportPartitionSetEntry(
                        str(e["source_quarter"]),
                        str(e["partition_identity"]),
                        str(e["manifest_hash"]),
                    )
                    for e in selected_catalog_entries
                )
            ).to_dict()
        )

        request_obj = SecNportQualificationRequest(
            root=str(root.resolve()),
            quarters=tuple(str(q) for q in quarters),
            universe_hash=universe.universe_hash,
            availability_policy=availability_policy,
            lag_days=lag_days,
            output=str(output.resolve()),
            partition_set_hash=pset_dict.get("manifest_hash"),
        )

        if progress:
            progress.report("WRITING_CANDIDATE_TABLES")

        output_artifacts, schemas_desc = write_candidate_qualification_tables(
            stage_dir=stage_dir,
            request_data=request_obj.to_dict(),
            coverage_rows=coverage_rows,
            vintage_quality_rows=vintage_quality_rows,
            amendment_facts_rows=amendment_facts_rows,
            identifier_quality_rows=identifier_quality_rows,
        )

        if progress:
            progress.report("INDEPENDENT_REPLAY_VALIDATION")

        # Independent replay & source reconstruction
        reconstruct_and_verify_candidate_bundle(
            root=root,
            stage_dir=stage_dir,
            selected_catalog_entries=selected_catalog_entries,
            universe=universe,
            request_obj=request_obj,
            output_artifacts=output_artifacts,
            counters=counters,
            deadline=deadline,
        )

        # Assess status & gates (only after replay completes successfully)
        if has_ambiguity:
            status = "STRUCTURALLY_AMBIGUOUS"
        elif has_partial_coverage:
            status = "STRUCTURALLY_PARTIAL"
        else:
            status = "STRUCTURALLY_COMPLETE"

        gates = {
            "SOURCE_VALIDATED": True,
            "PARTITION_SET_EXACT": True,
            "EXPECTED_FUND_QUARTERS_ACCOUNTED": not has_partial_coverage,
            "FUND_IDENTITIES_EXACT": True,
            "VINTAGE_IDENTITIES_UNIQUE": len(all_vintage_identities) == len(vintage_quality_rows),
            "AMENDMENT_FAMILIES_DETERMINISTIC": not has_ambiguity,
            "WEIGHT_FACTS_RECONSTRUCTED": True,
            "IDENTIFIER_FACTS_RECONSTRUCTED": True,
            "AVAILABILITY_FACTS_RECONSTRUCTED": True,
            "ARTIFACTS_REOPENED": True,
            "RESOURCE_ENVELOPE_PASSED": True,
        }

        completed_at_iso = datetime.now(UTC).isoformat()
        elapsed = time.monotonic() - start_time
        counters["phase_elapsed_seconds"] = elapsed
        counters["peak_rss_bytes"] = max(counters["peak_rss_bytes"], _safe_rss_bytes())

        receipt_dict: dict[str, Any] = {
            "schema_version": QUALIFICATION_RECEIPT_SCHEMA,
            "request": request_obj.to_dict(),
            "universe": {
                "schema_version": "sec-equity-etf-universe-v1",
                "universe_hash": universe.universe_hash,
                "fund_count": len(universe.funds),
            },
            "partition_set": pset_dict,
            "input_partitions": [
                {
                    "source_quarter": str(e["source_quarter"]),
                    "partition_identity": str(e["partition_identity"]),
                    "manifest_hash": str(e["manifest_hash"]),
                    "artifact_sha256": str(e["artifact_sha256"]),
                }
                for e in selected_catalog_entries
            ],
            "output_artifacts": output_artifacts,
            "schemas": schemas_desc,
            "counters": dict(counters),
            "coverage_summary": {
                "requested_quarters": len(quarters),
                "expected_funds": len(coverage_rows),
                "total_vintages": len(vintage_quality_rows),
                "status": status,
            },
            "availability_summary": {
                "policy": availability_policy,
                "lag_days": lag_days,
                "total_vintages": len(vintage_quality_rows),
                "unknown_anchors": sum(
                    1 for v in vintage_quality_rows if v.get("availability_anchor") is None
                ),
            },
            "gates": gates,
            "qualification_status": status,
            "started_at": started_at_iso,
            "completed_at": completed_at_iso,
            "elapsed_seconds": elapsed,
            "peak_rss_bytes": counters["peak_rss_bytes"],
        }

        if progress:
            progress.report("PUBLISHING_BUNDLE")

        receipt_identity = publish_qualification_receipt_and_rename(
            stage_dir=stage_dir,
            output_dir=output,
            receipt_data=receipt_dict,
        )

        return SecNportQualificationRef(
            output_dir=output,
            receipt_identity=receipt_identity,
            receipt=receipt_dict,
            counters=counters,
            status=status,
        )
    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def reconstruct_and_verify_qualification(
    *,
    output_dir: Path,
    expected_receipt: dict[str, Any] | None = None,
    counters: dict[str, Any] | None = None,
    root: Path | None = None,
    universe: SecEquityEtfUniverse | None = None,
    partition_set: SecNportPartitionSet | None = None,
    deadline: Deadline | None = None,
) -> dict[str, Any]:
    """Independently reopens and validates the published qualification bundle."""
    pa = importlib.import_module("pyarrow")
    pq: Any = importlib.import_module("pyarrow.parquet")

    req_path = output_dir / "qualification_request.json"
    rcp_path = output_dir / "qualification_receipt.json"

    if not req_path.is_file() or not rcp_path.is_file():
        raise SnapshotIntegrityError("qualification bundle missing request or receipt JSON")

    rcp_on_disk = json.loads(rcp_path.read_text(encoding="utf-8"))
    if rcp_on_disk.get("schema_version") != QUALIFICATION_RECEIPT_SCHEMA:
        raise SchemaMismatchError("qualification receipt schema mismatch on disk")

    if expected_receipt is not None and (
        canonical_hash(rcp_on_disk) != canonical_hash(expected_receipt)
        or rcp_on_disk != expected_receipt
    ):
        raise SnapshotIntegrityError("on-disk receipt does not match expected receipt")

    artifacts = rcp_on_disk.get("output_artifacts", {})
    for name, schema_fn in (
        ("quarter_fund_coverage.parquet", get_coverage_schema),
        ("vintage_quality.parquet", get_vintage_quality_schema),
        ("amendment_facts.parquet", get_amendment_facts_schema),
        ("identifier_quality.parquet", get_identifier_quality_schema),
    ):
        p = output_dir / name
        if not p.is_file():
            raise CoverageError(f"qualification artifact {name} missing on disk")
        art_desc = artifacts.get(name)
        if not art_desc:
            raise SnapshotIntegrityError(f"artifact descriptor missing for {name}")

        # Check sha256 and bytes
        actual_sha = _sha256_file(p)
        actual_bytes = p.stat().st_size
        if actual_sha != art_desc.get("sha256") or actual_bytes != art_desc.get("bytes"):
            raise SnapshotIntegrityError(f"hash or byte mismatch for artifact {name}")

        # Reopen table and verify schema & row count
        if counters is not None:
            counters["replay_table_scans"] = counters.get("replay_table_scans", 0) + 1
        tbl = pq.read_table(p)
        if counters is not None:
            counters["replay_rows_read"] = counters.get("replay_rows_read", 0) + tbl.num_rows

        if tbl.num_rows != art_desc.get("row_count"):
            raise SnapshotIntegrityError(f"row count mismatch for {name}")

        expected_schema = schema_fn(pa)
        expected_fp = schema_fingerprint(expected_schema)
        actual_fp = schema_fingerprint(tbl.schema)
        if actual_fp != expected_fp or actual_fp != art_desc.get("schema_fingerprint"):
            raise SchemaMismatchError(f"schema fingerprint mismatch for {name}")

        # Verify logical table hash
        table_key = name.removesuffix(".parquet")
        actual_log_hash = logical_table_hash(table_key, tbl.to_pylist())
        if actual_log_hash != art_desc.get("logical_hash"):
            raise SnapshotIntegrityError(f"logical table hash mismatch for {name}")

    if root is not None and universe is not None:
        req_dict = rcp_on_disk.get("request", {})
        quarters = tuple(Quarter.parse(q) for q in req_dict.get("quarters", []))
        batch = SecNportBatch(root)
        selected = select_exact_partitions(
            batch=batch,
            quarters=quarters,
            universe=universe,
            availability_policy=req_dict.get("availability_policy", ""),
            lag_days=req_dict.get("lag_days"),
            partition_set=partition_set,
        )
        dummy_counters: dict[str, Any] = {
            "replay_table_scans": 0,
            "replay_rows_read": 0,
        }
        recon_cov, recon_vint, recon_amend, recon_id, _, _, _ = extract_qualification_facts(
            root=root,
            selected_catalog_entries=selected,
            universe=universe,
            counters=dummy_counters,
            is_replay=True,
            deadline=deadline,
        )
        check_pairs = (
            (
                "quarter_fund_coverage.parquet",
                recon_cov,
                lambda r: (str(r.get("source_quarter")), str(r.get("fund_symbol"))),
            ),
            (
                "vintage_quality.parquet",
                recon_vint,
                lambda r: (
                    str(r.get("source_quarter")),
                    str(r.get("fund_symbol")),
                    str(r.get("report_date")),
                    str(r.get("accession_number")),
                    str(r.get("vintage_identity")),
                ),
            ),
            (
                "amendment_facts.parquet",
                recon_amend,
                lambda r: (
                    str(r.get("cik")),
                    str(r.get("series_id") or ""),
                    str(r.get("report_date")),
                    int(r.get("family_order", 0)),
                    str(r.get("accession_number")),
                ),
            ),
            (
                "identifier_quality.parquet",
                recon_id,
                lambda r: (
                    str(r.get("source_quarter")),
                    str(r.get("fund_symbol")),
                    str(r.get("accession_number")),
                    str(r.get("vintage_identity")),
                ),
            ),
        )
        for fname, rows, sort_fn in check_pairs:
            tbl_rows = pq.read_table(output_dir / fname).to_pylist()
            if tbl_rows != sorted(rows, key=sort_fn):
                raise SnapshotIntegrityError(f"source reconstruction mismatch for {fname}")

    return rcp_on_disk
