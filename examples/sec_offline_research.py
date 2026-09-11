"""Offline synthetic TTM/PIT demonstration; never downloads or certifies real data.

Run: uv run python examples/sec_offline_research.py
All identities, availability declarations, quality decisions and consumer commits
in this example are synthetic. The temporary snapshots are deleted on exit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    RequestSpec,
    SnapshotStore,
)
from ohmydata.providers.sec import (
    SecCompanyFinancialVintage,
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
    SecQuarterDeclaration,
    SecQuarterTtmConfig,
    SecQuarterTtmRecipe,
    SecStatementRow,
    compute_sec_four_quarter_ttm,
    load_sec_pit_bundle,
    serialize_sec_typed_rows_projection,
    write_sec_pit_bundle,
)

_CONFIG = hashlib.sha256(b"synthetic-research-configuration-v1").hexdigest()
_ARTIFACT = hashlib.sha256(b"synthetic-typed-rows-not-an-SEC-filing").hexdigest()
_DATASET = hashlib.sha256(b"synthetic-consumer-dataset").hexdigest()
_PUBLIC = datetime(2024, 2, 1, 9, tzinfo=UTC)
_OBSERVED = _PUBLIC + timedelta(hours=1)
_QUALITY = _OBSERVED + timedelta(minutes=30)
_COMMITTED = _PUBLIC + timedelta(hours=3)
_REVOKED = _PUBLIC + timedelta(days=1)


def run_example(root: Path) -> dict[str, object]:
    """Use a caller-owned output directory; no credentials or provider calls."""
    sources = SnapshotStore(root / "synthetic-projections")
    bundles = SnapshotStore(root / "synthetic-bundles")
    versions = []
    observations = {}
    quarters = []
    periods = (
        (date(2023, 1, 1), date(2023, 3, 31)),
        (date(2023, 4, 1), date(2023, 6, 30)),
        (date(2023, 7, 1), date(2023, 9, 30)),
        (date(2023, 10, 1), date(2023, 12, 31)),
    )
    for ordinal, (start, end) in enumerate(periods, start=1):
        value = Decimal(ordinal) * Decimal("10.00")
        row = SecStatementRow(
            "income_statement",
            "Synthetic revenue",
            "synthetic:Revenue",
            "Revenue",
            value,
            str(value),
            "USD",
            period_start=start,
            period_end=end,
            period_type="duration",
            dimension=None,
        )
        vintage = SecCompanyFinancialVintage(
            symbol="SYNTHETIC",
            cik="1",
            company_name="Synthetic example, no real issuer",
            form="10-Q",
            accession_number=f"0000000001-24-00000{ordinal}",
            filing_date=_PUBLIC.date(),
            rows=(row,),
        )
        payload = serialize_sec_typed_rows_projection(
            vintage,
            source_artifact_identity=_ARTIFACT,
            source_available_at=_PUBLIC,
        )
        observation = sources.observe(
            RequestSpec("sec", "financial-typed-rows", {"synthetic_quarter": ordinal}),
            payload,
            _OBSERVED,
            "sec-financial-typed-rows-projection-v1",
        )
        observations[observation.observation_identity] = observation
        version = SecNormalizedFinancialFactVersion.from_projection(
            store=sources,
            observation=observation,
            availability=AvailabilityEvidence(
                _PUBLIC,
                _OBSERVED,
                _OBSERVED,
                AvailabilityBasis.SOURCE_DECLARED,
                AvailabilityPrecision.TIMESTAMP,
            ),
            vintage=vintage,
            row_ordinal=0,
            schema_version="sec-financial-normalized-v1",
            adapter_version="synthetic-adapter-v1",
            normalization_version="synthetic-v1",
            configuration_identity=_CONFIG,
            recorded_at=_OBSERVED,
        )
        versions.append(version)
        quarters.append(
            SecQuarterDeclaration(
                version.normalized_version_id,
                2023,
                ordinal,
                True,
                start,
                end,
                "synthetic:Revenue",
                "synthetic-consolidated",
                "synthetic-common-equity",
                "synthetic-comparable-cohort",
                f"synthetic-quarter-{ordinal}",
            )
        )
    quality = tuple(
        SecQualityRecord(
            version.normalized_version_id,
            "synthetic-quality-v1",
            SecQualityStatus.PASS,
            _QUALITY,
        )
        for version in versions
    )
    commits = tuple(
        SecConsumerCommit(
            version.normalized_version_id,
            item.quality_record_id,
            _DATASET,
            _COMMITTED,
        )
        for version, item in zip(versions, quality, strict=True)
    )
    revoked = SecQualityRecord(
        versions[-1].normalized_version_id,
        "synthetic-quality-v1",
        SecQualityStatus.QUARANTINED,
        _REVOKED,
        quality[-1].quality_record_id,
    )
    retained_quality = (*quality, revoked)
    ref = write_sec_pit_bundle(
        store=bundles,
        batch_identity="synthetic-offline-research-v1",
        versions=versions,
        quality_records=retained_quality,
        consumer_commits=commits,
        source_store=sources,
        resolve_observation=observations.__getitem__,
        captured_at=_REVOKED,
    )
    # New store instances reload retained bytes. Locator ownership stays with the caller.
    restored = load_sec_pit_bundle(
        store=SnapshotStore(root / "synthetic-bundles"),
        bundle_ref=ref,
        source_store=SnapshotStore(root / "synthetic-projections"),
        resolve_observation=observations.__getitem__,
    )
    cases = (
        ("before_publication", _PUBLIC - timedelta(seconds=1), SecPitMode.MARKET_KNOWN, False),
        ("market_before_commit", _COMMITTED - timedelta(seconds=1), SecPitMode.MARKET_KNOWN, True),
        (
            "system_before_commit",
            _COMMITTED - timedelta(seconds=1),
            SecPitMode.SYSTEM_REPLAY,
            False,
        ),
        ("system_at_commit", _COMMITTED, SecPitMode.SYSTEM_REPLAY, True),
        ("system_after_quarantine", _REVOKED, SecPitMode.SYSTEM_REPLAY, False),
    )
    results = []
    for name, cutoff, mode, expected_available in cases:
        config = SecQuarterTtmConfig(
            SecQuarterTtmRecipe.REVENUE_V1,
            "1",
            "revenue",
            "synthetic-issuer-declaration",
            "synthetic-metric-declaration",
            tuple(quarters),
            mode,
            cutoff,
            SecPitPolicy(
                "sec-financial-normalized-v1",
                "synthetic-adapter-v1",
                "synthetic-v1",
                _CONFIG,
                "synthetic-quality-v1",
                cutoff,
            ),
        )
        record = {"case": name, "cutoff": cutoff.isoformat(), "mode": mode.value}
        try:
            result = compute_sec_four_quarter_ttm(
                config=config,
                versions=restored.versions,
                quality_records=restored.quality_records,
                consumer_commits=restored.consumer_commits,
            )
        except ValueError as exc:
            # Only the anticipated selection failure is part of this demonstration.
            if expected_available or "unavailable" not in str(exc):
                raise
            record.update(status="UNAVAILABLE", reason="REQUIRED_VERSION_NOT_ELIGIBLE")
        else:
            if not expected_available or result.value != Decimal("100.00"):
                raise AssertionError("synthetic PIT expectation failed")
            original = compute_sec_four_quarter_ttm(
                config=config,
                versions=versions,
                quality_records=retained_quality,
                consumer_commits=commits,
            )
            if result.result_identity != original.result_identity:
                raise AssertionError("bundle replay changed synthetic result")
            record.update(
                status="AVAILABLE",
                value=str(result.value),
                unit=result.unit,
                result_identity=result.result_identity,
                input_availability_bound=result.input_availability_bound.isoformat(),
                input_version_ids=[item.version.normalized_version_id for item in result.inputs],
            )
        results.append(record)
    return {
        "scope": "SYNTHETIC_ONLY; no real market data, SEC disclosure or financial PASS evidence",
        "recipe": SecQuarterTtmRecipe.REVENUE_V1.value,
        "source_artifact_declaration": _ARTIFACT,
        "bundle_response_sha256": ref.response_sha256,
        "cases": results,
    }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="omd-synthetic-research-") as directory:
        print(json.dumps(run_example(Path(directory)), indent=2))
