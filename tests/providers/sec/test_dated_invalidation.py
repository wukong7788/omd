import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core.errors import (
    CoverageError,
    IdentityConflictError,
    ResourceLimitError,
    SchemaMismatchError,
)
from ohmydata.providers.sec.dated_invalidation import (
    SecDatedInputChange,
    SecDatedInvalidationIndex,
    SecDatedInvalidationTarget,
    plan_sec_dated_invalidation,
)
from ohmydata.providers.sec.event_dependencies import (
    SecDataVersionId,
    SecDataVersionKind,
    SecDependencyEdge,
)
from ohmydata.providers.sec.pit import SecPitMode

TIME = datetime(2025, 1, 1, 12, tzinfo=UTC)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def version(
    name: str, kind: SecDataVersionKind = SecDataVersionKind.DERIVED_METRIC
) -> SecDataVersionId:
    return SecDataVersionId(kind, digest(name))


def edge(
    source: SecDataVersionId,
    output: SecDataVersionId,
    *,
    cik: str = "1",
    recipe: str = "r",
    recorded_at: datetime = TIME,
) -> SecDependencyEdge:
    return SecDependencyEdge(source, output, cik, digest(recipe), recorded_at)


def target(
    output: SecDataVersionId,
    *,
    cik: str = "1",
    instrument: str = "class-a",
    recipe: str = "r",
    valuation_at: datetime = TIME,
    cutoff: datetime = TIME,
    mode: SecPitMode = SecPitMode.MARKET_KNOWN,
    recorded_at: datetime = TIME,
) -> SecDatedInvalidationTarget:
    return SecDatedInvalidationTarget(
        output,
        cik,
        instrument,
        digest(f"binding-{instrument}"),
        digest(recipe),
        valuation_at,
        cutoff,
        mode,
        recorded_at,
    )


def change(
    old: SecDataVersionId,
    *,
    cik: str = "1",
    start: datetime = TIME - timedelta(days=1),
    end: datetime = TIME + timedelta(days=1),
    market: datetime | None = TIME,
    system: datetime | None = TIME,
    recorded_at: datetime = TIME,
    new: SecDataVersionId | None = None,
) -> SecDatedInputChange:
    return SecDatedInputChange(
        old, new, cik, start, end, market, system, "caller-evidence", recorded_at
    )


def test_exact_targets_isolate_classes_and_issuers_and_allow_same_output():
    old, output = version("old", SecDataVersionKind.RAW_FACT), version("output")
    other_old, other_output = (
        version("other-old", SecDataVersionKind.RAW_FACT),
        version("other-output"),
    )
    index = SecDatedInvalidationIndex(
        (edge(old, output), edge(other_old, other_output, cik="2")),
        (
            target(output, instrument="class-a"),
            target(output, instrument="class-b"),
            target(other_output, cik="2", instrument="issuer-two"),
        ),
    )
    plan = plan_sec_dated_invalidation(index, (change(old),), known_at=TIME)
    assert tuple(item.target_identity for item in plan.targets) == tuple(
        sorted(item.target_identity for item in plan.targets)
    )
    assert {item.instrument_id for item in plan.targets} == {"class-a", "class-b"}
    assert all(proof.change_identity == plan.changes[0].change_identity for proof in plan.proofs)


def test_half_open_valuation_window_is_distinct_from_cutoff_availability():
    old, output = version("old", SecDataVersionKind.RAW_FACT), version("output")
    index = SecDatedInvalidationIndex(
        (edge(old, output),),
        (
            target(output, valuation_at=TIME),
            target(output, instrument="at-end", valuation_at=TIME + timedelta(days=1)),
            target(output, instrument="late-available", cutoff=TIME - timedelta(minutes=1)),
        ),
    )
    plan = plan_sec_dated_invalidation(
        index,
        (change(old, start=TIME, end=TIME + timedelta(days=1), market=TIME),),
        known_at=TIME,
    )
    assert [item.instrument_id for item in plan.targets] == ["class-a"]


def test_recording_and_availability_cutoffs_and_system_mode():
    old, output = version("old", SecDataVersionKind.RAW_FACT), version("output")
    index = SecDatedInvalidationIndex(
        (edge(old, output),),
        (
            target(output, recorded_at=TIME + timedelta(seconds=1)),
            target(output, instrument="system", mode=SecPitMode.SYSTEM_REPLAY),
        ),
    )
    plan = plan_sec_dated_invalidation(
        index, (change(old, market=None, system=TIME),), known_at=TIME
    )
    assert [item.instrument_id for item in plan.targets] == ["system"]
    with pytest.raises(CoverageError, match="market_available_at"):
        plan_sec_dated_invalidation(
            SecDatedInvalidationIndex((edge(old, output),), (target(output),)),
            (change(old, market=None, system=TIME),),
            known_at=TIME,
        )
    assert not plan_sec_dated_invalidation(
        SecDatedInvalidationIndex((edge(old, output),), (target(output),)),
        (change(old, market=TIME + timedelta(seconds=1)),),
        known_at=TIME,
    ).targets


def test_old_seed_only_and_future_edge_do_not_invalidate():
    old, new, output = (
        version("old", SecDataVersionKind.RAW_FACT),
        version("new", SecDataVersionKind.RAW_FACT),
        version("output"),
    )
    index = SecDatedInvalidationIndex((edge(old, output),), (target(output),))
    assert plan_sec_dated_invalidation(index, (change(old, new=new),), known_at=TIME).targets == (
        target(output),
    )
    assert not plan_sec_dated_invalidation(index, (change(new),), known_at=TIME).targets
    future = SecDatedInvalidationIndex(
        (edge(old, output, recorded_at=TIME + timedelta(seconds=1)),), (target(output),)
    )
    assert not plan_sec_dated_invalidation(future, (change(old),), known_at=TIME).targets


