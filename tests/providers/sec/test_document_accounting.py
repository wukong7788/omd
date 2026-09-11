import socket
import sys
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from ohmydata.core.errors import ResourceLimitError
from ohmydata.providers.sec._metric_common import encoded
from ohmydata.providers.sec.document_accounting import (
    SecAccountingApplicability as Applicability,
)
from ohmydata.providers.sec.document_accounting import SecAccountingRule as Rule
from ohmydata.providers.sec.document_accounting import SecAccountingStatus as Status
from ohmydata.providers.sec.document_accounting import SecAccountingTerm as Term
from ohmydata.providers.sec.document_accounting import SecAccountingTolerance as Tolerance
from ohmydata.providers.sec.document_accounting import evaluate_sec_document_accounting as evaluate
from ohmydata.providers.sec.observed_accounting import evaluate_sec_observed_accounting

sys.path.insert(0, str(Path(__file__).parent))
from test_document_financials import build
from test_observed_xbrl_financials import _produce


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _second_context(payloads, *, unit="usd", decimals="0"):
    payloads["instance"] = (
        payloads["instance"]
        .replace(
            b'<unit id="usd">',
            b'<context id="c2"><entity><identifier scheme="http://www.sec.gov/CIK">'
            b"0000000001</identifier></entity><period><startDate>2024-01-01</startDate>"
            b"<endDate>2024-03-31</endDate></period></context>"
            + (b'<unit id="shares"><measure>shares</measure></unit>' if unit == "shares" else b"")
            + b'<unit id="usd">',
        )
        .replace(
            b"</xbrl>",
            b'<us-gaap:Revenues contextRef="c2" unitRef="'
            + unit.encode()
            + b'" decimals="'
            + decimals.encode()
            + b'">123</us-gaap:Revenues></xbrl>',
        )
    )


def _income_statement_pair(payloads, *, gross_profit):
    payloads["schema"] = payloads["schema"].replace(
        b'<xs:element name="Revenues" id="Revenues" type="xs:decimal" xbrli:periodType="duration"/>',
        b'<xs:element name="Revenues" id="Revenues" type="xs:decimal" xbrli:periodType="duration"/>'
        b'<xs:element name="GrossProfit" id="GrossProfit" type="xs:decimal" '
        b'xbrli:periodType="duration"/>',
    )
    payloads["presentation"] = (
        payloads["presentation"]
        .replace(
            b'<link:loc xlink:label="revenue" xlink:href="fake.xsd#us-gaap_Revenues"/>',
            b'<link:loc xlink:label="revenue" xlink:href="fake.xsd#us-gaap_Revenues"/>'
            b'<link:loc xlink:label="gross" xlink:href="fake.xsd#us-gaap_GrossProfit"/>',
        )
        .replace(
            b'<link:presentationArc xlink:from="root" xlink:to="revenue" order="1"/>',
            b'<link:presentationArc xlink:from="root" xlink:to="revenue" order="1"/>'
            b'<link:presentationArc xlink:from="root" xlink:to="gross" order="2"/>',
        )
    )
    payloads["labels"] = payloads["labels"].replace(
        b"</link:labelLink>",
        b'<link:loc xlink:label="gross" xlink:href="fake.xsd#us-gaap_GrossProfit"/>'
        b'<link:label xlink:label="gross-label" xlink:role="http://www.xbrl.org/2003/role/label">'
        b'Gross Profit</link:label><link:labelArc xlink:from="gross" xlink:to="gross-label"/>'
        b"</link:labelLink>",
    )
    payloads["instance"] = payloads["instance"].replace(
        b'<us-gaap:Revenues contextRef="c1" unitRef="usd" decimals="0">123</us-gaap:Revenues>',
        b'<us-gaap:Revenues contextRef="c1" unitRef="usd" decimals="0">123</us-gaap:Revenues>'
        b'<us-gaap:GrossProfit contextRef="c1" unitRef="usd" decimals="0">'
        + gross_profit.encode()
        + b"</us-gaap:GrossProfit>",
    )


def _rule(*, c2=False, tolerance=Tolerance.EXACT):
    return Rule(
        "synthetic:document-equation",
        Applicability.SAME_CONTEXT,
        (
            Term("income_statement", "us-gaap_Revenues", "c1", 1),
            Term("income_statement", "us-gaap_Revenues", "c2" if c2 else "missing", -1),
        ),
        "iso4217:USD",
        tolerance,
        "evidence:document-fixture",
    )


def _pair_rule():
    return Rule(
        "synthetic:document-income-pair",
        Applicability.SAME_CONTEXT,
        (
            Term("income_statement", "us-gaap_Revenues", "c1", 1),
            Term("income_statement", "us-gaap_GrossProfit", "c1", -1),
        ),
        "iso4217:USD",
        Tolerance.EXACT,
        "evidence:document-fixture",
    )


