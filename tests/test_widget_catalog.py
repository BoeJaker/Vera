"""
The widget catalogue (UI redesign m1 foundations; the WidgetSpec board §3): the
sources built from the live capability registry (a shape read off a
capability's name, a hand list for the well-known reads, the streams), and the
four capabilities a renderer or editor asks - widget.forms, widget.sources,
widget.validate, widget.render_spec. The module runs against a stub
orchestrator with a few fake capabilities registered. The wiring (the module
files, the libraries' routes, register_ui's sections/options) is held
text-level.
"""
import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _load():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "meta": kw}
            return fn
        return deco

    class _App:
        def get(self, *a, **k): return lambda fn: fn
        def post(self, *a, **k): return lambda fn: fn

    async def sysmon_history(node="", window="1h", trace_id=None): return {}
    async def sysmon_status(node="", trace_id=None): return {}
    async def code_write(path="", text="", trace_id=None): return {}
    async def jobs_list(limit=50, trace_id=None): return {}
    async def foo_bar(trace_id=None): return {}
    orch.CAPABILITY_REGISTRY.update({
        "sysmon.history": {"func": sysmon_history, "meta": {"description": "cpu / ram / gpu series per node"}},
        "sysmon.status": {"func": sysmon_status, "meta": {"description": "a snapshot"}},
        "code.write": {"func": code_write, "meta": {"description": "write a file"}},
        "jobs.list": {"func": jobs_list, "meta": {"description": "the jobs"}},
        "foo.bar": {"func": foo_bar, "meta": {"description": "nothing to tell"}},
    })
    orch.APP = _App(); orch.capability = capability
    pkg = types.ModuleType("Vera"); pkg.__path__ = []
    sub = types.ModuleType("Vera.vera"); sub.__path__ = []
    sys.modules["Vera"] = pkg; sys.modules["Vera.vera"] = sub; sys.modules["Vera.vera.capability_orchestration"] = orch

    def load(name, rel):
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

    c = load("widget_catalog_under_test", ("vera", "widgets", "widget_catalog.py"))
    return c, orch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


C, ORCH = _load()


def test_a_capabilitys_name_says_its_shape_and_a_write_is_never_a_source():
    assert C.guess_shape("sysmon.history") == "series"
    assert C.guess_shape("jobs.list") == "items"
    assert C.guess_shape("obs.health") == "values"
    assert C.guess_shape("obs.pending") == "level"
    assert C.guess_shape("markets.bars") == "ohlcv"
    assert C.guess_shape("code.write") == "" and C.guess_shape("exec.code.run") == "" and C.guess_shape("canvas.create") == ""
    assert C.guess_shape("foo.bar") == "", "no telling from the name"


def test_sources_come_from_the_live_registry_plus_the_hand_list_and_the_streams():
    src = C.sources()
    ids = {s["id"]: s for s in src}
    assert "sysmon.history" in ids and ids["sysmon.history"]["shape"] == "series" and ids["sysmon.history"]["refresh_min"] == "5s"
    assert ids["sysmon.history"]["args"] == ["node", "window"], "the capability's own arguments"
    assert "jobs.list" in ids and ids["jobs.list"]["note"] == "shape from the name"
    assert "code.write" not in ids and "foo.bar" not in ids
    assert ids["obs.node_temps"]["unit"] == "°C" and ids["obs.node_temps"]["note"] == "not registered here"
    assert ids["stream:events"]["shape"] == "events" and ids["stream:events"]["refresh_min"] == "live"
    assert [s["id"] for s in C.sources(shape="series")] and all(s["shape"] == "series" for s in C.sources(shape="series"))
    assert all("sysmon" in s["id"] for s in C.sources(q="sysmon"))
    assert C.source("panel:system-monitor")["shape"] == "panel" and C.source("nope") is None


def test_widget_forms_lists_the_catalogue_and_filters_by_shape():
    r = _run(C.widget_forms())
    assert r["ok"] and r["count"] >= 66 and "sizes" in r and r["sizes"]["xl"].startswith("a panel")
    s = _run(C.widget_forms(shape="series"))
    assert s["count"] and all(f["shape"] == "series" for f in s["forms"]) and any(f["id"] == "trace" for f in s["forms"])
    q = _run(C.widget_forms(q="heat"))
    assert [f["id"] for f in q["forms"]] == ["heat"]


