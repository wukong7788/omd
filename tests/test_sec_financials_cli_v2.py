"""Offline CLI and cross-track integration tests for SEC financials v2 commands."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ohmydata.cli import main
from ohmydata.core import SnapshotStore
from ohmydata.providers import sec
from ohmydata.providers.sec import (
    SEC_ADR_METADATA_SCHEMA,
    SecAdrShareMetadata,
    SecCompanyFinancialVintage,
    SecDeiSharesMetadata,
    SecFinancialsClient,
    SecFinancialsRequest,
    SecHttpClient,
    SecShareBasis,
    SecStatementRow,
    list_financials_incremental_batches,
    make_edgartools_filing_fetcher,
    normalize_sec_cik,
    sync_sec_financials_incremental,
    validate_bulk_companyfacts_dataset,
    validate_cik_financials_partition,
    validate_financials_incremental_batch,
    write_cik_financials_partition,
    write_financials_partition,
)
from ohmydata.providers.sec._financials_sync_batch import resolve_default_batch_hooks
from ohmydata.providers.sec.financials_dataset import (
    validate_financials_incremental_batch as dataset_validate_batch,
)
from ohmydata.providers.sec.financials_dataset import (
    write_financials_incremental_batch as dataset_write_batch,
)
from ohmydata.providers.sec.unit_evidence import SecFinancialUnitEvidence


def _sample_row(*, currency: str = "USD") -> SecStatementRow:
    return SecStatementRow(
        statement_type="income_statement",
        standard_concept="Revenue",
        concept="us-gaap_Revenues",
        label="Revenues",
        value=Decimal(1250000000),
        value_native="1250000000",
        unit=f"iso4217:{currency}",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        period_type="duration",
        is_point_in_time=False,
    )


def _sample_unit_evidence(cik: str, accession: str) -> SecFinancialUnitEvidence:
    return SecFinancialUnitEvidence(
        parser_version="sec-live-financial-parser-v2-edgartools-6.4.0",
        cik=str(int(cik)),
        accession_number=accession,
        instance_document="test-20250331.xml",
        instance_sha256="a" * 64,
    )


def _sample_vintage(
    *,
    symbol: str = "AAPL",
    cik: str = "0000320193",
    accession: str = "0000320193-25-000010",
    form: str = "10-Q",
    filing_date: date = date(2025, 5, 2),
    accepted_at: datetime = datetime(2025, 5, 2, 20, 15, 0, tzinfo=UTC),
    with_adr: bool = False,
    with_evidence: bool = True,
) -> SecCompanyFinancialVintage:
    adr_meta = (
        SecAdrShareMetadata(
            reporting_currency="USD",
            share_basis=SecShareBasis.UNKNOWN,
            ordinary_shares_per_adr=None,
            ratio_source=None,
            dei_shares=SecDeiSharesMetadata(
                shares_outstanding=Decimal(15000000000),
                as_of_date=date(2025, 4, 18),
                unit="shares",
                native_tag="dei:EntityCommonStockSharesOutstanding",
                context_ref="c-cover",
            ),
        )
        if with_adr
        else None
    )
    return SecCompanyFinancialVintage(
        symbol=symbol,
        cik=cik,
        company_name="Example Corp",
        form=form,
        accession_number=accession,
        filing_date=filing_date,
        period_end=date(2025, 3, 31),
        accepted_at=accepted_at,
        rows=(_sample_row(),),
        unit_evidence=_sample_unit_evidence(cik, accession) if with_evidence else None,
        adr_metadata=adr_meta,
    )


class _FakeStreamResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self.url = url
        self.headers = {"content-type": "application/json", "Content-Length": str(len(body))}
        self.body = io.BytesIO(body)


class _FakeSecHttpClient(SecHttpClient):
    """Offline SecHttpClient serving deterministic JSON responses by URL."""

    def __init__(
        self,
        user_agent: str,
        responses: dict[str, bytes],
        *,
        limiter: Any = None,
        error_on_url: dict[str, Exception] | None = None,
    ) -> None:
        super().__init__(user_agent, limiter=limiter)
        self._responses = dict(responses)
        self._error_on_url = dict(error_on_url or {})
        self.requested_urls: list[str] = []

    def open(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if self._limiter is not None:
            self._limiter()
        self.requested_urls.append(url)
        if url in self._error_on_url:
            raise self._error_on_url[url]
        if url not in self._responses:
            raise KeyError(f"unexpected URL in fake client: {url}")
        return _FakeStreamResponse(url, self._responses[url])


def _submissions_payload(
    cik: str,
    *,
    accessions: list[str],
    forms: list[str],
    filing_dates: list[str],
    report_dates: list[str],
    acceptance_datetimes: list[str],
    primary_docs: list[str],
    tickers: list[str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "cik": str(int(cik)),
            "entityType": "operating",
            "name": f"Issuer {cik}",
            "tickers": tickers if tickers is not None else [],
            "filings": {
                "recent": {
                    "accessionNumber": accessions,
                    "form": forms,
                    "filingDate": filing_dates,
                    "reportDate": report_dates,
                    "acceptanceDateTime": acceptance_datetimes,
                    "primaryDocument": primary_docs,
                },
                "files": [],
            },
        }
    ).encode("utf-8")


def _write_bulk_zips(tmp_path: Path) -> tuple[Path, Path]:
    cf_zip = tmp_path / "companyfacts.zip"
    sub_zip = tmp_path / "submissions.zip"

    cik = "0000320193"
    acc_q1 = "0000320193-24-000010"
    acc_fy = "0000320193-24-000040"

    cf_payload = {
        "cik": 320193,
        "entityName": "Example Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "units": {
                        "USD": [
                            {
                                "start": "2024-01-01",
                                "end": "2024-03-31",
                                "val": 90000000000,
                                "accn": acc_q1,
                                "fy": 2024,
                                "fp": "Q1",
                                "form": "10-Q",
                                "filed": "2024-05-03",
                                "frame": "CY2024Q1",
                            },
                            {
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "val": 390000000000,
                                "accn": acc_fy,
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-02-01",
                                "frame": "CY2024",
                            },
                        ]
                    },
                }
            }
        },
    }
    sub_payload = {
        "cik": "0000320193",
        "name": "Example Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [acc_q1, acc_fy],
                "form": ["10-Q", "10-K"],
                "filingDate": ["2024-05-03", "2025-02-01"],
                "reportDate": ["2024-03-31", "2024-12-31"],
                "acceptanceDateTime": [
                    "2024-05-03T16:30:00.000Z",
                    "2025-02-01T17:00:00.000Z",
                ],
                "primaryDocument": ["q1.htm", "fy.htm"],
            },
            "files": [],
        },
    }

    with zipfile.ZipFile(cf_zip, "w") as zf:
        zf.writestr(f"CIK{cik}.json", json.dumps(cf_payload))
    with zipfile.ZipFile(sub_zip, "w") as zf:
        zf.writestr(f"CIK{cik}.json", json.dumps(sub_payload))
    return cf_zip, sub_zip


def test_sec_package_exports_and_canonical_normalize_sec_cik() -> None:
    expected_exports = {
        "SEC_ADR_METADATA_SCHEMA",
        "BULK_COMPANYFACTS_DATASET_SCHEMA",
        "DEFAULT_SYNC_FORMS",
        "FINANCIALS_DATASET_SCHEMA_V5",
        "FPI_FINANCIAL_FORMS",
        "SUPPORTED_FINANCIAL_FORMS",
        "WRITER_PROFILE_V5",
        "SecAdrShareMetadata",
        "SecFpi6KParserRegistry",
        "Sec8KMetric",
        "SecRateLimiter",
        "build_bulk_companyfacts_dataset",
        "fetch_sec_canonical_quarters_by_cik",
        "fetch_sec_fpi_6k_quarters",
        "fetch_sec_8k_quarterly_metrics",
        "fetch_sec_8k_quarterly_batch",
        "fetch_sec_8k_metrics_for_discovery_events",
        "load_cik_universe",
        "make_edgartools_filing_fetcher",
        "normalize_sec_cik",
        "sync_sec_financials_incremental",
        "write_cik_financials_partition",
        "write_financials_incremental_batch",
    }
    assert expected_exports.issubset(set(sec.__all__))
    assert len(sec.__all__) == len(set(sec.__all__))
    assert SEC_ADR_METADATA_SCHEMA == "sec-adr-share-metadata-v1"

    assert normalize_sec_cik(320193) == "0000320193"
    assert normalize_sec_cik("320193") == "0000320193"
    assert normalize_sec_cik(" 0001046179 ") == "0001046179"
    for bad in (True, False, 0, -5, 10_000_000_000, "", "abc", "0"):
        with pytest.raises(ValueError, match="invalid CIK"):
            normalize_sec_cik(bad)


def test_financials_sync_real_integration_with_financials_dataset_v5_batch(tmp_path: Path) -> None:
    writer, validator = resolve_default_batch_hooks()
    assert writer is dataset_write_batch
    assert validator is dataset_validate_batch

    cik = "0000320193"
    acc = "0000320193-25-000010"
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    sub_body = _submissions_payload(
        cik,
        accessions=[acc],
        forms=["10-Q"],
        filing_dates=["2025-05-02"],
        report_dates=["2025-03-31"],
        acceptance_datetimes=["2025-05-02T20:15:00.000Z"],
        primary_docs=["aapl-20250331.htm"],
        tickers=["AAPL"],
    )
    http = _FakeSecHttpClient("ResearchApp/1.0 (test@example.invalid)", {url: sub_body})
    store = SnapshotStore(tmp_path / "snapshots")

    seen_requests: list[SecFinancialsRequest] = []
    vintage = _sample_vintage(cik=cik, accession=acc, with_adr=True, with_evidence=True)

    def _runner(req: SecFinancialsRequest) -> list[SecCompanyFinancialVintage]:
        seen_requests.append(req)
        return [vintage]

    fin_client = SecFinancialsClient("ResearchApp/1.0 (test@example.invalid)", runner=_runner)
    fetcher = make_edgartools_filing_fetcher(fin_client)

    receipt = sync_sec_financials_incremental(
        tmp_path,
        [cik],
        http,
        store,
        filing_fetcher=fetcher,
        financials_client=fin_client,
        forms=("10-Q",),
        as_of=datetime(2025, 5, 3, 0, 0, 0, tzinfo=UTC),
        overlap=timedelta(hours=24),
        run_id="int-roundtrip-01",
    )

    assert receipt.synced == 1
    assert receipt.failed_ciks == ()
    assert len(seen_requests) == 1
    assert seen_requests[0].ciks == (cik,)
    assert seen_requests[0].symbols == ()

    batches = list_financials_incremental_batches(tmp_path)
    assert len(batches) == 1
    manifest = validate_financials_incremental_batch(batches[0])
    assert manifest["dataset_schema"] == "sec-company-financials-v5"
    assert manifest["ciks"] == [cik]
    assert manifest["accession_numbers"] == [acc]
    assert manifest["metadata"]["run_id"] == "int-roundtrip-01"


def test_cli_sync_validate_inspect_with_ciks_and_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cik = "0001046179"
    delisted_cik = "0000999888"
    v_tsm = _sample_vintage(
        symbol="TSM",
        cik=cik,
        accession="0001046179-25-000005",
        form="20-F",
        with_adr=True,
    )
    v_delisted = _sample_vintage(
        symbol="",
        cik=delisted_cik,
        accession="0000999888-25-000001",
        form="10-K",
        with_adr=False,
    )

    captured_requests: list[SecFinancialsRequest] = []

    class _FakeFinClient:
        def __init__(self, user_agent: str, *, http_client: Any = None) -> None:
            self.user_agent = user_agent
            self.http_client = http_client

        def fetch_company_financials(
            self, req: SecFinancialsRequest
        ) -> list[SecCompanyFinancialVintage]:
            captured_requests.append(req)
            return [v_tsm, v_delisted]

    monkeypatch.setattr(
        "ohmydata.providers.sec.edgartools_adapter.SecFinancialsClient", _FakeFinClient
    )
    ua_file = tmp_path / "ua.txt"
    secret_ua = "PrivateTeam/2.0 (secret-sync@example.invalid)"
    ua_file.write_text(secret_ua + "\n", encoding="utf-8")

    out_root = tmp_path / "lake"
    code = main(
        [
            "sec",
            "financials",
            "sync",
            "--root",
            str(out_root),
            "--ciks",
            "1046179,0000999888",
            "--forms",
            "10-K,20-F",
            "--user-agent-file",
            str(ua_file),
            "--json",
            "--quiet",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert secret_ua not in captured.out
    assert secret_ua not in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "completed"
    assert payload["ciks_processed"] == 2
    assert set(payload["cik_manifests"].keys()) == {cik, delisted_cik}
    assert captured_requests[0].ciks == (delisted_cik, cik) or captured_requests[0].ciks == (
        cik,
        delisted_cik,
    )

    # Also write a symbol partition to verify mixed validate + inspect
    v_aapl = _sample_vintage(symbol="AAPL", cik="0000320193", accession="0000320193-25-000099")
    write_financials_partition(out_root, "AAPL", [v_aapl])

    # Validate all partitions (2 cik= + 1 symbol=)
    assert main(["sec", "financials", "validate", "--root", str(out_root), "--json"]) == 0
    val_all = json.loads(capsys.readouterr().out)
    assert val_all["partitions_verified"] == 3

    # Validate filtered by --ciks
    assert (
        main(
            [
                "sec",
                "financials",
                "validate",
                "--root",
                str(out_root),
                "--ciks",
                "1046179",
                "--json",
            ]
        )
        == 0
    )
    val_cik = json.loads(capsys.readouterr().out)
    assert val_cik["partitions_verified"] == 1

    # Validate missing requested CIK fails explicitly
    assert (
        main(
            [
                "sec",
                "financials",
                "validate",
                "--root",
                str(out_root),
                "--ciks",
                "0000000001",
            ]
        )
        == 2
    )
    assert "partition not found for cik" in capsys.readouterr().err

    # Inspect by --cik with --rows
    assert (
        main(
            [
                "sec",
                "financials",
                "inspect",
                "--root",
                str(out_root),
                "--cik",
                "1046179",
                "--rows",
                "--json",
            ]
        )
        == 0
    )
    insp = json.loads(capsys.readouterr().out)
    assert insp["cik"] == cik
    assert insp["partition_key"] == "cik"
    assert insp["vintage_count"] == 1
    assert len(insp["rows"]) == 1

    # Inspect with both --symbol and --cik rejected
    assert (
        main(
            [
                "sec",
                "financials",
                "inspect",
                "--root",
                str(out_root),
                "--symbol",
                "AAPL",
                "--cik",
                "1046179",
            ]
        )
        == 2
    )
    assert "exactly one" in capsys.readouterr().err

    # Verify cik partition directly
    assert validate_cik_financials_partition(out_root / f"cik={delisted_cik}")["symbols"] == []


def test_cli_sync_incremental_and_user_agent_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cik = "0000320193"
    acc = "0000320193-25-000010"
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    sub_body = _submissions_payload(
        cik,
        accessions=[acc],
        forms=["10-Q"],
        filing_dates=["2025-05-02"],
        report_dates=["2025-03-31"],
        acceptance_datetimes=["2025-05-02T20:15:00.000Z"],
        primary_docs=["aapl-20250331.htm"],
        tickers=["AAPL"],
    )
    secret_ua = "TopSecretDesk/9.9 (hidden-contact@example.invalid)"
    ua_file = tmp_path / "sec-contact.txt"
    ua_file.write_text(secret_ua + "\n", encoding="utf-8")

    universe_file = tmp_path / "ciks.txt"
    universe_file.write_text("# test universe\n320193\n", encoding="utf-8")

    def _http_factory(user_agent: str, *, limiter: Any = None) -> _FakeSecHttpClient:
        return _FakeSecHttpClient(user_agent, {url: sub_body}, limiter=limiter)

    vintage = _sample_vintage(cik=cik, accession=acc, with_adr=True)

    class _FakeFinClient(SecFinancialsClient):
        def __init__(self, user_agent: str, *, http_client: Any = None) -> None:
            super().__init__(
                user_agent,
                runner=lambda _req: [vintage],
                http_client=http_client,
            )

    monkeypatch.setattr("ohmydata.providers.sec.cli.SecHttpClient", _http_factory)
    monkeypatch.setattr(
        "ohmydata.providers.sec.edgartools_adapter.SecFinancialsClient", _FakeFinClient
    )

    root = tmp_path / "inc-lake"
    code = main(
        [
            "sec",
            "financials",
            "sync-incremental",
            "--root",
            str(root),
            "--universe-ciks",
            str(universe_file),
            "--forms",
            "10-Q",
            "--as-of",
            "2025-05-03T00:00:00Z",
            "--overlap-hours",
            "12",
            "--max-rps",
            "5.0",
            "--availability-policy",
            "accepted-at-plus-lag",
            "--lag-days",
            "0",
            "--user-agent-file",
            str(ua_file),
            "--run-id",
            "cli-run-001",
            "--json",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert secret_ua not in captured.out
    assert secret_ua not in captured.err
    assert "hidden-contact@example.invalid" not in captured.out
    assert "hidden-contact@example.invalid" not in captured.err

    data = json.loads(captured.out)
    assert data["status"] == "completed"
    assert data["run_id"] == "cli-run-001"
    assert data["synced"] == 1
    assert data["empty"] == 0
    assert data["failed_ciks"] == []
    assert Path(data["receipt_path"]).is_file()
    assert secret_ua not in Path(data["receipt_path"]).read_text(encoding="utf-8")

    # Validate CLI also validates incremental batches automatically
    assert main(["sec", "financials", "validate", "--root", str(root), "--json"]) == 0
    val_data = json.loads(capsys.readouterr().out)
    assert val_data["batches_verified"] == 1


def test_cli_sync_incremental_redacts_ua_in_failure_receipt_and_rejects_bad_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cik = "0000320193"
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    secret_ua = "SensitiveOrg/1.0 (private-ops@example.invalid)"

    def _failing_http_factory(user_agent: str, *, limiter: Any = None) -> _FakeSecHttpClient:
        return _FakeSecHttpClient(
            user_agent,
            {},
            limiter=limiter,
            error_on_url={url: ValueError(f"rejected User-Agent: {secret_ua}")},
        )

    class _FakeFinClient(SecFinancialsClient):
        def __init__(self, user_agent: str, *, http_client: Any = None) -> None:
            super().__init__(user_agent, runner=lambda _req: [], http_client=http_client)

    monkeypatch.setattr("ohmydata.providers.sec.cli.SecHttpClient", _failing_http_factory)
    monkeypatch.setattr(
        "ohmydata.providers.sec.edgartools_adapter.SecFinancialsClient", _FakeFinClient
    )

    root = tmp_path / "fail-lake"
    code = main(
        [
            "sec",
            "financials",
            "sync-incremental",
            "--root",
            str(root),
            "--ciks",
            "320193",
            "--as-of",
            "2025-05-03T00:00:00Z",
            "--user-agent",
            secret_ua,
            "--run-id",
            "cli-fail-001",
            "--json",
            "--quiet",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert secret_ua not in captured.out
    assert "private-ops@example.invalid" not in captured.out
    assert secret_ua not in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "completed_with_failures"
    assert payload["permanent_failure"] == 1
    assert "[REDACTED]" in payload["outcomes"][0]["error_message"]

    # Reject max-rps > 10 and naive --as-of
    assert (
        main(
            [
                "sec",
                "financials",
                "sync-incremental",
                "--root",
                str(root),
                "--ciks",
                "320193",
                "--max-rps",
                "12.0",
                "--user-agent",
                secret_ua,
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "sec",
                "financials",
                "sync-incremental",
                "--root",
                str(root),
                "--ciks",
                "320193",
                "--as-of",
                "2025-05-03T00:00:00",
                "--user-agent",
                secret_ua,
            ]
        )
        == 2
    )
    err_out = capsys.readouterr().err
    assert secret_ua not in err_out


def test_cli_bulk_project_end_to_end_and_filters(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cf_zip, sub_zip = _write_bulk_zips(tmp_path)
    out_dir = tmp_path / "bulk-out"
    universe_file = tmp_path / "bulk-ciks.json"
    universe_file.write_text(json.dumps({"ciks": ["320193"]}), encoding="utf-8")

    code = main(
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(out_dir),
            "--universe-ciks",
            str(universe_file),
            "--start-year",
            "2024",
            "--end-year",
            "2024",
            "--forms",
            "10-Q,10-K",
            "--no-amendments",
            "--acceptance-upper",
            "2024-12-31T23:59:59Z",
            "--json",
            "--quiet",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    data = json.loads(captured.out)
    assert data["status"] == "completed"
    assert data["counts"]["ok"] == 1
    assert data["filters"]["include_amendments"] is False
    assert data["filters"]["ciks"] == ["0000320193"]

    validated = validate_bulk_companyfacts_dataset(out_dir)
    assert validated["counts"]["ok"] == 1
    # Only Q1 (accepted 2024-05-03) passes acceptance_upper 2024-12-31; FY (accepted 2025-02-01) excluded
    assert validated["counts"]["fundamental_row_count"] == 1

    # Config file support with --root alias
    cfg_path = tmp_path / "bulk.json"
    out_dir_2 = tmp_path / "bulk-out-2"
    cfg_path.write_text(
        json.dumps(
            {
                "companyfacts_zip": str(cf_zip),
                "submissions_zip": str(sub_zip),
                "root": str(out_dir_2),
                "ciks": ["0000320193"],
                "quiet": True,
            }
        ),
        encoding="utf-8",
    )
    assert main(["sec", "financials", "bulk-project", "--config", str(cfg_path), "--json"]) == 0
    data2 = json.loads(capsys.readouterr().out)
    assert data2["counts"]["fundamental_row_count"] == 2
    assert write_cik_financials_partition(tmp_path / "check", "320193", [])["cik"] == "0000320193"
