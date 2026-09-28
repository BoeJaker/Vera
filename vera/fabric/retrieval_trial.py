"""Join retrieval quality and lifecycle evidence for one common corpus.

The executor and lifecycle coordinator deliberately remain independent.  This
module runs both over the same immutable snapshot, cases and provider adapters,
then emits a completeness receipt without ranking, selecting, activating or
falling back to any provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

from .dataset_provider import CancellationSignal, DatasetSnapshot
from .retrieval_execution import (
    RetrievalExecutionResult,
    RetrievalQueryBinding,
    SnapshotRetrievalAdapter,
    execute_retrieval_comparison,
)
from .retrieval_lifecycle import evaluate_retrieval_lifecycle


SCHEMA_VERSION = "vera.retrieval-trial/v1"


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("retrieval trial evidence must be finite JSON") from exc


def _query_coverage(observations: Sequence[Any]) -> dict[str, Any]:
    counts = {"completed": 0, "failed": 0, "cancelled": 0}
    errors: dict[str, int] = {}
    for row in observations:
        counts[row.status] += 1
        if row.error_code:
            errors[row.error_code] = errors.get(row.error_code, 0) + 1
    total = len(observations)
    if counts["completed"] == total:
        status = "complete"
    elif counts["completed"]:
        status = "partial"
    elif counts["cancelled"] == total:
        status = "cancelled"
    elif errors and all(code.endswith("_unavailable") or code == "snapshot_unavailable"
                        for code in errors):
        status = "unavailable"
    else:
        status = "failed"
    return {
        "status": status,
        "case_count": total,
        "outcomes": counts,
        "error_codes": dict(sorted(errors.items())),
    }


@dataclass(frozen=True, init=False)
class RetrievalTrialResult:
    execution: RetrievalExecutionResult
    trial_id: str
    _payload_json: str = field(repr=False)

    def __init__(self, execution: RetrievalExecutionResult,
                 lifecycle: Mapping[str, Any], synthesis: Mapping[str, Any],
                 trial_id: str) -> None:
        if not isinstance(execution, RetrievalExecutionResult):
            raise TypeError("execution must be RetrievalExecutionResult")
        if (not isinstance(trial_id, str) or not trial_id.startswith("rtrial_") or
                len(trial_id) != 71):
            raise ValueError("invalid retrieval trial ID")
        payload = {
            "schema": SCHEMA_VERSION,
            "trial_id": trial_id,
            "execution": execution.to_dict(),
            "lifecycle": dict(lifecycle),
            "synthesis": dict(synthesis),
        }
        object.__setattr__(self, "execution", execution)
        object.__setattr__(self, "trial_id", trial_id)
        object.__setattr__(self, "_payload_json", _canonical(payload))

    @property
    def lifecycle(self) -> dict[str, Any]:
        return json.loads(self._payload_json)["lifecycle"]

    @property
    def synthesis(self) -> dict[str, Any]:
        return json.loads(self._payload_json)["synthesis"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


def _synthesise(
    execution: RetrievalExecutionResult,
    lifecycle: Mapping[str, Any],
    *,
    attempt_recovery: bool,
    perform_teardown: bool,
) -> dict[str, Any]:
    rows = lifecycle.get("providers")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("lifecycle evidence must contain providers")
    lifecycle_by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("profile"), Mapping):
            raise ValueError("invalid lifecycle provider evidence")
        provider_id = row["profile"].get("provider_id")
        if not isinstance(provider_id, str) or provider_id in lifecycle_by_id:
            raise ValueError("lifecycle provider IDs must be unique")
        if row.get("snapshot_id") != execution.fixture.snapshot.snapshot_id:
            raise ValueError("lifecycle evidence uses another snapshot")
        lifecycle_by_id[provider_id] = row

    expected_profiles = {
        item.profile.provider_id: item.profile.to_dict()
        for item in execution.fixture.evidence
    }
    if set(lifecycle_by_id) != set(expected_profiles):
        raise ValueError("query and lifecycle provider sets must match")

    providers: dict[str, Any] = {}
    complete_count = 0
    for evidence in execution.fixture.evidence:
        provider_id = evidence.profile.provider_id
        lifecycle_row = lifecycle_by_id[provider_id]
        if lifecycle_row.get("profile") != expected_profiles[provider_id]:
            raise ValueError("query and lifecycle provider profiles must match")
        query = _query_coverage(evidence.observations)
        baseline = lifecycle_row.get("baseline")
        recovery = lifecycle_row.get("recovery")
        teardown = lifecycle_row.get("teardown")
        if not all(isinstance(item, Mapping)
                   for item in (baseline, recovery, teardown)):
            raise ValueError("invalid lifecycle phase evidence")
        lifecycle_complete = baseline.get("status") == "completed"
        recovery_complete = (
            recovery.get("status") in {"completed", "not_requested"}
            if attempt_recovery else recovery.get("status") == "not_requested")
        deletion_complete = (
            teardown.get("status") == "completed"
            if perform_teardown else teardown.get("status") == "not_requested")
        complete = (query["status"] == "complete" and lifecycle_complete and
                    recovery_complete and deletion_complete)
        complete_count += int(complete)
        providers[provider_id] = {
            "kind": evidence.profile.kind,
            "revision": evidence.profile.revision,
            "query": query,
            "baseline": dict(baseline),
            "recovery": dict(recovery),
            "deletion": dict(teardown),
            "evidence_complete": complete,
        }

    return {
        "schema": "vera.retrieval-trial-synthesis/v1",
        "snapshot_id": execution.fixture.snapshot.snapshot_id,
        "case_ids": [item.case_id for item in execution.fixture.cases],
        "provider_count": len(providers),
        "complete_provider_count": complete_count,
        "comparison_ready": complete_count == len(providers),
        "providers": providers,
        "attempt_recovery": attempt_recovery,
        "perform_teardown": perform_teardown,
        "winner": None,
        "fallback_selected": False,
        "activation_authority": False,
    }


async def run_retrieval_trial(
    *,
    snapshot: DatasetSnapshot,
    bindings: Sequence[RetrievalQueryBinding],
    adapters: Sequence[SnapshotRetrievalAdapter],
    timeout_seconds: float = 30.0,
    attempt_recovery: bool = False,
    perform_teardown: bool = False,
    cancellation: CancellationSignal | None = None,
) -> RetrievalTrialResult:
    """Run query and lifecycle evidence sequentially over one provider set."""
    signal = cancellation or CancellationSignal()
    providers = tuple(adapters)
    execution = await execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=bindings,
        adapters=providers,
        timeout_seconds=timeout_seconds,
        cancellation=signal,
    )
    signal.checkpoint()
    lifecycle = await evaluate_retrieval_lifecycle(
        snapshot=snapshot,
        adapters=providers,
        timeout_seconds=timeout_seconds,
        attempt_recovery=attempt_recovery,
        perform_teardown=perform_teardown,
        cancellation=signal,
    )
    synthesis = _synthesise(
        execution,
        lifecycle,
        attempt_recovery=bool(attempt_recovery),
        perform_teardown=bool(perform_teardown),
    )
    identity = {
        "schema": SCHEMA_VERSION,
        "execution": execution.to_dict(),
        "lifecycle": lifecycle,
        "synthesis": synthesis,
    }
    trial_id = "rtrial_" + hashlib.sha256(_canonical(identity).encode()).hexdigest()
    return RetrievalTrialResult(execution, lifecycle, synthesis, trial_id)
