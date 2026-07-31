from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, cast

from .policy import AttemptRecord
from .specs import RequestSpec


class EmptyDisposition(str, Enum):
    NOT_EMPTY = "NOT_EMPTY"
    ALLOWED_EMPTY = "ALLOWED_EMPTY"


@dataclass(frozen=True)
class FetchProvenance:
    provider: str
    endpoint: str
    request_identity: str
    effective_parameters: dict[str, Any]
    requested_fields: tuple[str, ...]
    retrieved_at: datetime
    attempts: tuple[AttemptRecord, ...]
    row_count: int
    columns: tuple[str, ...]
    warnings: tuple[str, ...]
    snapshot_identities: tuple[str, ...]
    empty_disposition: EmptyDisposition

    @classmethod
    def from_request(cls, spec: RequestSpec, **kwargs: Any) -> "FetchProvenance":
        return cls(
            spec.provider,
            spec.endpoint,
            spec.request_identity,
            spec.effective_parameters,
            tuple(spec.fields),
            **kwargs,
        )

    def __post_init__(self):
        if (
            self.retrieved_at.tzinfo is None
            or self.row_count < 0
            or len(set(self.columns)) != len(self.columns)
        ):
            raise ValueError("invalid provenance")
        if (self.row_count == 0) != (self.empty_disposition == EmptyDisposition.ALLOWED_EMPTY):
            raise ValueError("empty disposition mismatch")
        object.__setattr__(self, "retrieved_at", self.retrieved_at.astimezone(UTC))
        for name in ("attempts", "requested_fields", "columns", "warnings", "snapshot_identities"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "effective_parameters", self._freeze(self.effective_parameters))

    @staticmethod
    def _freeze(value: Any) -> Any:
        if isinstance(value, dict):
            source = cast(dict[str, Any], value)
            return MappingProxyType({k: FetchProvenance._freeze(v) for k, v in source.items()})
        if isinstance(value, list):
            return tuple(FetchProvenance._freeze(v) for v in cast(list[Any], value))
        return value

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "request_identity": self.request_identity,
            "effective_parameters": self._thaw(self.effective_parameters),
            "requested_fields": list(self.requested_fields),
            "retrieved_at": self.retrieved_at.astimezone(UTC).isoformat(),
            "attempt_count": self.attempt_count,
            "attempts": [a.__dict__ for a in self.attempts],
            "row_count": self.row_count,
            "columns": list(self.columns),
            "warnings": list(self.warnings),
            "snapshot_identities": list(self.snapshot_identities),
            "empty_disposition": self.empty_disposition.value,
        }

    @staticmethod
    def _thaw(value: Any) -> Any:
        if isinstance(value, MappingProxyType):
            source = cast(dict[str, Any], value)
            return {k: FetchProvenance._thaw(v) for k, v in source.items()}
        if isinstance(value, tuple):
            return [FetchProvenance._thaw(v) for v in cast(tuple[Any, ...], value)]
        return value
