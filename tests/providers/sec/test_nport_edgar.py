import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ohmydata.providers.sec import SecNportQuarterRequest
from ohmydata.providers.sec.artifacts import SecArtifactStore
from ohmydata.providers.sec.edgar import (
    SecPayloadReceipt,
    historical_basenames,
    parse_submissions,
    resolve_submissions,
)
from ohmydata.providers.sec.errors import CoverageError, SchemaMismatchError
from ohmydata.providers.sec.nport import (
    SecAvailabilityPolicy,
    SecFundHoldingVintage,
    SecHoldingVintageSet,
    extract_from_artifact,
    parse_sec_date,
    read_member,
)


@pytest.mark.parametrize(
    "token, month",
    [
        (m, i)
        for i, m in enumerate(
            ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
        )
    ],
)
def test_sec_date_ascii_months(token: str, month: int) -> None:
    assert parse_sec_date(f"29-{token.lower()}-2024") == date(2024, month, 29)


def test_sec_date_rejects_unicode_whitespace_and_calendar_errors() -> None:
    for value in (
        " 29-JAN-2024",
        "29-JAN-2024 ",
        "29-JAN-24",
        "29-FOO-2024",
        "30-FEB-2024",
        "٢٩-JAN-2024",
    ):
        with pytest.raises(SchemaMismatchError):
            parse_sec_date(value)


def test_classification_value_is_native_string() -> None:
    rows = list(
        read_member(
            io.BytesIO(b"FAIR_VALUE_LEVEL\tBALANCE\nN/A\t2.5\n"), table="FUND_REPORTED_HOLDING"
        )
    )
    assert rows[0]["FAIR_VALUE_LEVEL"] == "N/A"
    assert rows[0]["BALANCE"] == Decimal("2.5")


@pytest.mark.parametrize(
    "value", ["NaN", "sNaN", "Infinity", "-Infinity", "1e3", " 1", "1 ", "1_000", "١"]
)
def test_numeric_fields_reject_non_fixed_point(value: str) -> None:
    with pytest.raises(SchemaMismatchError, match="FUND_REPORTED_HOLDING.BALANCE"):
        list(read_member(io.BytesIO(f"BALANCE\n{value}\n".encode()), table="FUND_REPORTED_HOLDING"))


@pytest.mark.parametrize("value", ["-.0091723714", "0", "-100", "1.25"])
def test_numeric_fields_accept_sec_fixed_point(value: str) -> None:
    row: dict[str, Any] = next(
        iter(read_member(io.BytesIO(f"BALANCE\n{value}\n".encode()), table="FUND_REPORTED_HOLDING"))
    )
    assert row["BALANCE"] == Decimal(value)


@pytest.mark.parametrize(
    "table", ["FUND_REPORTED_INFO", "SUBMISSION", "REGISTRANT", "FUND_REPORTED_HOLDING"]
)
@pytest.mark.parametrize(
    "value", ["0000000001-000001-000001", "٠000000001-24-000001", "0000000001-24-000001 "]
)
def test_all_nport_accession_columns_are_strict_ascii(table: str, value: str) -> None:
    with pytest.raises(SchemaMismatchError, match="accession"):
        list(read_member(io.BytesIO(f"ACCESSION_NUMBER\n{value}\n".encode()), table=table))


def _zip(
    tmp_path: Path, *, extra_holdings: tuple[str, ...] = (), extra_after: bool = False
) -> Path:
    rows = {
        "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tSUB_TYPE\tREPORT_ENDING_PERIOD\tREPORT_DATE\tIS_LAST_FILING\n0000000001-24-000001\t01-MAY-2024\tNPORT-P\t31-MAR-2024\t31-MAR-2024\tY\n",
        "REGISTRANT.tsv": "ACCESSION_NUMBER\tCIK\tREGISTRANT_NAME\tFILE_NUM\tLEI\n0000000001-24-000001\t0000000001\tTest\t1\t\n",
        "FUND_REPORTED_INFO.tsv": "ACCESSION_NUMBER\tSERIES_ID\tSERIES_NAME\tSERIES_LEI\tTOTAL_ASSETS\tTOTAL_LIABILITIES\tNET_ASSETS\n0000000001-24-000001\tS1\tTest ETF\t\t100\t1\t99\n",
        "FUND_REPORTED_HOLDING.tsv": "ACCESSION_NUMBER\tHOLDING_ID\tISSUER_NAME\tISSUER_LEI\tISSUER_TITLE\tISSUER_CUSIP\tBALANCE\tUNIT\tOTHER_UNIT_DESC\tCURRENCY_CODE\tCURRENCY_VALUE\tEXCHANGE_RATE\tPERCENTAGE\tPAYOFF_PROFILE\tASSET_CAT\tOTHER_ASSET\tISSUER_TYPE\tOTHER_ISSUER\tINVESTMENT_COUNTRY\tIS_RESTRICTED_SECURITY\tFAIR_VALUE_LEVEL\tDERIVATIVE_CAT\n"
        + "\n".join(
            (
                "0000000001-24-000001\tH1\tIssuer\t\t\tCUS\t-2.5\tSH\t\tUSD\t\t\t-.5\t\tEC\t\tCORP\t\tUS\tN\t1\t",
                *extra_holdings,
            )
            if extra_after
            else (
                *extra_holdings,
                "0000000001-24-000001\tH1\tIssuer\t\t\tCUS\t-2.5\tSH\t\tUSD\t\t\t-.5\t\tEC\t\tCORP\t\tUS\tN\t1\t",
            )
        )
        + "\n",
        "IDENTIFIERS.tsv": "HOLDING_ID\tIDENTIFIERS_ID\tIDENTIFIER_ISIN\tIDENTIFIER_TICKER\tOTHER_IDENTIFIER\tOTHER_IDENTIFIER_DESC\nH1\tI1\t\tABC\t\t\n",
    }
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, content in rows.items():
            z.writestr(name, content)
    return path


