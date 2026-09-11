import hashlib

import pytest

from vera.fabric.dataset_provider import DatasetSnapshot
from vera.fabric.retrieval_comparison import (
    RetrievalCase,
    RetrievalCitation,
    RetrievalComparisonFixture,
    RetrievalLifecycleMetrics,
    RetrievalObservation,
    RetrievalProviderEvidence,
    RetrievalProviderProfile,
    compare_retrieval,
)


pytestmark = pytest.mark.critical


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _snapshot(source: str = "fabric") -> DatasetSnapshot:
    snapshot, _ = DatasetSnapshot.create(
        dataset_id="retrieval-corpus",
        created_at="2026-09-11T09:00:00Z",
        records=({"record_id": "r1", "revision_id": "v1"},),
        schema={"record_id": "string", "revision_id": "string"},
        provenance={"source": source},
    )
    return snapshot


def _case(key: str = "case-a") -> RetrievalCase:
    return RetrievalCase(
        case_key=key,
        query_digest=_digest("private query " + key),
        relevant_citations=(
            RetrievalCitation("r1", "v1"),
            RetrievalCitation("r2", "v2"),
        ),
        k=2,
    )


def _evidence(snapshot, case, provider_id, kind, observations=None, lifecycle=None):
    return RetrievalProviderEvidence(
        snapshot_id=snapshot.snapshot_id,
        profile=RetrievalProviderProfile(provider_id, kind, "adapter-v1"),
        observations=observations or (
            RetrievalObservation(case.case_id, "completed", (
                RetrievalCitation("r1", "v1"),
                RetrievalCitation("r2", "wrong-revision"),
            ), latency_ms=10),
        ),
        lifecycle=lifecycle or RetrievalLifecycleMetrics(),
    )


def test_comparison_is_offline_snapshot_pinned_and_dimensioned():
    snapshot = _snapshot()
    case = _case()
    fabric = _evidence(
        snapshot, case, "fabric", "fabric_graph",
        lifecycle=RetrievalLifecycleMetrics(
            index_ms=100, update_ms=20, storage_bytes=4096,
            rebuild_ms=90, deletion_ms=12,
        ),
    )
    jepa = _evidence(
        snapshot, case, "jepa", "jepa_worldview_evidence",
        observations=(RetrievalObservation(
            case.case_id, "completed", (RetrievalCitation("r2", "v2"),),
            latency_ms=30,
        ),),
    )

    report = compare_retrieval(RetrievalComparisonFixture(
        snapshot=snapshot, cases=(case,), evidence=(jepa, fabric),
    ))

    assert report["snapshot"]["snapshot_id"] == snapshot.snapshot_id
    assert report["case_ids"] == [case.case_id]
    assert report["effect"] == "none"
    assert report["providers_invoked"] is False
    assert report["winner"] is None
    assert list(report["providers"]) == ["fabric", "jepa"]
    assert report["providers"]["fabric"]["quality"] == {
        "evaluated_cases": 1,
        "recall_at_k": 0.5,
        "precision_at_k": 0.5,
        "mrr": 1.0,
        "citation_revision_accuracy": 0.5,
    }
    assert report["providers"]["fabric"]["latency_ms"] == {
        "p50": 10.0, "p95": 10.0,
    }
    assert report["providers"]["fabric"]["lifecycle"] == {
        "index_ms": 100, "update_ms": 20, "storage_bytes": 4096,
        "rebuild_ms": 90, "deletion_ms": 12,
    }


def test_failures_are_separate_from_quality_and_missing_lifecycle_is_not_zero():
    snapshot = _snapshot()
    case = _case()
    failed = (RetrievalObservation(case.case_id, "failed", error_code="timeout"),)
    first = _evidence(snapshot, case, "first", "qdrant", observations=failed)
    second = _evidence(snapshot, case, "second", "graphrag", observations=failed)

    report = compare_retrieval(RetrievalComparisonFixture(
        snapshot=snapshot, cases=(case,), evidence=(first, second),
    ))

    row = report["providers"]["first"]
    assert row["quality"] == {
        "evaluated_cases": 0, "recall_at_k": None,
        "precision_at_k": None, "mrr": None,
        "citation_revision_accuracy": None,
    }
    assert row["latency_ms"] == {"p50": None, "p95": None}
    assert row["outcomes"] == {
        "completed": 0, "failed": 1, "cancelled": 0,
        "unsuccessful_rate": 1.0,
    }
    assert set(row["lifecycle"].values()) == {None}


