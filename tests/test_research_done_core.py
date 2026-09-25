"""A research-only step with its sources in hand is answered (plan item 17e).

run74-76 (25 Sep 2026): 14, 3 and 27 web.* calls per set came AFTER web.research
had returned the step's sources, each an executor turn.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag import research_done_core as R  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
LATEST = '{"ok": true, "sources": [{"url": "https://valkey.io/blog/", "text": "Valkey 8 ..."}]}'


def test_a_third_research_call_in_a_research_only_step_is_served():
    note = R.saturated_note(["web.research"], "web.search", 2, LATEST)
    assert "sources are IN HAND" in note and "valkey.io" in note and "emit `done`" in note
    assert R.saturated_note(["web.research", "web.search", "web.fetch"], "web.fetch", 3, LATEST)


def test_before_two_results_or_without_a_result_the_call_runs():
    assert R.saturated_note(["web.research"], "web.search", 1, LATEST) == ""
    assert R.saturated_note(["web.research"], "web.search", 2, "") == ""
    assert R.saturated_note(["web.research"], "web.search", "x", LATEST) == ""


def test_a_step_that_also_authors_or_runs_is_left_alone():
    assert R.saturated_note(["web.research", "prose.author"], "web.fetch", 2, LATEST) == ""
    assert R.saturated_note(["exec.bash.run", "http.get"], "http.get", 2, LATEST) == ""
    assert R.saturated_note(["web.research"], "prose.author", 2, LATEST) == ""


def test_the_second_served_call_ends_the_step():
    assert not R.should_end(1) and R.should_end(2) and not R.should_end("no")


def test_the_executor_serves_it_before_the_other_guards_and_counts_results():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v5_run_step_inner")
    body = ast.get_source_segment(src, fn)
    assert "_research_done.saturated_note(" in body
    assert body.index("_research_done.saturated_note(") < body.index("_author_done.reauthor_note(")
    assert 'if entry_ok and tool == "web.research":' in body and "research_latest = preview[:_budget]" in body
    assert "research_done_core as _research_done" in src
