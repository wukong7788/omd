"""Offline deterministic tests for the incremental SEC financials sync engine."""

from __future__ import annotations

import io
import json
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec._statement_parser import SecStatementParseError, SecUnitEvidenceError
from ohmydata.providers.sec.edgartools_adapter import SecFinancialsClient
from ohmydata.providers.sec.errors import (
    CoverageError,
    PermanentProviderError,
    ResourceLimitError,
    SchemaMismatchError,
    TransientProviderError,
)
from ohmydata.providers.sec.event_discovery import SecDiscoveryCursor, SecFilingDiscoveryEvent
from ohmydata.providers.sec.financials import SecCompanyFinancialVintage, SecStatementRow
from ohmydata.providers.sec.financials_sync import (
    SEC_DISCOVERY_CURSOR_SCHEMA,
    SEC_FINANCIALS_SYNC_RECEIPT_SCHEMA,
    SecCursorStore,
    SecRateLimiter,
    SecSyncOutcomeStatus,
    default_write_financials_incremental_batch,
    load_cik_universe,
    make_edgartools_filing_fetcher,
    normalize_cik,
    sync_sec_financials_incremental,
    validate_default_financials_incremental_batch,
)
from ohmydata.providers.sec.http import SecHttpClient


def _submission_rows(
    entries: list[tuple[str, str, str, str]],
    *,
    report_date: str = "2024-03-31",
) -> dict[str, list[Any]]:
    """Build SEC submissions column arrays from (accession, form, filing_date, acceptance_iso)."""
    n = len(entries)
    return {
        "accessionNumber": [e[0] for e in entries],
        "form": [e[1] for e in entries],
        "filingDate": [e[2] for e in entries],
        "reportDate": [report_date] * n,
        "primaryDocument": [f"{e[0]}.htm" for e in entries],
        "acceptanceDateTime": [e[3] for e in entries],
        "items": [None] * n,
    }


def _root_payload(
    cik: str,
    recent_entries: list[tuple[str, str, str, str]],
    *,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "cik": cik.zfill(10),
        "filings": {
            "recent": _submission_rows(recent_entries),
            "files": files if files is not None else [],
        },
    }


class FakeSecHttpClient(SecHttpClient):
    """Offline SEC HTTP client returning synthetic payloads or exceptions by URL."""

    def __init__(
        self,
        pages: dict[str, Any],
        *,
        user_agent: str = "Synthetic Test contact@example.invalid",
    ) -> None:
        super().__init__(user_agent)
        self.pages = pages
        self.calls: list[str] = []

    def open(self, url: str, *, accept: str, max_bytes: int | None = None, **kwargs: Any) -> Any:
        self.calls.append(url)
        value = self.pages[url]
        if callable(value):
            value = value(url)
        if isinstance(value, Exception):
            raise value
        raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        return SimpleNamespace(url=url, body=io.BytesIO(raw), headers=Message())


def _make_vintage(
    cik: str,
    event: SecFilingDiscoveryEvent,
    *,
    symbol: str = "TEST",
    revenue: str = "1000000",
    currency_unit: str = "USD",
) -> SecCompanyFinancialVintage:
    return SecCompanyFinancialVintage(
        symbol=symbol,
        cik=cik.zfill(10),
        company_name=f"Synthetic Issuer {cik}",
        form=event.form,
        accession_number=event.accession,
        filing_date=date.fromisoformat(event.filing_date),
        period_end=date.fromisoformat(event.report_date) if event.report_date else None,
        accepted_at=event.acceptance_at,
        is_amendment=event.form.endswith("/A"),
        rows=(
            SecStatementRow(
                statement_type="income_statement",
                standard_concept="Revenue",
                concept="us-gaap:Revenues",
                label="Revenue",
                value=Decimal(revenue),
                value_native=revenue,
                unit=currency_unit,
                period_start=date(2024, 1, 1),
                period_end=date(2024, 3, 31),
                period_type="duration",
            ),
        ),
    )


def _fake_clock_and_sleep(start: float = 100.0):
    now = [start]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    return now, sleeps, clock, sleep


# ---------------------------------------------------------------------------
# 1. CIK Universe Loader
# ---------------------------------------------------------------------------


