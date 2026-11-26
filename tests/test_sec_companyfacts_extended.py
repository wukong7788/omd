from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import companyfacts
from ohmydata.providers.sec._event_discovery_models import SecDiscoverySource
from ohmydata.providers.sec.errors import SchemaMismatchError, TransientProviderError
from ohmydata.providers.sec.http import SecHttpClient
from ohmydata.providers.sec.quarterly import SecCompanyEligibilityStatus
from ohmydata.providers.sec.ticker_cik import SecTickerCikMapping

_CIK = "0000000123"
_AT = datetime(2026, 3, 1, tzinfo=UTC)


def _filing_row(
    accn: str,
    form: str,
    report_date: str,
    accepted: str,
    doc: str = "report.htm",
) -> dict[str, object]:
    return {
        "accessionNumber": accn,
        "form": form,
        "filingDate": accepted[:10],
        "reportDate": report_date,
        "primaryDocument": doc,
        "acceptanceDateTime": accepted,
    }


def _columns_dict(rows: list[dict[str, object]]) -> dict[str, list[object]]:
    columns = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
        "acceptanceDateTime",
    )
    return {key: [row[key] for row in rows] for key in columns}


def _make_root_body(
    cik: str,
    rows: list[dict[str, object]],
    *,
    entity_type: str = "operating",
    tickers: list[str] | None = None,
    files: list[dict[str, object]] | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "cik": int(cik),
        "entityType": entity_type,
        "tickers": tickers if tickers is not None else ["SYN"],
        "filings": {
            "recent": _columns_dict(rows),
            "files": files or [],
        },
    }
    return json.dumps(payload).encode("utf-8")


