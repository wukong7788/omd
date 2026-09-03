from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ohmydata.cli import main
from ohmydata.providers.sec.batch import ensure_safe_output_root
from ohmydata.providers.sec.errors import CoverageError


def _universe(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "sec-equity-etf-universe-v1",
                "funds": [
                    {
                        "symbol": "XLK",
                        "cik": "0001064641",
                        "selection_mode": "series",
                        "series_id": "S000006415",
                    }
                ],
            }
        )
    )


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        FakeClient.instances.append(self)


class FakeBatch:
    instances: ClassVar[list[FakeBatch]] = []

    def __init__(self, root: str | Path, client: object = None) -> None:
        self.client = client
        self.fetches: list[str] = []
        self.builds: list[str] = []
        FakeBatch.instances.append(self)

    def fetch_receipt_for_quarter(
        self, quarter: object, scheduled: object, digest: str, *, refresh: bool = False
    ) -> None:
        self.fetches.append(str(quarter))

    def build_all_receipts(
        self, quarter: object, digest: str, scheduled: object, policy: str, lag: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        self.builds.append(str(quarter))
        return ()

    def validate_local(self, quarters: object) -> dict[str, int]:
        return {"artifacts_checked": 0}

    def inspect_local(
        self, quarter: object, symbol: str | None = None, rows: bool = False
    ) -> dict[str, object]:
        return {"quarter": str(quarter), "partitions": 0}


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    import ohmydata.providers.sec.cli as module

    monkeypatch.setattr(module, "SecHttpClient", FakeClient)
    monkeypatch.setattr(module, "SecNportBatch", FakeBatch)


def test_plan_and_invalid_range(tmp_path: Path) -> None:
    u = tmp_path / "u.json"
    _universe(u)
    assert (
        main(
            [
                "sec",
                "nport",
                "plan",
                "--quarters",
                "2019q4:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--json",
            ]
        )
        == 0
    )


def test_invalid_output_limits_fail_before_client_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = tmp_path / "u.json"
    _universe(u)
    contact = tmp_path / "ua.txt"
    contact.write_text("research@example.invalid\n")
    _patch(monkeypatch)
    before = len(FakeClient.instances)
    assert (
        main(
            [
                "sec",
                "nport",
                "sync",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(contact),
                "--availability-policy",
                "observation-only",
                "--max-selected-rows",
                "0",
            ]
        )
        == 2
    )
    assert len(FakeClient.instances) == before
    assert (
        main(
            [
                "sec",
                "nport",
                "plan",
                "--quarters",
                "bad",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
            ]
        )
        != 0
    )


def test_contact_file_and_stdin_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)
    contact = "fake-contact@example.invalid"
    ua = tmp_path / "ua"
    ua.write_text(contact + "\n")
    assert (
        main(
            [
                "sec",
                "nport",
                "fetch",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
            ]
        )
        == 0
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(contact + "\n"))
    assert (
        main(
            [
                "sec",
                "nport",
                "fetch",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-stdin",
            ]
        )
        == 0
    )
    assert contact not in capsys.readouterr().out + capsys.readouterr().err


def test_sync_dispatch_and_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)
    ua = tmp_path / "ua"
    ua.write_text("fake@example.invalid\n")
    assert (
        main(
            [
                "sec",
                "nport",
                "sync",
                "--quarters",
                "2020q1:2020q2",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
                "--availability-policy",
                "observation-only",
            ]
        )
        == 0
    )
    assert len(FakeBatch.instances[-1].fetches) == 2 and len(FakeBatch.instances[-1].builds) == 2
    assert (
        main(
            [
                "sec",
                "nport",
                "build",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--availability-policy",
                "observation-only",
                "--lag-days",
                "1",
            ]
        )
        != 0
    )

    before = len(FakeClient.instances)
    batches_before = len(FakeBatch.instances)
    assert (
        main(
            [
                "sec",
                "nport",
                "sync",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
                "--availability-policy",
                "unknown",
            ]
        )
        == 2
    )
    assert len(FakeClient.instances) == before
    assert len(FakeBatch.instances) == batches_before


