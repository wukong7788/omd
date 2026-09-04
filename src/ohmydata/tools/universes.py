"""Strategy universe specifications, cluster constraints, and regime pools."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ClusterGroup:
    """Mutual exclusion cluster group in portfolio selection (e.g. Cluster Variant v3)."""

    name: str
    description: str
    symbols: tuple[str, ...]
    max_select: int = 1

    def __post_init__(self):
        object.__setattr__(self, "symbols", tuple(self.symbols))


@dataclass(frozen=True)
class RegimePool:
    """Market regime state pool (e.g. Risk-On vs Risk-Off)."""

    name: str
    condition: str
    symbols: tuple[str, ...]
    select_count: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "symbols", tuple(self.symbols))


@dataclass(frozen=True)
class StrategyUniverse:
    """Canonical representation of a strategy ETF/stock universe."""

    name: str
    description: str
    clusters: tuple[ClusterGroup, ...]
    regime_pools: tuple[RegimePool, ...]
    all_symbols: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "clusters", tuple(self.clusters))
        object.__setattr__(self, "regime_pools", tuple(self.regime_pools))
        if not self.all_symbols:
            seen: list[str] = []
            for pool in self.regime_pools:
                for sym in pool.symbols:
                    if sym not in seen:
                        seen.append(sym)
            for cluster in self.clusters:
                for sym in cluster.symbols:
                    if sym not in seen:
                        seen.append(sym)
            object.__setattr__(self, "all_symbols", tuple(seen))
        else:
            object.__setattr__(self, "all_symbols", tuple(self.all_symbols))


# r10a0 ETF Universe Definition
# Cluster Variant v3 with regime state-machine pools
R10A0_UNIVERSE = StrategyUniverse(
    name="r10a0",
    description="Multi-asset ETF rotation universe with cluster mutual exclusion and regime state-machine",
    clusters=(
        ClusterGroup(
            name="equity_risk",
            description="Broad equity & tech risk core",
            symbols=("SPY", "QQQ", "XLK", "IWM", "SMH"),
            max_select=1,
        ),
        ClusterGroup(
            name="sector_cyclicals",
            description="Cyclical sectors",
            symbols=("XLF", "XLE", "XLV"),
            max_select=1,
        ),
        ClusterGroup(
            name="defensive",
            description="Low volatility & traditional defensive assets",
            symbols=("TLT", "GLD", "USMV"),
            max_select=1,
        ),
    ),
    regime_pools=(
        RegimePool(
            name="risk_on",
            condition="Normal rotation pool (11 ETFs)",
            symbols=("SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "USMV", "TLT", "GLD", "SMH"),
            select_count=None,
        ),
        RegimePool(
            name="risk_off",
            condition="Defensive pool (triggered when SPY < MA200)",
            symbols=("SHY", "IEF", "GLD"),
            select_count=2,
        ),
    ),
    all_symbols=(
        "SPY",
        "QQQ",
        "XLK",
        "IWM",
        "SMH",
        "XLF",
        "XLE",
        "XLV",
        "TLT",
        "GLD",
        "USMV",
        "SHY",
        "IEF",
    ),
)

_REGISTRY: dict[str, StrategyUniverse] = {
    "r10a0": R10A0_UNIVERSE,
}


def get_universe(name: str) -> StrategyUniverse:
    """Retrieve predefined strategy universe by name (case-insensitive)."""
    normalized = name.strip().lower()
    if normalized not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys()))
        raise ValueError(f"Unknown universe {name!r}. Available: {available}")
    return _REGISTRY[normalized]
