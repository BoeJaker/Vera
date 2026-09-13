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
COUNTS = {"main": 24, "dream": 9, "wol-workers": 9, "wol-ollama": 8, "wol-jobs": 10, "wol-observe": 2, "wol-wkjobs": 8}   # wol-ollama: the Background Queue tile folded into the Jobs head (bleeding-edge 53043a9); its record is the element's


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
              "addRecord(eff, { silent: true, wid: wid, fromFile: true, span: span"):
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
        page_ids = [wid for wid, _ in page]
        # the page's tiles lead the layout, in the page's order; a tile the layout adds beyond them has no markup — the
        # element draws it from the file alone (VeraDash.applyFile → addRecord)
        assert [t["record"]["id"] for t in lay["widgets"] if t["record"]["id"] in page_ids] == page_ids, key
        spans = dict(page)
        for t in lay["widgets"]:
            r = t["record"]; wid = r["id"]
            assert r["form"] in forms, (key, wid, r["form"])
            assert r["title"] and r["source"] and r["shape"], (key, wid)
            span = spans.get(wid) or tuple(t["span"])
            if wid not in spans:
                assert r["draw"]["body"] == "record", (key, wid, "a tile without markup is the element's")
            assert t["span"] == list(span) and r["frame"]["span"] == list(span), (key, wid)
            assert r["frame"]["size"] == REC.size_for_span(*span), (key, wid)
            assert r["draw"]["body"] in ("page", "record"), (key, wid)
            assert isinstance(t["at"], list) and len(t["at"]) == 2, (key, wid)
            for c in r.get("children") or []:
                assert c["record"]["form"] in forms and c["slot"], (key, wid, c)
        total += MIG.count_records(lay)
    assert total >= 100, total   # the composites carry the children a tile's rows hold, not every sub-widget the inventory listed


PAGE_TILES = ("topology-map",)   # what stays the page's (the estate's own topology element), with its note
RECORD_ONLY = ("events", "health")   # the board's Live events and a Health tile: no page markup, drawn from the layout file