def test_two_quarter_sync_resume_does_not_refetch_completed_quarter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The service loop is quarter-atomic: q2 interruption leaves q1 reusable."""
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)
    ua = tmp_path / "ua"
    ua.write_text("fake@example.invalid\n")
    calls: list[str] = []
    fetch_invocations: list[str] = []
    failed = {"q2": True}

    class ResumeBatch(FakeBatch):
        def fetch_receipt_for_quarter(
            self, quarter: object, *args: object, **kwargs: object
        ) -> None:
            value = str(quarter)
            fetch_invocations.append(value)
            if value not in calls:
                calls.append(value)

        def build_all_receipts(
            self, quarter: object, *args: object, **kwargs: object
        ) -> tuple[dict[str, Any], ...]:
            if str(quarter).endswith("q2") and failed["q2"]:
                failed["q2"] = False
                raise RuntimeError("synthetic q2 interruption")
            return ()

    import ohmydata.providers.sec.cli as module

    monkeypatch.setattr(module, "SecNportBatch", ResumeBatch)
    argv = [
        "sec",
        "nport",
        "sync",
        "--quarters",
        "2020q1:2020q2",
        "--root",
        str(tmp_path),
        "--universe",
        str(u),
        "--user-agent-file",
        str(ua),
        "--availability-policy",
        "observation-only",
    ]
    assert main(argv) == 2
    assert main(argv) == 0
    assert fetch_invocations.count("2020q1") == 2
    assert calls.count("2020q1") == 1 and calls.count("2020q2") == 1


def test_cli_real_batch_resume_keeps_completed_quarter_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI must resume through the real batch service after q2 transport failure."""
    import hashlib
    import zipfile

    u = tmp_path / "u.json"
    u.write_text(
        json.dumps(
            {
                "schema_version": "sec-equity-etf-universe-v1",
                "funds": [
                    {
                        "symbol": "TST",
                        "cik": "0000000001",
                        "selection_mode": "series",
                        "series_id": "S1",
                    }
                ],
            }
        )
    )
    ua = tmp_path / "ua"
    ua.write_text("fake@example.invalid\n")

    def nport_zip(acc: str, report: str, filing: str) -> bytes:
        rows = {
            "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tSUB_TYPE\tREPORT_ENDING_PERIOD\tREPORT_DATE\tIS_LAST_FILING\n"
            + f"{acc}\t{filing}\tNPORT-P\t{report}\t{report}\tY\n",
            "REGISTRANT.tsv": f"ACCESSION_NUMBER\tCIK\tREGISTRANT_NAME\tFILE_NUM\tLEI\n{acc}\t0000000001\tTest\t1\t\n",
            "FUND_REPORTED_INFO.tsv": f"ACCESSION_NUMBER\tSERIES_ID\tSERIES_NAME\tSERIES_LEI\tTOTAL_ASSETS\tTOTAL_LIABILITIES\tNET_ASSETS\n{acc}\tS1\tTest\t\t100\t1\t99\n",
            "FUND_REPORTED_HOLDING.tsv": f"ACCESSION_NUMBER\tHOLDING_ID\tISSUER_NAME\tISSUER_LEI\tISSUER_TITLE\tISSUER_CUSIP\tBALANCE\tUNIT\tOTHER_UNIT_DESC\tCURRENCY_CODE\tCURRENCY_VALUE\tEXCHANGE_RATE\tPERCENTAGE\tPAYOFF_PROFILE\tASSET_CAT\tOTHER_ASSET\tISSUER_TYPE\tOTHER_ISSUER\tINVESTMENT_COUNTRY\tIS_RESTRICTED_SECURITY\tFAIR_VALUE_LEVEL\tDERIVATIVE_CAT\n{acc}\tH1\tIssuer\t\t\tCUS\t1\tSH\t\tUSD\t\t\t1\t\tEC\t\tCORP\t\tUS\tN\t1\t\n",
            "IDENTIFIERS.tsv": "HOLDING_ID\tIDENTIFIERS_ID\tIDENTIFIER_ISIN\tIDENTIFIER_TICKER\tOTHER_IDENTIFIER\tOTHER_IDENTIFIER_DESC\nH1\tI1\t\tABC\t\t\n",
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name, value in rows.items():
                archive.writestr(name, value)
        return buf.getvalue()

    payloads = {
        "2024q1": nport_zip("0000000001-24-000001", "31-MAR-2024", "01-MAY-2024"),
        "2024q2": nport_zip("0000000001-24-000002", "30-JUN-2024", "01-AUG-2024"),
    }

    def edgar(acc: str, report: str, filing: str) -> bytes:
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

    responses = [
        edgar("0000000001-24-000001", "2024-03-31", "2024-05-01"),
        edgar("0000000001-24-000002", "2024-06-30", "2024-08-01"),
    ]

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = io.BytesIO(body)

    class Transport:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.failed = False

        def open(self, url: str, **_: object) -> Response:
            self.calls.append(url)
            if "/submissions/CIK" in url:
                if len([x for x in self.calls if "/submissions/CIK" in x]) == 2 and not self.failed:
                    self.failed = True
                    raise RuntimeError("synthetic q2 interruption")
                return Response(responses[0 if not self.failed else 1])
            return Response(payloads["2024q1" if "2024q1_nport" in url else "2024q2"])

    transport = Transport()

    def client_factory(_user_agent: str) -> Transport:
        return transport

    monkeypatch.setattr("ohmydata.providers.sec.cli.SecHttpClient", client_factory)
    argv = [
        "sec",
        "nport",
        "sync",
        "--quarters",
        "2024q1:2024q2",
        "--root",
        str(tmp_path),
        "--universe",
        str(u),
        "--user-agent-file",
        str(ua),
        "--availability-policy",
        "observation-only",
    ]
    assert main(argv) == 2
    q1_files = {
        str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "core").rglob("*")
        if p.is_file() and "source_quarter=2024q1" in str(p)
    }
    q1_receipts = {
        str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "state" / "fetch-receipts" / "2024q1").glob("*.json")
    }
    q1_nport_calls = [x for x in transport.calls if "2024q1_nport" in x]
    q1_edgar_calls = [x for x in transport.calls if "/submissions/CIK" in x]
    q1_edgar_count = len(q1_edgar_calls)
    assert main(argv) == 0
    assert q1_files == {
        str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "core").rglob("*")
        if p.is_file() and "source_quarter=2024q1" in str(p)
    }
    assert q1_receipts == {
        str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "state" / "fetch-receipts" / "2024q1").glob("*.json")
    }
    assert [x for x in transport.calls if "2024q1_nport" in x] == q1_nport_calls
    # Both quarters intentionally share one CIK, so EDGAR's current-submissions
    # URL is identical; only the interrupted q2 request may be retried.
    assert len([x for x in transport.calls if "/submissions/CIK" in x]) == q1_edgar_count + 1
    assert list(
        (tmp_path / "core" / "sec-fund-holdings-pit-v1" / "source_quarter=2024q2").glob(
            "artifact=*/partition=*"
        )
    )


