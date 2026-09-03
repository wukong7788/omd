# pyright: reportArgumentType=false, reportUnnecessaryIsInstance=false
"""Deterministic windowed retrieval for Tushare ETF PCF constituents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ....core import FetchProvenance
from ....core.errors import PaginationError
from ..client import TushareClient, TushareFetchResult
from ..endpoints import EmptyPolicy, EtfShConsRequest, EtfSzConsRequest


@dataclass(frozen=True)
class EtfPcfHistoryRequest:
    ts_code: str
    exchange: str
    start_date: str
    end_date: str
    empty_policy: EmptyPolicy
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.ts_code) is not str or not self.ts_code.strip() or "," in self.ts_code:
            raise ValueError("ts_code must be exactly one non-empty symbol")
        if self.exchange not in ("SH", "SZ"):
            raise ValueError("exchange must be SH or SZ")
        if not self.ts_code.upper().endswith("." + self.exchange):
            raise ValueError("exchange conflicts with ts_code suffix")
        if not isinstance(self.empty_policy, EmptyPolicy):
            raise TypeError("empty_policy must be EmptyPolicy")
        endpoint = EtfShConsRequest if self.exchange == "SH" else EtfSzConsRequest
        endpoint(
            empty_policy=self.empty_policy,
            ts_code=self.ts_code,
            start_date=self.start_date,
            end_date=self.end_date,
            fields=self.fields,
        )


@dataclass(frozen=True, init=False)
class EtfPcfHistoryResult:
    _frame: Any
    provenances: tuple[FetchProvenance, ...]
    total_request_count: int
    truncation_count: int

    def __init__(
        self,
        frame: Any,
        provenances: tuple[FetchProvenance, ...],
        total_request_count: int,
        truncation_count: int,
    ) -> None:
        object.__setattr__(self, "_frame", frame.copy(deep=True))
        object.__setattr__(self, "provenances", tuple(provenances))
        object.__setattr__(self, "total_request_count", total_request_count)
        object.__setattr__(self, "truncation_count", truncation_count)

    @property
    def frame(self) -> Any:
        return self._frame.copy(deep=True)


def fetch_etf_pcf_history(
    client: TushareClient, request: EtfPcfHistoryRequest
) -> EtfPcfHistoryResult:
    endpoint_cls = EtfShConsRequest if request.exchange == "SH" else EtfSzConsRequest
    start = date(
        int(request.start_date[:4]), int(request.start_date[4:6]), int(request.start_date[6:])
    )
    end = date(int(request.end_date[:4]), int(request.end_date[4:6]), int(request.end_date[6:]))
    leaves: list[TushareFetchResult] = []
    truncations = 0

    def visit(lo: date, hi: date) -> None:
        nonlocal truncations
        try:
            if request.exchange == "SH":
                child_sh = EtfShConsRequest(
                    empty_policy=request.empty_policy,
                    ts_code=request.ts_code,
                    start_date=lo.strftime("%Y%m%d"),
                    end_date=hi.strftime("%Y%m%d"),
                    fields=request.fields,
                )
                result = client.fetch_etf_sh_cons(child_sh)
            else:
                child_sz = EtfSzConsRequest(
                    empty_policy=request.empty_policy,
                    ts_code=request.ts_code,
                    start_date=lo.strftime("%Y%m%d"),
                    end_date=hi.strftime("%Y%m%d"),
                    fields=request.fields,
                )
                result = client.fetch_etf_sz_cons(child_sz)
        except PaginationError:
            truncations += 1
            if lo == hi:
                raise
            midpoint = lo + timedelta(days=(hi - lo).days // 2)
            visit(lo, midpoint)
            visit(midpoint + timedelta(days=1), hi)
            return
        leaves.append(result)

    visit(start, end)
    import pandas as pd

    frames = [item.frame for item in leaves if not item.frame.empty]
    if frames:
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.sort_values(
            ["ts_code", "trade_date", "con_code"], kind="mergesort", ignore_index=True
        )
    else:
        fields = (
            request.fields
            or endpoint_cls(
                empty_policy=request.empty_policy,
                ts_code=request.ts_code,
                start_date=request.start_date,
                end_date=request.end_date,
            ).fields
        )
        merged = pd.DataFrame(columns=fields)
    return EtfPcfHistoryResult(
        merged, tuple(item.provenance for item in leaves), len(leaves) + truncations, truncations
    )


__all__ = ["EtfPcfHistoryRequest", "EtfPcfHistoryResult", "fetch_etf_pcf_history"]
