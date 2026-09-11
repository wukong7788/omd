from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.providers.sec import (
    SecMissingDataReason,
    SecQualityEvidenceRef,
    SecQualityFinding,
    SecQualityFindingStatus,
    SecQualityIssueClass,
    select_sec_quality_findings,
)

VERSION = "1" * 64
OBSERVATION = "2" * 64
FACT = "3" * 64
OTHER_OBSERVATION = "4" * 64
OTHER_FACT = "5" * 64
T0 = datetime(2024, 1, 1, 10, tzinfo=UTC)


def _finding(**changes: object) -> SecQualityFinding:
    values: dict[str, object] = {
        "normalized_version_id": VERSION,
        "affected_fields": ("value_native", "concept"),
        "finding_key": "revenue-source",
        "rule_id": "sec-rule",
        "rule_version": "v1",
        "adapter_version": "adapter-v1",
        "issue_class": SecQualityIssueClass.UNKNOWN,
        "missing_reason": None,
        "reason": "Caller observed a discrepancy.",
        "evidence": (SecQualityEvidenceRef(OBSERVATION, FACT),),
        "status": SecQualityFindingStatus.OPEN,
        "detected_at": T0,
        "recorded_at": T0,
    }
    values.update(changes)
    return SecQualityFinding(**values)  # type: ignore[arg-type]


def test_every_issue_and_missing_reason_is_explicit_and_identity_stable() -> None:
    issue_ids = {
        _finding(issue_class=issue, finding_key=f"issue-{issue.value}").finding_id
        for issue in SecQualityIssueClass
    }
    missing_ids = {
        _finding(missing_reason=reason, finding_key=f"missing-{reason.value}").finding_id
        for reason in SecMissingDataReason
    }
    assert len(issue_ids) == len(SecQualityIssueClass)
    assert len(missing_ids) == len(SecMissingDataReason)
    equivalent = _finding(
        affected_fields=("concept", "value_native"),
        evidence=(
            SecQualityEvidenceRef(OTHER_OBSERVATION, OTHER_FACT),
            SecQualityEvidenceRef(OBSERVATION, FACT),
        ),
    )
    reordered = _finding(
        evidence=(
            SecQualityEvidenceRef(OBSERVATION, FACT),
            SecQualityEvidenceRef(OTHER_OBSERVATION, OTHER_FACT),
        ),
    )
    assert equivalent.finding_id == reordered.finding_id
    assert equivalent.affected_fields == ("concept", "value_native")


def test_bounded_fields_reason_and_evidence_references_are_validated() -> None:
    with pytest.raises(ValueError, match="affected_fields"):
        _finding(affected_fields=())
    with pytest.raises(ValueError, match="reason"):
        _finding(reason="x" * 1001)
    with pytest.raises(ValueError, match="SecStatementRow"):
        _finding(affected_fields=("not_a_row_field",))
    with pytest.raises(ValueError, match="affected_fields"):
        _finding(affected_fields=("concept",) * 65)
    with pytest.raises(ValueError, match="evidence"):
        _finding(evidence=())
    with pytest.raises(ValueError, match="evidence"):
        _finding(
            evidence=tuple(SecQualityEvidenceRef(f"{index:064x}", FACT) for index in range(65))
        )
    with pytest.raises(ValueError, match="observation_identity"):
        SecQualityEvidenceRef("not-an-identity", FACT)
    with pytest.raises(ValueError, match="fact_version"):
        SecQualityEvidenceRef(OBSERVATION, "not-an-identity")
    with pytest.raises(TypeError, match="evidence"):
        _finding(evidence=(object(),))


def test_time_and_status_requirements_reject_naive_invalid_and_missing_retraction() -> None:
    for status in SecQualityFindingStatus:
        if status is SecQualityFindingStatus.OPEN:
            assert _finding(status=status).adjudicated_at is None
        else:
            kwargs: dict[str, object] = {"status": status, "adjudicated_at": T0}
            if status is SecQualityFindingStatus.RETRACTED:
                kwargs["supersedes_finding_id"] = "a" * 64
            assert _finding(**kwargs).status is status
    with pytest.raises(TypeError, match="timezone-aware"):
        _finding(detected_at=datetime(2024, 1, 1, 10))  # noqa: DTZ001
    with pytest.raises(ValueError, match="detected_at"):
        _finding(detected_at=T0 + timedelta(seconds=1))
    with pytest.raises(ValueError, match="OPEN"):
        _finding(adjudicated_at=T0)
    with pytest.raises(ValueError, match="non-OPEN"):
        _finding(status=SecQualityFindingStatus.CONFIRMED)
    with pytest.raises(ValueError, match="detected_at <="):
        _finding(
            status=SecQualityFindingStatus.CONFIRMED,
            adjudicated_at=T0 + timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="RETRACTED"):
        _finding(status=SecQualityFindingStatus.RETRACTED, adjudicated_at=T0)


