from __future__ import annotations

import json
import stat
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.cli import main as cli_main
from ohmydata.providers.sec import _companyfacts_models as cf_models
from ohmydata.providers.sec.bulk_zip import (
    BulkZipBombError,
    BulkZipSecurityError,
    SecBulkCikOutcomeStatus,
    build_bulk_companyfacts_dataset,
    project_bulk_companyfacts_zips,
)
from ohmydata.providers.sec.errors import ResourceLimitError, SchemaMismatchError

_CIK = "0000000123"


def _sub_cols(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    keys = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
        "acceptanceDateTime",
    )
    return {k: [r[k] for r in rows] for k in keys}


def _make_multi_cik_zips(tmp_path: Path, count: int = 10) -> tuple[Path, Path]:
    cf_zip = tmp_path / "companyfacts.zip"
    sub_zip = tmp_path / "submissions.zip"
    with (
        zipfile.ZipFile(cf_zip, "w", compression=zipfile.ZIP_DEFLATED) as cf_zf,
        zipfile.ZipFile(sub_zip, "w", compression=zipfile.ZIP_DEFLATED) as sub_zf,
    ):
        for idx in range(1, count + 1):
            cik = f"{idx:010d}"
            accn_q3 = f"{cik}-25-000003"
            accn_fy = f"{cik}-26-000001"
            accn_old = f"{cik}-20-000001"
            is_ifrs = idx % 3 == 0
            if is_ifrs:
                facts: dict[str, object] = {
                    "ifrs-full": {
                        "UnusedConceptAlpha": {
                            "label": "Unused",
                            "description": 'Includes escaped quote \\" and braces {[}]',
                            "units": {"EUR": [{"val": 1, "end": "2025-12-31"}]},
                        },
                        "Revenue": {
                            "units": {
                                "EUR": [
                                    {
                                        "val": 1_000_000 * idx,
                                        "accn": accn_fy,
                                        "form": "20-F",
                                        "fy": 2025,
                                        "fp": "FY",
                                        "start": "2025-01-01",
                                        "end": "2025-12-31",
                                        "filed": "2026-03-10",
                                    }
                                ]
                            }
                        },
                        "DilutedEarningsLossPerShare": {
                            "units": {
                                "EUR/shares": [
                                    {
                                        "val": Decimal("12.345") + Decimal(idx),
                                        "accn": accn_fy,
                                        "form": "20-F",
                                        "fy": 2025,
                                        "fp": "FY",
                                        "start": "2025-01-01",
                                        "end": "2025-12-31",
                                        "filed": "2026-03-10",
                                    }
                                ]
                            }
                        },
                    }
                }
                recent_rows = [
                    {
                        "accessionNumber": accn_fy,
                        "form": "20-F",
                        "filingDate": "2026-03-10",
                        "reportDate": "2025-12-31",
                        "primaryDocument": "f20.htm",
                        "acceptanceDateTime": "2026-03-10T15:00:00.000Z",
                    }
                ]
                hist_files: list[dict[str, object]] = []
            else:
                facts = {
                    "us-gaap": {
                        "UnusedConceptOne": {
                            "label": "Unused One",
                            "units": {
                                "USD": [
                                    {
                                        "val": 42,
                                        "accn": accn_fy,
                                        "form": "10-K",
                                        "fy": 2025,
                                        "fp": "FY",
                                        "start": "2025-01-01",
                                        "end": "2025-12-31",
                                        "filed": "2026-02-15",
                                    }
                                ]
                            },
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "val": 500_000 * idx,
                                        "accn": accn_old,
                                        "form": "10-K",
                                        "fy": 2019,
                                        "fp": "FY",
                                        "start": "2019-01-01",
                                        "end": "2019-12-31",
                                        "filed": "2020-02-15",
                                    },
                                    {
                                        "val": 750_000 * idx,
                                        "accn": accn_q3,
                                        "form": "10-Q",
                                        "fy": 2025,
                                        "fp": "Q3",
                                        "start": "2025-01-01",
                                        "end": "2025-09-30",
                                        "filed": "2025-11-05",
                                    },
                                    {
                                        "val": 1_100_000 * idx,
                                        "accn": accn_fy,
                                        "form": "10-K",
                                        "fy": 2025,
                                        "fp": "FY",
                                        "start": "2025-01-01",
                                        "end": "2025-12-31",
                                        "filed": "2026-02-15",
                                    },
                                ]
                            }
                        },
                        "EarningsPerShareDiluted": {
                            "units": {
                                "USD/shares": [
                                    {
                                        "val": Decimal("1.10") + Decimal(idx) * Decimal("0.01"),
                                        "accn": accn_fy,
                                        "form": "10-K",
                                        "fy": 2025,
                                        "fp": "FY",
                                        "start": "2025-01-01",
                                        "end": "2025-12-31",
                                        "filed": "2026-02-15",
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
                                        "val": 10_000_000 + idx,
                                        "accn": accn_fy,
                                        "form": "10-K",
                                        "fy": 2025,
                                        "fp": "FY",
                                        "end": "2026-01-31",
                                        "filed": "2026-02-15",
                                    }
                                ]
                            }
                        }
                    },
                }
                recent_rows = [
                    {
                        "accessionNumber": accn_fy,
                        "form": "10-K",
                        "filingDate": "2026-02-15",
                        "reportDate": "2025-12-31",
                        "primaryDocument": "f10k.htm",
                        "acceptanceDateTime": "2026-02-15T21:00:00.000Z",
                    },
                    {
                        "accessionNumber": accn_q3,
                        "form": "10-Q",
                        "filingDate": "2025-11-05",
                        "reportDate": "2025-09-30",
                        "primaryDocument": "f10q.htm",
                        "acceptanceDateTime": "2025-11-05T21:00:00.000Z",
                    },
                ]
                hist_name = f"CIK{cik}-submissions-001.json"
                hist_files = [
                    {
                        "name": hist_name,
                        "filingCount": 1,
                        "filingFrom": "2020-01-01",
                        "filingTo": "2020-12-31",
                    }
                ]
                sub_zf.writestr(
                    hist_name,
                    json.dumps(
                        _sub_cols(
                            [
                                {
                                    "accessionNumber": accn_old,
                                    "form": "10-K",
                                    "filingDate": "2020-02-15",
                                    "reportDate": "2019-12-31",
                                    "primaryDocument": "old10k.htm",
                                    "acceptanceDateTime": "2020-02-15T21:00:00.000Z",
                                }
                            ]
                        )
                    ),
                )

            cf_payload = {"cik": idx, "entityName": f"Issuer {idx}", "facts": facts}
            cf_zf.writestr(
                f"CIK{cik}.json",
                json.dumps(
                    cf_payload,
                    separators=(",", ":"),
                    default=lambda v: float(v) if isinstance(v, Decimal) else v,
                ),
            )
            sub_payload = {
                "cik": cik,
                "entityType": "operating",
                "name": f"Issuer {idx}",
                "tickers": [f"SYM{idx}"],
                "filings": {"recent": _sub_cols(recent_rows), "files": hist_files},
            }
            sub_zf.writestr(f"CIK{cik}.json", json.dumps(sub_payload, separators=(",", ":")))

        # Add EMPTY, MISSING_SUBMISSIONS, and PARSE_FAILED CIKs
        cf_zf.writestr("CIK0000999001.json", '{"cik":999001,"facts":{"us-gaap":{}}}')
        sub_zf.writestr(
            "CIK0000999001.json",
            json.dumps(
                {
                    "cik": "0000999001",
                    "name": "Empty Co",
                    "tickers": [],
                    "filings": {"recent": _sub_cols([]), "files": []},
                }
            ),
        )
        cf_zf.writestr("CIK0000999002.json", '{"cik":999002,"facts":{"us-gaap":{}}}')
        cf_zf.writestr("CIK0000999003.json", '{"cik":999003,"facts":{"us-gaap":{')
        sub_zf.writestr(
            "CIK0000999003.json",
            json.dumps(
                {
                    "cik": "0000999003",
                    "name": "Broken Co",
                    "tickers": [],
                    "filings": {"recent": _sub_cols([]), "files": []},
                }
            ),
        )
    return cf_zip, sub_zip


