import pytest

from vera.context_provider import ContextCitation, ContextItem, assemble_context
from vera.context_registry import ContextRegistry
from vera.fabric.dataset_provider import DatasetSnapshot
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage
from vera.worldview.context_ranker import (
    PinnedWorldviewContextRanker, WorldviewContextRanker,
)
from vera.worldview.evidence_provider import (
    EvidenceCitation, EvidenceObservation, WorldviewEvidence,
)

pytestmark = pytest.mark.critical


def item(name, score):
    return ContextItem(name, f"text {name}", name, "rev-source", "memory",
                       score, 2, (ContextCitation(name, f"fabric://{name}"),))


def pinned_evidence():
    snapshot = DatasetSnapshot.create(
        dataset_id="worldview-active-index",
        created_at="2026-10-13T10:00:00Z",
        records=[{"id": "rec-a"}, {"id": "rec-b"}],
        schema={"type": "object"}, provenance={"source": "fixture"})[0]
    checkpoint = ModelPackage(
        name="worldview-jepa", version="1",
        architecture="worldview-jepa-v1", format="pytorch",
        artifacts=(ModelArtifact(
            "checkpoint", "artifact://worldview", "a" * 64, 1),),
        compatibility=ModelCompatibility(
            ("worldview.evidence",), "vera.dataset-snapshot/v1",
            "vera.worldview-evidence/v1"))
    observations = tuple(EvidenceObservation(
        observation_id=f"rank:{record_id}",
        subject_id=f"record:{record_id}", score=score,
        citations=(EvidenceCitation(record_id, "rev-source"),),
        attributes={"rank": rank})
        for record_id, score, rank in (("rec-a", 0.0, 2), ("rec-b", 1.0, 1)))
    evidence = WorldviewEvidence(
        kind="reranking", snapshot=snapshot, checkpoint=checkpoint,
        provider_revision="worldview-jepa-v2",
        observed_at="2026-10-13T10:01:00Z", observations=observations)
    return snapshot, checkpoint, evidence


def test_worldview_ranks_only_existing_cited_context_and_records_evidence():
    original = [item("rec-a", .8), item("rec-b", .7)]
    results = [{"id": "rec-b", "score": 1.0, "text": "untrusted cached text"},
               {"id": "unknown", "score": 1.0, "text": "must not appear"}]
    ranked = WorldviewContextRanker(
        results, model_revision="wv-checkpoint-7", weight=.5).rank(original)
    assert [value.text for value in ranked] == ["text rec-a", "text rec-b"]
    assert ranked[0] is original[0]
    assert ranked[1].score == pytest.approx(.85)
    evidence = ranked[1].ranking_evidence[0]
    assert (evidence.provider, evidence.revision, evidence.score, evidence.weight) == (
        "worldview", "wv-checkpoint-7", 1.0, .5)


def test_worldview_can_change_selection_without_changing_authority():
    values = [item("rec-a", .9), item("rec-b", .6)]
    ranked = WorldviewContextRanker(
        [{"id": "rec-a", "score": -1.0}, {"id": "rec-b", "score": 1.0}],
        model_revision="wv-1", weight=.8).rank(values)
    selected = assemble_context(ranked, budget_tokens=2).items
    assert [value.item_id for value in selected] == ["rec-b"]
    assert selected[0].provider == "memory"
    assert selected[0].citations == values[1].citations


@pytest.mark.parametrize("result", [
    {}, {"id": "record"}, {"id": "record", "score": float("nan")},
    {"id": "record", "score": 2}, "not-an-object",
])
def test_malformed_worldview_evidence_fails_closed(result):
    with pytest.raises(ValueError):
        WorldviewContextRanker([result], model_revision="wv-1")


def test_duplicate_worldview_ids_keep_strongest_score_deterministically():
    ranker = WorldviewContextRanker(
        [{"id": "rec-a", "score": -.5}, {"id": "rec-a", "score": .5}],
        model_revision="wv-1", weight=1)
    assert ranker.rank([item("rec-a", .1)])[0].score == .75


