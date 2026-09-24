"""A stale edit anchor lands on its closest unique match (plan item 25).

run70-73 (24 Sep 2026): 11 code.edit calls were refused with "closest text
actually in the file - line N" because the anchor came from an earlier
version the model had itself edited; the hint named the line, the retry
re-typed it wrong. When exactly one region is a close match, anchor there.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag.edit_anchor_hint import nearest_unique_span  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
FILE = (
    '<input type="email" id="email" placeholder="Enter email" required onblur="validateEmail()">\n'
    '<span id="error" class="error"></span>\n'
    'const seconds = 90;\n'
    'let running = false;\n'
)


def test_the_census_case_lands_on_the_edited_line():
    stale = '<input type="email" id="email" placeholder="Enter email" required>'
    span = nearest_unique_span(FILE, stale)
    assert span == '<input type="email" id="email" placeholder="Enter email" required onblur="validateEmail()">'


def test_two_candidates_equally_close_means_no_guess():
    content2 = "def mean(x):\n    pass\ndef meann(x):\n    pass\n"
    assert nearest_unique_span(content2, "def meanx(x):") is None


def test_a_far_miss_is_refused():
    assert nearest_unique_span(FILE, "function totallyDifferent() {") is None


def test_a_multi_line_stale_anchor_keeps_its_height():
    stale = 'const seconds = 60;\nlet running = false;'
    assert nearest_unique_span(FILE, stale) == 'const seconds = 90;\nlet running = false;'


def test_the_editor_reanchors_and_reports_it():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_v5_apply_edits")
    body = ast.get_source_segment(src, fn)
    assert "_edit_anchor_hint.nearest_unique_span(out, find)" in body
    assert "reanchored_from" in body
    assert body.index("whitespace_only_span(out, find)") < body.index("nearest_unique_span(out, find)")
