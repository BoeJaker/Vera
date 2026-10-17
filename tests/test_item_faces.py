"""
How an item changes face (DEEP-29 11A.4 Decision 3; WSU-58 S2): vera/canvas/item_core.transition, one pure rule for every
kind - pill, tile, window, full; locked; parked - read off what the desk element does today. Owner O2 (2026-10-17): a
window stays until it is sent back.

The cases in tests/fixtures/item_face_cases.json are shared with the JS half (VeraItem.transition, tests/test_item_faces.cjs,
which also drives the element itself through the faces for every BLOCK kind). 'grid' is every face state x every event,
generated from this half; after a deliberate change of the rule, regenerate it with
    python tests/test_item_faces.py --write
and say in the commit what changed.
"""
import copy
import importlib.util
import itertools
import json
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
FIX = os.path.join(ROOT, "tests", "fixtures", "item_face_cases.json")


def _core():
    spec = importlib.util.spec_from_file_location("item_core_faces_under_test", os.path.join(ROOT, "vera", "canvas", "item_core.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


IC = _core()
EXTRA = [{"type": "drag", "to": "pills"}, {"type": "open", "arrange": "slots"}]


def _t(fs):
    return [fs["face"], fs["state"], int(fs["locked"]), fs["back"], fs["dock"], int(fs["pilled"])]


def _starts():
    out, seen = [], set()
    for face, state, locked, back, dock, pilled in itertools.product(IC.FACES, ("now", "parked", "hidden"), (False, True),
                                                                   ("", "pill", "tile", "window"), ("", "tile", "pill"), (False, True)):
        fs = IC.face_state({"face": face, "state": state, "locked": locked, "back": back, "dock": dock, "pilled": pilled})
        k = json.dumps(fs, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(fs)
    return out


def grid():
    return [[_t(fs), ev, _t(IC.transition(fs, ev))] for fs in _starts() for ev in list(IC.EVENTS) + EXTRA]


def _load():
    return json.load(open(FIX, encoding="utf-8"))


def test_the_written_cases():
    for c in _load()["cases"]:
        item = copy.deepcopy(c["item"])
        got = IC.transition(item, c["event"])
        assert item == c["item"], c["name"] + ": transition must not change the item"
        for k, v in c["want"].items():
            assert got[k] == v, (c["name"], k, got)


def test_the_grid_is_this_rule():
    assert _load()["grid"] == grid(), "the rule changed: regenerate with python tests/test_item_faces.py --write"


def test_every_answer_is_a_face_state_and_the_rule_is_closed():
    for fs in _starts():
        for ev in list(IC.EVENTS) + EXTRA:
            out = IC.transition(fs, ev)
            assert IC.face_state(out) == out, (fs, ev, out)
            assert out["face"] in IC.FACES and out["state"] in IC.STATES


def test_blur_and_focus_never_change_a_face():
    for fs in _starts():
        assert IC.transition(fs, "blur") == fs and IC.transition(fs, "focus") == fs


def test_a_locked_item_takes_nothing_from_the_layout_and_is_not_moved():
    for fs in _starts():
        if fs["locked"]:
            for ev in IC.LAYOUT_EVENTS + ("drag", "resize_end", "pill", "unpill", "open"):
                assert IC.transition(fs, ev) == fs, (fs, ev)


def test_max_then_restore_gives_back_every_face_it_can_fill_from():
    for fs in _starts():
        if fs["state"] in ("now", "pinned") and fs["face"] != "full":
            full = IC.transition(fs, "max")
            assert full["face"] == "full" and full["back"] == fs["face"]
            assert IC.transition(full, "restore") == fs and IC.transition(full, "max") == fs


def test_a_window_goes_only_when_sent_back():
    w = IC.face_state({"face": "window", "dock": "tile"})
    stays = [e for e in IC.EVENTS if IC.transition(w, e)["face"] == "window"]
    assert set(IC.EVENTS) - set(stays) == {"pill", "restore", "send_back", "resize_end", "park", "max"}


if __name__ == "__main__" and "--write" in sys.argv:
    doc = _load()
    doc["grid"] = grid()
    s = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    open(FIX, "w", encoding="utf-8").write(s.replace('},{"name"', '},\n{"name"').replace('],[["', '],\n[["') + "\n")
    print("wrote", len(doc["grid"]), "grid rows")