def _run(production, rules=None, **kwargs):
    return evaluate(
        production,
        [_rule()] if rules is None else rules,
        detected_at=production.produced_at,
        recorded_at=production.produced_at,
        **kwargs,
    )


def test_document_production_binds_output_and_preserves_old_report_algorithm(tmp_path):
    document, *_ = build(tmp_path / "document")
    old, *_ = _produce(tmp_path / "old")
    rules = (_rule(),)
    document_report = _run(document, rules)
    old_report = evaluate_sec_observed_accounting(
        old, rules, detected_at=old.produced_at, recorded_at=old.produced_at
    )
    assert document_report.checks == old_report.checks
    assert document_report.production_identity == document.production_identity
    assert (
        document_report.output_observation_identity
        == document.output_observation.observation_identity
    )
    assert document_report.output_fact_version == document.output_observation.fact_version
    assert replace(old_report).report_identity == old_report.report_identity
    assert (
        old_report.report_identity
        == "cfae8b298cffae2032a2e4b902277a5ef9d8fa54c3019fe81dea8a27726d82c5"
    )
    assert sha256(encoded(old_report)).hexdigest() == (
        "4f1fa88f49bc0948d14296d4300ca60fe71c9ae7b9d90d602084635ff8342766"
    )
    assert document_report.report_identity != old_report.report_identity


def test_actual_document_rows_preserve_missing_context_unit_and_precision_diagnostics(tmp_path):
    matched, *_ = build(
        tmp_path / "matched",
        change=lambda payloads: _income_statement_pair(payloads, gross_profit="123"),
    )
    assert _run(matched, [_pair_rule()]).checks[0].status is Status.MATCH
    mismatched, *_ = build(
        tmp_path / "mismatched",
        change=lambda payloads: _income_statement_pair(payloads, gross_profit="122"),
    )
    mismatch = _run(mismatched, [_pair_rule()]).checks[0]
    assert mismatch.status is Status.MISMATCH and mismatch.residual == 1

    missing, *_ = build(tmp_path / "missing")
    assert _run(missing).checks[0].status is Status.MISSING

    context, *_ = build(tmp_path / "context", change=_second_context)
    assert _run(context, [_rule(c2=True)]).checks[0].status is Status.INCOMPARABLE
    assert _run(context, [_rule(c2=True)]).checks[0].incomparable_reasons == (
        (-1, "CONTEXT_OR_PERIOD_MISMATCH"),
    )

    def wrong_unit(payloads):
        payloads["instance"] = payloads["instance"].replace(
            b'<unit id="usd"><measure>iso4217:USD</measure></unit>',
            b'<unit id="usd"><measure>shares</measure></unit>',
        )

    unit, *_ = build(tmp_path / "unit", change=wrong_unit)
    unit_result = _run(unit).checks[0]
    assert unit_result.status is Status.MISSING
    assert (0, "UNIT_MISMATCH_OR_MISSING") in unit_result.incomparable_reasons

    def bad_precision(payloads):
        payloads["instance"] = payloads["instance"].replace(b'decimals="0"', b'decimals="9999"')

    precision, *_ = build(tmp_path / "precision", change=bad_precision)
    precision_result = _run(
        precision, [_rule(tolerance=Tolerance.ASSUME_NEAREST_REPORTED_DECIMALS)]
    ).checks[0]
    assert precision_result.status is Status.MISSING
    assert (0, "PRECISION_MISSING_OR_INVALID") in precision_result.incomparable_reasons


def test_document_admission_rejects_old_type_future_and_tampering_without_repair(tmp_path):
    document, *_ = build(tmp_path / "document")
    old, *_ = _produce(tmp_path / "old")
    with pytest.raises(TypeError, match="document financial production"):
        _run(old)
    with pytest.raises(ValueError, match="detection precedes"):
        evaluate(
            document,
            [_rule()],
            detected_at=document.produced_at - timedelta(seconds=1),
            recorded_at=document.produced_at,
        )

    original = document.production_identity
    object.__setattr__(document, "production_identity", "f" * 64)
    with pytest.raises(ValueError, match="production identity"):
        _run(document)
    assert document.production_identity == "f" * 64
    object.__setattr__(document, "production_identity", original)

    original_vintage = document.vintage.vintage_identity
    object.__setattr__(document.vintage, "vintage_identity", "f" * 64)
    with pytest.raises(ValueError, match="nested identity"):
        _run(document)
    assert document.vintage.vintage_identity == "f" * 64
    object.__setattr__(document.vintage, "vintage_identity", original_vintage)


def test_document_row_budget_precedes_seal_repair(tmp_path):
    document, *_ = build(tmp_path)
    original = document.vintage.vintage_identity
    object.__setattr__(document.vintage, "rows", document.vintage.rows * 10001)
    with pytest.raises(ResourceLimitError, match="row budget"):
        _run(document)
    assert document.vintage.vintage_identity == original