def test_strict_companyfacts_json_security_and_precision(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Escaped quotes & backslashes inside strings containing structural brackets
    tricky = rb'{"a":"x{[}]\"\\y","b":"\\\\","c":"end\\","val":0.1,"big":12345678901234567890.123456789,"sci":1e-10,"int":42}'
    parsed = cf_models._strict_companyfacts_json(tricky)
    assert parsed["a"] == 'x{[}]"\\y'
    assert parsed["b"] == "\\\\"
    assert parsed["c"] == "end\\"
    assert parsed["val"] == Decimal("0.1")
    assert parsed["big"] == Decimal("12345678901234567890.123456789")
    assert parsed["sci"] == Decimal("1E-10")
    assert type(parsed["int"]) is int and parsed["int"] == 42

    # 2. Exact depth boundary: 64 allowed, 65 rejected (dict and list, plus deep RecursionError)
    depth_64 = b'{"a":' * 64 + b"1" + b"}" * 64
    assert isinstance(cf_models._strict_companyfacts_json(depth_64), dict)

    depth_65_dict = b'{"a":' * 65 + b"1" + b"}" * 65
    with pytest.raises(ResourceLimitError, match="structure limit"):
        cf_models._strict_companyfacts_json(depth_65_dict)

    depth_65_list = b"[" * 65 + b"]" * 65
    with pytest.raises(ResourceLimitError, match="structure limit"):
        cf_models._strict_companyfacts_json(depth_65_list)

    depth_2000 = b"[" * 2000 + b"]" * 2000
    with pytest.raises(ResourceLimitError, match="structure limit"):
        cf_models._strict_companyfacts_json(depth_2000)

    unclosed_depth_65 = b'{"a":' * 65 + b"1"
    with pytest.raises(ResourceLimitError, match="structure limit"):
        cf_models._strict_companyfacts_json(unclosed_depth_65)

    # Adversarial string containing 33 closing braces cannot hide true nesting depth 65
    adv_depth_65 = (
        b'{"a":' * 33
        + b'{"x":"'
        + b"}" * 33
        + b'","y":'
        + b'{"b":' * 31
        + b"1"
        + b"}" * 31
        + b"}"
        + b"}" * 33
    )
    with pytest.raises(ResourceLimitError, match="structure limit"):
        cf_models._strict_companyfacts_json(adv_depth_65)

    # 3. Duplicate keys, NaN/Infinity/-Infinity, truncated/unbalanced/unterminated/non-UTF-8
    for bad in (
        b'{"a":1,"a":2}',
        b'{"a":{"k":1,"k":2}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b'{"a":1',
        b'{"a":1}}',
        b'{"a":"unterminated',
        b'{"a":"trailing\\',
        b'{"a":"\xff"}',
        b"[]",
    ):
        with pytest.raises(SchemaMismatchError):
            cf_models._strict_companyfacts_json(bad)

    # 4. Node limit enforcement (and commas inside a string MUST NOT false-positive)
    monkeypatch.setattr(cf_models, "_MAX_JSON_NODES", 20)
    too_many_nodes = b"{" + b",".join(f'"k{i}":{i}'.encode() for i in range(25)) + b"}"
    with pytest.raises(ResourceLimitError, match="structure limit"):
        cf_models._strict_companyfacts_json(too_many_nodes)

    commas_in_string = b'{"a":"' + b"," * 50 + b'"}'
    assert cf_models._strict_companyfacts_json(commas_in_string) == {"a": "," * 50}

    # 5. Byte limit enforcement
    monkeypatch.setattr(cf_models, "_MAX_BODY_BYTES", 32)
    with pytest.raises(ResourceLimitError, match="byte limit"):
        cf_models._strict_companyfacts_json(b'{"a":"' + b"x" * 40 + b'"}')


def test_companyfacts_payload_preserves_tags_across_braced_descriptions_and_rejects_unused_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accn = f"{_CIK}-25-000001"
    # 1. Valid JSON where stray '{' and '}' in two unused tag descriptions bracket Revenues:
    # MUST NOT swallow or drop Revenues!
    braced_obj = {
        "cik": int(_CIK),
        "entityName": "Braced Descriptions Corp",
        "facts": {
            "us-gaap": {
                "UnusedA": {
                    "label": "Unused A",
                    "description": "see {note 1",
                    "units": {"USD": []},
                },
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 987_654_321,
                                "accn": accn,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-15",
                            }
                        ]
                    }
                },
                "EarningsPerShareDiluted": {
                    "units": {
                        "USD/shares": [
                            {
                                "val": 0.1,
                                "accn": accn,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-15",
                            }
                        ]
                    }
                },
                "UnusedB": {
                    "label": "Unused B",
                    "description": "end note 1}",
                    "units": {"USD": []},
                },
            }
        },
    }
    compact_bytes = json.dumps(braced_obj, separators=(",", ":")).encode("utf-8")
    pretty_bytes = json.dumps(braced_obj, indent=2).encode("utf-8")

    parsed_compact = cf_models.parse_sec_companyfacts_payload(compact_bytes, _CIK)
    parsed_pretty = cf_models.parse_sec_companyfacts_payload(pretty_bytes, _CIK)
    parsed_dict = cf_models.parse_sec_companyfacts_payload(
        cf_models._strict_companyfacts_json(compact_bytes), _CIK
    )
    assert parsed_compact == parsed_pretty == parsed_dict
    assert len(parsed_compact) == 2
    rev = next(f for f in parsed_compact if f.tag == "Revenues")
    assert rev.value == Decimal(987_654_321)
    eps = next(f for f in parsed_compact if f.tag == "EarningsPerShareDiluted")
    assert eps.value == Decimal("0.1")

    # 2. Duplicate keys anywhere (top, namespace, tag, unused tag inner object, canonical row) rejected
    for dup_payload in (
        b'{"cik":123,"cik":123,"facts":{"us-gaap":{}}}',
        b'{"cik":123,"facts":{"us-gaap":{},"us-gaap":{}}}',
        b'{"cik":123,"facts":{"us-gaap":{"Unused":{},"Unused":{}}}}',
        b'{"cik":123,"facts":{"us-gaap":{"Unused":{"units":{"USD":[]},"units":{"USD":[]}},"Revenues":{"units":{"USD":[]}}}}}',
        (
            b'{"cik":123,"facts":{"us-gaap":{"Revenues":{"units":{"USD":'
            b'[{"val":1,"val":2,"accn":"0000000123-25-000001","form":"10-K",'
            b'"fy":2025,"fp":"FY","start":"2025-01-01","end":"2025-12-31","filed":"2026-02-15"}]}}}}}'
        ),
    ):
        with pytest.raises(SchemaMismatchError, match="duplicate key"):
            cf_models.parse_sec_companyfacts_payload(dup_payload, _CIK)

    # 3. Non-JSON garbage or NaN inside unused tags rejected strictly
    for invalid_unused in (
        b'{"cik":123,"facts":{"us-gaap":{"Unused":{NOT_JSON_AT_ALL:{}},"Revenues":{"units":{"USD":[]}}}}}',
        b'{"cik":123,"facts":{"us-gaap":{"Unused":{"units":{"USD":[{"val":NaN}]}}}}}',
        (
            b'{"cik":123,"facts":{"us-gaap":{"Unused":{"description":"{{[]}},"Revenues":'
            b'{"units":{}},"Next":{"description":"}"}}}}'
        ),
    ):
        with pytest.raises(SchemaMismatchError):
            cf_models.parse_sec_companyfacts_payload(invalid_unused, _CIK)

    # 4. _MAX_TAGS enforced across unused + kept tags
    monkeypatch.setattr(cf_models, "_MAX_TAGS", 2)
    with pytest.raises(ResourceLimitError, match="tag limit"):
        cf_models.parse_sec_companyfacts_payload(compact_bytes, _CIK)


