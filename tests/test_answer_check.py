"""Opt-in answer check (4 Oct 2026, judged probes: the files were right and the FINAL answer
was not - invented dates and licences, "tests passed" with no test run, verifier findings
left out). After the final answer is written, the large CPU model checks it against the
files the run wrote and the findings it left unresolved, and corrects it."""
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.planning import planner_styles as PS  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

try:
    from vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None
needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")

pytestmark = pytest.mark.critical


def test_the_checker_trusts_the_files_and_answers_ok_or_a_corrected_answer():
    s = PS.ANSWER_CHECK_SYSTEM
    assert "smaller, faster model" in s and "reply exactly: OK" in s
    assert "FIXES:" in s and "ANSWER:" in s and "SHORTENED by us" in s


def test_unresolved_findings_are_the_last_unmet_verdicts_and_the_gate_unmet():
    results = [{"id": 1, "title": "write", "met": True, "met_reason": "fine"},
               {"id": 2, "title": "edit timer", "met": False,
                "met_reason": "STALE OLD VALUE: the page still shows 90"},
               {"id": 3, "title": "no verdict", "met": None}]
    f = PS.unresolved_findings(results, ["the tests were run"])
    assert f == ["step 2 (edit timer): STALE OLD VALUE: the page still shows 90",
                 "not met at the end: the tests were run"]
    assert PS.unresolved_findings([], None) == []


def test_prompt_shows_record_then_answer_and_marks_cuts():
    p = PS.answer_check_prompt("report on Valkey", [("report.md", "forked March 2024"),
                                                    ("big.md", "x" * (PS.ANSWER_CHECK_FILE_CHARS + 9))],
                               "Valkey was forked in April.", ["step 2: no citation"])
    assert p.startswith("GOAL: report on Valkey\n\nTHE RUN'S RECORD:\nFILE report.md:\nforked March 2024")
    assert "(first %d of" % PS.ANSWER_CHECK_FILE_CHARS in p and PS.CLIP_MARK in p
    assert "UNRESOLVED VERIFIER FINDINGS:\n- step 2: no citation" in p
    assert p.endswith("THE FINAL ANSWER TO CHECK:\nValkey was forked in April.")
    assert "(the run wrote no file and left no finding)" in PS.answer_check_prompt("g", [], "a")


def test_parse_keeps_the_original_unless_a_whole_corrected_answer_comes_back():
    orig = "Valkey was forked from Redis in April 2024 under a dual licence."
    assert PS.parse_answer_check("OK", orig) == ("", "")
    new, fixes = PS.parse_answer_check(
        "FIXES: 'April' -> March (report.md)\nANSWER:\nValkey was forked from Redis in March "
        "2024 and stays BSD-3-Clause.", orig)
    assert new.startswith("Valkey was forked from Redis in March") and "April" in fixes
    assert PS.parse_answer_check("FIXES: x\nANSWER:\nshort", orig)[0] == ""
    long_orig = "y" * (PS.ANSWER_CHECK_ANSWER_CHARS + 1)
    assert PS.parse_answer_check("ANSWER:\n" + "z" * 5000, long_orig)[0] == ""


def test_the_loop_is_wired_after_the_final_answer_and_off_by_default():
    sig = SRC[SRC.index("async def cap_dag_agent_loop_v6("):]
    sig = sig[:sig.index("\n):")]
    assert "answer_check: bool = False," in sig
    assert 'v6_answer_check = bool(body.get("answer_check", False))' in SRC
    assert "answer_check=v6_answer_check," in SRC
    i_final = SRC.index("    final = await _v5_synthesize_final(")
    i_check = SRC.index("    if answer_check and str(final or \"\").strip():")
    i_deliv = SRC.index("    # ── Delivery stage: dedicated final agent")
    i_done = SRC.index('await emit_event({"type": "agent_loop_v6.done", "stream_id": stream_id, "session_id": sid,')
    assert i_final < i_check < i_deliv < i_done
    block = SRC[i_check:i_deliv]
    assert '"agent_loop_v6.answer_check"' in block and 'final = _ac["answer"]' in block


@needs_app
def test_the_check_uses_the_cpu_enrich_role_and_corrects(monkeypatch):
    seen = {}

    async def fake_gen(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return ("FIXES: 'April' is wrong - report.md says March\nANSWER:\n"
                "Valkey was forked from Redis in March 2024 (report.md).")

    async def fake_load(path, session_id="", repo=""):
        return "# Report\nValkey was forked in March 2024."

    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)
    monkeypatch.setattr(M, "_plan_styles", PS)
    hist = [{"tool": "prose.author", "ok": True, "args": {"path": "report.md"}}]
    out = asyncio.run(M._v6_answer_check("report on Valkey", hist,
                                         [{"id": 1, "met": False, "met_reason": "no source"}],
                                         "Valkey was forked in April 2024.", [], "s1"))
    assert out["answer"].startswith("Valkey was forked from Redis in March") and "April" in out["fixes"]
    assert out["files"] == ["report.md"] and out["findings"] == ["step 1 (): no source"]
    assert seen["prefer_gpu"] is False and seen["role"] == PS.ENRICH_ROLE and seen["think"] is False
    assert seen["system"] == PS.ANSWER_CHECK_SYSTEM and "FILE report.md" in seen["prompt"]


@needs_app
def test_ok_keeps_the_answer_and_a_failed_call_never_raises(monkeypatch):
    async def ok_gen(prompt, **kw):
        return "OK"

    async def boom(prompt, **kw):
        raise RuntimeError("node down")

    async def fake_load(path, session_id="", repo=""):
        return ""

    monkeypatch.setattr(M, "_v5_load_current", fake_load)
    monkeypatch.setattr(M, "_plan_styles", PS)
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", ok_gen)
    out = asyncio.run(M._v6_answer_check("g", [], [], "fine answer", [], "s"))
    assert out["answer"] == "" and out["error"] == ""
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", boom)
    out = asyncio.run(M._v6_answer_check("g", [], [], "fine answer", [], "s"))
    assert out["answer"] == "" and "node down" in out["error"]
