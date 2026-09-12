"""
Dashboards on records (UI redesign M5, Notes/40 section 4; the Dashboard, Sizes and Harness boards).

Text-level: VeraDash carries the layout record (persist, migrate, the layout file, chrome by construction, the
span -> size rule, Layouts and Arrange); the seven layout files name every tile of their grid as a record with a
form the catalogue knows; the Workers > Jobs fixed tiles are a grid; the catalogue serves the files.
Behaviour: migrate_layouts.py against the fixture the node test holds VeraDash.migrate to; the catalogue's
layout capabilities under a stub orchestrator.
"""
import asyncio
import importlib.util
import json
import os
import re
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _load(name, *rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


DASH = _read("vera", "chat", "vera-dashboard.js")
WOL = _read("vera", "workers", "workers_ollama_panel.html")
CAT = _read("vera", "widgets", "widget_catalog.py")
MIG = _load("migrate_layouts", "vera", "widgets", "migrate_layouts.py")
REC = _load("widget_record", "vera", "widgets", "widget_record.py")
GRIDS = {"main": ("vera/capability_orchestration.html", "dashGrid"), "dream": ("vera/dream/dream_panel.html", "dashGrid"),
         "wol-workers": ("vera/workers/workers_ollama_panel.html", "w-grid"), "wol-ollama": ("vera/workers/workers_ollama_panel.html", "ol-grid"),
         "wol-jobs": ("vera/workers/workers_ollama_panel.html", "j-grid"), "wol-observe": ("vera/workers/workers_ollama_panel.html", "obs-grid"),
         "wol-wkjobs": ("vera/workers/workers_ollama_panel.html", "wkj-grid")}
COUNTS = {"main": 24, "dream": 9, "wol-workers": 9, "wol-ollama": 9, "wol-jobs": 10, "wol-observe": 2, "wol-wkjobs": 8}


def _layout(key):
    return json.loads(_read("vera", "widgets", "layouts", key + ".json"))


_TAG = re.compile(r"<(/?)(div|span|button|table|thead|tbody|tr|td|th|select|option|label|a|p|b|i|em|pre|code|ul|ol|li|section|header|footer|nav|form|textarea|h[1-6]|small|strong|details|summary|vera-[a-z-]+|svg|canvas|iframe|input|img|br|hr)\b[^>]*?(/?)>", re.I)


def _element_end(html, start):
    depth, first = 0, True
    for m in _TAG.finditer(html, start):
        tag, closing, selfc = m.group(2).lower(), m.group(1) == "/", (m.group(3) == "/" or m.group(2).lower() in ("input", "img", "br", "hr"))
        if first:
            first, depth = False, 1
            continue
        if selfc:
            continue
        if closing:
            depth -= 1
            if depth == 0:
                return m.start()
        else:
            depth += 1
    return len(html)


def _grid_tiles(key):
    """The direct .widget children of a grid in the page markup: [(wid, (w, h))]."""
    file, gid = GRIDS[key]
    html = _read(*file.split("/"))
    m = re.search(r'<div[^>]*id="%s"[^>]*>' % gid, html)
    assert m, key
    g = html[m.start():_element_end(html, m.start())]
    out, last = [], 0
    for wm in re.finditer(r'<div class="widget([^"]*)"([^>]*)>', g):
        if wm.start() < last:
            continue
        last = _element_end(g, wm.start())
        wid = re.search(r'data-wid="([^"]+)"', wm.group(2)).group(1)
        w = int((re.search(r"w-w(\d+)", wm.group(1)) or [None, "4"])[1])
        h = int((re.search(r"w-h(\d+)", wm.group(1)) or [None, "1"])[1])
        out.append((wid, (w, h)))
    return out


# ── VeraDash on records (text-level) ─────────────────────────────────────────

def test_veradash_persists_the_layout_record_and_migrates_a_legacy_state():
    for s in ("function layoutRecord()", "function unpack(rec)", "localStorage.setItem(SKEY, JSON.stringify(layoutRecord()))",
              "if (!Array.isArray(j.widgets))", "j = migrate(j, { key: key, page:", "function migrate(legacy, o)", "function flow(tiles, cols)",
              "function arrange(tiles, cols)", "var GRID = { cols: 12, row: 58, gap: 10, widths: [2, 3, 4, 6, 8, 12] };"):
        assert s in DASH, s
    assert "window.VeraDash = { init: init, migrate: migrate, flow: flow, arrange: arrange, sizeForSpan: sizeForSpan, spanFor: spanFor" in DASH


def test_veradash_fetches_the_layout_file_and_applies_it_under_the_users_state():
    for s in ("'/ui/widgets/layouts/' + encodeURIComponent(key)", "function applyFile(file)", "function loadFile()", "ctl.ready = loadFile();",
              "opts.layout === false", "if (fresh) state.order = fileOrder;", "r.draw && r.draw.body === 'page'",
              "addRecord(r, { silent: true, wid: wid, fromFile: true, span: span"):
        assert s in DASH, s


def test_every_tile_gets_its_chrome_by_construction_and_the_span_picks_the_size():
    for s in ("function ensureChrome(w)", "<span class=\"w-resize\" data-resize></span>')", "<span class=\"w-grip\">⠿</span>')",
              "function recordChip(w)", "chip.className = 'vd-rec'", "function sizeForSpan(w, h)", "function syncSize(w)",
              "el.setAttribute('size', sizeForSpan(sp0[0], sp0[1]))", "syncSize(w);       // the span picks the size",
              "body.style.maxHeight = mb + 'px'"):
        assert s in DASH, s
    assert "el.setAttribute('size', 'auto');" not in DASH, "a record tile draws at the size its span picks, not its pixels"


def test_the_loader_drops_a_panel_record_and_a_restored_panel_goes_back_through_the_loader():
    for s in ("function panelRecord(panelId, wid, label)", "var record = panelRecord(panelId, wid,", "state.dynamic[wid] = { panelId: panelId, wid: wid, record: record };",
              "if (info.panelId) { pending.push(addWidget(info.panelId, { silent: true })); return; }", "if (pending.length) Promise.all(pending).then(function () { applyLayout(); }"):
        assert s in DASH, s


def test_saved_layouts_a_layouts_menu_and_arrange_sit_beside_configure():
    for s in ("var LKEY = SKEY + '.layouts';", "function saveLayout(name)", "function loadLayout(name)", "function deleteLayout(name)",
              "function openLayouts()", "function renderLayouts()", "function injectToolbar()", "lb.textContent = 'Layouts ▾'", "ab.textContent = 'Arrange'",
              "function doArrange()", "layouts: layouts, saveLayout: saveLayout, loadLayout: loadLayout, deleteLayout: deleteLayout, openLayouts: openLayouts"):
        assert s in DASH, s


def test_every_mechanic_is_still_there():
    for s in ("function onDragStart(e)", "function _autoScrollOnDrag(e)", "function onResizeDown(e)", "ghost.className = 'vd-rghost'",
              "function snap(n, lo, hi, list)", "function renderHidden()", "function floatW(w)", "function soloRequest(w)", "function popWindow(w)",
              "function openLoader()", "function _soloBoot()", "html.vd-solo-mode", "function addRecord(record, o2)", "function addWidget(panelId, o2)"):
        assert s in DASH, s


# ── the layout files ─────────────────────────────────────────────────────────

def test_the_seven_layout_files_name_every_tile_of_their_grid_as_a_record():
    forms = set(REC.form_ids())
    total = 0
    for key, n in COUNTS.items():
        lay = _layout(key)
        assert lay["v"] == 2 and lay["key"] == key and lay["grid"] == {"cols": 12, "row": 58, "gap": 10, "widths": [2, 3, 4, 6, 8, 12]}, key
        page = _grid_tiles(key)
        assert len(page) == n, (key, len(page))
        assert [t["record"]["id"] for t in lay["widgets"]] == [wid for wid, _ in page], key
        for t, (wid, span) in zip(lay["widgets"], page):
            r = t["record"]
            assert r["form"] in forms, (key, wid, r["form"])
            assert r["title"] and r["source"] and r["shape"], (key, wid)
            assert t["span"] == list(span) and r["frame"]["span"] == list(span), (key, wid)
            assert r["frame"]["size"] == REC.size_for_span(*span), (key, wid)
            assert r["draw"]["body"] == "page", (key, wid)
            assert isinstance(t["at"], list) and len(t["at"]) == 2, (key, wid)
            for c in r.get("children") or []:
                assert c["record"]["form"] in forms and c["slot"], (key, wid, c)
        total += MIG.count_records(lay)
    assert total >= 110, total


def test_the_composites_carry_the_sub_widgets_the_inventory_names():
    main = {t["record"]["id"]: t["record"] for t in _layout("main")["widgets"]}
    assert main["sysmon-proxmox"]["form"] == "composite" and len(main["sysmon-proxmox"]["children"]) == 9
    assert [c["record"]["form"] for c in main["sysmon-proxmox"]["children"]].count("trace") == 3
    assert main["status"]["form"] == "counter" and main["status"]["source"] == "obs.health"
    assert main["topology-map"]["form"] == "topology" and main["topology-map"]["frame"]["size"] == "xl"
    assert main["sysmon-proxmox"]["frame"]["max_body"] == 290, "the inline cap became the record's max_body"
    ol = {t["record"]["id"]: t["record"] for t in _layout("wol-ollama")["widgets"]}
    assert ol["ol-bgqueue"]["form"] == "list" and ol["ol-bgqueue"]["source"] == "background.status"
    obs = {t["record"]["id"]: t["record"] for t in _layout("wol-observe")["widgets"]}
    assert obs["obs-stream"]["read"]["refresh"] == "live"


def test_the_workers_jobs_fixed_tiles_and_tables_joined_the_grid():
    assert 'id="wkj-grid"' in WOL and 'id="wkj-hidden-picker"' in WOL
    assert "wkjobs: {grid:'wkj-grid',editBtn:'w-edit-btn'" in WOL
    assert "function _gridPane(pane)" in WOL and "function gridToggleEdit(pane){ pane=_gridPane(pane);" in WOL
    for wid in ("wkj-pending", "wkj-running", "wkj-done", "wkj-failed", "wkj-cap-types", "wkj-avg-dur", "wkj-table", "wkj-cap-table"):
        assert 'data-wid="%s"' % wid in WOL, wid
        assert "P.gridHide('wkjobs','%s')" % wid in WOL, wid
    for kept in ('id="wkj-pend"', 'id="wkj-run"', 'id="wkj-done"', 'id="wkj-fail"', 'id="wkj-cap-types"', 'id="wkj-avg-dur"', 'id="wkj-search"',
                 'id="wkjt-all"', 'id="wkj-table"', 'id="wkj-tbody"', 'id="wkj-empty"', 'id="wkj-cap-table"', 'id="wkj-cap-tbody"', 'id="wkj-dot-pend"'):
        assert kept in WOL, kept
    assert '<div class="widget w-h1" style="grid-column:span 1">' not in WOL, "the fixed 6-column row is gone"
    assert "if(editBtn) editBtn.style.display=hideNodes?'none':'';" not in WOL, "Configure stays for the Jobs sub-pane"
    # nothing else of the page moved: every other grid's tiles are as they were
    assert len(_grid_tiles("wol-workers")) == 9 and len(_grid_tiles("wol-jobs")) == 10


# ── migrate_layouts.py (behaviour; the node test holds VeraDash.migrate to the same fixture) ─────────────────

LEGACY = {"order": ["b", "a", "dyn-system-monitor"], "hidden": ["c"], "sizes": {"a": {"w": 6, "h": 2}},
          "dynamic": {"dyn-system-monitor": {"panelId": "system-monitor", "wid": "dyn-system-monitor"},
                      "rec-x": {"record": {"form": "counter", "source": "obs.pending", "title": "Pending", "read": {"refresh": "5s"}}, "wid": "rec-x"}}}
PAGE = [{"id": "a", "span": [2, 1]}, {"id": "b", "span": [4, 1]}, {"id": "c", "span": [2, 1]}, {"id": "d", "span": [12, 2]}]


def test_migrate_is_the_same_rule_as_veradash_migrate():
    L = MIG.migrate("main", LEGACY, None, PAGE)
    assert L["v"] == 2 and L["key"] == "main" and L["layout"] == "default"
    assert L["grid"] == {"cols": 12, "row": 58, "gap": 10, "widths": [2, 3, 4, 6, 8, 12]}
    ids = [t["record"] if isinstance(t["record"], str) else t["record"]["id"] for t in L["widgets"]]
    assert ids == ["b", "a", "dyn-system-monitor", "c", "d", "rec-x"]
    assert [t["span"] for t in L["widgets"]] == [[4, 1], [6, 2], [6, 3], [2, 1], [12, 2], [4, 2]]
    assert [t["at"] for t in L["widgets"]] == [[0, 0], [4, 0], [0, 2], None, [0, 5], [6, 2]]
    assert [t["hidden"] for t in L["widgets"]] == [False, False, False, True, False, False]
    dyn = L["widgets"][2]["record"]
    assert (dyn["id"], dyn["form"], dyn["panel"], dyn["source"]) == ("dyn-system-monitor", "panel", "system-monitor", "panel:system-monitor")
    assert L["widgets"][5]["record"]["form"] == "counter" and L["widgets"][5]["refresh"] == "5s"
    assert isinstance(L["widgets"][0]["record"], str)


def test_migrate_takes_the_page_tiles_from_the_grids_layout_file():
    L = MIG.migrate("dream", {"order": ["cycle", "scheduler"], "hidden": ["idle"]}, _layout("dream"))
    ids = [t["record"] for t in L["widgets"]]
    assert ids[:2] == ["cycle", "scheduler"] and len(ids) == 9 and "idle" in ids
    assert L["dashboard"] == "dream"
    assert [t for t in L["widgets"] if t["record"] == "idle"][0]["hidden"] is True
    assert [t for t in L["widgets"] if t["record"] == "last-dream"][0]["span"] == [6, 2], "an untouched tile keeps the file's span"


def test_size_for_span_matches_widget_record():
    for w, h in ((1, 1), (2, 1), (3, 2), (4, 1), (4, 4), (6, 1), (6, 3), (8, 1), (12, 5)):
        assert MIG.size_for_span(w, h) == REC.size_for_span(w, h), (w, h)


def test_flow_and_arrange_pack_densely():
    tiles = [{"id": "a", "span": [4, 1]}, {"id": "b", "span": [6, 2]}, {"id": "c", "span": [6, 3]}, {"id": "h", "span": [2, 1], "hidden": True},
             {"id": "d", "span": [12, 2]}, {"id": "e", "span": [4, 2]}]
    assert [t["at"] for t in MIG.flow(tiles)] == [[0, 0], [4, 0], [0, 2], None, [0, 5], [6, 2]]
    assert [t["id"] for t in MIG.arrange(tiles)] == ["a", "b", "c", "e", "d", "h"]


def test_migrate_store_walks_a_localstorage_dump_and_ignores_the_wol_legacy():
    dump = {"vera.dash.main": json.dumps({"order": ["status"], "hidden": []}), "vera.dash.dream": {"v": 2, "widgets": [{"record": "cycle", "span": [2, 1]}]},
            "vera:wol-layout:workers": json.dumps({"order": ["w-total"]}), "vera.theme": "dusk", "vera.dash.broken": "{not json"}
    out = MIG.migrate_store(dump, {"main": _layout("main")})
    assert set(out) == {"main", "dream"}
    assert out["main"]["v"] == 2 and out["main"]["widgets"][0]["record"] == "status" and len(out["main"]["widgets"]) == 24
    assert out["dream"]["widgets"][0]["record"] == "cycle", "an entry already in the new shape passes through"


def test_migrate_is_idempotent_on_its_own_output():
    L = MIG.migrate("main", LEGACY, None, PAGE)
    assert not MIG.is_legacy(L)
    assert MIG.migrate_store({"vera.dash.main": L})["main"] == L


# ── the catalogue's layout capabilities, under a stub orchestrator ───────────

def _catalog():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}
    orch.ROUTES = []

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "meta": kw}
            return fn
        return deco

    class _App:
        def get(self, path, **k):
            orch.ROUTES.append(("GET", path))
            return lambda fn: fn

        def post(self, path, **k):
            orch.ROUTES.append(("POST", path))
            return lambda fn: fn

    orch.APP = _App()
    orch.capability = capability
    pkg = types.ModuleType("Vera"); pkg.__path__ = []
    sub = types.ModuleType("Vera.vera"); sub.__path__ = []
    sys.modules["Vera"] = pkg; sys.modules["Vera.vera"] = sub; sys.modules["Vera.vera.capability_orchestration"] = orch
    return _load("widget_catalog_under_test", "vera", "widgets", "widget_catalog.py"), orch


