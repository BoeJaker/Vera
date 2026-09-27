"""The Loop Lab's lenses: every tile a widget record, placeable anywhere; the page rotates through them."""

import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from vera.widgets import widget_record as REC  # noqa: E402

LAY = os.path.join(ROOT, "vera", "widgets", "layouts")
KEYS = ["looplab", "looplab-gates", "looplab-census", "looplab-perf", "looplab-loops", "looplab-agents", "looplab-branches", "looplab-work"]


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def _lay(k):
    return json.loads(_read("vera", "widgets", "layouts", k + ".json"))


def test_the_generator_and_the_files_agree():
    sys.path.insert(0, os.path.join(ROOT, "vera", "widgets"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_looplab_lenses", os.path.join(ROOT, "vera", "widgets", "gen_looplab_lenses.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    built = gen.build()
    assert list(built) == KEYS
    for k in KEYS:
        assert built[k] == _lay(k), k + ": run python3 vera/widgets/gen_looplab_lenses.py"


def test_every_tile_is_a_record_the_element_draws():
    forms = set(REC.form_ids())
    ids = []
    for k in KEYS:
        lay = _lay(k)
        assert lay["v"] == 2 and lay["key"] == k and lay["name"] and lay["note"]
        assert lay["grid"] == {"cols": 12, "row": 58, "gap": 10, "widths": [2, 3, 4, 6, 8, 12]}
        cells = set()
        for t in lay["widgets"]:
            r = t["record"]
            ids.append(r["id"])
            assert r["id"].startswith(k + "-"), r["id"]
            assert r["form"] in forms, (k, r["form"])
            assert r["draw"]["body"] == "record" and r["title"]
            assert t["span"] == r["frame"]["span"] and r["frame"]["size"] == REC.size_for_span(*t["span"])
            x, y = t["at"]
            w, h = t["span"]
            assert x + w <= 12, (k, r["id"])
            for cx in range(x, x + w):          # no two tiles on one cell
                for cy in range(y, y + h):
                    assert (cx, cy) not in cells, (k, r["id"], cx, cy)
                    cells.add((cx, cy))
            if r["form"] == "element":
                assert re.match(r"^vera-[a-z-]+$", r["draw"]["tag"]) and not r["source"]
            else:
                assert r["source"], (k, r["id"])
    assert len(ids) == len(set(ids)), "tile ids are DOM ids on one page - a repeat drops a lens's tile"


def test_every_source_reads_on_its_own_and_writes_nothing():
    js = _read("vera", "widgets", "widget_element.js")
    m = re.search(r"const readable = \(cap\) => (/.+?/)\.test\(cap\)", js)
    assert m
    for k in KEYS:
        for t in _lay(k)["widgets"]:
            src = t["record"]["source"]
            if not src:
                continue
            assert re.search(r"^(ci|loop\.ci|census|evolve|ollama|perf)\.", src), src
            assert not re.search(r"(write|delete|remove|create|run|exec|kill|restart|stop|start|set|save|send|post|push)\b", src), src
    for name in ("ci\\.(matrix|race|tests|pulse|fleet|board|census", "loop\\.ci\\.(matrix|race|board", "census\\.(runs|landed"):
        assert name in js, name


def test_the_element_form_mounts_the_loop_lab_elements_it_names():
    js = _read("vera", "widgets", "widget_element.js")
    for tag in ("vera-git-graph", "vera-author-map", "vera-test-activity-timeline", "vera-error-radar", "vera-branch-pipeline"):
        assert "'" + tag + "'" in js, tag
    assert "function mountElement(el, rec)" in js and "canon(form) === 'element' && size !== 'xs' && size !== 's'" in js


def test_the_page_rotates_through_the_lenses():
    html = _read("vera", "evolve", "evolve_panel.html")
    assert '<div class="card" id="lens-card">' in html and '<script src="/ui/vera-dashboard.js"></script>' in html
    for k in KEYS[:-1]:
        assert "key:'" + k + "'" in html, k
    assert '<vera-dashboard layout="looplab-work"' in html, "the Work page leads with its own lens"
    for s in ("d.setAttribute('layout',key)", "id=\"lensRotate\"", "pointerenter", "e.key===']'"):
        assert s in html, s


def test_the_loop_lab_widgets_are_templates_any_surface_can_place():
    reg = _read("vera", "widgets", "widget_registry.py")
    assert "_LHM_BUILTINS.extend(_looplab_templates())" in reg
    assert '"placed": ["dashboard", "canvas"]' in reg