def test_the_main_tiles_are_record_tiles_the_element_draws_from_their_sources():
    """The conversion switch is the layout file: draw.body = "record" hands a page tile to <vera-widget> (VeraDash
    retires the hand-drawn markup under it, ids intact). Nineteen of the twenty-four main tiles draw from their
    records: read.map names the part of the source's envelope each form draws (the element's applyMap: the shape's
    container field takes a path, every other key renames a field of each row); a composite reads its subject once
    and its children take $subject.<path> slices of it, or read on their own. Five stay the page's, each saying
    why in its record's note (a computed verdict, a boolean, a dict the map cannot turn into rows, a merged read,
    an element the harness does not load)."""
    main = _layout("main")
    converted = [t["record"]["id"] for t in main["widgets"] if t["record"]["draw"].get("body") == "record"]
    page = [t["record"]["id"] for t in main["widgets"] if t["record"]["draw"].get("body") == "page"]
    assert len(converted) == 25 and tuple(page) == PAGE_TILES, (converted, page)
    recs = {t["record"]["id"]: t["record"] for t in main["widgets"]}
    for wid in PAGE_TILES:
        assert recs[wid].get("note"), wid
    page_ids = [wid for wid, _ in _grid_tiles("main")]
    assert [wid for wid in recs if wid not in page_ids] == list(RECORD_ONLY)
    # a record tile reads a source the element reads on its own, through a map into the real envelope
    assert recs["workers"]["read"]["map"] == {"value": "workers"} and recs["workers"]["source"] == "obs.health"
    assert recs["pending"]["read"]["map"] == {"value": "count"} and recs["pending"]["source"] == "obs.pending"
    assert recs["mode"]["form"] == "string" and recs["mode"]["shape"] == "string" and recs["mode"]["read"]["map"] == {"text": "mode"}
    assert recs["status"]["title"] == "Health" and recs["status"]["source"] == "perf.scan" and recs["status"]["read"]["map"] == {"value": "summary.warn"}
    assert recs["redis"]["source"] == "obs.redis" and recs["redis"]["read"]["map"] == {"value": "connected_clients"} and recs["redis"]["draw"]["unit"] == "clients"
    assert recs["postgres"]["read"]["map"] == {"value": "backends.postgres.total"} and recs["postgres"]["draw"]["unit"] == "records"
    assert recs["neo4j"]["form"] == "pills" and recs["neo4j"]["read"]["map"] == {"values": "backends.neo4j"}
    assert recs["host-temps"]["form"] == "temps" and recs["host-temps"]["read"]["map"] == {"values": "hosts", "name": "label", "value": "max_c"} and recs["host-temps"]["draw"]["throttle"] == 70
    # the lists are tables: columns named, sorted, paged
    assert recs["scheduler"]["form"] == "table" and recs["scheduler"]["read"]["map"] == {"rows": "$"} and recs["scheduler"]["draw"]["columns"] == ["name", "interval", "runs", "last"]
    assert recs["workerlist"]["form"] == "table" and recs["workerlist"]["source"] == "obs.workers" and recs["workerlist"]["read"]["map"] == {"rows": "$"}, "a dict keyed by worker id → rows named by key"
    assert recs["workerlist"]["draw"]["columns"] == ["name", "status", "host", "tasks_done", "tasks_failed", "cap_count"]
    assert recs["ollama"]["form"] == "cards" and recs["ollama"]["source"] == "sysmon.status" and recs["ollama"]["read"]["map"]["rows"] == "ollama.nodes", "cluster.ollama is not a capability"
    assert recs["sandboxes-info"]["form"] == "table" and recs["sandboxes-info"]["source"] == "sandbox.session.list" and recs["sandboxes-info"]["draw"]["columns"] == ["session_id", "state", "source", "last_used"], "remote.sandbox.list is a route"
    # the board's Live events feed and a Health tile, on sources the old page never showed
    assert recs["events"]["form"] == "feed" and recs["events"]["source"] == "obs.events" and recs["events"]["read"]["map"] == {"events": "$", "t": "ts", "kind": "type", "text": "name"}
    assert recs["health"]["form"] == "composite" and recs["health"]["source"] == "perf.scan" and [c["slot"] for c in recs["health"]["children"]] == ["summary", "findings"]
    for wid in converted:
        r = recs[wid]
        assert r["form"] and r["source"] and r["shape"], wid
        if r["form"] != "composite":
            assert r["read"].get("map") or wid == "diagnostics", (wid, "a converted tile names the part of its envelope it draws")
    # the breadth: the Dashboard board's cluster overview is many different forms on real sources
    forms = set()
    for r in recs.values():
        if r["draw"]["body"] == "record":
            forms.add(r["form"]); forms.update(c["record"]["form"] for c in r.get("children") or [])
    assert forms >= {"composite", "counter", "hero", "pills", "ring", "meter", "trace", "rows", "table", "cards", "feed", "kv", "temps", "string", "terminal"}, sorted(forms)
    assert 'draw.body = "record": the element draws the record and the page\'s markup is retired under it (VeraDash.drawTile)' in main["note"]
    assert "a composite reads its subject once and its children take $subject.<path> slices of it" in main["note"]
    for s in ("function drawTile(w, rec)", "holder.className = 'w-page'; holder.hidden = true;", "var pageBody = !rec.form || (rec.draw && rec.draw.body === 'page');",
              "if (eff) drawTile(w, eff);   // the record draws the body it owns; a page body stays the page's"):
        assert s in DASH, s


def test_add_widget_opens_the_widget_surface_and_every_tile_has_its_gear():
    """The WidgetConfig board on the dashboard: + Add Widget opens VeraWidgetConfig (mode add, into dashboard, the
    size ladder, templates) and places the record; the panel picker stays as openPanels and through a row of the
    picker; gear on every tile opens the surface on its record (mode edit) and Save writes it back; without the
    surface the record sheet opens; a record without a readable source draws the form's sample."""
    for s in ("window.VeraWidgetConfig.open({ mode: 'add', into: 'dashboard', title: 'Add a widget', sizes: ladderSizes(), templates: true, onValidating: st.onValidating, onValidated: st.onValidated })",
              "return openPanels();", "function openPanels()", "data-records", "function placeRecord(rec)",
              "window.VeraWidgetConfig.open({ mode: 'edit', record: was, into: 'dashboard', title: was.title || wid, anchor: anchor || w, sizes: ladderSizes(), templates: true,",
              "function ensureCfg(w)", "b.textContent = '⚙';", "function configure(wid, anchor)", "function applyRecord(wid, r)", "function openSheet(wid, rec)",
              "function sizeLadder()", "function sampleFor(form)", "function withSample(rec)", "if (ed) t.edited = true;", "if (r && t.edited) state.edits[wid] = r;",
              "sizeLadder: sizeLadder, ladderSizes: ladderSizes, sample: sampleFor, withSample: withSample"):
        assert s in DASH, s
    MIGDOC = _read("vera", "widgets", "migrate_layouts.py")
    assert "the tile carries ``edited: true``" in MIGDOC


