import pytest

from vera.context_provider import ContextCitation, ContextItem, assemble_context
from vera.worldview.context_ranker import WorldviewContextRanker

pytestmark = pytest.mark.critical


def item(name, score):
    return ContextItem(name, f"text {name}", name, "rev-source", "memory",
                       score, 2, (ContextCitation(name, f"fabric://{name}"),))


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
