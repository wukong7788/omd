"""Edgar 5.56 statement boundary: native facts first, display frames as compatibility input."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from .financials import SecStatementRow, StatementType

_DISPLAY_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+\((FY|Q[1-4]|YTD)\))?$")
_STRUCTURED_PERIOD = re.compile(
    r"^(?:(instant)_(\d{4}-\d{2}-\d{2})|(duration)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2}))$"
)
_MAX_FACT_ARITHMETIC_DIGITS = 10_000


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


def _fact_precision(fact: Any) -> int | str | None:
    """Return finite precision, None for missing, and ``INF`` for infinity."""
    decimals = fact.decimals
    if decimals is None or type(decimals).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(decimals, str) and decimals.strip() == "INF":
        return "INF"
    if isinstance(decimals, bool):
        raise SecStatementParseError("invalid fact precision")
    if isinstance(decimals, int):
        return decimals
    if isinstance(decimals, str) and re.fullmatch(r"[+-]?\d+", decimals.strip()):
        return int(decimals.strip())
    raise SecStatementParseError("invalid fact precision")


def _fact_interval(value: Decimal, precision: int | str | None) -> tuple[Fraction, Fraction]:
    value_tuple = value.as_tuple()
    if not isinstance(value_tuple.exponent, int):
        raise SecStatementParseError("non-finite statement fact")
    if (
        abs(value_tuple.exponent) > _MAX_FACT_ARITHMETIC_DIGITS
        or len(value_tuple.digits) > _MAX_FACT_ARITHMETIC_DIGITS
    ):
        raise SecStatementParseError("statement fact exceeds arithmetic bounds")
    if isinstance(precision, int) and abs(precision) > _MAX_FACT_ARITHMETIC_DIGITS:
        raise SecStatementParseError("statement fact exceeds arithmetic bounds")
    exact = Fraction(value)
    if precision is None or precision == "INF":
        return exact, exact
    if not isinstance(precision, int):
        raise SecStatementParseError("invalid fact precision")
    half_unit = Fraction(10 ** (-precision), 2) if precision < 0 else Fraction(1, 2 * 10**precision)
    return exact - half_unit, exact + half_unit


def _select_native_facts(facts: list[Any]) -> list[Any]:
    """Validate one duplicate group and select its best fact per context."""
    parsed: list[tuple[Any, Decimal, int | str | None]] = []
    unit_refs = set()
    intervals = []
    missing_precision = False
    values_by_precision: dict[int | str, set[Decimal]] = {}
    best_by_context: dict[str, tuple[tuple[int, int], tuple[str, str], Any]] = {}

    def rank(precision: int | str | None) -> tuple[int, int]:
        if precision is None:
            return (0, 0)
        if precision == "INF":
            return (2, 0)
        if not isinstance(precision, int):
            raise SecStatementParseError("invalid fact precision")
        return (1, precision)

    for fact in facts:
        value = _number(fact.value)
        precision = _fact_precision(fact)
        missing_precision |= precision is None
        unit_refs.add(fact.unit_ref)
        if value is None:
            continue
        parsed.append((fact, value, precision))
        intervals.append(_fact_interval(value, precision))
        if precision is not None:
            values_by_precision.setdefault(precision, set()).add(value)
        context = fact.context_ref
        tie_break = (str(fact.value), str(fact.decimals))
        candidate = (rank(precision), tie_break, fact)
        current = best_by_context.get(context)
        if (
            current is None
            or candidate[0] > current[0]
            or (candidate[0] == current[0] and candidate[1] < current[1])
        ):
            best_by_context[context] = candidate
    if len(unit_refs) != 1:
        raise SecStatementParseError("conflicting native facts for period and dimensions")
    if not parsed:
        return []
    if len(parsed) != len(facts):
        raise SecStatementParseError("non-numeric statement fact")
    # Every interval must share one common point; pairwise or adjacent checks
    # can incorrectly accept a chain of individually overlapping intervals.
    lower = max(interval[0] for interval in intervals)
    upper = min(interval[1] for interval in intervals)
    if missing_precision and any(precision is not None for _, _, precision in parsed):
        raise SecStatementParseError("conflicting native facts for period and dimensions")
    if missing_precision and len({value for _, value, _ in parsed}) != 1:
        raise SecStatementParseError("conflicting native facts for period and dimensions")
    for values in values_by_precision.values():
        if len(values) != 1:
            raise SecStatementParseError("conflicting native facts for period and dimensions")
    if lower > upper:
        raise SecStatementParseError("conflicting native facts for period and dimensions")
    selected: list[Any] = []
    for context_ref in sorted(best_by_context):
        selected.append(best_by_context[context_ref][2])
    return selected


def _native_index(xbrl: Any, concepts: set[str]) -> dict[tuple[str, str, str], list[Any]]:
    """One pass over facts, avoiding a full source scan for each row or period."""
    # Edgar 5.56 XBRL.facts is FactsView, an enriched query interface whose
    # get_facts() may rewrite concept identifiers. The raw Fact objects are
    # owned by XBRLParser.facts (also exposed upstream as XBRL._facts).
    # Keep this version-specific boundary explicit: never use display values
    # or the enriched view as a fallback for unavailable native facts.
    try:
        native_facts = xbrl.parser.facts
    except AttributeError as exc:
        raise SecStatementParseError("native parser fact mapping unavailable") from exc
    if not isinstance(native_facts, Mapping):
        raise SecStatementParseError("native parser facts must be a mapping")
    result: dict[tuple[str, str, str], list[Any]] = {}
    seen: set[int] = set()
    for fact in native_facts.values():
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
            if index is not None:
                facts = index.get((concept.replace(":", "_"), key, _dim_key(dims)), [])
                if not facts:
                    raise SecStatementParseError("statement value has no native fact context")
                selected_facts = _select_native_facts(facts)
            else:
                selected_facts = [None]
            for selected_fact in selected_facts:
                selected_raw = raw
                selected_unit_ref = unit_ref
                selected_decimals = native_decimals
                if selected_fact is not None:
                    selected_raw = selected_fact.value
                    selected_unit_ref = selected_fact.unit_ref
                    selected_decimals = selected_fact.decimals
                    context_ref = selected_fact.context_ref
                value = _number(selected_raw)
                if value is None:
                    continue
                unit = _text(selected_unit_ref) or _text(item.get("unit"))
                if xbrl is not None and selected_unit_ref:
                    definition = xbrl.units.get(selected_unit_ref)
                    if definition is None:
                        unit = None
                    elif isinstance(definition, dict):
                        unit = _text(definition.get("measure")) or json.dumps(
                            definition, sort_keys=True
                        )
                    else:
                        raise SecStatementParseError("invalid native unit definition")
                decimals = None
                if selected_decimals is not None and str(selected_decimals).strip() != "INF":
                    try:
                        decimals = int(str(selected_decimals).strip())
                    except (TypeError, ValueError) as exc:
                        raise SecStatementParseError("invalid fact precision") from exc
                row = SecStatementRow(
                    statement_type=kind,
                    standard_concept=_text(item.get("standard_concept")) or concept,
                    concept=concept,
                    label=_text(item.get("label")) or "",
                    value=value,
                    value_native=str(selected_raw),
                    unit=unit,
                    unit_ref=_text(selected_unit_ref),
                    decimals=decimals,
                    decimals_native=_text(selected_decimals),
                    period_start=start,
                    period_end=end,
                    period_type=period_type,
                    period_key=str(key),
                    context_ref=context_ref,
                    dimension=_dim_key(dims) if dims else None,
                    is_point_in_time=period_type == "instant",
                    period_source="xbrl-context" if index is not None else "structured-period-key",
                )
                identity = concept, str(key), _dim_key(dims), context_ref
                if identity in seen:
                    previous = seen[identity]
                    if (previous.value, previous.unit) != (row.value, row.unit):
                        raise SecStatementParseError("conflicting duplicate statement facts")
                    continue
                seen[identity] = row
                rows.append(row)
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
