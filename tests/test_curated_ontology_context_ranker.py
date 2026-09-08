import pytest

from vera.context_provider import ContextCitation, ContextItem, assemble_context
from vera.context_registry import ContextRegistry
from vera.ontologies.context_ranker import (
    CuratedOntologyAssertion,
    CuratedOntologyContextRanker,
)

pytestmark = pytest.mark.critical


def item(name, score):
    return ContextItem(name, f"text {name}", name, "record-revision", "memory",
                       score, 2, (ContextCitation(name, f"fabric://{name}"),))


def assertion(name, confidence=.8, assertion_id="assertion-1"):
    return CuratedOntologyAssertion(
        assertion_id, name, "concept:relevant", "supports", confidence,
        "curator:knowledge-team")


def test_curated_ontology_ranks_only_existing_context_and_records_provenance():
    original = [item("record-a", .8), item("record-b", .4)]
    ranker = CuratedOntologyContextRanker(
        [assertion("record-b", 1)], ontology_id="domain",
        ontology_revision="snapshot-7", weight=.5)
    ranked = ranker.rank(original)
    assert ranked[0] is original[0]
    assert ranked[1].text == original[1].text
    assert ranked[1].citations == original[1].citations
    assert ranked[1].score == pytest.approx(.7)
    evidence = ranked[1].ranking_evidence[0]
    assert (evidence.provider, evidence.revision, evidence.score,
            evidence.weight, evidence.locator) == (
                "ontology:domain", "snapshot-7", 1, .5,
                "ontology:domain:assertion-1")


def test_unmatched_assertions_never_inject_context_and_can_change_final_selection():
    values = [item("record-a", .7), item("record-b", .6)]
    ranker = CuratedOntologyContextRanker(
        [assertion("record-b", 1), assertion("not-a-candidate", 1, "other")],
        ontology_id="domain", ontology_revision="snapshot-1", weight=1)
    ranked = ranker.rank(values)
    assert len(ranked) == len(values)
    assert [value.item_id for value in assemble_context(
        ranked, budget_tokens=2).items] == ["record-b"]


def test_strongest_assertion_per_source_is_deterministic():
    ranker = CuratedOntologyContextRanker(
        [assertion("record", .5, "a"), assertion("record", .9, "b")],
        ontology_id="domain", ontology_revision="snapshot-1", weight=1)
    evidence = ranker.rank([item("record", .1)])[0].ranking_evidence[0]
    assert (evidence.score, evidence.locator) == (.9, "ontology:domain:b")


def test_same_snapshot_and_assertion_are_idempotent():
    ranker = CuratedOntologyContextRanker(
        [assertion("record")], ontology_id="domain",
        ontology_revision="snapshot-1")
    once = ranker.rank([item("record", .1)])[0]
    twice = ranker.rank([once])[0]
    assert twice is once
    assert len(twice.ranking_evidence) == 1


def test_registry_accepts_multiple_explicit_ontology_snapshots():
    first = CuratedOntologyContextRanker(
        [], ontology_id="first", ontology_revision="r1")
    second = CuratedOntologyContextRanker(
        [], ontology_id="second", ontology_revision="r1")
    registry = ContextRegistry()
    registry.register_ranker(first)
    registry.register_ranker(second)
    assert [(entry.role, entry.component_id) for entry in registry.manifest()] == [
        ("ranker", "ontology:first"), ("ranker", "ontology:second")]


@pytest.mark.parametrize("factory", [
    lambda: CuratedOntologyAssertion("", "source", "concept", "supports", .5,
                                     "curator"),
    lambda: CuratedOntologyAssertion("a", "source", "concept", "supports",
                                     float("nan"), "curator"),
    lambda: CuratedOntologyContextRanker([], ontology_id="", ontology_revision="r1"),
    lambda: CuratedOntologyContextRanker([], ontology_id="id", ontology_revision=""),
    lambda: CuratedOntologyContextRanker([], ontology_id="id",
                                         ontology_revision="r1", weight=2),
    lambda: CuratedOntologyContextRanker(
        [assertion("a", assertion_id="same"),
         assertion("b", assertion_id="same")],
        ontology_id="id", ontology_revision="r1"),
])
def test_invalid_or_ambiguous_curated_evidence_fails_closed(factory):
    with pytest.raises(ValueError):
        factory()