def test_extract_preserves_decimal_negative_and_identifier(tmp_path: Path) -> None:
    req = SecNportQuarterRequest(
        2024, 2, ("S1",), ("S1",), utc_now=lambda: datetime(2024, 8, 1, tzinfo=UTC)
    )
    source = _zip(tmp_path)
    store = SecArtifactStore(tmp_path / "artifacts")
    ref = store.publish(
        io.BytesIO(source.read_bytes()),
        year=2024,
        quarter=2,
        source_url=req.source_url,
    )
    result = extract_from_artifact(store, ref, req, observed_at=datetime(2024, 6, 1, tzinfo=UTC))
    vintage = result.vintages[0]
    assert vintage.holdings[0]["BALANCE"] == Decimal("-2.5")
    assert vintage.holdings[0]["PERCENTAGE"] == Decimal("-.5")
    assert vintage.holdings[0]["__native__"]["PERCENTAGE"] == "-.5"
    assert vintage.identifiers[0]["IDENTIFIER_TICKER"] == "ABC"


def test_extract_selected_row_cap_fails_before_consuming_next_holding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ohmydata.providers.sec import nport

    extra = tuple(
        f"0000000001-24-000001\tH{i}\tIssuer {i}\t\t\tCUS{i}\t1\tSH\t\tUSD\t\t\t.1\t\tEC\t\tCORP\t\tUS\tN\t1\t"
        for i in (2, 3, 4)
    )
    source = _zip(tmp_path, extra_holdings=extra, extra_after=True)
    store = SecArtifactStore(tmp_path / "artifacts")
    req = SecNportQuarterRequest(
        2024, 2, ("S1",), ("S1",), utc_now=lambda: datetime(2024, 8, 1, tzinfo=UTC)
    )
    ref = store.publish(
        io.BytesIO(source.read_bytes()), year=2024, quarter=2, source_url=req.source_url
    )
    original = nport.read_member
    consumed = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal consumed
        rows = original(*args, **kwargs)
        if kwargs.get("table") != "FUND_REPORTED_HOLDING":
            return rows

        def stream() -> Any:
            nonlocal consumed
            for row in rows:
                consumed += 1
                yield row

        return stream()

    monkeypatch.setattr(nport, "read_member", counted)
    with pytest.raises(CoverageError, match="selected output row limit exceeded"):
        extract_from_artifact(store, ref, req, max_selected_rows=2)
    assert consumed == 2


