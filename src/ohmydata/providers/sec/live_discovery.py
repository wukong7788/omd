"""Fetch a retained SEC submissions closure and discover filing events."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ...core import SnapshotStore
from ._event_discovery_models import (
    SecDiscoveryBatch,
    SecDiscoveryCursor,
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecRootDiscoveryResult,
)
from .event_discovery import discover_sec_filing_events, discover_sec_incremental_events_from_root
from .http import SecHttpClient
from .submissions import fetch_sec_submissions_closure, fetch_sec_submissions_root


def fetch_sec_discovery_batch(
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    policy: SecDiscoveryPolicy,
    prior_cursor: SecDiscoveryCursor | None,
    clock: Callable[[], datetime],
) -> SecDiscoveryBatch:
    """Fetch the advertised closure, retain it, then run offline discovery.

    A fetch or validation failure never advances the ledger cursor. The caller
    owns scheduling, retries after body-read failure, and reconciliation.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot instances")
    if type(policy) is not SecDiscoveryPolicy or not callable(clock):
        raise TypeError("invalid discovery policy or clock")
    if prior_cursor is not None and type(prior_cursor) is not SecDiscoveryCursor:
        raise TypeError("invalid prior cursor")
    if (
        prior_cursor is not None
        and policy.mode is SecDiscoveryMode.INCREMENTAL
        and (
            policy.acceptance_lower > prior_cursor.acceptance_at - policy.overlap
            or policy.acceptance_upper < prior_cursor.acceptance_at
        )
    ):
        raise ValueError("incremental window excludes declared overlap")

    closure = fetch_sec_submissions_closure(store, client, policy.cik.zfill(10), utc_now=clock)
    return discover_sec_filing_events(
        store,
        closure.root_source,
        closure.historical_sources,
        policy=policy,
        prior_cursor=prior_cursor,
    )


def fetch_sec_incremental_discovery(
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    policy: SecDiscoveryPolicy,
    prior_cursor: SecDiscoveryCursor | None,
    clock: Callable[[], datetime],
) -> SecRootDiscoveryResult:
    """Fetch one root and report whether its recent rows cover the full window.

    NEEDS_RECONCILE carries no batch, so callers cannot mistake a partial root
    projection for a complete filing-event scan.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot instances")
    if type(policy) is not SecDiscoveryPolicy or not callable(clock):
        raise TypeError("invalid discovery policy or clock")
    if prior_cursor is not None and type(prior_cursor) is not SecDiscoveryCursor:
        raise TypeError("invalid prior cursor")
    root_source = fetch_sec_submissions_root(store, client, policy.cik.zfill(10), utc_now=clock)
    return discover_sec_incremental_events_from_root(
        store, root_source, policy=policy, prior_cursor=prior_cursor
    )


__all__ = ["fetch_sec_discovery_batch", "fetch_sec_incremental_discovery"]
