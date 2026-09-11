"""Bounded, caller-declared invalidation of exact dated derived-metric targets."""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from types import MappingProxyType

from ...core.errors import IdentityConflictError
from ._dated_invalidation_models import (
    SecDatedInputChange,
    SecDatedInvalidationProof,
    SecDatedInvalidationTarget,
    _copy_change,
    _copy_edge,
    _copy_target,
)
from ._event_discovery_models import _canonical, _stamp, _utc
from .errors import CoverageError, ResourceLimitError, SchemaMismatchError
from .event_dependencies import SecDataVersionId, SecDependencyEdge
from .pit import SecPitMode


@dataclass(frozen=True, init=False)
class SecDatedInvalidationIndex:
    """Immutable declared graph and CIK/date target buckets."""

    _edges: tuple[SecDependencyEdge, ...] = field(repr=False)
    _targets: tuple[SecDatedInvalidationTarget, ...] = field(repr=False)
    _index_identity: str
    _outgoing: Mapping[SecDataVersionId, tuple[SecDependencyEdge, ...]] = field(repr=False)
    _target_buckets: Mapping[str, tuple[SecDatedInvalidationTarget, ...]] = field(repr=False)
    _target_times: Mapping[str, tuple[datetime, ...]] = field(repr=False)

    def __init__(
        self,
        edges: Iterable[SecDependencyEdge],
        targets: Iterable[SecDatedInvalidationTarget],
        *,
        max_edges: int = 10_000,
        max_targets: int = 10_000,
    ) -> None:
        if type(max_edges) is not int or not 1 <= max_edges <= 10_000:
            raise ValueError("max_edges must be in 1..10000")
        if type(max_targets) is not int or not 1 <= max_targets <= 10_000:
            raise ValueError("max_targets must be in 1..10000")
        supplied_edges = tuple(islice(edges, max_edges + 1))
        supplied_targets = tuple(islice(targets, max_targets + 1))
        if len(supplied_edges) > max_edges:
            raise ResourceLimitError("dated dependency edge budget exceeded")
        if len(supplied_targets) > max_targets:
            raise ResourceLimitError("dated target budget exceeded")
        copied_supplied_edges = tuple(_copy_edge(edge) for edge in supplied_edges)
        unique_edges = {edge.edge_identity: edge for edge in copied_supplied_edges}
        unique_targets: dict[str, SecDatedInvalidationTarget] = {}
        for target in supplied_targets:
            copy = _copy_target(target)
            if copy.target_identity in unique_targets:
                continue
            unique_targets[copy.target_identity] = copy
        copied_edges = tuple(unique_edges[key] for key in sorted(unique_edges))
        outgoing: dict[SecDataVersionId, list[SecDependencyEdge]] = {}
        output_scope: dict[SecDataVersionId, tuple[str, str]] = {}
        indegree: dict[SecDataVersionId, int] = {}
        for edge in copied_edges:
            scope = (edge.canonical_cik, edge.recipe_identity)
            prior = output_scope.setdefault(edge.output_version, scope)
            if prior != scope:
                raise IdentityConflictError("derived graph output has conflicting CIK or recipe")
            outgoing.setdefault(edge.input_version, []).append(edge)
            indegree.setdefault(edge.input_version, 0)
            indegree[edge.output_version] = indegree.get(edge.output_version, 0) + 1
        queue = deque(node for node, degree in indegree.items() if degree == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for edge in outgoing.get(node, ()):
                indegree[edge.output_version] -= 1
                if indegree[edge.output_version] == 0:
                    queue.append(edge.output_version)
        if visited != len(indegree):
            raise SchemaMismatchError("dated dependency cycle rejected")
        for target in unique_targets.values():
            if output_scope.get(target.output_version) != (
                target.canonical_cik,
                target.recipe_identity,
            ):
                raise IdentityConflictError(
                    "dated target does not match declared output CIK or recipe"
                )
        buckets: dict[str, list[SecDatedInvalidationTarget]] = {}
        for target in unique_targets.values():
            buckets.setdefault(target.canonical_cik, []).append(target)
        ordered_buckets = {
            cik: tuple(sorted(values, key=lambda item: (item.valuation_at, item.target_identity)))
            for cik, values in buckets.items()
        }
        copied_targets = tuple(unique_targets[key] for key in sorted(unique_targets))
        object.__setattr__(self, "_edges", copied_edges)
        object.__setattr__(self, "_targets", copied_targets)
        object.__setattr__(
            self,
            "_outgoing",
            MappingProxyType(
                {
                    key: tuple(sorted(value, key=lambda e: e.edge_identity))
                    for key, value in outgoing.items()
                }
            ),
        )
        object.__setattr__(self, "_target_buckets", MappingProxyType(ordered_buckets))
        object.__setattr__(
            self,
            "_target_times",
            MappingProxyType(
                {
                    key: tuple(item.valuation_at for item in value)
                    for key, value in ordered_buckets.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "_index_identity",
            hashlib.sha256(
                _canonical(
                    {
                        "schema": "sec-dated-invalidation-index-v1",
                        "edges": [edge.canonical_payload() for edge in copied_edges],
                        "targets": [target.canonical_payload() for target in copied_targets],
                    }
                )
            ).hexdigest(),
        )

    @property
    def edges(self) -> tuple[SecDependencyEdge, ...]:
        return tuple(_copy_edge(edge) for edge in self._edges)

    @property
    def targets(self) -> tuple[SecDatedInvalidationTarget, ...]:
        return tuple(_copy_target(target) for target in self._targets)

    @property
    def index_identity(self) -> str:
        return self._index_identity

    def candidates(
        self, change: SecDatedInputChange, *, known_at: datetime
    ) -> tuple[SecDatedInvalidationTarget, ...]:
        """Return the bounded CIK/date candidates recorded by ``known_at``."""
        bucket, left, right = self._candidate_range(change)
        cutoff = _utc(known_at, "known_at")
        return tuple(
            _copy_target(target) for target in bucket[left:right] if target.recorded_at <= cutoff
        )

    def _candidate_range(
        self, change: SecDatedInputChange
    ) -> tuple[tuple[SecDatedInvalidationTarget, ...], int, int]:
        """Return an internal indexed range; callers charge each examined row."""
        bucket = self._target_buckets.get(change.canonical_cik, ())
        times = self._target_times.get(change.canonical_cik, ())
        return (
            bucket,
            bisect_left(times, change.valuation_from),
            bisect_left(times, change.valuation_to),
        )


@dataclass(frozen=True)
class SecDatedInvalidationPlan:
    index_identity: str
    known_at: datetime
    changes: tuple[SecDatedInputChange, ...]
    targets: tuple[SecDatedInvalidationTarget, ...]
    proofs: tuple[SecDatedInvalidationProof, ...]
    plan_identity: str


def _availability(change: SecDatedInputChange, target: SecDatedInvalidationTarget) -> bool:
    timestamp = (
        change.market_available_at
        if target.mode is SecPitMode.MARKET_KNOWN
        else change.system_available_at
    )
    if timestamp is None:
        label = (
            "market_available_at"
            if target.mode is SecPitMode.MARKET_KNOWN
            else "system_available_at"
        )
        raise CoverageError(f"dated target requires declared {label}")
    return timestamp <= target.knowledge_cutoff


def plan_sec_dated_invalidation(
    index: SecDatedInvalidationIndex,
    changes: Iterable[SecDatedInputChange],
    *,
    known_at: datetime,
    max_changes: int = 10_000,
    max_total_visits: int = 20_000,
    max_serialized_bytes: int = 8 * 1024 * 1024,
) -> SecDatedInvalidationPlan:
    """Plan exact targets, retaining one bounded deterministic proof per target."""
    if type(index) is not SecDatedInvalidationIndex:
        raise TypeError("invalid dated invalidation index")
    if type(max_changes) is not int or not 1 <= max_changes <= 10_000:
        raise ValueError("max_changes must be in 1..10000")
    if type(max_total_visits) is not int or not 1 <= max_total_visits <= 20_000:
        raise ValueError("max_total_visits must be in 1..20000")
    if type(max_serialized_bytes) is not int or not 1 <= max_serialized_bytes <= 16 * 1024 * 1024:
        raise ValueError("max_serialized_bytes must be in 1..16777216")
    cutoff = _utc(known_at, "known_at")
    supplied = tuple(islice(changes, max_changes + 1))
    if len(supplied) > max_changes:
        raise ResourceLimitError("dated input change budget exceeded")
    copied_changes = [_copy_change(item) for item in supplied]
    copied = tuple(copied_changes)
    active_changes = tuple(
        sorted(
            {item.change_identity: item for item in copied if item.recorded_at <= cutoff}.values(),
            key=lambda item: item.change_identity,
        )
    )
    selected: dict[str, SecDatedInvalidationTarget] = {}
    proofs: dict[str, SecDatedInvalidationProof] = {}
    visits = 0
    serialized_bytes = 256

    def charge_visits(count: int = 1) -> None:
        nonlocal visits
        visits += count
        if visits > max_total_visits:
            raise ResourceLimitError("dated invalidation traversal budget exceeded")

    def charge_bytes(value: object) -> None:
        nonlocal serialized_bytes
        serialized_bytes += len(_canonical(value))
        if serialized_bytes > max_serialized_bytes:
            raise ResourceLimitError("dated invalidation serialization budget exceeded")

    for change in active_changes:
        charge_bytes(change.canonical_payload())
    for change in active_changes:
        bucket, left, right = index._candidate_range(change)
        candidates: list[SecDatedInvalidationTarget] = []
        for target in bucket[left:right]:
            charge_visits()
            if target.recorded_at <= cutoff:
                candidates.append(target)
        if not candidates:
            continue
        predecessor: dict[SecDataVersionId, tuple[SecDataVersionId, SecDependencyEdge] | None] = {
            change.old_input_version: None
        }
        queue: deque[SecDataVersionId] = deque((change.old_input_version,))
        while queue:
            node = queue.popleft()
            charge_visits()
            for edge in index._outgoing.get(node, ()):
                charge_visits()
                if edge.recorded_at > cutoff or edge.canonical_cik != change.canonical_cik:
                    continue
                if edge.output_version not in predecessor:
                    predecessor[edge.output_version] = (node, edge)
                    queue.append(edge.output_version)
        for target in sorted(candidates, key=lambda item: item.target_identity):
            output = target.output_version
            last = predecessor.get(output)
            if last is None:
                continue
            if not _availability(change, target):
                continue
            path: list[str] = []
            current = output
            final_recipe: str | None = None
            while predecessor[current] is not None:
                charge_visits()
                step = predecessor[current]
                if step is None:  # Narrowing for static analysis; loop guard establishes this.
                    raise AssertionError("missing dated invalidation predecessor")
                parent, edge = step
                path.append(edge.edge_identity)
                if current == output:
                    final_recipe = edge.recipe_identity
                current = parent
            if final_recipe != target.recipe_identity:
                continue
            proof = SecDatedInvalidationProof(
                change.change_identity, target.target_identity, tuple(reversed(path))
            )
            prior = proofs.get(target.target_identity)
            if prior is None or (proof.change_identity, proof.edge_ids) < (
                prior.change_identity,
                prior.edge_ids,
            ):
                selected[target.target_identity] = target
                proofs[target.target_identity] = proof
                charge_bytes(target.canonical_payload())
                charge_bytes(
                    {
                        "change_identity": proof.change_identity,
                        "target_identity": proof.target_identity,
                        "edge_ids": list(proof.edge_ids),
                    }
                )
    selected_targets = tuple(_copy_target(selected[key]) for key in sorted(selected))
    ordered_proofs = tuple(proofs[target.target_identity] for target in selected_targets)
    payload = {
        "schema": "sec-dated-invalidation-plan-v1",
        "index_identity": index.index_identity,
        "known_at": _stamp(cutoff),
        "changes": [item.canonical_payload() for item in active_changes],
        "targets": [item.canonical_payload() for item in selected_targets],
        "proofs": [
            {
                "change_identity": item.change_identity,
                "target_identity": item.target_identity,
                "edge_ids": list(item.edge_ids),
            }
            for item in ordered_proofs
        ],
    }
    encoded_payload = _canonical(payload)
    if len(encoded_payload) > max_serialized_bytes:
        raise ResourceLimitError("dated invalidation serialization budget exceeded")
    return SecDatedInvalidationPlan(
        index.index_identity,
        cutoff,
        active_changes,
        selected_targets,
        ordered_proofs,
        hashlib.sha256(encoded_payload).hexdigest(),
    )
