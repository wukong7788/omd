import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, cast

_SECRET = re.compile(
    r"(^|_)(token|secret|password|api_key|access_key|authorization|cookie)$", re.IGNORECASE
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _canon(v: Any, path: str = "parameters") -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("non-finite float")
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise ValueError("naive datetime")
        return {"__datetime__": v.astimezone(UTC).isoformat().replace("+00:00", "Z")}
    if isinstance(v, date):
        return {"__date__": v.isoformat()}
    if isinstance(v, Mapping):
        out: dict[str, Any] = {}
        for raw_k, x in cast(Mapping[Any, Any], v).items():
            if not isinstance(raw_k, str):
                raise TypeError("mapping keys must be strings")
            k = raw_k
            normalized_key = re.sub(r"[^A-Za-z0-9]+", "_", k)
            if _SECRET.search(normalized_key):
                raise ValueError(f"secret key at {path}.{k}")
            out[k] = _canon(x, f"{path}.{k}")
        return {k: out[k] for k in sorted(out)}
    if isinstance(v, (list, tuple)):
        return [_canon(x, f"{path}[]") for x in cast(list[Any] | tuple[Any, ...], v)]
    raise TypeError(f"unsupported value at {path}")


def _freeze(v: Any) -> Any:
    if isinstance(v, dict):
        source = cast(dict[Any, Any], v)
        return MappingProxyType({k: _freeze(x) for k, x in source.items()})
    if isinstance(v, list):
        return tuple(_freeze(x) for x in cast(list[Any], v))
    if isinstance(v, tuple):
        return tuple(_freeze(x) for x in cast(tuple[Any, ...], v))
    return v


def _valid_field(value: object) -> bool:
    return isinstance(value, str) and bool(value)


@dataclass(frozen=True)
class RequestSpec:
    provider: str
    endpoint: str
    parameters: Mapping[str, Any]
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        for n in (self.provider, self.endpoint):
            if not _IDENTIFIER.fullmatch(n) or n in {".", ".."}:
                raise ValueError("unsafe identifier")
        if any(not _valid_field(f) for f in self.fields) or len(set(self.fields)) != len(
            self.fields
        ):
            raise ValueError("invalid fields")
        object.__setattr__(self, "parameters", _freeze(_canon(dict(self.parameters))))

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "parameters": _canon(self.parameters),
            "fields": list(self.fields),
        }

    @property
    def canonical_json(self) -> bytes:
        return json.dumps(
            self.canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

    @property
    def request_identity(self) -> str:
        return hashlib.sha256(self.canonical_json).hexdigest()

    @property
    def effective_parameters(self) -> dict[str, Any]:
        return _canon(self.parameters)
