from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from ohmydata.core import (
    InstrumentIdentity,
    InstrumentIdentityIndex,
    InstrumentType,
    IssuerIdentity,
    ProviderInstrumentAlias,
)
from ohmydata.providers.sec import (
    SecDataVersionId,
    SecDataVersionKind,
    SecDependencyEdge,
    SecDependencyIndex,
    plan_sec_event_invalidation,
)
from tests.providers.sec.test_metric_graph import Recipe, bridge, compute, config, step


def test_explicit_identity_binding_for_metrics_and_invalidation(tmp_path):
    recorded = datetime(2020, 1, 1, tzinfo=UTC)
    issuer = IssuerIdentity("demo:issuer", "evidence:issuer", recorded)
    security = InstrumentIdentity(
        "demo:class-a", issuer.issuer_id, InstrumentType.COMMON_SHARE, "evidence:security", recorded
    )
    alias = ProviderInstrumentAlias(
        "demo",
        "SYNTH",
        "XNAS",
        "USD",
        security.instrument_id,
        date(2020, 1, 1),
        date(2030, 1, 1),
        "evidence:alias",
        recorded,
    )
    index = InstrumentIdentityIndex([issuer], [security], [alias])
    resolved = index.resolve(
        provider="demo",
        alias="SYNTH",
        venue="XNAS",
        effective_date=date(2023, 6, 30),
        knowledge_cutoff=recorded,
    )
    versions, declarations = bridge(tmp_path)
    bound = tuple(
        replace(
            d,
            security_basis=resolved.instrument.instrument_id,
            declaration_reference=resolved.resolution_identity,
        )
        for d in declarations
    )
    cfg = config(versions, bound, (step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),))
    result = compute(cfg, versions)
    assert result.final.value == Decimal(140)
    assert result.final.metadata.security_basis == security.instrument_id
    assert (
        result.final.configuration_identity
        != config(versions, declarations, cfg.steps).configuration_identity
    )
    source = SecDataVersionId(SecDataVersionKind.INSTRUMENT_IDENTITY, resolved.binding_identity)
    output = SecDataVersionId(SecDataVersionKind.DERIVED_METRIC, result.final.value_identity)
    # CIK remains an explicit SEC caller declaration, never parsed from issuer_id.
    edge = SecDependencyEdge(source, output, "1", cfg.configuration_identity, recorded)
    plan = plan_sec_event_invalidation(SecDependencyIndex([edge]), [source], known_at=recorded)
    assert plan.affected_outputs == (output,)
    assert plan.traversed_edge_ids == (edge.edge_identity,)
