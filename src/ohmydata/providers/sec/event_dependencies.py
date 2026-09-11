"""Explicit version dependency edges; no inferred issuer or date propagation."""

from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import islice
from types import MappingProxyType

from ._event_discovery_models import _canonical, _stamp, _utc
from .errors import ResourceLimitError, SchemaMismatchError


class SecDataVersionKind(str, Enum):
    RAW_FACT = "RAW_FACT"
    NORMALIZED_FACT = "NORMALIZED_FACT"
    OBSERVED_PRODUCTION = "OBSERVED_PRODUCTION"
    DERIVED_METRIC = "DERIVED_METRIC"
    RECIPE_CONFIGURATION = "RECIPE_CONFIGURATION"
    INSTRUMENT_IDENTITY = "INSTRUMENT_IDENTITY"


def _identity(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("version identities must be lowercase SHA-256 strings")
    return value


@dataclass(frozen=True, order=True)
class SecDataVersionId:
    kind: SecDataVersionKind
    identity: str

    def __post_init__(self) -> None:
        if type(self.kind) is not SecDataVersionKind:
            raise TypeError("invalid SEC data version kind")
        _identity(self.identity)

    def canonical_payload(self) -> dict[str, str]:
        return {"kind": self.kind.value, "identity": self.identity}


@dataclass(frozen=True)
class SecDependencyEdge:
    input_version: SecDataVersionId
    output_version: SecDataVersionId
    canonical_cik: str
    recipe_identity: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.input_version) is not SecDataVersionId
            or type(self.output_version) is not SecDataVersionId
        ):
            raise TypeError("dependency endpoints must be typed version identities")
        if self.input_version == self.output_version:
            raise ValueError("dependency self edge rejected")
        if (
            type(self.canonical_cik) is not str
            or re.fullmatch(r"[1-9][0-9]{0,9}", self.canonical_cik) is None
        ):
            raise ValueError("dependency CIK must be canonical digits")
        _identity(self.recipe_identity)
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "sec-dependency-edge-v1",
            "input_version": self.input_version.canonical_payload(),
            "output_version": self.output_version.canonical_payload(),
            "canonical_cik": self.canonical_cik,
            "recipe_identity": self.recipe_identity,
            "recorded_at": _stamp(self.recorded_at),
        }

    @property
    def edge_identity(self) -> str:
        return hashlib.sha256(_canonical(self.canonical_payload())).hexdigest()


@dataclass(frozen=True, init=False)
class SecDependencyIndex:
    """Build once, then traverse only edges reachable from explicit changed IDs."""

    edges: tuple[SecDependencyEdge, ...]
    index_identity: str
    _outgoing: Mapping[SecDataVersionId, tuple[SecDependencyEdge, ...]] = field(repr=False)

    def __init__(self, edges: Iterable[SecDependencyEdge], *, max_edges: int = 10_000) -> None:
        if type(max_edges) is not int or not 1 <= max_edges <= 10_000:
            raise ValueError("max_edges must be in 1..10000")
        supplied = tuple(islice(edges, max_edges + 1))
        if len(supplied) > max_edges:
            raise ResourceLimitError("dependency edge budget exceeded")
        outgoing: dict[SecDataVersionId, list[SecDependencyEdge]] = {}
        unique: dict[str, SecDependencyEdge] = {}
        indegree: dict[SecDataVersionId, int] = {}
        for edge in supplied:
            if type(edge) is not SecDependencyEdge:
                raise TypeError("invalid dependency edge")
            if edge.edge_identity in unique:
                continue
            unique[edge.edge_identity] = edge
            outgoing.setdefault(edge.input_version, []).append(edge)
            indegree.setdefault(edge.input_version, 0)
            indegree[edge.output_version] = indegree.get(edge.output_version, 0) + 1
        queue = deque(node for node, count in indegree.items() if count == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for edge in outgoing.get(node, ()):
                indegree[edge.output_version] -= 1
                if indegree[edge.output_version] == 0:
                    queue.append(edge.output_version)
        if visited != len(indegree):
            raise SchemaMismatchError("dependency cycle rejected")
        object.__setattr__(self, "edges", tuple(unique[key] for key in sorted(unique)))
        object.__setattr__(
            self,
            "_outgoing",
            MappingProxyType({key: tuple(value) for key, value in outgoing.items()}),
        )
        identity = hashlib.sha256(
            _canonical(
                {
                    "schema": "sec-dependency-index-v1",
                    "edges": [edge.canonical_payload() for edge in self.edges],
                }
            )
        ).hexdigest()
        object.__setattr__(self, "index_identity", identity)

    def affected_versions(
        self,
        seeds: Iterable[SecDataVersionId],
        *,
        knowledge_cutoff: datetime,
    ) -> tuple[SecDataVersionId, ...]:
        """Return reachable outputs using only edges recorded by the cutoff."""
        return plan_sec_event_invalidation(self, seeds, known_at=knowledge_cutoff).affected_outputs


@dataclass(frozen=True)
class SecEventInvalidationPlan:
    index_identity: str
    changed_inputs: tuple[SecDataVersionId, ...]
    known_at: datetime
    affected_outputs: tuple[SecDataVersionId, ...]
    traversed_edge_ids: tuple[str, ...]
    plan_identity: str


def plan_sec_event_invalidation(
    index: SecDependencyIndex,
    changed_inputs: Iterable[SecDataVersionId],
    *,
    known_at: datetime,
) -> SecEventInvalidationPlan:
    if type(index) is not SecDependencyIndex:
        raise TypeError("invalid dependency index")
    cutoff = _utc(known_at, "known_at")
    supplied = tuple(islice(changed_inputs, 10_001))
    if len(supplied) > 10_000:
        raise ResourceLimitError("dependency seed budget exceeded")
    if any(type(seed) is not SecDataVersionId for seed in supplied):
        raise TypeError("dependency seeds must be typed version identities")
    seen = set(supplied)
    affected: set[SecDataVersionId] = set()
    traversed: set[str] = set()
    queue = deque(seen)
    while queue:
        node = queue.popleft()
        for edge in index._outgoing.get(node, ()):
            if edge.recorded_at > cutoff:
                continue
            traversed.add(edge.edge_identity)
            affected.add(edge.output_version)
            if edge.output_version not in seen:
                seen.add(edge.output_version)
                queue.append(edge.output_version)
    changes, outputs, edge_ids = (
        tuple(sorted(set(supplied))),
        tuple(sorted(affected)),
        tuple(sorted(traversed)),
    )
    payload = {
        "schema": "sec-event-invalidation-plan-v1",
        "index_identity": index.index_identity,
        "changed_inputs": [item.canonical_payload() for item in changes],
        "known_at": _stamp(cutoff),
        "affected_outputs": [item.canonical_payload() for item in outputs],
        "traversed_edge_ids": list(edge_ids),
    }
    return SecEventInvalidationPlan(
        index.index_identity,
        changes,
        cutoff,
        outputs,
        edge_ids,
        hashlib.sha256(_canonical(payload)).hexdigest(),
    )
