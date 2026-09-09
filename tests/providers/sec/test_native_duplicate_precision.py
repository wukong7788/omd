"""Synthetic regression coverage for native SEC duplicate precision handling."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from ohmydata.providers.sec.edgartools_adapter import (
    SecStatementParseError,
    parse_statement_rows,
)

Context = pytest.importorskip("edgar.xbrl.models").Context
Fact = pytest.importorskip("edgar.xbrl.models").Fact
PresentationNode = pytest.importorskip("edgar.xbrl.models").PresentationNode
Statement = pytest.importorskip("edgar.xbrl.statements").Statement
XBRL = pytest.importorskip("edgar.xbrl.xbrl").XBRL


def _statement(facts):
    concept = "us-gaap_Revenue"
    xbrl = XBRL()
    xbrl.parser.facts = facts
    xbrl.parser.contexts = {
        "a": Context(
            context_id="a",
            period={"type": "duration", "startDate": "2024-04-01", "endDate": "2024-06-30"},
        ),
        "b": Context(
            context_id="b",
            period={"type": "duration", "startDate": "2024-04-01", "endDate": "2024-06-30"},
        ),
        "c": Context(
            context_id="c",
            period={"type": "duration", "startDate": "2024-04-01", "endDate": "2024-06-30"},
        ),
    }
    xbrl.parser.context_period_map = {
        "a": "duration_2024-04-01_2024-06-30",
        "b": "duration_2024-04-01_2024-06-30",
        "c": "duration_2024-04-01_2024-06-30",
    }
    xbrl.parser.units = {"u": {"measure": "iso4217:USD"}}
    xbrl.parser.presentation_trees = {
        "role": SimpleNamespace(
            all_nodes={concept: PresentationNode(element_id=concept, standard_label="Revenue")}
        )
    }
    xbrl.find_statement = lambda *_: ([], "role", "IncomeStatement")
    assert not hasattr(xbrl.facts, "values")
    return Statement(xbrl, "IncomeStatement")


def _fact(key, context, value, decimals, *, unit="u"):
    return Fact(
        element_id="us-gaap_Revenue",
        context_ref=context,
        value=value,
        numeric_value=float(Decimal(value)),
        unit_ref=unit,
        decimals=decimals,
    )


def test_same_period_different_decimals_selects_each_context_highest_precision():
    statement = _statement(
        {
            "a-coarse": _fact("a-coarse", "a", "100", 0),
            "a-fine": _fact("a-fine", "a", "100.00", 2),
            "b": _fact("b", "b", "100.0", 1),
        }
    )
    rows = parse_statement_rows(statement, "income_statement")
    assert [(r.context_ref, r.value_native, r.decimals_native) for r in rows] == [
        ("a", "100.00", "2"),
        ("b", "100.0", "1"),
    ]


def test_different_precision_requires_intersecting_closed_intervals():
    statement = _statement(
        {
            "a": _fact("a", "a", "10.0", 1),
            "b": _fact("b", "b", "10.04", 2),
        }
    )
    assert len(parse_statement_rows(statement, "income_statement")) == 2


def test_negative_precision_and_inf_rank_keep_original_fact():
    statement = _statement(
        {
            "a-coarse": _fact("a-coarse", "a", "123400", -3),
            "a-fine": _fact("a-fine", "a", "123450", -2),
            "a-inf": _fact("a-inf", "a", "123450", "INF"),
        }
    )
    rows = parse_statement_rows(statement, "income_statement")
    assert [(r.context_ref, r.value_native, r.decimals_native) for r in rows] == [
        ("a", "123450", "INF")
    ]


def test_duplicate_order_does_not_change_selection_or_global_validation():
    facts = [
        ("a-coarse", "a", "10.0", 1),
        ("a-fine", "a", "10.04", 2),
        ("b", "b", "10.04", 2),
    ]
    first = {key: _fact(key, context, value, decimals) for key, context, value, decimals in facts}
    second = dict(reversed(list(first.items())))
    rows_a = parse_statement_rows(_statement(first), "income_statement")
    rows_b = parse_statement_rows(_statement(second), "income_statement")
    assert [(r.context_ref, r.value_native) for r in rows_a] == [
        (r.context_ref, r.value_native) for r in rows_b
    ]


def test_negative_zero_and_long_decimal_values_remain_native():
    long_value = "-123456789012345678901234567890.12345678901234567890"
    rows = parse_statement_rows(
        _statement({"a": _fact("a", "a", long_value, 20)}),
        "income_statement",
    )
    assert next(row for row in rows if row.context_ref == "a").value == Decimal(long_value)

    rows = parse_statement_rows(
        _statement({"b": _fact("b", "b", "-0", "INF")}),
        "income_statement",
    )
    assert next(row for row in rows if row.context_ref == "b").value == Decimal(0)
    assert next(row for row in rows if row.context_ref == "b").value_native == "-0"


def test_same_precision_unequal_values_fail_even_when_intervals_overlap():
    with pytest.raises(SecStatementParseError, match="conflicting native"):
        parse_statement_rows(
            _statement(
                {
                    "a": _fact("a", "a", "10.0", 1),
                    "b": _fact("b", "b", "10.04", 1),
                }
            ),
            "income_statement",
        )


def test_three_intervals_require_one_global_intersection():
    with pytest.raises(SecStatementParseError, match="conflicting native"):
        parse_statement_rows(
            _statement(
                {
                    "a": _fact("a", "a", "0", -1),
                    "b": _fact("b", "b", "0.45", 2),
                    "c": _fact("c", "c", "0.550", 3),
                }
            ),
            "income_statement",
        )


def test_missing_precision_only_allows_complete_unknown_duplicates():
    statement = _statement({"a": _fact("a", "a", "10", None), "b": _fact("b", "b", "10", None)})
    assert len(parse_statement_rows(statement, "income_statement")) == 2
    with pytest.raises(SecStatementParseError, match="conflicting native"):
        parse_statement_rows(
            _statement({"a": _fact("a", "a", "10", None), "b": _fact("b", "b", "10", 0)}),
            "income_statement",
        )


def test_all_nil_native_facts_remain_empty():
    nil_a = _fact("a", "a", "0", None).model_copy(update={"value": None, "numeric_value": None})
    nil_b = _fact("b", "b", "0", None).model_copy(update={"value": None, "numeric_value": None})
    assert (
        parse_statement_rows(
            _statement({"a": nil_a, "b": nil_b}),
            "income_statement",
        )
        == []
    )
    conflicting_unit = nil_b.model_copy(update={"unit_ref": "eur"})
    with pytest.raises(SecStatementParseError, match="conflicting native"):
        parse_statement_rows(_statement({"a": nil_a, "b": conflicting_unit}), "income_statement")
    invalid_precision = nil_b.model_copy(update={"decimals": 1.5})
    with pytest.raises(SecStatementParseError, match="precision"):
        parse_statement_rows(_statement({"a": nil_a, "b": invalid_precision}), "income_statement")


def test_non_intersecting_precision_group_and_unit_conflict_fail():
    for facts in (
        {"a": _fact("a", "a", "10.0", 1), "b": _fact("b", "b", "10.06", 2)},
        {"a": _fact("a", "a", "10", 0), "b": _fact("b", "b", "10", 0, unit="eur")},
    ):
        with pytest.raises(SecStatementParseError, match="conflicting native"):
            parse_statement_rows(_statement(facts), "income_statement")


def test_same_precision_conflict_and_invalid_precision_fail():
    for decimals in ("bad", "NaN"):
        facts = {"a": _fact("a", "a", "10", decimals), "b": _fact("b", "b", "10", decimals)}
        with pytest.raises(SecStatementParseError, match="precision"):
            parse_statement_rows(_statement(facts), "income_statement")


def test_equal_precision_tie_uses_lexical_native_value_order():
    first = {
        "a-low": _fact("a-low", "a", "10.0", 2),
        "a-long": _fact("a-long", "a", "10.00", 2),
    }
    second = dict(reversed(list(first.items())))
    rows_a = parse_statement_rows(_statement(first), "income_statement")
    rows_b = parse_statement_rows(_statement(second), "income_statement")
    assert rows_a[0].value_native == rows_b[0].value_native == "10.0"


def test_signed_and_zero_padded_precision_strings_are_valid_and_deterministic():
    first = {
        "a-plus": _fact("a-plus", "a", "10", "+02"),
        "a-padded": _fact("a-padded", "a", "10", "02"),
    }
    second = dict(reversed(list(first.items())))
    rows_a = parse_statement_rows(_statement(first), "income_statement")
    rows_b = parse_statement_rows(_statement(second), "income_statement")
    assert rows_a[0].decimals == rows_b[0].decimals == 2
    assert rows_a[0].decimals_native == rows_b[0].decimals_native == "+02"


def test_model_coercion_cannot_hide_malformed_precision_type():
    malformed = _fact("a", "a", "10", 0).model_copy(update={"decimals": 1.5})
    with pytest.raises(SecStatementParseError, match="precision"):
        parse_statement_rows(_statement({"a": malformed}), "income_statement")


def test_native_precision_arithmetic_bounds_fail_before_fraction_materialization():
    with pytest.raises(SecStatementParseError, match="arithmetic bounds"):
        parse_statement_rows(
            _statement({"a": _fact("a", "a", "1e1000000000", 0)}),
            "income_statement",
        )
    oversized_precision = _fact("a", "a", "1", 0).model_copy(update={"decimals": 1_000_000_000})
    with pytest.raises(SecStatementParseError, match="arithmetic bounds"):
        parse_statement_rows(_statement({"a": oversized_precision}), "income_statement")
