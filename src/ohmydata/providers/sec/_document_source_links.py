"""Bounded, filing-local reference validation for document source closures."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from xml.etree import ElementTree

from .observed_xbrl_package import _validate_components
from .sgml_financials import _validate_instance_identity
from .xbrl_package import SecXbrlPackageComponents

_LINK = "http://www.xbrl.org/2003/linkbase"
_XLINK = "http://www.w3.org/1999/xlink"
_ROLES = {
    f"http://www.xbrl.org/2003/role/{name}LinkbaseRef": role
    for name, role in (
        ("label", "labels"),
        ("presentation", "presentation"),
        ("calculation", "calculation"),
        ("definition", "definition"),
    )
}
_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)


def _basename(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 255
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None
        or ".." in value
    ):
        raise ValueError("invalid SEC document basename or reference")
    return value


class _PrimaryReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict[str, str]]] = []
        self.references: list[str] = []
        self.elements = 0

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], closed: bool) -> None:
        if len(attrs) > 256:
            raise ValueError("SEC primary HTML attribute limit exceeded")
        self.elements += 1
        if self.elements > 200_000 or len(self.stack) >= 128:
            raise ValueError("SEC primary HTML structure limit exceeded")
        values = dict(attrs)
        if len(values) != len(attrs):
            raise ValueError("duplicate SEC primary HTML attribute")
        if "xml:base" in values or (tag == "base" and "href" in values):
            raise ValueError("SEC document base URI overrides are unsupported")
        scope = dict(self.stack[-1][1]) if self.stack else {}
        for key, value in attrs:
            if key in {"xmlns:link", "xmlns:xlink"}:
                if value is None:
                    raise ValueError("invalid SEC primary namespace declaration")
                scope[key] = value
        if tag.rsplit(":", 1)[-1] == "schemaref":
            if (
                tag != "link:schemaref"
                or scope.get("xmlns:link") != _LINK
                or scope.get("xmlns:xlink") != _XLINK
            ):
                raise ValueError("unsupported SEC primary schemaRef namespace")
            self.references.append(_basename(values.get("xlink:href")))
            if len(self.references) > 1:
                raise ValueError("duplicate SEC primary schemaRef")
        if not closed and tag not in _VOID:
            self.stack.append((tag, scope))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, True)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            raise ValueError("invalid closing SEC HTML void element")
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("malformed SEC primary HTML nesting")
        self.stack.pop()


def _validate_links(
    primary: bytes, components: dict[str, bytes], filenames: dict[str, str], cik: str
) -> None:
    parser = _PrimaryReferences()
    parser.feed(primary.decode("utf-8"))
    parser.close()
    if parser.stack or parser.references != [filenames["schema"]]:
        raise ValueError("SEC primary schemaRef does not bind selected schema")
    package = SecXbrlPackageComponents(
        components["schema"],
        components["presentation"],
        components["labels"],
        components["instance"],
        components.get("calculation"),
        components.get("definition"),
    )
    _validate_components(
        package, max_component_bytes=2 * 1024 * 1024, max_elements=200_000, max_depth=128
    )
    for payload in components.values():
        for element in ElementTree.fromstring(payload).iter():
            if "{http://www.w3.org/XML/1998/namespace}base" in element.attrib:
                raise ValueError("SEC document base URI overrides are unsupported")
    instance = ElementTree.fromstring(components["instance"])
    refs = [item for item in instance.iter() if item.tag.rsplit("}", 1)[-1] == "schemaRef"]
    if (
        len(refs) != 1
        or refs[0].tag != f"{{{_LINK}}}schemaRef"
        or _basename(refs[0].get(f"{{{_XLINK}}}href")) != filenames["schema"]
    ):
        raise ValueError("SEC instance schemaRef does not bind selected schema")
    _validate_instance_identity(components["instance"].decode("utf-8"), cik)
    schema = ElementTree.fromstring(components["schema"])
    selected: dict[str, str] = {}
    for item in schema.iter():
        if item.tag.rsplit("}", 1)[-1] != "linkbaseRef":
            continue
        role = _ROLES.get(item.get(f"{{{_XLINK}}}role", ""))
        if item.tag != f"{{{_LINK}}}linkbaseRef" or role is None or role in selected:
            raise ValueError("unsupported or duplicate SEC linkbase role")
        selected[role] = _basename(item.get(f"{{{_XLINK}}}href"))
    expected = {
        role: filename
        for role, filename in filenames.items()
        if role in {"presentation", "labels", "calculation", "definition"}
    }
    if selected != expected:
        raise ValueError("SEC schema linkbase references do not match complete selected roles")
