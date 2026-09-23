"""The Automations hub is the one home for the flow/runner surfaces.

Two regressions guarded here, both reported live 2026-09-12.

1. Clicking "n8n editor" made the whole Automations panel go unresponsive.
   openEditor() replaced its own iframe (`f.outerHTML = ...`, and on the other
   branch `insertAdjacentHTML` + `f.remove()`) with a div carrying a LITERAL
   class="native active". `panes` is captured once at page load, so show()
   never knew that node existed and could never clear its .active. Since
   #frames panes are position:absolute;inset:0, the orphan sat opaque and
   pointer-events:auto on top of Overview, DAG flows, n8n and Actions and ate
   their clicks - for the rest of the session.

   It fired on the HAPPY path, which is why it was reproducible: the
   "did the frame render" probe read `f.contentDocument?.body` on a
   cross-origin frame, which is null whether the embed succeeded or was
   refused, so a perfectly good n8n embed was torn down to display the
   "n8n would not embed" message.

   The rule that keeps it fixed: openEditor() may only toggle attributes on
   nodes that were in the document at load. It may not create, replace or
   remove a pane.

2. The DAG workshop, n8n, the Operator and Home Assistant each had a
   top-level harness tab AND a home inside this hub. They are registered
   mode="element" now: still in UI_PANELS (so the dashboard-widget loader,
   custom tabs and solo popout still find them) but not auto-rendered as a
   top-level tab. OpenClaw is the same story one step further along - it lost
   its top-level tab earlier, and 2026-09-12 removed its duplicate pane from
   the Workers/Ollama panel too.
"""
import ast
import io
import os
import re

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
PANEL = os.path.join(ROOT, "vera", "automations", "automations_panel.html")
WORKERS = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")

# panel_id -> source file that registers it. Each of these is reached through
# the Automations hub and must not also claim a top-level tab.
HUB_PANELS = {
    "dag-workshop": os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"),
    "n8n-panel": os.path.join(ROOT, "vera", "n8n", "n8n_capabilities.py"),
    "ha-panel": os.path.join(ROOT, "vera", "homeassistant", "ha_capabilities.py"),
    "operator-studio": os.path.join(ROOT, "vera", "operator",
                                    "operator_web_capabilities.py"),
}


def _read(path):
    return io.open(path, encoding="utf-8").read()


def _panel_js(src):
    return "\n".join(re.findall(r"<script>(.*?)</script>", src, flags=re.S))


def _markup(src):
    """The page with <script> and <style> bodies removed."""
    out = re.sub(r"<script\b.*?</script>", "", src, flags=re.S | re.I)
    return re.sub(r"<style\b.*?</style>", "", out, flags=re.S | re.I)


def _code_only(js):
    """JS with whole-line `//` comments dropped.

    The comments in this panel deliberately NAME the APIs that must not be
    called, so a check against the raw source would fail on its own rationale.
    """
    return "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))


# ---------------------------------------------------------------- structure

def test_every_subtab_has_a_pane():
    markup = _markup(_read(PANEL))
    subtabs = re.findall(r'class="[^"]*\bnav-btn\b[^"]*" data-k="([\w-]+)"', markup)
    assert len(subtabs) >= 8, subtabs
    for key in subtabs:
        # the nav entry plus at least one pane carrying the same key
        assert len(re.findall(r'data-k="%s"' % re.escape(key), markup)) >= 2, \
            "subtab %r has no pane" % key


def test_the_hub_embeds_every_surface_it_owns():
    markup = _markup(_read(PANEL))
    for key, route in (("flows", "/workshop/panel"),
                       ("n8n", "/n8n/panel"),
                       ("openclaw", "/openclaw/panel"),
                       ("operator", "/operator/panel"),
                       ("home", "/ha/panel")):
        assert re.search(r'data-k="%s"\s+data-src="%s"' % (key, re.escape(route)),
                         markup), "%s pane does not embed %s" % (key, route)


# ------------------------------------------------- the n8n-editor freeze

def test_open_editor_never_replaces_a_pane():
    js = _code_only(_panel_js(_read(PANEL)))
    m = re.search(r"function openEditor\(\)\s*\{(.*?)\n\}", js, flags=re.S)
    assert m, "openEditor() not found"
    body = m.group(1)
    for forbidden in ("outerHTML", "insertAdjacentHTML", ".remove()",
                      "createElement", "appendChild"):
        assert forbidden not in body, (
            "openEditor() uses %s. Panes are captured once into `panes` at load; "
            "a node created or swapped in here is invisible to show() and stays "
            "active on top of every other pane." % forbidden)


def test_no_pane_is_built_with_a_literal_active_class():
    js = _code_only(_panel_js(_read(PANEL)))
    assert "native active" not in js, (
        'a pane built with class="native active" can never be deactivated by '
        "show(), which only toggles .active on the panes present at load")


def test_the_editor_pane_is_a_stable_container():
    markup = _markup(_read(PANEL))
    assert re.search(r'<div class="native editor" data-k="editor">', markup), \
        "the editor pane must be a container div, not a bare iframe"
    for node_id in ("edFrame", "edBlocked", "edOpen", "edNote"):
        assert 'id="%s"' % node_id in markup, "%s missing" % node_id
    # The way out of a refused embed is permanent markup, not something a
    # detector has to conjure up after the fact.
    assert re.search(r'id="edOpen"[^>]*target="_blank"', markup)


def test_cross_origin_render_is_not_guessed_at():
    js = _code_only(_panel_js(_read(PANEL)))
    assert "contentDocument" not in js and "contentWindow" not in js, (
        "contentDocument/contentWindow are null for a cross-origin frame whether "
        "it rendered or was refused; probing them tore down working embeds")


def test_pane_css_does_not_reach_into_nested_frames():
    css = _read(PANEL)
    assert "#frames > iframe" in css and "#frames > .native" in css, (
        "pane rules must select direct children only - the editor pane holds an "
        "iframe of its own, which the descendant form pinned at opacity:0")


# ------------------------------------------------- no duplicated top-level tabs

def _register_ui_mode(path, panel_id):
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "register_ui" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == panel_id:
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    return kw.value.value
            return "inject"          # register_ui's own default
    return None


@pytest.mark.parametrize("panel_id", sorted(HUB_PANELS))
def test_hub_panels_do_not_claim_a_top_level_tab(panel_id):
    mode = _register_ui_mode(HUB_PANELS[panel_id], panel_id)
    assert mode is not None, "%s is no longer registered at all" % panel_id
    assert mode == "element", (
        '%s is registered mode=%r. It is reached through the Automations hub; '
        'mode="tab" puts the same panel in two places.' % (panel_id, mode))


def test_openclaw_has_no_pane_in_the_workers_panel():
    src = _read(WORKERS)
    assert "openclaw" not in src.lower(), (
        "OpenClaw is reached through the Automations hub; the Workers/Ollama "
        "panel must not carry a duplicate pane for it")


def test_the_workers_panel_nav_and_panes_still_line_up():
    """The removal above deletes a nav entry and its pane. If it ever deletes
    only one of the two, the panel gets a dead tab or an unreachable pane."""
    markup = _markup(_read(WORKERS))
    navs = re.findall(r'class="[^"]*\bnav-btn\b[^"]*" data-pane="([\w-]+)"', markup)
    panes = re.findall(r'class="[^"]*\bpane\b[^"]*" id="pane-([\w-]+)"', markup)
    assert navs, "no nav entries found - the selector drifted"
    missing = [n for n in navs if n not in panes]
    assert not missing, "nav entries with no pane: %s" % missing
