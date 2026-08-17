"""A LISTING-criterion step must not be failed by the verifier for absent files
(2026-08-17): step 1 'generate a list of required files (index.html, style.css)'
failed on 'no files', was re-attempted, and duplicated its output. The verifier now
neither hard-gates nor feeds the misleading file-existence facts to the judge for a
listing criterion. Imports the monolith, runs in-container.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def test_listing_step_not_hardfailed_when_files_absent():
    step = {"success": "A clear list of required files (index.html, style.css, script.js) "
                       "is generated.",
            "goal": "define app requirements and tech stack"}
    res = {"ok": True,
           "summary": "Required files: index.html, style.css, script.js. Core logic: a timer.",
           "outputs": {},
           "history": [{"tool": "llm.generate", "ok": True, "preview": "listed the files"}]}
    # session_id="" avoids workdir I/O; dummy model makes the LLM judge fall back to res.ok
    v = asyncio.run(W._v6_verify_step(step, res, session_id="", model="__dummy__",
                                      instance_id="", prefer_gpu=False))
    # the ONLY thing we assert: it must NOT be the file-existence hard-fail
    assert not (v.get("met") is False and "do NOT exist" in (v.get("reason") or "")), \
        f"listing step wrongly hard-failed on file existence: {v}"


def test_real_file_step_can_still_hardfail_when_absent():
    # a genuine 'the file is created' criterion naming an absent file MUST still hard-fail
    step = {"success": "index.html is created with the pomodoro UI", "goal": "write the html"}
    res = {"ok": True, "summary": "wrote index.html", "outputs": {},
           "history": [{"tool": "llm.generate", "ok": True, "preview": "done"}]}
    # a fake session where the named file is reported absent
    orig = W._v6_check_paths_exist

    async def fake_exist(sid, paths):
        return {p: False for p in paths}
    try:
        W._v6_check_paths_exist = fake_exist
        v = asyncio.run(W._v6_verify_step(step, res, session_id="s1", model="__dummy__",
                                          instance_id="", prefer_gpu=False))
        assert v.get("met") is False and "do NOT exist" in (v.get("reason") or "")
    finally:
        W._v6_check_paths_exist = orig
