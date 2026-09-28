from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from vera.context_provider import ContextCitation, ContextItem
from vera.discovery_contract import (
    CollectionOption,
    CollectionReceipt,
    DiscoveredArtifact,
    DiscoveredContext,
    DiscoveredDataset,
    DiscoveryRequest,
    DiscoveryResult,
    SourceCandidate,
)
from vera.fabric.dataset_provider import DatasetSnapshot


NOW = "2026-09-28T08:00:00Z"


def request(**changes):
    values = dict(query="portable discovery", requester="agent.runner",
                  tenant_id="tenant.one", namespace="research.general",
                  as_of=NOW, source_kinds=("api", "dataset"), max_sources=4,
                  max_context_items=8, timeout_ms=5000, max_bytes=10_000)
    values.update(changes)
    return DiscoveryRequest(**values)


def option(**changes):
    values = dict(source_id="source.one", method="api", provider="web.adapter",
                  provider_revision="rev-1", resource="network",
                  output_kinds=("context", "dataset", "artifact"),
                  estimated_latency_ms=80, estimated_cost_units=1,
                  max_bytes=10_000, network_required=True)
    values.update(changes)
    return CollectionOption(**values)


def candidate(req, opt=None, **changes):
    opt = opt or option()
    values = dict(request_id=req.request_id, source_id="source.one",
                  locator="https://example.test/data", source_kind="api",
                  provider="discovery.scout", revision="index-7", observed_at=NOW,
                  authority_score=.8, relevance_score=.9, freshness_score=.7,
                  options=(opt,))
    values.update(changes)
    return SourceCandidate(**values)


def receipt(req, cand, opt, **changes):
    values = dict(request_id=req.request_id, candidate_id=cand.candidate_id,
                  option_id=opt.option_id, provider_revision=opt.provider_revision,
                  status="succeeded", started_at=NOW,
                  completed_at="2026-09-28T08:00:01Z", item_count=3,
                  byte_count=1000, duration_ms=1000, cost_units=1)
    values.update(changes)
    return CollectionReceipt(**values)


def outputs(rec):
    context = DiscoveredContext(
        rec.receipt_id,
        ContextItem("item-1", "cited result", "source.one", "rev-1",
                    "web.adapter", .9, 3,
                    (ContextCitation(rec.receipt_id,
                                     "https://example.test/data"),)))
    snapshot, _ = DatasetSnapshot.create(
        dataset_id="dataset.one", created_at=NOW, records=({"id": 1},),
        schema={"fields": ["id"]},
        provenance={"collection_receipt_id": rec.receipt_id,
                    "source_id": "source.one"})
    dataset = DiscoveredDataset(rec.receipt_id, snapshot)
    artifact = DiscoveredArtifact(
        rec.receipt_id, "artifact.one", "s3://bucket/key", "rev-1",
        "sha256:" + "a" * 64)
    return context, dataset, artifact


@pytest.mark.critical
def test_portable_end_to_end_bundle_preserves_every_authority():
    req = request()
    opt = option()
    cand = candidate(req, opt)
    rec = receipt(req, cand, opt)
    context, dataset, artifact = outputs(rec)
    value = DiscoveryResult(
        request=req, candidates=(cand,), receipts=(rec,), context=(context,),
        datasets=(dataset,), artifacts=(artifact,))

    assert value.result_id.startswith("dsx_")
    assert value.context[0].item.citations[0].source_id == rec.receipt_id
    assert value.datasets[0].snapshot.provenance["collection_receipt_id"] == rec.receipt_id
    assert value.artifacts[0].sha256 == "sha256:" + "a" * 64
    encoded = value.to_dict()
    assert encoded["request"]["request_id"] == req.request_id
    assert encoded["context"][0]["item"]["text"] == "cited result"
    assert encoded["datasets"][0]["snapshot"]["snapshot_id"] == dataset.snapshot.snapshot_id
    with pytest.raises(FrozenInstanceError):
        value.request = request(query="changed")


def test_identities_are_order_stable_and_budget_sensitive():
    assert request(source_kinds=("dataset", "api")).request_id == request().request_id
    assert request(max_bytes=9999).request_id != request().request_id
    assert request(max_cost_units=999).request_id != request().request_id
    assert option(output_kinds=("dataset", "context", "artifact")).option_id == option().option_id
    req = request()
    first = option(method="api")
    second = option(method="feed", output_kinds=("context",))
    assert candidate(req, options=(first, second)).candidate_id == candidate(
        req, options=(second, first)).candidate_id


@pytest.mark.parametrize("change", [
    {"query": ""}, {"timeout_ms": 0}, {"max_sources": 257},
    {"source_kinds": ("unknown",)}, {"as_of": "2026-09-28"},
])
def test_request_fails_closed_on_invalid_or_unbounded_values(change):
    with pytest.raises(ValueError):
        request(**change)


@pytest.mark.parametrize("change", [
    {"method": "shell"}, {"resource": "automatic"},
    {"output_kinds": ()}, {"estimated_latency_ms": -1},
])
def test_option_fails_closed_on_unsupported_execution_claims(change):
    with pytest.raises(ValueError):
        option(**change)


def test_option_does_not_coerce_network_policy_strings():
    with pytest.raises(ValueError, match="boolean"):
        option(network_required="false")


