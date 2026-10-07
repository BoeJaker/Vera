import pytest
from types import SimpleNamespace

from vera.agents.rag_context_adapter import (
    MAX_AUTHORITY_CHARS, MAX_HITS, MAX_TEXT_CHARS, project_agent_rag_batch,
    project_agent_rag_results,
)


pytestmark = pytest.mark.critical


def project(rows):
    return project_agent_rag_results(
        rows, provider_id="agent-rag", token_counter=lambda text: len(text.split()))


def test_exact_native_authority_becomes_cited_portable_context():
    items = project([{"dataset": "docs", "id": "record-1",
                      "revision_id": "rev_abc", "text": "bounded cited text",
                      "score": 0.75}])
    assert len(items) == 1
    item = items[0]
    assert (item.source, item.revision, item.provider) == (
        "record-1", "rev_abc", "agent-rag")
    assert item.citations[0].locator == \
        "fabric://docs/record-1?revision=rev_abc"


def test_projection_receipt_is_deterministic_and_payload_free():
    row = {"dataset": "docs", "id": "record-1", "revision_id": "rev_abc",
           "text": "secret source payload", "score": 0.5}
    first = project_agent_rag_batch(
        [row], provider_id="agent-rag", token_counter=lambda text: 3)
    replay = project_agent_rag_batch(
        [dict(row)], provider_id="agent-rag", token_counter=lambda text: 3)
    assert first.receipt == replay.receipt
    encoded = str(first.receipt.to_dict())
    assert "secret source payload" not in encoded
    assert first.receipt.item_identities == (("docs", "record-1", "rev_abc"),)


@pytest.mark.parametrize("missing", ["dataset", "id", "revision_id", "text"])
def test_missing_authority_or_payload_fails_closed(missing):
    row = {"dataset": "docs", "id": "record-1", "revision_id": "rev_abc",
           "text": "text", "score": 0.5}
    row.pop(missing)
    with pytest.raises(ValueError, match="present|bounded text"):
        project([row])


def test_duplicates_scores_bounds_and_token_counts_fail_closed():
    row = {"dataset": "docs", "id": "record-1", "revision_id": "rev_abc",
           "text": "text", "score": 0.5}
    with pytest.raises(ValueError, match="unique"):
        project([row, row])
    with pytest.raises(ValueError, match="score"):
        project([{**row, "score": float("nan")}])
    with pytest.raises(ValueError, match="at most"):
        project([{**row, "id": str(index)} for index in range(MAX_HITS + 1)])
    with pytest.raises(ValueError, match="token_count"):
        project_agent_rag_results([row], provider_id="agent-rag",
                                  token_counter=lambda text: 0)
    with pytest.raises(ValueError, match="bounded"):
        project([{**row, "id": "x" * (MAX_AUTHORITY_CHARS + 1)}])
    with pytest.raises(ValueError, match="bounded text"):
        project([{**row, "text": "x" * (MAX_TEXT_CHARS + 1)}])


def test_citation_locator_escapes_authority_components():
    item = project([{"dataset": "docs/private", "id": "record?one",
                     "revision_id": "rev#abc", "text": "text", "score": 0.5}])[0]
    assert item.citations[0].locator == \
        "fabric://docs%2Fprivate/record%3Fone?revision=rev%23abc"


def test_adapter_has_no_runtime_or_fabric_query_dependency():
    import vera.agents.rag_context_adapter as module
    source = open(module.__file__, encoding="utf-8").read()
    assert "agents.py" not in source
    assert "data_fabric" not in source
    assert "fabric.query" not in source


@pytest.mark.asyncio
async def test_native_agent_rag_preserves_fabric_record_authority(monkeypatch):
    from vera.agents import agents

    async def call(name, **kwargs):
        assert name == "fabric.query"
        assert kwargs["include_revision_authority"] is True
        return {"results": [{"id": "record-1", "revision_id": "rev_abc",
                              "summary": "authoritative text", "score": 0.75}]}

    monkeypatch.setattr(agents, "_call_registered_cap", call)
    record = SimpleNamespace(rag_inject_limit=4, rag_dataset="docs",
                             knowledge_sources=[{"type": "web"}])
    rows = await agents.agent_rag_retrieve(record, "question")
    assert rows == [{"dataset": "docs", "record_id": "record-1",
                     "revision_id": "rev_abc", "text": "authoritative text",
                     "score": 0.75}]