def test_the_catalogue_lists_and_serves_the_layout_files():
    cat, orch = _catalog()
    assert ("GET", "/ui/widgets/layouts/{key}") in orch.ROUTES
    assert orch.CAPABILITY_REGISTRY["widget.layouts"]["meta"]["http_path"] == "/ui/widgets/layouts"
    assert orch.CAPABILITY_REGISTRY["widget.layout.migrate"]["meta"]["http_path"] == "/ui/widgets/layouts/migrate"
    assert cat.layout_keys() == sorted(COUNTS)
    r = _run(cat.widget_layouts())
    assert r["ok"] and r["count"] == 7 and {x["key"]: x["widgets"] for x in r["layouts"]} == COUNTS
    assert [x for x in r["layouts"] if x["key"] == "main"][0]["records"] == 55
    one = _run(cat.widget_layouts(key="wol-observe"))
    assert one["ok"] and one["layout"]["widgets"][0]["record"]["id"] == "obs-stream"
    assert cat.load_layout("../widget_record") is None and cat.load_layout("nope") is None
    miss = _run(cat.widget_layouts(key="nope"))
    assert not miss["ok"] and "no layout" in miss["error"]


def test_the_catalogue_migrates_a_legacy_state_with_the_grids_file():
    cat, _ = _catalog()
    r = _run(cat.widget_layout_migrate(key="wol-jobs", legacy={"order": ["j-list"], "hidden": ["j-pending"], "sizes": {"j-list": {"w": 12, "h": 6}}}))
    assert r["ok"] and r["layout"]["v"] == 2 and r["layout"]["widgets"][0]["record"] == "j-list" and r["layout"]["widgets"][0]["span"] == [12, 6]
    assert len(r["layout"]["widgets"]) == 10
    again = _run(cat.widget_layout_migrate(key="wol-jobs", legacy=r["layout"]))
    assert again["ok"] and again["layout"] == r["layout"]
    assert not _run(cat.widget_layout_migrate(key=""))["ok"]
