"""Bounded in-memory system replay for document financial productions."""

from __future__ import annotations

from collections.abc import Iterable
from copy import copy
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import cast

from ...core import SnapshotObservationRef
from .document_financials import SecDocumentFinancialProduction
from .document_source import SecDocumentSourcePackage
from .embedded_document_source import SecEmbeddedDocumentSourcePackage
from .financials import SecStatementRow
from .observed_financial_replay import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    SecObservedFinancialReplayPolicy,
    _bounded,
    _deduplicate_commits,
    _deduplicate_quality,
    _limit,
    _select_validated,
)
from .observed_xbrl_financials import SecObservedFinancialVintage
from .sgml_financials import SecSgmlFinancialsRequest

_MAX_BYTES = 32 * 1024 * 1024
_ALLOWED = (
    SecDocumentFinancialProduction,
    SecDocumentSourcePackage,
    SecEmbeddedDocumentSourcePackage,
    SecObservedFinancialVintage,
    SecStatementRow,
    SecSgmlFinancialsRequest,
    SnapshotObservationRef,
    SecObservedFinancialQualityRecord,
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialReplayPolicy,
)


def _admit(values: tuple[object, ...], maximum: int) -> None:
    # Conservative JSON expansion accounting without copying source manifests or
    # materializing all row dictionaries. Every occurrence, including duplicates,
    # is charged. Exact known dataclasses prevent arbitrary traversal callbacks.
    used = nodes = 0

    def visit(value: object, depth: int = 0) -> None:
        nonlocal used, nodes
        nodes += 1
        if nodes > 500_000 or depth > 16:
            raise ValueError("SEC document replay admission node/depth limit exceeded")
        used += 64
        if type(value) is str:
            used += 12 * len(value)
        elif type(value) is bytes:
            used += 6 * len(value)
        elif type(value) is Decimal:
            # adjusted() is allocation-free; inspect coefficient length only after
            # bounding exponent. Existing parser limits remain independently active.
            if not value.is_finite() or abs(value.adjusted()) > 10_000:
                raise ValueError("SEC document replay decimal admission limit exceeded")
            if value.__sizeof__() > min(8192, maximum - used):
                raise ValueError("SEC document replay byte admission limit exceeded")
            used += 6 * len(value.as_tuple().digits) + 64
        elif isinstance(value, Path):
            for part in value.parts:
                visit(part, depth + 1)
        elif type(value) in _ALLOWED:
            for field in fields(cast(SecDocumentFinancialProduction, value)):
                if field.name != "_capability":
                    visit(getattr(value, field.name), depth + 1)
        elif type(value) is tuple:
            for item in value:
                visit(item, depth + 1)
        elif type(value) is int:
            if value.bit_length() > 64:
                raise ValueError("SEC document replay integer admission limit exceeded")
        elif value is None or type(value) in (bool, date, datetime) or isinstance(value, Enum):
            pass
        else:
            raise TypeError("unsupported SEC document replay admission value")
        if used > maximum:
            raise ValueError("SEC document replay byte admission limit exceeded")

    visit(values)


@dataclass(frozen=True)
class SecDocumentFinancialReplayResult:
    """Complete document production with caller-attested quality and consumer use."""

    production: SecDocumentFinancialProduction
    quality: SecObservedFinancialQualityRecord
    commit: SecObservedFinancialConsumerCommit


def select_sec_document_financial_productions(
    productions: Iterable[SecDocumentFinancialProduction],
    quality_records: Iterable[SecObservedFinancialQualityRecord],
    consumer_commits: Iterable[SecObservedFinancialConsumerCommit],
    policy: SecObservedFinancialReplayPolicy,
    *,
    max_productions: int = 100,
    max_quality_records: int = 10_000,
    max_commits: int = 10_000,
    max_rows: int = 100_000,
    max_admission_bytes: int = _MAX_BYTES,
) -> tuple[SecDocumentFinancialReplayResult, ...]:
    """Select using explicit known-by, quality and commit cutoffs; perform no I/O."""
    limits = (
        _limit(max_productions, "max_productions", 100),
        _limit(max_quality_records, "max_quality_records", 10_000),
        _limit(max_commits, "max_commits", 10_000),
        _limit(max_rows, "max_rows", 100_000),
        _limit(max_admission_bytes, "max_admission_bytes", _MAX_BYTES),
    )
    if type(policy) is not SecObservedFinancialReplayPolicy:
        raise TypeError("policy must be SecObservedFinancialReplayPolicy")
    items = _bounded(productions, "production", limits[0])
    qualities = _bounded(quality_records, "quality record", limits[1])
    commits = _bounded(consumer_commits, "commit", limits[2])
    if any(type(item) is not SecDocumentFinancialProduction for item in items):
        raise TypeError("productions must contain SecDocumentFinancialProduction")
    items = cast(tuple[SecDocumentFinancialProduction, ...], items)
    rows = 0
    for item in items:
        if (
            type(item.vintage) is not SecObservedFinancialVintage
            or type(item.vintage.rows) is not tuple
        ):
            raise TypeError("invalid SEC document replay vintage")
        rows += len(item.vintage.rows)
        if rows > limits[3]:
            raise ValueError("SEC document replay aggregate row limit exceeded")
    _admit((items, qualities, commits, policy), limits[4])
    checked_policy = copy(policy)
    checked_policy.__post_init__()
    unique: dict[str, SecDocumentFinancialProduction] = {}
    for item in items:
        checked = copy(item)
        checked.__post_init__()
        if checked.production_identity != item.production_identity:
            raise ValueError("SEC document replay production identity mismatch")
        prior = unique.get(item.production_identity)
        if prior is not None and prior != item:
            raise ValueError("conflicting SEC document replay production identity")
        unique[item.production_identity] = item
    return tuple(
        SecDocumentFinancialReplayResult(*item)
        for item in _select_validated(
            tuple(unique.values()),
            _deduplicate_quality(qualities),
            _deduplicate_commits(commits),
            checked_policy,
            known_by=lambda item: item.vintage.known_by_at,
            accession=lambda item: item.vintage.accession_number,
        )
    )
