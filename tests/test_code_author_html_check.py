"""Regression guard for code.author's HTML structural check.

The broken-app failure mode — a coder that narrated its reasoning into the file
and "restarted" the document (a second <head>/<body>, JS left in an open
<script>) — sailed through because HTML had no real syntax check: html.parser is
too lenient. `_v5_check_syntax` now runs `_html_structural_error` for html/htm,
so the repair loop catches it and code.author returns ok=False instead of a
false success. These pin that contract: real breakage is flagged, valid pages
pass, and a '<body>' inside JS/comments is NOT a false positive.

Pure/deterministic — just regex over a string, no I/O.
"""
import pytest

from vera.dag import dag_workshop_capabilities as m

pytestmark = pytest.mark.critical


def _chk(html):
    return m._v5_check_syntax(html, "html", "index.html")


def test_unbalanced_script_is_flagged():
    broken = ('<!DOCTYPE html><html><head></head><body><div>hi</div>\n'
              '<script file=script.js>\n// restart\n</head><body>\n'
              '<script>console.log(1)</script>')
    r = _chk(broken)
    assert r["ok"] is False
    assert "script" in r["error"].lower()


def test_duplicate_body_is_flagged():
    r = _chk("<html><head></head><body>a</body><body>b</body></html>")
    assert r["ok"] is False
    assert "body" in r["error"].lower()


def test_valid_selfcontained_page_passes():
    good = ('<!DOCTYPE html><html><head><style>body{color:red}</style></head>'
            '<body><div id="a">hi</div>'
            '<script>document.getElementById("a").textContent="ok"</script>'
            '</body></html>')
    assert _chk(good)["ok"] is True


def test_body_inside_script_or_comment_is_not_a_false_positive():
    tricky = ('<!DOCTYPE html><html><head></head><body>'
              '<script>el.innerHTML="<body>x</body>"; /* <head> */ var s="<html>";</script>'
              '</body></html>')
    assert _chk(tricky)["ok"] is True


def test_two_balanced_scripts_pass():
    two = ('<html><head></head><body><script>var a=1</script>'
           '<script src="x.js"></script></body></html>')
    assert _chk(two)["ok"] is True


def test_helper_is_empty_for_a_clean_fragment():
    assert m._html_structural_error("<div><p>hi</p></div>") == ""
