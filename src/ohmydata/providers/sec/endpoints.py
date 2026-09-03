from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from ...core import RequestSpec


class SecEmptyPolicy(str, Enum):
    REQUIRE_ROWS = "REQUIRE_ROWS"
    ALLOW_EMPTY = "ALLOW_EMPTY"


def _series(xs: tuple[str, ...], name: str) -> tuple[str, ...]:
    vals = tuple(xs)
    if not vals or any(not x.strip() for x in vals):
        raise ValueError(f"{name} must be non-empty")
    if len(set(vals)) != len(vals):
        raise ValueError(f"{name} must be unique")
    return tuple(sorted(vals))


@dataclass(frozen=True)
class SecNportQuarterRequest:
    year: int
    quarter: int
    series_ids: tuple[str, ...]
    required_series_ids: tuple[str, ...] = ()
    empty_policy: SecEmptyPolicy = SecEmptyPolicy.REQUIRE_ROWS
    single_series_ciks: tuple[str, ...] = ()
    selected_pairs: tuple[tuple[str, str], ...] = ()
    required_pairs: tuple[tuple[str, str], ...] = ()
    utc_now: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC), repr=False, compare=False
    )
    endpoint: str = field(init=False, default="nport_quarter")

    def __post_init__(self) -> None:
        if (
            type(self.year) is not int
            or not 2019 <= self.year <= 2100
            or type(self.quarter) is not int
            or self.quarter not in range(1, 5)
        ):
            raise ValueError("invalid quarter")
        now = self.utc_now()
        if now.tzinfo is None:
            raise ValueError("utc_now must return aware datetime")
        if (self.year, self.quarter) > (now.year, (now.month - 1) // 3 + 1):
            raise ValueError("quarter is not yet current")
        ids = tuple(self.series_ids)
        singles = tuple(self.single_series_ciks)
        if not ids and not singles:
            raise ValueError("at least one selector is required")
        if ids:
            ids = _series(ids, "series_ids")
        required = (
            _series(self.required_series_ids, "required_series_ids")
            if self.required_series_ids
            else ()
        )
        if any(not re.fullmatch(r"\d{10}", x) for x in singles) or len(set(singles)) != len(
            singles
        ):
            raise ValueError("invalid single-series CIKs")
        if not set(required).issubset(ids):
            raise ValueError("required series must be selected")
        pairs = tuple(self.selected_pairs)
        required_pairs = tuple(self.required_pairs)
        if any(not re.fullmatch(r"\d{10}", c) or not s for c, s in pairs + required_pairs):
            raise ValueError("invalid exact selector pair")
        if not set(required_pairs).issubset(pairs):
            raise ValueError("required exact pair must be selected")
        if type(self.empty_policy) is not SecEmptyPolicy:
            raise TypeError("empty_policy")
        object.__setattr__(self, "series_ids", ids)
        object.__setattr__(self, "required_series_ids", required)
        object.__setattr__(self, "single_series_ciks", tuple(sorted(singles)))
        object.__setattr__(self, "selected_pairs", tuple(sorted(set(pairs))))
        object.__setattr__(self, "required_pairs", tuple(sorted(set(required_pairs))))

    @property
    def spec(self) -> RequestSpec:
        return RequestSpec(
            "sec",
            self.endpoint,
            {
                "year": self.year,
                "quarter": self.quarter,
                "series_ids": self.series_ids,
                "required_series_ids": self.required_series_ids,
                "single_series_ciks": self.single_series_ciks,
                "selected_pairs": self.selected_pairs,
                "required_pairs": self.required_pairs,
                "empty_policy": self.empty_policy.value,
            },
            (),
        )

    @property
    def source_url(self) -> str:
        return f"https://www.sec.gov/files/dera/data/form-n-port-data-sets/{self.year}q{self.quarter}_nport.zip"


@dataclass(frozen=True)
class SecEdgarSubmissionsRequest:
    cik: str
    required_accessions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{10}", self.cik):
            raise ValueError("CIK must be ten digits")
        vals = tuple(self.required_accessions)
        if (
            not vals
            or len(set(vals)) != len(vals)
            or any(not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", x) for x in vals)
        ):
            raise ValueError("invalid accessions")
        object.__setattr__(self, "required_accessions", tuple(sorted(vals)))

    @property
    def spec(self) -> RequestSpec:
        return RequestSpec(
            "sec",
            "edgar_submissions",
            {"cik": self.cik, "required_accessions": self.required_accessions},
            (),
        )

    @property
    def source_url(self) -> str:
        return f"https://data.sec.gov/submissions/CIK{self.cik}.json"


__all__ = ["SecEdgarSubmissionsRequest", "SecEmptyPolicy", "SecNportQuarterRequest"]
