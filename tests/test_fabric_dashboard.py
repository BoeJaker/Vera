"""
The Data Fabric's own widgets and dashboard (owner, 2026-09-27: "id like some widgets that can display the data
fabric and incoming data feeds perhaps the data fabric should have a dashboard and widgets of its own").

Held here, without Vera booting:
  - the fabric:* templates are built-ins of the widget registry, each a valid template that reads a fabric
    capability and carries the read.map its answer needs;
  - a template keeps its read.map and its form's draw options through the registry's normalisation (the element
    draws a template with both) - and a template without them is unchanged;
  - the layout file vera/widgets/layouts/fabric.json places those templates: every tile's record is its template's
    (source, form, args, map, draw options), the spans fit the 12-column grid without overlap, no tile is taller
    than VeraDash draws (six rows);
  - the Fabric panel reaches it: an Overview menu item, its section, VeraDash booted on the 'fabric' key, and the
    section known to the panel's bridge.
"""
import ast
import importlib.util
import json
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _builtins():
    src = _read("vera", "widgets", "widget_registry.py")
    m = re.search(r"_LHM_BUILTINS: List\[Dict\[str, Any\]\] = (\[.*?\n\])\n", src, re.S)
    assert m, "the registry's built-in list"
    return ast.literal_eval(m.group(1))


def _record_module():
    spec = importlib.util.spec_from_file_location("widget_record_fabric_test", os.path.join(ROOT, "vera", "widgets", "widget_record.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _record_module()
FABRIC = [t for t in _builtins() if t["id"].startswith("fabric:")]
BY_ID = {t["id"]: t for t in _builtins()}
LAYOUT = json.loads(_read("vera", "widgets", "layouts", "fabric.json"))


def test_the_fabric_templates_are_built_ins_that_read_the_fabric():
    assert len(FABRIC) >= 15
    ids = [t["id"] for t in FABRIC]
    assert len(ids) == len(set(ids))
    for t in FABRIC:
        n = R.normalise_template(dict(t))
        assert R.template_problems(n, ("counter",), ("dashboard", "canvas", "LHM")) == [], (t["id"], R.template_problems(n, ("counter",), ("dashboard", "canvas", "LHM")))
        assert R.form(t["form"]), t["id"]
        assert t["reads"]["cap"].startswith("fabric."), t["id"]
        assert (t.get("read") or {}).get("map"), "%s maps its answer to its form" % t["id"]
    caps = {t["reads"]["cap"] for t in FABRIC}
    for cap in ("fabric.health", "fabric.stats", "fabric.sources", "fabric.discover.history", "fabric.tags.list_grouped"):
        assert cap in caps, cap


def test_the_incoming_feeds_have_their_own_widgets():
    feeds = [t for t in FABRIC if t["reads"]["cap"] == "fabric.sources"]
    forms = {t["form"] for t in feeds}
    assert {"table", "ranked", "column"} <= forms
    fresh = BY_ID["fabric:feed-fresh"]
    assert fresh["draw"]["sort"] == "last_pulled" and "last_pulled" in fresh["draw"]["columns"]
    assert BY_ID["fabric:crawls"]["form"] == "log" and BY_ID["fabric:crawls"]["read"]["map"]["events"] == "crawls"


def test_a_template_keeps_its_map_and_draw_options_and_one_without_them_is_unchanged():
    t = R.normalise_template(dict(BY_ID["fabric:feed-fresh"]))
    assert t["read"]["map"] == {"rows": "sources"}
    assert t["draw"]["columns"][0] == "label" and t["draw"]["sort"] == "last_pulled" and t["draw"]["form"] == "table"
    plain = R.normalise_template({"name": "GPU", "form": "meter", "reads": {"cap": "sysmon.status"}})
    assert "read" not in plain and plain["draw"] == {"form": "meter", "size": "M", "motion": ""}
    # the full record's map and options survive the fold to the template shape
    back = R.to_template({"id": "x", "form": "ranked", "source": "fabric.sources", "title": "X",
                          "read": {"map": {"values": "sources", "count": "source_type"}}, "draw": {"limit": 8}})
    assert back["read"]["map"] == {"values": "sources", "count": "source_type"} and back["draw"]["limit"] == 8


def test_the_layout_places_the_templates_as_they_are():
    assert LAYOUT["key"] == "fabric" and LAYOUT["dashboard"] == "fabric" and LAYOUT["v"] == 2
    tiles = [w for w in LAYOUT["widgets"] if w["record"]["form"] != "section"]
    placed = {w["record"]["template"] for w in tiles}
    assert placed >= {t["id"] for t in FABRIC}, "every fabric template is on the overview"
    assert "graph:fabric" in placed
    for w in tiles:
        r = w["record"]
        t = BY_ID[r["template"]]
        assert r["source"] == t["reads"]["cap"] and r["form"] == t["form"] and r["title"] == t["name"], r["id"]
        assert r["read"]["args"] == t["reads"]["args"], r["id"]
        assert r["read"]["map"] == (t.get("read") or {}).get("map", {}), r["id"]
        for k, v in t["draw"].items():
            if k not in ("form", "size", "motion"):
                assert r["draw"][k] == v, (r["id"], k)
        assert r["draw"]["body"] == "record"
        assert r["frame"]["span"] == w["span"] and r["frame"]["size"] == R.size_for_span(*w["span"]), r["id"]


def test_the_layout_fits_the_grid():
    occ = set()
    for w in LAYOUT["widgets"]:
        (c, r), (sw, sh) = w["at"], w["span"]
        assert sw in LAYOUT["grid"]["widths"] and 1 <= sh <= 6 and c + sw <= 12, w["record"]["id"]
        cells = {(y, x) for y in range(r, r + sh) for x in range(c, c + sw)}
        assert not (cells & occ), "overlap at " + w["record"]["id"]
        occ |= cells
    rows = max(y for y, _ in occ) + 1
    assert len(occ) == rows * 12, "the overview is packed: no hole in its grid"


def test_the_fabric_panel_opens_its_overview():
    html = _read("vera", "fabric", "fabric_panel.html")
    assert 'id="fnav-dash" data-section="dash" onclick="fabSection(\'dash\')"' in html
    assert '<div id="fsec-dash" style="display:none">' in html and 'id="fabDashGrid"' in html
    assert "['dash','datasets','sources'," in html, "fabSection shows and hides the overview"
    assert "if(name==='dash')fabDashBoot();" in html
    assert "var SECTIONS = ['dash','datasets'," in html, "the bridge's go_to_section knows it"
    assert "VeraDash.init(g,{key:'fabric'," in html and "s.src='/ui/vera-dashboard.js'" in html