@pytest.mark.parametrize(
    ("extra", "after", "raises"),
    [
        (
            (
                "0000000002-24-000001\tH1\tOther\t\t\tCUS2\t1\tSH\t\tUSD\t\t\t.1\t\tEC\t\tCORP\t\tUS\tN\t1\t",
            ),
            False,
            True,
        ),
        (
            (
                "0000000002-24-000001\tH1\tOther\t\t\tCUS2\t1\tSH\t\tUSD\t\t\t.1\t\tEC\t\tCORP\t\tUS\tN\t1\t",
            ),
            True,
            True,
        ),
        (
            (
                "0000000002-24-000001\tH2\tOther\t\t\tCUS2\t1\tSH\t\tUSD\t\t\t.1\t\tEC\t\tCORP\t\tUS\tN\t1\t",
            ),
            False,
            False,
        ),
        (
            (
                "0000000002-24-000001\tH9\tOther\t\t\tCUS2\t1\tSH\t\tUSD\t\t\t.1\t\tEC\t\tCORP\t\tUS\tN\t1\t",
                "0000000003-24-000001\tH9\tOther\t\t\tCUS3\t1\tSH\t\tUSD\t\t\t.1\t\tEC\t\tCORP\t\tUS\tN\t1\t",
            ),
            False,
            False,
        ),
    ],
)
def test_cross_accession_holding_id_index(
    tmp_path: Path, extra: tuple[str, ...], after: bool, raises: bool
) -> None:
    req = SecNportQuarterRequest(
        2024, 2, ("S1",), ("S1",), utc_now=lambda: datetime(2024, 8, 1, tzinfo=UTC)
    )
    source = _zip(tmp_path, extra_holdings=extra, extra_after=after)
    store = SecArtifactStore(tmp_path / "artifacts")
    ref = store.publish(
        io.BytesIO(source.read_bytes()), year=2024, quarter=2, source_url=req.source_url
    )
    if raises:
        with pytest.raises(CoverageError):
            extract_from_artifact(store, ref, req)
    else:
        assert extract_from_artifact(store, ref, req).vintages[0].holdings[0]["HOLDING_ID"] == "H1"


def test_edgar_exact_coverage_and_historical_basename() -> None:
    row = {
        k: [v]
        for k, v in {
            "accessionNumber": "0000000001-24-000001",
            "form": "NPORT-P",
            "filingDate": "2024-05-01",
            "reportDate": "2024-03-31",
            "primaryDocument": "x.htm",
            "acceptanceDateTime": "20240501120000",
        }.items()
    }
    payload = {
        "cik": "0000000001",
        "filings": {"recent": row, "files": [{"name": "CIK0000000001-submissions-001.json"}]},
    }
    assert (
        parse_submissions(payload, "0000000001", ("0000000001-24-000001",))["0000000001-24-000001"][
            "form"
        ]
        == "NPORT-P"
    )
    assert historical_basenames(payload, "0000000001") == ("CIK0000000001-submissions-001.json",)
    with pytest.raises(CoverageError):
        parse_submissions(payload, "0000000001", ("0000000001-24-000002",))


def test_edgar_historical_loader_accounts_exact_wire_bytes() -> None:
    accession = "0000000001-24-000002"
    row = {
        k: [v]
        for k, v in {
            "accessionNumber": accession,
            "form": "NPORT-P",
            "filingDate": "2024-05-01",
            "reportDate": "2024-03-31",
            "primaryDocument": "x.htm",
            "acceptanceDateTime": "20240501120000",
        }.items()
    }
    parent = {
        "cik": "0000000001",
        "filings": {
            "recent": {
                k: ["0000000001-24-000001"] if k == "accessionNumber" else [v[0]]
                for k, v in row.items()
            },
            "files": [{"name": "CIK0000000001-submissions-001.json"}],
        },
    }
    child: dict[str, Any] = {"cik": "0000000001", "filings": {"recent": row, "files": []}}
    import json

    raw = json.dumps(child).encode()
    result = resolve_submissions(
        parent, "0000000001", (accession,), load=lambda _: SecPayloadReceipt.from_bytes(raw)
    )
    assert result[accession]["form"] == "NPORT-P"
    with pytest.raises(SchemaMismatchError):
        resolve_submissions(parent, "0000000001", (accession,), load=lambda _: child)

    # Real SEC EDGAR historical submissions files contain the raw columns dictionary directly
    raw_direct = json.dumps(row).encode()
    res_direct = resolve_submissions(
        parent, "0000000001", (accession,), load=lambda _: SecPayloadReceipt.from_bytes(raw_direct)
    )
    assert res_direct[accession]["form"] == "NPORT-P"


def test_resolver_accepted_policy_and_unknown_acceptance() -> None:
    v = SecFundHoldingVintage(
        "0000000001-24-000001",
        "0000000001",
        "S1",
        None,
        date(2024, 3, 31),
        date(2024, 5, 1),
        "NPORT-P",
        (),
        accepted_at=datetime(2024, 5, 1, tzinfo=UTC),
        observed_at=datetime(2024, 8, 1, tzinfo=UTC),
    )
    s = SecHoldingVintageSet((v,))
    resolved = s.resolve(
        "0000000001",
        "S1",
        datetime(2024, 5, 1, tzinfo=UTC),
        policy=SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1,
    )
    assert isinstance(resolved, SecFundHoldingVintage)
    assert resolved.availability_policy == SecAvailabilityPolicy.ACCEPTED_AT_PLUS_LAG_V1.value
    assert (s.resolve("0000000001", "S1", datetime(2024, 5, 1, tzinfo=UTC)) is None) is False