def _build_multi_year_fixtures(
    cik: str,
    start_year: int,
    end_year: int,
    *,
    split_history_year: int | None = None,
) -> tuple[bytes, bytes, dict[str, bytes]]:
    """Build multi-year US-GAAP 10-Q/10-K companyfacts + submissions root/history fixtures."""
    rev_rows: list[dict[str, object]] = []
    eps_rows: list[dict[str, object]] = []
    dei_rows: list[dict[str, object]] = []
    recent_filings: list[dict[str, object]] = []
    hist_filings: list[dict[str, object]] = []

    seq = 1
    for year in range(start_year, end_year + 1):
        target = (
            hist_filings if (split_history_year and year <= split_history_year) else recent_filings
        )
        q_specs = (
            (
                "Q1",
                f"{year}-01-01",
                f"{year}-03-31",
                f"{year}-05-01T16:00:00.000Z",
                100 + year - start_year,
            ),
            (
                "Q2",
                f"{year}-04-01",
                f"{year}-06-30",
                f"{year}-08-01T16:00:00.000Z",
                110 + year - start_year,
            ),
            (
                "Q3",
                f"{year}-07-01",
                f"{year}-09-30",
                f"{year}-11-01T16:00:00.000Z",
                120 + year - start_year,
            ),
        )
        ytd_sum = 0
        for fp, start_s, end_s, acc_ts, rev_val in q_specs:
            accn = f"{cik}-{year % 100:02d}-{seq:06d}"
            seq += 1
            ytd_sum += rev_val
            target.append(_filing_row(accn, "10-Q", end_s, acc_ts))
            rev_rows.append(
                {
                    "val": rev_val,
                    "accn": accn,
                    "form": "10-Q",
                    "fy": year,
                    "fp": fp,
                    "start": start_s,
                    "end": end_s,
                    "filed": acc_ts[:10],
                    "frame": f"CY{year}{fp}",
                }
            )
            if fp == "Q3":
                rev_rows.append(
                    {
                        "val": ytd_sum,
                        "accn": accn,
                        "form": "10-Q",
                        "fy": year,
                        "fp": "Q3",
                        "start": f"{year}-01-01",
                        "end": end_s,
                        "filed": acc_ts[:10],
                    }
                )
            eps_rows.append(
                {
                    "val": Decimal("1.00") + Decimal(year - start_year) * Decimal("0.10"),
                    "accn": accn,
                    "form": "10-Q",
                    "fy": year,
                    "fp": fp,
                    "start": start_s,
                    "end": end_s,
                    "filed": acc_ts[:10],
                    "frame": f"CY{year}{fp}",
                }
            )
            dei_rows.append(
                {
                    "val": 1_000_000 + seq,
                    "accn": accn,
                    "form": "10-Q",
                    "fy": year,
                    "fp": fp,
                    "end": acc_ts[:10],
                    "filed": acc_ts[:10],
                }
            )
        fy_accn = f"{cik}-{(year + 1) % 100:02d}-{seq:06d}"
        seq += 1
        fy_acc_ts = f"{year + 1}-02-15T16:00:00.000Z"
        q4_val = 130 + year - start_year
        target.append(_filing_row(fy_accn, "10-K", f"{year}-12-31", fy_acc_ts))
        rev_rows.append(
            {
                "val": ytd_sum + q4_val,
                "accn": fy_accn,
                "form": "10-K",
                "fy": year,
                "fp": "FY",
                "start": f"{year}-01-01",
                "end": f"{year}-12-31",
                "filed": fy_acc_ts[:10],
                "frame": f"CY{year}",
            }
        )
        dei_rows.append(
            {
                "val": 1_050_000 + seq,
                "accn": fy_accn,
                "form": "10-K",
                "fy": year,
                "fp": "FY",
                "end": fy_acc_ts[:10],
                "filed": fy_acc_ts[:10],
            }
        )

    history_map: dict[str, bytes] = {}
    file_refs: list[dict[str, object]] = []
    if hist_filings:
        hist_name = f"CIK{cik}-submissions-001.json"
        hist_dates = [str(r["filingDate"]) for r in hist_filings]
        file_refs.append(
            {
                "name": hist_name,
                "filingCount": len(hist_filings),
                "filingFrom": min(hist_dates),
                "filingTo": max(hist_dates),
            }
        )
        history_map[hist_name] = json.dumps(_columns_dict(hist_filings)).encode("utf-8")

    cf_body = json.dumps(
        {
            "cik": int(cik),
            "entityName": "MultiYear Corp",
            "facts": {
                "us-gaap": {
                    "Revenues": {"units": {"USD": rev_rows}},
                    "EarningsPerShareDiluted": {"units": {"USD/shares": eps_rows}},
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {"units": {"shares": dei_rows}},
                },
            },
        },
        default=lambda v: float(v) if isinstance(v, Decimal) else v,
    ).encode("utf-8")
    root_body = _make_root_body(cik, recent_filings, tickers=[], files=file_refs)
    return cf_body, root_body, history_map


