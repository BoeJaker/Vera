"""The step critic's false flags (4 Oct 2026: 32 of 44 notes were flags on runs that mostly
scored 1.0). A NEXT-only note is advice, not a flag, and never reaches the completion gate;
and every cut in what the critic is shown is marked, so it stops calling the step's output
"truncated" at our own clip."""

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.planning import planner_styles as PS  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


# ── what counts as a flag ─────────────────────────────────────────────────────

def test_next_only_notes_are_not_flags():
    # real notes from 4 Oct, after critic_note's formatting
    for raw in ("NEXT: Report that the shell is running as root.",
                "NEXT: Report the final answer: Python 3.14.8",
                "- NEXT: confirm goal is met\n- NEXT: terminate the loop",
                "**NEXT:** state the converted value"):
        note = PS.critic_note(raw)
        assert note, raw
        assert PS.critic_is_flag(note) is False, raw


def test_missing_or_wrong_lines_are_flags():
    for raw in ("MISSING: Chroma documentation file not written\nNEXT: fetch it",
                "WRONG: claimed met despite web.fetch returning Page Not Found",
                "NEXT: re-check\nWRONG: the value was never applied",
                "**MISSING:** the count only covered depth 1"):
        assert PS.critic_is_flag(PS.critic_note(raw)) is True, raw


def test_an_unlabelled_complaint_is_a_flag_and_nothing_is_not():
    assert PS.critic_is_flag(PS.critic_note("The step never wrote report.md")) is True
    assert PS.critic_is_flag("") is False
    assert PS.critic_is_flag(PS.critic_note("OK")) is False
    assert PS.critic_is_flag(PS.critic_note("MISSING: None\nWRONG: None\nNEXT: OK")) is False


# ── every cut is marked ───────────────────────────────────────────────────────

def test_a_long_result_is_marked_clipped_and_a_short_one_is_not():
    long_out = "The TestDevLab O" * 400
    p = PS.critic_prompt("g", "", {"id": 1}, {"summary": long_out, "ok": True}, [])
    assert PS.CLIP_MARK in p
    assert p.index(PS.CLIP_MARK) > p.index("RESULT:")
    short = PS.critic_prompt("g", "", {"id": 1}, {"summary": "done", "ok": True}, [])
    assert PS.CLIP_MARK not in short and short.endswith("done")


def test_a_clipped_file_is_marked_and_a_whole_file_is_not():
    ev = PS.critic_evidence([("big.md", "y" * 5000), ("small.md", "hello")], [])
    assert "first %d of 5000 chars" % PS.CRITIC_FILE_CHARS in ev
    big = ev[ev.index("FILE big.md"):ev.index("FILE small.md")]
    assert PS.CLIP_MARK in big
    assert ev.endswith("FILE small.md (current content):\nhello")


def test_the_evidence_cap_is_marked_and_respected():
    files = [("f%d.md" % i, "z" * 3900) for i in range(4)]
    ev = PS.critic_evidence(files, [])
    assert len(ev) <= PS.CRITIC_EVIDENCE_CHARS
    assert ev.endswith(PS.CLIP_MARK)


def test_tool_lines_are_called_previews_and_the_system_knows_about_clips():
    ev = PS.critic_evidence([], ["  web.fetch -> ok: Python 3.14.8 Possible cause"])
    assert "PREVIEW" in ev
    s = PS.CRITIC_SYSTEM
    assert "SHORTENED" in s and "never call the step's output truncated" in s
    # the earlier contract still holds
    assert "THIS step's own job" in s and "never call it missing" in s


# ── the running critic ───────────────────────────────────────────────────────

def _run_critic(reply: str, n_steps: int = 1):
    events = []

    async def fake_gen(prompt, **kw):
        return reply

    async def fake_emit(ev):
        events.append(ev)

    async def fake_load(path, session_id="", repo=""):
        return ""

    return fake_gen, fake_emit, fake_load, events


@needs_app
def test_running_critic_next_only_is_ok_and_kept_from_the_gate(monkeypatch):
    fake_gen, fake_emit, fake_load, events = _run_critic("NEXT: Report the final answer: 37.78")
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "emit_event", fake_emit)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)

    async def go():
        c = M._V6StepCritic("convert 100F to C", sid="s1", stream_id="")
        c.submit({"id": 1, "title": "convert"}, {"id": 1, "summary": "37.78", "ok": True, "history": []}, [], "")
        await asyncio.sleep(0.05)
        out = (c.recent_block(), c.new_block())
        c.cancel()
        return out

    gate, controller = asyncio.run(go())
    (ev,) = [e for e in events if e.get("type") == "agent_loop_v6.step_critique"]
    assert ev["ok"] is True and ev["flag"] is False and "37.78" in ev["note"]
    assert gate == ""                                  # the gate is not held open by advice
    assert "37.78" in controller                       # the controller still gets the advice


@needs_app
def test_running_critic_missing_is_a_flag_and_reaches_the_gate(monkeypatch):
    fake_gen, fake_emit, fake_load, events = _run_critic("MISSING: report.md was not written\nNEXT: write it")
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "emit_event", fake_emit)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)

    async def go():
        c = M._V6StepCritic("write a report", sid="s2", stream_id="")
        c.submit({"id": 1, "title": "write"}, {"id": 1, "summary": "wrote", "ok": True, "history": []}, [], "")
        await asyncio.sleep(0.05)
        out = c.recent_block()
        c.cancel()
        return out

    gate = asyncio.run(go())
    (ev,) = [e for e in events if e.get("type") == "agent_loop_v6.step_critique"]
    assert ev["ok"] is False and ev["flag"] is True
    assert "report.md was not written" in gate
