"""Provider-independent source availability evidence."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .snapshot import SnapshotObservationRef, SnapshotStore


class AvailabilityBasis(str, Enum):
    SOURCE_DECLARED = "SOURCE_DECLARED"
    INFERRED_SCHEDULE = "INFERRED_SCHEDULE"
    PROVIDER_FIRST_OBSERVED = "PROVIDER_FIRST_OBSERVED"
    UNKNOWN = "UNKNOWN"


class AvailabilityPrecision(str, Enum):
    TIMESTAMP = "TIMESTAMP"
    DATE = "DATE"
    UNKNOWN = "UNKNOWN"


def _utc_datetime(value: Any, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _source_value(value: Any) -> datetime | date | None:
    if value is None:
        return None
    if type(value) is datetime:
        return _utc_datetime(value, "source_available_at")
    if type(value) is date:
        return value
    raise TypeError("source_available_at must be a date, datetime, or None")


@dataclass(frozen=True)
class AvailabilityEvidence:
    source_available_at: datetime | date | None
    provider_first_observed_at: datetime
    snapshot_fetched_at: datetime
    availability_basis: AvailabilityBasis
    availability_precision: AvailabilityPrecision

    def __post_init__(self) -> None:
        if type(self.availability_basis) is not AvailabilityBasis:
            raise TypeError("availability_basis must be an AvailabilityBasis")
        if type(self.availability_precision) is not AvailabilityPrecision:
            raise TypeError("availability_precision must be an AvailabilityPrecision")
        source = _source_value(self.source_available_at)
        first = _utc_datetime(self.provider_first_observed_at, "provider_first_observed_at")
        fetched = _utc_datetime(self.snapshot_fetched_at, "snapshot_fetched_at")
        if fetched < first:
            raise ValueError("snapshot_fetched_at must not precede provider_first_observed_at")
        valid_source = (
            (
                self.availability_basis
                in (AvailabilityBasis.SOURCE_DECLARED, AvailabilityBasis.INFERRED_SCHEDULE)
                and self.availability_precision is AvailabilityPrecision.TIMESTAMP
                and type(source) is datetime
            )
            or (
                self.availability_basis
                in (AvailabilityBasis.SOURCE_DECLARED, AvailabilityBasis.INFERRED_SCHEDULE)
                and self.availability_precision is AvailabilityPrecision.DATE
                and type(source) is date
            )
            or (
                self.availability_basis
                in (AvailabilityBasis.PROVIDER_FIRST_OBSERVED, AvailabilityBasis.UNKNOWN)
                and self.availability_precision is AvailabilityPrecision.UNKNOWN
                and source is None
            )
        )
        if not valid_source:
            raise ValueError("invalid availability evidence state")
        object.__setattr__(self, "source_available_at", source)
        object.__setattr__(self, "provider_first_observed_at", first)
        object.__setattr__(self, "snapshot_fetched_at", fetched)

    @property
    def availability_anchor(self) -> datetime | date | None:
        if self.availability_basis in (
            AvailabilityBasis.SOURCE_DECLARED,
            AvailabilityBasis.INFERRED_SCHEDULE,
        ):
            return self.source_available_at
        if self.availability_basis is AvailabilityBasis.PROVIDER_FIRST_OBSERVED:
            return self.provider_first_observed_at
        return None

    @property
    def pit_proven(self) -> bool:
        return (
            self.availability_basis is AvailabilityBasis.SOURCE_DECLARED
            and self.availability_precision is AvailabilityPrecision.TIMESTAMP
        )

    def to_dict(self) -> dict[str, Any]:
        def encode(value: datetime | date | None) -> str | None:
            if value is None:
                return None
            if type(value) is datetime:
                return value.isoformat().replace("+00:00", "Z")
            return value.isoformat()

        return {
            "source_available_at": encode(self.source_available_at),
            "provider_first_observed_at": encode(self.provider_first_observed_at),
            "snapshot_fetched_at": encode(self.snapshot_fetched_at),
            "availability_basis": self.availability_basis.value,
            "availability_precision": self.availability_precision.value,
            "pit_proven": self.pit_proven,
        }

    @classmethod
    def from_observation(
        cls,
        store: "SnapshotStore",
        observation: "SnapshotObservationRef",
        *,
        source_available_at: datetime | date | None = None,
        availability_basis: AvailabilityBasis = AvailabilityBasis.PROVIDER_FIRST_OBSERVED,
        availability_precision: AvailabilityPrecision = AvailabilityPrecision.UNKNOWN,
    ) -> "AvailabilityEvidence":
        replay = store.replay_observation(observation)
        retrieved_at = replay.manifest["retrieved_at"]
        first = datetime.fromisoformat(retrieved_at).astimezone(UTC)
        return cls(
            source_available_at,
            first,
            observation.snapshot_fetched_at,
            availability_basis,
            availability_precision,
        )
