"""code.author output salvage (_v5_clean_code_body) — the 2026-08-17 bug where an
unclosed ```python fence survived as line 1 (SyntaxError) and the editor role's
{"edits":...} repair-JSON leaked into the saved file. Imports the monolith, so it
runs in-container (not the pure critical gate).
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def test_strips_unclosed_fence_and_editor_json_leak():
    raw = ("```python\n"
           "# Track the timer state for this session run\n"
           "timer_state = None\n"
           "def run():\n"
           "    return timer_state\n"
           '{"edits":[], "note":"Line 1 starts with ```python but is never closed"}\n'
           '{"edits":[{"find":"x","replace":"y"}], "note":"more"}\n')
    out = W._v5_clean_code_body(raw)
    assert not out.lstrip().startswith("```")       # opening fence gone
    assert '"edits"' not in out and '"note"' not in out   # editor JSON leak gone
    assert "def run():" in out and "timer_state = None" in out
    ast.parse(out)                                   # must now parse


def test_leaves_clean_code_untouched():
    good = "import os\n\n\ndef f():\n    return os.getpid()\n"
    assert W._v5_clean_code_body(good) == good


def test_strips_balanced_wrapping_fences():
    out = W._v5_clean_code_body("```python\nprint('hi')\n```\n")
    assert out.strip() == "print('hi')"
    ast.parse(out)


def test_does_not_truncate_a_dict_inside_code():
    # an INDENTED "edits" key (a real dict literal inside code) is not the col-0 leak
    code = 'config = {\n    "edits": [],\n    "note": "ok",\n}\n'
    out = W._v5_clean_code_body(code)
    assert '"edits"' in out
    ast.parse(out)


def test_empty_and_none_safe():
    assert W._v5_clean_code_body("") == ""
    assert W._v5_clean_code_body(None) is None
