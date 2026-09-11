"""Private complete-receipt payloads for observed-financial integrity seals."""

from ...core import SnapshotObservationRef


def receipt_binding(receipt: SnapshotObservationRef) -> dict[str, str]:
    """Keep private paths in integrity input while excluding them from output bytes."""
    return {
        "path": str(receipt.path),
        "observation_identity": receipt.observation_identity,
        "snapshot_identity": receipt.snapshot_identity,
        "fact_version": receipt.fact_version,
        "mode": receipt.mode.value,
        "provider": receipt.provider,
        "endpoint": receipt.endpoint,
        "request_identity": receipt.request_identity,
        "response_sha256": receipt.response_sha256,
        "serialization_identifier": receipt.serialization_identifier,
        "snapshot_fetched_at": receipt.snapshot_fetched_at.isoformat().replace("+00:00", "Z"),
    }