def test_parse_ifrs_only_and_dei_preserves_namespace_and_native_currencies() -> None:
    accn_20f = f"{_CIK}-26-000001"
    accn_40f = f"{_CIK}-25-000001"
    payload = {
        "cik": int(_CIK),
        "entityName": "Foreign IFRS FPI",
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "EUR": [
                            {
                                "val": 28_000_000_000,
                                "accn": accn_20f,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "frame": "CY2025",
                            }
                        ],
                        "TWD": [
                            {
                                "val": 900_000_000_000,
                                "accn": accn_40f,
                                "form": "40-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-02-20",
                            }
                        ],
                    }
                },
                "ProfitLossFromOperatingActivities": {
                    "units": {
                        "EUR": [
                            {
                                "val": 9_000_000_000,
                                "accn": accn_20f,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                            }
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "EUR": [
                            {
                                "val": 42_000_000_000,
                                "accn": accn_20f,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                            }
                        ]
                    }
                },
                "Borrowings": {
                    "units": {
                        "EUR": [
                            {
                                "val": 4_500_000_000,
                                "accn": accn_20f,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                            }
                        ]
                    }
                },
                "DilutedEarningsLossPerShare": {
                    "units": {
                        "EUR/shares": [
                            {
                                "val": Decimal("19.25"),
                                "accn": accn_20f,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-10-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "frame": "CY2025Q4",
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
                                "val": 393_200_000,
                                "accn": accn_20f,
                                "form": "20-F",
                                "fy": None,
                                "fp": None,
                                "end": "2026-02-12",
                                "filed": "2026-02-20",
                            }
                        ]
                    }
                }
            },
        },
    }
    raw = json.dumps(payload, default=lambda v: float(v) if isinstance(v, Decimal) else v).encode()
    parsed = companyfacts.parse_sec_companyfacts_payload(raw, _CIK)
    by_tag = {(f.namespace, f.tag, f.unit): f for f in parsed}
    assert ("ifrs-full", "Revenue", "EUR") in by_tag
    assert ("ifrs-full", "Revenue", "TWD") in by_tag
    assert ("ifrs-full", "ProfitLossFromOperatingActivities", "EUR") in by_tag
    assert ("ifrs-full", "Assets", "EUR") in by_tag
    assert by_tag[("ifrs-full", "Assets", "EUR")].start is None
    assert ("ifrs-full", "Borrowings", "EUR") in by_tag
    assert by_tag[("ifrs-full", "Borrowings", "EUR")].start is None
    dei = by_tag[("dei", "EntityCommonStockSharesOutstanding", "shares")]
    assert dei.start is None
    assert dei.end == date(2026, 2, 12)
    assert dei.value == Decimal(393_200_000)

    # Project discrete Q4 EPS in EUR without coercing to USD
    filing = companyfacts.SecCompanyFactsFiling(
        accn_20f, "20-F", date(2025, 12, 31), datetime(2026, 2, 20, 14, tzinfo=UTC)
    )
    slots = companyfacts.project_sec_companyfacts_quarters(
        parsed, {accn_20f: filing}, "a" * 64, requested_periods=((2025, 4),)
    )
    eps = slots[0].field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert eps.value == Decimal("19.25")
    assert eps.unit == "EUR/shares"
    assert isinstance(eps.evidence[0], companyfacts.SecCanonicalFactEvidence)
    assert eps.evidence[0].namespace == "ifrs-full"


def test_parse_rejects_missing_both_taxonomies_and_malformed_namespace() -> None:
    only_dei = json.dumps({"cik": 123, "facts": {"dei": {}}}).encode()
    with pytest.raises(SchemaMismatchError, match="us-gaap or ifrs-full"):
        companyfacts.parse_sec_companyfacts_payload(only_dei, _CIK)
    bad_ifrs = json.dumps({"cik": 123, "facts": {"us-gaap": {}, "ifrs-full": []}}).encode()
    with pytest.raises(SchemaMismatchError, match="ifrs-full"):
        companyfacts.parse_sec_companyfacts_payload(bad_ifrs, _CIK)
    bad_dei_fy = json.dumps(
        {
            "cik": 123,
            "facts": {
                "us-gaap": {},
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "units": {
                            "shares": [
                                {
                                    "val": 100,
                                    "accn": f"{_CIK}-25-000001",
                                    "form": "10-K",
                                    "fy": "bad",
                                    "fp": "FY",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-01",
                                }
                            ]
                        }
                    }
                },
            },
        }
    ).encode()
    with pytest.raises(SchemaMismatchError, match="fiscal or accession"):
        companyfacts.parse_sec_companyfacts_payload(bad_dei_fy, _CIK)