def test_provider_error_generic_and_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)
    ua = tmp_path / "ua"
    ua.write_text("secret-contact@example.invalid\n")

    def fail(*_: object, **__: object) -> None:
        raise RuntimeError("secret-contact@example.invalid headers")

    monkeypatch.setattr(FakeBatch, "fetch_receipt_for_quarter", fail)
    assert (
        main(
            [
                "sec",
                "nport",
                "fetch",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
            ]
        )
        != 0
    )
    assert "secret-contact" not in capsys.readouterr().err
    FakeClient.instances.clear()
    assert (
        main(["sec", "nport", "validate", "--quarters", "2020q1:2020q1", "--root", str(tmp_path)])
        == 0
    )
    assert not FakeClient.instances


def test_domain_error_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)

    def fail_fetch(*_args: object, **_kwargs: object) -> None:
        raise CoverageError("secret-contact")

    monkeypatch.setattr(FakeBatch, "fetch_receipt_for_quarter", fail_fetch)
    ua = tmp_path / "ua"
    ua.write_text("fake@example.invalid\n")
    assert (
        main(
            [
                "sec",
                "nport",
                "fetch",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
            ]
        )
        == 2
    )
    output = capsys.readouterr().err
    assert "secret-contact" not in output and "Traceback" not in output


def test_nonexistent_output_root_git_safety(tmp_path: Path) -> None:
    repo_descendant = Path(__file__).resolve().parents[3] / "not-created-output"
    with pytest.raises(ValueError):
        ensure_safe_output_root(repo_descendant)
    ignored = Path(__file__).resolve().parents[3] / "artifacts" / "not-created-output"
    assert ensure_safe_output_root(ignored) == ignored.resolve()
    assert ensure_safe_output_root(tmp_path / "outside") == (tmp_path / "outside").resolve()