def test_widget_sources_capability():
    r = _run(C.widget_sources(shape="items", limit=5))
    assert r["ok"] and len(r["sources"]) <= 5 and r["count"] >= 5 and all(s["shape"] == "items" for s in r["sources"])


def test_widget_validate_binds_the_source_shape_to_the_form():
    ok = _run(C.widget_validate(record={"form": "trace", "source": "sysmon.history", "title": "GPU", "window": "1h"}))
    assert ok["valid"] and ok["record"]["read"]["window"] == "1h" and ok["template"]["reads"]["cap"] == "sysmon.history"
    bad = _run(C.widget_validate(record={"form": "trace", "source": "sysmon.status", "title": "x"}))
    assert not bad["valid"] and any("wants series" in p for p in bad["problems"])
    warn = _run(C.widget_validate(record={"name": "x", "form": "meter", "reads": {"cap": "obs.pending"}, "draw": {"form": "meter", "size": "S", "glow": 1}}))
    assert warn["valid"] and any("glow" in w for w in warn["warnings"]), "the template shape is accepted; unknown draw options are dropped with a warning"
    panel = _run(C.widget_validate(record={"form": "panel", "source": "panel:system-monitor", "title": "Monitor"}))
    assert panel["valid"] and panel["record"]["panel"] == "system-monitor"


def test_widget_render_spec_answers_source_form_and_size_in_one():
    r = _run(C.widget_render_spec(record={"form": "trace", "source": "sysmon.history", "title": "GPU", "frame": {"span": [6, 3]}}))
    assert r["ok"] and r["source"]["shape"] == "series" and r["form"]["id"] == "trace"
    assert r["size"]["size"] == "xl" and r["size"]["span"] == [6, 3] and "table" in r["size"]["composition"]
    t = _run(C.widget_render_spec(record={"form": "terminal", "source": "sandbox.session.exec", "size": "s"}))
    assert t["size"]["size"] == "xl", "a form without the asked size draws at the largest it has"


# ── the wiring, text-level ────────────────────────────────────────────────────

def test_the_modules_load_and_the_libraries_are_served():
    orch = _read("vera", "capability_orchestration.py")
    assert 'os.path.join(_here, "ui/libs.py"),' in orch and 'os.path.join(_here, "widgets/widget_catalog.py"),' in orch
    assert orch.index('"ui/scripts.py"') < orch.index('"ui/libs.py"') < orch.index('"widgets/widget_catalog.py"')
    libs = _read("vera", "ui", "libs.py")
    assert '@APP.get("/ui/iso.js"' in libs and '@APP.get("/ui/menus.js"' in libs
    assert os.path.exists(os.path.join(ROOT, "vera", "ui", "iso.js")) and os.path.exists(os.path.join(ROOT, "vera", "ui", "menus.js"))
    iso = _read("vera", "ui", "iso.js")
    assert "window.VeraISO = { proj, box, face, scene, fit, px, shade, edgeOfBox, route, segs, ring, discTf, frame, isoFitK, isoScene };" in iso
    assert "window.MENUS = { rows, kindOf, label, kinds:Object.keys(K) };" in _read("vera", "ui", "menus.js")


def test_register_ui_accepts_a_panels_sections_and_options():
    orch = _read("vera", "capability_orchestration.py")
    assert "sections: List[dict] = None,\n                options: List[dict] = None):" in orch
    assert '"sections":  [s for s in (sections or []) if isinstance(s, dict)],' in orch
    assert '"options":   [o for o in (options or []) if isinstance(o, dict)],' in orch


def test_the_registry_normalises_and_validates_through_the_record_module():
    reg = _read("vera", "widgets", "widget_registry.py")
    assert '_rec = _sibling("widget_record")' in reg
    assert "return _rec.normalise_template(t if isinstance(t, dict) else {})" in reg
    assert "return _rec.template_problems(t, FORMS, WHERES)" in reg
    assert "return _rec.to_template(t)" in reg, "the full record is accepted by the registry too"
