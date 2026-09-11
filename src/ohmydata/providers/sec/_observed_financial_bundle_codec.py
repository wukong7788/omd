"""Canonical envelope and receipt codecs for observed-financial bundles."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import copy
from datetime import UTC, datetime

from ...core import SnapshotObservationRef
from .observed_financial_replay import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
)
from .observed_xbrl_financials import SecObservedFinancialProduction
from .sgml_financials import SecSgmlFinancialsRequest

_SCHEMA = "sec-observed-financial-bundle-v1"
_TOP = frozenset(
    {
        "schema",
        "batch_identity",
        "captured_at",
        "productions",
        "quality_records",
        "consumer_commits",
    }
)
_RECEIPT = frozenset(
    {
        "observation_identity",
        "snapshot_identity",
        "fact_version",
        "mode",
        "provider",
        "endpoint",
        "request_identity",
        "response_sha256",
        "serialization_identifier",
        "snapshot_fetched_at",
    }
)
_PRODUCTION = frozenset(
    {
        "production_identity",
        "request",
        "produced_at",
        "source_receipt",
        "package_receipt",
        "output_receipt",
    }
)
_REQUEST = frozenset(
    {"symbol", "cik", "accession_number", "form", "statement_types", "include_dimensions"}
)
_QUALITY = frozenset(
    {
        "production_identity",
        "quality_policy_version",
        "status",
        "recorded_at",
        "supersedes_quality_record_id",
        "quality_record_id",
    }
)
_COMMIT = frozenset(
    {
        "production_identity",
        "quality_record_id",
        "consumer_dataset_identity",
        "committed_at",
        "commit_id",
    }
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PRODUCTIONS = 10
_MAX_QUALITY = 10_000
_MAX_COMMITS = 10_000
_MAX_ROWS = 100_000
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024
_MAX_DEPENDENCY_BYTES = 32 * 1024 * 1024
_MAX_PAYLOAD = 8 * 1024 * 1024


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _time(value: object, name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
    if _stamp(parsed) != value:
        raise ValueError(f"invalid {name}")
    return _utc(parsed, name)


def _limit(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return value


def _identity(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def _receipt(observation: SnapshotObservationRef) -> dict[str, str]:
    return {
        "observation_identity": observation.observation_identity,
        "snapshot_identity": observation.snapshot_identity,
        "fact_version": observation.fact_version,
        "mode": observation.mode.value,
        "provider": observation.provider,
        "endpoint": observation.endpoint,
        "request_identity": observation.request_identity,
        "response_sha256": observation.response_sha256,
        "serialization_identifier": observation.serialization_identifier,
        "snapshot_fetched_at": _stamp(observation.snapshot_fetched_at),
    }


def _request(request: SecSgmlFinancialsRequest) -> dict[str, object]:
    return {
        "symbol": request.symbol,
        "cik": request.cik,
        "accession_number": request.accession_number,
        "form": request.form,
        "statement_types": list(request.statement_types),
        "include_dimensions": request.include_dimensions,
    }


def _production(item: SecObservedFinancialProduction) -> dict[str, object]:
    return {
        "production_identity": item.production_identity,
        "request": _request(item.request),
        "produced_at": _stamp(item.produced_at),
        "source_receipt": _receipt(item.evidence.source_observation),
        "package_receipt": _receipt(item.evidence.package_observation),
        "output_receipt": _receipt(item.output_observation),
    }


def _quality(item: SecObservedFinancialQualityRecord) -> dict[str, object]:
    return {
        "production_identity": item.production_identity,
        "quality_policy_version": item.quality_policy_version,
        "status": item.status.value,
        "recorded_at": _stamp(item.recorded_at),
        "supersedes_quality_record_id": item.supersedes_quality_record_id,
        "quality_record_id": item.quality_record_id,
    }


def _commit(item: SecObservedFinancialConsumerCommit) -> dict[str, object]:
    return {
        "production_identity": item.production_identity,
        "quality_record_id": item.quality_record_id,
        "consumer_dataset_identity": item.consumer_dataset_identity,
        "committed_at": _stamp(item.committed_at),
        "commit_id": item.commit_id,
    }


def _canonical(data: Mapping[str, object], maximum: int) -> bytes:
    def strings(value: object) -> None:
        if isinstance(value, str) and (
            len(value) > maximum or len(value.encode("utf-8")) > maximum
        ):
            raise ValueError("SEC observed financial bundle byte limit exceeded")
        if isinstance(value, Mapping):
            for child in value.values():
                strings(child)
        elif isinstance(value, list):
            for child in value:
                strings(child)

    strings(data)
    chunks: list[bytes] = []
    size = 0
    for chunk in json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), allow_nan=False
    ).iterencode(data):
        encoded = chunk.encode("utf-8")
        size += len(encoded)
        if size > maximum:
            raise ValueError("SEC observed financial bundle byte limit exceeded")
        chunks.append(encoded)
    return b"".join(chunks)


def _decode(payload: bytes, maximum: int) -> dict[str, object]:
    if len(payload) > maximum:
        raise ValueError("SEC observed financial bundle byte limit exceeded")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key in SEC observed financial bundle")
            result[key] = value
        return result

    try:
        data = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid SEC observed financial bundle JSON") from exc
    if not isinstance(data, dict) or frozenset(data) != _TOP or data["schema"] != _SCHEMA:
        raise ValueError("invalid SEC observed financial bundle schema")
    if not isinstance(data["batch_identity"], str) or not data["batch_identity"]:
        raise ValueError("invalid SEC observed financial bundle batch identity")
    if not isinstance(data["captured_at"], str) or any(
        not isinstance(data[k], list)
        for k in ("productions", "quality_records", "consumer_commits")
    ):
        raise TypeError("invalid SEC observed financial bundle fields")
    if _canonical(data, maximum) != payload:
        raise ValueError("noncanonical SEC observed financial bundle bytes")
    for field, identity in (
        ("productions", "production_identity"),
        ("quality_records", "quality_record_id"),
        ("consumer_commits", "commit_id"),
    ):
        entries = data[field]
        if not isinstance(entries, list) or any(
            not isinstance(item, dict) or type(item.get(identity)) is not str for item in entries
        ):
            raise ValueError("invalid SEC observed financial bundle receipts")
        identities = [item[identity] for item in entries]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("noncanonical SEC observed financial bundle ordering")
    return data


def validate_existing_production(item: SecObservedFinancialProduction) -> None:
    """Revalidate an untrusted supplied seal without changing the caller object."""
    if type(item) is not SecObservedFinancialProduction:
        raise TypeError("productions must contain SecObservedFinancialProduction values")
    evidence, vintage, production = copy(item.evidence), copy(item.vintage), copy(item)
    evidence.__post_init__()
    vintage.__post_init__()
    if vintage.vintage_identity != item.vintage.vintage_identity:
        raise ValueError("conflicting SEC observed financial vintage_identity")
    object.__setattr__(production, "evidence", evidence)
    object.__setattr__(production, "vintage", vintage)
    production.__post_init__()
    if production.production_identity != item.production_identity:
        raise ValueError("conflicting SEC observed financial production_identity")
