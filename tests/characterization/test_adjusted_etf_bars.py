"""Offline characterization of the consumers' shared adjusted-bar semantics."""

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parents[1] / "fixtures" / "characterization"
REQUIRED_DAILY = {"trade_date", "open", "high", "low", "close"}


def _load(name: str) -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / name).read_text())
    return payload["rows"]


def _adjusted_bars(daily: list[dict[str, Any]], adj: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not daily:
        return []
    missing = REQUIRED_DAILY - set().union(*(set(row) for row in daily))
    if missing:
        raise ValueError(f"fund_daily missing required fields: {sorted(missing)}")
    if any(REQUIRED_DAILY - set(row) for row in daily):
        raise ValueError("fund_daily row missing required fields")
    dates = [row["trade_date"] for row in adj]
    if len(dates) != len(set(dates)):
        raise ValueError("fund_adj contains duplicate trade_date values")
    factors = {row["trade_date"]: row.get("adj_factor") for row in adj}
    if any(row["trade_date"] not in factors for row in daily):
        raise ValueError("fund_adj does not completely cover fund_daily")
    result: list[dict[str, Any]] = []
    for row in sorted(daily, key=lambda item: item["trade_date"]):
        factor = factors[row["trade_date"]]
        if factor is None or any(row[field] is None for field in ("open", "high", "low", "close")):
            raise ValueError("strict adjusted bars reject missing numeric data")
        result.append({**row, "adj_factor": factor, "adj_close": row["close"] * factor})
    return result


def test_shared_adjusted_bar_formula_and_order() -> None:
    bars = _adjusted_bars(_load("fund_daily.json"), _load("fund_adj.json"))
    assert [row["trade_date"] for row in bars] == ["20240102", "20240103", "20240104"]
    assert bars[0]["open"] == 9.9
    assert bars[0]["adj_factor"] == 1.1
    assert bars[0]["adj_close"] == pytest.approx(11.0)


def test_rejects_missing_required_daily_field() -> None:
    daily = _load("fund_daily.json")
    del daily[0]["low"]
    with pytest.raises(ValueError, match="missing required"):
        _adjusted_bars(daily, _load("fund_adj.json"))


def test_rejects_duplicate_adjustment_dates() -> None:
    adj = _load("fund_adj.json")
    adj.append(dict(adj[0]))
    with pytest.raises(ValueError, match="duplicate"):
        _adjusted_bars(_load("fund_daily.json"), adj)


def test_rejects_incomplete_adjustment_coverage() -> None:
    adj = _load("fund_adj.json")[:-1]
    with pytest.raises(ValueError, match="completely cover"):
        _adjusted_bars(_load("fund_daily.json"), adj)


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_rejects_missing_numeric_in_strict_mode(field: str) -> None:
    daily = _load("fund_daily.json")
    daily[0][field] = None
    with pytest.raises(ValueError, match="missing numeric"):
        _adjusted_bars(daily, _load("fund_adj.json"))


def test_rejects_missing_adjustment_factor() -> None:
    adj = _load("fund_adj.json")
    adj[0]["adj_factor"] = None
    with pytest.raises(ValueError, match="missing numeric"):
        _adjusted_bars(_load("fund_daily.json"), adj)
