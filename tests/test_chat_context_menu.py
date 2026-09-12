"""
One context menu on every entity (the Canvas board's `.cmenu`): the chat page draws the registry's rows
(/ui/menus.js — window.MENUS.rows) for whatever was right-clicked — a message, a capability card, a context record,
a quick-menu row (session · family · loop · record), an attachment, an exploded item, a canvas item, the composer,
the canvas — binds every action id to the chat's own functions (nothing new behind them), and a capability row opens
the STAGED CAPABILITY RUNNER (the board's `.crun`: the JSON, Send, the receipt). Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")


def test_the_registry_is_loaded_and_the_menu_is_drawn_from_it():
    assert '<script src="/ui/menus.js"></script>' in HTML, "the shared menu registry"
    assert "function _cmOpen(ev, kind, name, el, x){" in HTML
    assert "MENUS.rows(kind, name, {noPin:kind==='canvas'||kind==='view'})" in HTML, "rows from the registry"
    assert "seen[k]=1; return true; }" in HTML, "one row per action, whatever the registry repeats"
    assert ".cmenu{" in HTML and ".cm-h" in HTML and ".cm-i" in HTML and ".cm-sep" in HTML
    assert "context menu · menu" in HTML, "a labelled widget"


def test_every_entity_resolves_to_a_kind():
    assert "function _cmTarget(t){" in HTML
    for probe in (".cap-inline", ".cg-node[data-kind]", ".vw-gd", "[data-key]", ".mwrap", ".lrow[data-sid]", ".lrow[data-fam]", ".lhm-quick .lps", ".lrow[data-rid]"):
        assert probe in HTML, probe
    assert "addEventListener('contextmenu'" in HTML and "ev.shiftKey" in HTML, "shift keeps the browser's menu"


def test_actions_bind_to_the_chats_own_functions():
    assert "function _cmAct(id" in HTML
    for fn in ("_msgEdit", "_msgResubmit", "_msgCopy", "_msgSaveToNotebook", "_msgSaveToIde", "_msgSaveToProject", "_msgThinkLater", "_msgPodcast", "_msgHtmlReport", "_msgRedeliver", "_ctxGrow", "loadSession"):
        assert fn in HTML, fn
    assert "_capCall('canvas.add'" in HTML, "pin = a keyed canvas item through the resolver"


def test_the_staged_capability_runner():
    assert "function _crunOpen(cap, arg, target, cx, cy){" in HTML
    assert ".crun{" in HTML and ".crun-b" in HTML and ".crun-r" in HTML
    assert "JSON.parse(" in HTML and "_capCall(cap" in HTML, "the staged JSON is sent through the chat's capability call"
    assert "_cmOpen,_cmClose,_crunOpen,_cmTarget" in HTML, "exported for the host and the tests"