def test_uncapped_history_depth_year_bounds_and_validation(monkeypatch, tmp_path) -> None:
    cf_body, root_body, history_map = _build_multi_year_fixtures(
        _CIK, 2014, 2025, split_history_year=2019
    )
    store = SnapshotStore(tmp_path)
    root_ref = store.observe(
        RequestSpec("sec", "edgar_submissions", {"cik": _CIK}),
        root_body,
        _AT,
        "sec-submissions-json-v1",
        SnapshotMode.APPEND,
    )
    root = SecDiscoverySource(f"https://data.sec.gov/submissions/CIK{_CIK}.json", root_ref)
    monkeypatch.setattr(companyfacts, "fetch_sec_submissions_root", lambda *args, **kwargs: root)
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())

    class Response:
        def __init__(self, url: str, data: bytes) -> None:
            self.url = url
            self.body = io.BytesIO(data)

    cf_url = companyfacts.SEC_COMPANYFACTS_URL.format(cik=_CIK)

    def fake_open(url: str, **kwargs: Any) -> Response:
        if url == cf_url:
            return Response(url, cf_body)
        basename = url.rsplit("/", 1)[-1]
        if basename in history_map:
            return Response(url, history_map[basename])
        raise AssertionError(f"unexpected URL {url}")

    client.open = fake_open  # type: ignore[method-assign]

    # 1. Default count=8 backward compatibility
    default_res = companyfacts.fetch_sec_canonical_quarters_by_cik(
        123, client, store, utc_now=lambda: _AT
    )
    assert default_res.ticker == f"CIK{_CIK}"
    assert default_res.cik == _CIK
    assert default_res.coverage_complete and default_res.periods_resolved
    assert len(default_res.slots) == 8
    assert (default_res.slots[0].fiscal_year, default_res.slots[0].fiscal_quarter) == (2024, 1)
    assert (default_res.slots[-1].fiscal_year, default_res.slots[-1].fiscal_quarter) == (2025, 4)
    assert default_res.taxonomy == "us-gaap"
    assert default_res.reporting_currency == "USD"
    assert len(default_res.dei_shares_outstanding) > 0

    # 2. Uncapped count=40 (10 years) pulls historical submissions page seamlessly
    ten_yr = companyfacts.fetch_sec_canonical_quarters_by_cik(
        "123", client, store, ticker="DELIST", utc_now=lambda: _AT, count=40
    )
    assert ten_yr.ticker == "DELIST"
    assert ten_yr.coverage_complete and ten_yr.periods_resolved
    assert len(ten_yr.slots) == 40
    assert (ten_yr.slots[0].fiscal_year, ten_yr.slots[0].fiscal_quarter) == (2016, 1)
    assert (ten_yr.slots[-1].fiscal_year, ten_yr.slots[-1].fiscal_quarter) == (2025, 4)
    q4_2016 = next(s for s in ten_yr.slots if (s.fiscal_year, s.fiscal_quarter) == (2016, 4))
    assert q4_2016.field(companyfacts.SecCanonicalMetric.REVENUE).value == Decimal(132)

    # 3. count=None returns full 12-year history (48 quarters)
    full = companyfacts.fetch_sec_canonical_quarters_by_cik(
        _CIK, client, store, utc_now=lambda: _AT, count=None
    )
    assert full.coverage_complete and full.periods_resolved
    assert len(full.slots) == 48
    assert (full.slots[0].fiscal_year, full.slots[0].fiscal_quarter) == (2014, 1)

    # 4. start_year / end_year windowing
    windowed = companyfacts.fetch_sec_canonical_quarters_by_cik(
        _CIK,
        client,
        store,
        utc_now=lambda: _AT,
        count=None,
        start_year=2018,
        end_year=2021,
    )
    assert windowed.coverage_complete and windowed.periods_resolved
    assert len(windowed.slots) == 16
    assert (windowed.slots[0].fiscal_year, windowed.slots[0].fiscal_quarter) == (2018, 1)
    assert (windowed.slots[-1].fiscal_year, windowed.slots[-1].fiscal_quarter) == (2021, 4)

    # 5. Strict PIT acceptance_upper zero look-ahead
    pit = companyfacts.fetch_sec_canonical_quarters_by_cik(
        _CIK,
        client,
        store,
        utc_now=lambda: _AT,
        count=None,
        acceptance_upper=datetime(2020, 12, 31, 23, 59, 59, tzinfo=UTC),
    )
    assert pit.coverage_complete and pit.periods_resolved
    assert (pit.slots[-1].fiscal_year, pit.slots[-1].fiscal_quarter) == (2020, 3)

    # 6. Invalid argument validation
    for bad_count in (0, -1, True):
        with pytest.raises(ValueError, match="count"):
            companyfacts.fetch_sec_canonical_quarters_by_cik(
                _CIK,
                client,
                store,
                count=bad_count,  # type: ignore[arg-type]
            )
    with pytest.raises(ValueError, match="start_year"):
        companyfacts.fetch_sec_canonical_quarters_by_cik(
            _CIK, client, store, start_year=2025, end_year=2024
        )
    for bad_cik in (0, -5, "abc", "12345678901", True):
        with pytest.raises(ValueError, match="CIK"):
            companyfacts.fetch_sec_canonical_quarters_by_cik(
                bad_cik,
                client,
                store,  # type: ignore[arg-type]
            )


