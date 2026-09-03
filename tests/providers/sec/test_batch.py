from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ohmydata.providers.sec.batch import (
    AppendOnlyIndex,
    Quarter,
    SecFundSelector,
    SecScheduledFundSelector,
    canonical_hash,
    enumerate_receipts,
    partition_identity,
    publish_directory,
    quarter_range,
    receipt_hashes,
    resolve_single_series_cik,
    store_immutable_payload,
)
from ohmydata.providers.sec.errors import CoverageError, SchemaMismatchError


def test_receipt_identity_changes_for_accessions_and_payload_refs() -> None:
    base: dict[str, Any] = {
        "schema_version": "sec-fetch-receipt-v1",
        "source_quarter": "2024q2",
        "nport_artifact_sha256": "a" * 64,
        "nport_manifest_sha256": "b" * 64,
        "universe_hash": "c" * 64,
        "payloads": [{"cik": "0000000001", "payload_sha256": "d" * 64}],
        "accessions": {"0000000001": ["0000000001-24-000001"]},
    }
    closure, receipt = receipt_hashes(base)
    changed_accessions = {**base, "accessions": {"0000000001": ["0000000001-24-000002"]}}
    changed_payloads = {**base, "payloads": [{"cik": "0000000001", "payload_sha256": "e" * 64}]}
    assert receipt_hashes(changed_accessions) != (closure, receipt)
    assert receipt_hashes(changed_payloads) != (closure, receipt)


def test_quarter_bounds_and_schedule(tmp_path: Path) -> None:
    now = lambda: __import__("datetime").datetime(2026, 8, 1, tzinfo=__import__("datetime").UTC)
    assert [str(x) for x in quarter_range("2019Q4", "2020q1", now=now)] == ["2019q4", "2020q1"]
    with pytest.raises(ValueError):
        quarter_range("2019q3", "2019q4", now=now)
    with pytest.raises(ValueError):
        quarter_range("2026q3", "2026q4", now=now)
    u = tmp_path / "u.json"
    u.write_text(
        json.dumps(
            {
                "schema_version": "sec-equity-etf-universe-v1",
                "funds": [
                    {
                        "symbol": "XLK",
                        "cik": "0001064641",
                        "selection_mode": "series",
                        "series_id": "S000006415",
                        "valid_from_quarter": "2020q1",
                        "valid_to_quarter": "2025q4",
                    }
                ],
            }
        )
    )
    from ohmydata.providers.sec.batch import load_universe

    values, digest = load_universe(u)
    assert values[0].active(Quarter.parse("2021q1")) and not values[0].active(
        Quarter.parse("2019q4")
    )
    assert digest == canonical_hash(
        {
            "schema_version": "sec-equity-etf-universe-v1",
            "funds": [
                {
                    "symbol": "XLK",
                    "cik": "0001064641",
                    "selection_mode": "series",
                    "series_id": "S000006415",
                    "valid_from_quarter": "2020q1",
                    "valid_to_quarter": "2025q4",
                }
            ],
        }
    )


def test_universe_duplicate_and_spy_selection() -> None:
    with pytest.raises(CoverageError):
        resolve_single_series_cik(
            [{"CIK": "1", "SERIES_ID": ""}, {"CIK": "1", "SERIES_ID": ""}], "1"
        )
    assert resolve_single_series_cik([{"CIK": "1", "SERIES_ID": ""}], "1") is None


