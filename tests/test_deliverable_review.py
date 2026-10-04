"""Opt-in deliverable review (user 2026-10-04: "the larger models should look at the output
of the gpu adversarially as its a weaker model"): when the final gate says complete, a large
CPU model reviews the finished deliverable ONCE; real defects become one fix step."""
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


def test_the_reviewer_is_adversarial_and_knows_about_clips():
    s = PS.REVIEW_SYSTEM
    assert "ADVERSARIAL" in s and "smaller, faster model" in s
    assert "MISSING:" in s and "WRONG:" in s and "answer exactly: OK" in s
    assert "SHORTENED by us" in s


def test_review_prompt_shows_files_and_answer_and_marks_cuts():
    p = PS.review_prompt("write a report on X", "report.md exists",
                         [("report.md", "a" * (PS.REVIEW_FILE_CHARS + 50)), ("empty.md", "")],
                         answer="done: wrote report.md")
    assert p.startswith("GOAL: write a report on X\nDONE WHEN: report.md exists")
    assert "FILE report.md (first %d of %d chars)" % (PS.REVIEW_FILE_CHARS, PS.REVIEW_FILE_CHARS + 50) in p
    assert PS.CLIP_MARK in p and "empty.md" not in p
    assert "THE RUN'S ANSWER:\ndone: wrote report.md" in p
    assert "(the run produced no file and no answer)" in PS.review_prompt("g", "", [], "")


def test_review_prompt_stays_bounded():
    files = [("f%d.md" % i, "z" * 5000) for i in range(6)]
    p = PS.review_prompt("g", "", files, "y" * 9000)
    assert len(p) < PS.REVIEW_EVIDENCE_CHARS + 200


def test_the_fix_step_carries_the_review_and_edits_in_place():
    s = PS.review_fix_step("  - MISSING: no sources cited", "report on X")
    assert s["title"] == "Fix what the deliverable review found"
    assert "MISSING: no sources cited" in s["goal"] and "do not start over" in s["goal"]
    assert "GOAL: report on X" in s["goal"] and s["success"]


def test_the_loop_is_wired_and_off_by_default():
    sig = SRC[SRC.index("async def cap_dag_agent_loop_v6("):]
    sig = sig[:sig.index("\n):")]
    assert "deliverable_review: bool = False," in sig
    assert 'v6_deliverable_review = bool(body.get("deliverable_review", False))' in SRC
    assert "deliverable_review=v6_deliverable_review," in SRC
    gate = SRC[SRC.index("    _review_done = False"):]
    gate = gate[:gate.index("# ── Synthesize final")]
    assert "if (not follow_up and deliverable_review and not _review_done" in gate
    assert 'bool(gate.get("complete")) and executed < hard_cap' in gate
    assert "_plan_styles.review_fix_step(" in gate
    assert '"agent_loop_v6.deliverable_review"' in gate


@needs_app
def test_the_review_call_uses_the_cpu_enrich_role_and_flags(monkeypatch):
    seen = {}

    async def fake_gen(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return "MISSING: the report cites no sources\nNEXT: add citations"

    async def fake_load(path, session_id="", repo=""):
        return "# Report\nRedis changed its licence."

    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)
    hist = [{"tool": "prose.author", "ok": True, "args": {"path": "report.md"}}]
    out = asyncio.run(M._v6_deliverable_review("write a cited report", "report.md", hist,
                                               [{"summary": "wrote report.md"}], "s1"))
    assert out["flag"] is True and "cites no sources" in out["note"] and out["files"] == ["report.md"]
    assert seen["prefer_gpu"] is False and seen["role"] == PS.ENRICH_ROLE and seen["think"] is False
    assert seen["system"] == PS.REVIEW_SYSTEM and "FILE report.md" in seen["prompt"]


@needs_app
def test_an_ok_review_is_no_flag_and_a_failed_call_never_raises(monkeypatch):
    async def ok_gen(prompt, **kw):
        return "OK"

    async def boom(prompt, **kw):
        raise RuntimeError("node down")

    async def fake_load(path, session_id="", repo=""):
        return ""

    monkeypatch.setattr(M, "_v5_load_current", fake_load)
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", ok_gen)
    out = asyncio.run(M._v6_deliverable_review("g", "", [], [{"summary": "fine"}], "s"))
    assert out["flag"] is False and out["note"] == ""
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", boom)
    out = asyncio.run(M._v6_deliverable_review("g", "", [], [], "s"))
    assert out["flag"] is False and "node down" in out["error"]
