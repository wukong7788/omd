from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from ohmydata.providers.sec.bulk_zip import (
    BULK_COMPANYFACTS_DATASET_SCHEMA,
    BULK_COMPANYFACTS_WRITER_PROFILE,
    BulkZipBombError,
    BulkZipSecurityError,
    SecBulkCikOutcomeStatus,
    build_bulk_companyfacts_dataset,
    project_bulk_companyfacts_zips,
    validate_bulk_companyfacts_dataset,
    write_bulk_companyfacts_dataset,
)

_US_CIK = "0000320193"
_FPI_CIK = "0001046179"
_MISS_SUB_CIK = "0000999001"
_CORRUPT_CIK = "0000999002"
_EMPTY_CIK = "0000999003"


def _sub_row(
    accn: str,
    form: str,
    report_date: str,
    accepted: str,
    doc: str = "filing.htm",
) -> dict[str, str]:
    return {
        "accessionNumber": accn,
        "form": form,
        "filingDate": accepted[:10],
        "reportDate": report_date,
        "primaryDocument": doc,
        "acceptanceDateTime": accepted,
    }


def _cols(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    keys = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
        "acceptanceDateTime",
    )
    return {k: [r[k] for r in rows] for k in keys}


def _make_sample_zips(tmp_path: Path) -> tuple[Path, Path]:
    cf_zip = tmp_path / "companyfacts.zip"
    sub_zip = tmp_path / "submissions.zip"

    # US-GAAP company with root + 2 paginated history pages (2015 + 2020 + 2025)
    accn_2015 = f"{_US_CIK}-16-000001"
    accn_2020 = f"{_US_CIK}-21-000001"
    accn_2025_q3 = f"{_US_CIK}-25-000003"
    accn_2025_q3_a = f"{_US_CIK}-25-000004"
    accn_2025_fy = f"{_US_CIK}-26-000001"

    us_cf = {
        "cik": int(_US_CIK),
        "entityName": "Synthetic US Tech Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 50_000,
                                "accn": accn_2015,
                                "form": "10-K",
                                "fy": 2015,
                                "fp": "FY",
                                "start": "2015-01-01",
                                "end": "2015-12-31",
                                "filed": "2016-02-15",
                            },
                            {
                                "val": 80_000,
                                "accn": accn_2020,
                                "form": "10-K",
                                "fy": 2020,
                                "fp": "FY",
                                "start": "2020-01-01",
                                "end": "2020-12-31",
                                "filed": "2021-02-15",
                            },
                            {
                                "val": 30_000,
                                "accn": accn_2025_q3,
                                "form": "10-Q",
                                "fy": 2025,
                                "fp": "Q3",
                                "start": "2025-07-01",
                                "end": "2025-09-30",
                                "filed": "2025-11-04",
                            },
                            {
                                "val": 90_000,
                                "accn": accn_2025_q3,
                                "form": "10-Q",
                                "fy": 2025,
                                "fp": "Q3",
                                "start": "2025-01-01",
                                "end": "2025-09-30",
                                "filed": "2025-11-04",
                            },
                            {
                                "val": 30_500,
                                "accn": accn_2025_q3_a,
                                "form": "10-Q/A",
                                "fy": 2025,
                                "fp": "Q3",
                                "start": "2025-07-01",
                                "end": "2025-09-30",
                                "filed": "2025-11-20",
                            },
                            {
                                "val": 125_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            },
                            # Comparative prior-year row in FY2025 10-K: MUST NOT contaminate FY2025
                            {
                                "val": 110_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2026-02-10",
                            },
                        ]
                    }
                },
                "GrossProfit": {
                    "units": {
                        "USD": [
                            {
                                "val": 40_000,
                                "accn": accn_2025_q3,
                                "form": "10-Q",
                                "fy": 2025,
                                "fp": "Q3",
                                "start": "2025-01-01",
                                "end": "2025-09-30",
                                "filed": "2025-11-04",
                            },
                            {
                                "val": 56_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            },
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "val": 25_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            {
                                "val": 31_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {
                                "val": 4_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "val": 350_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                },
                "LongTermDebtAndCapitalLeaseObligations": {
                    "units": {
                        "USD": [
                            {
                                "val": 95_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                },
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "val": 15_500_000_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                },
                "EarningsPerShareDiluted": {
                    "units": {
                        "USD/shares": [
                            {
                                "val": Decimal("1.61"),
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                },
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "val": 15_408_095_000,
                                "accn": accn_2025_fy,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2026-01-31",
                                "filed": "2026-02-10",
                            }
                        ]
                    }
                }
            },
        },
    }

    # IFRS FPI reporting in TWD via Form 20-F
    accn_fpi_2025 = f"{_FPI_CIK}-26-000001"
    fpi_cf = {
        "cik": int(_FPI_CIK),
        "entityName": "Synthetic Foundry ADR",
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "TWD": [
                            {
                                "val": 2_894_308_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "GrossProfit": {
                    "units": {
                        "TWD": [
                            {
                                "val": 1_624_000_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "ProfitLossFromOperatingActivities": {
                    "units": {
                        "TWD": [
                            {
                                "val": 1_322_000_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "ProfitLoss": {
                    "units": {
                        "TWD": [
                            {
                                "val": 1_173_000_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "CashFlowsFromUsedInOperatingActivities": {
                    "units": {
                        "TWD": [
                            {
                                "val": 1_826_000_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": {
                    "units": {
                        "TWD": [
                            {
                                "val": 956_000_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "TWD": [
                            {
                                "val": 6_691_000_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "Borrowings": {
                    "units": {
                        "TWD": [
                            {
                                "val": 950_000_000_000,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
                "DilutedEarningsLossPerShare": {
                    "units": {
                        "TWD/shares": [
                            {
                                "val": Decimal("45.25"),
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                },
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "val": 25_932_733_242,
                                "accn": accn_fpi_2025,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2026-02-28",
                                "filed": "2026-04-17",
                            }
                        ]
                    }
                }
            },
        },
    }

    empty_cf = {"cik": int(_EMPTY_CIK), "entityName": "Empty Corp", "facts": {"us-gaap": {}}}
    miss_sub_cf = {"cik": int(_MISS_SUB_CIK), "entityName": "Orphan Corp", "facts": {"us-gaap": {}}}

    def _dump(obj: object) -> str:
        return json.dumps(obj, default=lambda v: float(v) if isinstance(v, Decimal) else v)

    with zipfile.ZipFile(cf_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"CIK{_US_CIK}.json", _dump(us_cf))
        zf.writestr(f"CIK{_FPI_CIK}.json", _dump(fpi_cf))
        zf.writestr(f"CIK{_MISS_SUB_CIK}.json", _dump(miss_sub_cf))
        zf.writestr(f"CIK{_CORRUPT_CIK}.json", "{not valid json")
        zf.writestr(f"CIK{_EMPTY_CIK}.json", _dump(empty_cf))

    us_sub_root = {
        "cik": _US_CIK,
        "entityType": "operating",
        "name": "Synthetic US Tech Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": _cols(
                [
                    _sub_row(accn_2025_fy, "10-K", "2025-12-31", "2026-02-10T21:30:05.000Z"),
                    _sub_row(accn_2025_q3_a, "10-Q/A", "2025-09-30", "2025-11-20T18:00:00.000Z"),
                    _sub_row(accn_2025_q3, "10-Q", "2025-09-30", "2025-11-04T20:15:00.000Z"),
                ]
            ),
            "files": [
                {
                    "name": f"CIK{_US_CIK}-submissions-001.json",
                    "filingCount": 1,
                    "filingFrom": "2021-01-01",
                    "filingTo": "2021-12-31",
                },
                {
                    "name": f"CIK{_US_CIK}-submissions-002.json",
                    "filingCount": 1,
                    "filingFrom": "2016-01-01",
                    "filingTo": "2016-12-31",
                },
            ],
        },
    }
    us_sub_h1 = _cols([_sub_row(accn_2020, "10-K", "2020-12-31", "2021-02-15T21:00:00.000Z")])
    us_sub_h2 = _cols([_sub_row(accn_2015, "10-K", "2015-12-31", "2016-02-15T21:00:00.000Z")])

    fpi_sub_root = {
        "cik": _FPI_CIK,
        "entityType": "operating",
        "name": "Synthetic Foundry ADR",
        "tickers": ["TSM"],
        "filings": {
            "recent": _cols(
                [_sub_row(accn_fpi_2025, "20-F", "2025-12-31", "2026-04-17T11:22:33.000Z")]
            ),
            "files": [],
        },
    }
    corrupt_sub_root = {
        "cik": _CORRUPT_CIK,
        "entityType": "operating",
        "name": "Corrupt Corp",
        "tickers": [],
        "filings": {"recent": _cols([]), "files": []},
    }
    empty_sub_root = {
        "cik": _EMPTY_CIK,
        "entityType": "operating",
        "name": "Empty Corp",
        "tickers": [],
        "filings": {"recent": _cols([]), "files": []},
    }

    with zipfile.ZipFile(sub_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"CIK{_US_CIK}.json", json.dumps(us_sub_root))
        zf.writestr(f"CIK{_US_CIK}-submissions-001.json", json.dumps(us_sub_h1))
        zf.writestr(f"CIK{_US_CIK}-submissions-002.json", json.dumps(us_sub_h2))
        zf.writestr(f"CIK{_FPI_CIK}.json", json.dumps(fpi_sub_root))
        zf.writestr(f"CIK{_CORRUPT_CIK}.json", json.dumps(corrupt_sub_root))
        zf.writestr(f"CIK{_EMPTY_CIK}.json", json.dumps(empty_sub_root))

    return cf_zip, sub_zip


def test_bulk_zip_end_to_end_multi_taxonomy_history_and_parquet_validation(tmp_path: Path) -> None:
    cf_zip, sub_zip = _make_sample_zips(tmp_path)
    out_dir = tmp_path / "bulk_out"

    manifest = build_bulk_companyfacts_dataset(cf_zip, sub_zip, out_dir)
    assert manifest["dataset_schema"] == BULK_COMPANYFACTS_DATASET_SCHEMA
    assert manifest["writer_profile"] == BULK_COMPANYFACTS_WRITER_PROFILE
    assert manifest["source_archives"]["companyfacts_zip"]["sha256"] == (
        hashlib.sha256(cf_zip.read_bytes()).hexdigest()
    )
    assert manifest["source_archives"]["submissions_zip"]["sha256"] == (
        hashlib.sha256(sub_zip.read_bytes()).hexdigest()
    )

    outcomes = manifest["cik_outcomes"]
    assert outcomes[_US_CIK]["status"] == SecBulkCikOutcomeStatus.OK.value
    assert outcomes[_US_CIK]["history_pages_read"] == 2
    assert outcomes[_US_CIK]["taxonomy"] == "us-gaap"
    assert outcomes[_US_CIK]["reporting_currency"] == "USD"
    assert outcomes[_US_CIK]["symbols"] == ["AAPL"]

    assert outcomes[_FPI_CIK]["status"] == SecBulkCikOutcomeStatus.OK.value
    assert outcomes[_FPI_CIK]["taxonomy"] == "ifrs-full"
    assert outcomes[_FPI_CIK]["reporting_currency"] == "TWD"
    assert outcomes[_FPI_CIK]["symbols"] == ["TSM"]

    assert outcomes[_MISS_SUB_CIK]["status"] == SecBulkCikOutcomeStatus.MISSING_SUBMISSIONS.value
    assert outcomes[_CORRUPT_CIK]["status"] == SecBulkCikOutcomeStatus.PARSE_FAILED.value
    assert outcomes[_EMPTY_CIK]["status"] == SecBulkCikOutcomeStatus.EMPTY.value

    fund_rows = pq.read_table(out_dir / "fundamentals.parquet").to_pylist()
    # Verify historical pages (2015, 2020) were joined with exact UTC accepted_at
    us_fy_years = {
        r["fiscal_year"] for r in fund_rows if r["cik"] == _US_CIK and r["period_kind"] == "FY"
    }
    assert us_fy_years == {2015, 2020, 2025}

    us_2025_fy = next(
        r
        for r in fund_rows
        if r["cik"] == _US_CIK and r["fiscal_year"] == 2025 and r["period_kind"] == "FY"
    )
    assert us_2025_fy["revenue"] == "125000"  # NOT contaminated by comparative 2024 row 110000
    assert us_2025_fy["total_assets"] == "350000"
    assert us_2025_fy["total_debt"] == "95000"
    assert us_2025_fy["diluted_shares"] == "15500000000"
    assert us_2025_fy["dei_shares_outstanding"] == "15408095000"
    assert us_2025_fy["dei_shares_outstanding_end"] == date(2026, 1, 31)
    assert us_2025_fy["operating_income"] is None  # Missing stays None, NEVER 0
    assert us_2025_fy["accepted_at"] == datetime(2026, 2, 10, 21, 30, 5, tzinfo=UTC)

    # Verify Q4_DERIVED exact subtraction (125000 - 90000 = 35000, 56000 - 40000 = 16000)
    us_2025_q4 = next(
        r
        for r in fund_rows
        if r["cik"] == _US_CIK and r["fiscal_year"] == 2025 and r["period_kind"] == "Q4_DERIVED"
    )
    assert us_2025_q4["revenue"] == "35000"
    assert us_2025_q4["gross_profit"] == "16000"
    assert us_2025_q4["eps_diluted"] is None  # EPS is NEVER derived by subtraction

    # Verify IFRS 20-F TWD values preserved without USD coercion
    fpi_2025 = next(r for r in fund_rows if r["cik"] == _FPI_CIK)
    assert fpi_2025["form"] == "20-F"
    assert fpi_2025["reporting_currency"] == "TWD"
    assert fpi_2025["taxonomy"] == "ifrs-full"
    assert fpi_2025["revenue"] == "2894308000000"
    assert fpi_2025["capex"] == "956000000000"
    assert fpi_2025["total_debt"] == "950000000000"
    assert fpi_2025["eps_diluted"] == "45.25"
    assert fpi_2025["dei_shares_outstanding"] == "25932733242"
    assert fpi_2025["dei_shares_outstanding_end"] == date(2026, 2, 28)

    # Idempotent re-write returns identical manifest
    proj = project_bulk_companyfacts_zips(cf_zip, sub_zip)
    assert write_bulk_companyfacts_dataset(out_dir, proj) == manifest

    # Writing different dataset to same immutable output dir fails explicitly
    filtered_proj = project_bulk_companyfacts_zips(cf_zip, sub_zip, ciks=[_US_CIK])
    with pytest.raises(ValueError, match="immutable bulk companyfacts dataset differs"):
        write_bulk_companyfacts_dataset(out_dir, filtered_proj)

    # Tampering with parquet file fails SHA-256 validation
    (out_dir / "facts.parquet").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash or size mismatch"):
        validate_bulk_companyfacts_dataset(out_dir)


def test_bulk_zip_filters_pit_cutoff_and_amendment_toggle(tmp_path: Path) -> None:
    cf_zip, sub_zip = _make_sample_zips(tmp_path)

    # Exclude amendments and restrict to US CIK + 2020..2025 + acceptance_upper before FY2025 10-K
    cutoff = datetime(2026, 2, 10, 21, 30, 4, tzinfo=UTC)  # 1 second before FY2025 10-K acceptance
    proj = project_bulk_companyfacts_zips(
        cf_zip,
        sub_zip,
        ciks=[int(_US_CIK)],
        start_year=2020,
        end_year=2025,
        forms=("10-K", "10-Q"),
        include_amendments=False,
        acceptance_upper=cutoff,
    )
    assert len(proj.outcomes) == 1
    assert proj.outcomes[0].status is SecBulkCikOutcomeStatus.OK
    assert all(not r.is_amendment for r in proj.fundamental_rows)
    assert all(r.accepted_at <= cutoff for r in proj.fundamental_rows)
    assert all(2020 <= r.fiscal_year <= 2025 for r in proj.fundamental_rows)
    # FY2025 10-K (and thus Q4_DERIVED) MUST NOT leak before acceptance_upper
    assert not any(
        r.fiscal_year == 2025 and r.period_kind in {"FY", "Q4_DERIVED"}
        for r in proj.fundamental_rows
    )


def test_bulk_zip_rejects_zip_slip_symlinks_and_zip_bombs(tmp_path: Path) -> None:
    cf_zip, sub_zip = _make_sample_zips(tmp_path)

    # 1. Path traversal (Zip Slip) rejected immediately
    evil_zip = tmp_path / "evil_slip.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../escape.json", "{}")
    with pytest.raises(BulkZipSecurityError, match="path traversal"):
        project_bulk_companyfacts_zips(evil_zip, sub_zip)

    # 2. Symlink entry rejected immediately
    sym_zip = tmp_path / "evil_symlink.zip"
    with zipfile.ZipFile(sym_zip, "w") as zf:
        info = zipfile.ZipInfo(f"CIK{_US_CIK}.json")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(BulkZipSecurityError, match="symlink"):
        project_bulk_companyfacts_zips(sym_zip, sub_zip)

    # 3. Zip bomb per-entry byte limit enforced
    with pytest.raises(BulkZipBombError, match="per-entry byte limit"):
        project_bulk_companyfacts_zips(cf_zip, sub_zip, ciks=[_US_CIK], max_entry_bytes=64)

    # 4. Zip bomb cumulative total byte limit enforced
    with pytest.raises(BulkZipBombError, match="cumulative decompressed bytes"):
        project_bulk_companyfacts_zips(cf_zip, sub_zip, max_total_bytes=128)


def test_bulk_zip_missing_referenced_history_page_fails_explicitly(tmp_path: Path) -> None:
    cf_zip, _ = _make_sample_zips(tmp_path)
    incomplete_sub_zip = tmp_path / "incomplete_submissions.zip"
    sub_root = {
        "cik": _US_CIK,
        "entityType": "operating",
        "name": "Synthetic US Tech Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": _cols([]),
            "files": [
                {
                    "name": f"CIK{_US_CIK}-submissions-001.json",
                    "filingCount": 1,
                    "filingFrom": "2021-01-01",
                    "filingTo": "2021-12-31",
                }
            ],
        },
    }
    with zipfile.ZipFile(incomplete_sub_zip, "w") as zf:
        zf.writestr(f"CIK{_US_CIK}.json", json.dumps(sub_root))

    proj = project_bulk_companyfacts_zips(cf_zip, incomplete_sub_zip, ciks=[_US_CIK])
    assert proj.outcomes[0].status is SecBulkCikOutcomeStatus.PARSE_FAILED
    assert "missing from submissions.zip" in (proj.outcomes[0].error_message or "")


def test_bulk_zip_validator_rejects_tampered_source_archives_filters_and_counts(
    tmp_path: Path,
) -> None:
    cf_zip, sub_zip = _make_sample_zips(tmp_path)
    out_dir = tmp_path / "bulk_tamper"
    original = build_bulk_companyfacts_dataset(cf_zip, sub_zip, out_dir)
    manifest_path = out_dir / "manifest.json"

    def _write_tampered(mutator) -> None:
        copy = json.loads(json.dumps(original))
        mutator(copy)
        manifest_path.write_text(json.dumps(copy, indent=2, sort_keys=True), encoding="utf-8")

    # 1. Invalid source_archives sha256
    _write_tampered(lambda m: m["source_archives"]["companyfacts_zip"].__setitem__("sha256", "xyz"))
    with pytest.raises(ValueError, match="source archive invalid"):
        validate_bulk_companyfacts_dataset(out_dir)

    # 2. Negative source_archives bytes
    _write_tampered(lambda m: m["source_archives"]["submissions_zip"].__setitem__("bytes", -1))
    with pytest.raises(ValueError, match="source archive invalid"):
        validate_bulk_companyfacts_dataset(out_dir)

    # 3. Non-dict filters raises ValueError (not AttributeError)
    _write_tampered(lambda m: m.__setitem__("filters", ["bad"]))
    with pytest.raises(ValueError, match="filters must be a mapping"):
        validate_bulk_companyfacts_dataset(out_dir)

    # 4. Inconsistent total_ciks
    _write_tampered(lambda m: m["counts"].__setitem__("total_ciks", 999))
    with pytest.raises(ValueError, match="total_ciks mismatch"):
        validate_bulk_companyfacts_dataset(out_dir)

    # 5. Inconsistent per-status count
    _write_tampered(lambda m: m["counts"].__setitem__("ok", 99))
    with pytest.raises(ValueError, match="count mismatch for ok"):
        validate_bulk_companyfacts_dataset(out_dir)


def _write_single_cik_zips(
    tmp_path: Path,
    *,
    cik: str,
    entity_name: str,
    tickers: list[str],
    facts: dict[str, object],
    filings: list[dict[str, str]],
    prefix: str = "case",
) -> tuple[Path, Path]:
    cf_zip = tmp_path / f"{prefix}_cf.zip"
    sub_zip = tmp_path / f"{prefix}_sub.zip"
    cf_payload = {"cik": int(cik), "entityName": entity_name, "facts": facts}
    sub_payload = {
        "cik": cik,
        "entityType": "operating",
        "name": entity_name,
        "tickers": tickers,
        "filings": {"recent": _cols(filings), "files": []},
    }
    with zipfile.ZipFile(cf_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"CIK{cik}.json",
            json.dumps(cf_payload, default=lambda v: float(v) if isinstance(v, Decimal) else v),
        )
    with zipfile.ZipFile(sub_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"CIK{cik}.json", json.dumps(sub_payload))
    return cf_zip, sub_zip


def test_bulk_zip_20f_dual_currency_cny_and_twd_convenience_translation(tmp_path: Path) -> None:
    # 1. US-GAAP 20-F ADR (CNY functional + USD convenience translation for latest year)
    cny_cik = "0001577552"
    cny_accn = f"{cny_cik}-26-000001"
    cny_facts = {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "CNY": [
                        {
                            "val": 941_168_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2023-04-01",
                            "end": "2024-03-31",
                            "filed": "2026-06-20",
                        },
                        {
                            "val": 996_347_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2024-04-01",
                            "end": "2025-03-31",
                            "filed": "2026-06-20",
                        },
                        {
                            "val": 1_023_670_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2025-04-01",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        },
                    ],
                    "USD": [
                        {
                            "val": 141_200_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2025-04-01",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ],
                }
            },
            "NetIncomeLoss": {
                "units": {
                    "CNY": [
                        {
                            "val": 103_592_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2025-04-01",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ],
                    "USD": [
                        {
                            "val": 14_300_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2025-04-01",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ],
                }
            },
            "Assets": {
                "units": {
                    "CNY": [
                        {
                            "val": 1_909_570_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ],
                    "USD": [
                        {
                            "val": 263_400_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ],
                }
            },
            "WeightedAverageNumberOfDilutedSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "val": 19_235_000_000,
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2025-04-01",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ]
                }
            },
            "EarningsPerShareDiluted": {
                "units": {
                    "CNY/shares": [
                        {
                            "val": Decimal("5.50"),
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2025-04-01",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ],
                    "USD/shares": [
                        {
                            "val": Decimal("0.76"),
                            "accn": cny_accn,
                            "form": "20-F",
                            "fy": 2026,
                            "fp": "FY",
                            "start": "2025-04-01",
                            "end": "2026-03-31",
                            "filed": "2026-06-20",
                        }
                    ],
                }
            },
        }
    }
    cf_zip, sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=cny_cik,
        entity_name="Synthetic CNY ADR",
        tickers=["BABA"],
        facts=cny_facts,
        filings=[_sub_row(cny_accn, "20-F", "2026-03-31", "2026-06-20T12:00:00.000Z")],
        prefix="cny",
    )
    proj = project_bulk_companyfacts_zips(cf_zip, sub_zip)
    assert proj.outcomes[0].reporting_currency == "CNY"
    # Native facts preserve BOTH CNY and USD untouched
    assert {f.currency for f in proj.joined_facts if f.currency} == {"CNY", "USD"}
    assert len(proj.fundamental_rows) == 1
    row = proj.fundamental_rows[0]
    assert row.reporting_currency == "CNY"
    assert row.revenue == Decimal(1023670000000)
    assert row.net_income == Decimal(103592000000)
    assert row.total_assets == Decimal(1909570000000)
    assert row.diluted_shares == Decimal(19235000000)
    assert row.eps_diluted == Decimal("5.5")
    prov = json.loads(row.source_tags_json)
    assert prov["eps_diluted"]["unit"] == "CNY/shares"
    assert prov["currency_selection"] == {
        "selected": "CNY",
        "alternate_currencies": ["USD"],
        "basis": "filing_comparative_periods",
    }

    # 2. IFRS 20-F ADR (TWD functional + USD convenience translation, single-year tiebreak)
    twd_cik = "0001046179"
    twd_accn = f"{twd_cik}-25-000001"
    twd_facts = {
        "ifrs-full": {
            "Revenue": {
                "units": {
                    "TWD": [
                        {
                            "val": 2_894_307_700_000,
                            "accn": twd_accn,
                            "form": "20-F",
                            "fy": 2024,
                            "fp": "FY",
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "filed": "2025-04-17",
                        }
                    ],
                    "USD": [
                        {
                            "val": 90_080_000_000,
                            "accn": twd_accn,
                            "form": "20-F",
                            "fy": 2024,
                            "fp": "FY",
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "filed": "2025-04-17",
                        }
                    ],
                }
            },
            "DilutedEarningsLossPerShare": {
                "units": {
                    "TWD/shares": [
                        {
                            "val": Decimal("44.67"),
                            "accn": twd_accn,
                            "form": "20-F",
                            "fy": 2024,
                            "fp": "FY",
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "filed": "2025-04-17",
                        }
                    ],
                    "USD/shares": [
                        {
                            "val": Decimal("1.39"),
                            "accn": twd_accn,
                            "form": "20-F",
                            "fy": 2024,
                            "fp": "FY",
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "filed": "2025-04-17",
                        }
                    ],
                }
            },
        }
    }
    twd_cf_zip, twd_sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=twd_cik,
        entity_name="Synthetic TWD ADR",
        tickers=["TSM"],
        facts=twd_facts,
        filings=[_sub_row(twd_accn, "20-F", "2024-12-31", "2025-04-17T10:00:00.000Z")],
        prefix="twd",
    )
    twd_proj = project_bulk_companyfacts_zips(twd_cf_zip, twd_sub_zip)
    assert len(twd_proj.fundamental_rows) == 1
    twd_row = twd_proj.fundamental_rows[0]
    assert twd_row.reporting_currency == "TWD"
    assert twd_row.revenue == Decimal(2894307700000)
    assert twd_row.eps_diluted == Decimal("44.67")
    twd_prov = json.loads(twd_row.source_tags_json)
    assert twd_prov["currency_selection"] == {
        "selected": "TWD",
        "alternate_currencies": ["USD"],
        "basis": "fpi_convenience_translation_tiebreak",
    }


def test_bulk_zip_never_mixes_currencies_and_fails_closed_on_ambiguous_non_usd_tie(
    tmp_path: Path,
) -> None:
    # 1. USD-primary 20-F with ONLY EUR/shares EPS: MUST NOT mix EUR/shares into USD row
    mix_cik = "0000879764"
    mix_accn = f"{mix_cik}-26-000001"
    mix_facts = {
        "ifrs-full": {
            "Revenue": {
                "units": {
                    "USD": [
                        {
                            "val": 195_000_000_000,
                            "accn": mix_accn,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "filed": "2026-03-20",
                        },
                        {
                            "val": 201_000_000_000,
                            "accn": mix_accn,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-03-20",
                        },
                    ]
                }
            },
            "Assets": {
                "units": {
                    "USD": [
                        {
                            "val": 291_000_000_000,
                            "accn": mix_accn,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "end": "2025-12-31",
                            "filed": "2026-03-20",
                        }
                    ]
                }
            },
            "DilutedEarningsLossPerShare": {
                "units": {
                    "EUR/shares": [
                        {
                            "val": Decimal("5.10"),
                            "accn": mix_accn,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-03-20",
                        }
                    ]
                }
            },
        }
    }
    cf_zip, sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=mix_cik,
        entity_name="Synthetic USD-Primary FPI",
        tickers=["TTE"],
        facts=mix_facts,
        filings=[_sub_row(mix_accn, "20-F", "2025-12-31", "2026-03-20T12:00:00.000Z")],
        prefix="nomix",
    )
    proj = project_bulk_companyfacts_zips(cf_zip, sub_zip)
    assert len(proj.fundamental_rows) == 1
    row = proj.fundamental_rows[0]
    assert row.reporting_currency == "USD"
    assert row.revenue == Decimal(201000000000)
    assert row.total_assets == Decimal(291000000000)
    assert row.eps_diluted is None  # EUR/shares NEVER mixed into USD row!
    assert json.loads(row.source_tags_json)["currency_selection"]["alternate_currencies"] == ["EUR"]

    # 2. Genuine non-USD tie (EUR vs GBP equal evidence, even on disjoint concepts) fails closed!
    amb_cik = "0000999888"
    amb_accn = f"{amb_cik}-26-000001"
    amb_facts = {
        "ifrs-full": {
            "Revenue": {
                "units": {
                    "EUR": [
                        {
                            "val": 100_000,
                            "accn": amb_accn,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-03-20",
                        }
                    ]
                }
            },
            "ProfitLoss": {
                "units": {
                    "GBP": [
                        {
                            "val": 20_000,
                            "accn": amb_accn,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-03-20",
                        }
                    ]
                }
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "val": 1_000_000,
                            "accn": amb_accn,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "end": "2026-01-31",
                            "filed": "2026-03-20",
                        }
                    ]
                }
            }
        },
    }
    amb_cf_zip, amb_sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=amb_cik,
        entity_name="Ambiguous Dual Non-USD Corp",
        tickers=["AMBG"],
        facts=amb_facts,
        filings=[_sub_row(amb_accn, "20-F", "2025-12-31", "2026-03-20T12:00:00.000Z")],
        prefix="ambig",
    )
    amb_proj = project_bulk_companyfacts_zips(amb_cf_zip, amb_sub_zip)
    assert len(amb_proj.fundamental_rows) == 1
    amb_row = amb_proj.fundamental_rows[0]
    assert amb_row.reporting_currency is None
    assert amb_row.revenue is None
    assert amb_row.net_income is None
    assert amb_row.dei_shares_outstanding == Decimal(1000000)
    assert json.loads(amb_row.source_tags_json)["currency_selection"] == {
        "selected": None,
        "alternate_currencies": ["EUR", "GBP"],
        "basis": "ambiguous_tie",
    }

    # 3. Domestic 10-K with equal CNY + USD facts also fails closed (no 20-F tie-breaker)
    dom_cik = "0000999777"
    dom_accn = f"{dom_cik}-26-000001"
    dom_facts = {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "CNY": [
                        {
                            "val": 700_000,
                            "accn": dom_accn,
                            "form": "10-K",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-02-20",
                        }
                    ],
                    "USD": [
                        {
                            "val": 100_000,
                            "accn": dom_accn,
                            "form": "10-K",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-02-20",
                        }
                    ],
                }
            }
        }
    }
    dom_cf_zip, dom_sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=dom_cik,
        entity_name="Ambiguous Domestic 10-K Corp",
        tickers=["DOMT"],
        facts=dom_facts,
        filings=[_sub_row(dom_accn, "10-K", "2025-12-31", "2026-02-20T12:00:00.000Z")],
        prefix="domtie",
    )
    dom_proj = project_bulk_companyfacts_zips(dom_cf_zip, dom_sub_zip)
    assert dom_proj.fundamental_rows == ()


def test_bulk_zip_issuer_pit_evidence_and_q4_derived_currency_scoping(tmp_path: Path) -> None:
    # 10-K + 10-Q in USD where 10-K also discloses a EUR-denominated debt footnote
    cik = "0001067983"
    accn_q3 = f"{cik}-25-000003"
    accn_fy = f"{cik}-26-000001"
    facts = {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "val": 75_000,
                            "accn": accn_q3,
                            "form": "10-Q",
                            "fy": 2025,
                            "fp": "Q3",
                            "start": "2025-01-01",
                            "end": "2025-09-30",
                            "filed": "2025-11-05",
                        },
                        {
                            "val": 25_000,
                            "accn": accn_q3,
                            "form": "10-Q",
                            "fy": 2025,
                            "fp": "Q3",
                            "start": "2025-07-01",
                            "end": "2025-09-30",
                            "filed": "2025-11-05",
                        },
                        {
                            "val": 90_000,
                            "accn": accn_fy,
                            "form": "10-K",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "filed": "2026-02-20",
                        },
                        {
                            "val": 105_000,
                            "accn": accn_fy,
                            "form": "10-K",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-02-20",
                        },
                    ]
                }
            },
            "DebtAndCapitalLeaseObligations": {
                "units": {
                    "USD": [
                        {
                            "val": 120_000,
                            "accn": accn_fy,
                            "form": "10-K",
                            "fy": 2025,
                            "fp": "FY",
                            "end": "2025-12-31",
                            "filed": "2026-02-20",
                        }
                    ],
                    "EUR": [
                        {
                            "val": 6_000,
                            "accn": accn_fy,
                            "form": "10-K",
                            "fy": 2025,
                            "fp": "FY",
                            "end": "2025-12-31",
                            "filed": "2026-02-20",
                        }
                    ],
                }
            },
        }
    }
    cf_zip, sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=cik,
        entity_name="Synthetic US Multi-Currency Debt Corp",
        tickers=["BRKB"],
        facts=facts,
        filings=[
            _sub_row(accn_fy, "10-K", "2025-12-31", "2026-02-20T20:00:00.000Z"),
            _sub_row(accn_q3, "10-Q", "2025-09-30", "2025-11-05T20:00:00.000Z"),
        ],
        prefix="q4ccy",
    )
    proj = project_bulk_companyfacts_zips(cf_zip, sub_zip)
    assert proj.outcomes[0].reporting_currency == "USD"
    q4_row = next(r for r in proj.fundamental_rows if r.period_kind == "Q4_DERIVED")
    assert q4_row.reporting_currency == "USD"
    assert q4_row.revenue == Decimal(30000)
    assert q4_row.total_debt == Decimal(120000)
    assert json.loads(q4_row.source_tags_json)["currency_selection"] == {
        "selected": "USD",
        "alternate_currencies": ["EUR"],
        "basis": "filing_comparative_periods",
    }

    # PIT issuer history resolves a later tied 20-F without look-ahead from future filings
    pit_cik = "0000999666"
    accn_early_tie = f"{pit_cik}-24-000001"
    accn_later_eur = f"{pit_cik}-25-000001"
    accn_final_tie = f"{pit_cik}-26-000001"
    pit_facts = {
        "ifrs-full": {
            "Revenue": {
                "units": {
                    "EUR": [
                        {
                            "val": 10_000,
                            "accn": accn_early_tie,
                            "form": "20-F",
                            "fy": 2023,
                            "fp": "FY",
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "filed": "2024-03-15",
                        },
                        {
                            "val": 11_000,
                            "accn": accn_later_eur,
                            "form": "20-F",
                            "fy": 2024,
                            "fp": "FY",
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "filed": "2025-03-15",
                        },
                        {
                            "val": 12_000,
                            "accn": accn_final_tie,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-03-15",
                        },
                    ],
                    "GBP": [
                        {
                            "val": 8_500,
                            "accn": accn_early_tie,
                            "form": "20-F",
                            "fy": 2023,
                            "fp": "FY",
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "filed": "2024-03-15",
                        },
                        {
                            "val": 10_200,
                            "accn": accn_final_tie,
                            "form": "20-F",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-03-15",
                        },
                    ],
                }
            }
        }
    }
    pit_cf_zip, pit_sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=pit_cik,
        entity_name="Synthetic PIT Currency Corp",
        tickers=["PITC"],
        facts=pit_facts,
        filings=[
            _sub_row(accn_final_tie, "20-F", "2025-12-31", "2026-03-15T12:00:00.000Z"),
            _sub_row(accn_later_eur, "20-F", "2024-12-31", "2025-03-15T12:00:00.000Z"),
            _sub_row(accn_early_tie, "20-F", "2023-12-31", "2024-03-15T12:00:00.000Z"),
        ],
        prefix="pitccy",
    )
    pit_proj = project_bulk_companyfacts_zips(pit_cf_zip, pit_sub_zip)
    by_fy = {r.fiscal_year: r for r in pit_proj.fundamental_rows}
    # FY2023 EUR/GBP tie MUST NOT look ahead to FY2024 EUR-only filing -> fails closed
    assert 2023 not in by_fy
    # FY2024 is EUR-only (no currency_selection provenance noise)
    assert by_fy[2024].reporting_currency == "EUR"
    assert "currency_selection" not in json.loads(by_fy[2024].source_tags_json)
    # FY2025 EUR/GBP tie resolves to EUR using strictly prior FY2024 PIT evidence
    assert by_fy[2025].reporting_currency == "EUR"
    assert by_fy[2025].revenue == Decimal(12000)
    assert json.loads(by_fy[2025].source_tags_json)["currency_selection"] == {
        "selected": "EUR",
        "alternate_currencies": ["GBP"],
        "basis": "issuer_pit_evidence",
    }

    # Q4_DERIVED never subtracts mismatched FY (CAD) vs Q3 (USD) currencies
    mis_cik = "0000999555"
    mis_q3 = f"{mis_cik}-25-000003"
    mis_fy = f"{mis_cik}-26-000001"
    mis_facts = {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "val": 60_000,
                            "accn": mis_q3,
                            "form": "10-Q",
                            "fy": 2025,
                            "fp": "Q3",
                            "start": "2025-01-01",
                            "end": "2025-09-30",
                            "filed": "2025-11-05",
                        }
                    ],
                    "CAD": [
                        {
                            "val": 100_000,
                            "accn": mis_fy,
                            "form": "10-K",
                            "fy": 2025,
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "filed": "2026-02-20",
                        }
                    ],
                }
            }
        }
    }
    mis_cf_zip, mis_sub_zip = _write_single_cik_zips(
        tmp_path,
        cik=mis_cik,
        entity_name="Mismatched Q4 Currency Corp",
        tickers=["MISM"],
        facts=mis_facts,
        filings=[
            _sub_row(mis_fy, "10-K", "2025-12-31", "2026-02-20T20:00:00.000Z"),
            _sub_row(mis_q3, "10-Q", "2025-09-30", "2025-11-05T20:00:00.000Z"),
        ],
        prefix="q4mis",
    )
    mis_proj = project_bulk_companyfacts_zips(mis_cf_zip, mis_sub_zip)
    assert not any(r.period_kind == "Q4_DERIVED" for r in mis_proj.fundamental_rows)
