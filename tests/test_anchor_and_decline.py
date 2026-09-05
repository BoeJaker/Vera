"""Census 33: code.edit stopped failing on JSON and started failing on anchors.

Removing the forced JSON output mode worked - author-then-edit passed for the
first time in four censuses and every "not the requested JSON object" error on
a real edit disappeared. What remained were six failures of three new shapes:

  1. `find` differs from the file ONLY in indentation (2 cases). The hint code
     already detected it - "It DOES appear at line 41 apart from whitespace" -
     and then asked the model to retype the anchor more carefully, which is
     precisely what it had just got wrong.
  2. A DECLINE reported as a malformed reply (1 case). The editor answered
     "<<<NOTE>>> No changes needed as the requested 'blur' event listener is
     already present" - correct, in the format we asked for - and
     looks_like_blocks demanded EDIT+REPLACE, so it fell through to the JSON
     path and was reported as unparseable, then re-run.
  3. A code fence INSIDE a block (1 case). research-report sent an anchor
     beginning "```markdown\\n# Redis and Valkey:" - the model fenced markdown
     out of habit, and no such text is in the file.

Note on the namespace trap: dag_workshop imports edit_blocks as
Vera.vera.dag.edit_blocks, which resolves to the MAIN checkout, so a test that
just imports dag_workshop exercises main's copy and passes regardless of this
branch. The wiring tests below INJECT the worktree module.

Pure: no LLM, no browser.
"""
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vera.dag import edit_blocks as EB            # noqa: E402
from vera.dag import edit_anchor_hint as AH       # noqa: E402

pytestmark = pytest.mark.critical