def test_load_cik_universe_txt_json_csv_and_dedup(tmp_path: Path) -> None:
    txt = tmp_path / "universe.txt"
    txt.write_text(
        "# comment line\n\n  320193  # Apple\n0000789019\n0000320193\n1046179\n",
        encoding="utf-8",
    )
    assert load_cik_universe(txt) == ("0000320193", "0000789019", "0001046179")

    json_list = tmp_path / "list.json"
    json_list.write_text(json.dumps(["789019", 320193, "0000789019"]), encoding="utf-8")
    assert load_cik_universe(json_list) == ("0000789019", "0000320193")

    json_obj = tmp_path / "obj.json"
    json_obj.write_text(
        json.dumps({"ciks": [{"cik": "320193", "symbol": "AAPL"}, {"cik": 789019}]}),
        encoding="utf-8",
    )
    assert load_cik_universe(json_obj) == ("0000320193", "0000789019")

    json_dicts = tmp_path / "dicts.json"
    json_dicts.write_text(
        json.dumps([{"cik": "0001046179"}, {"cik": "320193"}]),
        encoding="utf-8",
    )
    assert load_cik_universe(json_dicts) == ("0001046179", "0000320193")

    csv_file = tmp_path / "universe.csv"
    csv_file.write_text(
        "\ufeffsymbol,CIK,name\nAAPL,320193,Apple\n,,\nMSFT,0000789019,Microsoft\nAAPL,0000320193,Dup\n",
        encoding="utf-8",
    )
    assert load_cik_universe(csv_file) == ("0000320193", "0000789019")


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("empty.txt", "# only comments\n   \n"),
        ("bad.txt", "320193\nAAPL\n"),
        ("zero.txt", "0000000000\n"),
        ("too_long.txt", "12345678901\n"),
        ("unicode_digits.txt", "①②③\n"),
        ("empty_list.json", "[]"),
        ("empty_obj.json", '{"ciks": []}'),
        ("wrong_obj.json", '{"symbols": ["AAPL"]}'),
        ("missing_cik_key.json", '[{"symbol": "AAPL"}]'),
        ("bool_in_json.json", "[true]"),
        ("bad_json.json", "{not-json"),
        ("empty.csv", ""),
        ("header_only.csv", "symbol,cik\n"),
        ("no_cik_col.csv", "symbol,name\nAAPL,Apple\n"),
        ("bad_row.csv", "symbol,cik\nAAPL,abc\n"),
        ("missing_cell.csv", "symbol,cik\nAAPL,\n"),
        ("unsupported.xml", "<ciks/>"),
    ],
)
def test_load_cik_universe_rejects_malformed_or_empty(
    tmp_path: Path, filename: str, content: str
) -> None:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_cik_universe(p)


def test_load_cik_universe_missing_file_and_normalize_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_cik_universe(tmp_path / "missing.txt")
    for bad in ("", "0", "0000000000", "12345678901", "AAPL", True, False, -5, 0, None, 12.3):
        with pytest.raises(ValueError):
            normalize_cik(bad)


# ---------------------------------------------------------------------------
# 2. Unified Injectable Rate Limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_deterministic_throttling_and_http_hook() -> None:
    now, sleeps, clock, sleep = _fake_clock_and_sleep(start=10.0)
    limiter = SecRateLimiter(5.0, clock=clock, sleep=sleep)

    assert limiter.acquire() == 0.0
    assert sleeps == []

    now[0] += 0.05  # 0.05s elapsed, min_interval is 0.2s -> must sleep 0.15s
    waited = limiter.acquire()
    assert waited == pytest.approx(0.15)
    assert sleeps == [pytest.approx(0.15)]
    assert now[0] == pytest.approx(10.2)

    # Immediate next call -> full 0.2s sleep
    assert limiter() == pytest.approx(0.2)
    assert now[0] == pytest.approx(10.4)

    # Idle period longer than min_interval -> no sleep
    now[0] = 20.0
    result = limiter.call(lambda x: x * 2, 21)
    assert result == 42
    assert len(sleeps) == 2

    wrapped = limiter.wrap(lambda a, b: a + b)
    assert wrapped(2, 3) == 5
    assert limiter.acquired_count == 5
    assert limiter.total_sleep_seconds == pytest.approx(0.55)


