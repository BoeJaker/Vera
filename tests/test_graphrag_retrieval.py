import asyncio

import pytest

from vera.fabric.dataset_provider import CancellationSignal, DatasetSnapshot
from vera.fabric.external_retrieval import (
    ExternalRetrievalRequest,
    ExternalSnapshotBinding,
    external_adapter,
)
from vera.fabric.graphrag_retrieval import GraphRAGSnapshotDriver
from vera.fabric.retrieval_execution import RetrievalProviderFailure, RetrievalProviderUnavailable
from vera.fabric.retrieval_lifecycle import evaluate_retrieval_lifecycle


pytestmark = pytest.mark.critical


RECORDS = (
    {"record_id": "rec-a", "revision_id": "rev-a", "text": "alpha source"},
    {"record_id": "rec-b", "revision_id": "rev-b", "text": "beta source"},
)


def snapshot():
    return DatasetSnapshot.create(
        dataset_id="graphrag", created_at="2026-09-26T00:00:00Z",
        records=RECORDS, schema={}, provenance={})[0]


def binding(mode="local"):
    return ExternalSnapshotBinding.create(
        snapshot=snapshot(), records=RECORDS, kind="graphrag",
        provider_revision="graphrag-api-v1", mode=mode,
        projection_revision="graph-index-v1")


def request_for(bound, *, limit=2):
    return ExternalRetrievalRequest(
        snapshot_id=bound.snapshot.snapshot_id,
        projection_id=bound.projection_id,
        provider_revision=bound.provider_revision,
        mode=bound.mode,
        limit=limit,
        _query_text="private question",
    )


class Runtime:
    def __init__(self, bound):
        self.bound = bound
        self.requests = []
        self.active = False
        self.recoveries = 0

    def _identity(self, request):
        return {key: request[key] for key in (
            "schema", "snapshot_id", "projection_id",
            "provider_revision", "mode", "workspace_id")}

    async def index(self, request, *, cancellation):
        cancellation.checkpoint()
        self.requests.append(("index", request))
        self.active = True
        return {
            **self._identity(request),
            "citation_manifest": request["citation_manifest"],
            "record_count": len(request["documents"]),
            "active": True,
        }

    async def query(self, request, *, cancellation):
        cancellation.checkpoint()
        self.requests.append(("query", request))
        return {
            **self._identity(request),
            "citations": [{"record_id": "rec-a", "revision_id": "rev-a"}],
            "answer": "must not cross the boundary",
        }

    async def inspect(self, request, *, cancellation):
        cancellation.checkpoint()
        self.requests.append(("inspect", request))
        return {
            **self._identity(request),
            "citation_manifest": [item.to_dict() for item in self.bound.citations],
            "record_count": len(self.bound.citations),
            "active": self.active,
            "metrics": {"update_ms": None, "storage_bytes": 4096,
                        "rebuild_ms": 7},
        }

    async def recover(self, request, *, cancellation):
        cancellation.checkpoint()
        self.recoveries += 1
        self.active = True
        return {**self._identity(request), "active": True}

    async def delete(self, request, *, cancellation):
        cancellation.checkpoint()
        self.requests.append(("delete", request))
        self.active = False
        return {**self._identity(request), "active": False}


def driver(mode="local", runtime=None):
    bound = binding(mode)
    runtime = runtime or Runtime(bound)
    return GraphRAGSnapshotDriver(
        binding=bound, records=RECORDS, runtime=runtime), bound, runtime


@pytest.mark.parametrize("mode", ["local", "global", "drift"])
def test_all_explicit_modes_provision_and_query_without_answer_retention(mode):
    subject, bound, runtime = driver(mode)

    async def run():
        provision = await subject.provision(cancellation=CancellationSignal())
        result = await subject.query(
            request_for(bound), cancellation=CancellationSignal())
        return provision, result

    provision, result = asyncio.run(run())
    assert provision["record_count"] == 2
    assert provision["activation_authority"] is False
    assert result["matches"] == [{"record_id": "rec-a", "revision_id": "rev-a"}]
    assert "answer" not in result
    index_request = next(value for name, value in runtime.requests if name == "index")
    assert index_request["documents"] == [
        {"record_id": "rec-a", "revision_id": "rev-a", "text": "alpha source"},
        {"record_id": "rec-b", "revision_id": "rev-b", "text": "beta source"},
    ]
    query_request = next(value for name, value in runtime.requests if name == "query")
    assert query_request["mode"] == mode
    assert query_request["return_answer"] is False
    assert query_request["return_context"] is False


