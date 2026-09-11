"""Bounded neutral financial formulas with SEC PIT and external attestation lineage."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from itertools import islice
from typing import Any

from ._metric_arithmetic import validate_decimal
from ._metric_common import (
    SecMetricCapexSign,
    SecMetricDomainPolicy,
    SecMetricRecipe,
    SecMetricRounding,
    identity,
)
from ._metric_evaluation import evaluate_step
from ._metric_graph_models import (
    SecMetricGraphConfig,
    SecMetricGraphResult,
    SecMetricMetadata,
    SecMetricStep,
    SecMetricTerminalEvidence,
    SecMetricValue,
)
from ._metric_inputs import SecMetricExternalInput, SecMetricTerminalDeclaration
from .pit import (
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecPitMode,
    SecPitResult,
    SecQualityRecord,
    select_sec_financial_versions,
)


def _bounded(values: Iterable[Any], remaining: int) -> tuple[Any, ...]:
    result = tuple(islice(values, remaining + 1))
    if len(result) > remaining:
        raise ValueError("metric aggregate input record budget exceeded")
    return result


def _metadata(declaration: SecMetricTerminalDeclaration) -> SecMetricMetadata:
    return SecMetricMetadata(
        declaration.metric,
        declaration.period_kind,
        declaration.fiscal_year,
        declaration.fiscal_end_quarter,
        declaration.period_start,
        declaration.period_end,
        declaration.accounting_scope,
        declaration.attribution_scope,
        declaration.dimension,
        declaration.comparability_cohort,
        declaration.security_basis,
    )


def _sec_terminal(
    config: SecMetricGraphConfig, declaration: SecMetricTerminalDeclaration, selected: SecPitResult
) -> SecMetricTerminalEvidence:
    version = selected.version
    row = version.row
    if (
        row.concept != declaration.native_concept
        or row.period_type != "duration"
        or row.period_start != declaration.period_start
        or row.period_end != declaration.period_end
        or row.dimension != declaration.dimension
        or row.currency != declaration.currency
        or row.unit is None
        or not row.unit
    ):
        raise ValueError(
            "SEC terminal does not match declared concept, period, dimension or currency"
        )
    value = validate_decimal(row.value)
    bound = version.source_available_at
    evidence_ids = [
        version.normalized_version_id,
        version.content_identity,
        version.observation.observation_identity,
        version.observation.fact_version,
        selected.quality_record.quality_record_id,
    ]
    if config.mode is SecPitMode.SYSTEM_REPLAY:
        if selected.consumer_commit is None:
            raise ValueError("SEC system replay terminal lacks consumer commit")
        bound = max(
            bound,
            version.observation.snapshot_fetched_at,
            version.recorded_at,
            selected.quality_record.recorded_at,
            selected.consumer_commit.committed_at,
        )
    if selected.consumer_commit is not None:
        evidence_ids.append(selected.consumer_commit.commit_id)
    result = SecMetricValue(
        declaration.local_id,
        config.configuration_identity,
        value,
        row.unit,
        declaration.currency,
        _metadata(declaration),
        (),
        tuple(sorted(evidence_ids)),
        bound,
        None,
        (),
        None,
    )
    return SecMetricTerminalEvidence(declaration, result, selected, None)


def _external_terminal(
    config: SecMetricGraphConfig,
    declaration: SecMetricTerminalDeclaration,
    external: SecMetricExternalInput,
) -> SecMetricTerminalEvidence:
    if any(
        getattr(declaration, name) != getattr(external, name)
        for name in (
            "metric",
            "currency",
            "period_start",
            "period_end",
            "accounting_scope",
            "attribution_scope",
            "security_basis",
        )
    ):
        raise ValueError("external attestation disagrees with terminal declaration")
    if (
        external.source_available_at > config.knowledge_cutoff
        or external.quality_recorded_at > config.policy.quality_cutoff
    ):
        raise ValueError("external terminal is unavailable at selected cutoffs")
    if (
        config.mode is SecPitMode.SYSTEM_REPLAY
        and config.policy.quality_cutoff != config.knowledge_cutoff
    ):
        raise ValueError("system replay quality cutoff must equal knowledge cutoff")
    bound = external.source_available_at
    if config.mode is SecPitMode.SYSTEM_REPLAY:
        if external.committed_at is None:
            raise ValueError("external system replay terminal lacks consumer commit")
        bound = max(
            bound,
            external.observed_at,
            external.recorded_at,
            external.quality_recorded_at,
            external.committed_at,
        )
        if bound > config.knowledge_cutoff:
            raise ValueError("external system replay terminal is not yet committed")
    metadata = replace(
        _metadata(declaration),
        forecast_horizon=external.forecast_horizon,
        valuation_at=external.valuation_at,
        company_total_equity=external.company_total_equity,
    )
    evidence_ids = [external.external_id, external.observation_identity]
    if external.commit_identity is not None:
        evidence_ids.append(external.commit_identity)
    result = SecMetricValue(
        declaration.local_id,
        config.configuration_identity,
        external.value,
        external.unit,
        external.currency,
        metadata,
        (),
        tuple(sorted(evidence_ids)),
        bound,
        None,
        (),
        None,
    )
    return SecMetricTerminalEvidence(declaration, result, None, external)


def compute_sec_metric_graph(
    *,
    config: SecMetricGraphConfig,
    versions: Iterable[SecNormalizedFinancialFactVersion],
    quality_records: Iterable[SecQualityRecord],
    consumer_commits: Iterable[SecConsumerCommit] = (),
    external_inputs: Iterable[SecMetricExternalInput] = (),
    max_input_records: int = 10_000,
) -> SecMetricGraphResult:
    """Evaluate named formulas; issuer and external evidence remain caller assertions."""
    if type(config) is not SecMetricGraphConfig:
        raise TypeError("config must be a SecMetricGraphConfig")
    if type(max_input_records) is not int or not 1 <= max_input_records <= 10_000:
        raise ValueError("max_input_records must be integer1..10000")
    # Reconstruct frozen declarations/configuration before trusting caller objects.
    checked = replace(
        config,
        declarations=tuple(replace(item) for item in config.declarations),
        steps=tuple(replace(item) for item in config.steps),
    )
    if checked.configuration_identity != config.configuration_identity:
        raise ValueError("metric configuration identity mismatch")
    version_items = _bounded(versions, max_input_records)
    quality_items = _bounded(quality_records, max_input_records - len(version_items))
    commit_items = _bounded(
        consumer_commits, max_input_records - len(version_items) - len(quality_items)
    )
    external_items = _bounded(
        external_inputs,
        max_input_records - len(version_items) - len(quality_items) - len(commit_items),
    )
    selected = select_sec_financial_versions(
        version_items,
        mode=config.mode,
        knowledge_cutoff=config.knowledge_cutoff,
        policy=config.policy,
        quality_records=quality_items,
        consumer_commits=commit_items,
    )
    by_id = {item.version.normalized_version_id: item for item in selected}
    externals: dict[str, SecMetricExternalInput] = {}
    for item in external_items:
        if type(item) is not SecMetricExternalInput:
            raise TypeError("invalid external metric input")
        rebuilt = replace(item)
        if rebuilt.external_id != item.external_id:
            raise ValueError("external attestation identity mismatch")
        if item.external_id in externals:
            raise ValueError("duplicate external metric input")
        externals[item.external_id] = rebuilt
    expected_external = {
        item.source_identity
        for item in config.declarations
        if item.source_kind == "EXTERNAL_ATTESTATION"
    }
    if externals.keys() != expected_external:
        raise ValueError("external terminal coverage differs from declarations")
    terminals = []
    for declaration in config.declarations:
        if declaration.source_kind == "NORMALIZED_FACT":
            if declaration.source_identity not in by_id:
                raise ValueError("requested SEC metric input is unavailable")
            terminals.append(_sec_terminal(config, declaration, by_id[declaration.source_identity]))
        else:
            terminals.append(
                _external_terminal(config, declaration, externals[declaration.source_identity])
            )
    results = {item.result.local_id: item.result for item in terminals}
    steps = []
    for step in config.steps:
        output = evaluate_step(config, step, tuple(results[ref] for ref in step.input_refs))
        results[step.step_id] = output
        steps.append(output)
    result_id = identity(
        "sec-metric-graph-result-v1",
        {
            "configuration_identity": config.configuration_identity,
            "terminal_value_identities": tuple(item.result.value_identity for item in terminals),
            "step_value_identities": tuple(item.value_identity for item in steps),
            "final_id": steps[-1].value_identity,
        },
    )
    return SecMetricGraphResult(config, tuple(terminals), tuple(steps), result_id)


__all__ = [
    "SecMetricCapexSign",
    "SecMetricDomainPolicy",
    "SecMetricExternalInput",
    "SecMetricGraphConfig",
    "SecMetricGraphResult",
    "SecMetricMetadata",
    "SecMetricRecipe",
    "SecMetricRounding",
    "SecMetricStep",
    "SecMetricTerminalDeclaration",
    "SecMetricTerminalEvidence",
    "SecMetricValue",
    "compute_sec_metric_graph",
]
