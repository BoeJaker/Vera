"""DA-02b: the server mirror of VeraWidget.present (widget_cap_output.present, served as widget.from_result) reads the
shared fixtures tests/fixtures/presentation/ - the same files tests/test_presentation_browser.cjs runs - and must agree:
a declared presentation decides exactly; else the hint and shape rules; nothing drawable -> no item."""
import asyncio
import glob
import importlib.util
import json
import os
import sys
import types

import pytest

from orchestration_stub import stubbed_orchestration
from vera.capability_contract_core import PRESENTATION_KINDS

ROOT = os.path.join(os.path.dirname(__file__), "..")
FIXDIR = os.path.join(os.path.dirname(__file__), "fixtures", "presentation")

pytestmark = pytest.mark.critical


def _load(name, *rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CO = _load("widget_cap_output_present", "vera", "widgets", "widget_cap_output.py")
FIXTURES = sorted(glob.glob(os.path.join(FIXDIR, "*", "*.json")))


def test_the_fixtures_are_there():
    assert len(FIXTURES) >= 20


def test_the_kinds_and_views_are_the_contract_cores():
    assert set(CO.PRESENT_KINDS) == set(PRESENTATION_KINDS)
    for kind, spec in PRESENTATION_KINDS.items():
        assert CO.PRESENT_KINDS[kind]["views"] == list(spec["views"]), kind


def test_the_browser_and_the_server_share_one_vocabulary():
    js = open(os.path.join(ROOT, "vera", "widgets", "present.js"), encoding="utf-8").read()
    for kind, spec in CO.PRESENT_KINDS.items():
        line = "%s: { element: '%s', form: '%s'" % (kind, spec["element"], spec["form"])
        assert line in js, line


@pytest.mark.parametrize("path", FIXTURES, ids=[os.path.relpath(p, FIXDIR).replace(os.sep, "/") for p in FIXTURES])
def test_fixture(path):
    fx = json.load(open(path, encoding="utf-8"))
    got = CO.present(fx["cap"], fx["result"], args=fx.get("args"), presentation=fx.get("presentation"))
    e = fx["expect"]
    assert got["element"] == e["element"]
    assert got["kind"] == e["kind"]
    assert got["title"] == e["title"]
    assert [r["form"] for r in got["records"]] == e["forms"]
    if "empty" in e:
        assert got.get("empty") == e["empty"]
    if "first" in e:
        d = got["records"][0]["data"]
        d0 = d[0] if isinstance(d, list) else d
        for k, v in e["first"].items():
            assert d0.get(k) == v, k
    if "data" in e:
        # the first record's data exactly - the browser reads the same fixture to the same data (DA-01e keyed series)
        assert got["records"][0]["data"] == e["data"]
    if fx.get("presentation") and fx["presentation"].get("status") != "invalid":
        assert got["declared"] is True
        # each record carries the declaration it was read by (a composite's parts: their own)
        parts = fx["presentation"].get("parts") or []
        want = [p["kind"] for p in parts] if fx["presentation"]["kind"] == "composite" else [fx["presentation"]["kind"]] * len(got["records"])
        assert [r.get("presentation", {}).get("kind") for r in got["records"]] == want


def test_an_undeclared_answer_reads_as_from_cap_result():
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    got = CO.present("demo.rows", rows)
    assert got["declared"] is False
    assert [r["form"] for r in got["records"]] == [r["form"] for r in CO.from_cap_result("demo.rows", rows)]


def test_pick_reads_paths_like_the_page():
    x = {"a": {"b": [{"c": 1}, {"c": 2}]}, "h": [1, 2, 3]}
    assert CO.pick(x, "a.b[1].c") == 2
    assert CO.pick(x, "h[-1]") == 3
    assert CO.pick(x, "$") is x
    assert CO.pick(x, "a.zz") is CO._MISSING


def _catalog_with(registry_entries):
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
        _load("widget_catalog_present", "vera", "widgets", "widget_catalog.py")
    orch.CAPABILITY_REGISTRY.update(registry_entries)
    return orch.CAPABILITY_REGISTRY["widget.from_result"]["func"]


def test_widget_from_result_reads_the_capabilitys_own_declaration():
    fn = _catalog_with({"demo.quotes": {"contract": {"effects": ["read"], "presentation": {"kind": "table", "map": {"rows": "quotes", "name": "symbol", "value": "last"}}}}})
    run = asyncio.new_event_loop().run_until_complete
    r = run(fn(cap="demo.quotes", result={"quotes": [{"symbol": "A", "last": 1}, {"symbol": "B", "last": 2}]}))
    assert r["ok"] and r["presentation"]["declared"] is True and r["presentation"]["kind"] == "table"
    assert [x["form"] for x in r["records"]] == ["table", "ranked"]
    # handed a declaration, it reads by that one
    r2 = run(fn(cap="demo.quotes", result={"body": "# hi"}, presentation={"kind": "document", "map": {"body": "body"}}))
    assert r2["presentation"]["kind"] == "document" and r2["records"][0]["form"] == "markdown"
    # an undeclared capability: the records are from_cap_result's, as before
    r3 = run(fn(cap="evolve.pipeline.diff", result={"diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y"}))
    assert r3["records"][0]["form"] == "diff" and r3["presentation"]["declared"] is False and "markdown" in r3["forms"]
