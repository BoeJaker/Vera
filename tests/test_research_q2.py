"""Research: an appended analyst note never replaces the report, and only nodes whose
follow-ups can run plan them (four-minds Q2, 2026-10-05)."""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "research", "researcher_api.py")
TEXT = open(SRC, encoding="utf-8").read()
TREE = ast.parse(TEXT)


def _fns(*names):
    ns = {}
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name in names:
            exec(compile(ast.get_source_segment(TEXT, n), SRC, "exec"), ns)
    return ns


DRAFT = "# Redis licensing\n\nRedis moved to the SSPL in 2024. " + "Body text. " * 200


def test_a_verification_note_is_appended_not_substituted():
    merge = _fns("_merge_analyst_output")["_merge_analyst_output"]
    note = "## Verification\nClaims check out; one date is unsourced."
    out = merge(DRAFT, note)
    assert out.startswith("# Redis licensing") and out.rstrip().endswith("one date is unsourced.")


def test_a_full_rewrite_is_kept_and_empty_keeps_the_draft():
    merge = _fns("_merge_analyst_output")["_merge_analyst_output"]
    rewrite = DRAFT.replace("Body text.", "Revised text.") + "\n## Verification\nok"
    assert merge(DRAFT, rewrite) == rewrite.strip()
    assert merge(DRAFT, "") == DRAFT and merge(DRAFT, None) == DRAFT


def test_planning_slots_match_the_budget():
    slots = _fns("_planning_slots")["_planning_slots"]
    assert slots(11, 4) == 4          # level 1 with the defaults: every node plans, as before
    assert slots(0, 4) == 0 and slots(-3, 4) == 0   # level 2: nothing can run, nobody plans
    assert slots(5, 4) == 3


def test_the_level_loop_passes_the_budget():
    assert "plan_children=_i < _planners" in TEXT
    assert "if node.depth < MAX_RECURSIVE_DEPTH and plan_children:" in TEXT
    assert "return _merge_analyst_output(draft, result)" in TEXT
