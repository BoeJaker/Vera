"""The UI scale must be painted once per frame tree, not once per frame.

CSS `zoom` inherits into an iframe's content, so a panel that zooms itself
inside the already-zoomed shell renders at scale squared. That squeezed the
Estate panel's left menu into a strip, and because Chromium maps pointer
coordinates into a nested zoomed frame from the unscaled geometry, each menu
item's hit area sat left of where it was painted: clicking "Map" did nothing
(18 Sep 2026). vera-ui.js runs in every frame, so the guard lives there.

Runs vera-ui.js's scale logic under a stub DOM in node, as a top-level document
and as a frame whose parent has already zoomed.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.critical
# The two behavioural tests run the real scale helpers, so they need node; they
# skip rather than pass quietly where it is missing (the gate's test container).
# The static checks below carry the guard in every environment.
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

ROOT = os.path.join(os.path.dirname(__file__), "..")
UI_JS = os.path.join(ROOT, "vera", "vera-ui.js")
SHELL = os.path.join(ROOT, "vera", "capability_orchestration.html")

# The scale helpers, lifted out of the file's IIFE so they can run without the
# rest of the panel runtime (fetch, WebSocket, DOM ready hooks).
HARNESS = r"""
const SRC = require('fs').readFileSync(process.argv[2], 'utf8');
function take(name){
  const i = SRC.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('missing ' + name);
  let depth = 0, started = false;
  for (let j = i; j < SRC.length; j++){
    if (SRC[j] === '{'){ depth++; started = true; }
    else if (SRC[j] === '}'){ depth--; if (started && depth === 0) return SRC.slice(i, j + 1); }
  }
  throw new Error('unterminated ' + name);
}
const scaleCode = ['_clampScale', '_readScale', '_ancestorPaintedScale', '_paintScale']
  .map(take).join('\n');

function makeDoc(){
  const style = {zoom: '', width: '', height: '', _props: {},
    setProperty(k, v){ this._props[k] = v; }, removeProperty(k){ delete this._props[k]; }};
  return {documentElement: {style}};
}
function run(scale, ancestorZoom){
  const document = makeDoc();
  const top = {document: makeDoc()};
  if (ancestorZoom) top.document.documentElement.style.zoom = String(ancestorZoom);
  const self = {document};
  self.parent = ancestorZoom == null ? self : top;   // no ancestor => top-level frame
  top.parent = top;
  const localStorage = {getItem: () => String(scale), setItem(){}};
  const SCALE_KEY = 'vera:ui:scale', SCALE_MIN = 0.6, SCALE_MAX = 2.0, SCALE_DEFAULT = 1.0;
  const fn = new Function('window', 'document', 'localStorage', 'SCALE_KEY', 'SCALE_MIN',
                          'SCALE_MAX', 'SCALE_DEFAULT',
    scaleCode + '\nreturn {paint: _paintScale, read: _readScale};');
  const api = fn(self, document, localStorage, SCALE_KEY, SCALE_MIN, SCALE_MAX, SCALE_DEFAULT);
  api.paint(api.read());
  const s = document.documentElement.style;
  return {zoom: s.zoom, width: s.width, uiScale: s._props['--ui-scale'] || ''};
}
console.log(JSON.stringify({
  top_scaled:      run(1.4, null),
  nested_in_scaled: run(1.4, 1.4),
  top_plain:       run(1, null),
  nested_in_plain:  run(1.4, 0),
  clamped_high:    run(9, null),
}));
"""


def _run_node():
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(HARNESS)
        path = f.name
    try:
        r = subprocess.run(["node", path, UI_JS], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[:2000]
        return json.loads(r.stdout)
    finally:
        os.unlink(path)


@needs_node
def test_a_top_level_document_still_paints_the_scale():
    out = _run_node()
    assert out["top_scaled"] == {"zoom": "1.4", "width": "calc(100vw / 1.4)", "uiScale": "1.4"}
    assert out["top_plain"]["zoom"] == "" and out["top_plain"]["uiScale"] == ""
    assert out["clamped_high"]["zoom"] == "2"


@needs_node
def test_a_frame_inside_a_scaled_ancestor_inherits_instead_of_multiplying():
    out = _run_node()
    assert out["nested_in_scaled"] == {"zoom": "", "width": "", "uiScale": ""}
    # A frame whose ancestors are NOT scaled paints normally (a panel opened on
    # its own, or embedded in a page that left the scale at 100%).
    assert out["nested_in_plain"]["zoom"] == "1.4"


def test_only_the_shell_boot_and_vera_ui_paint_zoom():
    """Any third place that sets documentElement.style.zoom would reintroduce the
    multiplication, since each runs in its own frame."""
    zoomers = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "vera")):
        for name in files:
            if not name.endswith((".html", ".js")):
                continue
            p = os.path.join(dirpath, name)
            with open(p, encoding="utf-8", errors="replace") as fh:
                if re.search(r"documentElement\s*\.\s*style\s*\.\s*zoom\s*=|d\.style\.zoom\s*=", fh.read()):
                    zoomers.append(os.path.relpath(p, ROOT).replace("\\", "/"))
    assert sorted(zoomers) == ["vera/capability_orchestration.html", "vera/vera-ui.js"]


def test_the_shell_boot_runs_only_in_the_top_document():
    """It is inline in the shell page itself, which is never framed by us."""
    text = open(SHELL, encoding="utf-8").read()
    assert "d.style.zoom=String(sc)" in text
    assert "/ui/panels/" not in text.split("</head>")[0]


def test_the_guard_is_wired_into_the_painter():
    """Runs everywhere, node or not: the walk up the frame chain exists, it looks
    at an ancestor's own zoom, and _paintScale consults it before painting."""
    src = open(UI_JS, encoding="utf-8").read()
    guard = re.search(r"function _ancestorPaintedScale\(\)\s*\{(.+?)\n  \}", src, re.S)
    assert guard, "the frame-chain guard is gone"
    body = guard.group(1)
    assert "w.parent" in body and "documentElement.style.zoom" in body
    assert "try" in body and "catch" in body, "a cross-origin ancestor must not throw"
    paint = re.search(r"function _paintScale\(s\)\s*\{(.+?)\n  \}", src, re.S)
    assert paint and "_ancestorPaintedScale()" in paint.group(1), \
        "_paintScale no longer consults the guard"
