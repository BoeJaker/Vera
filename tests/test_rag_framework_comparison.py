from __future__ import annotations

import pytest

from vera.execution.rag_framework_comparison import (
    MANIFEST_SCHEMA,
    RAGFrameworkEvidence,
    compare_rag_frameworks,
    export_framework_manifest,
    import_framework_manifest,
)
from vera.fabric.dataset_provider import DatasetSnapshot
from vera.fabric.retrieval_comparison import (
    RetrievalCase,
    RetrievalCitation,
    RetrievalLifecycleMetrics,
    RetrievalObservation,
    RetrievalProviderEvidence,
    RetrievalProviderProfile,
)


pytestmark = pytest.mark.critical


def _manifest(framework: str) -> dict:
    roles = (["reader", "parser", "embedder", "store", "retriever", "synthesizer"]
             if framework == "llamaindex" else
             ["converter", "preprocessor", "embedder", "store", "retriever", "generator"])
    components = []
    for index, role in enumerate(roles):
        components.append({
            "id": f"step-{index + 1}", "role": role,
            "component_type": f"example.{role}",
            "depends_on": [] if not components else [components[-1]["id"]],
        })
    return {
        "schema": MANIFEST_SCHEMA, "framework": framework,
        "framework_version": "test-1", "pipeline_id": f"{framework}-pipeline",
        "name": "Portable ingestion and query", "components": components,
    }


def _snapshot() -> DatasetSnapshot:
    snapshot, _ = DatasetSnapshot.create(
        dataset_id="rag-fixture", created_at="2026-10-05T12:00:00Z",
        records=[{"record_id": "record-1", "revision_id": "revision-1"}],
        schema={"type": "object"}, provenance={"fixture": "lib-11"})
    return snapshot


def _retrieval(snapshot: DatasetSnapshot, framework: str,
               case: RetrievalCase) -> RetrievalProviderEvidence:
    return RetrievalProviderEvidence(
        snapshot_id=snapshot.snapshot_id,
        profile=RetrievalProviderProfile(
            provider_id=framework, kind=framework, revision="test-1"),
        observations=(RetrievalObservation(
            case_id=case.case_id, status="completed",
            citations=(RetrievalCitation("record-1", "revision-1"),),
            latency_ms=10 if framework == "llamaindex" else 12),),
        lifecycle=RetrievalLifecycleMetrics(
            index_ms=20, storage_bytes=100, deletion_ms=4),
    )


def test_supported_manifests_round_trip_without_execution() -> None:
    for framework in ("llamaindex", "haystack"):
        imported = import_framework_manifest(_manifest(framework))
        assert imported["ok"] is True
        assert imported["gaps"] == []
        assert imported["executes"] is False
        assert imported["providers_invoked"] is False
        assert imported["authority"] == {
            "workflow": "vera.workflow_ir", "records": "vera.fabric"}

        exported = export_framework_manifest(
            imported["workflow"], framework=framework,
            framework_version="test-1")
        assert exported["ok"] is True
        assert exported["manifest"]["framework"] == framework
        assert [row["role"] for row in exported["manifest"]["components"]] == [
            row["role"] for row in _manifest(framework)["components"]]
        assert exported["executes"] is False


def test_unsupported_semantics_fail_closed_but_can_be_inspected_lossily() -> None:
    manifest = _manifest("llamaindex")
    manifest["components"].append({
        "id": "agent", "role": "autonomous_agent",
        "component_type": "AgentWorkflow", "depends_on": ["step-6"],
        "callbacks": ["network"],
    })
    blocked = import_framework_manifest(manifest)
    assert blocked["ok"] is False
    assert blocked["workflow"] is None
    assert {gap["code"] for gap in blocked["gaps"]} == {
        "unsupported_component_fields", "unsupported_component_role"}

    inspected = import_framework_manifest(manifest, allow_lossy=True)
    assert inspected["ok"] is True
    assert inspected["lossy"] is True
    assert all(step["id"] != "agent" for step in inspected["workflow"]["steps"])


def test_manifest_dependencies_must_be_topologically_ordered() -> None:
    manifest = _manifest("haystack")
    manifest["components"][0]["depends_on"] = ["step-2"]
    with pytest.raises(ValueError, match="must precede"):
        import_framework_manifest(manifest)


def test_cross_framework_export_does_not_reuse_foreign_component_types() -> None:
    imported = import_framework_manifest(_manifest("llamaindex"))
    exported = export_framework_manifest(
        imported["workflow"], framework="haystack", framework_version="test-1")
    assert exported["ok"] is True
    assert exported["manifest"]["components"][0]["component_type"] == "converter"
    assert exported["gaps"][0]["code"] == "framework_specific_component_type"
    assert exported["gaps"][0]["blocking"] is False