def test_worker_cached_zip_handle_requires_matching_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ohmydata.providers.sec import _bulk_zip_parallel as bzp

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    cf_a, sub_a = _make_multi_cik_zips(dir_a, count=1)
    cf_b, sub_b = _make_multi_cik_zips(dir_b, count=2)
    task_b = bzp.BulkWorkerChunkTask(
        cf_path=str(cf_b),
        sub_path=str(sub_b),
        cik_members=(("0000000002", "CIK0000000002.json"),),
        allowed_forms=frozenset({"10-K", "10-Q"}),
        start_year=2015,
        end_year=2026,
        acceptance_upper=None,
        max_entry_bytes=128 * 1024**2,
        max_total_bytes=128 * 1024**2,
        max_history_pages_per_cik=256,
    )
    with zipfile.ZipFile(cf_a, "r") as zf_cf_a, zipfile.ZipFile(sub_a, "r") as zf_sub_a:
        monkeypatch.setattr(bzp, "_WORKER_CF_ZF", zf_cf_a)
        monkeypatch.setattr(bzp, "_WORKER_SUB_ZF", zf_sub_a)
        monkeypatch.setattr(bzp, "_WORKER_CF_PATH", str(cf_a))
        monkeypatch.setattr(bzp, "_WORKER_SUB_PATH", str(sub_a))
        # Must ignore cached handles for archive A and read CIK 0000000002 from archive B
        res = bzp.run_bulk_worker_chunk(task_b)
        assert len(res.outcomes) == 1
        assert res.outcomes[0].cik == "0000000002"
        assert res.outcomes[0].status is SecBulkCikOutcomeStatus.OK


