# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownLambdaType=false, reportArgumentType=false, reportUnnecessaryIsInstance=false
"""stock_basic and index_member_all typed endpoint tests."""

import math
from datetime import UTC, datetime

import pandas as pd
import pytest

from ohmydata.core.errors import PaginationError, SchemaMismatchError
from ohmydata.providers.tushare import (
    EmptyPolicy,
    IndexMemberAllRequest,
    StockBasicRequest,
    TushareClient,
)


def stock_basic_frame(rows, fields=None):
    request_fields = (
        fields or StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ").fields
    )
    return pd.DataFrame(
        [{**dict.fromkeys(request_fields), **row} for row in rows], columns=request_fields
    )


def index_member_frame(rows, fields=None):
    request_fields = (
        fields or IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ").fields
    )
    defaults = {
        "ts_code": "000001.SZ",
        "l1_code": "801010.SI",
        "l2_code": "801011.SI",
        "l3_code": "801013.SI",
        "in_date": "20200101",
        "is_new": "Y",
    }
    return pd.DataFrame(
        [{**dict.fromkeys(request_fields), **defaults, **row} for row in rows],
        columns=request_fields,
    )


class EndpointFake:
    def __init__(self, frames, endpoint):
        self.frames = frames
        self.endpoint = endpoint
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            return self.frames.pop(0)

        return call


def make_client(fake):
    return TushareClient(fake, clock=lambda: datetime(2024, 1, 2, tzinfo=UTC))


# ---- StockBasicRequest request validation ----


def test_stock_basic_requires_explicit_selector_or_all_market():
    with pytest.raises(ValueError, match="all_market"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW)
    StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, all_market=True)
    StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ")


def test_stock_basic_selectors_must_be_nonblank():
    with pytest.raises(ValueError, match="non-empty"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="")
    with pytest.raises(ValueError, match="non-empty"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, name="  ")


def test_stock_basic_rejects_unknown_enum_values():
    with pytest.raises(ValueError, match="market"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, market="未来")
    with pytest.raises(ValueError, match="exchange"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, exchange="NYSE")
    with pytest.raises(ValueError, match="list_status"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, list_status="X")
    with pytest.raises(ValueError, match="is_hs"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, is_hs="X")


def test_stock_basic_fields_must_include_ts_code():
    with pytest.raises(ValueError, match="ts_code"):
        StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ", fields=("name",))


def test_stock_basic_default_fields_preserve_identity_and_industry():
    request = StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ")
    for field in (
        "ts_code",
        "symbol",
        "name",
        "market",
        "exchange",
        "list_status",
        "list_date",
        "delist_date",
        "industry",
    ):
        assert field in request.fields
    assert request.endpoint == "stock_basic"


def test_stock_basic_spec_sends_only_explicit_parameters():
    request = StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ")
    assert request.spec.parameters == {"ts_code": "000001.SZ"}
    request = StockBasicRequest(empty_policy=EmptyPolicy.ALLOW, all_market=True)
    assert request.spec.parameters == {}


# ---- stock_basic fetch behavior ----


def test_fetch_stock_basic_returns_provider_native_frame():
    fake = EndpointFake(
        [
            stock_basic_frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "symbol": "000001",
                        "name": "平安银行",
                        "industry": "银行",
                        "list_status": "L",
                        "list_date": "19910403",
                    }
                ]
            )
        ],
        "stock_basic",
    )
    result = make_client(fake).fetch_stock_basic(
        StockBasicRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
    )
    assert result.frame.iloc[0]["name"] == "平安银行"
    assert result.frame.iloc[0]["industry"] == "银行"
    assert result.frame.iloc[0]["list_date"] == "19910403"
    assert result.provenance.endpoint == "stock_basic"


def test_fetch_stock_basic_rejects_rows_outside_selector_scope():
    fake = EndpointFake(
        [
            stock_basic_frame(
                [
                    {"ts_code": "000001.SZ", "name": "平安银行"},
                    {"ts_code": "600000.SH", "name": "浦发银行"},
                ]
            )
        ],
        "stock_basic",
    )
    with pytest.raises(SchemaMismatchError, match="outside request scope"):
        make_client(fake).fetch_stock_basic(
            StockBasicRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
        )


def test_fetch_stock_basic_rejects_duplicate_ts_code():
    fake = EndpointFake(
        [
            stock_basic_frame(
                [{"ts_code": "000001.SZ"}, {"ts_code": "000001.SZ", "name": "平安银行2"}]
            )
        ],
        "stock_basic",
    )
    with pytest.raises(SchemaMismatchError, match="duplicate endpoint key"):
        make_client(fake).fetch_stock_basic(
            StockBasicRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
        )


def test_fetch_stock_basic_rejects_exact_documented_cap():
    rows = [{"ts_code": f"{i:06d}.SZ", "name": f"n{i}"} for i in range(6000)]
    fake = EndpointFake([stock_basic_frame(rows)], "stock_basic")
    with pytest.raises(PaginationError, match="ambiguous row cap"):
        make_client(fake).fetch_stock_basic(
            StockBasicRequest(empty_policy=EmptyPolicy.ERROR, all_market=True)
        )


def test_fetch_stock_basic_allows_below_cap_and_empty_policy():
    fake = EndpointFake([stock_basic_frame([{"ts_code": "000001.SZ"}])], "stock_basic")
    result = make_client(fake).fetch_stock_basic(
        StockBasicRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
    )
    assert result.frame.iloc[0]["ts_code"] == "000001.SZ"


# ---- IndexMemberAllRequest request validation ----


