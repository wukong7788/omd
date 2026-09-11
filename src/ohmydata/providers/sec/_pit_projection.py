"""Strict decoding for SEC typed-row projection snapshots."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .financials import SecStatementRow

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = "sec-financial-typed-rows-projection-v1"


def _decode_projection(payload: bytes) -> dict[str, Any]:
    def reject_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid SEC typed-row projection") from exc
    required = {
        "schema",
        "accession_number",
        "source_artifact_identity",
        "source_available_at",
        "vintage_identity",
        "rows",
    }
    if not isinstance(decoded, dict) or set(decoded) != required:
        raise ValueError("invalid SEC typed-row projection fields")
    if decoded["schema"] != _SCHEMA or not isinstance(decoded["accession_number"], str):
        raise ValueError("invalid SEC typed-row projection identity")
    if any(
        not isinstance(decoded[name], str) or _HEX64.fullmatch(decoded[name]) is None
        for name in ("source_artifact_identity", "vintage_identity")
    ):
        raise ValueError("invalid SEC typed-row projection identity")
    _projection_datetime(decoded["source_available_at"], "availability")
    if not isinstance(decoded["rows"], list):
        raise TypeError("invalid SEC typed-row projection rows")
    return decoded


def _projection_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TypeError(f"invalid SEC typed-row projection {name}")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"invalid SEC typed-row projection {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"invalid SEC typed-row projection {name}")
    return parsed.astimezone(UTC)


def _row_from_payload(payload: object) -> SecStatementRow:
    """Decode the canonical row shape emitted by the typed-row projection."""
    fields = {
        "statement_type",
        "standard_concept",
        "concept",
        "label",
        "value",
        "value_native",
        "unit",
        "currency",
        "decimals",
        "period_start",
        "period_end",
        "period_type",
        "dimension",
        "period_key",
        "context_ref",
        "unit_ref",
        "decimals_native",
        "period_source",
        "is_point_in_time",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise ValueError("invalid SEC typed-row projection row")

    def parsed_date(value: object) -> date | None:
        if value is None:
            return None
        if (
            not isinstance(value, Mapping)
            or set(value) != {"date"}
            or not isinstance(value["date"], str)
        ):
            raise ValueError("invalid SEC typed-row projection date")
        try:
            return date.fromisoformat(value["date"])
        except ValueError as exc:
            raise ValueError("invalid SEC typed-row projection date") from exc

    raw = payload["value"]
    if raw is None:
        decimal = None
    elif isinstance(raw, Mapping) and set(raw) == {"decimal"} and isinstance(raw["decimal"], str):
        try:
            decimal = Decimal(raw["decimal"])
        except Exception as exc:
            raise ValueError("invalid SEC typed-row projection decimal") from exc
        if not decimal.is_finite():
            raise ValueError("invalid SEC typed-row projection decimal")
    else:
        raise ValueError("invalid SEC typed-row projection decimal")
    if any(
        not isinstance(payload[name], str)
        for name in ("statement_type", "standard_concept", "concept", "label")
    ):
        raise ValueError("invalid SEC typed-row projection row")
    nullable = (
        "value_native",
        "unit",
        "period_type",
        "dimension",
        "period_key",
        "context_ref",
        "unit_ref",
        "decimals_native",
        "period_source",
        "currency",
    )
    if any(payload[name] is not None and not isinstance(payload[name], str) for name in nullable):
        raise ValueError("invalid SEC typed-row projection row")
    if type(payload["decimals"]) is not int and payload["decimals"] is not None:
        raise TypeError("invalid SEC typed-row projection decimals")
    if type(payload["is_point_in_time"]) is not bool:
        raise TypeError("invalid SEC typed-row projection point-in-time flag")
    row = SecStatementRow(
        payload["statement_type"],
        payload["standard_concept"],
        payload["concept"],
        payload["label"],
        decimal,
        payload["value_native"],
        payload["unit"],
        payload["decimals"],
        parsed_date(payload["period_start"]),
        parsed_date(payload["period_end"]),
        period_type=payload["period_type"],
        dimension=payload["dimension"],
        period_key=payload["period_key"],
        context_ref=payload["context_ref"],
        unit_ref=payload["unit_ref"],
        decimals_native=payload["decimals_native"],
        period_source=payload["period_source"],
        is_point_in_time=payload["is_point_in_time"],
    )
    if payload["currency"] != row.currency:
        raise ValueError("invalid SEC typed-row projection currency")
    return row
