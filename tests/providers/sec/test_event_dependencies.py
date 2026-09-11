import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core.errors import ResourceLimitError, SchemaMismatchError
from ohmydata.providers.sec.event_dependencies import (
    SecDataVersionId,
    SecDataVersionKind,
    SecDependencyEdge,
    SecDependencyIndex,
    plan_sec_event_invalidation,
)

TIME = datetime(2025, 1, 1, tzinfo=UTC)


def node(name, kind=SecDataVersionKind.DERIVED_METRIC):
    return SecDataVersionId(kind, hashlib.sha256(name.encode()).hexdigest())


def edge(source, target, *, cik="1", time=TIME):
    return SecDependencyEdge(source, target, cik, "a" * 64, time)


def test_transitive_diamond_and_two_issuer_isolation():
    a, b, c, d, other, result = map(node, ("a", "b", "c", "d", "other", "result"))
    edges = (edge(a, b), edge(a, c), edge(b, d), edge(c, d), edge(other, result, cik="2"))
    index = SecDependencyIndex(edges)
    plan = plan_sec_event_invalidation(index, (a, a), known_at=TIME)
    assert plan.affected_outputs == tuple(sorted((b, c, d)))
    assert plan.changed_inputs == (a,)
    assert plan.traversed_edge_ids == tuple(sorted(value.edge_identity for value in edges[:4]))
    assert plan == plan_sec_event_invalidation(
        SecDependencyIndex(reversed(edges)), (a,), known_at=TIME
    )
    assert index.affected_versions((a,), knowledge_cutoff=TIME) == plan.affected_outputs


def test_future_unknown_and_configuration_inputs():
    source = node("config", SecDataVersionKind.RECIPE_CONFIGURATION)
    result, later = node("result"), node("later")
    index = SecDependencyIndex(
        (edge(source, result), edge(result, later, time=TIME + timedelta(days=1)))
    )
    assert index.affected_versions((source,), knowledge_cutoff=TIME) == (result,)
    assert index.affected_versions((source,), knowledge_cutoff=TIME - timedelta(seconds=1)) == ()
    assert index.affected_versions((node("unknown"),), knowledge_cutoff=TIME) == ()
    same_hash_other_kind = SecDataVersionId(SecDataVersionKind.RAW_FACT, source.identity)
    assert index.affected_versions((same_hash_other_kind,), knowledge_cutoff=TIME) == ()


def test_cycles_and_limits_reject_before_traversal():
    a, b = node("a"), node("b")
    with pytest.raises(SchemaMismatchError, match="cycle"):
        SecDependencyIndex((edge(a, b), edge(b, a)))
    with pytest.raises(ValueError, match="self"):
        edge(a, a)
    assert len(SecDependencyIndex((edge(a, b),), max_edges=1).edges) == 1
    consumed = []

    def many():
        for i in range(100):
            consumed.append(i)
            yield edge(a, b)

    with pytest.raises(ResourceLimitError):
        SecDependencyIndex(many(), max_edges=1)
    assert consumed == [0, 1]
    index = SecDependencyIndex(())
    assert index.affected_versions((a for _ in range(10_000)), knowledge_cutoff=TIME) == ()
    with pytest.raises(ResourceLimitError):
        index.affected_versions((a for _ in range(10_001)), knowledge_cutoff=TIME)


def test_duplicate_edges_are_idempotent_and_cutoff_is_aware():
    a, b = node("a"), node("b")
    assert (
        SecDependencyIndex((edge(a, b), edge(a, b))).index_identity
        == SecDependencyIndex((edge(a, b),)).index_identity
    )
    with pytest.raises(ValueError, match="timezone"):
        SecDependencyIndex(()).affected_versions((), knowledge_cutoff=TIME.replace(tzinfo=None))


def test_index_identity_and_adjacency_are_immutable():
    a, b = node("a"), node("b")
    index = SecDependencyIndex((edge(a, b),))
    before = plan_sec_event_invalidation(index, (a,), known_at=TIME)
    with pytest.raises(FrozenInstanceError):
        index.index_identity = "f" * 64
    with pytest.raises(FrozenInstanceError):
        index.edges = ()
    with pytest.raises(TypeError):
        index._outgoing[a] = ()
    assert type(index._outgoing[a]) is tuple
    assert plan_sec_event_invalidation(index, (a,), known_at=TIME) == before