def _worktree_module(name):
    """This branch's copy of a vera.dag module, regardless of what
    Vera.vera.dag resolves to (it resolves to the MAIN checkout)."""
    spec = importlib.util.spec_from_file_location(
        "_wt_" + name, os.path.join(ROOT, "vera", "dag", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _worktree_edit_blocks():
    """This branch's edit_blocks, regardless of what Vera.vera.dag resolves to."""
    spec = importlib.util.spec_from_file_location(
        "_wt_edit_blocks", os.path.join(ROOT, "vera", "dag", "edit_blocks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. an anchor that differs only in indentation ──────────────────────────

FILE = ("body {\n"
        "  margin: 0;\n"
        "}\n"
        "  .timer-container {\n"
        "      text-align: center;\n"
        "  }\n"
        "footer { }\n")

MODEL_ANCHOR = ("    .timer-container {\n"
                "        text-align: center;\n"
                "    }")


def test_the_span_returned_is_the_files_own_text_not_the_models():
    """Anchoring on the model's version would replace nothing; the file's own
    text is what has to be substituted."""
    span = AH.whitespace_only_span(FILE, MODEL_ANCHOR)
    assert span == "  .timer-container {\n      text-align: center;\n  }"
    assert span in FILE


def test_a_single_line_anchor_also_recovers():
    assert AH.whitespace_only_span("a\n    x = 1\nb\n", "x = 1") == "    x = 1"


def test_an_anchor_matching_two_places_is_refused():
    """A whitespace-insensitive compare makes collisions MORE likely, so the
    exactly-once invariant matters more here, not less."""
    twice = "  x = 1\nmiddle\n    x = 1\n"
    assert AH.whitespace_only_match(twice, "x = 1") == [1, 3]
    assert AH.whitespace_only_span(twice, "x = 1") is None


def test_an_anchor_that_is_genuinely_absent_returns_nothing():
    assert AH.whitespace_only_span(FILE, "def nowhere():") is None
    assert AH.whitespace_only_span("", "x") is None
    assert AH.whitespace_only_span(FILE, "") is None


def test_the_span_does_not_swallow_a_trailing_newline():
    """Replacing a span that includes the newline would join two lines."""
    span = AH.whitespace_only_span("a\n  x = 1\nb\n", "x = 1")
    assert span == "  x = 1" and not span.endswith("\n")


def _apply(content, edits):
    """Run the REAL _v5_apply_edits with this branch's hint module injected."""
    import vera.dag.dag_workshop_capabilities as DW
    real = DW._edit_anchor_hint
    DW._edit_anchor_hint = _worktree_module("edit_anchor_hint")
    try:
        return DW._v5_apply_edits(content, edits)
    finally:
        DW._edit_anchor_hint = real


def test_the_matcher_now_applies_a_whitespace_only_anchor():
    """End to end through the real _v5_apply_edits."""
    res = _apply(FILE, [{"find": MODEL_ANCHOR, "replace": "  .tc { }"}])
    assert res["ok"] is True, res["errors"]
    assert "  .tc { }" in res["content"]
    assert ".timer-container" not in res["content"]


def test_an_exact_anchor_still_wins_and_is_untouched():
    """The verbatim path must be tried first - a file may legitimately contain
    text that only differs from another region by whitespace."""
    res = _apply(FILE, [{"find": "body {\n  margin: 0;\n}", "replace": "body { }"}])
    assert res["ok"] is True and "body { }" in res["content"]


def test_a_truly_missing_anchor_is_still_refused_with_the_hint():
    res = _apply(FILE, [{"find": "def nowhere():", "replace": "x"}])
    assert res["ok"] is False and res["errors"]


def test_an_ambiguous_whitespace_anchor_is_refused_end_to_end():
    """The exactly-once invariant must survive the new fallback."""
    twice = "  x = 1\nmiddle\n    x = 1\n"
    res = _apply(twice, [{"find": "x = 1", "replace": "y = 2"}])
    assert res["ok"] is False, "an ambiguous anchor was applied"


# ── 2. a decline is an answer ───────────────────────────────────────────────

DECLINE = ("<<<NOTE>>>\nNo changes needed as the requested 'blur' event "
           "listener is already present in the code.\n")


def test_a_note_only_reply_is_recognised_as_blocks():
    wt = _worktree_edit_blocks()
    assert wt.looks_like_blocks(DECLINE) is True


def test_a_note_only_reply_parses_as_a_decline_not_an_error():
    wt = _worktree_edit_blocks()
    r = wt.parse(DECLINE)
    assert r["edits"] == []
    assert r["error"] == "", "a decline must not be reported as a parse failure"
    assert "already present" in r["note"]


def test_blocks_with_no_note_and_no_edits_is_still_an_error():
    """Silence is not a decline - there is nothing to report to the caller."""
    wt = _worktree_edit_blocks()
    r = wt.parse("<<<EDIT>>>\nonly a find\n<<<END>>>\n")
    assert r["edits"] == [] and r["error"]


def test_the_decline_reaches_classify_as_DECLINED():
    """The wiring: _editor_obj_from_reply must hand classify {edits: [], note}
    rather than {} - injected, because the module-level import resolves to the
    main checkout."""
    import vera.dag.dag_workshop_capabilities as DW
    from vera.dag import editor_reply as ER
    real = DW._edit_blocks
    DW._edit_blocks = _worktree_edit_blocks()
    try:
        obj = DW._editor_obj_from_reply(DECLINE)
        assert obj.get("note"), "the decline was lost: %r" % obj
        verdict = ER.classify(obj, DECLINE)
        assert verdict["kind"] == ER.DECLINED
        assert verdict["retry_worthwhile"] is False, "a decline must not be re-run"
    finally:
        DW._edit_blocks = real


# ── 3. a fence inside a block ───────────────────────────────────────────────

def test_a_fence_wrapping_a_section_is_stripped():
    wt = _worktree_edit_blocks()
    r = wt.parse("<<<EDIT>>>\n```markdown\n# Redis and Valkey\n```\n"
                 "<<<REPLACE>>>\n```markdown\n# Redis\n```\n<<<END>>>\n")
    assert r["edits"][0]["find"] == "# Redis and Valkey"
    assert r["edits"][0]["replace"] == "# Redis"


def test_a_fence_in_the_MIDDLE_of_a_section_is_real_content():
    """A markdown file that contains a code block - stripping that would
    corrupt the anchor."""
    wt = _worktree_edit_blocks()
    body = "# Title\n\n```py\nx = 1\n```\n\nmore"
    r = wt.parse("<<<EDIT>>>\n" + body + "\n<<<REPLACE>>>\nnew\n<<<END>>>\n")
    assert r["edits"][0]["find"] == body


def test_an_unpaired_fence_is_left_alone():
    wt = _worktree_edit_blocks()
    r = wt.parse("<<<EDIT>>>\n```markdown\n# Title\n<<<REPLACE>>>\nnew\n<<<END>>>\n")
    assert r["edits"][0]["find"].startswith("```markdown")
