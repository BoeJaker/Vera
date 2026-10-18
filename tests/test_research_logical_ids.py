"""S8 / D18-1: research loads find records by the logical id they were saved under.

_query_by_id compared the physical fabric row id with the research id (a cell id, an iteration target, "singleton"),
so every load returned None: notebook saves 404, iterations stopped at once, the web config was lost on restart.
Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import json
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.research.research_fabric as rf  # noqa: E402  (fails loudly, never skips)


def fake_fabric(rows):
    async def _sqlite_query(dataset_id="", limit=50, offset=0):
        return [r for r in rows if r["dataset_id"] == dataset_id][:limit]   # newest first, like the real one
    return types.SimpleNamespace(_sqlite_query=_sqlite_query)


def row(phys, ds, data):
    return {"id": phys, "dataset_id": ds, "data": json.dumps(data)}


@pytest.fixture
def fabric(monkeypatch):
    rows = [row("phys-3", rf.DATASET_NOTEBOOK_CELLS, {"id": "cell-1", "text": "newest", "thread": "[]"}),
            row("phys-2", rf.DATASET_NOTEBOOK_CELLS, {"id": "cell-1", "text": "older", "thread": "[]"}),
            row("phys-1", rf.DATASET_ITERATION_TARGETS, {"id": "it-9", "traversal_map": "{}", "status": "active"}),
            row("phys-0", rf.DATASET_WEB_CONFIG, {"id": "singleton", "engine": "searxng"})]
    monkeypatch.setattr(rf, "_fabric", lambda: fake_fabric(rows))
    return rows


def test_a_cell_loads_by_its_id_and_the_newest_save_wins(fabric):
    cell = asyncio.run(rf.DB.load_cell("cell-1"))
    assert cell is not None and cell["text"] == "newest" and cell["id"] == "cell-1"


def test_an_iteration_target_and_the_singleton_config_load(fabric):
    it = asyncio.run(rf.DB.load_iteration_target("it-9"))
    assert it is not None and it["status"] == "active"
    cfg = asyncio.run(rf._query_by_id(rf.DATASET_WEB_CONFIG, "singleton"))
    assert cfg is not None and cfg["engine"] == "searxng"


def test_missing_ids_and_physical_ids(fabric):
    assert asyncio.run(rf._query_by_id(rf.DATASET_NOTEBOOK_CELLS, "nope")) is None
    assert asyncio.run(rf._query_by_id(rf.DATASET_NOTEBOOK_CELLS, "")) is None
    by_phys = asyncio.run(rf._query_by_id(rf.DATASET_NOTEBOOK_CELLS, "phys-2"))
    assert by_phys is not None and by_phys["text"] == "older"
