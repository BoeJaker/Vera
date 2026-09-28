"""
Capability output widgets, the python mirror (the widget review, round 2): widget_cap_output.from_cap_result draws a
capability's answer as the same forms VeraWidget.fromCapResult does - the fixtures are shared with
tests/test_widget_cap_output.cjs - and the catalogue serves it as widget.from_result.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

from orchestration_stub import stubbed_orchestration

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(name, *rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CO = _load("widget_cap_output", "vera", "widgets", "widget_cap_output.py")
REC = _load("widget_record", "vera", "widgets", "widget_record.py")
FIX = json.load(open(os.path.join(os.path.dirname(__file__), "widget_cap_fixtures.json"), encoding="utf-8"))


def test_every_result_form_is_a_catalogue_form():
    ids = set(REC.form_ids())
    assert set(CO.CAP_FORMS) <= ids, sorted(set(CO.CAP_FORMS) - ids)
    for f in ("json", "diff", "code", "progress", "status", "media", "error", "markdown"):
        assert REC.form(f)["id"] == f and REC.form(f)["boards"] == ["reply", "spec"], f
    assert all(h["form"] in ids for h in CO.CAP_HINTS.values())


def test_the_mirror_draws_every_fixture_as_the_page_does():
    for f in FIX:
        recs = CO.from_cap_result(f["cap"], f["result"], args={"a": 1})
        got = [r["form"] for r in recs]
        if f["form"] is None:
            assert recs == [], f["cap"]
            continue
        assert got and got[0] == f["form"], (f["cap"], got)
        if f.get("second"):
            assert len(got) > 1 and got[1] == f["second"], (f["cap"], got)
        for r in recs:
            assert r["source"] == f["cap"] and r["read"]["args"] == {"a": 1} and r["why"] and "data" in r, (f["cap"], r)
            assert r["form"] in set(REC.form_ids()), r["form"]


def test_an_envelope_is_opened_and_max_bounds_the_records():
    assert CO.from_cap_result("x", {"type": "tool_result", "content": {"ok": False, "error": "e"}})[0]["form"] == "error"
    rows = {"samples": [{"t": 1, "a": 1, "b": 2}, {"t": 2, "a": 2, "b": 3}, {"t": 3, "a": 1, "b": 1}]}
    assert len(CO.from_cap_result("u", rows)) == 2 and len(CO.from_cap_result("u", rows, max_records=1)) == 1
    assert CO.from_cap_result("u", None) == [] and CO.from_cap_result("u", "") == []


def test_the_page_and_the_mirror_share_the_hints():
    js = open(os.path.join(ROOT, "vera", "widgets", "widget_element.js"), encoding="utf-8").read()
    for cap, h in CO.CAP_HINTS.items():
        assert ("'%s': { form: '%s'" % (cap, h["form"])) in js, cap


def test_the_catalogue_serves_it_as_widget_from_result():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "meta": kw}
            return fn
        return deco

    class _App:
        def get(self, path, **k):
            return lambda fn: fn

        def post(self, path, **k):
            return lambda fn: fn

    orch.APP = _App()
    orch.capability = capability
    with stubbed_orchestration(orch):
        _load("widget_catalog_cap_output", "vera", "widgets", "widget_catalog.py")
    cap = orch.CAPABILITY_REGISTRY["widget.from_result"]
    assert cap["meta"]["http_path"] == "/ui/widgets/from_result"
    r = asyncio.new_event_loop().run_until_complete(cap["func"](cap="evolve.pipeline.diff", result={"diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y"}))
    assert r["ok"] and r["records"][0]["form"] == "diff" and "markdown" in r["forms"]
