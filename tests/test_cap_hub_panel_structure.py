"""The Capabilities tab (vera/capabilities/cap_hub.html) is one page per concern.

Pins what a restructure of that large file can silently break: unbalanced
markup (a stray close tag moves every later page out of #panes), an element the
page script looks up by a fixed id that no longer exists, the rail, the shell
menu and the page list drifting apart, the chip bars under the search coming
back, and /cap_hub/elements.js losing the web components it serves."""
import ast
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "vera" / "capabilities" / "cap_hub.html"
PY = ROOT / "vera" / "capabilities" / "cap_hub_capabilities.py"

pytestmark = pytest.mark.critical

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


class _Tree(HTMLParser):
    """Each id's direct parent, and every end tag that closes the wrong element."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []        # open elements as (tag, id)
        self.parent = {}       # id -> (tag, id) of the direct parent
        self.ids = set()
        self.mismatched = []

    def _note(self, attrs):
        el_id = dict(attrs).get("id") or ""
        if el_id:
            self.ids.add(el_id)
            self.parent[el_id] = self.stack[-1] if self.stack else None
        return el_id

    def handle_starttag(self, tag, attrs):
        el_id = self._note(attrs)
        if tag not in VOID:
            self.stack.append((tag, el_id))

    def handle_startendtag(self, tag, attrs):
        self._note(attrs)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            return
        self.mismatched.append((tag, self.getpos()))
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] == tag:
                del self.stack[k:]
                break


def _html():
    return HTML.read_text(encoding="utf-8")


def _page_part():
    """Everything before the reusable web components: the page and its script."""
    html = _html()
    return html[:html.index("INJECT ELEMENTS")]


@pytest.fixture(scope="module")
def tree():
    t = _Tree()
    t.feed(_html())
    t.close()
    return t


def test_markup_is_balanced(tree):
    assert tree.mismatched == []
    assert tree.stack == []


def test_rail_shell_menu_and_page_list_agree(tree):
    html = _page_part()
    panes = re.findall(r"'([\w-]+)'", re.search(r"const PANES = \[([^\]]*)\]", html).group(1))
    rail = re.findall(r'class="snav[^"]*"\s+data-pane="([\w-]+)"', html)
    menu = re.findall(r"\{id: '([\w-]+)'", re.search(r"registerNav\(\[(.*?)\]\)", html, re.S).group(1))
    assert panes == rail == menu == ["browse", "tracking", "ontology", "mcp", "modules"]
    for p in panes:
        assert tree.parent.get("pane-" + p) == ("div", "panes"), p
    assert tree.parent.get("panes") == ("div", "shell")


def test_every_fixed_id_the_page_script_uses_exists(tree):
    part = _page_part()
    used = set(re.findall(r"getElementById\('([\w-]+)'\)", part))
    used |= set(re.findall(r"setText\('([\w-]+)'", part))
    assert sorted(used - tree.ids) == []


def test_no_chip_bars_under_the_search():
    part = _page_part()
    assert 'id="grp-chip-bar"' not in part and 'id="trk-filter-bar"' not in part
    assert "renderGrpChips" not in part


def test_elements_js_serves_the_web_components():
    mod = ast.parse(PY.read_text(encoding="utf-8"))
    keep = [n for n in mod.body
            if (isinstance(n, ast.FunctionDef) and n.name in {"_panel_html", "_elements_js"})
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_ELEMENTS_MARKER_START" for t in n.targets))]
    scope = {"_HERE": HTML.parent}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(PY), "exec"), scope)
    js = scope["_elements_js"]()
    for tag in ("vera-cap-list", "vera-cap-search", "vera-activity-track",
                "vera-mcp-servers", "vera-job-stream"):
        assert f"customElements.define('{tag}'" in js, tag
    assert "function refreshAll" not in js