def test_index_member_all_requires_classification_or_stock_selector():
    with pytest.raises(ValueError, match="at least one"):
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW)
    with pytest.raises(ValueError, match="at least one"):
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, is_new="Y")
    IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, l1_code="801010.SI")
    IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ")


def test_index_member_all_rejects_bad_is_new_and_blank_selectors():
    with pytest.raises(ValueError, match="is_new"):
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ", is_new="X")
    with pytest.raises(ValueError, match="non-empty"):
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="  ")


def test_index_member_all_complete_history_requires_single_ts_code():
    with pytest.raises(ValueError, match="complete_history"):
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, complete_history=True)
    with pytest.raises(ValueError, match="complete_history"):
        IndexMemberAllRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ,600000.SH", complete_history=True
        )
    with pytest.raises(ValueError, match="complete_history"):
        IndexMemberAllRequest(
            empty_policy=EmptyPolicy.ALLOW,
            ts_code="000001.SZ",
            l1_code="801010.SI",
            complete_history=True,
        )
    request = IndexMemberAllRequest(
        empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ", complete_history=True
    )
    assert request.spec.parameters == {"ts_code": "000001.SZ"}


def test_index_member_all_default_fields_and_required_identity():
    request = IndexMemberAllRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ")
    for field in (
        "l1_code",
        "l1_name",
        "l2_code",
        "l2_name",
        "l3_code",
        "l3_name",
        "ts_code",
        "name",
        "in_date",
        "out_date",
        "is_new",
    ):
        assert field in request.fields
    with pytest.raises(ValueError, match="fields must include"):
        IndexMemberAllRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ", fields=("ts_code", "name")
        )


# ---- index_member_all fetch behavior ----


def test_fetch_index_member_all_preserves_open_out_date_and_ordering():
    fake = EndpointFake(
        [
            index_member_frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "l2_code": "801011.SI",
                        "l3_code": "801013.SI",
                        "in_date": "20200101",
                        "out_date": math.nan,
                        "is_new": "Y",
                    },
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "l2_code": "801011.SI",
                        "l3_code": "801013.SI",
                        "in_date": "20150101",
                        "out_date": "20191231",
                        "is_new": "N",
                    },
                ]
            )
        ],
        "index_member_all",
    )
    result = make_client(fake).fetch_index_member_all(
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
    )
    assert result.frame.iloc[0]["in_date"] == "20150101"
    assert result.frame.iloc[1]["in_date"] == "20200101"
    assert math.isnan(result.frame.iloc[1]["out_date"])  # type: ignore[arg-type]


def test_fetch_index_member_all_preserves_pandas_na_open_out_date():
    fake = EndpointFake(
        [index_member_frame([{"ts_code": "000001.SZ", "out_date": pd.NA}])],
        "index_member_all",
    )
    result = make_client(fake).fetch_index_member_all(
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
    )
    assert pd.isna(result.frame.iloc[0]["out_date"])


def test_fetch_index_member_all_preserves_exact_duplicate_rows():
    fake = EndpointFake(
        [
            index_member_frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "in_date": "20200101",
                        "is_new": "Y",
                    },
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "in_date": "20200101",
                        "is_new": "Y",
                    },
                ]
            )
        ],
        "index_member_all",
    )
    result = make_client(fake).fetch_index_member_all(
        IndexMemberAllRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
    )
    assert len(result.frame) == 2


def test_fetch_index_member_all_rejects_conflicting_membership_identity():
    fake = EndpointFake(
        [
            index_member_frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "in_date": "20200101",
                        "out_date": "20210101",
                        "is_new": "N",
                    },
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "in_date": "20200101",
                        "out_date": math.nan,
                        "is_new": "Y",
                    },
                ]
            )
        ],
        "index_member_all",
    )
    with pytest.raises(SchemaMismatchError, match="conflicting"):
        make_client(fake).fetch_index_member_all(
            IndexMemberAllRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
        )


def test_fetch_index_member_all_rejects_rows_outside_selector_scope():
    fake = EndpointFake(
        [
            index_member_frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "in_date": "20200101",
                        "is_new": "Y",
                    },
                    {
                        "ts_code": "600000.SH",
                        "l1_code": "801010.SI",
                        "in_date": "20200101",
                        "is_new": "Y",
                    },
                ]
            )
        ],
        "index_member_all",
    )
    with pytest.raises(SchemaMismatchError, match="outside request scope"):
        make_client(fake).fetch_index_member_all(
            IndexMemberAllRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
        )


def test_fetch_index_member_all_rejects_malformed_dates():
    fake = EndpointFake(
        [
            index_member_frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "l1_code": "801010.SI",
                        "in_date": "20201301",
                        "is_new": "Y",
                    }
                ]
            )
        ],
        "index_member_all",
    )
    with pytest.raises(SchemaMismatchError, match="malformed"):
        make_client(fake).fetch_index_member_all(
            IndexMemberAllRequest(empty_policy=EmptyPolicy.ERROR, ts_code="000001.SZ")
        )


def test_fetch_index_member_all_rejects_exact_documented_cap():
    rows = [
        {
            "ts_code": f"{i % 100:06d}.SZ",
            "l1_code": "801010.SI",
            "in_date": "20200101",
            "is_new": "Y",
        }
        for i in range(2000)
    ]
    fake = EndpointFake([index_member_frame(rows)], "index_member_all")
    with pytest.raises(PaginationError, match="ambiguous row cap"):
        make_client(fake).fetch_index_member_all(
            IndexMemberAllRequest(empty_policy=EmptyPolicy.ERROR, l1_code="801010.SI")
        )