def test_cik_direct_investment_and_transient_fail_closed(monkeypatch, tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    root_body = _make_root_body(_CIK, [], entity_type="investment", tickers=["FUND"])
    root_ref = store.observe(
        RequestSpec("sec", "edgar_submissions", {"cik": _CIK}),
        root_body,
        _AT,
        "sec-submissions-json-v1",
        SnapshotMode.APPEND,
    )
    root = SecDiscoverySource(f"https://data.sec.gov/submissions/CIK{_CIK}.json", root_ref)
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())

    monkeypatch.setattr(companyfacts, "fetch_sec_submissions_root", lambda *args, **kwargs: root)
    inv = companyfacts.fetch_sec_canonical_quarters_by_cik(_CIK, client, store, utc_now=lambda: _AT)
    assert inv.eligibility.status is SecCompanyEligibilityStatus.NON_COMPANY
    assert inv.ticker == "FUND"
    assert inv.slots == ()

    monkeypatch.setattr(
        companyfacts,
        "fetch_sec_submissions_root",
        lambda *args, **kwargs: (_ for _ in ()).throw(TransientProviderError("timeout")),
    )
    failed = companyfacts.fetch_sec_canonical_quarters_by_cik(
        _CIK, client, store, utc_now=lambda: _AT
    )
    assert failed.eligibility.status is SecCompanyEligibilityStatus.TRANSIENT_FAILURE
    assert failed.slots == ()


def test_fetch_by_ticker_accepts_20f_ifrs_operating_fpi(monkeypatch, tmp_path) -> None:
    accn_20f = f"{_CIK}-26-000001"
    root_body = _make_root_body(
        _CIK,
        [_filing_row(accn_20f, "20-F", "2025-12-31", "2026-02-20T14:00:00.000Z")],
        entity_type="operating",
        tickers=["FPI"],
    )
    cf_body = json.dumps(
        {
            "cik": int(_CIK),
            "entityName": "FPI Corp",
            "facts": {
                "ifrs-full": {
                    "Revenue": {
                        "units": {
                            "EUR": [
                                {
                                    "val": 5000,
                                    "accn": accn_20f,
                                    "form": "20-F",
                                    "fy": 2025,
                                    "fp": "FY",
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-20",
                                }
                            ]
                        }
                    },
                    "DilutedEarningsLossPerShare": {
                        "units": {
                            "EUR/shares": [
                                {
                                    "val": 2.5,
                                    "accn": accn_20f,
                                    "form": "20-F",
                                    "fy": 2025,
                                    "fp": "FY",
                                    "start": "2025-10-01",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-20",
                                    "frame": "CY2025Q4",
                                }
                            ]
                        }
                    },
                }
            },
        }
    ).encode()
    store = SnapshotStore(tmp_path)
    map_ref = store.observe(
        RequestSpec("sec", "ticker_cik", {}), b"{}", _AT, "map-v1", SnapshotMode.APPEND
    )
    mapping = SecTickerCikMapping(
        MappingProxyType({"FPI": (_CIK, "FPI Corp")}),
        "https://www.sec.gov/files/company_tickers.json",
        map_ref,
    )
    root_ref = store.observe(
        RequestSpec("sec", "edgar_submissions", {"cik": _CIK}),
        root_body,
        _AT,
        "sec-submissions-json-v1",
        SnapshotMode.APPEND,
    )
    root = SecDiscoverySource(f"https://data.sec.gov/submissions/CIK{_CIK}.json", root_ref)
    monkeypatch.setattr(
        companyfacts, "fetch_sec_ticker_cik_mapping", lambda *args, **kwargs: mapping
    )
    monkeypatch.setattr(companyfacts, "fetch_sec_submissions_root", lambda *args, **kwargs: root)
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())

    class Response:
        url = companyfacts.SEC_COMPANYFACTS_URL.format(cik=_CIK)

        def __init__(self) -> None:
            self.body = io.BytesIO(cf_body)

    client.open = lambda *args, **kwargs: Response()  # type: ignore[method-assign]
    res = companyfacts.fetch_sec_canonical_quarters(
        "FPI", client, store, utc_now=lambda: _AT, count=4
    )
    assert res.eligibility.status is SecCompanyEligibilityStatus.SEC_COMPANY
    assert res.taxonomy == "ifrs-full"
    assert res.reporting_currency == "EUR"
    q4 = res.slots[-1]
    assert (q4.fiscal_year, q4.fiscal_quarter) == (2025, 4)
    eps = q4.field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert eps.value == Decimal("2.5")
    assert eps.unit == "EUR/shares"


