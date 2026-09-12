"""The Ollama panel's panes must be laid out inside #panes.

.pane is position:absolute; inset:0, so it is placed against its nearest
positioned ancestor, and #panes is meant to be that ancestor. A pane that ends
up anywhere else is placed against the viewport instead: it starts at x=0 and
covers the section rail, and the operator is stuck on whichever pane is showing.

That happened on 2026-09-11. Removing the Background Queue widget (a8f3b4e) left
the widget's own closing </div> behind. It closed the Ollama grid early, the
cascade closed #panes straight after the Ollama pane, and every pane from
Observe onward became a child of #shell. The file still passed `node --check`
and the conflict-marker scan, because unbalanced markup is not a syntax error.
"""
import os
from html.parser import HTMLParser

import pytest

_PANEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "vera", "workers", "workers_ollama_panel.html")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "source", "track", "wbr"}
_BLOCK = {"div", "section", "main", "aside", "nav", "table", "form"}
_GRIDS = {"w-grid", "ol-grid", "j-grid", "obs-grid"}

pytestmark = pytest.mark.critical


class _Tree(HTMLParser):
    # <script> and <style> bodies are CDATA to HTMLParser, so markup inside
    # template strings is not mistaken for the page's own structure.
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.nodes, self.problems = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        node = {"tag": tag, "id": a.get("id") or "", "cls": (a.get("class") or "").split(),
                "wid": a.get("data-wid") or "", "line": self.getpos()[0],
                "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                over = [n for n in self.stack[i + 1:] if n["tag"] in _BLOCK]
                if over:
                    self.problems.append("line %d: </%s> also closes %s" % (
                        self.getpos()[0], tag,
                        ", ".join("<%s id=%r> from line %d" % (n["tag"], n["id"], n["line"]) for n in over)))
                del self.stack[i:]
                return
        self.problems.append("line %d: </%s> has nothing to close" % (self.getpos()[0], tag))


def structure_problems(html):
    """Every way the layout can silently come apart, as readable strings."""
    t = _Tree()
    t.feed(html)
    t.close()
    out = list(t.problems)
    panes = [n for n in t.nodes if "pane" in n["cls"]]
    if len(panes) < 2:
        out.append("found %d panes - the pane check would pass vacuously" % len(panes))
    for n in panes:
        p = n["parent"]
        if not p or p["id"] != "panes":
            out.append("pane %r (line %d) is inside %r, not #panes" % (n["id"], n["line"], p and p["id"]))
    holder = [n for n in t.nodes if n["id"] == "panes"]
    if not holder or not holder[0]["parent"] or holder[0]["parent"]["id"] != "shell":
        out.append("#panes is not a direct child of #shell")
    for n in t.nodes:
        if "widget" not in n["cls"]:
            continue
        anc, grid = n["parent"], None
        while anc is not None:
            if anc["id"] in _GRIDS:
                grid = anc
                break
            anc = anc["parent"]
        if grid is not None and n["parent"] is not grid:
            out.append("widget %r (line %d) is nested inside %r instead of sitting in %r" % (
                n["wid"], n["line"], n["parent"]["id"] or n["parent"]["tag"], grid["id"]))
    return out


def _problems(kind):
    html = open(_PANEL, encoding="utf-8").read()
    return [p for p in structure_problems(html) if kind(p)]


def test_markup_closes_what_it_opens():
    probs = _problems(lambda p: p.startswith("line "))
    assert not probs, "; ".join(probs)


def test_every_pane_is_laid_out_inside_panes():
    probs = _problems(lambda p: p.startswith(("pane ", "#panes", "found ")))
    assert not probs, "; ".join(probs)


def test_every_widget_sits_directly_in_its_grid():
    probs = _problems(lambda p: p.startswith("widget "))
    assert not probs, "; ".join(probs)
