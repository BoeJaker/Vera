"""S18 / D11-1 + D11-2: ML modules save, edit and delete for real, and the OHLCV task trains forward in time.

Every save appended a fabric row with a random id: an edit never replaced the old copy, `ml.list` could bring the old
one back, and `ml.delete` (by id) matched no row, so deleted modules returned. And the fabric returns rows newest
first, so ml.data.prepare(ohlcv_direction) built the series backwards and split it at random. Fabric and Redis are
faked; the modules load from their files like test_ml_workshop_pool."""
import asyncio
import importlib.util
import json
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical
np = pytest.importorskip("numpy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML = os.path.join(ROOT, "vera", "machine learning")


def _load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, os.path.join(ML, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeFabric(types.ModuleType):
    """ingest_dataset with the `_id` keyed upsert, _sqlite_query newest first, _enqueue_write for raw DELETEs."""

    def __init__(self):
        super().__init__("fake_fabric")
        self.rows, self.n = [], 0

    async def ingest_dataset(self, dataset_id="", data=None, source="", tags=None, **_):
        for item in data or []:
            item = dict(item)
            rid = item.pop("_id", None)
            self.n += 1
            if not rid:
                rid = f"rand-{self.n}"
            self.rows = [r for r in self.rows if r["id"] != rid]
            self.rows.append({"id": rid, "dataset_id": dataset_id, "seq": self.n,
                              "data": json.dumps({"text": item.get("text", ""), "data": item})})

    async def _sqlite_query(self, dataset_id="", limit=50, **_):
        rows = [r for r in self.rows if r["dataset_id"] == dataset_id]
        return sorted(rows, key=lambda r: -r["seq"])[:limit]

    async def _enqueue_write(self, op, wait=False):
        if op.get("sql", "").startswith("DELETE FROM fabric_records"):
            ds, rid = op["params"]
            self.rows = [r for r in self.rows if not (r["dataset_id"] == ds and r["id"] == rid)]


class FakeRedis:
    def __init__(self):
        self.kv = {}

    async def set(self, k, v, ex=None):
        self.kv[k] = v

    async def get(self, k):
        return self.kv.get(k)

    async def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)

    async def scan_iter(self, match="*", count=None):
        pre = match.rstrip("*")
        for k in list(self.kv):
            if k.startswith(pre):
                yield k

    async def keys(self, pattern):                     # the old path: must not be needed any more
        raise AssertionError("KEYS used")


@pytest.fixture
def ml(monkeypatch):
    fab = FakeFabric()
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.data_fabric", fab)
    import Vera.vera.capability_orchestration as orch
    r = FakeRedis()
    monkeypatch.setattr(orch, "REDIS", r)
    mw = _load("ml_workshop")
    mw._MODULES.clear()
    return mw, fab, r


def _module(mid, name, ts):
    return {"id": mid, "name": name, "nodes": [{"id": "d"}], "edges": [], "updated_at": ts}


def test_an_edit_replaces_the_module_and_a_delete_removes_it(ml):
    mw, fab, r = ml

    async def run():
        await mw._save_module("m1", _module("m1", "first", "2026-10-18T10:00:00"))
        await mw._save_module("m1", _module("m1", "edited", "2026-10-18T11:00:00"))
        assert len([x for x in fab.rows if x["dataset_id"] == "ml.modules"]) == 1      # replaced, not appended
        mw._MODULES.clear()
        await mw._load_all_modules()
        assert mw._MODULES["m1"]["name"] == "edited"
        # a copy an older save appended with a random id is removed by the delete too
        await fab.ingest_dataset("ml.modules", [dict(_module("m1", "ancient", "2026-01-01T00:00:00"), _record_id="m1")])
        removed = await mw._fabric_delete("ml.modules", "m1")
        await r.delete(mw.REDIS_MODULE_PREFIX + "m1")
        mw._MODULES.clear()
        await mw._load_all_modules()
        return removed

    removed = asyncio.run(run())
    assert removed == 2 and "m1" not in mw._MODULES


def test_the_newest_copy_wins_whatever_the_row_order(ml):
    mw, fab, r = ml

    async def run():
        # legacy duplicates (random ids): the newer one was stored FIRST
        await fab.ingest_dataset("ml.modules", [dict(_module("m2", "new", "2026-10-18T12:00:00"), _record_id="m2")])
        await fab.ingest_dataset("ml.modules", [dict(_module("m2", "old", "2026-10-01T12:00:00"), _record_id="m2")])
        await mw._load_all_modules()

    asyncio.run(run())
    assert mw._MODULES["m2"]["name"] == "new"


def test_module_cache_keys_are_their_own_prefix(ml):
    mw, fab, r = ml
    r.kv["vera:ml:weights:x"] = "{}"                     # never read by a module load
    asyncio.run(mw._save_module("m3", _module("m3", "x", "2026-10-18T10:00:00")))
    assert mw.REDIS_MODULE_PREFIX + "m3" in r.kv


def test_ohlcv_bars_go_oldest_first_and_split_by_time():
    mt = _load("ml_training")
    rows = [{"created_at": f"2026-01-{d:02d}"} for d in range(1, 4)]
    bars = [({"date": "2026-01-03"}, rows[2]), ({"date": "2026-01-01"}, rows[0]), ({"date": "2026-01-02"}, rows[1])]
    bars.sort(key=lambda dr: mt._bar_time(*dr))
    assert [d["date"] for d, _ in bars] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert mt._bar_time({"t": 946684800000}, {}) == 946684800.0          # ms (2000-01-01) read as ms
    X = np.arange(100, dtype=np.float32).reshape(-1, 1)
    y = np.arange(100, dtype=np.float32)
    Xtr, ytr, Xva, yva, Xte, yte = mt._chronological_split(X, y, val=0.15, test=0.15, gap=5)
    assert ytr.max() < yva.min() - 5 + 1 and yva.max() < yte.min() - 5 + 1     # train < val < test, gaps between
    assert len(Xte) == 15 and len(Xva) == 15 and len(Xtr) == 60