def test_parallel_workers_byte_identical_and_enforces_security_limits(tmp_path: Path) -> None:
    cf_zip, sub_zip = _make_multi_cik_zips(tmp_path, count=10)

    proj_1 = project_bulk_companyfacts_zips(cf_zip, sub_zip, workers=1)
    proj_4 = project_bulk_companyfacts_zips(cf_zip, sub_zip, workers=4)
    assert proj_4 == proj_1
    assert any(o.status is SecBulkCikOutcomeStatus.OK for o in proj_4.outcomes)
    assert any(o.status is SecBulkCikOutcomeStatus.EMPTY for o in proj_4.outcomes)
    assert any(o.status is SecBulkCikOutcomeStatus.MISSING_SUBMISSIONS for o in proj_4.outcomes)
    assert any(o.status is SecBulkCikOutcomeStatus.PARSE_FAILED for o in proj_4.outcomes)

    out_1 = tmp_path / "ds_w1"
    out_4 = tmp_path / "ds_w4"
    m1 = build_bulk_companyfacts_dataset(cf_zip, sub_zip, out_1, workers=1)
    m4 = build_bulk_companyfacts_dataset(cf_zip, sub_zip, out_4, workers=4)
    assert m4 == m1
    assert (out_1 / "fundamentals.parquet").read_bytes() == (
        out_4 / "fundamentals.parquet"
    ).read_bytes()
    assert (out_1 / "facts.parquet").read_bytes() == (out_4 / "facts.parquet").read_bytes()
    assert (out_1 / "manifest.json").read_bytes() == (out_4 / "manifest.json").read_bytes()

    # Aggregate max_total_bytes enforced across workers (even when each CIK < limit)
    # Each CIK is ~1.5 KB; 10 CIKs > 4 KB total while each single CIK < 4 KB
    with pytest.raises(BulkZipBombError, match="cumulative decompressed bytes"):
        project_bulk_companyfacts_zips(cf_zip, sub_zip, max_total_bytes=4096, workers=4)

    # Per-entry limit enforced with workers=4
    with pytest.raises(BulkZipBombError, match="per-entry byte limit"):
        project_bulk_companyfacts_zips(cf_zip, sub_zip, max_entry_bytes=64, workers=4)

    # Archive security checks (Zip Slip & symlink) enforced with workers=4
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../escape.json", "{}")
    with pytest.raises(BulkZipSecurityError, match="path traversal"):
        project_bulk_companyfacts_zips(evil_zip, sub_zip, workers=4)

    sym_zip = tmp_path / "sym.zip"
    with zipfile.ZipFile(sym_zip, "w") as zf:
        info = zipfile.ZipInfo("CIK0000000001.json")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(BulkZipSecurityError, match="symlink"):
        project_bulk_companyfacts_zips(sym_zip, sub_zip, workers=4)

    # Invalid workers rejected strictly
    for bad_workers in (0, -1, True, False, 65, 1.5, "4"):
        with pytest.raises(ValueError, match="workers"):
            project_bulk_companyfacts_zips(
                cf_zip,
                sub_zip,
                workers=bad_workers,  # type: ignore[arg-type]
            )


