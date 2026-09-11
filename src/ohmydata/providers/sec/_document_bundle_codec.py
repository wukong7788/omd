"""Strict document-bundle records and bounded recursive dependency admission."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...core import SnapshotObservationRef, SnapshotStore
from ._observed_financial_bundle_codec import (
    _COMMIT,
    _QUALITY,
    _commit,
    _identity,
    _quality,
    _receipt,
    _time,
)
from .document_source import ObservationResolver, _canonical
from .observed_financial_replay import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
)
from .pit import SecQualityStatus


@dataclass
class Dependencies:
    resolver: ObservationResolver
    maximum: int
    used: int = 0
    records: dict[str, tuple[SnapshotStore, SnapshotObservationRef, int]] = field(
        default_factory=dict
    )

    def resolve(self, identity: str) -> tuple[SnapshotStore, SnapshotObservationRef]:
        _identity(identity, "observation_identity")
        resolved = self.resolver(identity)
        if (
            type(resolved) is not tuple
            or len(resolved) != 2
            or type(resolved[0]) is not SnapshotStore
            or type(resolved[1]) is not SnapshotObservationRef
        ):
            raise TypeError("resolver must return SnapshotStore and SnapshotObservationRef")
        store, observation = resolved
        if observation.observation_identity != identity:
            raise ValueError("document bundle resolved identity mismatch")
        prior = self.records.get(identity)
        if prior is not None:
            if observation != prior[1] or store.root != prior[0].root:
                raise ValueError("document bundle resolver changed receipt or path")
            return store, observation
        remaining = self.maximum - self.used
        if remaining <= 0 or len(self.records) >= 120:
            raise ValueError("document bundle dependency limit exceeded")
        replay = store.replay_observation(
            observation, max_payload_bytes=min(8 * 1024 * 1024, remaining)
        )
        size = len(replay.payload)
        self.used += size
        self.records[identity] = (store, observation, size)
        return store, observation

    def claim(self, value: object) -> tuple[SnapshotStore, SnapshotObservationRef]:
        if not isinstance(value, dict):
            raise ValueError("invalid document bundle receipt")  # noqa: TRY004 -- persisted schema
        identity = _identity(value.get("observation_identity"), "observation_identity")
        store, observation = self.resolve(identity)
        if _canonical(_receipt(observation)) != _canonical(value):
            raise ValueError("document bundle receipt mismatch")
        return store, observation


def quality(value: object) -> SecObservedFinancialQualityRecord:
    if not isinstance(value, dict) or set(value) != _QUALITY:
        raise ValueError("invalid document bundle quality receipt")
    result = SecObservedFinancialQualityRecord(
        value["production_identity"],
        value["quality_policy_version"],
        SecQualityStatus(value["status"]),
        _time(value["recorded_at"], "recorded_at"),
        value["supersedes_quality_record_id"],
    )
    if _canonical(_quality(result)) != _canonical(value):
        raise ValueError("document bundle quality identity mismatch")
    return result


def commit(value: object) -> SecObservedFinancialConsumerCommit:
    if not isinstance(value, dict) or set(value) != _COMMIT:
        raise ValueError("invalid document bundle commit receipt")
    result = SecObservedFinancialConsumerCommit(
        value["production_identity"],
        value["quality_record_id"],
        value["consumer_dataset_identity"],
        _time(value["committed_at"], "committed_at"),
    )
    if _canonical(_commit(result)) != _canonical(value):
        raise ValueError("document bundle commit identity mismatch")
    return result
