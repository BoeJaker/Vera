"""code.author GENERATES code from a task — it must REFUSE pasted code.

Design intent (owner, 2026-08-18): the entire point of code.author is that the
CODING SPECIALIST writes the code from a plain-English task. It must never be
given code to merely save/verify — a caller that writes the code itself and
hands it over (as `content`) has bypassed the coder, which defeats the cap.
Observed live: the executor generated 4137 chars of HTML and passed it as
`content` with no `task`, and code.author (via its old content-recovery) just
saved it. Now it refuses that and redirects to task-based generation.

The refusal happens before any LLM call, so no generation is exercised.
Imports the monolith module, so it runs in-container.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def test_pasted_code_is_refused_and_redirected_to_task():
    r = asyncio.run(W.cap_code_author(
        path="index.html",
        content="<!DOCTYPE html><html><body><h1>Pomodoro</h1></body></html>"))
    assert r["ok"] is False, r
    msg = r["error"].lower()
    assert "generate" in msg                      # says code.author generates
    assert "task" in msg                          # points to task
    assert "do not" in msg and "code" in msg      # forbids passing code


def test_no_task_no_content_still_asks_for_a_task():
    r = asyncio.run(W.cap_code_author(path="script.py"))
    assert r["ok"] is False
    assert "task is required" in r["error"].lower()
    assert "not code" in r["error"].lower()       # task is a description, not code


def test_description_advertises_generate_not_content(monkeypatch):
    # The signature the model sees must NOT invite passing code, and must say GENERATE.
    sig = W.rich_cap_signature("code.author")
    assert "GENERATE" in sig
    assert "content='<code you already wrote>'" not in sig   # no longer advertised
    assert "path AND task are BOTH REQUIRED" in sig
