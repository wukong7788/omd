# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportArgumentType=false

import math
from datetime import UTC, datetime

import pandas as pd
import pytest

from ohmydata.core import EmptyResponseError, PaginationError
from ohmydata.providers.tushare import (
    EmptyPolicy,
    EtfPcfHistoryRequest,
    EtfPcfHistoryResult,
    EtfShConsRequest,
    EtfSzConsRequest,
    TushareClient,
    fetch_etf_pcf_history,
)


def sh_frame(rows):
    fields = EtfShConsRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH").fields
    return pd.DataFrame([{**dict.fromkeys(fields), **row} for row in rows], columns=fields)


def sz_frame(rows):
    fields = EtfSzConsRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="159001.SZ").fields
    return pd.DataFrame([{**dict.fromkeys(fields), **row} for row in rows], columns=fields)


class EndpointFake:
    def __init__(self, frame_factory, overflow=()):
        self.frame_factory = frame_factory
        self.overflow = set(overflow)
        self.calls = []

    def etf_sh_cons(self, **kwargs):
        self.calls.append(("SH", kwargs))
        key = (kwargs.get("start_date"), kwargs.get("end_date"))
        if key in self.overflow:
            return sh_frame(
                [
                    {"trade_date": "20240101", "ts_code": "510050.SH", "con_code": f"{i:06d}.SZ"}
                    for i in range(3000)
                ]
            )
        return self.frame_factory("SH", kwargs)

    def etf_sz_cons(self, **kwargs):
        self.calls.append(("SZ", kwargs))
        return self.frame_factory("SZ", kwargs)


def make_client(fake):
    return TushareClient(fake, clock=lambda: datetime(2024, 1, 2, tzinfo=UTC))


def test_history_bisects_nonoverlapping_and_counts_truncation():
    def rows(exchange, kwargs):
        return sh_frame(
            [
                {
                    "trade_date": kwargs["start_date"],
                    "ts_code": "510050.SH",
                    "con_code": "000002.SZ",
                    "qty": 0,
                    "cpr": math.nan,
                    "rdr": float("inf"),
                    "sca": "-",
                }
            ]
        )

    fake = EndpointFake(rows, overflow={("20240101", "20240104")})
    result = fetch_etf_pcf_history(
        make_client(fake),
        EtfPcfHistoryRequest(
            ts_code="510050.SH",
            exchange="SH",
            start_date="20240101",
            end_date="20240104",
            empty_policy=EmptyPolicy.ALLOW,
        ),
    )
    assert result.total_request_count == 3
    assert result.truncation_count == 1
    assert [(c[1]["start_date"], c[1]["end_date"]) for c in fake.calls] == [
        ("20240101", "20240104"),
        ("20240101", "20240102"),
        ("20240103", "20240104"),
    ]
    assert [
        (p.effective_parameters["start_date"], p.effective_parameters["end_date"])
        for p in result.provenances
    ] == [("20240101", "20240102"), ("20240103", "20240104")]
    assert result.frame.trade_date.tolist() == ["20240101", "20240103"]
    assert result.frame.iloc[0].qty == 0 and math.isnan(result.frame.iloc[0].cpr)
    assert result.frame.iloc[0].rdr == float("inf") and result.frame.iloc[0].sca == "-"


def test_history_multilevel_and_single_day_overflow():
    fake = EndpointFake(
        lambda exchange, kwargs: sh_frame([]),
        overflow={("20240101", "20240104"), ("20240101", "20240102"), ("20240103", "20240104")},
    )
    result = fetch_etf_pcf_history(
        make_client(fake),
        EtfPcfHistoryRequest(
            ts_code="510050.SH",
            exchange="SH",
            start_date="20240101",
            end_date="20240104",
            empty_policy=EmptyPolicy.ALLOW,
        ),
    )
    assert result.total_request_count == 7 and result.truncation_count == 3
    overflow = EndpointFake(
        lambda exchange, kwargs: sh_frame([]), overflow={("20240101", "20240101")}
    )
    with pytest.raises(PaginationError):
        fetch_etf_pcf_history(
            make_client(overflow),
            EtfPcfHistoryRequest(
                ts_code="510050.SH",
                exchange="SH",
                start_date="20240101",
                end_date="20240101",
                empty_policy=EmptyPolicy.ALLOW,
            ),
        )


def test_history_empty_policy_and_defensive_result():
    fake = EndpointFake(lambda exchange, kwargs: sh_frame([]))
    request = EtfPcfHistoryRequest(
        ts_code="510050.SH",
        exchange="SH",
        start_date="20240101",
        end_date="20240101",
        empty_policy=EmptyPolicy.ALLOW,
    )
    result = fetch_etf_pcf_history(make_client(fake), request)
    assert result.frame.empty and result.total_request_count == 1 and result.truncation_count == 0
    assert len(result.provenances) == 1 and result.provenances[0].row_count == 0
    returned = result.frame
    returned["ts_code"] = "MUTATED"
    assert result.frame.empty
    error_fake = EndpointFake(lambda exchange, kwargs: sh_frame([]))
    with pytest.raises(EmptyResponseError):
        fetch_etf_pcf_history(
            make_client(error_fake),
            EtfPcfHistoryRequest(
                ts_code="510050.SH",
                exchange="SH",
                start_date="20240101",
                end_date="20240101",
                empty_policy=EmptyPolicy.ERROR,
            ),
        )


@pytest.mark.parametrize(
    "exchange, code, request_type",
    [("SH", "510050.SH", EtfShConsRequest), ("SZ", "159001.SZ", EtfSzConsRequest)],
)
def test_history_routes_exchange_and_validates_suffix(exchange, code, request_type):
    fake = EndpointFake(lambda ex, kwargs: sh_frame([]) if ex == "SH" else sz_frame([]))
    request = EtfPcfHistoryRequest(
        ts_code=code,
        exchange=exchange,
        start_date="20240101",
        end_date="20240101",
        empty_policy=EmptyPolicy.ALLOW,
    )
    fetch_etf_pcf_history(make_client(fake), request)
    assert fake.calls[0][0] == exchange
    with pytest.raises(ValueError):
        EtfPcfHistoryRequest(
            ts_code=code,
            exchange="SZ" if exchange == "SH" else "SH",
            start_date="20240101",
            end_date="20240101",
            empty_policy=EmptyPolicy.ALLOW,
        )


def test_history_request_rejects_bad_dates_and_requires_explicit_empty_policy():
    with pytest.raises(ValueError):
        EtfPcfHistoryRequest(
            ts_code="510050.SH",
            exchange="SH",
            start_date="20240230",
            end_date="20240301",
            empty_policy=EmptyPolicy.ALLOW,
        )
    with pytest.raises(TypeError):
        EtfPcfHistoryRequest(
            ts_code="510050.SH",
            exchange="SH",
            start_date="20240101",
            end_date="20240101",
            empty_policy="ALLOW",
        )


def test_result_has_only_frozen_public_attributes():
    result = EtfPcfHistoryResult(pd.DataFrame({"x": [1]}), (), 0, 0)
    assert set(vars(result)) == {"_frame", "provenances", "total_request_count", "truncation_count"}
    assert not hasattr(result, "data") and not hasattr(result, "request_count")
