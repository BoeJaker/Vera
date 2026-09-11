"""
The unified graph, pass A (UI redesign canvas-graph part 2; the GraphViews
board): the memory graph panel draws one document of families with the mixer
and the board's views beside every view it had; families.js is served; the
chat's graph column posts the turn's context and the turns as families. The
files are text, so this runs anywhere (the adapters run under node in
tests/test_graph_families.cjs).
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


PANEL = _read("vera", "fabric", "memory_graph_panel.html")
ROUTES = _read("vera", "vera_graph_panels.py")
CHAT = _read("vera", "chat", "chat_panel.html")
FAM = _read("vera", "graph", "families.js")


def test_families_are_served_and_loaded_by_the_panel():
    assert '@APP.get("/ui/graph/families.js", include_in_schema=False)' in ROUTES and 'Path(__file__).parent / "graph" / "families.js"' in ROUTES
    assert '<script src="/ui/graph/families.js"></script>' in PANEL and '<script src="/ui/iso.js"></script>' in PANEL
    assert "root.VeraGraphFamilies = api;" in FAM and "module.exports = api;" in FAM


def test_the_panel_has_the_views_the_mixer_and_keeps_its_own():
    for chip in ("vchip-galaxy", "vchip-iso", "vchip-flow", "vchip-force", "vchip-timeline", "vchip-hierarchy", "vchip-radial"):
        assert 'id="%s"' % chip in PANEL, chip
    assert 'id="family-chips"' in PANEL and "function rebuildFamilyChips()" in PANEL and "function cycleFamily(f)" in PANEL
    assert "['force','timeline','hierarchy','radial','galaxy','iso','flow'].forEach" in PANEL
    assert "_applyGalaxyLayout(false);" in PANEL and "_applyGalaxyLayout(!!window.VeraISO);" in PANEL
    assert "} else if (_viewMode === 'hierarchy' || _viewMode === 'flow') {" in PANEL
    g = PANEL[PANEL.index("function _applyGalaxyLayout(iso) {"):PANEL.index("function _famPostToParent()")]
    assert "VeraISO.proj(30, 45, 1, true)" in g and "F.FAMILIES.map(x => x.id)" in g, "iso goes through the shared projection; sectors follow the families' order"
    assert "if (_famVisible && !_famVisible.has(n.id)) return false;" in PANEL, "the mixer gates visibility"
    assert "family: rec.family || (rec.non_authoritative ? 'dag' : 'memory')," in PANEL
    assert "if (d.type === 'vera:graph:family' && d.family)" in PANEL and "if (d.type === 'vera:graph:anchor')" in PANEL
    # Note 39 §23: what the unified graph must keep — the panel's own controls are all still here
    for fn in ("function onSearch()", "function rebuildFilters()", "function selectNode(id)", "async function expandSel()", "function focusSel()", "async function labelSel()",
               "async function runCap()", "function loadSessionList()", "function setWindow(hours)", "async function loadOlder()", "function toggleCleanupMenu()",
               "function _applyTimelineLayout()", "function _applyHierarchyLayout()", "function _applyRadialLayout()", "function warmupPhysics(ticks)", "function fitAll()",
               'id="node-detail"', 'id="cap-runner"', 'id="session-picker"', 'id="type-chips"', 'id="edge-chips"', 'id="source-chips"'):
        assert fn in PANEL, fn + " still there"


def test_the_chat_posts_its_families_with_the_anchor():
    assert "try{ _graphFamiliesPost(frame, msg.mid); }catch(_){}" in CHAT
    f = CHAT[CHAT.index("function _graphFamiliesPost(frame, mid){"):CHAT.index("// which turn is in focus:")]
    assert "family:'context', payload:ctx" in f and "family:'turns', payload:{turns}" in f
    assert "turn:mid||''" in f, "the context is retrieved into the focused turn"
    assert "if(sig===_grFamSig) return;" in f, "posted only when it changed"