def test_spy_single_series_extraction_public_api(tmp_path: Path) -> None:
    """Blank-series selection succeeds per accession and rejects same-accession duplicates."""
    import io
    import zipfile
    from datetime import UTC, datetime

    from ohmydata.providers.sec.artifacts import SecArtifactStore
    from ohmydata.providers.sec.endpoints import SecNportQuarterRequest
    from ohmydata.providers.sec.nport import extract_from_artifact

    acc = "0000000001-24-000001"
    tables: dict[str, str] = {
        "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tSUB_TYPE\tREPORT_ENDING_PERIOD\tREPORT_DATE\tIS_LAST_FILING\n"
        + f"{acc}\t01-MAY-2024\tNPORT-P\t31-MAR-2024\t31-MAR-2024\tY\n",
        "REGISTRANT.tsv": f"ACCESSION_NUMBER\tCIK\tREGISTRANT_NAME\tFILE_NUM\tLEI\n{acc}\t0000000001\tTest\t1\t\n",
        "FUND_REPORTED_INFO.tsv": f"ACCESSION_NUMBER\tSERIES_ID\tSERIES_NAME\tSERIES_LEI\tTOTAL_ASSETS\tTOTAL_LIABILITIES\tNET_ASSETS\n{acc}\t\tSPY\t\t100\t1\t99\n",
        "FUND_REPORTED_HOLDING.tsv": f"ACCESSION_NUMBER\tHOLDING_ID\tISSUER_NAME\tISSUER_LEI\tISSUER_TITLE\tISSUER_CUSIP\tBALANCE\tUNIT\tOTHER_UNIT_DESC\tCURRENCY_CODE\tCURRENCY_VALUE\tEXCHANGE_RATE\tPERCENTAGE\tPAYOFF_PROFILE\tASSET_CAT\tOTHER_ASSET\tISSUER_TYPE\tOTHER_ISSUER\tINVESTMENT_COUNTRY\tIS_RESTRICTED_SECURITY\tFAIR_VALUE_LEVEL\tDERIVATIVE_CAT\n{acc}\tH1\tIssuer\t\t\tCUS\t1\tSH\t\tUSD\t\t\t1\t\tEC\t\tCORP\t\tUS\tN\t1\t\n",
        "IDENTIFIERS.tsv": "HOLDING_ID\tIDENTIFIERS_ID\tIDENTIFIER_ISIN\tIDENTIFIER_TICKER\tOTHER_IDENTIFIER\tOTHER_IDENTIFIER_DESC\nH1\tI1\t\tABC\t\t\n",
    }
    source = tmp_path / "spy.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for name, text in tables.items():
            archive.writestr(name, text)
    store = SecArtifactStore(tmp_path / "artifacts")
    req = SecNportQuarterRequest(
        2024,
        2,
        (),
        single_series_ciks=("0000000001",),
        utc_now=lambda: datetime(2024, 8, 1, tzinfo=UTC),
    )
    ref = store.publish(
        io.BytesIO(source.read_bytes()), year=2024, quarter=2, source_url=req.source_url
    )
    result = extract_from_artifact(store, ref, req, observed_at=datetime(2024, 6, 1, tzinfo=UTC))
    assert result.vintages[0].series_id is None
    tables["FUND_REPORTED_INFO.tsv"] += f"{acc}\t\tDuplicate\t\t100\t1\t99\n"
    with zipfile.ZipFile(source, "w") as archive:
        for name, text in tables.items():
            archive.writestr(name, text)
    with pytest.raises((CoverageError, SchemaMismatchError)):
        ref2 = store.publish(
            io.BytesIO(source.read_bytes()), year=2024, quarter=2, source_url=req.source_url
        )
        extract_from_artifact(store, ref2, req)


def test_exact_pair_request_keeps_cik_identity() -> None:
    from datetime import UTC, datetime

    from ohmydata.providers.sec.endpoints import SecNportQuarterRequest

    request = SecNportQuarterRequest(
        2024,
        2,
        ("S1",),
        selected_pairs=(("0000000001", "S1"),),
        required_pairs=(("0000000001", "S1"),),
        utc_now=lambda: datetime(2024, 8, 1, tzinfo=UTC),
    )
    assert request.spec.effective_parameters["selected_pairs"] == [["0000000001", "S1"]]


def test_missing_single_cik_is_rejected() -> None:
    with pytest.raises(CoverageError):
        resolve_single_series_cik([], "0000000001")


def test_same_series_two_cik_exact_selection() -> None:
    from datetime import UTC, datetime

    from ohmydata.providers.sec.endpoints import SecNportQuarterRequest

    request = SecNportQuarterRequest(
        2024,
        2,
        ("S1",),
        selected_pairs=(("0000000001", "S1"),),
        required_pairs=(("0000000001", "S1"),),
        utc_now=lambda: datetime(2024, 8, 1, tzinfo=UTC),
    )
    assert ("0000000002", "S1") not in request.selected_pairs


def test_blank_and_nonblank_parent_conflict_is_ambiguous() -> None:
    with pytest.raises(CoverageError):
        resolve_single_series_cik(
            [{"CIK": "0000000001", "SERIES_ID": ""}, {"CIK": "0000000001", "SERIES_ID": "S1"}],
            "0000000001",
        )


def test_index_idempotency_collision_and_order(tmp_path: Path) -> None:
    index = AppendOnlyIndex(tmp_path / "state.json", "x", ("key", "value"))
    entry = {"key": "b", "value": 2}
    index.append(entry, ("key",))
    index.append(entry, ("key",))
    index.append({"key": "a", "value": 1}, ("key",))
    assert [x["key"] for x in index.read()["entries"]] == ["a", "b"]
    with pytest.raises(SchemaMismatchError):
        index.append({"key": "b", "value": 3}, ("key",))
    index.path.write_text('{"schema_version":"x","revision":2,"entries":[{"key":"z"},{"key":"a"}]}')
    with pytest.raises(SchemaMismatchError):
        index.read()


def test_immutable_payload_and_tamper(tmp_path: Path) -> None:
    body = b'{"cik":"0000000001"}'
    manifest = {
        "cik": "0000000001",
        "payload_sha256": __import__("hashlib").sha256(body).hexdigest(),
    }
    path, digest = store_immutable_payload(tmp_path, "0000000001", body, manifest)
    assert path.joinpath("source.json").read_bytes() == body
    assert store_immutable_payload(tmp_path, "0000000001", body, manifest)[1] == digest
    path.joinpath("source.json").write_bytes(b"tampered")
    with pytest.raises(SchemaMismatchError):
        store_immutable_payload(tmp_path, "0000000001", body, manifest)