def test_scope_binds_exact_normalized_version_rule_adapter_fields_and_key() -> None:
    initial = _finding()
    revised = _finding(
        status=SecQualityFindingStatus.CONFIRMED,
        adjudicated_at=T0 + timedelta(minutes=1),
        recorded_at=T0 + timedelta(minutes=1),
        supersedes_finding_id=initial.finding_id,
    )
    assert select_sec_quality_findings(
        [initial, revised],
        normalized_version_id=VERSION,
        rule_version="v1",
        cutoff=T0 + timedelta(hours=1),
    ) == (revised,)
    changed = replace(
        revised,
        adapter_version="adapter-v2",
        supersedes_finding_id=initial.finding_id,
    )
    with pytest.raises(ValueError, match="scope"):
        select_sec_quality_findings(
            [initial, changed],
            normalized_version_id=VERSION,
            rule_version="v1",
            cutoff=T0 + timedelta(hours=1),
        )
    changed_fields = replace(
        revised,
        affected_fields=("concept",),
        supersedes_finding_id=initial.finding_id,
    )
    with pytest.raises(ValueError, match="scope"):
        select_sec_quality_findings(
            [initial, changed_fields],
            normalized_version_id=VERSION,
            rule_version="v1",
            cutoff=T0 + timedelta(hours=1),
        )
    other_version = _finding(normalized_version_id="6" * 64, finding_key="other-version")
    other_rule = _finding(rule_version="v2", finding_key="other-rule")
    assert select_sec_quality_findings(
        [initial, other_version, other_rule],
        normalized_version_id=VERSION,
        rule_version="v1",
        cutoff=T0,
    ) == (initial,)


def test_history_returns_revisions_retractions_and_is_order_independent() -> None:
    initial = _finding()
    confirmed = _finding(
        status=SecQualityFindingStatus.CONFIRMED,
        adjudicated_at=T0 + timedelta(minutes=1),
        recorded_at=T0 + timedelta(minutes=1),
        supersedes_finding_id=initial.finding_id,
        reason="Caller confirmed the assertion.",
    )
    retracted = _finding(
        status=SecQualityFindingStatus.RETRACTED,
        adjudicated_at=T0 + timedelta(minutes=2),
        recorded_at=T0 + timedelta(minutes=2),
        supersedes_finding_id=confirmed.finding_id,
        reason="Caller retracted the assertion.",
    )
    before = select_sec_quality_findings(
        [confirmed, initial, initial, retracted],
        normalized_version_id=VERSION,
        rule_version="v1",
        cutoff=T0 + timedelta(minutes=1),
    )
    after = select_sec_quality_findings(
        [retracted, initial, confirmed],
        normalized_version_id=VERSION,
        rule_version="v1",
        cutoff=T0 + timedelta(hours=1),
    )
    assert before == (confirmed,)
    assert after == (retracted,)


def test_history_rejects_missing_predecessor_independent_roots_forks_and_same_time_revision() -> (
    None
):
    orphan = _finding(
        status=SecQualityFindingStatus.RETRACTED,
        adjudicated_at=T0,
        supersedes_finding_id="a" * 64,
    )
    with pytest.raises(ValueError, match="missing"):
        select_sec_quality_findings(
            [orphan], normalized_version_id=VERSION, rule_version="v1", cutoff=T0
        )
    first, second = _finding(), _finding(reason="independent root")
    with pytest.raises(ValueError, match="independent roots"):
        select_sec_quality_findings(
            [first, second], normalized_version_id=VERSION, rule_version="v1", cutoff=T0
        )
    left = _finding(
        reason="left",
        status=SecQualityFindingStatus.CONFIRMED,
        adjudicated_at=T0 + timedelta(minutes=1),
        recorded_at=T0 + timedelta(minutes=1),
        supersedes_finding_id=first.finding_id,
    )
    right = _finding(
        reason="right",
        status=SecQualityFindingStatus.DISMISSED,
        adjudicated_at=T0 + timedelta(minutes=2),
        recorded_at=T0 + timedelta(minutes=2),
        supersedes_finding_id=first.finding_id,
    )
    with pytest.raises(ValueError, match="fork"):
        select_sec_quality_findings(
            [first, left, right],
            normalized_version_id=VERSION,
            rule_version="v1",
            cutoff=T0 + timedelta(hours=1),
        )
    same_time = _finding(
        reason="same time",
        status=SecQualityFindingStatus.CONFIRMED,
        adjudicated_at=T0,
        supersedes_finding_id=first.finding_id,
    )
    with pytest.raises(ValueError, match="increasing"):
        select_sec_quality_findings(
            [first, same_time], normalized_version_id=VERSION, rule_version="v1", cutoff=T0
        )


def test_future_orphan_is_isolated_before_visible_chain_validation() -> None:
    initial = _finding()
    future_orphan = _finding(
        finding_key="future",
        status=SecQualityFindingStatus.RETRACTED,
        adjudicated_at=T0 + timedelta(days=1),
        recorded_at=T0 + timedelta(days=1),
        supersedes_finding_id="a" * 64,
    )
    assert select_sec_quality_findings(
        [future_orphan, initial], normalized_version_id=VERSION, rule_version="v1", cutoff=T0
    ) == (initial,)


def test_bounded_scan_consumes_at_most_max_plus_one_and_requires_exact_types() -> None:
    consumed = 0

    def endless():
        nonlocal consumed
        while True:
            consumed += 1
            yield _finding(finding_key=f"key-{consumed}")

    with pytest.raises(ValueError, match="exceeds"):
        select_sec_quality_findings(
            endless(), normalized_version_id=VERSION, rule_version="v1", cutoff=T0, max_records=3
        )
    assert consumed == 4
    with pytest.raises(TypeError, match="SecQualityFinding"):
        select_sec_quality_findings(
            [object()], normalized_version_id=VERSION, rule_version="v1", cutoff=T0
        )
    with pytest.raises(ValueError, match="max_records"):
        select_sec_quality_findings(
            [], normalized_version_id=VERSION, rule_version="v1", cutoff=T0, max_records=10_001
        )
