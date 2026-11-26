"""Bounded SEC filing XBRL instance attachment discovery and retrieval."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._live_unit_corroboration import _RAW_INSTANCE_MAX_BYTES
from ._statement_parser import SecUnitEvidenceError
from .http import SecHttpClient, validate_sec_url

_SAFE_DOCUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _filing_archive_prefix(filing: Any) -> str:
    """Return the one SEC archive directory that may supply this filing's instance."""
    try:
        cik = str(int(str(filing.cik)))
        accession = str(filing.accession_number)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SecUnitEvidenceError("filing identity is unavailable for raw XBRL evidence") from exc
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise SecUnitEvidenceError("filing accession is invalid for raw XBRL evidence")
    return f"https://www.sec.gov:443/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"


def _attachment_document(item: Any) -> str:
    document = getattr(item, "document", None)
    if not isinstance(document, str) or not _SAFE_DOCUMENT.fullmatch(document):
        raise SecUnitEvidenceError("classified XBRL instance has an unsafe document name")
    return document


def _attachment_sources(container: Any) -> list[Any]:
    """Return the pinned classifier's source set without inspecting names or content."""
    if container is None:
        return []
    data_files = getattr(container, "data_files", None)
    if data_files is None:
        data_files = getattr(container, "datafiles", None)
    if data_files is None:
        return []
    if not isinstance(data_files, list):
        raise SecUnitEvidenceError("filing XBRL attachment source list is invalid")
    return data_files


def _classifier_eligible(attachment: Any) -> bool:
    """Mirror the pinned classifier's pre-content filter exactly."""
    document_type = getattr(attachment, "document_type", None)
    extension = getattr(attachment, "extension", None)
    return (
        document_type in {"XML", "EX-101.INS"}
        and isinstance(extension, str)
        and extension.endswith((".xml", ".XML"))
    )


def _materialize_attachment(
    filing: Any, attachment: Any, client: SecHttpClient | None
) -> tuple[str, bytes, str]:
    document = _attachment_document(attachment)
    supplied_url = getattr(attachment, "url", None)
    if supplied_url is not None and not isinstance(supplied_url, str):
        raise SecUnitEvidenceError("classified XBRL instance URL is invalid")
    url, validator = _instance_url(filing, document, supplied_url)
    sgml = getattr(attachment, "sgml_document", None)
    content = getattr(sgml, "content", None) if sgml is not None else None
    if isinstance(content, str):
        content = content.encode("utf-8")
    if content is not None and type(content) is not bytes:
        raise SecUnitEvidenceError("classified XBRL instance content is not bytes")
    if content is None:
        if client is None:
            raise SecUnitEvidenceError("v2 raw XBRL evidence requires an injected SecHttpClient")
        content = _read_instance(client, url, validator)
    if len(content) > _RAW_INSTANCE_MAX_BYTES:
        raise SecUnitEvidenceError("raw XBRL instance exceeds byte limit")
    return document, content, url


def _classify_instance(document: str, attachment: Any, raw: bytes) -> bool:
    """Run edgartools' exact classifier over an in-memory one-document proxy."""
    try:
        from edgar.xbrl.xbrl import XBRLAttachments
    except ImportError as exc:
        raise SecUnitEvidenceError("pinned edgartools XBRL classifier is unavailable") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    proxy = type(
        "_BoundedAttachment",
        (),
        {
            "document": document,
            "document_type": getattr(attachment, "document_type", None),
            "extension": getattr(attachment, "extension", Path(document).suffix),
            "content": content,
        },
    )()
    attachments: Any = type("_OneAttachment", (), {"data_files": [proxy]})()
    return XBRLAttachments(attachments).get("instance") is not None


