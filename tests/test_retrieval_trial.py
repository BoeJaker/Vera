import asyncio
import hashlib
import json

import pytest

from vera.fabric.dataset_provider import CancellationSignal, DatasetSnapshot, QueryCancelled
from vera.fabric.retrieval_comparison import (
    RetrievalCase,
    RetrievalCitation,
    RetrievalLifecycleMetrics,
    RetrievalProviderProfile,
)
from vera.fabric.retrieval_execution import (
    RetrievalProviderUnavailable,
    RetrievalQueryBinding,
    UnavailableRetrievalAdapter,
)
from vera.fabric.retrieval_trial import run_retrieval_trial


pytestmark = pytest.mark.critical


RECORDS = (
    {"record_id": "rec-a", "revision_id": "rev-a", "text": "alpha"},
    {"record_id": "rec-b", "revision_id": "rev-b", "text": "beta"},
)


def snapshot():
    return DatasetSnapshot.create(
        dataset_id="trial", created_at="2026-09-26T00:00:00Z",
        records=RECORDS, schema={}, provenance={})[0]


def query_binding(key, text, citation):
    case = RetrievalCase(
        case_key=key,
        query_digest="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        relevant_citations=(citation,),
        k=1,
    )
    return RetrievalQueryBinding(case, text)


def bindings():
    return (
        query_binding("alpha", "private query alpha",
                      RetrievalCitation("rec-a", "rev-a")),
        query_binding("beta", "private query beta",
                      RetrievalCitation("rec-b", "rev-b")),
    )


class Adapter:
    def __init__(self, provider_id, kind, *, fail_query=False,
                 unavailable_lifecycle=False, bad_teardown=False):
        self.profile = RetrievalProviderProfile(provider_id, kind, "runtime-v1")
        self.fail_query = fail_query
        self.unavailable_lifecycle = unavailable_lifecycle
        self.bad_teardown = bad_teardown
        self.deleted = False

    async def retrieve(self, snapshot_value, binding, cancellation):
        cancellation.checkpoint()
        if self.fail_query:
            raise RuntimeError("secret provider detail")
        return binding.case.relevant_citations

    async def lifecycle(self, snapshot_value, cancellation):
        cancellation.checkpoint()
        if self.unavailable_lifecycle:
            raise RetrievalProviderUnavailable("provider_unavailable")
        return RetrievalLifecycleMetrics(
            index_ms=4, update_ms=2, storage_bytes=512, rebuild_ms=5)

    async def teardown(self, cancellation=None):
        if cancellation:
            cancellation.checkpoint()
        self.deleted = True
        return {
            "snapshot_id": snapshot().snapshot_id,
            "active": True if self.bad_teardown else False,
            "deletion_ms": 3,
        }


def healthy_adapters():
    return (
        Adapter("qdrant-dense", "qdrant"),
        Adapter("graphrag-local", "graphrag"),
    )


def test_common_trial_joins_query_lifecycle_and_deletion_without_winner():
    providers = healthy_adapters()
    result = asyncio.run(run_retrieval_trial(
        snapshot=snapshot(), bindings=bindings(), adapters=providers,
        perform_teardown=True))
    payload = result.to_dict()
    assert result.trial_id.startswith("rtrial_")
    assert payload["synthesis"]["comparison_ready"] is True
    assert payload["synthesis"]["complete_provider_count"] == 2
    assert payload["synthesis"]["winner"] is None
    assert payload["synthesis"]["fallback_selected"] is False
    assert payload["synthesis"]["activation_authority"] is False
    assert all(item.deleted for item in providers)
    assert all(row["deletion"]["status"] == "completed"
               for row in payload["synthesis"]["providers"].values())


def test_ephemeral_query_text_does_not_enter_trial_evidence():
    result = asyncio.run(run_retrieval_trial(
        snapshot=snapshot(), bindings=bindings(), adapters=healthy_adapters()))
    encoded = json.dumps(result.to_dict(), sort_keys=True)
    assert "private query" not in encoded
    assert result.to_dict()["execution"]["cases"][0]["query_digest"].startswith(
        "sha256:")
    mutable_copy = result.to_dict()
    mutable_copy["synthesis"]["winner"] = "qdrant-dense"
    assert result.to_dict()["synthesis"]["winner"] is None


def test_unavailable_provider_is_explicit_and_not_scored_as_ready():
    missing_profile = RetrievalProviderProfile(
        "graphrag-local", "graphrag", "runtime-v1")
    result = asyncio.run(run_retrieval_trial(
        snapshot=snapshot(), bindings=bindings(), adapters=(
            Adapter("qdrant-dense", "qdrant"),
            UnavailableRetrievalAdapter(
                missing_profile, error_code="graphrag_unavailable"),
        )))
    row = result.synthesis["providers"]["graphrag-local"]
    assert row["query"]["status"] == "unavailable"
    assert row["query"]["error_codes"] == {"graphrag_unavailable": 2}
    assert row["evidence_complete"] is False
    assert result.synthesis["comparison_ready"] is False


def test_provider_errors_are_redacted_and_preserved_as_incomplete():
    result = asyncio.run(run_retrieval_trial(
        snapshot=snapshot(), bindings=bindings(), adapters=(
            Adapter("qdrant-dense", "qdrant", fail_query=True),
            Adapter("graphrag-local", "graphrag"),
        )))
    payload = result.to_dict()
    row = payload["synthesis"]["providers"]["qdrant-dense"]
    assert row["query"]["status"] == "failed"
    assert row["query"]["error_codes"] == {"provider_error": 2}
    assert "secret provider detail" not in json.dumps(payload)


def test_lifecycle_unavailability_is_separate_from_query_quality():
    result = asyncio.run(run_retrieval_trial(
        snapshot=snapshot(), bindings=bindings(), adapters=(
            Adapter("qdrant-dense", "qdrant", unavailable_lifecycle=True),
            Adapter("graphrag-local", "graphrag"),
        )))
    row = result.synthesis["providers"]["qdrant-dense"]
    assert row["query"]["status"] == "complete"
    assert row["baseline"]["status"] == "unavailable"
    assert row["evidence_complete"] is False


def test_failed_teardown_blocks_complete_evidence_without_losing_queries():
    result = asyncio.run(run_retrieval_trial(
        snapshot=snapshot(), bindings=bindings(), adapters=(
            Adapter("qdrant-dense", "qdrant", bad_teardown=True),
            Adapter("graphrag-local", "graphrag"),
        ), perform_teardown=True))
    row = result.synthesis["providers"]["qdrant-dense"]
    assert row["query"]["status"] == "complete"
    assert row["deletion"]["status"] == "failed"
    assert row["deletion"]["error_code"] == "teardown_not_confirmed"
    assert result.synthesis["comparison_ready"] is False


def test_duplicate_provider_ids_and_cancelled_run_fail_closed():
    duplicate = Adapter("same", "qdrant")
    other = Adapter("same", "graphrag")
    with pytest.raises(ValueError, match="provider IDs"):
        asyncio.run(run_retrieval_trial(
            snapshot=snapshot(), bindings=bindings(), adapters=(duplicate, other)))

    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        asyncio.run(run_retrieval_trial(
            snapshot=snapshot(), bindings=bindings(), adapters=healthy_adapters(),
            cancellation=signal))