def test_fixture_rejects_different_snapshots_or_case_sets():
    snapshot = _snapshot()
    other_snapshot = _snapshot("godseye-export")
    case = _case()
    other_case = _case("case-b")
    good = _evidence(snapshot, case, "fabric", "fabric_vector")
    wrong_snapshot = _evidence(other_snapshot, case, "qdrant", "qdrant")
    wrong_cases = _evidence(snapshot, other_case, "graph", "graphrag")

    with pytest.raises(ValueError, match="identical DatasetSnapshot"):
        RetrievalComparisonFixture(snapshot, (case,), (good, wrong_snapshot))
    with pytest.raises(ValueError, match="identical case set"):
        RetrievalComparisonFixture(snapshot, (case,), (good, wrong_cases))
    with pytest.raises(ValueError, match="at least two"):
        RetrievalComparisonFixture(snapshot, (case,), (good,))


@pytest.mark.parametrize("kind", ["worldview", "godseye", "non_jepa_worldview"])
def test_ambiguous_or_non_jepa_worldview_provider_names_are_rejected(kind):
    with pytest.raises(ValueError, match="unsupported retrieval provider kind"):
        RetrievalProviderProfile("provider", kind, "v1")


def test_case_identity_is_content_bound_and_contains_no_query_text():
    case = _case()
    reordered = RetrievalCase(
        case_key=case.case_key,
        query_digest=case.query_digest,
        relevant_citations=tuple(reversed(case.relevant_citations)),
        k=case.k,
    )
    changed = RetrievalCase(
        case_key=case.case_key,
        query_digest=_digest("different private query"),
        relevant_citations=case.relevant_citations,
        k=case.k,
    )

    assert reordered.case_id == case.case_id
    assert changed.case_id != case.case_id
    assert "private query" not in repr(case.to_dict())


def test_cancellation_is_not_misreported_as_provider_failure():
    snapshot = _snapshot()
    case = _case()
    cancelled = (RetrievalObservation(case.case_id, "cancelled"),)
    first = _evidence(snapshot, case, "first", "qdrant", observations=cancelled)
    second = _evidence(snapshot, case, "second", "graphrag", observations=cancelled)

    report = compare_retrieval(RetrievalComparisonFixture(
        snapshot=snapshot, cases=(case,), evidence=(first, second),
    ))

    assert report["providers"]["first"]["outcomes"] == {
        "completed": 0, "failed": 0, "cancelled": 1,
        "unsuccessful_rate": 1.0,
    }


def test_validation_rejects_malformed_evidence_without_attribute_errors():
    snapshot = _snapshot()
    case = _case()
    profile = RetrievalProviderProfile("fabric", "fabric_graph", "v1")

    with pytest.raises(ValueError, match="observations cannot be empty"):
        RetrievalProviderEvidence(snapshot.snapshot_id, profile, (object(),))
    with pytest.raises(ValueError, match="provider evidence"):
        RetrievalComparisonFixture(snapshot, (case,), (object(), object()))
    with pytest.raises(ValueError, match="DatasetSnapshot"):
        RetrievalProviderEvidence("snap_short", profile, (
            RetrievalObservation(case.case_id, "cancelled"),
        ))
    with pytest.raises(ValueError, match="non-negative integer"):
        RetrievalLifecycleMetrics(storage_bytes=True)


def test_source_has_no_runtime_provider_imports():
    from pathlib import Path
    import vera.fabric.retrieval_comparison as module

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    assert "import qdrant" not in source
    assert "import graphrag" not in source
    assert "from vera.worldview" not in source