def test_receipts_order_and_validation(tmp_path: Path) -> None:
    for key in ("b", "a"):
        folder = tmp_path / "state" / "fetch-receipts" / "2020q1"
        folder.mkdir(parents=True, exist_ok=True)
        value: dict[str, Any] = {
            "schema_version": "sec-fetch-receipt-v1",
            "source_quarter": "2020q1",
            "universe_hash": "u",
            "nport_artifact_sha256": key,
            "nport_manifest_sha256": "m",
            "payloads": [],
            "accessions": {},
        }
        from ohmydata.providers.sec.batch import receipt_hashes

        value["edgar_closure_hash"], value["receipt_sha256"] = receipt_hashes(value)
        (folder / f"{value['receipt_sha256']}.json").write_bytes(
            (json.dumps(value, separators=(",", ":")) + "\n").encode()
        )
    values = enumerate_receipts(tmp_path, "2020q1", "u")
    assert [x[1]["nport_artifact_sha256"] for x in values] == ["a", "b"]
    first = values[0][0]
    first.write_text(first.read_text().replace('"payloads":[]', '"payloads":[{}]'))
    with pytest.raises(SchemaMismatchError):
        enumerate_receipts(tmp_path, "2020q1", "u")


def test_partition_identity_and_directory_collision(tmp_path: Path) -> None:
    assert partition_identity(a=1) != partition_identity(a=2)
    source, target = tmp_path / "tmp", tmp_path / "final"
    source.mkdir()
    (source / "x").write_text("1")
    publish_directory(source, target)
    publish_directory(tmp_path / "missing", target) if False else None
    other = tmp_path / "other"
    other.mkdir()
    (other / "x").write_text("2")
    with pytest.raises(SchemaMismatchError):
        publish_directory(other, target)


def test_canonical_hash_preserves_decimal_dates_and_rejects_naive() -> None:
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from ohmydata.providers.sec.batch import canonical_hash

    value = {
        "decimal": Decimal("1.2300"),
        "date": date(2024, 1, 2),
        "timestamp": datetime(2024, 1, 2, tzinfo=UTC),
        "list": [Decimal(2)],
    }
    assert canonical_hash(value) == canonical_hash(value)
    with pytest.raises(ValueError):
        canonical_hash(datetime(2024, 1, 2))  # noqa: DTZ001


class _Response:
    def __init__(self, body: bytes):
        self.body = io.BytesIO(body)


class _Client:
    def __init__(self, body: bytes):
        self.body, self.calls = body, []
        self.calls: list[str]

    def open(self, url: str, **_: object) -> _Response:
        self.calls.append(url)
        return _Response(self.body)


def test_fetch_quarter_cache_and_refresh_identical(tmp_path: Path) -> None:
    import zipfile

    from ohmydata.providers.sec.batch import Quarter, SecNportBatch

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for name in (
            "SUBMISSION",
            "REGISTRANT",
            "FUND_REPORTED_INFO",
            "FUND_REPORTED_HOLDING",
            "IDENTIFIERS",
        ):
            z.writestr(name + ".tsv", "X\n")
    client = _Client(data.getvalue())
    batch = SecNportBatch(tmp_path, client)
    q = Quarter(2026, 2)
    first = batch.fetch_quarter(q)
    calls = len(client.calls)
    assert batch.fetch_quarter(q).sha256 == first.sha256 and len(client.calls) == calls
    assert batch.fetch_quarter(q, refresh=True).sha256 == first.sha256


