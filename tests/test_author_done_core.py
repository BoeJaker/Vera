"""An authoring-only step is answered by its author call (plan item 17c).

run74 (25 Sep 2026): after a parser-verified code.author the executor spent ten
turns reading the file back, serving it or running it. The author's verdict is
the step's answer when the step's planned work is that one file.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag import author_done_core as A  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
OK = {"ok": True, "path": "clock.html", "fs_path": "/workspace/clock.html", "bytes": 2100,
      "syntax_ok": True, "checked_with": "html5lib"}


def test_the_census_shape_ends_the_step():
    ans, why = A.step_is_answered(["code.author"], "Create self-contained clock.html\nWrite clock.html with a live clock",
                                  "code.author", OK)
    assert ans and "verified by html5lib" in why and "no read-back" in why


def test_a_step_that_plans_more_than_authoring_does_not_end():
    assert not A.step_is_answered(["code.author", "exec.bash.run"], "Create clock.html", "code.author", OK)[0]
    assert not A.step_is_answered(["code.author", "operator.run"], "Create form.html and verify it", "code.author", OK)[0]


def test_a_second_part_or_a_second_file_keeps_the_step_open():
    assert not A.step_is_answered(["code.author"], "Create timer.html with a 60 second countdown, then change it to 90 seconds",
                                  "code.author", dict(OK, path="timer.html"))[0]
    assert not A.step_is_answered(["code.author"], "Create stats.py and test_stats.py", "code.author",
                                  dict(OK, path="stats.py"))[0]
    assert A.step_is_answered(["code.author"], "Create the statkit package's test file", "code.author",
                              dict(OK, path="test_stats.py"))[0]


def test_only_a_verified_successful_author_call_counts():
    assert not A.step_is_answered(["code.author"], "Create clock.html", "code.author", dict(OK, syntax_ok=False))[0]
    assert not A.step_is_answered(["code.author"], "Create clock.html", "code.author", {"ok": False, "error": "x"})[0]
    assert not A.step_is_answered(["code.author"], "Create clock.html", "code.edit", OK)[0]
    assert not A.step_is_answered(["code.author"], "Create clock.html", "code.author", "ok")[0]
    assert A.step_is_answered(["prose.author"], "Write summary.md", "prose.author", {"ok": True, "path": "summary.md"})[0]


def test_the_authored_file_must_be_the_named_one():
    assert not A.step_is_answered(["code.author"], "Create clock.html", "code.author", dict(OK, path="index.html"))[0]
    assert A.files_named("Create /workspace/statkit/stats.py providing mean") == ["stats.py"]


def test_the_steer_and_journal_tail_is_not_the_steps_text():
    """run76 research-web: the goal carried the controller steer and the run
    journal, with URLs and other steps' files, so the one-file test refused."""
    goal = ("Draft summary document citing sources\nWrite a short markdown report citing the URLs found in step 1."
            "\n\nCONTROLLER STEER (after step 1, alignment: advances): Proceed to draft the summary using prose.author, "
            "then verify it.\n\nRUN JOURNAL (structured context already produced by earlier steps - reuse these outputs):"
            " step 1 wrote notes.md and read https://caniuse.com/webgpu.html and https://developer.mozilla.org/x.md")
    assert A.files_named(A.own_text(goal)) == []
    ans, why = A.step_is_answered(["prose.author"], goal, "prose.author", {"ok": True, "path": "summary.md"})
    assert ans, why


def test_a_file_this_step_wrote_is_not_written_again():
    note = A.reauthor_note("prose.author", "summary.md", '{"ok": true, "path": "summary.md", "bytes": 4173}', failed_since=False)
    assert "ALREADY written summary.md" in note and "code.edit" in note and "4173" in note
    assert A.reauthor_note("prose.author", "summary.md", "", failed_since=False) == ""          # first write
    assert A.reauthor_note("prose.author", "summary.md", "earlier", failed_since=True) == ""    # a failure since: rewrite may be needed


def test_the_executor_ends_the_step_before_the_stuck_loop_guard():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v5_run_step_inner")
    body = ast.get_source_segment(src, fn)
    assert "_author_done.step_is_answered(" in body
    assert body.index("_author_done.step_is_answered(") < body.index("_tool_call_n = tool_calls.get(tool, 0)")
    assert "agent_loop_v5.author_answered_step" in body
    assert 'step.get("caps") or caps' in body                       # 17c judges the PLANNED caps
    assert body.index("_author_done.reauthor_note(") < body.index("_call_sig = _v5_call_sig(tool, args)")
    assert "authored_here[_apath2] = preview[:_budget]" in body
    assert "author_done_core as _author_done" in src