def test_enrich_8k_q4_eps_chunks_over_eight_missing_q4s_and_rejects_tickerless_cik(
    monkeypatch, tmp_path
) -> None:
    from ohmydata.providers.sec import earnings_8k

    cf_body, root_body, history_map = _build_multi_year_fixtures(
        _CIK, 2014, 2025, split_history_year=2019
    )
    store = SnapshotStore(tmp_path)
    root_ref = store.observe(
        RequestSpec("sec", "edgar_submissions", {"cik": _CIK}),
        root_body,
        _AT,
        "sec-submissions-json-v1",
        SnapshotMode.APPEND,
    )
    root = SecDiscoverySource(f"https://data.sec.gov/submissions/CIK{_CIK}.json", root_ref)
    monkeypatch.setattr(companyfacts, "fetch_sec_submissions_root", lambda *args, **kwargs: root)
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())

    class Response:
        def __init__(self, url: str, data: bytes) -> None:
            self.url = url
            self.body = io.BytesIO(data)

    cf_url = companyfacts.SEC_COMPANYFACTS_URL.format(cik=_CIK)

    def fake_open(url: str, **kwargs: Any) -> Response:
        if url == cf_url:
            return Response(url, cf_body)
        basename = url.rsplit("/", 1)[-1]
        if basename in history_map:
            return Response(url, history_map[basename])
        raise AssertionError(f"unexpected URL {url}")

    client.open = fake_open  # type: ignore[method-assign]

    # 1. Delisted/tickerless CIK rejects enrich_8k_q4_eps=True explicitly
    with pytest.raises(ValueError, match="enrich_8k_q4_eps requires a real ticker"):
        companyfacts.fetch_sec_canonical_quarters_by_cik(
            _CIK, client, store, utc_now=lambda: _AT, count=None, enrich_8k_q4_eps=True
        )

    # 2. When real ticker is supplied and 12 Q4s lack EPS, chunks calls into <= 8 quarters
    seen_chunks: list[tuple[tuple[int, int, date], ...]] = []

    def fake_fetch_8k(ticker: str, *args: Any, target_quarters: tuple[Any, ...], **kwargs: Any):
        assert ticker == "DELIST"
        assert 1 <= len(target_quarters) <= 8
        seen_chunks.append(target_quarters)
        return object()

    monkeypatch.setattr(earnings_8k, "fetch_sec_8k_quarterly_eps", fake_fetch_8k)
    monkeypatch.setattr(
        earnings_8k,
        "enrich_sec_canonical_quarters_with_8k_eps",
        lambda res, eps_res: res,
    )
    enriched = companyfacts.fetch_sec_canonical_quarters_by_cik(
        _CIK,
        client,
        store,
        ticker="DELIST",
        utc_now=lambda: _AT,
        count=None,
        enrich_8k_q4_eps=True,
    )
    assert len(enriched.slots) == 48
    assert [len(chunk) for chunk in seen_chunks] == [8, 4]


