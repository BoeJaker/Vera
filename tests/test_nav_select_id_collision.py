"""A host's injected menu must select the item the panel registered, not a
heading that happens to share the name.

The Estate panel marks every item with the view that owns it
(`data-view="estate"`) and gives its map pane `data-pane="estate"`. The bridge
resolved nav_select through one selector list, so document order decided, and
`[data-view="estate"]` matched first: selecting "Map" clicked the "Overview"
section heading and nothing happened, while every other item in the same menu
worked (18 Sep 2026).

Two guards: the bridge tries the attributes one at a time, most specific first;
and the panel registers a select callback so it never depends on that fallback.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.critical
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

ROOT = os.path.join(os.path.dirname(__file__), "..")
BRIDGE = os.path.join(ROOT, "vera", "chat", "vera-panel-bridge.js")
PANEL = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")

# The collision, as the live panel has it: headings carry the view name, the map
# pane carries it as its own id, and the heading comes first in document order.
HARNESS = r"""
const SRC = require('fs').readFileSync(process.argv[2], 'utf8');
const m = SRC.match(/var ATTRS = \[([\s\S]*?)\];/);
if(!m) { console.log(JSON.stringify({error: 'ATTRS list is gone'})); process.exit(0); }
const attrs = m[1].split(',').map(s => s.trim().replace(/^'|'$/g, '')).filter(Boolean);

// A stand-in for the panel's nav: a heading and the items, in document order.
const dom = [
  {tag: 'DIV', cls: 'nsec',  attrs: {'data-view': 'estate'}, text: 'Overview'},
  {tag: 'DIV', cls: 'snav',  attrs: {'data-pane': 'overview', 'data-view': 'estate'}, text: 'Overview'},
  {tag: 'DIV', cls: 'snav',  attrs: {'data-pane': 'estate',   'data-view': 'estate'}, text: 'Map'},
  {tag: 'DIV', cls: 'snav',  attrs: {'data-pane': 'docker',   'data-view': 'estate'}, text: 'Docker'},
];
function resolve(id){
  for(const a of attrs){
    const hit = dom.find(e => e.attrs[a] === id);
    if(hit) return hit;
  }
  return null;
}
console.log(JSON.stringify({
  attrs,
  map:      resolve('estate'),
  overview: resolve('overview'),
  docker:   resolve('docker'),
}));
"""


def _resolved():
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(HARNESS)
        path = f.name
    try:
        r = subprocess.run(["node", path, BRIDGE], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[:1500]
        import json
        return json.loads(r.stdout)
    finally:
        os.unlink(path)


@needs_node
def test_the_item_wins_over_a_heading_that_shares_its_name():
    out = _resolved()
    assert not out.get("error"), out.get("error")
    assert out["map"]["text"] == "Map" and out["map"]["cls"] == "snav", \
        "selecting the map still lands on the section heading"
    assert out["overview"]["attrs"]["data-pane"] == "overview"
    assert out["docker"]["attrs"]["data-pane"] == "docker"


@needs_node
def test_data_view_is_tried_last_because_it_groups_rather_than_names():
    out = _resolved()
    assert out["attrs"][-1] == "data-view"
    assert "data-pane" in out["attrs"] and out["attrs"].index("data-pane") < out["attrs"].index("data-view")


def test_the_bridge_resolves_one_attribute_at_a_time():
    """Runs without node: a single querySelector list would hand the choice back
    to document order, which is the bug."""
    src = open(BRIDGE, encoding="utf-8").read()
    body = re.search(r"function _navSelect\(p\)\{?(.+?)\n  \}", src, re.S)
    assert body, "_navSelect is gone"
    text = body.group(1)
    assert "ATTRS" in text and "for(" in text, "the attributes are no longer tried in order"
    assert not re.search(r'querySelector\(\s*\n?\s*.\[data-sec="', text), \
        "back to one combined selector list, where document order decides"


def test_the_estate_panel_registers_its_own_select_callback():
    """Belt and braces: this panel must not depend on the bridge's fallback,
    because its pane ids and its data-view values share a namespace."""
    html = open(PANEL, encoding="utf-8").read()
    call = re.search(r"VeraPanelBridge\.registerNav\((.+?)\);", html, re.S)
    assert call, "the panel no longer registers its nav"
    args = call.group(1)
    assert "showPane(" in args and "data-pane=" in args, \
        "registerNav no longer passes a select callback that maps an id to its pane"