def test_cli_bulk_project_workers_flag_and_config_bool_rejection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cf_zip, sub_zip = _make_multi_cik_zips(tmp_path, count=6)
    out_w1 = tmp_path / "cli_w1"
    out_w2 = tmp_path / "cli_w2"

    rc1 = cli_main(
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(out_w1),
            "--workers",
            "1",
            "--json",
            "--quiet",
        ]
    )
    assert rc1 == 0
    capsys.readouterr()

    rc2 = cli_main(
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(out_w2),
            "--workers",
            "2",
            "--json",
            "--quiet",
        ]
    )
    assert rc2 == 0
    capsys.readouterr()

    assert (out_w1 / "manifest.json").read_bytes() == (out_w2 / "manifest.json").read_bytes()
    assert (out_w1 / "fundamentals.parquet").read_bytes() == (
        out_w2 / "fundamentals.parquet"
    ).read_bytes()

    # Invalid CLI --workers fails explicitly
    rc_bad = cli_main(
        [
            "sec",
            "financials",
            "bulk-project",
            "--companyfacts-zip",
            str(cf_zip),
            "--submissions-zip",
            str(sub_zip),
            "--output",
            str(tmp_path / "cli_bad"),
            "--workers",
            "0",
            "--quiet",
        ]
    )
    assert rc_bad != 0
    capsys.readouterr()

    # JSON --config with boolean workers fails closed instead of coercing True -> 1
    cfg = tmp_path / "bulk_cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "companyfacts_zip": str(cf_zip),
                "submissions_zip": str(sub_zip),
                "output": str(tmp_path / "cli_bool"),
                "workers": True,
            }
        ),
        encoding="utf-8",
    )
    rc_bool = cli_main(["sec", "financials", "bulk-project", "--config", str(cfg), "--quiet"])
    assert rc_bool != 0
    err = capsys.readouterr().err
    assert "--workers must be an integer" in err