def test_export_rejects_nonportable_workflow_structure() -> None:
    imported = import_framework_manifest(_manifest("haystack"))
    workflow = dict(imported["workflow"])
    workflow.pop("content_hash")
    workflow["steps"] = list(workflow["steps"]) + [{
        "id": "fanout", "type": "parallel", "branches": [{
            "id": "branch", "type": "task", "task": "data.retrieve",
            "output": "branch_result",
        }],
    }]
    exported = export_framework_manifest(
        workflow, framework="haystack", framework_version="test-1")
    assert exported["ok"] is False
    assert exported["manifest"] is None
    assert exported["gaps"][0]["code"] == "unsupported_structure"


def test_comparison_reuses_exact_snapshot_case_and_revision_citations() -> None:
    snapshot = _snapshot()
    citation = RetrievalCitation("record-1", "revision-1")
    case = RetrievalCase(
        case_key="known-fact", query_digest="sha256:" + "1" * 64,
        relevant_citations=(citation,), k=1)
    evidence = []
    for framework in ("llamaindex", "haystack"):
        projection = import_framework_manifest(_manifest(framework))
        evidence.append(RAGFrameworkEvidence(
            framework=framework, projection=projection,
            retrieval=_retrieval(snapshot, framework, case),
            import_ms=2, export_ms=3, dependency_count=10,
            installed_bytes=1024, teardown_ms=5,
            residual_resources=(), duplicate_authorities=()))

    result = compare_rag_frameworks(
        snapshot=snapshot, cases=(case,), evidence=tuple(evidence))
    assert result["winner"] is None
    assert result["effect"] == "none"
    assert result["providers_invoked"] is False
    assert result["frameworks_imported"] is False
    assert result["case_ids"] == [case.case_id]
    for framework in ("llamaindex", "haystack"):
        row = result["frameworks"][framework]
        assert row["projection"]["blocking_gap_count"] == 0
        assert row["retrieval"]["quality"]["citation_revision_accuracy"] == 1.0
        assert row["authority"] == {"duplicate_authorities": [], "passes": True}


def test_comparison_surfaces_teardown_and_duplicate_authority_debt() -> None:
    snapshot = _snapshot()
    case = RetrievalCase(
        case_key="known-fact", query_digest="sha256:" + "2" * 64,
        relevant_citations=(RetrievalCitation("record-1", "revision-1"),), k=1)
    evidence = []
    for framework in ("llamaindex", "haystack"):
        evidence.append(RAGFrameworkEvidence(
            framework=framework,
            projection=import_framework_manifest(_manifest(framework)),
            retrieval=_retrieval(snapshot, framework, case),
            import_ms=1, export_ms=1, dependency_count=1,
            installed_bytes=1, teardown_ms=1,
            residual_resources=("worker",) if framework == "haystack" else (),
            duplicate_authorities=("document_store",) if framework == "haystack" else ()))
    result = compare_rag_frameworks(
        snapshot=snapshot, cases=(case,), evidence=tuple(evidence))
    assert result["frameworks"]["haystack"]["overhead"]["residual_resources"] == ["worker"]
    assert result["frameworks"]["haystack"]["authority"]["passes"] is False


def test_comparison_requires_both_frameworks_once() -> None:
    snapshot = _snapshot()
    case = RetrievalCase(
        case_key="known-fact", query_digest="sha256:" + "3" * 64,
        relevant_citations=(RetrievalCitation("record-1", "revision-1"),), k=1)
    item = RAGFrameworkEvidence(
        framework="llamaindex",
        projection=import_framework_manifest(_manifest("llamaindex")),
        retrieval=_retrieval(snapshot, "llamaindex", case),
        import_ms=1, export_ms=1, dependency_count=1,
        installed_bytes=1, teardown_ms=1)
    with pytest.raises(ValueError, match="exactly one"):
        compare_rag_frameworks(snapshot=snapshot, cases=(case,), evidence=(item, item))


def test_evidence_rejects_cross_labelled_retrieval_and_detaches_projection() -> None:
    snapshot = _snapshot()
    case = RetrievalCase(
        case_key="known-fact", query_digest="sha256:" + "4" * 64,
        relevant_citations=(RetrievalCitation("record-1", "revision-1"),), k=1)
    projection = import_framework_manifest(_manifest("llamaindex"))
    item = RAGFrameworkEvidence(
        framework="llamaindex", projection=projection,
        retrieval=_retrieval(snapshot, "llamaindex", case),
        import_ms=1, export_ms=1, dependency_count=1,
        installed_bytes=1, teardown_ms=1)
    projection["gaps"].append({"blocking": True})
    assert item.projection["gaps"] == []

    with pytest.raises(ValueError, match="profile kind"):
        RAGFrameworkEvidence(
            framework="llamaindex", projection=item.projection,
            retrieval=_retrieval(snapshot, "haystack", case),
            import_ms=1, export_ms=1, dependency_count=1,
            installed_bytes=1, teardown_ms=1)