def test_same_worldview_revision_is_idempotent():
    ranker = WorldviewContextRanker(
        [{"id": "rec-a", "score": 1}], model_revision="wv-1", weight=.5)
    once = ranker.rank([item("rec-a", .2)])[0]
    twice = ranker.rank([once])[0]
    assert twice is once
    assert len(twice.ranking_evidence) == 1


def test_worldview_requires_versioned_model_and_bounded_weight():
    with pytest.raises(ValueError, match="model_revision"):
        WorldviewContextRanker([], model_revision="")
    with pytest.raises(ValueError, match="weight"):
        WorldviewContextRanker([], model_revision="wv-1", weight=1.1)


def test_pinned_ranker_requires_exact_snapshot_and_model_identity():
    snapshot, checkpoint, evidence = pinned_evidence()
    with pytest.raises(ValueError, match="dataset_snapshot_mismatch"):
        PinnedWorldviewContextRanker(
            evidence, expected_snapshot_id="stale-snapshot",
            expected_model_package_id=checkpoint.package_id)
    with pytest.raises(ValueError, match="model_package_mismatch"):
        PinnedWorldviewContextRanker(
            evidence, expected_snapshot_id=snapshot.snapshot_id,
            expected_model_package_id="stale-model")
    with pytest.raises(ValueError, match="evidence_not_available"):
        PinnedWorldviewContextRanker(
            None, expected_snapshot_id=snapshot.snapshot_id,
            expected_model_package_id=checkpoint.package_id)


def test_pinned_ranker_applies_only_exact_record_revisions_and_is_idempotent():
    snapshot, checkpoint, evidence = pinned_evidence()
    ranker = PinnedWorldviewContextRanker(
        evidence, expected_snapshot_id=snapshot.snapshot_id,
        expected_model_package_id=checkpoint.package_id, weight=.8)
    original = (item("rec-a", .9), item("rec-b", .6))
    ranked = ranker.rank(original)
    assert [value.score for value in ranked] == pytest.approx([.18, .92])
    assert [value.item_id for value in ranked] == ["rec-a", "rec-b"]
    assert ranked[1].citations == original[1].citations
    recorded = ranked[1].ranking_evidence[-1]
    assert (recorded.provider, recorded.revision,
            recorded.score, recorded.weight) == (
                ranker.ranker_id, evidence.evidence_id, 1.0, .8)
    assert ranker.rank(ranked) == ranked
    wrong_revision = ContextItem(
        "rec-a", "text rec-a", "rec-a", "other-revision", "memory",
        .9, 2, (ContextCitation("rec-a", "fabric://rec-a"),))
    with pytest.raises(ValueError, match="outside the candidate set"):
        ranker.rank((wrong_revision, item("rec-b", .6)))


@pytest.mark.asyncio
async def test_pinned_ranker_is_default_off_and_requires_explicit_registry_selection():
    snapshot, checkpoint, evidence = pinned_evidence()
    ranker = PinnedWorldviewContextRanker(
        evidence, expected_snapshot_id=snapshot.snapshot_id,
        expected_model_package_id=checkpoint.package_id, weight=.8)

    class Provider:
        provider_id = "memory"
        async def search(self, query, *, limit, cancellation=None):
            return (item("rec-a", .9), item("rec-b", .6))[:limit]

    registry = ContextRegistry()
    registry.register_provider(Provider())
    registry.register_ranker(ranker)
    baseline = await registry.compose(
        "query", provider_ids=("memory",), limit_per_provider=2,
        budget_tokens=2)
    canary = await registry.compose(
        "query", provider_ids=("memory",), ranker_ids=(ranker.ranker_id,),
        limit_per_provider=2, budget_tokens=2)
    assert [value.item_id for value in baseline.assembly.items] == ["rec-a"]
    assert baseline.rankers == ()
    assert [value.item_id for value in canary.assembly.items] == ["rec-b"]
    assert canary.rankers == (ranker.ranker_id,)
