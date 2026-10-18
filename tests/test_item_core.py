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


def test_a_parked_item_reads_as_a_pill_whatever_its_stored_face():
    # WSU-58 S2: parking wins over a stored face - a parked item whose record says window is a pill on the shelf
    assert IC.face_of({"face": "window", "state": "parked"}) == "pill"
    assert IC.normalise({"key": "k", "face": "full", "state": "parked"})["face"] == "pill"
    assert IC.face_of({"face": "window", "state": "now"}) == "window"


def test_state_moves_take_the_face_s2s_transition_gives():
    b = {"key": "k", "face": "window", "state": "now"}
    changed, prev, probs = IC.apply_set(b, {"state": "parked"})
    assert not probs and changed == {"state": "parked", "face": "pill"} and prev == {"state": "now", "face": "window"}
    changed, prev, _ = IC.apply_set(b, {"state": "now"})
    assert changed == {"state": "now", "face": "tile"} and prev == {"state": "parked", "face": "pill"}
    # a face asked with the move is the person's own: it stands
    b = {"key": "k", "face": "pill", "state": "parked"}
    changed, _, _ = IC.apply_set(b, {"state": "now", "face": "window"})
    assert changed == {"state": "now", "face": "window"}
    # the server's helper agrees with transition('park' / 'unpark' / 'pin' from the shelf)
    assert IC.face_for_state({"state": "now", "face": "window"}, "parked") == "pill"
    assert IC.face_for_state({"state": "parked", "face": "window"}, "now") == "tile"
    assert IC.face_for_state({"state": "parked"}, "pinned") == "tile"
    assert IC.face_for_state({"state": "now", "face": "window"}, "pinned") is None
    if hasattr(IC, "transition"):                     # S2 (feat/wsu58-s2-faces) once it is in the base
        for st, ev in (("now", "park"), ("parked", "unpark"), ("parked", "pin")):
            s = IC.transition({"state": st, "face": "window"}, ev)
            assert s["face"] == IC.face_for_state({"state": st, "face": "window"}, s["state"]), (st, ev, s)


def test_park_then_bring_back_gives_a_pill_then_a_tile():
    _fresh()
    _add("note:p")
    _run(C.cap_canvas_item_set(session_id=SID, key="note:p", face="window"))
    r = _run(C.cap_canvas_park(session_id=SID, key="note:p"))
    assert r["ok"] and r["item"]["face"] == "pill"
    items = _run(C.cap_canvas_items(session_id=SID, state="parked"))["items"]
    assert [i["face"] for i in items] == ["pill"]
    r = _run(C.cap_canvas_add(session_id=SID, key="note:p"))
    assert r["resolved"] == "shown" and r["item"]["state"] == "now" and r["item"]["face"] == "tile"
    # pinned straight off the shelf: a tile too
    _run(C.cap_canvas_item_set(session_id=SID, key="note:p", face="window"))
    _run(C.cap_canvas_park(session_id=SID, key="note:p"))
    r = _run(C.cap_canvas_pin(session_id=SID, key="note:p"))
    assert r["item"]["state"] == "pinned" and r["item"]["face"] == "tile"
    # a shown item that was never parked keeps its face
    _run(C.cap_canvas_item_set(session_id=SID, key="note:p", face="window"))
    assert _run(C.cap_canvas_add(session_id=SID, key="note:p"))["item"]["face"] == "window"


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


# ── WSU-58 S7: max, layers, groups and kept windows live in the item record ─────────────────────────────────────
def test_s7_the_four_states_round_trip_through_canvas_item_set_and_canvas_items():
    _fresh()
    for k in ("note:m", "note:w", "records:g", "records:j", "panel:l"):
        _add(k)
    r = _run(C.cap_canvas_item_set(session_id=SID, key="note:m", face="full", back="tile"))
    assert r["ok"] and r["item"]["face"] == "full" and r["item"]["back"] == "tile"
    r = _run(C.cap_canvas_item_set(session_id=SID, key="note:w", face="window", dock="tile",
                                   place={"fx": 0.4, "fy": 0.4, "w": 640, "h": 420, "by": "person"}))
    assert r["ok"] and r["item"]["dock"] == "tile" and r["item"]["place"]["w"] == 640
    r = _run(C.cap_canvas_item_set(session_id=SID, key="records:g", group={"sep": True, "at": 1760000000123}))
    assert r["ok"] and r["item"]["group"] == {"front": False, "at": 1760000000123, "sep": True}
    r = _run(C.cap_canvas_item_set(session_id=SID, key="records:j", group='{"id": "records:g", "front": true, "at": 5}'))
    assert r["ok"] and r["item"]["group"] == {"id": "records:g", "front": True, "at": 5}
    r = _run(C.cap_canvas_item_set(session_id=SID, key="panel:l", place={"layer": "behind"}))
    assert r["ok"] and r["item"]["place"]["layer"] == "behind"
    items = {i["key"]: i for i in _run(C.cap_canvas_items(session_id=SID))["items"]}
    assert (items["note:m"]["face"], items["note:m"]["back"]) == ("full", "tile")
    assert (items["note:w"]["face"], items["note:w"]["dock"]) == ("window", "tile")
    assert items["records:g"]["group"]["sep"] is True and items["records:j"]["group"]["id"] == "records:g"
    assert items["panel:l"]["place"]["layer"] == "behind"
    # restored: the face it came back to, back cleared - and the raw block says so (a reader can tell "restored" from "never set")
    r = _run(C.cap_canvas_item_set(session_id=SID, key="note:m", face="tile", back=""))
    assert r["ok"] and r["item"]["face"] == "tile" and r["item"]["back"] == "" and r["changed"] == {"face": "tile", "back": ""}
    doc = _run(C.cap_canvas_add(session_id=SID, key="note:m"))
    assert doc["item"]["back"] == ""
    # sent back to the desk's own grouping
    r = _run(C.cap_canvas_item_set(session_id=SID, key="records:g", group={}))
    assert r["ok"] and r["item"]["group"] == {}


def test_s7_bad_back_dock_and_groups_are_refused_and_write_nothing():
    _fresh()
    rev = _add("note:x")["revision"]
    for kw in ({"back": "sideways"}, {"dock": "full"}, {"group": {"front": True}}, {"group": {"sep": True, "at": "soon"}}, {"group": "loose"}):
        r = _run(C.cap_canvas_item_set(session_id=SID, key="note:x", **kw))
        assert not r["ok"], kw
    assert _run(C.cap_canvas_items(session_id=SID))["revision"] == rev


def test_s7_a_parked_full_item_reads_as_a_pill_with_no_back():
    rec = IC.normalise({"key": "k", "face": "full", "back": "window", "dock": "tile", "state": "parked"})
    assert (rec["face"], rec["back"], rec["dock"]) == ("pill", "", "")
    assert IC.face_state({"face": "full", "back": "window", "dock": "tile"}) == {
        "face": "full", "state": "now", "locked": False, "back": "window", "dock": "tile", "pilled": False}