def test_cli_progress_stderr_and_quiet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)
    ua = tmp_path / "ua"
    ua.write_text("fake@example.invalid\n")

    # 1. Default run should emit progress to stderr without polluting stdout
    assert (
        main(
            [
                "sec",
                "nport",
                "sync",
                "--quarters",
                "2020q1:2020q2",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
                "--availability-policy",
                "observation-only",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["status"] == "completed"
    assert parsed["count"] == 2
    assert "[1/2] 2020q1: fetching N-PORT artifact & EDGAR metadata..." in captured.err
    assert "[1/2] 2020q1: building core Parquet dataset (observation-only)..." in captured.err
    assert "[1/2] 2020q1: completed in" in captured.err
    assert "[2/2] 2020q2: fetching N-PORT artifact & EDGAR metadata..." in captured.err
    assert "[2/2] 2020q2: completed in" in captured.err

    # 2. Run with --quiet should suppress stderr progress
    assert (
        main(
            [
                "sec",
                "nport",
                "sync",
                "--quarters",
                "2020q1:2020q2",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
                "--availability-policy",
                "observation-only",
                "--quiet",
                "--json",
            ]
        )
        == 0
    )
    quiet_captured = capsys.readouterr()
    assert quiet_captured.err == ""
    quiet_parsed = json.loads(quiet_captured.out)
    assert quiet_parsed["status"] == "completed"


def test_cli_download_progress_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeDownloadBatch(FakeBatch):
        progress: Any = None

        def fetch_receipt_for_quarter(
            self, quarter: object, scheduled: object, digest: str, *, refresh: bool = False
        ) -> None:
            if callable(self.progress):
                self.progress("download", 50 * 1024 * 1024)
                self.progress("download", 100 * 1024 * 1024)

    import ohmydata.providers.sec.cli as module

    monkeypatch.setattr(module, "SecHttpClient", FakeClient)
    monkeypatch.setattr(module, "SecNportBatch", FakeDownloadBatch)

    u = tmp_path / "u.json"
    _universe(u)
    ua = tmp_path / "ua"
    ua.write_text("fake@example.invalid\n")

    assert (
        main(
            [
                "sec",
                "nport",
                "fetch",
                "--quarters",
                "2020q1:2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "[1/1] 2020q1: downloaded 50 MB..." in captured.err
    assert "[1/1] 2020q1: downloaded 100 MB..." in captured.err


def test_cli_quarters_full_and_single(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)

    # 1. --quarters full
    assert (
        main(
            [
                "sec",
                "nport",
                "plan",
                "--quarters",
                "full",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["quarters"][0] == "2019q4"
    assert data["count"] >= 20

    # 2. single quarter token
    assert (
        main(
            [
                "sec",
                "nport",
                "plan",
                "--quarters",
                "2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["quarters"] == ["2020q1"]
    assert data["count"] == 1


def test_cli_config_json_and_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)
    ua = tmp_path / "ua.txt"
    ua.write_text("fake@example.invalid\n")

    # 1. JSON config
    cfg_json = tmp_path / "sync.json"
    cfg_json.write_text(
        json.dumps(
            {
                "quarters": "2020q1:2020q1",
                "root": str(tmp_path),
                "universe": str(u),
                "user_agent_file": str(ua),
                "availability_policy": "observation-only",
            }
        )
    )
    assert main(["sec", "nport", "sync", "--config", str(cfg_json), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "completed"

    # 2. YAML config with dashed keys and comments
    cfg_yaml = tmp_path / "sync.yaml"
    cfg_yaml.write_text(
        f"""
        # test comment
        quarters: 2020q1
        root: {tmp_path}
        universe: {u}
        user-agent-file: {ua}
        availability-policy: observation-only
        quiet: true
        """
    )
    assert main(["sec", "nport", "sync", "--config", str(cfg_yaml)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""

    # 3. CLI override takes precedence over config
    assert (
        main(
            [
                "sec",
                "nport",
                "sync",
                "--config",
                str(cfg_json),
                "--quarters",
                "2020q2:2020q2",
                "--json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["quarters"] == ["2020q2"]


def test_cli_inspect_latest_quarter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    assert main(["sec", "nport", "inspect", "--quarter", "latest", "--root", str(tmp_path)]) == 0


def test_cli_user_agent_with_spaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    u = tmp_path / "u.json"
    _universe(u)
    ua = tmp_path / "ua.txt"
    ua.write_text("Sample Company AdminContact@sample.example.com\n")
    assert (
        main(
            [
                "sec",
                "nport",
                "fetch",
                "--quarters",
                "2020q1",
                "--root",
                str(tmp_path),
                "--universe",
                str(u),
                "--user-agent-file",
                str(ua),
            ]
        )
        == 0
    )
