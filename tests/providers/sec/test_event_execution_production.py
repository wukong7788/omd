"""Real installed parser inside an offline synthetic event execution cycle."""

import json
import socket
import time
from datetime import timedelta
from decimal import Decimal

import pytest

from ohmydata.core import RequestSpec
from ohmydata.providers.sec import (
    SecExecutionStatus,
    SecExecutionValidation,
    produce_sec_financials_from_observed_xbrl_package,
    retain_sec_execution_outputs,
    retain_sec_execution_validation,
)
from tests.providers.sec.test_event_execution import fixture, run
from tests.providers.sec.test_observed_xbrl_financials import _request, _setup
from tests.providers.sec.test_sgml_financials import _raw


def production_probe(root):
    parts = fixture(root)
    handler, validator = parts[3:5]
    productions = []
    counts = {"production_calls": 0, "validation_calls": 0}

    def execute(operation):
        counts["production_calls"] += 1
        source, sr, packages, pr, _ = _setup(
            root / "production",
            source_time=handler.clock() - timedelta(hours=1),
            package_time=handler.clock() - timedelta(minutes=30),
            raw=_raw(acceptance="20240501080000"),
        )
        produced = produce_sec_financials_from_observed_xbrl_package(
            source_store=source,
            source_observation=sr,
            package_store=packages,
            package_observation=pr,
            output_store=handler.store,
            request=_request(),
            produced_at=handler.clock(),
        )
        assert produced.vintage.accepted_at == parts[1].events[0].acceptance_at
        productions.append(produced)
        ref = retain_sec_execution_outputs(
            store=handler.store,
            operation=operation,
            outputs=(produced.output_observation,),
            recorded_at=handler.clock(),
        )
        handler.receipts[operation.key] = ref
        return ref

    def validate(operation, acquisition, outputs):
        counts["validation_calls"] += 1
        assert len(outputs) == 1
        data = json.loads(validator.store.replay_observation(outputs[0]).payload)
        rows = data["rows"]
        # This fixture's single cell is a deliberately narrow, synthetic validation policy.
        assert len(rows) == 1 and rows[0]["value"] == {"decimal": "123"}
        assert rows[0]["period_start"] == {"date": "2024-01-01"}
        assert rows[0]["period_end"] == {"date": "2024-03-31"}
        evidence = validator.store.observe(
            RequestSpec("sec", "synthetic-fixture-validation", {"operation": operation.key}),
            b'{"policy":"synthetic-one-cell-only","matched":true}',
            validator.clock(),
            "synthetic-policy-evidence-v1",
        )
        ref = retain_sec_execution_validation(
            store=validator.store,
            operation=operation,
            acquisition=acquisition,
            validation=SecExecutionValidation(True, (evidence,)),
            recorded_at=validator.clock(),
            canonical_cik="1",
        )
        validator.receipts[operation.key] = ref
        return ref

    handler.execute, validator.validate = execute, validate
    started = time.perf_counter()
    cold = run(parts)
    cold_seconds = time.perf_counter() - started
    assert cold.status is SecExecutionStatus.READY
    assert productions[0].vintage.rows[0].value == Decimal(123)

    def forbidden(*args):
        raise AssertionError("warm replay must not invoke callbacks")

    handler.execute = handler.recover = validator.validate = validator.recover = forbidden
    started = time.perf_counter()
    warm = run(parts)
    warm_seconds = time.perf_counter() - started
    assert warm.state == cold.state and warm.output_receipt == cold.output_receipt
    assert warm.validation_receipt == cold.validation_receipt
    assert counts == {"production_calls": 1, "validation_calls": 1}
    return {
        "scope": "synthetic single-cell policy; no real financial PASS or consumer publication",
        "counts": counts,
        "cold_status": cold.status.value,
        "warm_status": warm.status.value,
        "output_sha256": productions[0].output_observation.response_sha256,
        "production_identity": productions[0].production_identity,
        "execution_output_receipt": cold.output_receipt.observation_identity,
        "execution_validation_receipt": cold.validation_receipt.observation_identity,
        "state_identity": cold.state.state_identity,
        "known_by_at": productions[0].vintage.known_by_at.isoformat(),
        "warm_callbacks_forbidden": True,
        "cold_seconds": cold_seconds,
        "warm_seconds": warm_seconds,
    }


def test_installed_parser_production_to_retained_ready_and_warm(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))
    report = production_probe(tmp_path)
    assert report["warm_status"] == "READY"