def test_candidate_rejects_credentials_foreign_options_and_nonfinite_scores():
    req = request()
    with pytest.raises(ValueError, match="credentials"):
        candidate(req, locator="https://user:secret@example.test/data")
    with pytest.raises(ValueError, match="another source"):
        candidate(req, options=(option(source_id="source.two"),))
    with pytest.raises(ValueError, match="between zero and one"):
        candidate(req, relevance_score=float("nan"))


@pytest.mark.critical
def test_receipts_never_turn_failures_into_output_claims():
    req = request()
    opt = option()
    cand = candidate(req, opt)
    with pytest.raises(ValueError, match="requires an error_code"):
        receipt(req, cand, opt, status="failed", item_count=0, byte_count=0)
    with pytest.raises(ValueError, match="cannot claim"):
        receipt(req, cand, opt, status="failed", error_code="source_unavailable")
    with pytest.raises(ValueError, match="cannot carry"):
        receipt(req, cand, opt, error_code="warning")


def test_receipt_compares_timestamp_instants_not_string_shapes():
    req = request()
    opt = option()
    cand = candidate(req, opt)
    with pytest.raises(ValueError, match="cannot precede"):
        receipt(req, cand, opt, started_at="2026-09-28T08:00:00.9Z",
                completed_at="2026-09-28T08:00:00.10Z")


def test_context_dataset_and_artifact_require_receipt_lineage(tmp_path):
    req = request()
    opt = option()
    cand = candidate(req, opt)
    rec = receipt(req, cand, opt)
    item = ContextItem("item-1", "text", "source.one", "rev-1",
                       "web.adapter", .8, 1,
                       (ContextCitation("different", "fabric://record/1"),))
    with pytest.raises(ValueError, match="cite"):
        DiscoveredContext(rec.receipt_id, item)
    snapshot, _ = DatasetSnapshot.create(
        dataset_id="dataset.one", created_at=NOW, records=(), schema={},
        provenance={"collection_receipt_id": "different"})
    with pytest.raises(ValueError, match="provenance"):
        DiscoveredDataset(rec.receipt_id, snapshot)
    with pytest.raises(ValueError, match="content-addressed"):
        DiscoveredArtifact(rec.receipt_id, "artifact.one", "file:///tmp/a",
                           "rev-1", "not-a-hash")


@pytest.mark.critical
def test_result_enforces_exact_request_option_output_and_budget_identity():
    req = request()
    opt = option()
    cand = candidate(req, opt)
    rec = receipt(req, cand, opt)
    context, dataset, artifact = outputs(rec)
    with pytest.raises(ValueError, match="another request"):
        DiscoveryResult(request=request(query="other"), candidates=(cand,),
                        receipts=(rec,))
    wrong = CollectionReceipt(
        request_id=req.request_id, candidate_id=cand.candidate_id,
        option_id=opt.option_id, provider_revision="wrong", status="failed",
        started_at=NOW, completed_at=NOW, error_code="rejected")
    with pytest.raises(ValueError, match="option identity"):
        DiscoveryResult(request=req, candidates=(cand,), receipts=(wrong,))
    with pytest.raises(ValueError, match="item_count"):
        DiscoveryResult(request=req, candidates=(cand,), receipts=(rec,),
                        context=(context,))
    tiny = request(max_bytes=999)
    tiny_cand = candidate(tiny, opt)
    tiny_rec = receipt(tiny, tiny_cand, opt)
    with pytest.raises(ValueError, match="bytes exceed"):
        DiscoveryResult(request=tiny, candidates=(tiny_cand,), receipts=(tiny_rec,),
                        context=outputs(tiny_rec)[:1],
                        datasets=outputs(tiny_rec)[1:2],
                        artifacts=outputs(tiny_rec)[2:])


def test_result_rejects_output_kind_not_declared_by_option():
    req = request()
    opt = option(output_kinds=("dataset",))
    cand = candidate(req, opt)
    rec = receipt(req, cand, opt, item_count=1)
    context = outputs(rec)[0]
    with pytest.raises(ValueError, match="not declared"):
        DiscoveryResult(request=req, candidates=(cand,), receipts=(rec,),
                        context=(context,))


@pytest.mark.parametrize(("request_change", "receipt_change", "message"), [
    ({"max_cost_units": 0}, {"cost_units": 1}, "cost exceeds"),
    ({"timeout_ms": 999}, {"duration_ms": 1000}, "duration exceeds"),
])
def test_result_enforces_cost_and_time_budgets(request_change, receipt_change,
                                               message):
    req = request(**request_change)
    opt = option()
    cand = candidate(req, opt)
    rec = receipt(req, cand, opt, **receipt_change)
    context, dataset, artifact = outputs(rec)
    with pytest.raises(ValueError, match=message):
        DiscoveryResult(request=req, candidates=(cand,), receipts=(rec,),
                        context=(context,), datasets=(dataset,),
                        artifacts=(artifact,))


def test_contract_module_contains_no_runtime_or_network_dependencies():
    source = (Path(__file__).parents[1] / "vera" / "discovery_contract.py").read_text()
    for forbidden in ("requests", "httpx", "aiohttp", "torch", "tensorflow",
                      "CAPABILITY_REGISTRY", "dispatch_task", "ollama"):
        assert forbidden not in source
