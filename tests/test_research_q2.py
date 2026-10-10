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


def test_scored_sources_keep_the_good_ones_and_top_up_to_four():
    cur = _fns("_curate_scored")["_curate_scored"]
    scored = [(5, "a"), (4, "b"), (3, "c"), (2, "d"), (1, "e"), (0, "f")]
    assert cur(scored) == ["a", "b", "c", "d"]
    assert cur([(5, "a"), (4, "b"), (3, "c"), (3, "d"), (2, "e"), (0, "f")]) == ["a", "b", "c", "d", "e"]
    # few score well: the scores still rank, the best four are kept (they used to be thrown away)
    assert cur([(3, "a"), (1, "b"), (1, "c"), (0, "d"), (0, "e"), (0, "f")]) == ["a", "b", "c", "d"]
    assert cur([(1, "a"), (0, "b")]) == ["a", "b"]
    assert cur([]) == []


def test_research_node_uses_the_curation():
    assert "good = _curate_scored(scored)\n                if good:" in TEXT
