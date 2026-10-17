"""
The item contract (DEEP-29 11A.4, owner decision D1 2026-10-16): the item record every surface reads
(vera/canvas/item_core.py) and canvas.item.set / canvas.items, which write and read it on the session canvas.

The cases in tests/fixtures/item_contract_cases.json are shared with the JS half (tests/test_vera_item.cjs), so the
browser and the server give one answer.
"""
import copy
import importlib.util
import json
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
CASES = json.load(open(os.path.join(ROOT, "tests", "fixtures", "item_contract_cases.json"), encoding="utf-8"))


def _core():
    spec = importlib.util.spec_from_file_location("item_core_under_test", os.path.join(ROOT, "vera", "canvas", "item_core.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


IC = _core()


def test_normalise_cases():
    for c in CASES["normalise"]:
        got = IC.normalise(copy.deepcopy(c["block"]))
        if "want" in c:
            assert got == c["want"], c["name"]
        for k, v in (c.get("want_fields") or {}).items():
            assert got[k] == v, (c["name"], k, got[k])


def test_apply_set_cases():
    for c in CASES["apply_set"]:
        block = copy.deepcopy(c["block"])
        before = copy.deepcopy(block)
        changed, prev, probs = IC.apply_set(block, c["fields"])
        assert {"changed": changed, "prev": prev, "problems": probs} == c["want"], c["name"]
        if probs:
            assert block == before, c["name"] + ": a refused call must not write"
        for k, v in changed.items():
            assert block[k] == v


def test_every_record_has_every_field():
    keys = set(CASES["normalise"][0]["want"])
    for c in CASES["normalise"]:
        assert set(IC.normalise(c["block"])) == keys, c["name"]


# ── the capabilities, against the canvas module on its stub orchestrator ─────────────────────────────────────────
from test_canvas_resolver import C, SID, _fresh, _run, _updates  # noqa: E402


def _add(key, **kw):
    return _run(C.cap_canvas_add(session_id=SID, kind=kw.pop("kind", "note"), content=kw.pop("content", {"text": key}), key=key, **kw))


def test_item_set_writes_one_event_naming_the_fields():
    _fresh()
    _add("note:a")
    n = len(_updates())
    r = _run(C.cap_canvas_item_set(session_id=SID, key="note:a", face="window", place={"fx": 0.5, "fy": 0.4, "w": 420, "h": 260, "by": "person"}))
    assert r["ok"] and set(r["changed"]) == {"face", "place"}
    assert r["prev"]["face"] == "tile"
    ev = _updates()[n:]
    assert len(ev) == 1 and ev[0]["op"] == "item" and ev[0]["key"] == "note:a" and set(ev[0]["fields"]) == {"face", "place"}
    assert r["item"]["face"] == "window" and r["item"]["place"]["w"] == 420


def test_item_set_that_changes_nothing_writes_nothing():
    _fresh()
    rev = _add("note:b")["revision"]
    n = len(_updates())
    r = _run(C.cap_canvas_item_set(session_id=SID, key="note:b", face="tile"))
    assert r["ok"] and r["changed"] == {} and len(_updates()) == n and r["revision"] == rev


def test_item_set_refuses_bad_values_and_unknown_keys():
    _fresh()
    _add("note:c")
    assert not _run(C.cap_canvas_item_set(session_id=SID, key="note:c", face="blob"))["ok"]
    assert not _run(C.cap_canvas_item_set(session_id=SID, key="nope", face="pill"))["ok"]


def test_items_reads_records_and_honours_if_rev():
    _fresh()
    _add("note:d")
    _add("note:e", at="parked")
    r = _run(C.cap_canvas_items(session_id=SID))
    assert r["ok"] and [i["key"] for i in r["items"]] == ["note:d", "note:e"]
    assert [i["face"] for i in r["items"]] == ["tile", "pill"]
    assert _run(C.cap_canvas_items(session_id=SID, if_rev=r["revision"])).get("unchanged") is True
    assert [i["key"] for i in _run(C.cap_canvas_items(session_id=SID, state="parked"))["items"]] == ["note:e"]


def test_the_item_view_carries_the_contract_fields_once_set():
    _fresh()
    _add("note:f")
    _run(C.cap_canvas_item_set(session_id=SID, key="note:f", view="cards", folded=True))
    doc = _run(C.cap_canvas_add(session_id=SID, key="note:f"))
    assert doc["item"]["view"] == "cards" and doc["item"]["folded"] is True


def test_every_view_a_body_offers_is_written_and_an_unknown_one_refused():
    # WSU-58 S1: the JSON lens (tree / flat / validate) and an app's own drawn view were refused by canvas.item.set
    for i, v in enumerate(("tree", "flat", "validate", "drawn")):
        _fresh()
        _add(f"note:v{i}")
        r = _run(C.cap_canvas_item_set(session_id=SID, key=f"note:v{i}", view=v))
        assert r["ok"] and r["item"]["view"] == v, (v, r)
    _fresh()
    _add("note:vx")
    r = _run(C.cap_canvas_item_set(session_id=SID, key="note:vx", view="radar"))
    assert not r["ok"] and "view must be one of" in r["error"]