def test_adapter_lifecycle_recovery_and_teardown_share_exact_workspace():
    subject, bound, runtime = driver("drift")

    async def run():
        await subject.provision(cancellation=CancellationSignal())
        adapter = external_adapter(binding=bound, driver=subject)
        report = await evaluate_retrieval_lifecycle(
            snapshot=bound.snapshot, adapters=(adapter,), perform_teardown=True)
        return report

    report = asyncio.run(run())
    row = report["providers"][0]
    assert row["baseline"]["status"] == "completed"
    assert row["baseline"]["metrics"]["storage_bytes"] == 4096
    assert row["teardown"]["status"] == "completed"
    assert report["activation_authority"] is False
    workspace_ids = {
        value["workspace_id"] for name, value in runtime.requests
        if name in {"index", "inspect", "delete"}}
    assert len(workspace_ids) == 1


def test_recovery_requires_fresh_complete_lifecycle_proof():
    subject, bound, runtime = driver()

    async def run():
        await subject.provision(cancellation=CancellationSignal())
        runtime.active = False
        adapter = external_adapter(binding=bound, driver=subject)
        return await evaluate_retrieval_lifecycle(
            snapshot=bound.snapshot, adapters=(adapter,), attempt_recovery=True)

    report = asyncio.run(run())
    row = report["providers"][0]
    assert row["baseline"]["status"] == "unavailable"
    assert row["recovery"]["status"] == "completed"
    assert runtime.recoveries == 1


def test_query_before_provision_and_request_identity_drift_fail_closed():
    subject, bound, _ = driver()
    with pytest.raises(RetrievalProviderUnavailable, match="graphrag_unavailable"):
        asyncio.run(subject.query(request_for(bound), cancellation=CancellationSignal()))
    asyncio.run(subject.provision(cancellation=CancellationSignal()))
    changed = ExternalRetrievalRequest(
        snapshot_id=bound.snapshot.snapshot_id,
        projection_id="xproj_" + "0" * 64,
        provider_revision=bound.provider_revision,
        mode=bound.mode,
        limit=1,
        _query_text="question",
    )
    with pytest.raises(RetrievalProviderFailure, match="receipt_identity_mismatch"):
        asyncio.run(subject.query(changed, cancellation=CancellationSignal()))


@pytest.mark.parametrize("failure", ["identity", "manifest", "count", "citation"])
def test_runtime_receipt_drift_fails_closed(failure):
    bound = binding()

    class Broken(Runtime):
        async def index(self, request, *, cancellation):
            value = await super().index(request, cancellation=cancellation)
            if failure == "identity":
                value["projection_id"] = "xproj_" + "0" * 64
            elif failure == "manifest":
                value["citation_manifest"] = value["citation_manifest"][:1]
            elif failure == "count":
                value["record_count"] = 1
            return value

        async def query(self, request, *, cancellation):
            value = await super().query(request, cancellation=cancellation)
            if failure == "citation":
                value["citations"] = [
                    {"record_id": "rec-a", "revision_id": "wrong"}]
            return value

    subject = GraphRAGSnapshotDriver(
        binding=bound, records=RECORDS, runtime=Broken(bound))
    if failure == "citation":
        asyncio.run(subject.provision(cancellation=CancellationSignal()))
        with pytest.raises(RetrievalProviderFailure, match="outside_snapshot"):
            asyncio.run(subject.query(
                request_for(bound), cancellation=CancellationSignal()))
    else:
        with pytest.raises(RetrievalProviderFailure):
            asyncio.run(subject.provision(cancellation=CancellationSignal()))


def test_records_runtime_and_error_detail_are_bounded():
    bound = binding()
    incomplete = RECORDS[:1]
    with pytest.raises(ValueError, match="recreate"):
        GraphRAGSnapshotDriver(
            binding=bound, records=incomplete, runtime=Runtime(bound))
    bad_text = (
        {"record_id": "rec-a", "revision_id": "rev-a", "text": ""},
        RECORDS[1],
    )
    bad_snapshot = DatasetSnapshot.create(
        dataset_id="graphrag", created_at="2026-09-26T00:00:00Z",
        records=bad_text, schema={}, provenance={})[0]
    bad_binding = ExternalSnapshotBinding.create(
        snapshot=bad_snapshot, records=bad_text, kind="graphrag",
        provider_revision="graphrag-api-v1", mode="local",
        projection_revision="graph-index-v1")
    with pytest.raises(ValueError, match="non-empty text"):
        GraphRAGSnapshotDriver(
            binding=bad_binding, records=bad_text, runtime=Runtime(bad_binding))

    class Exploding(Runtime):
        async def index(self, request, *, cancellation):
            raise RuntimeError("secret runtime detail")

    subject = GraphRAGSnapshotDriver(
        binding=bound, records=RECORDS, runtime=Exploding(bound))
    with pytest.raises(RetrievalProviderFailure, match="graphrag_runtime_error") as caught:
        asyncio.run(subject.provision(cancellation=CancellationSignal()))
    assert "secret runtime detail" not in str(caught.value)
