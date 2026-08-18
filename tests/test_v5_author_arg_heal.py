"""Authoring-cap arg self-heal — fill code.author/prose.author task/path from the step.

A BUILD step ("Create index.html", cap code.author) reliably burned 3-4 cycles
because the specialist fumbled the authoring contract: code.author(language='html')
and code.author() both fail "task is required" before it finally lands
content+path. The step ITSELF already describes the file (its goal = what to do,
its success/title = the filename), so _v5_heal_author_args fills the missing
`task`/`path` deterministically. These tests pin that pure helper.

Imports the monolith module, so it runs in-container (see test_loop_cancel.py).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


_STEP = {"id": 1, "title": "Create index.html",
         "goal": "Create the index.html file with the HTML structure for a pomodoro timer.",
         "success": "index.html exists and contains the timer markup"}


def _apply(tool, args, step=_STEP):
    for fld, val, _note in W._v5_heal_author_args(tool, args, step):
        args[fld] = val
    return args


def test_fills_task_and_path_when_both_missing():
    a = _apply("code.author", {"language": "html"})
    assert a["task"].startswith("Create the index.html"), a
    assert a["path"] == "index.html", a


def test_fills_task_when_call_is_empty():
    a = _apply("code.author", {})
    assert a.get("task"), a
    assert a.get("path") == "index.html", a


def test_does_not_override_provided_task_or_path():
    a = _apply("code.author", {"task": "do the real thing", "path": "custom.html"})
    assert a["task"] == "do the real thing"
    assert a["path"] == "custom.html"


def test_content_draft_suppresses_task_fill():
    # When the model already drafted the file and passed it as content, code.author's
    # own content-recovery handles it — do NOT inject a task that would change intent.
    a = _apply("code.author", {"content": "<!DOCTYPE html>...", "path": "index.html"})
    assert "task" not in a, a


def test_prose_author_also_healed():
    step = {"title": "Write the report", "goal": "Write a summary report of the findings.",
            "success": "report.md exists"}
    a = _apply("prose.author", {}, step)
    assert a.get("task"), a
    assert a.get("path") == "report.md", a


def test_code_edit_gets_task_but_not_path():
    # code.edit takes no `content`/path-from-nothing here; it heals task only.
    a = _apply("code.edit", {"path": "app.py"})
    assert a.get("task"), a
    assert a["path"] == "app.py"   # untouched


def test_non_authoring_tool_untouched():
    a = _apply("exec.bash.run", {"command": "ls"})
    assert a == {"command": "ls"}