def _try_instance_attachment(
    filing: Any, client: SecHttpClient | None
) -> tuple[str, bytes, str] | None:
    """Return the unique classified XBRL instance or None when no XBRL instance exists."""
    original = _attachment_sources(getattr(filing, "attachments", None))
    if not original:
        original = _attachment_sources(getattr(filing, "xbrl_attachments", None))
    sources = original
    homepage_used = False
    while True:
        found: dict[tuple[str, str], bytes] = {}
        for attachment in sources:
            if not _classifier_eligible(attachment):
                continue
            document, raw, url = _materialize_attachment(filing, attachment, client)
            if _classify_instance(document, attachment, raw):
                key = (document, url)
                existing = found.get(key)
                if existing is not None and existing != raw:
                    raise SecUnitEvidenceError(
                        "classified XBRL instance source has conflicting duplicate bytes"
                    )
                found[key] = raw
        if found or homepage_used:
            break
        homepage_used = True
        homepage = getattr(filing, "homepage", None)
        sources = _attachment_sources(getattr(homepage, "attachments", homepage))
    if not found:
        return None
    if len(found) != 1:
        raise SecUnitEvidenceError("filing must have exactly one classified original XBRL instance")
    (document, url), raw = next(iter(found.items()))
    return document, raw, url


def _instance_attachment(filing: Any, client: SecHttpClient | None) -> tuple[str, bytes, str]:
    """Classify every original candidate separately, then one same-filing homepage set."""
    result = _try_instance_attachment(filing, client)
    if result is None:
        raise SecUnitEvidenceError("filing must have exactly one classified original XBRL instance")
    return result


def _has_xbrl_attachment_candidates(filing: Any) -> bool:
    """Return True if a filing advertises any eligible XML/EX-101.INS attachment."""
    for container in (
        getattr(filing, "attachments", None),
        getattr(filing, "xbrl_attachments", None),
    ):
        for attachment in _attachment_sources(container):
            if _classifier_eligible(attachment):
                return True
    return False


def _instance_url(
    filing: Any, document: str, supplied_url: str | None = None
) -> tuple[str, Callable[[str], str]]:
    prefix = _filing_archive_prefix(filing)
    url = prefix + document

    def _same_directory(candidate: str) -> str:
        normalized = validate_sec_url(candidate)
        if not normalized.startswith(prefix):
            raise SecUnitEvidenceError("XBRL instance URL does not match filing archive directory")
        tail = normalized.removeprefix(prefix)
        if not _SAFE_DOCUMENT.fullmatch(tail):
            raise SecUnitEvidenceError("XBRL instance URL is not a safe filing attachment")
        return normalized

    selected = _same_directory(url if supplied_url is None else supplied_url)
    if not selected.endswith("/" + document):
        raise SecUnitEvidenceError("XBRL instance URL does not match classified attachment")
    return selected, _same_directory


def _read_instance(client: SecHttpClient, url: str, validator: Callable[[str], str]) -> bytes:
    response = client.open(
        url,
        accept="application/xml, text/xml, application/octet-stream",
        max_bytes=_RAW_INSTANCE_MAX_BYTES,
        redirect_validator=validator,
    )
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.body.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _RAW_INSTANCE_MAX_BYTES:
                raise SecUnitEvidenceError("raw XBRL instance exceeds byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.body.close()


def _load_user_agent_from_config(config_path: str | Path) -> str:
    """Read SEC User-Agent from a YAML, JSON, or TOML config file."""
    import importlib

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    raw: dict[str, Any]
    if path.suffix in (".yaml", ".yml"):
        try:
            yaml: Any = importlib.import_module("yaml")
            raw = yaml.safe_load(text) or {}
        except ImportError:
            import json

            raw = json.loads(text)
    elif path.suffix == ".toml":
        import tomllib

        raw = tomllib.loads(text)
    else:
        import json

        raw = json.loads(text)

    user_agent = raw.get("user_agent")
    if not user_agent and raw.get("user_agent_file"):
        ua_file = Path(raw["user_agent_file"])
        if not ua_file.is_absolute():
            ua_file = path.parent / ua_file
        if ua_file.exists():
            user_agent = ua_file.read_text(encoding="utf-8").strip()

    if not user_agent:
        raise ValueError(
            "config must specify either 'user_agent' or 'user_agent_file' pointing to contact info"
        )
    return str(user_agent)


__all__ = [
    "_has_xbrl_attachment_candidates",
    "_instance_attachment",
    "_instance_url",
    "_load_user_agent_from_config",
    "_read_instance",
    "_try_instance_attachment",
]
