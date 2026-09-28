import pytest

from vera.context_enrichment import (
    ContextEnrichmentEvidence, ContextEnrichmentLedger,
    ContextEnrichmentTombstone, EnrichmentAuthority, context_authority,
    project_context_enrichment,
)
from vera.context_provider import ContextCitation, ContextItem


def item(revision="r1"):
    return ContextItem("a", "bounded cited context", "memory", revision, "fabric",
                       .8, 4, (ContextCitation("record-1", "fabric://record-1"),))


def evidence(producer="nlp", parents=(), authorities=None, citations=None,
             observed=100, until=500, **kwargs):
    target = item()
    return ContextEnrichmentEvidence(
        producer, "model-r1", kwargs.pop("kind", "entity"), observed, until,
        tuple(authorities or (context_authority(target),)),
        tuple(citations or target.citations), tuple(parents), **kwargs)


def test_cross_system_derivation_preserves_context_authority_and_lineage():
    source = item()
    nlp = evidence(score=.9, attributes=(("entity_count", 2),))
    jepa = evidence("jepa-worldview", (nlp.evidence_id,), nlp.authorities,
                    nlp.citations, 120, 400, kind="semantic-fit")
    ledger = ContextEnrichmentLedger((jepa, nlp))
    view = project_context_enrichment(source, ledger, observed_at_ms=200)
    assert {x.producer for x in view.current} == {"nlp", "jepa-worldview"}
    assert view.item is source


def test_producer_cannot_reconsume_itself_anywhere_in_ancestry():
    root = evidence("nlp")
    bridge = evidence("worldview", (root.evidence_id,), root.authorities, root.citations,
                      110, 450)
    loop = evidence("nlp", (bridge.evidence_id,), bridge.authorities, bridge.citations,
                    120, 400)
    with pytest.raises(ValueError, match="own ancestry"):
        ContextEnrichmentLedger((root, bridge, loop))


@pytest.mark.parametrize("change,match", [
    ({"authorities": (EnrichmentAuthority("record", "other", "r1"),)}, "authority"),
    ({"citations": (ContextCitation("other", "other://1"),)}, "citation"),
    ({"observed": 99}, "predates"),
    ({"until": 501}, "outlives"),
])
def test_derived_evidence_cannot_weaken_parent_guarantees(change, match):
    parent = evidence()
    values = {"authorities": parent.authorities, "citations": parent.citations,
              "observed": 110, "until": 450}
    values.update(change)
    child = evidence("worldview", (parent.evidence_id,), **values)
    with pytest.raises(ValueError, match=match):
        ContextEnrichmentLedger((parent, child))


def test_append_is_deduplicated_and_order_independent():
    first = evidence("nlp")
    second = evidence("jepa")
    left = ContextEnrichmentLedger().append(first, second, first)
    right = ContextEnrichmentLedger().append(second, first)
    assert left == right
    assert len(left.evidence) == 2


def test_tombstone_is_append_only_producer_authorized_and_time_aware():
    value = evidence()
    tombstone = ContextEnrichmentTombstone("nlp", value.evidence_id,
                                          "source_deleted", 300, value.citations)
    ledger = ContextEnrichmentLedger((value,), (tombstone,))
    assert project_context_enrichment(item(), ledger, observed_at_ms=200).current == (value,)
    assert project_context_enrichment(item(), ledger, observed_at_ms=350).tombstoned == (value,)
    foreign = ContextEnrichmentTombstone("jepa", value.evidence_id,
                                        "invalidated", 300, value.citations)
    with pytest.raises(ValueError, match="producer"):
        ContextEnrichmentLedger((value,), (foreign,))


def test_tombstone_cannot_predate_or_strip_citations():
    value = evidence()
    with pytest.raises(ValueError, match="predates"):
        ContextEnrichmentLedger((value,), (ContextEnrichmentTombstone(
            "nlp", value.evidence_id, "invalidated", 99, value.citations),))
    with pytest.raises(ValueError, match="citation"):
        ContextEnrichmentLedger((value,), (ContextEnrichmentTombstone(
            "nlp", value.evidence_id, "invalidated", 200,
            (ContextCitation("other", "other://1"),)),))


def test_projection_requires_exact_context_revision_and_separates_stale():
    value = evidence(until=150)
    ledger = ContextEnrichmentLedger((value,))
    assert project_context_enrichment(item("r2"), ledger, observed_at_ms=120).current == ()
    assert project_context_enrichment(item(), ledger, observed_at_ms=200).stale == (value,)


@pytest.mark.parametrize("attributes", [
    (("content", "copied text"),), (("embedding", "1,2"),),
    (("secret_locator", "vault"),), (("safe", {"nested": "payload"}),),
])
def test_payload_bearing_or_nested_attributes_are_rejected(attributes):
    with pytest.raises(ValueError):
        evidence(attributes=attributes)


def test_lineage_depth_is_bounded():
    records = [evidence("p0")]
    for index in range(1, 17):
        parent = records[-1]
        records.append(evidence(f"p{index}", (parent.evidence_id,),
                                parent.authorities, parent.citations,
                                100 + index, 500 - index))
    with pytest.raises(ValueError, match="depth"):
        ContextEnrichmentLedger(tuple(records))


def test_contract_has_no_runtime_provider_model_store_or_clock_dependency():
    import vera.context_enrichment as module
    source = open(module.__file__, encoding="utf-8").read()
    for forbidden in ("requests", "httpx", "sqlite", "ollama", "torch", "time.time"):
        assert forbidden not in source