def test_edit_mode_carries_the_dashboard_boards_cues_and_a_drop_on_the_grid():
    for s in ("function gridVars()", "if (state.editing) gridVars();", "function onGridDragOver(e)", "function onGridDrop(e)",
              "grid.addEventListener('dragover', onGridDragOver); grid.addEventListener('drop', onGridDrop);",
              "'.dash-grid.editing > .widget > .w-resize{display:flex;opacity:.85}'", "moving · drop on the grid", "'.widget.vd-landed{animation:vdLanded 1.6s ease-out}'",
              "function snapWidth(n)"):
        assert s in DASH, s


def test_the_composites_carry_the_sub_widgets_the_inventory_names():
    """A composite's children are full records: a slice of the subject's one read ($subject.<path>) or a source of
    their own with a map; four children (two rows of slots) is what an M tile's body holds."""
    main = {t["record"]["id"]: t["record"] for t in _layout("main")["widgets"]}
    for wid, subject in (("sysmon-proxmox", "sysmon.status"), ("sysmon-docker", "sysmon.status"), ("sysmon-ollama", "sysmon.status"),
                         ("host-resources", "sysmon.history"), ("queues", "jobs.stats"), ("mesh-info", "mesh.nodes"), ("looplab-info", "evolve.sandbox.status"),
                         ("connections", "ide.vscode.instances"), ("health", "perf.scan")):
        r = main[wid]
        assert r["form"] == "composite" and r["source"] == subject and r["draw"]["body"] == "record" and r["layout"] == "2x2", wid
        assert 2 <= len(r["children"]) <= 4, (wid, len(r["children"]))
        assert r["frame"]["max_body"] is None, (wid, "a composite fills its tile; the rows are the height")
        assert len(set(c["record"]["form"] for c in r["children"])) >= 2, (wid, "a composite mixes its forms")
        for c in r["children"]:
            k = c["record"]
            assert k["id"] == wid + ":" + c["slot"] and k["form"] and k["shape"] and k["source"], (wid, c)
            assert k["source"].startswith("$subject") or k.get("read", {}).get("map") or k["shape"] == "values", (wid, c["slot"], "a child of its own source says what it draws (a values form lists the envelope's own numbers and values)")
    ol_kids = {c["slot"]: c["record"] for c in main["sysmon-ollama"]["children"]}
    assert ol_kids["inuse"]["form"] == "ring" and ol_kids["inuse"]["source"] == "$subject.ollama" and ol_kids["inuse"]["read"]["map"] == {"value": "in_use", "max": "total"}
    assert ol_kids["nodes"]["form"] == "rows" and ol_kids["nodes"]["source"] == "$subject.ollama.nodes"
    assert main["sysmon-proxmox"]["children"][1]["record"]["form"] == "meter" and main["sysmon-proxmox"]["children"][1]["record"]["read"]["map"] == {"value": "mem_used_gb", "max": "mem_total_gb"}
    assert main["host-resources"]["children"][0]["record"]["read"]["map"] == {"v": "cpu"}, "a series of {t, cpu} rows: v renamed per row"
    assert main["queues"]["children"][3]["record"]["form"] == "feed" and main["queues"]["children"][3]["record"]["read"]["map"] == {"events": "entries", "t": "ts", "kind": "instance", "text": "model"}
    assert main["status"]["form"] == "counter" and main["status"]["source"] == "perf.scan"
    assert main["topology-map"]["form"] == "topology" and main["topology-map"]["frame"]["size"] == "xl" and main["topology-map"]["draw"]["body"] == "page"
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
    assert out["main"]["v"] == 2 and out["main"]["widgets"][0]["record"] == "status" and len(out["main"]["widgets"]) == 26   # the page's 24 tiles + the file's two record-only tiles
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
    assert r["ok"] and r["count"] == 7 and {x["key"]: x["widgets"] for x in r["layouts"]} == dict(COUNTS, main=26, **{"wol-ollama": 9})   # main: the page's 24 + two record-only tiles; wol-ollama: 8 + the Background Queue, record-only
    assert [x for x in r["layouts"] if x["key"] == "main"][0]["records"] == 56
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