def test_fetch_quarter_streams_transport_in_bounded_chunks(tmp_path: Path) -> None:
    import zipfile

    from ohmydata.providers.sec.batch import Quarter, SecNportBatch

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for name in (
            "SUBMISSION",
            "REGISTRANT",
            "FUND_REPORTED_INFO",
            "FUND_REPORTED_HOLDING",
            "IDENTIFIERS",
        ):
            z.writestr(name + ".tsv", "X\n")
    payload = data.getvalue()

    class ChunkOnly:
        def __init__(self, value: bytes) -> None:
            self.value, self.offset, self.sizes = value, 0, []
            self.sizes: list[int]

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                raise AssertionError("unbounded read")
            self.sizes.append(size)
            chunk = self.value[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    class Client:
        def __init__(self) -> None:
            self.body = ChunkOnly(payload)

        def open(self, url: str, **_: object) -> Any:
            return type("Response", (), {"body": self.body})()

    client = Client()
    ref = SecNportBatch(tmp_path, client).fetch_quarter(Quarter(2026, 2))
    assert ref.source_zip.is_file()
    assert client.body.sizes and all(size > 0 for size in client.body.sizes)


def test_fetch_edgar_current_only_manifest(tmp_path: Path) -> None:
    from ohmydata.providers.sec.batch import SecNportBatch

    acc = "0000000001-24-000001"
    payload: dict[str, Any] = {
        "cik": "1",
        "filings": {
            "recent": {
                k: [v]
                for k, v in {
                    "accessionNumber": acc,
                    "form": "NPORT-P",
                    "filingDate": "2024-05-01",
                    "reportDate": "2024-03-31",
                    "primaryDocument": "x",
                    "acceptanceDateTime": "20240501120000",
                }.items()
            },
            "files": [],
        },
    }
    client = _Client(json.dumps(payload).encode())
    batch = SecNportBatch(tmp_path, client)
    result = batch.fetch_edgar("0000000001", (acc,))
    manifest = result["refs"]["current"]
    assert manifest["manifest_sha256"] == canonical_hash(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    )
    assert len(client.calls) == 1


def test_edgar_historical_closure_and_cache(tmp_path: Path) -> None:
    from ohmydata.providers.sec.batch import SecNportBatch

    acc = "0000000001-18-000001"
    base = "CIK0000000001-submissions-001.json"
    current: dict[str, Any] = {
        "cik": "1",
        "filings": {
            "recent": {
                "accessionNumber": [],
                "form": [],
                "filingDate": [],
                "reportDate": [],
                "primaryDocument": [],
                "acceptanceDateTime": [],
            },
            "files": [{"name": base}],
        },
    }
    history: dict[str, Any] = {
        "cik": "1",
        "filings": {
            "recent": {
                k: [v]
                for k, v in {
                    "accessionNumber": acc,
                    "form": "NPORT-P",
                    "filingDate": "2018-05-01",
                    "reportDate": "2018-03-31",
                    "primaryDocument": "x",
                    "acceptanceDateTime": "20180501120000",
                }.items()
            },
            "files": [],
        },
    }

    class Client(_Client):
        def open(self, url: str, **kwargs: object) -> _Response:
            self.calls.append(url)
            return _Response(json.dumps(history if url.endswith(base) else current).encode())

    first_client = Client(b"")
    result = SecNportBatch(tmp_path, first_client).fetch_edgar("0000000001", (acc,))
    assert base in result["refs"]
    second_client = Client(b"")
    replayed = SecNportBatch(tmp_path, second_client).fetch_edgar("0000000001", (acc,))
    assert base in replayed["refs"]
    assert second_client.calls == []
    index = json.loads((tmp_path / "state" / "sec-edgar-receipt-index-v1.json").read_text())
    assert len(index["entries"]) == 2
    assert all(len(entry["observations"]) == 1 for entry in index["entries"])


def test_edgar_refresh_appends_current_observation(tmp_path: Path) -> None:
    from ohmydata.providers.sec.batch import SecNportBatch

    current: dict[str, Any] = {
        "cik": "1",
        "filings": {
            "recent": {
                k: []
                for k in (
                    "accessionNumber",
                    "form",
                    "filingDate",
                    "reportDate",
                    "primaryDocument",
                    "acceptanceDateTime",
                )
            },
            "files": [],
        },
    }
    changed = dict(current)
    changed["extra"] = "second-observation"

    class RefreshClient(_Client):
        def __init__(self) -> None:
            super().__init__(json.dumps(current).encode())
            self.responses = [json.dumps(current).encode(), json.dumps(changed).encode()]

        def open(self, url: str, **kwargs: object) -> _Response:
            self.calls.append(url)
            return _Response(self.responses.pop(0))

    client = RefreshClient()
    batch = SecNportBatch(tmp_path, client)
    batch.fetch_edgar("0000000001", ())
    batch.fetch_edgar("0000000001", (), refresh=True)
    index = json.loads((tmp_path / "state" / "sec-edgar-receipt-index-v1.json").read_text())
    current_entry = next(e for e in index["entries"] if e["request_kind"] == "current")
    assert len(current_entry["observations"]) == 2


def test_missing_edgar_accession_publishes_no_receipt(tmp_path: Path) -> None:
    from ohmydata.providers.sec.batch import SecNportBatch

    payload: dict[str, Any] = {
        "cik": "1",
        "filings": {
            "recent": {
                "accessionNumber": [],
                "form": [],
                "filingDate": [],
                "reportDate": [],
                "primaryDocument": [],
                "acceptanceDateTime": [],
            },
            "files": [],
        },
    }
    batch = SecNportBatch(tmp_path, _Client(json.dumps(payload).encode()))
    with pytest.raises(CoverageError):
        batch.fetch_edgar("0000000001", ("0000000001-24-000001",))
    assert not (tmp_path / "state" / "fetch-receipts").exists()


def test_build_receipt_is_offline_and_applies_policies(tmp_path: Path) -> None:
    from ohmydata.providers.sec.batch import SecNportBatch

    batch = SecNportBatch(tmp_path, _Client(b"network-must-not-be-used"))
    with pytest.raises((CoverageError, SchemaMismatchError, FileNotFoundError)):
        batch.build_receipt(tmp_path / "missing.json", (), "observation-only")
    assert batch.client.calls == []


def test_build_receipt_cap_failure_publishes_no_partition_or_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ohmydata.providers.sec import batch as sec_batch
    from ohmydata.providers.sec.batch import SecNportBatch, atomic_json

    batch = SecNportBatch(tmp_path, _Client(b"offline"), max_selected_rows=2)
    receipt: dict[str, Any] = {
        "schema_version": "sec-fetch-receipt-v1",
        "source_quarter": "2024q2",
        "nport_artifact_sha256": "a" * 64,
        "nport_manifest_sha256": "b" * 64,
        "universe_hash": "c" * 64,
        "payloads": [],
        "accessions": {},
    }
    receipt["edgar_closure_hash"], receipt["receipt_sha256"] = receipt_hashes(receipt)
    path = tmp_path / "receipt.json"
    atomic_json(path, receipt)

    def replay_noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(batch.store, "replay", replay_noop)

    def cap_failure(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["max_selected_rows"] == 2
        raise CoverageError("selected output row limit exceeded")

    monkeypatch.setattr(sec_batch, "extract_from_artifact", cap_failure)
    scheduled = (SecScheduledFundSelector(SecFundSelector("TST", "0000000001", "series", "S1")),)
    with pytest.raises(CoverageError, match="selected output row limit exceeded"):
        batch.build_receipt(path, scheduled, "observation-only")
    assert not (tmp_path / "core" / "sec-fund-holdings-pit-v1" / "catalog.json").exists()
    core = tmp_path / "core"
    if core.exists():
        assert not list(core.rglob("partition=*"))


def test_fetch_receipt_cap_failure_publishes_no_receipt_or_core(
    tmp_path: Path,
) -> None:
    import zipfile

    from ohmydata.providers.sec.batch import SecNportBatch

    acc = "0000000001-24-000001"
    tables = {
        "SUBMISSION.tsv": (
            "ACCESSION_NUMBER\tFILING_DATE\tSUB_TYPE\tREPORT_ENDING_PERIOD\tREPORT_DATE\tIS_LAST_FILING\n"
            f"{acc}\t01-MAY-2024\tNPORT-P\t31-MAR-2024\t31-MAR-2024\tY\n"
        ),
        "REGISTRANT.tsv": f"ACCESSION_NUMBER\tCIK\tREGISTRANT_NAME\tFILE_NUM\tLEI\n{acc}\t0000000001\tTest\t1\t\n",
        "FUND_REPORTED_INFO.tsv": f"ACCESSION_NUMBER\tSERIES_ID\tSERIES_NAME\tSERIES_LEI\tTOTAL_ASSETS\tTOTAL_LIABILITIES\tNET_ASSETS\n{acc}\tS1\tTest\t\t100\t1\t99\n",
        "FUND_REPORTED_HOLDING.tsv": (
            "ACCESSION_NUMBER\tHOLDING_ID\tISSUER_NAME\tISSUER_LEI\tISSUER_TITLE\tISSUER_CUSIP\tBALANCE\tUNIT\tOTHER_UNIT_DESC\tCURRENCY_CODE\tCURRENCY_VALUE\tEXCHANGE_RATE\tPERCENTAGE\tPAYOFF_PROFILE\tASSET_CAT\tOTHER_ASSET\tISSUER_TYPE\tOTHER_ISSUER\tINVESTMENT_COUNTRY\tIS_RESTRICTED_SECURITY\tFAIR_VALUE_LEVEL\tDERIVATIVE_CAT\n"
            f"{acc}\tH1\tIssuer\t\t\tCUS\t1\tSH\t\tUSD\t\t\t1\t\tEC\t\tCORP\t\tUS\tN\t1\t\n"
        ),
        "IDENTIFIERS.tsv": "HOLDING_ID\tIDENTIFIERS_ID\tIDENTIFIER_ISIN\tIDENTIFIER_TICKER\tOTHER_IDENTIFIER\tOTHER_IDENTIFIER_DESC\nH1\tI1\t\tABC\t\t\n",
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        for name, content in tables.items():
            z.writestr(name, content)

    client = _Client(archive.getvalue())
    batch = SecNportBatch(tmp_path, client, max_selected_rows=2)
    scheduled = (SecScheduledFundSelector(SecFundSelector("TST", "0000000001", "series", "S1")),)
    with pytest.raises(CoverageError, match="selected output row limit exceeded"):
        batch.fetch_receipt_for_quarter(Quarter(2024, 2), scheduled, "u")
    assert not (tmp_path / "state" / "fetch-receipts").exists()
    core = tmp_path / "core"
    assert not core.exists() or not list(core.rglob("partition=*"))


def test_enrichment_rejects_edgar_report_date_mismatch_and_retains_missing_acceptance() -> None:
    from datetime import UTC, datetime

    from ohmydata.providers.sec.nport import (
        SecAvailabilityPolicy,
        SecFundHoldingVintage,
        enrich_vintages,
    )

    vintage = SecFundHoldingVintage(
        accession_number="0000000001-24-000001",
        cik="0000000001",
        series_id="S1",
        series_name="Test",
        report_date=date(2024, 3, 31),
        filing_date=date(2024, 5, 1),
        submission_type="NPORT-P",
        holdings=(),
        observed_at=datetime(2024, 8, 1, tzinfo=UTC),
    )
    missing_acceptance: dict[str, Any] = {
        "cik": "0000000001",
        "filings": {
            "recent": {
                "accessionNumber": [vintage.accession_number],
                "form": ["NPORT-P"],
                "filingDate": ["2024-05-01"],
                "reportDate": ["2024-03-31"],
                "primaryDocument": ["x"],
                "acceptanceDateTime": [None],
            },
            "files": [],
        },
    }
    enriched = enrich_vintages((vintage,), missing_acceptance, vintage.cik)
    assert enriched[0].accepted_at is None
    assert "ACCEPTANCE_TIME_MISSING" in enriched[0].quality_flags
    assert enriched[0].availability_policy == SecAvailabilityPolicy.OBSERVATION_ONLY_V1.value

    mismatched = json.loads(json.dumps(missing_acceptance))
    mismatched["filings"]["recent"]["reportDate"] = ["2024-03-30"]
    with pytest.raises(SchemaMismatchError):
        enrich_vintages((vintage,), mismatched, vintage.cik)


def test_build_all_receipts_deterministic_and_tamper_fails(tmp_path: Path) -> None:
    from ohmydata.providers.sec.batch import SecNportBatch, atomic_json

    batch = SecNportBatch(tmp_path, _Client(b"network-must-not-be-used"))
    folder = tmp_path / "state" / "fetch-receipts" / "2024q2"
    folder.mkdir(parents=True)
    value: dict[str, Any] = {
        "schema_version": "sec-fetch-receipt-v1",
        "source_quarter": "2024q2",
        "universe_hash": "u",
        "nport_artifact_sha256": "a" * 64,
        "nport_manifest_sha256": "b" * 64,
        "payloads": [],
        "accessions": {},
    }
    from ohmydata.providers.sec.batch import receipt_hashes

    value["edgar_closure_hash"], value["receipt_sha256"] = receipt_hashes(value)
    atomic_json(folder / f"{value['receipt_sha256']}.json", value)
    from ohmydata.providers.sec.errors import SnapshotIntegrityError

    with pytest.raises((CoverageError, SchemaMismatchError, SnapshotIntegrityError)):
        batch.build_all_receipts(Quarter(2024, 2), "u", (), "observation-only")
    assert batch.client.calls == []


def test_validate_local_accepts_consistent_published_partition(tmp_path: Path) -> None:
    import shutil
    import zipfile
    from datetime import UTC, datetime

    from ohmydata.providers.sec.artifacts import SecArtifactStore
    from ohmydata.providers.sec.batch import SecNportBatch, atomic_json
    from ohmydata.providers.sec.core_dataset import FUND_COLUMNS, write_partition

    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        for name in (
            "SUBMISSION",
            "REGISTRANT",
            "FUND_REPORTED_INFO",
            "FUND_REPORTED_HOLDING",
            "IDENTIFIERS",
        ):
            archive.writestr(name + ".tsv", "X\n")
    store = SecArtifactStore(tmp_path / "raw")
    ref = store.publish(
        source=io.BytesIO(source.getvalue()),
        year=2024,
        quarter=2,
        source_url="https://www.sec.gov/files/test.zip",
    )
    batch = SecNportBatch(tmp_path, _Client(b"offline"))
    batch._quarter_index().append(  # pyright: ignore[reportPrivateUsage]
        {
            "year": 2024,
            "quarter": 2,
            "artifact_sha256": ref.sha256,
            "manifest_sha256": ref.manifest_sha256,
            "source_url": "https://www.sec.gov/files/test.zip",
            "retrieved_at": "2024-08-01T00:00:00Z",
        },
        ("year", "quarter", "artifact_sha256", "manifest_sha256", "source_url", "retrieved_at"),
    )
    receipt: dict[str, Any] = {
        "schema_version": "sec-fetch-receipt-v1",
        "source_quarter": "2024q2",
        "nport_artifact_sha256": ref.sha256,
        "nport_manifest_sha256": ref.manifest_sha256,
        "universe_hash": "u",
        "payloads": [],
        "accessions": {},
    }
    from ohmydata.providers.sec.batch import receipt_hashes

    receipt["edgar_closure_hash"], receipt["receipt_sha256"] = receipt_hashes(receipt)
    atomic_json(
        tmp_path / "state" / "fetch-receipts" / "2024q2" / f"{receipt['receipt_sha256']}.json",
        receipt,
    )
    write_partition(
        tmp_path,
        source_quarter="2024q2",
        artifact_sha256=ref.sha256,
        artifact_manifest_sha256=ref.manifest_sha256,
        universe_hash="u",
        edgar_closure_hash=receipt["edgar_closure_hash"],
        parser_version="1",
        availability_policy="observation-only",
        lag_days=None,
        tables=(
            [
                {
                    **{key: None for key in FUND_COLUMNS},
                    "provider": "sec",
                    "fund_symbol": "TST",
                    "cik": "0000000001",
                    "accession_number": "0000000001-24-000001",
                    "report_date": date(2024, 3, 31),
                    "artifact_sha256": ref.sha256,
                    "artifact_manifest_sha256": ref.manifest_sha256,
                    "payload_hash": "p",
                    "vintage_identity": "v",
                    "universe_hash": "u",
                    "observed_at": datetime(2024, 8, 1, tzinfo=UTC),
                    "quality_flags": [],
                }
            ],
            [],
            [],
        ),
        quality={"scan_counts": {}, "qa": {}, "quality_flags": []},
    )
    assert batch.validate_local((Quarter(2024, 2),))["partitions_checked"] == 1
    report = batch.inspect_local(Quarter(2024, 2), "TST", rows=True)
    assert report["facts"][0]["fund_symbol"] == "TST"
    assert report["rows"]["holdings"] == [] and report["table_counts"]["fund_vintages"] == 1

    # The catalog is an identity-bearing index: a self-consistent partition
    # moved to a different in-root path must still be rejected.
    catalog_path = tmp_path / "core" / "sec-fund-holdings-pit-v1" / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    original = tmp_path / catalog["entries"][0]["partition_path"]
    moved = tmp_path / "core" / "sec-fund-holdings-pit-v1" / "moved-partition"
    shutil.copytree(original, moved)
    catalog["entries"][0]["partition_path"] = str(moved.relative_to(tmp_path))
    catalog_path.write_text(json.dumps(catalog, separators=(",", ":")) + "\n")
    with pytest.raises(CoverageError):
        batch.validate_local((Quarter(2024, 2),))


def test_two_quarter_real_service_resume_preserves_q1_partition(tmp_path: Path) -> None:
    """Offline fake transport exercises fetch, receipt, build, interruption and resume."""
    import hashlib
    import zipfile

    from ohmydata.providers.sec.batch import SecNportBatch

    cik, series = "0000000001", "S1"

    def make_zip(acc: str, report: str, filing: str) -> bytes:
        values = {
            "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tSUB_TYPE\tREPORT_ENDING_PERIOD\tREPORT_DATE\tIS_LAST_FILING\n"
            + f"{acc}\t{filing}\tNPORT-P\t{report}\t{report}\tY\n",
            "REGISTRANT.tsv": f"ACCESSION_NUMBER\tCIK\tREGISTRANT_NAME\tFILE_NUM\tLEI\n{acc}\t{cik}\tTest\t1\t\n",
            "FUND_REPORTED_INFO.tsv": f"ACCESSION_NUMBER\tSERIES_ID\tSERIES_NAME\tSERIES_LEI\tTOTAL_ASSETS\tTOTAL_LIABILITIES\tNET_ASSETS\n{acc}\t{series}\tTest\t\t100\t1\t99\n",
            "FUND_REPORTED_HOLDING.tsv": f"ACCESSION_NUMBER\tHOLDING_ID\tISSUER_NAME\tISSUER_LEI\tISSUER_TITLE\tISSUER_CUSIP\tBALANCE\tUNIT\tOTHER_UNIT_DESC\tCURRENCY_CODE\tCURRENCY_VALUE\tEXCHANGE_RATE\tPERCENTAGE\tPAYOFF_PROFILE\tASSET_CAT\tOTHER_ASSET\tISSUER_TYPE\tOTHER_ISSUER\tINVESTMENT_COUNTRY\tIS_RESTRICTED_SECURITY\tFAIR_VALUE_LEVEL\tDERIVATIVE_CAT\n{acc}\tH1\tIssuer\t\t\tCUS\t1\tSH\t\tUSD\t\t\t1\t\tEC\t\tCORP\t\tUS\tN\t1\t\n",
            "IDENTIFIERS.tsv": "HOLDING_ID\tIDENTIFIERS_ID\tIDENTIFIER_ISIN\tIDENTIFIER_TICKER\tOTHER_IDENTIFIER\tOTHER_IDENTIFIER_DESC\nH1\tI1\t\tABC\t\t\n",
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name, text in values.items():
                archive.writestr(name, text)
        return buf.getvalue()

    payloads = {
        "2024q1": make_zip("0000000001-24-000001", "31-MAR-2024", "01-MAY-2024"),
        "2024q2": make_zip("0000000001-24-000002", "30-JUN-2024", "01-AUG-2024"),
    }

    def make_edgar(acc: str, filing: str, report: str) -> bytes:
        return json.dumps(
            {
                "cik": "1",
                "filings": {
                    "recent": {
                        "accessionNumber": [acc],
                        "form": ["NPORT-P"],
                        "filingDate": [filing],
                        "reportDate": [report],
                        "primaryDocument": ["x"],
                        "acceptanceDateTime": [filing.replace("-", "") + "120000"],
                    },
                    "files": [],
                },
            }
        ).encode()

    edgar_q1 = make_edgar("0000000001-24-000001", "2024-05-01", "2024-03-31")
    edgar_q2 = make_edgar("0000000001-24-000002", "2024-08-01", "2024-06-30")

    class ServiceClient(_Client):
        def __init__(self) -> None:
            super().__init__(b"")
            self.fail_q2_edgar = True
            self.edgar_calls = 0

        def open(self, url: str, **_: object) -> _Response:
            self.calls.append(url)
            if "/submissions/CIK" in url:
                self.edgar_calls += 1
                if self.fail_q2_edgar and self.edgar_calls == 2:
                    self.fail_q2_edgar = False
                    raise RuntimeError("synthetic q2 interruption")
                return _Response(edgar_q1 if self.edgar_calls == 1 else edgar_q2)
            return _Response(payloads["2024q1" if "2024q1_nport" in url else "2024q2"])

    client = ServiceClient()
    batch = SecNportBatch(tmp_path, client)
    scheduled = (SecScheduledFundSelector(SecFundSelector("TST", cik, "series", series)),)

    def run_loop() -> None:
        for quarter in (Quarter(2024, 1), Quarter(2024, 2)):
            batch.fetch_receipt_for_quarter(quarter, scheduled, "u")
            batch.build_all_receipts(quarter, "u", scheduled, "observation-only")

    with pytest.raises(RuntimeError):
        run_loop()
    q1_files = {
        str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "core").rglob("*")
        if p.is_file() and "source_quarter=2024q1" in str(p)
    }
    q1_receipts = sorted((tmp_path / "state" / "fetch-receipts" / "2024q1").glob("*.json"))
    q1_edgar_calls_before_resume = client.edgar_calls
    q1_transport = [u for u in client.calls if "2024q1_nport" in u]
    run_loop()
    q1_after = {
        str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "core").rglob("*")
        if p.is_file() and "source_quarter=2024q1" in str(p)
    }
    assert q1_after == q1_files
    assert sorted((tmp_path / "state" / "fetch-receipts" / "2024q1").glob("*.json")) == q1_receipts
    assert client.edgar_calls == q1_edgar_calls_before_resume + 1
    assert [u for u in client.calls if "2024q1_nport" in u] == q1_transport
    assert list(
        (tmp_path / "core" / "sec-fund-holdings-pit-v1" / "source_quarter=2024q2").glob(
            "artifact=*/partition=*"
        )
    )


def test_batch_progress_callback(tmp_path: Path) -> None:
    import zipfile

    from ohmydata.providers.sec.batch import Quarter, SecNportBatch

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for name in (
            "SUBMISSION",
            "REGISTRANT",
            "FUND_REPORTED_INFO",
            "FUND_REPORTED_HOLDING",
            "IDENTIFIERS",
        ):
            z.writestr(name + ".tsv", "X\n")
    payload = data.getvalue()

    class Client:
        def open(self, url: str, **_: object) -> Any:
            return type("Response", (), {"body": io.BytesIO(payload)})()

    events: list[tuple[str, int]] = []
    batch = SecNportBatch(tmp_path, Client(), progress=lambda s, c: events.append((s, c)))
    ref = batch.fetch_quarter(Quarter(2026, 2))
    assert ref.source_zip.is_file()
    assert len(events) == 1
    assert events[0] == ("download", len(payload))


def test_latest_completed_quarter() -> None:
    from datetime import UTC, datetime

    from ohmydata.providers.sec.batch import Quarter, latest_completed_quarter

    assert latest_completed_quarter(now=lambda: datetime(2026, 2, 15, tzinfo=UTC)) == Quarter(
        2025, 4
    )
    assert latest_completed_quarter(now=lambda: datetime(2026, 5, 1, tzinfo=UTC)) == Quarter(
        2026, 1
    )
    assert latest_completed_quarter(now=lambda: datetime(2026, 9, 3, tzinfo=UTC)) == Quarter(
        2026, 2
    )
    assert latest_completed_quarter(now=lambda: datetime(2026, 11, 20, tzinfo=UTC)) == Quarter(
        2026, 3
    )

    with pytest.raises(ValueError, match="aware datetime"):
        latest_completed_quarter(now=lambda: datetime(2026, 9, 3))  # noqa: DTZ001
