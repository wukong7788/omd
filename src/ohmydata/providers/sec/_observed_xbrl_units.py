"""Bounded raw-XBRL unit decoding for observed-financial parser v2."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from xml.parsers import expat

_XBRLI = "http://www.xbrl.org/2003/instance"
_ISO4217 = "http://www.xbrl.org/2003/iso4217"


def _ncname(value: str) -> bool:
    """XML 1.0 NameStartChar/NameChar, excluding the QName separator colon."""

    def start(char: str) -> bool:
        code = ord(char)
        return char == "_" or any(
            a <= code <= b
            for a, b in (
                (65, 90),
                (97, 122),
                (0xC0, 0xD6),
                (0xD8, 0xF6),
                (0xF8, 0x2FF),
                (0x370, 0x37D),
                (0x37F, 0x1FFF),
                (0x200C, 0x200D),
                (0x2070, 0x218F),
                (0x2C00, 0x2FEF),
                (0x3001, 0xD7FF),
                (0xF900, 0xFDCF),
                (0xFDF0, 0xFFFD),
                (0x10000, 0xEFFFF),
            )
        )

    return (
        bool(value)
        and start(value[0])
        and all(
            start(c)
            or c in "-.0123456789"
            or ord(c) == 0xB7
            or 0x300 <= ord(c) <= 0x36F
            or 0x203F <= ord(c) <= 0x2040
            for c in value[1:]
        )
    )


def _qname(value: str, namespaces: dict[str, str]) -> str:
    value = value.strip()
    if not value or value.count(":") > 1:
        raise ValueError("invalid raw XBRL unit QName")
    prefix, colon, local = value.partition(":")
    if not colon:
        local = prefix
    if not _ncname(local) or (colon and not _ncname(prefix)):
        raise ValueError("invalid raw XBRL unit QName")
    uri = namespaces.get(prefix) if colon else namespaces.get("", "")
    if uri is None or (colon and not uri):
        raise ValueError("unresolved raw XBRL unit QName")
    if uri == _ISO4217:
        return f"iso4217:{local}"
    if uri == _XBRLI and local in {"shares", "pure"}:
        return local
    return f"{{{uri}}}{local}"


@dataclass
class _Node:
    name: str
    attrs: dict[str, str]
    namespaces: dict[str, str]
    text: list[str] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)


def _measures(node: _Node) -> list[str]:
    if (
        "".join(node.text).strip()
        or not node.children
        or any(item.name != f"{_XBRLI}}}measure" or item.children for item in node.children)
    ):
        raise ValueError("invalid raw XBRL unit measures")
    return sorted(_qname("".join(item.text), item.namespaces) for item in node.children)


def _encode(node: _Node) -> str:
    if "".join(node.text).strip():
        raise ValueError("invalid raw XBRL unit structural text")
    if node.children and all(c.name == f"{_XBRLI}}}measure" for c in node.children):
        values = _measures(node)
        if len(values) == 1:
            return values[0]
        value = {"type": "product", "measures": values}
    else:
        if len(node.children) != 1 or node.children[0].name != f"{_XBRLI}}}divide":
            raise ValueError("invalid raw XBRL unit definition")
        divide = node.children[0]
        if "".join(divide.text).strip():
            raise ValueError("invalid raw XBRL divide structural text")
        parts = {c.name: c for c in divide.children}
        numerator, denominator = f"{_XBRLI}}}unitNumerator", f"{_XBRLI}}}unitDenominator"
        if len(divide.children) != 2 or set(parts) != {numerator, denominator}:
            raise ValueError("invalid raw XBRL divide definition")
        value = {
            "type": "divide",
            "numerator": _measures(parts[numerator]),
            "denominator": _measures(parts[denominator]),
        }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def decode_raw_units(instance: bytes, *, max_elements: int, max_depth: int) -> dict[str, str]:
    """Decode every declared XBRL unit without flattening divide descendants."""
    if type(instance) is not bytes or len(instance) > 2 * 1024 * 1024:
        raise ValueError("raw XBRL unit instance exceeds byte limit")
    if (
        type(max_elements) is not int
        or not 0 < max_elements <= 200_000
        or type(max_depth) is not int
        or not 0 < max_depth <= 128
    ):
        raise ValueError("invalid raw XBRL unit XML limits")
    if b"<!DOCTYPE" in instance.upper() or b"<!ENTITY" in instance.upper():
        raise ValueError("unsafe raw XBRL unit XML")
    result: dict[str, str] = {}
    pending: dict[str, str] = {}
    stack: list[_Node] = []
    unit_root: _Node | None = None
    count = 0
    parser = expat.ParserCreate(namespace_separator="}")

    def start_ns(prefix: str | None, uri: str | None) -> None:
        pending[prefix or ""] = uri or ""

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal count, unit_root
        count += 1
        if count > max_elements or len(stack) >= max_depth:
            raise ValueError("raw XBRL unit XML limit exceeded")
        ns = (
            dict(stack[-1].namespaces) if stack else {"xml": "http://www.w3.org/XML/1998/namespace"}
        )
        ns.update(pending)
        pending.clear()
        node = _Node(name, attrs, ns)
        if unit_root is not None:
            if name == f"{_XBRLI}}}unit":
                raise ValueError("nested raw XBRL unit")
            stack[-1].children.append(node)
        if name == f"{_XBRLI}}}unit":
            unit_root = node
        stack.append(node)

    def char(data: str) -> None:
        if unit_root is not None:
            stack[-1].text.append(data)

    def end(name: str) -> None:
        nonlocal unit_root
        node = stack.pop()
        if name == f"{_XBRLI}}}unit":
            identifier = node.attrs.get("id")
            if not identifier or not identifier.strip() or identifier in result:
                raise ValueError("raw XBRL unit IDs must be unique and nonempty")
            result[identifier] = _encode(node)
            unit_root = None

    parser.StartNamespaceDeclHandler, parser.StartElementHandler = start_ns, start
    parser.CharacterDataHandler, parser.EndElementHandler = char, end

    def unsafe(*args: object) -> None:
        raise ValueError("unsafe raw XBRL unit XML declaration")

    parser.StartDoctypeDeclHandler = unsafe
    parser.EntityDeclHandler = unsafe
    try:
        parser.Parse(instance, True)
    except (expat.ExpatError, ValueError) as exc:
        raise ValueError("invalid raw XBRL unit definition") from exc
    if not result:
        raise ValueError("raw XBRL instance has no units")
    return result
