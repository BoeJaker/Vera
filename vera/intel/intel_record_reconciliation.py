"""Read-only reconciliation of Intelligence records with shared evidence.

Intel's SQLite store remains authoritative.  This module does not create a
RecordRevision, infer a MemoryProjection, or turn a successful delivery call
into a stronger receipt than the native system retained.  It only compares
opaque business identities that are already present in Intel and Fabric.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "vera.intel-record-reconciliation/v1"
SIGNAL_DATASET = "intel.signals"
REPORT_DATASET = "intel.reports"


FAMILY_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "id": "topics",
        "native_authority": "intel.sqlite",
        "portable_record": {"applicable": False, "reason": "native_control_state"},
        "memory_projection": {"applicable": False, "reason": "control_state_not_retrieval_projection"},
        "shared_contract": "intel.topic",
    },
    {
        "id": "signals",
        "native_authority": "intel.sqlite",
        "portable_record": {"applicable": True, "status": "not_revision_backed"},
        "memory_projection": {"applicable": False, "reason": "fabric_discovery_record"},
        "shared_contract": "fabric.dataset:intel.signals",
    },
    {
        "id": "snapshots",
        "native_authority": "intel.sqlite",
        "portable_record": {"applicable": True, "status": "not_revision_backed"},
        "memory_projection": {"applicable": False, "reason": "assessment_history_not_memory_delivery"},
        "shared_contract": "none",
    },
    {
        "id": "reports",
        "native_authority": "intel.sqlite",
        "portable_record": {"applicable": True, "status": "not_revision_backed"},
        "memory_projection": {
            "applicable": True,
            "status": "delivery_evidence_only",
            "reason": "native_delivery_log_retains_no_projection_identity",
            "condition": "explicit_successful_memory_delivery",
        },
        "shared_contract": "explicit_delivery_only",
    },
    {
        "id": "decisions_outcomes",
        "native_authority": "intel.sqlite",
        "portable_record": {"applicable": False, "reason": "four_minds_ledger_contract"},
        "memory_projection": {"applicable": False, "reason": "decision_evidence_not_retrieval_projection"},
        "shared_contract": "four_minds.decision_outcome",
    },
    {
        "id": "actions_deliveries",
        "native_authority": "intel.sqlite",
        "portable_record": {"applicable": False, "reason": "activity_and_effect_receipts"},
        "memory_projection": {"applicable": False, "reason": "execution_evidence_not_retrieval_projection"},
        "shared_contract": "activity.effect_evidence",
    },
    {
        "id": "configuration",
        "native_authority": "intel.sqlite",
        "portable_record": {"applicable": False, "reason": "native_control_configuration"},
        "memory_projection": {"applicable": False, "reason": "configuration_not_retrieval_projection"},
        "shared_contract": "intel.config",
    },
)


def family_contracts() -> list[dict[str, Any]]:
    """Return copies so callers cannot mutate the closed classification."""
    return [
        {
            **row,
            "portable_record": dict(row["portable_record"]),
            "memory_projection": dict(row["memory_projection"]),
        }
        for row in FAMILY_CONTRACTS
    ]


def _opaque_id(value: Any) -> str:
    value = str(value or "").strip()
    return value[:256] if value else ""


def fabric_business_id(record: Mapping[str, Any], *, family: str) -> str:
    """Read the producer-owned identity without using record content as proof."""
    data = record.get("data")
    if isinstance(data, Mapping):
        value = _opaque_id(data.get("id"))
        if value:
            return value
    physical = _opaque_id(record.get("id"))
    if family == "signals" and physical.startswith("intel-sig-"):
        return physical[len("intel-sig-"):]
    return ""


def reconcile_ids(
    *, family: str, local_ids: Iterable[Any], shared_records: Sequence[Mapping[str, Any]],
    local_total: int, shared_total: int, local_complete: bool, shared_complete: bool,
    expected_shared_ids: Iterable[Any] | None = None, example_limit: int = 20,
) -> dict[str, Any]:
    """Compare bounded opaque IDs and state exactly when coverage is incomplete."""
    local = {_opaque_id(item) for item in local_ids}
    local.discard("")
    expected = ({_opaque_id(item) for item in expected_shared_ids}
                if expected_shared_ids is not None else set(local))
    expected.discard("")
    shared = {fabric_business_id(row, family=family) for row in shared_records}
    shared.discard("")
    matched = expected & shared
    missing = expected - shared
    unexpected = shared - local
    unidentified = sum(1 for row in shared_records
                       if not fabric_business_id(row, family=family))
    complete = bool(local_complete and shared_complete)
    if not complete:
        status = "bounded_sample"
    elif missing or unexpected or unidentified:
        status = "gaps"
    else:
        status = "matched"
    result = {
        "status": status,
        "coverage_complete": complete,
        "native": {"total": int(local_total), "sampled": len(local)},
        "shared": {
            "total": int(shared_total), "sampled": len(shared_records),
            "identified": len(shared), "unidentified": unidentified,
        },
        "expected_shared": len(expected),
        "matched": len(matched),
        "missing": len(missing),
        "unexpected": len(unexpected),
        "missing_ids": sorted(missing)[:example_limit],
        "unexpected_ids": sorted(unexpected)[:example_limit],
    }
    if not complete:
        result["reason"] = "comparison_limit_reached"
    return result


def unavailable_reconciliation(*, family: str, local_total: int,
                               local_sampled: int, error: str) -> dict[str, Any]:
    return {
        "status": "shared_source_unavailable",
        "coverage_complete": False,
        "native": {"total": int(local_total), "sampled": int(local_sampled)},
        "shared": {"total": 0, "sampled": 0, "identified": 0, "unidentified": 0},
        "expected_shared": 0,
        "matched": 0,
        "missing": 0,
        "unexpected": 0,
        "missing_ids": [],
        "unexpected_ids": [],
        "reason": "fabric_browse_failed",
        "error": str(error or "unavailable")[:240],
        "family": family,
    }


def result_envelope(*, families: Sequence[Mapping[str, Any]],
                    reconciliation: Mapping[str, Any], limit: int) -> dict[str, Any]:
    statuses = [str(row.get("status") or "") for row in reconciliation.values()
                if isinstance(row, Mapping)]
    return {
        "schema": SCHEMA,
        "native_authority": "intel.sqlite",
        "read_only": True,
        "executes": False,
        "writes": False,
        "limit": int(limit),
        "families": [dict(row) for row in families],
        "reconciliation": dict(reconciliation),
        "summary": {
            "matched": sum(status == "matched" for status in statuses),
            "with_gaps": sum(status == "gaps" for status in statuses),
            "bounded": sum(status == "bounded_sample" for status in statuses),
            "unavailable": sum(status == "shared_source_unavailable" for status in statuses),
        },
    }
