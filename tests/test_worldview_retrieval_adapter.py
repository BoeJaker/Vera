import asyncio
import hashlib

import pytest

from vera.fabric.dataset_provider import DatasetSnapshot
from vera.fabric.retrieval_comparison import RetrievalCase
from vera.fabric.retrieval_execution import (
    RetrievalQueryBinding,
    UnavailableRetrievalAdapter,
    execute_retrieval_comparison,
)
from vera.fabric.retrieval_comparison import RetrievalProviderProfile
from vera.worldview.retrieval_adapter import JepaWorldviewRetrievalAdapter
from vera.worldview.retrieval_provenance import JepaRetrievalProvenance


pytestmark = pytest.mark.critical


def _fixture():
    records = (
        {"record_id": "r1", "revision_id": "rev-a", "text": "alpha"},
        {"record_id": "r2", "revision_id": "rev-b", "text": "beta"},
    )
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id="worldview-corpus", created_at="2026-09-24T12:00:00Z",
        records=records,
        schema={"record_id": "string", "revision_id": "string", "text": "string"},
        provenance={"source": "test"})
    provenance = JepaRetrievalProvenance.create(
        snapshot=snapshot, snapshot_records=frozen,
        checkpoint_blob=b"checkpoint", indexed_record_ids=("r1", "r2"),
        provider_revision="worldview-jepa-v2")
    query = "alpha"
    case = RetrievalCase(
        case_key="case-a",
        query_digest="sha256:" + hashlib.sha256(query.encode()).hexdigest(),
        relevant_citations=provenance.citations,
        k=2)
    return snapshot, provenance, query, case


def test_adapter_requires_and_forwards_exact_live_receipt():
    snapshot, provenance, query, case = _fixture()

    async def runner(text, top_k, snapshot_id):
        assert (text, top_k, snapshot_id) == (query, 2, snapshot.snapshot_id)
        return {
            "ok": True, "eligible": True,
            "query_digest": case.query_digest,
            "citations": [{"record_id": "r1", "revision_id": "rev-a"}],
            "provenance": provenance.receipt(),
        }

    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, query),),
        adapters=(
            JepaWorldviewRetrievalAdapter(binding=provenance, query=runner),
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("qdrant", "qdrant", "unavailable")),
        )))
    evidence = {item.profile.provider_id: item for item in result.fixture.evidence}
    observation = evidence["jepa-worldview"].observations[0]
    assert observation.status == "completed"
    assert observation.citations[0].revision_id == "rev-a"
    assert query not in repr(result.to_dict())


@pytest.mark.parametrize("mutate,error_code", [
    (lambda body: body["provenance"].update({"snapshot_id": "snap_" + "0" * 64}),
     "provenance_mismatch"),
    (lambda body: body.update({"query_digest": "sha256:" + "0" * 64}),
     "query_digest_mismatch"),
    (lambda body: body.update({"citations": [
        {"record_id": "outside", "revision_id": "rev-x"}]}),
     "invalid_citation"),
])
def test_adapter_rejects_live_identity_drift(mutate, error_code):
    snapshot, provenance, query, case = _fixture()

    async def runner(text, top_k, snapshot_id):
        body = {
            "ok": True, "eligible": True,
            "query_digest": case.query_digest,
            "citations": [{"record_id": "r1", "revision_id": "rev-a"}],
            "provenance": provenance.receipt(),
        }
        mutate(body)
        return body

    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, query),),
        adapters=(
            JepaWorldviewRetrievalAdapter(binding=provenance, query=runner),
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "qdrant", "v1")),
        )))
    evidence = {item.profile.provider_id: item for item in result.fixture.evidence}
    assert evidence["jepa-worldview"].observations[0].error_code == error_code


def test_adapter_preserves_explicit_live_unavailability():
    snapshot, provenance, query, case = _fixture()

    async def runner(text, top_k, snapshot_id):
        return {"ok": False, "eligible": False,
                "error_code": "provenance_unavailable"}

    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, query),),
        adapters=(
            JepaWorldviewRetrievalAdapter(binding=provenance, query=runner),
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "qdrant", "v1")),
        )))
    evidence = {item.profile.provider_id: item for item in result.fixture.evidence}
    observation = evidence["jepa-worldview"].observations[0]
    assert observation.status == "failed"
    assert observation.error_code == "provenance_unavailable"
