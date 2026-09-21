"""Every graph companion route must serve a file that exists.

`vera_graph_panels._read()` returns the string `/* <name> not found at … */`
when the file is missing, rather than raising. That is deliberate — a missing
companion should not take the page down — but it means a renamed or moved JS
file serves a 200 with a comment in it, the panel silently never registers, and
nothing anywhere reports a problem. The failure is invisible from the server
side and looks like "the tab just isn't there" from the browser.

So: every filename the module hands to `_read()` must exist on disk, and every
companion must actually be reachable — a panel file that exists but has no
route is equally invisible.

Pure: reads source, checks the filesystem. No app import, no browser.
"""
import ast
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

pytestmark = pytest.mark.critical

_PANELS_PY = os.path.join(_ROOT, "vera", "vera_graph_panels.py")
_VERA = os.path.join(_ROOT, "vera")


def _source():
    with open(_PANELS_PY, encoding="utf-8") as fh:
        return fh.read()


def _read_targets(src):
    """Every literal filename passed to _read(...)."""
    return sorted(set(re.findall(r'_read\(\s*"([^"]+)"\s*\)', src)))


def _routes(src):
    """Every /ui/... path the module serves."""
    return sorted(set(re.findall(r'"(/ui/[A-Za-z0-9._-]+\.js)"', src)))


def test_every_served_file_exists():
    missing = [n for n in _read_targets(_source())
               if not os.path.exists(os.path.join(_VERA, n))]
    assert not missing, (
        "these files are served but do not exist, so the route returns a "
        f"'not found' COMMENT with HTTP 200 and the panel silently vanishes: {missing}")


def test_the_module_actually_serves_something():
    """Guard the guard: if the regex stopped matching, the test above would
    pass by finding nothing to check."""
    targets = _read_targets(_source())
    assert len(targets) >= 4, f"only found {targets} — the detector has drifted"
    assert any("explode" in t for t in targets)
    assert any("graph_embed" in t for t in targets)


def test_the_new_companions_have_routes():
    routes = _routes(_source())
    assert "/ui/vera-graph-panel-explode.js" in routes
    assert "/ui/vera-graph-embed.js" in routes


def test_explode_is_in_the_standard_script_set():
    """A companion with a route but not in VERA_GRAPH_PANEL_SCRIPTS only appears
    on pages that happen to include it by hand — which is how a panel ends up
    working in one place and missing in another."""
    src = _source()
    # Match the ASSIGNMENT, not the first mention — the name also appears in the
    # module docstring, and splitting on that reads the wrong region entirely.
    m = re.search(r"^VERA_GRAPH_PANEL_SCRIPTS\s*=\s*\((.*?)\n\)",
                  src, re.S | re.M)
    assert m, "VERA_GRAPH_PANEL_SCRIPTS assignment not found"
    block = m.group(1)
    assert "vera-graph-panel-explode.js" in block


def test_capability_names_are_unique():
    """Two capabilities with the same name silently overwrite each other in the
    registry, and the second one wins."""
    names = re.findall(r'@capability\(\s*"([^"]+)"', _source())
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate capability names: {dupes}"


def test_embed_element_defines_the_custom_element_once():
    """A second define() of the same tag throws and kills the script, taking the
    element with it."""
    p = os.path.join(_VERA, "graph_embed_element.js")
    js = open(p, encoding="utf-8").read()
    assert js.count("customElements.define(") == 1
    assert "customElements.get('vera-graph-embed')" in js, (
        "define() is not guarded — a second load of this file throws")


def test_embed_turns_off_every_piece_of_chrome():
    """The inline graph must be bare. If a control creeps back in it will sit
    inside a chat turn, which is exactly what this element exists to avoid."""
    js = open(os.path.join(_VERA, "graph_embed_element.js"), encoding="utf-8").read()
    for opt in ("showSearch: false", "showLegend: false", "showLeftPanel: false",
                "layerUI: false", "actionsEnabled: false"):
        assert opt in js, f"inline graph does not disable chrome: {opt}"


def test_embed_does_not_fork_the_renderer():
    """It must configure vera_graph.js, not reimplement it — a fork would drift
    and the inline and full views would stop agreeing."""
    js = open(os.path.join(_VERA, "graph_embed_element.js"), encoding="utf-8").read()
    assert "veraUI.Graph.create" in js
    assert "fetchSnapshot" in js


def test_explode_panel_registers_through_the_documented_contract():
    js = open(os.path.join(_VERA, "vera_graph_panel_explode.js"), encoding="utf-8").read()
    assert "veraUI.Graph.registerPanel" in js
    for field in ("id:", "title:", "icon:", "mount:"):
        assert field in js, f"panel definition missing {field}"
    # It must degrade rather than throw when loaded without the core.
    assert "registerPanel" in js.split("if (!window.veraUI")[1][:400], (
        "no guard for being loaded before vera_graph.js")
