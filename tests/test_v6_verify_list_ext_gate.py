"""v6 verify — the extension-class file gate must skip LIST/IDENTIFICATION criteria.

The 2026-08-17 incident: a "Define app requirements and tech stack" step ran with
the planner-written criterion

    "A clear list of required files (index.html, style.css, script.js) and core
     logic components is generated."

That is a LIST criterion — it NAMES the files the app will have as its content,
not files that must be on disk now. The step correctly produced a requirements
document (a .md), but `_v6_verify_step`'s extension-class grounding gate pulled
`.html/.css/.js` out of the criterion TEXT and hard-failed the step for the
working directory not containing those web files. That false failure drove a
retry which, told "you need .html/.css/.js", generated the whole app's CODE
inside the requirements step (via llm.generate). The named-path existence gate
one block up already excludes list criteria via `_V6_FILE_LIST_CRIT_RE`; the
extension gate simply hadn't applied the same guard.

These tests pin: the extension gate is suppressed for a list/identification
criterion, but still fires for a genuine "a file with extension X must exist"
criterion. The filesystem + judge helpers are mocked so no real I/O or LLM call
happens; the list-gate returns before the judge, and for the non-list cases the
judge is mocked to a clean pass so only the gate decides the outcome.

Imports the monolith module, so it runs in-container (see test_loop_cancel.py).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def _verify(monkeypatch, crit, workdir_files, judge_met=True):
    async def _fake_workdir(session_id, limit=40):
        return list(workdir_files)

    async def _fake_exist(session_id, paths):
        # Nothing the criterion names exists on disk (the whole point).
        return {p: False for p in (paths or [])}

    async def _fake_generate(*a, **kw):
        import json
        return json.dumps({"met": bool(judge_met), "reason": "judge: list produced"})

    monkeypatch.setattr(W, "_v5_workdir_files", _fake_workdir)
    monkeypatch.setattr(W, "_v6_check_paths_exist", _fake_exist)
    monkeypatch.setattr(W, "_safe_ollama_generate_dw", _fake_generate)

    step = {"id": 1, "title": "Define app requirements", "goal": "define requirements",
            "success": crit}
    res = {"ok": True, "summary": "wrote the requirements document",
           "outputs": {}, "history": [
               {"tool": "code.author", "ok": True, "preview": "wrote requirements doc"}]}
    return asyncio.run(W._v6_verify_step(
        step, res, session_id="s1", model="m", instance_id="i", prefer_gpu=False))


# The exact incident criterion.
_LIST_CRIT = ("A clear list of required files (index.html, style.css, script.js) "
              "and core logic components is generated.")
# A genuine "produce a file of this type" criterion (NOT a list).
_EXT_CRIT = "A document-shaped file (.md, .txt, .html) exists in the working directory."


def test_list_criterion_naming_extensions_is_not_hard_failed(monkeypatch):
    # Only a .md on disk; criterion names .html/.css/.js as CONTENT of the list.
    v = _verify(monkeypatch, _LIST_CRIT, ["output_features_and_stack.md"], judge_met=True)
    # The extension gate must NOT have fired — its reason mentions "extensions".
    assert "extensions" not in v["reason"], v
    assert v["met"] is True, v


def test_list_criterion_still_reaches_the_llm_judge(monkeypatch):
    # If the judge says the list was NOT actually produced, the step still fails —
    # suppressing the extension gate must not blanket-pass a planning step that
    # did nothing; it just moves the decision to the judge instead of a filesystem
    # extension leak.
    v = _verify(monkeypatch, _LIST_CRIT, ["output_features_and_stack.md"], judge_met=False)
    assert v["met"] is False, v
    assert "extensions" not in v["reason"], v


def test_nonlist_extension_criterion_still_hard_failed_when_absent(monkeypatch):
    # A real "a .md/.txt/.html file must exist" criterion with only a .py present
    # must STILL be hard-failed by the extension gate (regression guard: the fix
    # must not weaken this legitimate case).
    v = _verify(monkeypatch, _EXT_CRIT, ["build_report.py"], judge_met=True)
    assert v["met"] is False, v
    assert "extensions" in v["reason"], v


def test_nonlist_extension_criterion_passes_when_file_present(monkeypatch):
    v = _verify(monkeypatch, _EXT_CRIT, ["report.md"], judge_met=True)
    assert v["met"] is True, v


def test_list_regex_separates_the_two_criteria():
    # Pure unit check on the guard regex itself.
    assert W._V6_FILE_LIST_CRIT_RE.search(_LIST_CRIT)      # "list" → excluded
    assert not W._V6_FILE_LIST_CRIT_RE.search(_EXT_CRIT)   # genuine ext requirement
