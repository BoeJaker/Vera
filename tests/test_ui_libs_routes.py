"""The shared run routers and the structured graph are served from vera/ui/libs.py, and the exploded scene draws
with the shared copy rather than one of its own (EXPLODE.md §5.1: one implementation, never a fork)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBS = (ROOT / "vera" / "ui" / "libs.py").read_text(encoding="utf-8")
ROUTES = (ROOT / "vera" / "ui" / "routes.js").read_text(encoding="utf-8")
STRUCT = (ROOT / "vera" / "ui" / "structgraph_element.js").read_text(encoding="utf-8")
EXPLODED = (ROOT / "vera" / "chat" / "exploded_element.js").read_text(encoding="utf-8")


def test_the_libraries_are_served_and_exist():
    for route, name in (("/ui/routes.js", "routes.js"), ("/ui/structgraph.js", "structgraph_element.js"), ("/ui/iso.js", "iso.js")):
        assert '@APP.get("%s"' % route in LIBS, route
        assert '_script("%s")' % name in LIBS, name
        assert (ROOT / "vera" / "ui" / name).exists(), name


def test_the_routers_live_in_routes_js_once():
    for fn in ("function cardsRouter(G)", "function isoRouter(G)", "function channelRouter(G)", "function rank(ids, edges)", "function order(cells, edges, yOf)"):
        assert ROUTES.count(fn) == 1, fn
        assert fn not in EXPLODED, "the exploded scene must not keep its own copy of " + fn
        assert fn not in STRUCT
    assert "root.VeraRoutes = api" in ROUTES


def test_the_exploded_scene_resolves_the_shared_routers_and_loads_them_on_a_page():
    assert "const cardsRouter = (G) => { const L = routesLib();" in EXPLODED
    assert "const isoRouter = (G) => { const L = routesLib();" in EXPLODED
    assert "s.src = '/ui/routes.js'" in EXPLODED
    assert "ensureRoutes(this.ownerDocument, () => this._schedule())" in EXPLODED
    # a render before the routers land waits for them rather than throwing into the page
    assert "if (!routesLib()) { ensureRoutes(this.ownerDocument, () => this._schedule()); return; }" in EXPLODED


def test_the_structured_graph_defines_its_element_once_and_is_pure_where_it_says():
    assert STRUCT.count("customElements.define('vera-structgraph'") == 1
    assert "root.VeraStructGraph = api" in STRUCT
    assert "function layout(doc, W, H, o)" in STRUCT
    # every run is styled by its kind AND its resolution — an unmarked guess is worse than no edge
    assert "e.resolution === 'heuristic' ? ' heur' : e.resolution === 'external' ? ' ext' : ''" in STRUCT
    # the runs are routed through the shared channel router, never drawn as chords
    assert "R.channelRouter({ gutters: geo.gutters, channels: geo.channels" in STRUCT
    assert re.search(r"fit.*never below half", STRUCT, re.S), "the embedded stage keeps its text readable"
