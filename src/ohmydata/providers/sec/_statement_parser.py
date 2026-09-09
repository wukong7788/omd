"""Edgar 5.56 statement boundary: native facts first, display frames as compatibility input."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .financials import SecStatementRow, StatementType

_DISPLAY_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+\((FY|Q[1-4]|YTD)\))?$")
_STRUCTURED_PERIOD = re.compile(
    r"^(?:(instant)_(\d{4}-\d{2}-\d{2})|(duration)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2}))$"
)


class SecStatementParseError(ValueError):
    """A supplied financial statement could not be interpreted without losing identity."""


def _missing(value: Any) -> bool:
    return (
        value is None
        or type(value).__name__ in {"NAType", "NaTType"}
        or str(value).strip().lower() in {"", "nan", "none", "null", "<na>", "nat"}
    )


def _flag(value: Any) -> bool:
    if _missing(value):
        return False
    if type(value).__name__ in {"bool", "bool_"}:
        return bool(value)
    raise SecStatementParseError("invalid statement boolean metadata")


def _text(value: Any) -> str | None:
    return None if _missing(value) else str(value).strip()


def _number(value: Any) -> Decimal | None:
    if isinstance(value, Decimal) and not value.is_finite():
        raise SecStatementParseError("non-finite statement fact")
    if type(value).__name__ in {"bool", "bool_"} or _missing(value):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SecStatementParseError("non-numeric statement fact") from exc
    if not result.is_finite():
        raise SecStatementParseError("non-finite statement fact")
    return result


def _period(key: str) -> tuple[str, date | None, date]:
    match = _STRUCTURED_PERIOD.fullmatch(key)
    if not match:
        raise SecStatementParseError("unrecognized structured period key")
    try:
        if match.group(1):
            return "instant", None, date.fromisoformat(match.group(2))
        start, end = date.fromisoformat(match.group(4)), date.fromisoformat(match.group(5))
        if start > end:
            raise ValueError("reversed period")
        return "duration", start, end
    except ValueError as exc:
        raise SecStatementParseError("invalid structured period dates") from exc


def _dimensions(item: Mapping[str, Any]) -> dict[str, str]:
    metadata = item.get("dimension_metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, list):
        raise SecStatementParseError("invalid dimension metadata")
    result = {}
    for dim in metadata:
        if not isinstance(dim, dict) or not dim.get("dimension") or not dim.get("member"):
            raise SecStatementParseError("dimension identity unavailable")
        axis, member = str(dim["dimension"]), str(dim["member"])
        if axis in result and result[axis] != member:
            raise SecStatementParseError("conflicting dimension identity")
        result[axis] = member
    return result


def _dim_key(dimensions: Mapping[str, Any]) -> str:
    return json.dumps(dict(dimensions), sort_keys=True, separators=(",", ":"))


def _native_index(xbrl: Any, concepts: set[str]) -> dict[tuple[str, str, str], list[Any]]:
    """One pass over facts, avoiding a full source scan for each row or period."""
    result: dict[tuple[str, str, str], list[Any]] = {}
    seen: set[int] = set()
    for fact in xbrl.facts.values():
        if id(fact) in seen:
            continue
        seen.add(id(fact))
        concept = fact.element_id.replace(":", "_")
        if concept not in concepts:
            continue
        context = xbrl.contexts.get(fact.context_ref)
        period = xbrl.context_period_map.get(fact.context_ref)
        if context is None or period is None:
            raise SecStatementParseError("native fact context unavailable")
        key = concept, period, _dim_key(context.dimensions)
        result.setdefault(key, []).append(fact)
    return result


def _structured_rows(
    data: list[Any],
    kind: StatementType,
    dimensions: bool,
    xbrl: Any = None,
    native_index: dict[tuple[str, str, str], list[Any]] | None = None,
) -> list[SecStatementRow]:
    if any(not isinstance(item, dict) for item in data):
        raise SecStatementParseError("invalid structured statement rows")
    concepts = {str(item.get("concept", "")).replace(":", "_") for item in data}
    index = (
        native_index
        if native_index is not None
        else _native_index(xbrl, concepts)
        if xbrl is not None
        else None
    )
    rows = []
    seen: dict[tuple[str, str, str, str | None], SecStatementRow] = {}
    for item in data:
        if _flag(item.get("is_abstract", item.get("abstract"))):
            continue
        dims = _dimensions(item)
        dimensional = _flag(item.get("is_dimension", item.get("dimension"))) or bool(dims)
        if dimensional and not dimensions:
            continue
        if dimensional and not dims:
            raise SecStatementParseError("dimension identity unavailable")
        concept = _text(item.get("concept"))
        if not concept:
            raise SecStatementParseError("statement concept unavailable")
        values = item.get("values", {})
        if not isinstance(values, dict):
            raise SecStatementParseError("invalid structured values")
        for key, raw in values.items():
            period_type, start, end = _period(str(key))
            declared_type = (item.get("period_types") or {}).get(key)
            if declared_type is not None and declared_type != period_type:
                raise SecStatementParseError("conflicting period type")
            if index is None and _number(raw) is None:
                continue
            unit_ref = (item.get("units") or {}).get(key)
            native_decimals = (item.get("decimals") or {}).get(key)
            context_ref = None
            context_refs: list[str | None] = [None]
            if index is not None:
                facts = index.get((concept.replace(":", "_"), key, _dim_key(dims)), [])
                if not facts:
                    raise SecStatementParseError("statement value has no native fact context")
                identities = {(f.value, f.unit_ref, str(f.decimals)) for f in facts}
                if len(identities) != 1:
                    raise SecStatementParseError(
                        "conflicting native facts for period and dimensions"
                    )
                fact = min(facts, key=lambda f: f.context_ref)
                raw, unit_ref, native_decimals = fact.value, fact.unit_ref, fact.decimals
                context_ref = fact.context_ref
                context_refs = sorted({f.context_ref for f in facts})
            value = _number(raw)
            if value is None:
                continue
            unit = _text(unit_ref) or _text(item.get("unit"))
            if xbrl is not None and unit_ref:
                definition = xbrl.units.get(unit_ref)
                if definition is None:
                    unit = None
                elif isinstance(definition, dict):
                    unit = _text(definition.get("measure")) or json.dumps(
                        definition, sort_keys=True
                    )
                else:
                    raise SecStatementParseError("invalid native unit definition")
            decimals = None
            if native_decimals is not None and str(native_decimals) != "INF":
                try:
                    decimals = int(native_decimals)
                except (TypeError, ValueError) as exc:
                    raise SecStatementParseError("invalid fact precision") from exc
            row = SecStatementRow(
                statement_type=kind,
                standard_concept=_text(item.get("standard_concept")) or concept,
                concept=concept,
                label=_text(item.get("label")) or "",
                value=value,
                value_native=str(raw),
                unit=unit,
                unit_ref=_text(unit_ref),
                decimals=decimals,
                decimals_native=_text(native_decimals),
                period_start=start,
                period_end=end,
                period_type=period_type,
                period_key=str(key),
                context_ref=context_ref,
                dimension=_dim_key(dims) if dims else None,
                is_point_in_time=period_type == "instant",
                period_source="xbrl-context" if index is not None else "structured-period-key",
            )
            for reference in context_refs:
                contextual_row = replace(row, context_ref=reference)
                identity = concept, str(key), _dim_key(dims), reference
                if identity in seen:
                    previous = seen[identity]
                    if (previous.value, previous.unit) != (row.value, row.unit):
                        raise SecStatementParseError("conflicting duplicate statement facts")
                    continue
                seen[identity] = contextual_row
                rows.append(contextual_row)
    return rows


def _native_statement_rows(
    statement: Any, kind: StatementType, dimensions: bool
) -> list[SecStatementRow]:
    """Use presentation membership plus instance facts, bypassing display deduplication."""
    xbrl = statement.xbrl
    _, role, _ = xbrl.find_statement(statement.canonical_type or statement.role_or_type)
    tree = xbrl.presentation_trees.get(role)
    if tree is None:
        raise SecStatementParseError("statement presentation tree unavailable")
    nodes = {
        node.element_id.replace(":", "_"): node
        for node in tree.all_nodes.values()
        if not node.is_abstract
    }
    index = _native_index(xbrl, set(nodes))
    data = []
    for (concept, key, dimension_key), facts in sorted(index.items()):
        dims = json.loads(dimension_key)
        if dims and not dimensions:
            continue
        node = nodes[concept]
        data.append(
            {
                "concept": node.element_id,
                "label": node.display_label,
                "is_dimension": bool(dims),
                "dimension_metadata": [
                    {"dimension": axis, "member": member} for axis, member in dims.items()
                ],
                "values": {key: facts[0].value},
            }
        )
    return _structured_rows(data, kind, dimensions, xbrl, index)


def _display_rows(statement: Any, kind: StatementType, dimensions: bool) -> list[SecStatementRow]:
    try:
        frame = statement.to_dataframe(
            standard=False,
            include_unit=True,
            include_point_in_time=True,
            include_standardization=True,
            presentation=False,
        )
    except Exception as exc:
        raise SecStatementParseError("failed to parse statement dataframe") from exc
    if frame is None:
        raise SecStatementParseError("statement dataframe unavailable")
    if frame.empty:
        return []
    metadata_columns = {
        "concept",
        "label",
        "standard_concept",
        "unit",
        "abstract",
        "dimension",
        "point_in_time",
        "dimension_axis",
        "dimension_member",
        "dimension_member_label",
        "dimension_label",
        "level",
        "balance",
        "weight",
        "preferred_sign",
        "is_breakdown",
        "parent_concept",
        "parent_abstract_concept",
        "original_label",
        "is_standardized",
    }
    for col in frame.columns:
        if col not in metadata_columns and not _DISPLAY_PERIOD.fullmatch(str(col)):
            raise SecStatementParseError("unrecognized financial period column")
    period_cols = [(col, _DISPLAY_PERIOD.fullmatch(str(col))) for col in frame.columns]
    period_cols = [(col, match) for col, match in period_cols if match is not None]
    if not period_cols:
        raise SecStatementParseError("no recognized financial period columns")
    if frame.columns.has_duplicates:
        raise SecStatementParseError("duplicate statement columns")
    rows = []
    for _, item in frame.iterrows():
        if _flag(item.get("abstract")):
            continue
        dimensional = _flag(item.get("dimension"))
        if dimensional and not dimensions:
            continue
        dim = None
        if dimensional:
            axis, member = _text(item.get("dimension_axis")), _text(item.get("dimension_member"))
            if not axis or not member:
                raise SecStatementParseError("dimension identity unavailable in display input")
            dim = _dim_key({axis: member})
        concept = _text(item.get("concept"))
        if not concept:
            raise SecStatementParseError("statement concept unavailable")
        for col, match in period_cols:
            raw = item[col]
            value = _number(raw)
            if value is None:
                continue
            try:
                end = date.fromisoformat(match.group(1))
            except ValueError as exc:
                raise SecStatementParseError("invalid display period date") from exc
            suffix = match.group(2)
            instant = _flag(item.get("point_in_time")) or kind == "balance_sheet"
            if instant and suffix:
                raise SecStatementParseError("conflicting display period type")
            rows.append(
                SecStatementRow(
                    statement_type=kind,
                    standard_concept=_text(item.get("standard_concept")) or concept,
                    concept=concept,
                    label=_text(item.get("label")) or "",
                    value=value,
                    value_native=str(raw),
                    unit=_text(item.get("unit")),
                    period_end=end,
                    period_type="instant" if instant else "duration",
                    period_key=str(col),
                    dimension=dim,
                    is_point_in_time=instant,
                    period_source="display-column-unknown-start",
                )
            )
    return rows


def parse_statement_rows(
    statement: Any, statement_type: StatementType, *, include_dimensions: bool = False
) -> list[SecStatementRow]:
    """Preserve native fact identity; never infer duration starts from display suffixes."""
    if statement is None:
        return []
    from edgar.xbrl.statements import Statement

    try:
        if isinstance(statement, Statement):
            return _native_statement_rows(statement, statement_type, include_dimensions)
        getter = getattr(statement, "get_raw_data", None)
        raw = getter() if callable(getter) else None
        if isinstance(raw, list):
            return _structured_rows(raw, statement_type, include_dimensions)
        return _display_rows(statement, statement_type, include_dimensions)
    except SecStatementParseError:
        raise
    except Exception as exc:
        raise SecStatementParseError("invalid financial statement structure") from exc