def test_deterministic_single_shortest_proof_not_all_paths():
    raw = version("raw", SecDataVersionKind.RAW_FACT)
    left, right, output = version("left"), version("right"), version("out")
    edges = (
        edge(raw, right, recipe="mid-r"),
        edge(raw, left, recipe="mid-l"),
        edge(left, output),
        edge(right, output),
    )
    index = SecDatedInvalidationIndex(edges, (target(output),))
    first = plan_sec_dated_invalidation(index, (change(raw),), known_at=TIME)
    second = plan_sec_dated_invalidation(
        SecDatedInvalidationIndex(reversed(edges), (target(output),)), (change(raw),), known_at=TIME
    )
    assert first == second
    assert len(first.proofs) == 1
    assert len(first.proofs[0].edge_ids) == 2


def test_conflicts_cycles_types_limits_and_seal_tampering_fail_closed():
    raw = version("raw", SecDataVersionKind.RAW_FACT)
    a, b = version("a"), version("b")
    with pytest.raises(IdentityConflictError, match="conflicting"):
        SecDatedInvalidationIndex((edge(raw, a, recipe="one"), edge(b, a, recipe="two")), ())
    with pytest.raises(SchemaMismatchError, match="cycle"):
        SecDatedInvalidationIndex((edge(a, b), edge(b, a)), ())
    with pytest.raises(TypeError):
        SecDatedInvalidationIndex((object(),), ())  # type: ignore[arg-type]
    seen: list[int] = []

    def many():
        for i in range(3):
            seen.append(i)
            yield edge(raw, a)

    with pytest.raises(ResourceLimitError):
        SecDatedInvalidationIndex(many(), (), max_edges=1)
    assert seen == [0, 1]
    with pytest.raises(ValueError, match="UTF-8"):
        target(a, instrument="x" * 1025)
    index = SecDatedInvalidationIndex((edge(raw, a),), (target(a),))
    with pytest.raises(ResourceLimitError):
        plan_sec_dated_invalidation(index, (change(raw),), known_at=TIME, max_total_visits=1)
    with pytest.raises(ResourceLimitError):
        plan_sec_dated_invalidation(
            index, (change(raw) for _ in range(2)), known_at=TIME, max_changes=1
        )
    public_edge = index.edges[0]
    object.__setattr__(public_edge, "canonical_cik", "2")
    assert plan_sec_dated_invalidation(index, (change(raw),), known_at=TIME).targets == (target(a),)
    public_target = plan_sec_dated_invalidation(index, (change(raw),), known_at=TIME).targets[0]
    object.__setattr__(public_target, "instrument_id", "forged")
    assert plan_sec_dated_invalidation(index, (change(raw),), known_at=TIME).targets == (target(a),)


def test_cached_identities_and_dense_candidates_are_bounded():
    raw, output = version("raw", SecDataVersionKind.RAW_FACT), version("output")
    declared_target = target(output)
    object.__setattr__(declared_target, "target_identity", digest("forged"))
    with pytest.raises(SchemaMismatchError, match="target identity"):
        SecDatedInvalidationIndex((edge(raw, output),), (declared_target,))
    valid = SecDatedInvalidationIndex((edge(raw, output),), (target(output),))
    declared_change = change(raw)
    object.__setattr__(declared_change, "change_identity", digest("forged"))
    with pytest.raises(SchemaMismatchError, match="change identity"):
        plan_sec_dated_invalidation(valid, (declared_change,), known_at=TIME)
    outputs = tuple(version(f"fanout-{i}") for i in range(8))
    fanout = SecDatedInvalidationIndex(
        tuple(edge(raw, item) for item in outputs),
        tuple(target(item, instrument=f"class-{i}") for i, item in enumerate(outputs)),
    )
    with pytest.raises(ResourceLimitError, match="traversal"):
        plan_sec_dated_invalidation(fanout, (change(raw),), known_at=TIME, max_total_visits=10)


def test_candidate_bucket_and_proof_steps_are_budgeted_without_issuer_scan():
    raw, middle, output = (
        version("raw", SecDataVersionKind.RAW_FACT),
        version("middle"),
        version("output"),
    )
    other_raw = version("other-raw", SecDataVersionKind.RAW_FACT)
    other_outputs = tuple(version(f"other-{i}") for i in range(12))
    index = SecDatedInvalidationIndex(
        (edge(raw, middle), edge(middle, output))
        + tuple(edge(other_raw, item, cik="2") for item in other_outputs),
        (target(output),)
        + tuple(
            target(item, cik="2", instrument=f"other-{i}") for i, item in enumerate(other_outputs)
        ),
    )
    assert plan_sec_dated_invalidation(
        index, (change(raw),), known_at=TIME, max_total_visits=8
    ).targets == (target(output),)
    with pytest.raises(ResourceLimitError, match="traversal"):
        plan_sec_dated_invalidation(index, (change(raw),), known_at=TIME, max_total_visits=7)
    with pytest.raises(ResourceLimitError, match="serialization"):
        plan_sec_dated_invalidation(index, (), known_at=TIME, max_serialized_bytes=1)
