"""Immutable identity for the SEC filing instance used to resolve XBRL units."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

SEC_LIVE_FINANCIAL_PARSER_V2 = "sec-live-financial-parser-v2-edgartools-5.56.0"
SEC_FINANCIAL_UNIT_EVIDENCE_SCHEMA = "sec-financial-unit-evidence-v1"

_CIK_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_BASENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class SecFinancialUnitEvidence:
    """Canonical, raw-byte-free identity of a filing instance document.

    ``cik`` is deliberately required in its zero-stripped decimal form.  Callers
    that receive an SEC-padded CIK must make that normalization explicit before
    constructing this value object.
    """

    parser_version: str
    cik: str
    accession_number: str
    instance_document: str
    instance_sha256: str
    _cached_evidence_identity: str | None = field(
        default=None, repr=False, compare=False, init=False
    )

    def __post_init__(self) -> None:
        if self.parser_version != SEC_LIVE_FINANCIAL_PARSER_V2:
            raise ValueError("unit evidence requires the SEC live financial parser v2")
        if not isinstance(self.cik, str) or not _CIK_RE.fullmatch(self.cik):
            raise ValueError("unit evidence cik must be canonical zero-stripped decimal")
        if not isinstance(self.accession_number, str) or not _ACCESSION_RE.fullmatch(
            self.accession_number
        ):
            raise ValueError(
                "unit evidence accession_number must be canonical dashed SEC accession"
            )
        if (
            not isinstance(self.instance_document, str)
            or not _SAFE_BASENAME_RE.fullmatch(self.instance_document)
            or self.instance_document in {".", ".."}
        ):
            raise ValueError("unit evidence instance_document must be a safe basename")
        if not isinstance(self.instance_sha256, str) or not _DIGEST_RE.fullmatch(
            self.instance_sha256
        ):
            raise ValueError(
                "unit evidence instance_sha256 must be 64 lowercase hexadecimal characters"
            )

    @property
    def canonical_payload(self) -> dict[str, Any]:
        """Return the versioned canonical payload used for the evidence identity."""
        return {
            "parser_version": self.parser_version,
            "cik": self.cik,
            "accession_number": self.accession_number,
            "instance_document": self.instance_document,
            "instance_sha256": self.instance_sha256,
            "schema_version": SEC_FINANCIAL_UNIT_EVIDENCE_SCHEMA,
        }

    @property
    def evidence_identity(self) -> str:
        cached = self._cached_evidence_identity
        if cached is not None:
            return cached
        identity = hashlib.sha256(
            json.dumps(self.canonical_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        object.__setattr__(self, "_cached_evidence_identity", identity)
        return identity
