"""Bounded caller-declared identities; no source verification or PIT proof."""

import json
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
from itertools import pairwise
from types import MappingProxyType

from .errors import CoverageError, IdentityConflictError, ResourceLimitError


def _text(value: str, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    if not value or len(value) > 1024 or len(value.encode()) > 1024:
        raise ValueError(f"{name} must contain 1..1024 UTF-8 bytes")
    if value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(f"{name} contains whitespace or control characters")
    return value


def _time(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError("recording/cutoff time must be timezone aware")
    return value.astimezone(UTC)


def _date(value: date) -> date:
    if type(value) is not date:
        raise TypeError("effective bounds/query must be exact dates")
    return value


def _hash(schema: str, **fields: object) -> str:
    payload = json.dumps(
        {"schema": schema, **fields}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return sha256(payload).hexdigest()


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class InstrumentType(str, Enum):
    COMMON_SHARE = "COMMON_SHARE"
    ADR = "ADR"
    ETF = "ETF"
    INDEX = "INDEX"


@dataclass(frozen=True, slots=True)
class IssuerIdentity:
    issuer_id: str
    evidence_reference: str
    recorded_at: datetime
    declaration_identity: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.issuer_id, "issuer_id")
        _text(self.evidence_reference, "evidence_reference")
        object.__setattr__(self, "recorded_at", _time(self.recorded_at))
        object.__setattr__(
            self,
            "declaration_identity",
            _hash(
                "issuer-declaration-v1",
                issuer_id=self.issuer_id,
                evidence_reference=self.evidence_reference,
                recorded_at=_stamp(self.recorded_at),
            ),
        )


@dataclass(frozen=True, slots=True)
class InstrumentIdentity:
    instrument_id: str
    issuer_id: str | None
    instrument_type: InstrumentType
    evidence_reference: str
    recorded_at: datetime
    declaration_identity: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if type(self.instrument_type) is not InstrumentType:
            raise TypeError("instrument_type must be InstrumentType")
        if self.issuer_id is None:
            if self.instrument_type is not InstrumentType.INDEX:
                raise ValueError("non-index instruments require issuer_id")
        else:
            _text(self.issuer_id, "issuer_id")
        _text(self.evidence_reference, "evidence_reference")
        object.__setattr__(self, "recorded_at", _time(self.recorded_at))
        object.__setattr__(
            self,
            "declaration_identity",
            _hash(
                "instrument-declaration-v1",
                instrument_id=self.instrument_id,
                issuer_id=self.issuer_id,
                instrument_type=self.instrument_type.value,
                evidence_reference=self.evidence_reference,
                recorded_at=_stamp(self.recorded_at),
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderInstrumentAlias:
    provider: str
    alias: str
    venue: str
    currency: str | None
    instrument_id: str
    effective_from: date
    effective_to: date
    evidence_reference: str
    recorded_at: datetime
    declaration_identity: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("provider", "alias", "venue", "instrument_id", "evidence_reference"):
            _text(getattr(self, name), name)
        if self.currency is not None and (
            type(self.currency) is not str
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("currency must be uppercase three-letter text or None")
        if _date(self.effective_from) >= _date(self.effective_to):
            raise ValueError("effective interval must be nonempty")
        object.__setattr__(self, "recorded_at", _time(self.recorded_at))
        object.__setattr__(
            self,
            "declaration_identity",
            _hash(
                "provider-instrument-alias-v1",
                provider=self.provider,
                alias=self.alias,
                venue=self.venue,
                currency=self.currency,
                instrument_id=self.instrument_id,
                effective_from=self.effective_from.isoformat(),
                effective_to=self.effective_to.isoformat(),
                evidence_reference=self.evidence_reference,
                recorded_at=_stamp(self.recorded_at),
            ),
        )


@dataclass(frozen=True, slots=True)
class InstrumentResolution:
    index_identity: str
    issuer: IssuerIdentity | None
    instrument: InstrumentIdentity
    alias_declaration: ProviderInstrumentAlias
    effective_date: date
    knowledge_cutoff: datetime
    declaration_recorded_bound: datetime
    binding_identity: str
    resolution_identity: str


@dataclass(frozen=True, slots=True, init=False)
class InstrumentIdentityIndex:
    """Immutable catalog; reject all overlaps, including future declarations."""

    identity: str
    _issuers: Mapping[str, IssuerIdentity]
    _instruments: Mapping[str, InstrumentIdentity]
    _aliases: Mapping[tuple[str, str, str], tuple[ProviderInstrumentAlias, ...]]
    _starts: Mapping[tuple[str, str, str], tuple[date, ...]]

    def __init__(
        self,
        issuers: Iterable[IssuerIdentity],
        instruments: Iterable[InstrumentIdentity],
        aliases: Iterable[ProviderInstrumentAlias],
        *,
        max_records: int = 10000,
    ):
        if type(max_records) is not int or not 1 <= max_records <= 10000:
            raise ValueError("max_records must be an integer in 1..10000")
        count = 0

        def collect(records, cls, key):
            nonlocal count
            result = {}
            for record in records:
                count += 1
                if count > max_records:
                    raise ResourceLimitError("identity declaration budget exceeded")
                if type(record) is not cls:
                    raise TypeError("unexpected identity declaration type")
                # Reconstruct a validated independent copy; never trust cached hashes.
                copy = cls(
                    **{
                        name: getattr(record, name)
                        for name, f in cls.__dataclass_fields__.items()
                        if f.init
                    }
                )
                record_key = getattr(copy, key)
                if record_key in result and result[record_key] != copy:
                    raise IdentityConflictError("conflicting identity declaration")
                result[record_key] = copy
            return result

        issuer_map = collect(issuers, IssuerIdentity, "issuer_id")
        instrument_map = collect(instruments, InstrumentIdentity, "instrument_id")
        alias_map = collect(aliases, ProviderInstrumentAlias, "declaration_identity")
        for instrument in instrument_map.values():
            if instrument.issuer_id is not None:
                issuer = issuer_map.get(instrument.issuer_id)
                if issuer is None:
                    raise CoverageError("instrument issuer declaration absent")
                if instrument.recorded_at < issuer.recorded_at:
                    raise ValueError("instrument predates issuer declaration")
        buckets = {}
        for alias in alias_map.values():
            instrument = instrument_map.get(alias.instrument_id)
            if instrument is None:
                raise CoverageError("alias instrument declaration absent")
            if alias.recorded_at < instrument.recorded_at:
                raise ValueError("alias predates instrument declaration")
            if alias.currency is None and instrument.instrument_type is not InstrumentType.INDEX:
                raise ValueError("non-index alias requires quote currency")
            buckets.setdefault((alias.provider, alias.alias, alias.venue), []).append(alias)
        ordered = {}
        for key, values in buckets.items():
            values.sort(key=lambda a: (a.effective_from, a.declaration_identity))
            if any(a.effective_to > b.effective_from for a, b in pairwise(values)):
                raise IdentityConflictError("overlapping provider alias intervals")
            ordered[key] = tuple(values)
        object.__setattr__(self, "_issuers", MappingProxyType(issuer_map))
        object.__setattr__(self, "_instruments", MappingProxyType(instrument_map))
        object.__setattr__(self, "_aliases", MappingProxyType(ordered))
        object.__setattr__(
            self,
            "_starts",
            MappingProxyType({k: tuple(a.effective_from for a in v) for k, v in ordered.items()}),
        )
        object.__setattr__(
            self,
            "identity",
            _hash(
                "instrument-identity-index-v1",
                issuers=sorted(i.declaration_identity for i in issuer_map.values()),
                instruments=sorted(i.declaration_identity for i in instrument_map.values()),
                aliases=sorted(alias_map),
            ),
        )

    def resolve(
        self,
        *,
        provider: str,
        alias: str,
        venue: str,
        effective_date: date,
        knowledge_cutoff: datetime,
    ) -> InstrumentResolution:
        key = (_text(provider, "provider"), _text(alias, "alias"), _text(venue, "venue"))
        effective = _date(effective_date)
        cutoff = _time(knowledge_cutoff)
        position = bisect_right(self._starts.get(key, ()), effective) - 1
        if position < 0:
            raise CoverageError("no effective instrument alias")
        selected = self._aliases[key][position]
        if effective >= selected.effective_to or selected.recorded_at > cutoff:
            raise CoverageError("no known effective instrument alias")
        instrument = self._instruments[selected.instrument_id]
        issuer = None if instrument.issuer_id is None else self._issuers[instrument.issuer_id]
        binding = _hash(
            "instrument-binding-v1",
            issuer=None if issuer is None else issuer.declaration_identity,
            instrument=instrument.declaration_identity,
            alias=selected.declaration_identity,
        )
        identity = _hash(
            "instrument-resolution-v1",
            index_identity=self.identity,
            binding_identity=binding,
            provider=provider,
            alias=alias,
            venue=venue,
            effective_date=effective.isoformat(),
            knowledge_cutoff=_stamp(cutoff),
            declaration_recorded_bound=_stamp(selected.recorded_at),
        )
        return InstrumentResolution(
            self.identity,
            issuer,
            instrument,
            selected,
            effective,
            cutoff,
            selected.recorded_at,
            binding,
            identity,
        )