def test_incomplete_xbrl_amendments_detects_missing_ifrs_metric_class() -> None:
    base = f"{_CIK}-26-000010"
    amended = f"{_CIK}-26-000011"
    filings = {
        base: companyfacts.SecCompanyFactsFiling(
            base, "10-Q", date(2026, 6, 30), datetime(2026, 8, 1, 16, tzinfo=UTC)
        ),
        amended: companyfacts.SecCompanyFactsFiling(
            amended, "10-Q/A", date(2026, 6, 30), datetime(2026, 8, 3, 16, tzinfo=UTC)
        ),
    }
    base_rev = companyfacts.SecCompanyFactsFact(
        "Revenue",
        "EUR",
        Decimal(500),
        date(2026, 4, 1),
        date(2026, 6, 30),
        2026,
        "Q2",
        "10-Q",
        date(2026, 8, 1),
        base,
        None,
        namespace="ifrs-full",
    )
    base_eps = companyfacts.SecCompanyFactsFact(
        "DilutedEarningsLossPerShare",
        "EUR/shares",
        Decimal("1.20"),
        date(2026, 4, 1),
        date(2026, 6, 30),
        2026,
        "Q2",
        "10-Q",
        date(2026, 8, 1),
        base,
        None,
        namespace="ifrs-full",
    )
    amended_rev = companyfacts.SecCompanyFactsFact(
        "Revenue",
        "EUR",
        Decimal(500),
        date(2026, 4, 1),
        date(2026, 6, 30),
        2026,
        "Q2",
        "10-Q/A",
        date(2026, 8, 3),
        amended,
        None,
        namespace="ifrs-full",
    )
    periods, accessions = companyfacts._incomplete_xbrl_amendments(
        (base_rev, base_eps, amended_rev), filings, None, {amended}
    )
    assert periods == frozenset({(2026, 2)})
    assert accessions == (amended,)


def test_parse_skips_unfocused_non_dei_rows_and_normalizes_dei_focus() -> None:
    accn_valid = f"{_CIK}-25-000001"
    accn_null_fy = f"{_CIK}-20-000002"
    accn_zero_blank = f"{_CIK}-10-000003"
    accn_excel_fy = f"{_CIK}-19-000004"
    accn_both_null = f"{_CIK}-21-000005"
    accn_cy_fp = f"{_CIK}-15-000006"
    accn_ifrs_null = f"{_CIK}-24-000007"
    accn_ifrs_valid = f"{_CIK}-25-000008"

    payload = {
        "cik": int(_CIK),
        "entityName": "Synthetic Focus Edge Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 500,
                                "accn": accn_valid,
                                "form": "10-K",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-02-20",
                            },
                            {
                                "val": 100,
                                "accn": accn_null_fy,
                                "form": "10-Q",
                                "fy": None,
                                "fp": "Q1",
                                "start": "2020-01-01",
                                "end": "2020-03-31",
                                "filed": "2020-05-08",
                            },
                            {
                                "val": 110,
                                "accn": accn_zero_blank,
                                "form": "10-Q",
                                "fy": 0,
                                "fp": "",
                                "start": "2009-10-01",
                                "end": "2009-12-31",
                                "filed": "2010-02-09",
                            },
                            {
                                "val": 120,
                                "accn": accn_excel_fy,
                                "form": "10-Q",
                                "fy": 43646,
                                "fp": "Q2",
                                "start": "2019-04-01",
                                "end": "2019-06-30",
                                "filed": "2019-07-25",
                            },
                            {
                                "val": 130,
                                "accn": accn_both_null,
                                "form": "10-Q",
                                "fy": None,
                                "fp": None,
                                "start": "2021-07-01",
                                "end": "2021-09-30",
                                "filed": "2021-11-03",
                            },
                            {
                                "val": 140,
                                "accn": accn_cy_fp,
                                "form": "40-F",
                                "fy": 2014,
                                "fp": "CY",
                                "start": "2014-01-01",
                                "end": "2014-12-31",
                                "filed": "2015-03-24",
                            },
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "val": 900,
                                "accn": accn_cy_fp,
                                "form": "40-F",
                                "fy": 2014,
                                "fp": "CY",
                                "end": "2014-12-31",
                                "filed": "2015-03-24",
                            }
                        ]
                    }
                },
            },
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "EUR": [
                            {
                                "val": 700,
                                "accn": accn_ifrs_valid,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-03-28",
                            },
                            {
                                "val": 650,
                                "accn": accn_ifrs_null,
                                "form": "20-F",
                                "fy": None,
                                "fp": None,
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "filed": "2024-03-28",
                            },
                        ]
                    }
                }
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "val": 10_000,
                                "accn": accn_zero_blank,
                                "form": "10-Q",
                                "fy": 0,
                                "fp": "",
                                "end": "2010-01-31",
                                "filed": "2010-02-09",
                            },
                            {
                                "val": 20_000,
                                "accn": accn_excel_fy,
                                "form": "10-Q",
                                "fy": 43646,
                                "fp": "Q2",
                                "end": "2019-07-18",
                                "filed": "2019-07-25",
                            },
                            {
                                "val": 30_000,
                                "accn": accn_cy_fp,
                                "form": "40-F",
                                "fy": 2014,
                                "fp": "CY",
                                "end": "2014-12-31",
                                "filed": "2015-03-24",
                            },
                        ]
                    }
                }
            },
        },
    }
    parsed = companyfacts.parse_sec_companyfacts_payload(json.dumps(payload).encode(), _CIK)
    non_dei = [f for f in parsed if f.namespace != "dei"]
    dei = {f.accn: f for f in parsed if f.namespace == "dei"}

    assert [(f.namespace, f.accn, f.fy, f.fp, f.value) for f in non_dei] == [
        ("us-gaap", accn_valid, 2024, "FY", Decimal(500)),
        ("ifrs-full", accn_ifrs_valid, 2024, "FY", Decimal(700)),
    ]
    assert set(dei) == {accn_zero_blank, accn_excel_fy, accn_cy_fp}
    assert (dei[accn_zero_blank].fy, dei[accn_zero_blank].fp) == (None, None)
    assert (dei[accn_excel_fy].fy, dei[accn_excel_fy].fp) == (None, "Q2")
    assert (dei[accn_cy_fp].fy, dei[accn_cy_fp].fp) == (2014, None)


