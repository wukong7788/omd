"""Pinned-parser evidence index for live SEC financial statement units."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from ._observed_xbrl_units import decode_raw_units
from .financials import SecStatementRow

_MAX_ELEMENTS = 200_000
_MAX_DEPTH = 128
_RAW_INSTANCE_MAX_BYTES = 16 * 1024 * 1024
RAW_XBRL_INSTANCE_MAX_BYTES = _RAW_INSTANCE_MAX_BYTES
_MAX_BYTES = _RAW_INSTANCE_MAX_BYTES


class SecUnitEvidenceError(ValueError):
    """Raw filing-instance evidence cannot safely bind native statement facts."""


@dataclass(frozen=True)
class RawInstanceEvidence:
    """One parsed raw instance, reusable for every native statement in a filing."""

    units: dict[str, str]
    facts: Mapping[tuple[Any, ...], tuple[tuple[Any, ...], ...]]
    conflicts: frozenset[tuple[str, str]]
    expected_cik: str | None


def _dimensions(dimensions: Mapping[str, Any]) -> str | None:
    if not dimensions:
        return None
    pairs: dict[str, str] = {}
    for axis, member in dimensions.items():
        if not isinstance(axis, str) or not axis or not isinstance(member, str) or not member:
            raise SecUnitEvidenceError("raw XBRL dimension identity is unavailable")
        if axis in pairs and pairs[axis] != member:
            raise SecUnitEvidenceError("raw XBRL dimension identity is unavailable")
        pairs[axis] = member
    import json

    return json.dumps(pairs, sort_keys=True, separators=(",", ":"))


def _context_identity(context: Any) -> tuple[str, str | None, str | None, str | None]:
    try:
        period, dimensions = context.period, context.dimensions
    except AttributeError as exc:
        raise SecUnitEvidenceError("raw XBRL fact context is unavailable") from exc
    if not isinstance(period, Mapping) or not isinstance(dimensions, Mapping):
        raise SecUnitEvidenceError("raw XBRL fact context is unavailable")
    if period.get("type") == "instant" and isinstance(period.get("instant"), str):
        return f"instant_{period['instant']}", None, period["instant"], _dimensions(dimensions)
    if (
        period.get("type") == "duration"
        and isinstance(period.get("startDate"), str)
        and isinstance(period.get("endDate"), str)
    ):
        return (
            f"duration_{period['startDate']}_{period['endDate']}",
            period["startDate"],
            period["endDate"],
            _dimensions(dimensions),
        )
    raise SecUnitEvidenceError("raw XBRL context period is unavailable")


def _parse_decimals(decimals: Any) -> int | str | None:
    """Parse XBRL decimals as an int or 'INF', or None if malformed/missing."""
    if decimals is None or type(decimals).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(decimals, bool):
        return None
    if isinstance(decimals, int):
        if abs(decimals) > 1000:
            return None
        return decimals
    if isinstance(decimals, str):
        cleaned = decimals.strip()
        if cleaned.upper() == "INF":
            return "INF"
        if re.fullmatch(r"[+-]?\d+", cleaned):
            val = int(cleaned)
            if abs(val) > 1000:
                return None
            return val
    return None


def _half_quantum(decimals: int) -> Fraction:
    """Return half the rounding quantum 10**(-decimals) as an exact Fraction."""
    if decimals >= 0:
        return Fraction(1, 2 * (10**decimals))
    return Fraction(10 ** (-decimals), 2)


def _are_signatures_compatible(sig_a: tuple[Any, ...], sig_b: tuple[Any, ...]) -> bool:
    """Determine whether two raw XBRL facts with the same concept/context are compatible."""
    if sig_a == sig_b:
        return True

    unit_ref_a, value_a, dec_str_a, period_key_a, start_a, end_a, dim_a = sig_a
    unit_ref_b, value_b, dec_str_b, period_key_b, start_b, end_b, dim_b = sig_b

    if (period_key_a, start_a, end_a, dim_a) != (period_key_b, start_b, end_b, dim_b):
        return False
    if unit_ref_a != unit_ref_b:
        return False
    if not isinstance(value_a, Decimal) or not value_a.is_finite():
        return False
    if not isinstance(value_b, Decimal) or not value_b.is_finite():
        return False

    dec_a = _parse_decimals(dec_str_a)
    dec_b = _parse_decimals(dec_str_b)
    if dec_a is None or dec_b is None:
        return False

    if value_a == value_b:
        return True

    if not isinstance(dec_a, int) or not isinstance(dec_b, int):
        return False

    h_a = _half_quantum(dec_a)
    h_b = _half_quantum(dec_b)
    diff = abs(Fraction(value_a) - Fraction(value_b))
    return diff < (h_a + h_b)


def prepare_raw_instance(raw: bytes, *, expected_cik: str | None = None) -> RawInstanceEvidence:
    """Parse and validate an instance once, preserving duplicate facts for checking."""
    if type(raw) is not bytes or len(raw) > _RAW_INSTANCE_MAX_BYTES:
        raise SecUnitEvidenceError("raw XBRL instance exceeds byte limit")
    try:
        from edgar.xbrl import XBRL
        from edgar.xbrl.models import XBRLProcessingError
    except ImportError as exc:
        raise SecUnitEvidenceError(
            "raw XBRL instance cannot be parsed by pinned edgartools"
        ) from exc
    try:
        units = decode_raw_units(
            raw,
            max_elements=_MAX_ELEMENTS,
            max_depth=_MAX_DEPTH,
            max_bytes=_RAW_INSTANCE_MAX_BYTES,
        )

        parsed = XBRL()
        parsed.parser.parse_instance_content(raw.decode("utf-8"))
    except SecUnitEvidenceError:
        raise
    except (UnicodeDecodeError, AttributeError, TypeError, ValueError, XBRLProcessingError) as exc:
        raise SecUnitEvidenceError(
            "raw XBRL instance cannot be parsed by pinned edgartools"
        ) from exc
    facts = getattr(parsed.parser, "facts", None)
    contexts = getattr(parsed.parser, "contexts", None)
    if not isinstance(facts, Mapping) or not isinstance(contexts, Mapping):
        raise SecUnitEvidenceError("raw XBRL parser facts are unavailable")
    expected = (expected_cik.lstrip("0") or "0") if expected_cik is not None else None
    indexed: dict[tuple[Any, ...], list[tuple[Any, ...]]] = defaultdict(list)
    by_context: dict[tuple[str, str], set[tuple[Any, ...]]] = defaultdict(set)
    conflicts: set[tuple[str, str]] = set()
    for fact in facts.values():
        context_ref = getattr(fact, "context_ref", None)
        context = contexts.get(context_ref)
        if not isinstance(context_ref, str) or context is None:
            raise SecUnitEvidenceError("raw XBRL fact context is unavailable")
        entity = getattr(context, "entity", {})
        cik = entity.get("identifier", "") if isinstance(entity, Mapping) else ""
        try:
            value = Decimal(str(fact.value))
        except (AttributeError, InvalidOperation, ValueError):
            continue
        if not value.is_finite():
            raise SecUnitEvidenceError("raw XBRL fact value is non-finite")
        period_key, start, end, dimension = _context_identity(context)
        concept = str(getattr(fact, "element_id", "")).replace(":", "_")
        unit_ref = getattr(fact, "unit_ref", None)
        decimals = getattr(fact, "decimals", None)
        if not concept or not isinstance(unit_ref, str) or not unit_ref:
            continue
        signature = (
            unit_ref,
            value,
            None if decimals is None else str(decimals),
            period_key,
            start,
            end,
            dimension,
        )
        identity = (concept, context_ref)
        if identity not in conflicts:
            for existing_sig in by_context[identity]:
                if not _are_signatures_compatible(existing_sig, signature):
                    conflicts.add(identity)
                    break
        by_context[identity].add(signature)
        indexed[
            (concept, context_ref, unit_ref, value, None if decimals is None else str(decimals))
        ].append((period_key, start, end, dimension, str(cik).lstrip("0") or "0"))
    return RawInstanceEvidence(
        dict(units),
        {key: tuple(value) for key, value in indexed.items()},
        frozenset(conflicts),
        expected,
    )


def corroborate_rows(evidence: RawInstanceEvidence, rows: list[SecStatementRow]) -> dict[str, str]:
    """Require every retained native fact to match the prepared instance exactly."""
    for row in rows:
        if not row.context_ref or not row.unit_ref or row.value is None:
            raise SecUnitEvidenceError("native row lacks raw XBRL evidence identity")
        if (row.concept.replace(":", "_"), row.context_ref) in evidence.conflicts:
            raise SecUnitEvidenceError("conflicting raw XBRL facts share concept and context")
        key = (
            row.concept.replace(":", "_"),
            row.context_ref,
            row.unit_ref,
            row.value,
            row.decimals_native,
        )
        expected = (
            row.period_key,
            row.period_start.isoformat() if row.period_start else None,
            row.period_end.isoformat() if row.period_end else None,
            row.dimension,
        )
        matches = evidence.facts.get(key, ())
        if not any(
            item[:4] == expected
            and (evidence.expected_cik is None or item[4] == evidence.expected_cik)
            for item in matches
        ):
            raise SecUnitEvidenceError("native statement fact does not match raw XBRL instance")
        if row.unit_ref not in evidence.units:
            raise SecUnitEvidenceError("selected raw XBRL unit reference is missing")
    return evidence.units
