import asyncio
from dataclasses import dataclass

import pytest

from vera.discovery_operator_readmodel import DiscoveryOperatorLedger
from vera.research.discovery_shadow import (
    SHADOW_SCHEMA,
    adapt_research_citations,
    observe_research_citations,
    record_research_citations_shadow,
)


@dataclass
class CitationStub:
    id: str
    url: str
    title: str
    snippet: str
    full_text: str = ""
    screenshot_path: str = ""
    rank_score: float = 0.0


def _citations():
    return [
        CitationStub("a", "https://example.test/a?q=secret", "A title", "A snippet",
                     full_text="Complete body A", screenshot_path="a.png", rank_score=0.8),
        CitationStub("b", "https://example.test/b", "B title", "Complete body B"),
        CitationStub("bad", "javascript:alert(1)", "bad", "not portable"),
    ]


def test_existing_citations_become_ephemeral_portable_context_without_callbacks():
    items = adapt_research_citations(_citations())
    assert len(items) == 2
    assert items[0].provider == "research.native.web"
    assert items[0].text == "Complete body A"
    assert items[0].citations[0].locator == "https://example.test/a?q=secret"
    assert items[0].revision.startswith("sha256:")


def test_shadow_projection_is_payload_free_and_truthful_about_missing_route_evidence():
    items, observation = observe_research_citations("private query text", _citations())
    projected = DiscoveryOperatorLedger().record_research_shadow(observation)
    encoded = repr(projected)
    assert projected["schema"] == SHADOW_SCHEMA
    assert projected["kind"] == "research_shadow"
    assert projected["native_result_count"] == 3
    assert projected["portable_context_count"] == len(items) == 2
    assert projected["excluded_count"] == 1
    assert projected["full_text_count"] == 1
    assert projected["screenshot_count"] == 1
    assert projected["execution_authority"] == "research.native"
    assert projected["worker_evidence_available"] is False
    assert projected["route_evidence_available"] is False
    assert projected["consumer_routing_changed"] is False
    assert projected["duplicate_provider_calls"] is False
    assert projected["payloads_included"] is False
    for secret in ("private query text", "Complete body A", "A title",
                   "https://example.test/a?q=secret"):
        assert secret not in encoded


def test_operator_ledger_bounds_and_reports_research_shadows():
    ledger = DiscoveryOperatorLedger(capacity=2)
    for query in ("one", "two", "three"):
        _, observation = observe_research_citations(query, _citations()[:1])
        ledger.record_research_shadow(observation)
    snapshot = ledger.snapshot()
    assert snapshot["counts"]["research_shadows"] == 2
    assert len(snapshot["research_shadows"]) == 2
    assert snapshot["payloads_included"] is False
    assert snapshot["control_authority"] is False


def test_adapter_never_performs_async_or_network_work(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("adapter attempted to schedule work")
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    monkeypatch.setattr(asyncio, "run", forbidden)
    items, observation = observe_research_citations("query", _citations())
    assert len(items) == observation.portable_context_count


def test_best_effort_hook_preserves_exact_native_results_when_recording_fails():
    citations = _citations()
    errors = []
    returned = record_research_citations_shadow(
        "query", citations,
        lambda _observation: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
        on_error=errors.append,
    )
    assert returned is citations
    assert len(errors) == 1
    assert str(errors[0]) == "ledger unavailable"


def test_invalid_input_is_bounded():
    with pytest.raises(ValueError, match="query is required"):
        observe_research_citations("", [])
    with pytest.raises(ValueError, match="citation limit"):
        adapt_research_citations([{}] * 1001)


def test_operator_projection_rejects_payload_disguised_as_identity():
    _, observation = observe_research_citations("query", _citations()[:1])
    forged = observation.to_dict()
    forged["portable_item_ids"] = ["raw secret payload"]
    with pytest.raises(ValueError, match="portable identities"):
        DiscoveryOperatorLedger().record_research_shadow(forged)


@pytest.mark.critical
def test_shadow_contract_keeps_native_authority_and_no_duplicate_calls():
    _, observation = observe_research_citations("query", _citations())
    assert observation.execution_authority == "research.native"
    assert not observation.consumer_routing_changed
    assert not observation.duplicate_provider_calls