def test_parse_still_rejects_structural_corruption_and_wrong_focus_types() -> None:
    accn = f"{_CIK}-25-000001"
    base_row: dict[str, object] = {
        "val": 100,
        "accn": accn,
        "form": "10-Q",
        "fy": 2025,
        "fp": "Q1",
        "start": "2025-01-01",
        "end": "2025-03-31",
        "filed": "2025-05-01",
    }

    def _wrap(row: dict[str, object]) -> bytes:
        return json.dumps(
            {"cik": int(_CIK), "facts": {"us-gaap": {"Revenues": {"units": {"USD": [row]}}}}}
        ).encode()

    # Missing fy or fp keys on non-DEI rows fail explicitly
    no_fy = {k: v for k, v in base_row.items() if k != "fy"}
    with pytest.raises(SchemaMismatchError, match="fiscal or accession"):
        companyfacts.parse_sec_companyfacts_payload(_wrap(no_fy), _CIK)

    # Wrong fy/fp types and negative fy fail explicitly
    for bad_fields in (
        {"fy": "bad"},
        {"fy": "2025"},
        {"fy": True},
        {"fy": False},
        {"fy": -1},
        {"fy": 2025.5},
        {"fp": 123},
        {"fp": True},
        {"accn": "bad-accn"},
    ):
        with pytest.raises(SchemaMismatchError, match="fiscal or accession"):
            companyfacts.parse_sec_companyfacts_payload(_wrap({**base_row, **bad_fields}), _CIK)

    # Structural defects on unfocused rows still raise SchemaMismatchError (never silently skipped)
    with pytest.raises(SchemaMismatchError, match="duration is reversed"):
        companyfacts.parse_sec_companyfacts_payload(
            _wrap({**base_row, "fy": None, "fp": None, "start": "2025-04-01", "end": "2025-03-31"}),
            _CIK,
        )
    with pytest.raises(SchemaMismatchError, match="frame is invalid"):
        companyfacts.parse_sec_companyfacts_payload(
            _wrap({**base_row, "fy": 0, "fp": "", "frame": "BADFRAME"}),
            _CIK,
        )
    with pytest.raises(SchemaMismatchError, match="fact value is invalid"):
        companyfacts.parse_sec_companyfacts_payload(
            _wrap({**base_row, "fy": None, "fp": None, "val": "not-a-number"}),
            _CIK,
        )
