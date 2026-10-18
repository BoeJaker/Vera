"""
WSU-58 S8 (DEEP-29 11A.4 Decision 5.1): the canvas gains the size xs. canvas.item.set took it already (item_core.SIZES);
canvas.add, canvas.size and canvas.result now take it too, from the same list. Loaded against the resolver test's stub
orchestrator (tests/test_canvas_resolver.py), so the capabilities run for real.
"""
import importlib.util
import os
import re

import test_canvas_resolver as R

ROOT = os.path.join(os.path.dirname(__file__), "..")
C, _run, SID = R.C, R._run, R.SID


def _core():
    spec = importlib.util.spec_from_file_location("item_core_xs", os.path.join(ROOT, "vera", "canvas", "item_core.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_canvas_sizes_are_the_item_records_sizes():
    assert tuple(C.ITEM_SIZES) == tuple(_core().SIZES) == ("xs", "s", "m", "l", "xl")
    js = open(os.path.join(ROOT, "vera", "ui", "vera_item.js"), encoding="utf-8").read()
    m = re.search(r"var SIZES = \[([^\]]*)\]", js)
    assert m and [x.strip().strip("'") for x in m.group(1).split(",")] == list(C.ITEM_SIZES), "the JS mirror lists the same sizes"


def test_canvas_add_takes_xs():
    R._fresh()
    r = _run(C.cap_canvas_add(session_id=SID, kind="note", content="n", key="note:xs", size="xs"))
    assert r["ok"] and r["item"]["size"] == "xs"
    doc = _run(C.cap_canvas_get(id=r["id"]))
    assert doc["blocks"][0]["size"] == "xs"
    bad = _run(C.cap_canvas_add(session_id=SID, kind="note", content="x", key="note:bad", size="xxs"))
    assert bad["item"]["size"] == "m", "an unknown size still falls back to m"


def test_canvas_size_takes_xs_and_says_what_it_was():
    R._fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="n", key="note:n"))
    s = _run(C.cap_canvas_size(key="note:n", size="XS", session_id=SID))
    assert s["ok"] and s["size"] == "xs" and s["prev"] == "m"
    back = _run(C.cap_canvas_size(key="note:n", size="m", session_id=SID))
    assert back["ok"] and back["prev"] == "xs"
    bad = _run(C.cap_canvas_size(key="note:n", size="xxl", session_id=SID))
    assert bad["ok"] is False and "xs|s|m|l|xl" in bad["error"]


def test_the_descriptions_name_xs():
    add = C.cap_canvas_add, C.cap_canvas_size
    src = open(os.path.join(ROOT, "vera", "canvas", "canvas_capabilities.py"), encoding="utf-8").read()
    assert all(callable(f) for f in add)
    assert "size ('xs'|'s'|'m'|'l'|'xl'), anchor" in src
    assert "Inputs: key (str!), size ('xs'|'s'|'m'|'l'|'xl'), id or session_id." in src