def test_rate_limiter_validation_and_thread_safety() -> None:
    for invalid in (0, -1.0, 10.01, float("inf"), float("nan"), True, False):
        with pytest.raises(ValueError):
            SecRateLimiter(invalid)

    lock = threading.Lock()
    sleeps: list[float] = []

    def safe_sleep(dt: float) -> None:
        with lock:
            sleeps.append(dt)

    limiter = SecRateLimiter(10.0, clock=lambda: 100.0, sleep=safe_sleep)
    barrier = threading.Barrier(6)
    delays: list[float] = []

    def worker() -> None:
        barrier.wait()
        d = limiter.acquire()
        with lock:
            delays.append(d)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert limiter.acquired_count == 6
    assert sorted(round(d, 4) for d in delays) == [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
    assert len(sleeps) == 5


# ---------------------------------------------------------------------------
# 3. Persistent Cursor Store
# ---------------------------------------------------------------------------


def test_cursor_store_atomic_roundtrip_and_fail_closed_corruption(tmp_path: Path) -> None:
    store = SecCursorStore(tmp_path)
    cik = "320193"
    assert store.load(cik) is None
    assert store.list_ciks() == ()

    c1 = SecDiscoveryCursor(datetime(2024, 5, 1, 12, 0, tzinfo=UTC), "0000320193-24-000001")
    t1 = datetime(2024, 5, 2, 0, 0, tzinfo=UTC)
    rec = store.save(
        cik,
        c1,
        updated_at=t1,
        last_batch_id="run1-cik-0000320193",
        processed_accessions=("0000320193-24-000001",),
    )
    assert rec.cik == "0000320193"
    cursor_file = tmp_path / "cursors" / "cik=0000320193.json"
    assert cursor_file.is_file()

    raw = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SEC_DISCOVERY_CURSOR_SCHEMA
    assert raw["cik"] == "0000320193"
    assert raw["acceptance_at"] == "2024-05-01T12:00:00Z"
    assert raw["accession_number"] == "0000320193-24-000001"
    assert raw["updated_at"] == "2024-05-02T00:00:00Z"
    assert raw["last_batch_id"] == "run1-cik-0000320193"

    loaded = store.load_record("0000320193")
    assert loaded == rec
    assert store.load("320193") == c1
    assert store.list_ciks() == ("0000320193",)
    assert list((tmp_path / "cursors").glob(".*")) == []

    # Corrupted JSON
    cursor_file.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(SchemaMismatchError):
        store.load(cik)

    # Wrong CIK inside file
    bad_cik = dict(raw, cik="0000789019")
    cursor_file.write_text(json.dumps(bad_cik), encoding="utf-8")
    with pytest.raises(SchemaMismatchError, match="CIK mismatch"):
        store.load(cik)

    # Wrong schema version
    bad_schema = dict(raw, schema_version="sec-discovery-cursor-v99")
    cursor_file.write_text(json.dumps(bad_schema), encoding="utf-8")
    with pytest.raises(SchemaMismatchError):
        store.load(cik)

    # Extra unknown field
    bad_extra = dict(raw, unexpected="x")
    cursor_file.write_text(json.dumps(bad_extra), encoding="utf-8")
    with pytest.raises(SchemaMismatchError):
        store.load(cik)


# ---------------------------------------------------------------------------
# 4. Built-in Immutable Incremental Batch Writer & Validator
# ---------------------------------------------------------------------------


def test_default_incremental_batch_writer_immutability_and_tamper_detection(
    tmp_path: Path,
) -> None:
    event = SecFilingDiscoveryEvent(
        key="a" * 64,
        cik="320193",
        accession="0000320193-24-000001",
        kind="SEC_FILING_ACCEPTED",  # type: ignore[arg-type]
        form="10-Q",
        filing_date="2024-05-01",
        report_date="2024-03-31",
        acceptance_native="2024-05-01T12:00:00Z",
        acceptance_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
        primary_document="doc.htm",
        items=None,
        metadata_digest="b" * 64,
        source_observation_identities=("c" * 64,),
    )
    v1 = _make_vintage("0000320193", event, symbol="", revenue="100")
    v2 = _make_vintage("0000320193", event, symbol="", revenue="200")

    m1 = default_write_financials_incremental_batch(
        tmp_path, "batch-001", [v1], metadata={"run_id": "r1"}
    )
    assert m1["partition_key"] == "batch"
    assert m1["ciks"] == ["0000320193"]
    assert m1["symbols"] == []
    assert m1["vintage_count"] == 1
    validate_default_financials_incremental_batch(tmp_path / "batches" / "batch-001")

    # Idempotent rewrite with identical vintages + metadata
    m1_again = default_write_financials_incremental_batch(
        tmp_path, "batch-001", [v1], metadata={"run_id": "r1"}
    )
    assert m1_again == m1

    # Differing rewrite fails closed
    with pytest.raises(ValueError, match="collision"):
        default_write_financials_incremental_batch(
            tmp_path, "batch-001", [v2], metadata={"run_id": "r1"}
        )

    # Unsafe batch_id rejected
    with pytest.raises(ValueError):
        default_write_financials_incremental_batch(tmp_path, "../escape", [v1])

    # Tampered parquet detected by validator
    pq_file = tmp_path / "batches" / "batch-001" / "company_financial_vintages.parquet"
    pq_file.write_bytes(pq_file.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="integrity mismatch"):
        validate_default_financials_incremental_batch(tmp_path / "batches" / "batch-001")


# ---------------------------------------------------------------------------
# 5. End-to-End Incremental Sync, Reconcile Fallback, Foreign Forms, Resume
# ---------------------------------------------------------------------------


def test_incremental_sync_and_automatic_reconcile_fallback(tmp_path: Path) -> None:
    cik = "0000000001"
    root_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    hist_name = f"CIK{cik}-submissions-001.json"
    hist_url = f"https://data.sec.gov/submissions/{hist_name}"

    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"

    # Seed prior cursor at 2024-05-01T12:00:00Z
    cursor_store = SecCursorStore(sync_root)
    prior_cursor = SecDiscoveryCursor(
        datetime(2024, 5, 1, 12, 0, tzinfo=UTC), "0000000001-24-000001"
    )
    cursor_store.save(
        cik,
        prior_cursor,
        updated_at=datetime(2024, 5, 1, 13, 0, tzinfo=UTC),
        last_batch_id="seed-batch",
        processed_accessions=("0000000001-24-000001",),
    )

    # Case 1: Root recent rows do NOT cover full overlap window (oldest recent row is 2024-05-05,
    # while overlap window starts at 2024-04-30T12:00Z) AND a missed 10-Q lives in history page!
    pages = {
        root_url: _root_payload(
            cik,
            [
                ("0000000001-24-000003", "10-Q", "2024-05-05", "2024-05-05T14:00:00Z"),
            ],
            files=[
                {"name": hist_name, "filingFrom": "2024-04-29", "filingTo": "2024-05-03"},
            ],
        ),
        hist_url: {
            "cik": cik,
            **_submission_rows(
                [
                    ("0000000001-24-000002", "10-Q/A", "2024-05-02", "2024-05-02T10:00:00Z"),
                    ("0000000001-24-000001", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z"),
                ]
            ),
        },
    }
    client = FakeSecHttpClient(pages)
    fetched_accessions: list[tuple[str, ...]] = []

    def fetcher(req_cik: str, events: tuple[SecFilingDiscoveryEvent, ...]):
        fetched_accessions.append(tuple(e.accession for e in events))
        return [_make_vintage(req_cik, e) for e in events]

    _now, sleeps, mono_clock, mono_sleep = _fake_clock_and_sleep(start=1.0)
    limiter = SecRateLimiter(5.0, clock=mono_clock, sleep=mono_sleep)

    receipt = sync_sec_financials_incremental(
        sync_root,
        [cik],
        client,
        store,
        fetcher,
        as_of=datetime(2024, 5, 6, 0, 0, tzinfo=UTC),
        overlap=timedelta(hours=24),
        rate_limiter=limiter,
        utc_now=lambda: datetime(2024, 5, 6, 0, 0, tzinfo=UTC),
    )

    assert receipt.schema_version == SEC_FINANCIALS_SYNC_RECEIPT_SCHEMA
    assert receipt.synced == 1
    assert receipt.empty == 0
    assert receipt.reconciled_ciks == (cik,)
    assert receipt.failed_ciks == ()
    assert receipt.outcomes[0].reconciled is True
    assert receipt.outcomes[0].discovery_mode == "RECONCILE"
    # Already-processed 0000000001-24-000001 skipped; BOTH history 000002 and root 000003 synced!
    assert fetched_accessions == [("0000000001-24-000002", "0000000001-24-000003")]
    assert receipt.outcomes[0].vintage_count == 2
    assert hist_url in client.calls

    # Rate limiter throttled deterministically (3 slots: inc discovery, reconcile, fetcher)
    assert limiter.acquired_count == 3
    assert len(sleeps) == 2

    # Cursor advanced to newest acceptance
    new_rec = cursor_store.load_record(cik)
    assert new_rec is not None
    assert new_rec.cursor == SecDiscoveryCursor(
        datetime(2024, 5, 5, 14, 0, tzinfo=UTC), "0000000001-24-000003"
    )
    assert new_rec.last_batch_id == receipt.outcomes[0].batch_id
    assert new_rec.processed_accessions == (
        "0000000001-24-000001",
        "0000000001-24-000002",
        "0000000001-24-000003",
    )

    # Case 2: Next run where root recent rows cover the overlap window completely (no history files)
    pages_complete = {
        root_url: _root_payload(
            cik,
            [
                ("0000000001-24-000003", "10-Q", "2024-05-05", "2024-05-05T14:00:00Z"),
                ("0000000001-24-000001", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z"),
            ],
            files=[],
        )
    }
    client2 = FakeSecHttpClient(pages_complete)
    receipt2 = sync_sec_financials_incremental(
        sync_root,
        [cik],
        client2,
        store,
        fetcher,
        as_of=datetime(2024, 5, 7, 0, 0, tzinfo=UTC),
        overlap=timedelta(hours=24),
        utc_now=lambda: datetime(2024, 5, 7, 0, 0, tzinfo=UTC),
    )
    assert receipt2.empty == 1
    assert receipt2.synced == 0
    assert receipt2.reconciled_ciks == ()
    assert receipt2.outcomes[0].discovery_mode == "INCREMENTAL"
    assert receipt2.outcomes[0].reconciled is False
    assert client2.calls == [root_url]
    # Fetcher was NOT called again
    assert len(fetched_accessions) == 1


def test_foreign_forms_20f_6k_and_overlap_late_arrival(tmp_path: Path) -> None:
    cik = "0001046179"
    root_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"

    # Run 1: initial sync discovers 20-F and 6-K using default forms
    pages1 = {
        root_url: _root_payload(
            cik,
            [
                ("0001046179-24-000010", "6-K", "2024-05-01", "2024-05-01T14:00:00Z"),
                ("0001046179-24-000005", "20-F", "2024-04-18", "2024-04-18T10:00:00Z"),
            ],
        )
    }
    client1 = FakeSecHttpClient(pages1)
    fetched: list[list[str]] = []

    def fetcher(req_cik: str, events: tuple[SecFilingDiscoveryEvent, ...]):
        fetched.append([f"{e.form}:{e.accession}" for e in events])
        return [_make_vintage(req_cik, e, symbol="TSM", currency_unit="TWD") for e in events]

    r1 = sync_sec_financials_incremental(
        sync_root,
        [cik],
        client1,
        store,
        fetcher,
        as_of=datetime(2024, 5, 2, 0, 0, tzinfo=UTC),
        utc_now=lambda: datetime(2024, 5, 2, 0, 0, tzinfo=UTC),
    )
    assert r1.synced == 1
    assert fetched == [["20-F:0001046179-24-000005", "6-K:0001046179-24-000010"]]

    # Run 2: A late-indexed 6-K appears at 2024-05-01T08:00:00Z (within 24h overlap of 14:00Z!)
    pages2 = {
        root_url: _root_payload(
            cik,
            [
                ("0001046179-24-000010", "6-K", "2024-05-01", "2024-05-01T14:00:00Z"),
                ("0001046179-24-000009", "6-K", "2024-05-01", "2024-05-01T08:00:00Z"),
                ("0001046179-24-000005", "20-F", "2024-04-18", "2024-04-18T10:00:00Z"),
            ],
        )
    }
    client2 = FakeSecHttpClient(pages2)
    r2 = sync_sec_financials_incremental(
        sync_root,
        [cik],
        client2,
        store,
        fetcher,
        as_of=datetime(2024, 5, 2, 6, 0, tzinfo=UTC),
        overlap=timedelta(hours=24),
        utc_now=lambda: datetime(2024, 5, 2, 6, 0, tzinfo=UTC),
    )
    assert r2.synced == 1
    # Only the late-arriving 000009 was fetched; 000010 was NOT duplicated!
    assert fetched[-1] == ["6-K:0001046179-24-000009"]


def test_resume_semantics_mixed_outcomes_run1_and_run2(tmp_path: Path) -> None:
    """Explicit resume test:
    Run 1: 1 synced, 1 empty, 1 transient failure, 1 permanent failure.
    Run 2: same universe -> synced CIK sees empty/no-dup, transient CIK retries & succeeds!
    """
    cik_synced = "0000000001"
    cik_empty = "0000000002"
    cik_transient = "0000000003"
    cik_permanent = "0000000004"
    universe = [cik_synced, cik_empty, cik_transient, cik_permanent]

    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"
    cursor_store = SecCursorStore(sync_root)

    # Seed an old cursor on cik_transient so we prove it retries from the OLD cursor!
    old_transient_cursor = SecDiscoveryCursor(
        datetime(2024, 4, 1, 12, 0, tzinfo=UTC), "0000000003-24-000001"
    )
    cursor_store.save(
        cik_transient,
        old_transient_cursor,
        updated_at=datetime(2024, 4, 2, 0, 0, tzinfo=UTC),
        last_batch_id="prior-batch-3",
        processed_accessions=("0000000003-24-000001",),
    )

    pages_run1 = {
        f"https://data.sec.gov/submissions/CIK{cik_synced}.json": _root_payload(
            cik_synced,
            [("0000000001-24-000010", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z")],
        ),
        f"https://data.sec.gov/submissions/CIK{cik_empty}.json": _root_payload(
            cik_empty,
            [("0000000002-24-000010", "8-K", "2024-05-01", "2024-05-01T12:00:00Z")],
        ),
        f"https://data.sec.gov/submissions/CIK{cik_transient}.json": TransientProviderError(
            "SEC 503 temporary outage"
        ),
        f"https://data.sec.gov/submissions/CIK{cik_permanent}.json": _root_payload(
            cik_permanent,
            [("0000000004-24-000010", "10-K", "2024-05-01", "2024-05-01T12:00:00Z")],
        ),
    }

    fetched_run1: list[str] = []

    def fetcher_run1(cik: str, events: tuple[SecFilingDiscoveryEvent, ...]):
        if cik == cik_permanent:
            raise SecStatementParseError("malformed XBRL statement table")
        fetched_run1.append(cik)
        return [_make_vintage(cik, e) for e in events]

    t1 = datetime(2024, 5, 2, 0, 0, tzinfo=UTC)
    r1 = sync_sec_financials_incremental(
        sync_root,
        universe,
        FakeSecHttpClient(pages_run1),
        store,
        fetcher_run1,
        as_of=t1,
        utc_now=lambda: t1,
    )

    assert r1.synced == 1
    assert r1.empty == 1
    assert r1.transient_failure == 1
    assert r1.permanent_failure == 1
    assert r1.failed_ciks == (cik_transient, cik_permanent)
    assert r1.transient_failed_ciks == (cik_transient,)
    assert r1.permanent_failed_ciks == (cik_permanent,)
    assert fetched_run1 == [cik_synced]
    assert r1.receipt_path.is_file()

    by_cik_r1 = {o.cik: o for o in r1.outcomes}
    assert by_cik_r1[cik_synced].status is SecSyncOutcomeStatus.SYNCED
    assert by_cik_r1[cik_empty].status is SecSyncOutcomeStatus.EMPTY
    assert by_cik_r1[cik_transient].status is SecSyncOutcomeStatus.TRANSIENT_FAILURE
    assert by_cik_r1[cik_permanent].status is SecSyncOutcomeStatus.PERMANENT_FAILURE

    # CRITICAL: Failed CIK cursors were NOT advanced!
    assert cursor_store.load(cik_transient) == old_transient_cursor
    assert cursor_store.load(cik_permanent) is None
    assert cursor_store.load(cik_synced) == SecDiscoveryCursor(
        datetime(2024, 5, 1, 12, 0, tzinfo=UTC), "0000000001-24-000010"
    )
    run1_batches = sorted(p.name for p in (sync_root / "batches").iterdir())
    assert len(run1_batches) == 1

    # --- RUN 2: Same universe, transient outage resolved ---
    pages_run2 = {
        f"https://data.sec.gov/submissions/CIK{cik_synced}.json": _root_payload(
            cik_synced,
            [
                ("0000000001-24-000010", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z"),
                ("0000000001-24-000001", "10-K", "2024-03-01", "2024-03-01T12:00:00Z"),
            ],
        ),
        f"https://data.sec.gov/submissions/CIK{cik_empty}.json": _root_payload(
            cik_empty,
            [("0000000002-24-000010", "8-K", "2024-05-01", "2024-05-01T12:00:00Z")],
        ),
        f"https://data.sec.gov/submissions/CIK{cik_transient}.json": _root_payload(
            cik_transient,
            [
                ("0000000003-24-000002", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z"),
                ("0000000003-24-000001", "10-K", "2024-04-01", "2024-04-01T12:00:00Z"),
            ],
        ),
        f"https://data.sec.gov/submissions/CIK{cik_permanent}.json": _root_payload(
            cik_permanent,
            [("0000000004-24-000010", "10-K", "2024-05-01", "2024-05-01T12:00:00Z")],
        ),
    }

    fetched_run2: list[tuple[str, tuple[str, ...]]] = []

    def fetcher_run2(cik: str, events: tuple[SecFilingDiscoveryEvent, ...]):
        if cik == cik_permanent:
            raise SecStatementParseError("malformed XBRL statement table")
        fetched_run2.append((cik, tuple(e.accession for e in events)))
        return [_make_vintage(cik, e) for e in events]

    t2 = datetime(2024, 5, 2, 6, 0, tzinfo=UTC)
    r2 = sync_sec_financials_incremental(
        sync_root,
        universe,
        FakeSecHttpClient(pages_run2),
        store,
        fetcher_run2,
        as_of=t2,
        utc_now=lambda: t2,
    )

    assert r2.synced == 1
    assert r2.empty == 2
    assert r2.transient_failure == 0
    assert r2.permanent_failure == 1
    assert r2.failed_ciks == (cik_permanent,)
    assert r2.receipt_path != r1.receipt_path
    assert r1.receipt_path.is_file() and r2.receipt_path.is_file()

    by_cik_r2 = {o.cik: o for o in r2.outcomes}
    # CIK 1 already synced -> EMPTY, zero duplicate fetch/batch!
    assert by_cik_r2[cik_synced].status is SecSyncOutcomeStatus.EMPTY
    assert by_cik_r2[cik_synced].batch_id is None
    # CIK 3 retried from old cursor -> SYNCED only the new accession 000002!
    assert by_cik_r2[cik_transient].status is SecSyncOutcomeStatus.SYNCED
    assert fetched_run2 == [(cik_transient, ("0000000003-24-000002",))]
    assert cursor_store.load(cik_transient) == SecDiscoveryCursor(
        datetime(2024, 5, 1, 12, 0, tzinfo=UTC), "0000000003-24-000002"
    )
    # CIK 4 still failed, cursor still None
    assert by_cik_r2[cik_permanent].status is SecSyncOutcomeStatus.PERMANENT_FAILURE
    assert cursor_store.load(cik_permanent) is None

    # Prior batch from Run 1 untouched alongside Run 2's new batch
    all_batches = sorted(p.name for p in (sync_root / "batches").iterdir())
    assert len(all_batches) == 2
    assert run1_batches[0] in all_batches


def test_cursor_never_advances_on_batch_write_validation_or_coverage_failure(
    tmp_path: Path,
) -> None:
    cik1 = "0000000011"
    cik2 = "0000000012"
    cik3 = "0000000013"
    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"
    cursor_store = SecCursorStore(sync_root)

    pages = {
        f"https://data.sec.gov/submissions/CIK{c}.json": _root_payload(
            c, [(f"{c}-24-000001", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z")]
        )
        for c in (cik1, cik2, cik3)
    }

    def fetcher(cik: str, events: tuple[SecFilingDiscoveryEvent, ...]):
        if cik == cik3:
            return ()  # Empty vintage list for discovered event -> CoverageError!
        return [_make_vintage(cik, e) for e in events]

    def failing_writer(root: Any, batch_id: str, vintages: Any, *, metadata: Any = None):
        if cik1 in batch_id:
            raise OSError("disk full")
        return default_write_financials_incremental_batch(
            root, batch_id, vintages, metadata=metadata
        )

    def failing_validator(batch_dir: Any):
        if cik2 in str(batch_dir):
            raise ValueError("post-write hash validation failed")
        return validate_default_financials_incremental_batch(batch_dir)

    t = datetime(2024, 5, 2, 0, 0, tzinfo=UTC)
    receipt = sync_sec_financials_incremental(
        sync_root,
        [cik1, cik2, cik3],
        FakeSecHttpClient(pages),
        store,
        fetcher,
        as_of=t,
        utc_now=lambda: t,
        batch_writer=failing_writer,
        batch_validator=failing_validator,
    )

    assert receipt.synced == 0
    assert receipt.permanent_failure == 3
    for cik in (cik1, cik2, cik3):
        assert cursor_store.load(cik) is None


def test_error_classification_and_secret_redaction_in_receipts(tmp_path: Path) -> None:
    secret_ua = "PrivateFundResearch secret.analyst@private-hedge.invalid"
    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"

    errors_by_cik: dict[str, Exception] = {
        "0000000101": TimeoutError(f"timed out sending User-Agent: {secret_ua}"),
        "0000000102": ConnectionError("reset by peer"),
        "0000000103": PermanentProviderError(
            f"SEC HTTP status 404 with {secret_ua} token=supersecret123"
        ),
        "0000000104": SecUnitEvidenceError("missing unit table"),
        "0000000105": ResourceLimitError("max bytes exceeded"),
        "0000000106": CoverageError("missing history page"),
        "0000000107": SchemaMismatchError("bad json"),
    }
    pages = {
        f"https://data.sec.gov/submissions/CIK{cik}.json": exc for cik, exc in errors_by_cik.items()
    }
    client = FakeSecHttpClient(pages, user_agent=secret_ua)
    t = datetime(2024, 5, 2, 0, 0, tzinfo=UTC)

    receipt = sync_sec_financials_incremental(
        sync_root,
        list(errors_by_cik),
        client,
        store,
        lambda c, ev: [],
        as_of=t,
        utc_now=lambda: t,
        run_id="redaction-check-1",
    )

    assert receipt.transient_failure == 2
    assert receipt.permanent_failure == 5
    assert receipt.transient_failed_ciks == ("0000000101", "0000000102")
    assert receipt.permanent_failed_ciks == (
        "0000000103",
        "0000000104",
        "0000000105",
        "0000000106",
        "0000000107",
    )

    persisted_text = receipt.receipt_path.read_text(encoding="utf-8")
    assert secret_ua not in persisted_text
    assert "secret.analyst@private-hedge.invalid" not in persisted_text
    assert "supersecret123" not in persisted_text
    assert "[REDACTED]" in persisted_text

    # Reusing the exact same explicit run_id fails closed (immutable receipt!)
    with pytest.raises(SchemaMismatchError, match="already exists"):
        sync_sec_financials_incremental(
            sync_root,
            ["0000000101"],
            client,
            store,
            lambda c, ev: [],
            as_of=t,
            utc_now=lambda: t,
            run_id="redaction-check-1",
        )


def test_corrupted_cursor_isolates_single_cik_and_auto_run_id_avoids_collision(
    tmp_path: Path,
) -> None:
    cik_bad = "0000000201"
    cik_good = "0000000202"
    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"

    bad_cursor_path = sync_root / "cursors" / f"cik={cik_bad}.json"
    bad_cursor_path.parent.mkdir(parents=True, exist_ok=True)
    bad_cursor_path.write_text("{corrupt-json", encoding="utf-8")

    pages = {
        f"https://data.sec.gov/submissions/CIK{cik_good}.json": _root_payload(
            cik_good,
            [("0000000202-24-000001", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z")],
        )
    }
    fixed_t = datetime(2024, 5, 2, 0, 0, tzinfo=UTC)
    r1 = sync_sec_financials_incremental(
        sync_root,
        [cik_bad, cik_good],
        FakeSecHttpClient(pages),
        store,
        lambda c, ev: [_make_vintage(c, e) for e in ev],
        as_of=fixed_t,
        utc_now=lambda: fixed_t,
    )
    assert r1.permanent_failure == 1
    assert r1.synced == 1
    assert r1.failed_ciks == (cik_bad,)

    # Second call with identical fixed clock auto-increments run_id without overwriting receipt
    r2 = sync_sec_financials_incremental(
        sync_root,
        [cik_bad, cik_good],
        FakeSecHttpClient(pages),
        store,
        lambda c, ev: [_make_vintage(c, e) for e in ev],
        as_of=fixed_t,
        utc_now=lambda: fixed_t,
    )
    assert r2.run_id != r1.run_id
    assert r2.empty == 1
    assert r2.permanent_failure == 1


def test_make_edgartools_filing_fetcher_adapter_with_custom_runner() -> None:
    event = SecFilingDiscoveryEvent(
        key="a" * 64,
        cik="320193",
        accession="0000320193-24-000001",
        kind="SEC_FILING_ACCEPTED",  # type: ignore[arg-type]
        form="10-Q",
        filing_date="2024-05-01",
        report_date="2024-03-31",
        acceptance_native="2024-05-01T12:00:00Z",
        acceptance_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
        primary_document="aapl.htm",
        items=None,
        metadata_digest="b" * 64,
        source_observation_identities=("c" * 64,),
    )
    expected = _make_vintage("0000320193", event, symbol="AAPL")
    extra_event = SecFilingDiscoveryEvent(
        key="d" * 64,
        cik="320193",
        accession="0000320193-24-000099",
        kind="SEC_FILING_ACCEPTED",  # type: ignore[arg-type]
        form="10-Q",
        filing_date="2024-05-01",
        report_date="2024-03-31",
        acceptance_native="2024-05-01T12:00:00Z",
        acceptance_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
        primary_document="other.htm",
        items=None,
        metadata_digest="e" * 64,
        source_observation_identities=("c" * 64,),
    )
    extra = _make_vintage("0000320193", extra_event, symbol="AAPL")

    fin_client = SecFinancialsClient(
        "Synthetic Test contact@example.invalid",
        runner=lambda req: [expected, extra],
    )
    fetcher = make_edgartools_filing_fetcher(
        fin_client,
        parser_version="sec-live-financial-parser-v1-edgartools-6.4.0",
        symbol_resolver=lambda cik: "AAPL",
    )
    result = fetcher("0000320193", (event,))
    assert result == (expected,)


def test_foreign_only_forms_exclude_probe_10k_and_share_real_http_client_limiter(
    tmp_path: Path,
) -> None:
    cik = "0001046179"
    root_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    payload = _root_payload(
        cik,
        [
            ("0001046179-24-000020", "10-K", "2024-05-01", "2024-05-01T18:00:00Z"),
            ("0001046179-24-000015", "20-F", "2024-04-20", "2024-04-20T12:00:00Z"),
        ],
    )
    raw_bytes = json.dumps(payload).encode("utf-8")

    class _FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.headers = Message()
            self.headers["Content-Type"] = "application/json"
            self.headers["Content-Length"] = str(len(raw_bytes))
            self._buf = io.BytesIO(raw_bytes)

        def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        def close(self) -> None:
            self._buf.close()

    class _FakeOpener:
        def open(self, req: Any, timeout: int = 3600) -> _FakeResponse:
            assert req.full_url.endswith(f"/CIK{cik}.json")
            return _FakeResponse()

    _now, sleeps, clock, sleep = _fake_clock_and_sleep(start=50.0)
    limiter = SecRateLimiter(5.0, clock=clock, sleep=sleep)
    real_http_client = SecHttpClient(
        "Synthetic Test contact@example.invalid",
        opener=_FakeOpener(),
        clock=clock,
        sleep=sleep,
        limiter=limiter,
    )
    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"
    fetched_forms: list[str] = []

    def fetcher(req_cik: str, events: tuple[SecFilingDiscoveryEvent, ...]):
        fetched_forms.extend(e.form for e in events)
        return [_make_vintage(req_cik, e, symbol="TSM", currency_unit="TWD") for e in events]

    t = datetime(2024, 5, 2, 0, 0, tzinfo=UTC)
    receipt = sync_sec_financials_incremental(
        sync_root,
        [cik],
        real_http_client,
        store,
        fetcher,
        forms=("20-F", "6-K"),
        as_of=t,
        rate_limiter=limiter,
        utc_now=lambda: t,
    )
    assert receipt.synced == 1
    assert fetched_forms == ["20-F"]
    # Cursor must reflect 20-F, NOT the excluded 10-K probe form!
    assert SecCursorStore(sync_root).load(cik) == SecDiscoveryCursor(
        datetime(2024, 4, 20, 12, 0, tzinfo=UTC), "0001046179-24-000015"
    )
    # 1 slot in SecHttpClient.open + 1 slot before filing_fetcher (no double-acquire!)
    assert limiter.acquired_count == 2
    assert len(sleeps) == 1
    assert root_url.endswith(f"/CIK{cik}.json")


def test_8k_item_202_not_double_counted_and_duplicate_or_extra_vintages_rejected(
    tmp_path: Path,
) -> None:
    cik1 = "0000000301"
    cik2 = "0000000302"
    cik3 = "0000000303"
    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"

    def _root_with_items(cik: str, acc: str) -> dict[str, Any]:
        rows = _submission_rows([(acc, "8-K", "2024-05-01", "2024-05-01T12:00:00Z")])
        rows["items"] = ["2.02,9.01"]
        return {"cik": cik, "filings": {"recent": rows, "files": []}}

    pages = {
        f"https://data.sec.gov/submissions/CIK{cik1}.json": _root_with_items(
            cik1, f"{cik1}-24-000001"
        ),
        f"https://data.sec.gov/submissions/CIK{cik2}.json": _root_payload(
            cik2, [(f"{cik2}-24-000001", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z")]
        ),
        f"https://data.sec.gov/submissions/CIK{cik3}.json": _root_payload(
            cik3, [(f"{cik3}-24-000001", "10-Q", "2024-05-01", "2024-05-01T12:00:00Z")]
        ),
    }
    received_counts: dict[str, int] = {}

    def fetcher(req_cik: str, events: tuple[SecFilingDiscoveryEvent, ...]):
        received_counts[req_cik] = len(events)
        if req_cik == cik1:
            return [_make_vintage(req_cik, e) for e in events]
        if req_cik == cik2:
            # Duplicate vintage for same accession -> must fail closed!
            v = _make_vintage(req_cik, events[0])
            return [v, v]
        # Unexpected extra accession -> must fail closed!
        extra_ev = SecFilingDiscoveryEvent(
            key="f" * 64,
            cik=str(int(req_cik)),
            accession=f"{req_cik}-24-999999",
            kind=events[0].kind,
            form="10-Q",
            filing_date="2024-05-01",
            report_date="2024-03-31",
            acceptance_native="2024-05-01T12:00:00Z",
            acceptance_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
            primary_document="extra.htm",
            items=None,
            metadata_digest="0" * 64,
            source_observation_identities=("1" * 64,),
        )
        return [_make_vintage(req_cik, events[0]), _make_vintage(req_cik, extra_ev)]

    t = datetime(2024, 5, 2, 0, 0, tzinfo=UTC)
    receipt = sync_sec_financials_incremental(
        sync_root,
        [cik1, cik2, cik3],
        FakeSecHttpClient(pages),
        store,
        fetcher,
        forms=("8-K", "10-Q"),
        as_of=t,
        utc_now=lambda: t,
    )
    by_cik = {o.cik: o for o in receipt.outcomes}
    assert by_cik[cik1].status is SecSyncOutcomeStatus.SYNCED
    assert by_cik[cik1].discovered_event_count == 1
    assert by_cik[cik1].new_event_count == 1
    assert received_counts[cik1] == 1

    assert by_cik[cik2].status is SecSyncOutcomeStatus.PERMANENT_FAILURE
    assert by_cik[cik2].error_type == "SchemaMismatchError"
    assert SecCursorStore(sync_root).load(cik2) is None

    assert by_cik[cik3].status is SecSyncOutcomeStatus.PERMANENT_FAILURE
    assert by_cik[cik3].error_type == "SchemaMismatchError"
    assert SecCursorStore(sync_root).load(cik3) is None


def test_initial_acceptance_lower_after_as_of_fails_fast_without_receipt(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    sync_root = tmp_path / "sync"
    client = FakeSecHttpClient({})
    with pytest.raises(ValueError, match="initial_acceptance_lower exceeds as_of"):
        sync_sec_financials_incremental(
            sync_root,
            ["0000320193"],
            client,
            store,
            lambda c, ev: [],
            as_of=datetime(2024, 5, 1, tzinfo=UTC),
            initial_acceptance_lower=datetime(2024, 6, 1, tzinfo=UTC),
            utc_now=lambda: datetime(2024, 5, 2, tzinfo=UTC),
        )
    assert client.calls == []
    assert not (sync_root / "receipts").exists()


def test_default_batch_validator_rejects_mismatched_symbols(tmp_path: Path) -> None:
    event = SecFilingDiscoveryEvent(
        key="a" * 64,
        cik="320193",
        accession="0000320193-24-000001",
        kind="SEC_FILING_ACCEPTED",  # type: ignore[arg-type]
        form="10-Q",
        filing_date="2024-05-01",
        report_date="2024-03-31",
        acceptance_native="2024-05-01T12:00:00Z",
        acceptance_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
        primary_document="doc.htm",
        items=None,
        metadata_digest="b" * 64,
        source_observation_identities=("c" * 64,),
    )
    v = _make_vintage("0000320193", event, symbol="AAPL")
    default_write_financials_incremental_batch(tmp_path, "batch-sym", [v])
    manifest_path = tmp_path / "batches" / "batch-sym" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["symbols"] = ["MSFT"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="symbols do not match"):
        validate_default_financials_incremental_batch(tmp_path / "batches" / "batch-sym")
