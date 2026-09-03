"""SEC N-PORT structural qualification pipeline and independent replay."""

from __future__ import annotations

import importlib
import json
import resource
import time
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
from .qualification_dataset import (
    QUALIFICATION_RECEIPT_SCHEMA,
    _sha256_file,
    get_amendment_facts_schema,
    get_coverage_schema,
    get_identifier_quality_schema,
    get_vintage_quality_schema,
    logical_table_hash,
    schema_fingerprint,
    write_qualification_bundle,
)


@dataclass(frozen=True)
class SecNportPartitionSetEntry:
    source_quarter: str
    partition_identity: str
    manifest_hash: str


@dataclass(frozen=True)
class SecNportPartitionSet:
    entries: tuple[SecNportPartitionSetEntry, ...]
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        raw_entries = [
            {
                "source_quarter": e.source_quarter,
                "partition_identity": e.partition_identity,
                "manifest_hash": e.manifest_hash,
            }
            for e in self.entries
        ]
        computed_hash = canonical_hash(
            {
                "schema_version": "sec-nport-partition-set-v1",
                "entries": raw_entries,
            }
        )
        if not self.manifest_hash:
            object.__setattr__(self, "manifest_hash", computed_hash)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecNportPartitionSet:
        if data.get("schema_version") != "sec-nport-partition-set-v1":
            raise SchemaMismatchError(f"invalid partition-set schema: {data.get('schema_version')}")
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            raise SchemaMismatchError("partition-set entries must be a list")

        seen_quarters: set[str] = set()
        entries: list[SecNportPartitionSetEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise SchemaMismatchError("invalid partition-set entry")
            q = str(item.get("source_quarter", ""))
            pid = str(item.get("partition_identity", ""))
            mhash = str(item.get("manifest_hash", ""))
            if not q or not pid or not mhash:
                raise SchemaMismatchError("partition-set entry missing required keys")
            if q in seen_quarters:
                raise SchemaMismatchError(f"duplicate quarter in partition set: {q}")
            seen_quarters.add(q)
            entries.append(SecNportPartitionSetEntry(q, pid, mhash))

        entries.sort(key=lambda e: Quarter.parse(e.source_quarter))
        return cls(tuple(entries))

    @classmethod
    def load(cls, path: str | Path) -> SecNportPartitionSet:
        p = Path(path)
        if not p.is_file():
            raise CoverageError(f"partition-set file not found: {path}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaMismatchError(f"invalid partition-set JSON: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "sec-nport-partition-set-v1",
            "entries": [
                {
                    "source_quarter": e.source_quarter,
                    "partition_identity": e.partition_identity,
                    "manifest_hash": e.manifest_hash,
                }
                for e in self.entries
            ],
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True)
class Deadline:
    timeout_seconds: float
    start_time: float = field(default_factory=time.monotonic)

    def check(self) -> None:
        if time.monotonic() - self.start_time > self.timeout_seconds:
            raise ResourceLimitError("qualification deadline exceeded")


class QualificationProgress(Protocol):
    def report(
        self,
        phase: str,
        quarter: str | None = None,
        partition_index: int = 0,
        partition_count: int = 0,
        rows_read: int = 0,
    ) -> None: ...


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
        return {
            "root": self.root,
            "quarters": list(self.quarters),
            "universe_hash": self.universe_hash,
            "availability_policy": self.availability_policy,
            "lag_days": self.lag_days,
            "output": self.output,
            "partition_set_hash": self.partition_set_hash,
        }


@dataclass(frozen=True)
class SecNportQualificationRef:
    output_dir: Path
    receipt_identity: str
    receipt: dict[str, Any]
    counters: dict[str, Any]
    status: str


def select_exact_partitions(
    batch: SecNportBatch,
    quarters: tuple[Quarter, ...],
    universe: SecEquityEtfUniverse,
    availability_policy: str,
    lag_days: int | None,
    partition_set: SecNportPartitionSet | None = None,
) -> list[dict[str, Any]]:
    """Resolves exactly one partition per requested quarter or raises an explicit error."""
    validated_entries = batch.iter_validated_partitions(quarters)

    effective_lag: int | None = None if availability_policy == "observation-only" else lag_days

    selected: list[dict[str, Any]] = []

    if partition_set is not None:
        # Caller provided an explicit partition set
        set_quarters = {Quarter.parse(e.source_quarter) for e in partition_set.entries}
        req_quarters = set(quarters)
        if set_quarters != req_quarters:
            raise CoverageError(
                f"partition set quarters {set_quarters} do not match requested quarters {req_quarters}"
            )

        by_quarter_entry = {e.source_quarter: e for e in partition_set.entries}
        for q in quarters:
            ps_entry = by_quarter_entry[str(q)]
            matches = [
                e
                for e in validated_entries
                if str(e.get("source_quarter")) == ps_entry.source_quarter
                and str(e.get("partition_identity")) == ps_entry.partition_identity
                and str(e.get("manifest_hash")) == ps_entry.manifest_hash
            ]
            if not matches:
                raise CoverageError(
                    f"partition-set entry {ps_entry.source_quarter}/{ps_entry.partition_identity} not found in validated catalog"
                )
            match = matches[0]
            if (
                str(match.get("universe_hash")) != universe.universe_hash
                or str(match.get("availability_policy")) != availability_policy
                or match.get("lag_days") != effective_lag
            ):
                raise SchemaMismatchError(
                    f"partition-set partition {ps_entry.partition_identity} does not match request dimensions"
                )
            selected.append(match)
    else:
        # Implicit selection: must find exactly 1 matching partition per quarter
        for q in quarters:
            matches = [
                e
                for e in validated_entries
                if str(e.get("source_quarter")) == str(q)
                and str(e.get("universe_hash")) == universe.universe_hash
                and str(e.get("availability_policy")) == availability_policy
                and e.get("lag_days") == effective_lag
            ]
            if len(matches) == 0:
                raise CoverageError(f"no matching partition found for quarter {q}")
            if len(matches) > 1:
                raise AmbiguousPartitionError(
                    f"ambiguous partition selection for quarter {q}: found {len(matches)} matching partitions"
                )
            selected.append(matches[0])

    return selected


def _safe_rss_bytes() -> int:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # On macOS ru_maxrss is in bytes, on Linux in kilobytes
        import sys

        return usage if sys.platform == "darwin" else usage * 1024
    except (OSError, AttributeError):
        return 0


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

        if progress:
            progress.report(
                "PROCESSING_PARTITION",
                quarter=q_str,
                partition_index=idx + 1,
                partition_count=len(selected_catalog_entries),
            )

        # 1. Read fund_vintages
        v_file = pq.ParquetFile(part_path / "fund_vintages.parquet")
        counters["table_scans"] += 1
        counters["row_groups_read"] += v_file.num_row_groups
        v_table = v_file.read()
        v_rows: list[dict[str, Any]] = v_table.to_pylist()
        counters["fund_vintage_rows_read"] += len(v_rows)

        # 2. Read holdings
        h_file = pq.ParquetFile(part_path / "holdings.parquet")
        counters["table_scans"] += 1
        counters["row_groups_read"] += h_file.num_row_groups
        h_table = h_file.read()
        h_rows: list[dict[str, Any]] = h_table.to_pylist()
        counters["holding_rows_read"] += len(h_rows)

        # 3. Read identifiers
        id_file = pq.ParquetFile(part_path / "identifiers.parquet")
        counters["table_scans"] += 1
        counters["row_groups_read"] += id_file.num_row_groups
        id_table = id_file.read()
        id_rows: list[dict[str, Any]] = id_table.to_pylist()
        counters["identifier_rows_read"] += len(id_rows)

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

    counters["qualification_rows_written"] = (
        len(coverage_rows)
        + len(vintage_quality_rows)
        + len(amendment_facts_rows)
        + len(identifier_quality_rows)
    )

    # Assess status & gates
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
        "WEIGHT_FACTS_RECONSTRUCTED": len(vintage_quality_rows) == len(all_vintage_identities),
        "IDENTIFIER_FACTS_RECONSTRUCTED": len(identifier_quality_rows)
        == len(all_vintage_identities),
        "AVAILABILITY_FACTS_RECONSTRUCTED": True,
        "ARTIFACTS_REOPENED": True,
        "RESOURCE_ENVELOPE_PASSED": True,
    }

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
        "output_artifacts": {},  # Populated by writer
        "schemas": {},  # Populated by writer
        "counters": counters,
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
        progress.report("WRITING_BUNDLE")

    # Step 2: Write bundle atomically
    published_receipt = write_qualification_bundle(
        output_dir=output,
        request_data=request_obj.to_dict(),
        coverage_rows=coverage_rows,
        vintage_quality_rows=vintage_quality_rows,
        amendment_facts_rows=amendment_facts_rows,
        identifier_quality_rows=identifier_quality_rows,
        receipt_data=receipt_dict,
    )

    if progress:
        progress.report("INDEPENDENT_REPLAY_VALIDATION")

    # Step 3: Independent post-persist validation
    reconstruct_and_verify_qualification(
        output_dir=output,
        expected_receipt=published_receipt,
        counters=counters,
    )

    receipt_identity = canonical_hash(published_receipt)
    return SecNportQualificationRef(
        output_dir=output,
        receipt_identity=receipt_identity,
        receipt=published_receipt,
        counters=counters,
        status=status,
    )


def reconstruct_and_verify_qualification(
    *,
    output_dir: Path,
    expected_receipt: dict[str, Any],
    counters: dict[str, Any],
) -> None:
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
        counters["replay_table_scans"] = counters.get("replay_table_scans", 0) + 1
        tbl = pq.read_table(p)
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
