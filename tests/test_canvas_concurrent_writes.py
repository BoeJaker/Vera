"""
Two writers, one canvas (B1; DEEP-29 11A.4 Decision 2): a canvas is saved whole, and two writes in flight at once each
saved what they had read - the later lost the earlier's change (2026-10-16: a lock and a pill sent together kept only
the pill). A save now merges over a document that moved on since it was read: each writer's own items win, the other's
are kept, the timeline and the removed list keep both.
"""
import asyncio

from test_canvas_resolver import C, ORCH, SID, _fresh, _run


class _SlowRedis(type(ORCH.REDIS)):
    """the fake store, with a yield in every call - so two writes really interleave"""
    async def get(self, k):
        await asyncio.sleep(0)
        return self.d.get(k)

    async def set(self, k, v, ex=None):
        await asyncio.sleep(0)
        self.d[k] = v


def _with_slow():
    slow = _SlowRedis()
    ORCH.REDIS = slow
    return slow


def _items():
    return {i["key"]: i for i in _run(C.cap_canvas_items(session_id=SID))["items"]}


def test_two_item_sets_at_once_both_land():
    _fresh(); _with_slow()
    for k in ("note:a", "note:b", "note:c"):
        _run(C.cap_canvas_add(session_id=SID, kind="note", content={"text": k}, key=k))

    async def both():
        return await asyncio.gather(
            C.cap_canvas_item_set(session_id=SID, key="note:a", place={"fx": 0.3, "fy": 0.2, "w": 300, "h": 200, "locked": True}),
            C.cap_canvas_item_set(session_id=SID, key="note:b", face="pill"),
            C.cap_canvas_item_set(session_id=SID, key="note:c", view="table"))
    rs = _run(both())
    assert all(r["ok"] for r in rs)
    it = _items()
    assert it["note:a"]["locked"] is True and it["note:a"]["place"]["w"] == 300
    assert it["note:b"]["face"] == "pill"
    assert it["note:c"]["view"] == "table"


def test_an_add_and_a_remove_at_once_keep_each_other():
    _fresh(); _with_slow()
    for k in ("note:x", "note:y"):
        _run(C.cap_canvas_add(session_id=SID, kind="note", content={"text": k}, key=k))

    async def both():
        return await asyncio.gather(
            C.cap_canvas_add(session_id=SID, kind="note", content={"text": "z"}, key="note:z"),
            C.cap_canvas_remove(session_id=SID, key="note:x"))
    _run(both())
    keys = set(_items())
    assert keys == {"note:y", "note:z"}, keys
    doc = _run(C._load(C._session_canvas_id(SID)))
    revs = [e["rev"] for e in doc["timeline"]]
    assert revs == sorted(revs) and len(revs) == len(set(revs)), revs
    assert any(r.get("key") == "note:x" for r in doc.get("removed") or [])


def test_the_merge_is_three_way():
    base_doc = {"id": "c", "revision": 3, "title": "t", "blocks": [{"key": "a", "v": 1}, {"key": "b", "v": 1}], "timeline": [{"rev": 3}]}
    base = C._snapshot(base_doc)
    mine = {"id": "c", "revision": 3, "title": "t", "blocks": [{"key": "a", "v": 2}, {"key": "b", "v": 1}, {"key": "n", "v": 1}], "timeline": [{"rev": 3}, {"rev": 4, "op": "mine"}]}
    cur = {"id": "c", "revision": 4, "title": "renamed", "blocks": [{"key": "a", "v": 1}, {"key": "b", "v": 9}], "timeline": [{"rev": 3}, {"rev": 4, "op": "theirs"}]}
    m = C._merge(cur, mine, base)
    assert m["blocks"] == [{"key": "a", "v": 2}, {"key": "b", "v": 9}, {"key": "n", "v": 1}]
    assert m["title"] == "renamed" and m["revision"] == 4
    assert [e.get("op") for e in m["timeline"]] == [None, "theirs", "mine"]
