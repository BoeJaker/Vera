"""
The widget registry (UI redesign, Notes/40 §3): every part of the UI as a
record — templates (saved, the chat LHM's parts, every registered panel as a
`panel` widget), instances (placements), and the capabilities over them.

The module is loaded here against a stub of the orchestrator (a fake async
Redis, an empty panel registry with two panels), so the record logic runs for
real without Vera booting: normalisation, the problems a save refuses, the
built-ins, list/filter/counts, save/version/copy-of-a-built-in, delete rules,
instantiate + instance list/remove. The wiring the UIs need is held text-level:
the module is in the loader's list, the harness realises dashboard placements,
the LHM library opens and saves records, the chat names its panes' templates.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ── a stub orchestrator: enough for the module to import and run ─────────────

class _FakeRedis:
    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ex=None):
        self.d[k] = v

    async def delete(self, k):
        return 1 if self.d.pop(k, None) is not None else 0

    async def scan_iter(self, match="*", count=100):
        prefix = match.rstrip("*")
        for k in list(self.d):
            if k.startswith(prefix):
                yield k


def _load_module():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.REDIS = _FakeRedis()
    orch.UI_PANELS = {
        "memory-graph": {"id": "memory-graph", "label": "Memory Graph", "icon": "🕸", "ui_caps": ["memory.select", "memory.graph"], "mode": "element"},
        "exec-panel": {"id": "exec-panel", "label": "Exec", "icon": "⌘", "ui_caps": ["exec.bash.run"], "mode": "tab"},
    }
    orch.CAPS = {}
    orch.UI = {}

    def capability(name, **kw):
        def deco(fn):
            orch.CAPS[name] = {"func": fn, "meta": kw}
            return fn
        return deco

    class _App:
        def get(self, *a, **k):
            return lambda fn: fn

        def post(self, *a, **k):
            return lambda fn: fn

    orch.APP = _App()
    orch.capability = capability
    async def _emit(ev):          # awaited by the registry since the control-plane slice
        orch.EVENTS.append(ev)
    orch.EVENTS = []
    orch.emit_event = _emit
    orch.now_iso = lambda: "2026-09-11T00:00:00+00:00"
    orch.register_ui = lambda *a, **k: orch.UI.__setitem__(a[0], {"args": a, "kw": k})
    pkg = types.ModuleType("Vera"); pkg.__path__ = []
    sub = types.ModuleType("Vera.vera"); sub.__path__ = []
    sys.modules["Vera"] = pkg; sys.modules["Vera.vera"] = sub; sys.modules["Vera.vera.capability_orchestration"] = orch
    spec = importlib.util.spec_from_file_location("widget_registry_under_test", os.path.join(ROOT, "vera", "widgets", "widget_registry.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, orch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


MOD, ORCH = _load_module()


# ── the record ───────────────────────────────────────────────────────────────

def test_normalise_gives_every_template_one_shape():
    t = MOD._normalise({"name": "GPU + queue", "form": "meter", "reads": "sysmon.status", "can": "refresh . pin", "placed": ["dashboard", {"where": "LHM"}]})
    assert t["id"] == "gpu-queue" and t["form"] == "meter"
    assert t["reads"] == {"cap": "sysmon.status", "args": {}, "every": "", "note": ""}
    assert t["can"] == ["refresh", "pin"] and t["placed"] == ["dashboard", "LHM"]
    assert t["draw"]["size"] == "M" and t["version"] == 1


def test_a_save_refuses_what_says_nothing_about_itself():
    assert "a template needs a name" in MOD.problems(MOD._normalise({"form": "meter", "reads": {"cap": "x"}}))
    assert any("unknown form" in p for p in MOD.problems(MOD._normalise({"name": "x", "form": "blob", "reads": {"cap": "x"}})))
    assert any("must say what it reads" in p for p in MOD.problems(MOD._normalise({"name": "x", "form": "meter"})))
    assert MOD.problems(MOD._normalise({"name": "x", "form": "button"})) == [], "a button reads nothing and that is fine"


def test_the_built_ins_are_the_lhm_parts_and_every_panel():
    lhm = MOD._builtin_lhm()
    assert [t["id"] for t in lhm][:3] == ["lhm:rail", "lhm:header", "lhm:sessions"]
    assert all(t["source"]["origin"] == "built-in" for t in lhm)
    assert all(not MOD.problems(t) for t in lhm), [MOD.problems(t) for t in lhm if MOD.problems(t)]
    panels = MOD._builtin_panels()
    assert {t["id"] for t in panels} == {"panel:memory-graph", "panel:exec-panel"}
    mg = [t for t in panels if t["id"] == "panel:memory-graph"][0]
    assert mg["form"] == "panel" and mg["reads"]["cap"] == "memory.select" and mg["source"]["panel"] == "memory-graph"
    assert "dashboard" in mg["placed"]


# ── the capabilities ─────────────────────────────────────────────────────────

def test_list_filters_and_counts():
    r = _run(MOD.widget_template_list())
    assert r["ok"] and r["total"] == len(MOD._LHM_BUILTINS) + 2
    assert r["kinds"]["panel"] == 2 and r["kinds"]["graph"] >= 2
    r2 = _run(MOD.widget_template_list(kind="panel"))
    assert {t["id"] for t in r2["templates"]} == {"panel:memory-graph", "panel:exec-panel"}
    r3 = _run(MOD.widget_template_list(where="dashboard"))
    assert all(any(p["where"] == "dashboard" for p in t["placements"]) for t in r3["templates"]) and r3["count"] >= 2
    r4 = _run(MOD.widget_template_list(q="terminal"))
    assert [t["id"] for t in r4["templates"]] == ["lhm:terminal"]


def test_save_creates_then_versions_and_a_built_in_copy_becomes_yours():
    r = _run(MOD.widget_template_save({"name": "Boot cost", "form": "counter", "reads": {"cap": "obs.provenance", "args": {"kind": "boot"}}, "placed": ["dashboard"]}))
    assert r["ok"] and r["created"] and r["template"]["id"] == "boot-cost" and r["template"]["version"] == 1
    r2 = _run(MOD.widget_template_save({"id": "boot-cost", "name": "Boot cost", "form": "counter", "reads": {"cap": "obs.provenance"}}))
    assert r2["ok"] and not r2["created"] and r2["template"]["version"] == 2
    bad = _run(MOD.widget_template_save({"name": "Nameless form", "form": "blob"}))
    assert not bad["ok"] and bad["problems"]
    forced = _run(MOD.widget_template_save({"name": "Odd", "form": "counter"}, force=True))
    assert forced["ok"] and forced["problems"]
    # saving a built-in under its own id makes it yours (the source says where it came from); the built-in stays listable
    lhm = _run(MOD.widget_template_get(id="lhm:terminal"))["template"]
    assert lhm["source"]["origin"] == "built-in"
    mine = _run(MOD.widget_template_save(dict(lhm, frame="attached . my own frame")))
    assert mine["ok"] and mine["template"]["source"]["origin"] == "you" and mine["template"]["source"]["from_builtin"] == "lhm:terminal"
    got = _run(MOD.widget_template_get(id="lhm:terminal"))["template"]
    assert got["frame"] == "attached . my own frame", "the saved record shadows the built-in"
    listed = _run(MOD.widget_template_list())
    assert listed["total"] >= len(MOD._LHM_BUILTINS) + 2 + 2, "new ids add, a shadowed built-in does not"
    _run(MOD.widget_template_delete(id="lhm:terminal"))   # leave the built-in as we found it for the other tests


def test_delete_refuses_a_built_in_but_removes_a_saved_copy():
    r = _run(MOD.widget_template_delete(id="lhm:rail"))
    assert not r["ok"] and "built in" in r["error"]
    assert _run(MOD.widget_template_save({"name": "Boot cost", "form": "counter", "reads": {"cap": "obs.provenance"}}))["ok"]   # its own fixture: tests run in any order
    r2 = _run(MOD.widget_template_delete(id="boot-cost"))
    assert r2["ok"]
    assert not _run(MOD.widget_template_get(id="boot-cost"))["ok"]
    shadow = _run(MOD.widget_template_get(id="lhm:terminal"))["template"]
    _run(MOD.widget_template_save(dict(shadow, frame="shadowed")))          # a saved shadow of a built-in…
    r3 = _run(MOD.widget_template_delete(id="lhm:terminal"))                 # …goes, and the built-in stays
    assert r3["ok"] and _run(MOD.widget_template_get(id="lhm:terminal"))["template"]["source"]["origin"] == "built-in"


def test_instantiate_places_and_the_placements_count_it():
    r = _run(MOD.widget_template_instantiate(id="panel:memory-graph", where="dashboard", host="main", session_id="chat-1"))
    assert r["ok"] and r["instance"]["panel"] == "memory-graph" and r["instance"]["where"] == "dashboard" and r["instance"]["host"] == "main"
    bad = _run(MOD.widget_template_instantiate(id="panel:memory-graph", where="fridge"))
    assert not bad["ok"] and "unknown placement" in bad["error"]
    missing = _run(MOD.widget_template_instantiate(id="nope", where="dashboard"))
    assert not missing["ok"]
    got = _run(MOD.widget_template_get(id="panel:memory-graph"))
    assert got["template"]["instances"] == 1 and any(p["where"] == "dashboard" and p["count"] == 1 for p in got["template"]["placements"])
    lst = _run(MOD.widget_instance_list(where="dashboard", host="main"))
    assert lst["count"] == 1 and lst["instances"][0]["template"] == "panel:memory-graph"
    assert _run(MOD.widget_instance_list(session_id="chat-2"))["count"] == 0
    rm = _run(MOD.widget_instance_remove(id=r["instance"]["id"]))
    assert rm["ok"] and _run(MOD.widget_instance_list())["count"] == 0
    assert not _run(MOD.widget_instance_remove(id="inst-nope"))["ok"]


def test_the_registry_panel_is_registered_as_an_element_with_its_caps():
    reg = ORCH.UI["widget-registry"]
    assert reg["args"][1] == "Widgets" and reg["kw"]["mode"] == "element"
    assert set(reg["kw"]["ui_caps"]) >= {"widget.template.list", "widget.template.save", "widget.template.instantiate", "widget.instance.list"}
    for cap in ("widget.template.list", "widget.template.get", "widget.template.save", "widget.template.delete", "widget.template.instantiate", "widget.instance.list", "widget.instance.remove"):
        assert cap in ORCH.CAPS, cap
    assert ORCH.CAPS["widget.template.list"]["meta"]["http_path"] == "/ui/widgets/templates"


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_module_is_loaded_beside_the_agent_registry():
    src = _read("vera", "capability_orchestration.py")
    i = src.index('os.path.join(_here, "registry/registry_capabilities.py"),')
    assert 'os.path.join(_here, "widgets/widget_registry.py"),' in src[i:i + 600]
    assert os.path.exists(os.path.join(ROOT, "vera", "widgets", "__init__.py"))
    assert os.path.exists(os.path.join(ROOT, "vera", "widgets", "widget_registry_panel.html"))


def test_the_harness_realises_dashboard_placements():
    h = _read("vera", "capability_orchestration.html")
    assert "const r=await api('/ui/widgets/instances?where=dashboard&host=main');" in h
    assert "try{ await _dashCtl.addWidget(inst.panel); }catch(_){}" in h
    assert "_dashPlacedStart();" in h[h.index("_dashCtl=VeraDash.init(g,{key:'main'"):][:400]
    assert "has no renderer yet" in h


def test_the_lhm_library_opens_and_saves_a_parts_record():
    lib = _read("vera", "chat", "vera-lhm.js")
    assert "function openRecord(tplId, label){" in lib and "function saveAsTemplate(tplId, label){" in lib and "function placeInto(tplId, where){" in lib
    assert "'/ui/widgets/template?id=' + encodeURIComponent(tplId)" in lib
    assert "'/ui/widgets/templates/save'" in lib and "'/ui/widgets/instantiate'" in lib
    assert "_rail.setAttribute('data-tpl', 'lhm:rail');" in lib and "_hd.setAttribute('data-tpl', 'lhm:header');" in lib and "_cta.setAttribute('data-tpl', 'lhm:cta');" in lib
    assert ".lhm-wcfgmode .lhm-det > :not(.lhm-hd):not(.lhm-wcfg){display:none!important}" in lib
    assert "if(_editing) _wireBars();" in lib


def test_the_sources_view_lists_the_registry_by_domain():
    PANEL = _read("vera", "widgets", "widget_registry_panel.html")
    assert 'onclick="wrView(\'sources\')"' in PANEL and 'id="srcDoms"' in PANEL and 'id="srcList"' in PANEL and "function wrSourcesDraw()" in PANEL
    assert "name:'widget.sources', arguments:{limit:5000, refresh:!!refresh}" in PANEL and "probe:true, probe_limit:40" in PANEL
    assert "' measured · ' + (tiers.hand || 0) + ' hand · ' + (tiers.declared || 0) + ' declared'" in PANEL
    assert "rows.slice(0, 1500)" in PANEL and "' more · narrow the search</div>'" in PANEL          # the whole registry (704 on the mirror) is on the list
    assert "async function wrSrcUse(id)" in PANEL and "record: { form, source: s.id, read: { map: s.map || {}, args:" in PANEL


def test_the_gallery_view_shows_every_form_at_its_sizes():
    # the registry panel carries the Gallery view (the Widgets boards as one gallery): a board filter, the five sizes,
    # the projection switch, a cell per form drawn by <vera-widget> from its sample, + Add on a cell → the config sheet
    PANEL = _read("vera", "widgets", "widget_registry_panel.html"); REG = _read("vera", "widgets", "widget_registry.py")
    assert 'onclick="wrView(\'gallery\')"' in PANEL and 'id="galGrid"' in PANEL and "function wrGalleryDraw()" in PANEL
    assert "GAL_BOARDS = [['all', 'All'], ['widgets', 'Still'], ['motion', 'Motion'], ['iso', 'Iso']" in PANEL
    assert "[['xs', 'XS'], ['s', 'S'], ['m', 'M'], ['l', 'L'], ['xl', 'XL']]" in PANEL and "[['board', \"the board's\"], ['flat', 'flat'], ['iso', 'iso']]" in PANEL
    assert "el.record = rec" in PANEL and "projection: (f.proj || []).includes(proj)" in PANEL
    assert "' with a live build · '" in PANEL and "a size is a composition, not a scale" in PANEL
    assert "S.open({ mode: 'add', into: 'dashboard', title: 'Add · ' + (f.name || f.id), templates: true, ok: 'Save', base: BASE, record: rec })" in PANEL
    # its own page and a registered panel
    assert '@APP.get("/ui/widgets/gallery", include_in_schema=False)' in REG and 'register_ui("widget-gallery", "Widget gallery", "▦", _GALLERY_HTML' in REG
    gal = ORCH.UI["widget-gallery"]; assert gal["args"][1] == "Widget gallery" and gal["kw"]["mode"] == "element" and "widget.forms" in gal["kw"]["ui_caps"]
    assert "u.searchParams.get('view') === 'gallery' || /\\/gallery\\/?$/.test(u.pathname)" in PANEL


def test_the_chat_names_its_panes_templates():
    chat = _read("vera", "chat", "chat_panel.html")
    for tpl in ("lhm:sessions", "lhm:ctx-galaxy", "lhm:ctx-budget", "lhm:memory-graph", "lhm:loop-graph", "lhm:file-tree", "lhm:actions"):
        assert "tpl:'" + tpl + "'" in chat, tpl
    assert "base:BASE," in chat
    # every template a pane names is a built-in the registry ships
    ids = {t["id"] for t in MOD._LHM_BUILTINS}
    import re
    for tpl in re.findall(r"tpl:'(lhm:[a-z-]+)'", chat):
        assert tpl in ids, tpl
