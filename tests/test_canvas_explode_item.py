# -*- coding: utf-8 -*-
"""The canvas explode item's binding (EXPLODE.md §8.3): "the explode mode in the canvas doesnt draw anything"
(owner, 2026-09-22). It never had. An explode item binds to a code item BY KEY, and `canvas.append` -- the obvious
way to put both on a canvas -- had no `key` parameter at all: the key a caller passed was dropped, the binding
could never resolve, the item asked the server for nothing and the slot stayed empty.

The store is Redis, which a test container has no business needing: these drive the capability over an in-memory
one, because what is being pinned is the KEY logic, not the persistence.
"""
import asyncio
import json

import pytest

from vera.canvas import canvas_capabilities as CV


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def canvas(monkeypatch):
    store = {"cv_t": {"id": "cv_t", "title": "t", "mode": "static", "blocks": [], "revision": 0}}

    async def load(cid):
        d = store.get(cid)
        return json.loads(json.dumps(d)) if d else None

    async def save(doc):
        store[doc["id"]] = json.loads(json.dumps(doc))

    async def emit(*a, **k):
        return None

    monkeypatch.setattr(CV, "_load", load)
    monkeypatch.setattr(CV, "_save", save)
    monkeypatch.setattr(CV, "_emit", emit)
    return "cv_t"


def test_append_keeps_the_key_it_is_given_so_one_item_can_name_another(canvas):
    a = run(CV.cap_canvas_append(id=canvas, type="code", key="src",
                                 content={"code": "def f():\n    return 1\n", "lang": "python"}))
    assert not a.get("error")
    run(CV.cap_canvas_append(id=canvas, type="explode", key="xp", content={"binds": "src"}))
    doc = run(CV.cap_canvas_get(id=canvas))
    assert [b.get("key") for b in doc["blocks"]] == ["src", "xp"]
    xp = next(b for b in doc["blocks"] if b["type"] == "explode")
    # the binding resolves to an item that is really there -- which is the whole of what was missing
    assert any(b.get("key") == xp["content"]["binds"] for b in doc["blocks"])


def test_a_key_already_on_the_canvas_is_refused_rather_than_doubled(canvas):
    run(CV.cap_canvas_append(id=canvas, type="note", key="k", content={"text": "one"}))
    out = run(CV.cap_canvas_append(id=canvas, type="note", key="k", content={"text": "two"}))
    assert out.get("error") and "already" in out["error"]
    doc = run(CV.cap_canvas_get(id=canvas))
    assert len([b for b in doc["blocks"] if b.get("key") == "k"]) == 1


def test_a_block_without_a_key_is_still_fine_and_still_has_its_id(canvas):
    run(CV.cap_canvas_append(id=canvas, type="note", content={"text": "no key"}))
    doc = run(CV.cap_canvas_get(id=canvas))
    b = doc["blocks"][0]
    assert "key" not in b and b["id"].startswith("bk")


def test_the_explode_block_still_takes_what_it_always_did(canvas):
    for content in ({"binds": "src"}, {"path": "vera/research/assess_core.py", "depth": 1},
                    {"code": "def f(): pass", "lang": "python"}, {"text": "a passage"},
                    {"record": "rec-1", "ranges": [[0, 10]]}):
        out = run(CV.cap_canvas_append(id=canvas, type="explode", content=content))
        assert not out.get("error"), (content, out)
    doc = run(CV.cap_canvas_get(id=canvas))
    assert len([b for b in doc["blocks"] if b["type"] == "explode"]) == 5
