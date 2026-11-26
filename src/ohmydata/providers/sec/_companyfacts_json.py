"""Fast, bounded, DoS-hardened JSON parsing for SEC companyfacts payloads."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from .errors import ResourceLimitError, SchemaMismatchError

_JSON_STRUCT_TOKEN_RE = re.compile(rb'"[^"\\]*+(?:\\.[^"\\]*+)*+"|[{}\[\],"]', re.DOTALL)
_JSON_CONTAINERS = frozenset({dict, list})


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise SchemaMismatchError("duplicate key in SEC companyfacts JSON")
    return result


def _reject_json_constant(token: str) -> Any:
    raise ValueError(token)


def _scan_json_structure_exact(body: bytes, *, max_depth: int, max_nodes: int) -> None:
    depth = nodes = 0
    for match in _JSON_STRUCT_TOKEN_RE.finditer(body):
        token = match.group(0)
        first = token[0]
        if first == 34:
            if len(token) == 1:
                raise SchemaMismatchError("malformed SEC companyfacts JSON")
            continue
        if first in (123, 91):
            depth += 1
            nodes += 1
        elif first in (125, 93):
            depth -= 1
            if depth < 0:
                raise SchemaMismatchError("malformed SEC companyfacts JSON")
            continue
        else:
            nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise ResourceLimitError("SEC companyfacts JSON structure limit exceeded")
    if depth != 0:
        raise SchemaMismatchError("malformed SEC companyfacts JSON")


def _check_json_depth(root: Any, max_depth: int) -> None:
    containers = _JSON_CONTAINERS
    if type(root) not in containers:
        return
    _dict = dict

    def _walk(node: Any, rem: int) -> None:
        if rem < 0:
            raise ResourceLimitError("SEC companyfacts JSON structure limit exceeded")
        nxt = rem - 1
        vals = node.values() if type(node) is _dict else node
        for item in vals:
            if type(item) in containers:
                _walk(item, nxt)

    _walk(root, max_depth - 1)


def strict_companyfacts_json(
    body: bytes,
    *,
    max_body_bytes: int,
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    """Decode and structurally validate a full SEC companyfacts JSON object."""
    if type(body) is not bytes or len(body) > max_body_bytes:
        raise ResourceLimitError("SEC companyfacts response exceeds byte limit")
    if (
        len(body) >= (3 * max_nodes) // 2
        and body.count(b"{") + body.count(b"[") + body.count(b",") > max_nodes
    ):
        _scan_json_structure_exact(body, max_depth=max_depth, max_nodes=max_nodes)
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        _scan_json_structure_exact(body, max_depth=max_depth, max_nodes=max_nodes)
        raise SchemaMismatchError("invalid SEC companyfacts JSON") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, SchemaMismatchError):
            raise
        raise SchemaMismatchError("invalid SEC companyfacts JSON") from exc
    _check_json_depth(value, max_depth)
    if type(value) is not dict:
        raise SchemaMismatchError("SEC companyfacts payload must be an object")
    return value


__all__ = ["strict_companyfacts_json"]
