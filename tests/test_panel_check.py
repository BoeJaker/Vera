"""The gate's UI half: a panel with conflict markers or an unparseable inline
script must not pass. Marked critical because an adopt actually passed with
markers in a panel on 2026-09-11 and would have shipped a dead page."""
import os, sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.evolve import panel_check as P

pytestmark = pytest.mark.critical

GOOD = "<html><script>\nconst a=1;\nfunction f(){return a}\n</script><script src='/x.js'></script></html>"
MARKERS = "<html><script>\nconst a=1;\n<<<<<<< HEAD\nconst b=2;\n=======\nconst b=3;\n>>>>>>> bleeding-edge\n</script></html>"
BROKEN = "<html>\n<script>\nfunction f( {\n</script></html>"


def test_conflict_markers_are_found_with_their_lines():
    assert P.conflict_markers(MARKERS) == [3, 5, 7]
    assert P.conflict_markers(GOOD) == []


def test_a_bare_equals_line_inside_prose_is_still_a_marker_only_when_alone():
    """'=======' on its own line is the middle marker; '==' in code is not."""
    assert P.conflict_markers("a === b\nx == y\n") == []
    assert P.conflict_markers("a\n=======\nb") == [2]


def test_only_inline_classic_scripts_are_extracted():
    s = P.inline_scripts(GOOD)
    assert len(s) == 1 and s[0]["line"] == 1
    assert P.inline_scripts('<script src="a.js"></script>') == []
    assert P.inline_scripts('<script type="module">import x from "y"</script>') == []
    assert P.inline_scripts('<script type="application/json">{"a":1}</script>') == []


def test_markers_fail_the_check_even_without_node():
    r = P.check_html(MARKERS, use_node=False)
    assert not r["ok"]
    assert any("conflict marker" in p for p in r["problems"])


def test_without_node_the_script_check_is_reported_skipped_not_passed():
    """A gate that cannot run a check must say so, not say PASS."""
    r = P.check_html(GOOD, use_node=False)
    assert r["ok"] and r["skipped"]


@pytest.mark.skipif(not P.node_available(), reason="node not on this box")
def test_a_broken_inline_script_fails_with_its_line():
    r = P.check_html(BROKEN)
    assert not r["ok"]
    assert any("line 2" in p and "SyntaxError" in p for p in r["problems"]), r


@pytest.mark.skipif(not P.node_available(), reason="node not on this box")
def test_the_exact_2026_09_11_case_is_caught():
    """Conflict markers inside a script: BOTH the marker check and node see it."""
    r = P.check_html(MARKERS)
    assert not r["ok"]
    assert len(r["problems"]) >= 2


@pytest.mark.skipif(not P.node_available(), reason="node not on this box")
def test_a_good_panel_passes():
    assert P.check_html(GOOD)["ok"]


def test_check_file_routes_by_extension():
    assert P.check_file("x/y_panel.html", GOOD, use_node=False)["ok"]
    assert not P.check_file("x/y.js", "a\n=======\nb", use_node=False)["ok"]
    assert P.check_file("x/y.py", "<<<<<<< HEAD", use_node=False)["ok"]   # not this gate's job
    assert P.is_ui_file("a.html") and P.is_ui_file("b.js") and not P.is_ui_file("c.py")


ESM = 'import fs from "fs";\nexport const x = fs.existsSync("/");\n'
CJS = 'const fs = require("fs");\nmodule.exports = { x: fs.existsSync("/") };\n'


@pytest.mark.skipif(not P.node_available(), reason="node not on this box")
def test_a_valid_esm_mjs_file_passes():
    """node decides script vs module by EXTENSION. The first version copied
    every file to a temp .js, so valid ESM failed on its first `import` -
    pipeline 3b28bcbf rejected tests/test_widget_element.mjs that way."""
    assert P.check_file("tests/x.mjs", ESM)["ok"]


@pytest.mark.skipif(not P.node_available(), reason="node not on this box")
def test_a_valid_cjs_file_passes_and_a_broken_one_fails():
    assert P.check_file("tests/x.cjs", CJS)["ok"]
    assert not P.check_file("tests/x.cjs", "function (")["ok"]


@pytest.mark.skipif(not P.node_available(), reason="node not on this box")
def test_esm_syntax_in_a_plain_js_file_is_still_rejected():
    """Unchanged on purpose: a .js in this repo is a classic script (the panels
    are inline scripts) and `import` at top level of one is a real error."""
    assert not P.check_file("x.js", ESM)["ok"]
