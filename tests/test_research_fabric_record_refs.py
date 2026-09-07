import pytest

from Vera.vera.research import research_fabric


pytestmark = pytest.mark.critical


class _Fabric:
    async def _sqlite_query(self, *, dataset_id, limit):
        assert dataset_id == "research.citations"
        assert limit == 5000
        return [
            {"id": "physical-b", "data": '{"id":"logical-b","text":"secret"}'},
            {"id": "physical-a", "data": '{"id":"logical-a","text":"secret"}'},
        ]


@pytest.mark.asyncio
async def test_record_ref_resolution_preserves_order_without_content(monkeypatch):
    monkeypatch.setattr(research_fabric, "_fabric", lambda: _Fabric())
    refs = await research_fabric.resolve_record_refs(
        "research.citations", ["logical-a", "missing", "logical-b"])
    assert refs == [
        {"dataset_id": "research.citations", "record_id": "physical-a",
         "logical_id": "logical-a"},
        {"dataset_id": "research.citations", "record_id": "physical-b",
         "logical_id": "logical-b"},
    ]
    assert "secret" not in str(refs)


@pytest.mark.asyncio
async def test_record_ref_resolution_fails_closed_when_fabric_is_unavailable(monkeypatch):
    monkeypatch.setattr(research_fabric, "_fabric", lambda: None)
    assert await research_fabric.resolve_record_refs("research.jobs", ["job-1"]) == []
