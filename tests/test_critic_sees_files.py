"""The stepwise-reviewed critic sees what the step wrote (user, 2026-10-03: the reviewer
had no file access - 6 of 70 critiques said the content was "not provided")."""

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

HIST = [
    {"tool": "web.fetch", "args": {"url": "https://x"}, "ok": True, "preview": "Unit tests catch regressions early"},
    {"tool": "prose.author", "args": {"path": "unit_tests.md"}, "ok": True, "preview": "wrote unit_tests.md"},
    {"tool": "code.edit", "args": {"path": "broken.py"}, "ok": False, "error": "find text not present"},
    {"tool": "code.author", "args": {"path": "timer.html"}, "ok": True, "preview": "wrote timer.html"},
    {"tool": "prose.author", "args": {"path": "unit_tests.md"}, "ok": True, "preview": "rewrote"},
]


def test_written_paths_are_successful_writes_latest_last_deduplicated():
    assert PS.critic_written_paths(HIST) == ["timer.html", "unit_tests.md"]
    assert PS.critic_written_paths(HIST, limit=1) == ["unit_tests.md"]
    assert PS.critic_written_paths([{"tool": "exec.bash.run", "args": {"path": "x"}}]) == []


def test_evidence_shows_files_and_tool_results_bounded():
    big = "x" * 5000
    ev = PS.critic_evidence([("timer.html", "<h1>00:30</h1>"), ("big.txt", big), ("gone.md", "")],
                            PS.critic_tool_lines(HIST))
    assert "FILE timer.html (current content):\n<h1>00:30</h1>" in ev
    assert "first %d of 5000 chars" % PS.CRITIC_FILE_CHARS in ev
    assert "gone.md" not in ev                          # unread is not shown as missing
    assert "code.edit -> FAILED: find text not present" in ev
    assert "web.fetch -> ok: Unit tests catch regressions early" in ev
    assert len(ev) <= PS.CRITIC_EVIDENCE_CHARS
    assert PS.critic_evidence([], []) == ""


def test_the_prompt_carries_the_evidence_and_the_system_trusts_it():
    p = PS.critic_prompt("g", "", {"id": 1}, {"summary": "s", "ok": True}, [], evidence="FILE a.md (current content):\nhi")
    assert p.endswith("FILE a.md (current content):\nhi")
    assert PS.critic_prompt("g", "", {"id": 1}, {"summary": "s"}, []).endswith("s")
    assert "Judge only from the result shown" not in PS.CRITIC_SYSTEM
    assert "never call it missing" in PS.CRITIC_SYSTEM


@needs_app
def test_the_running_critic_reads_the_files_the_step_wrote(monkeypatch):
    seen = {"prompts": [], "reads": []}

    async def fake_gen(prompt, **kw):
        seen["prompts"].append(prompt)
        return "OK"

    async def fake_emit(ev):
        pass

    async def fake_load(path, session_id="", repo=""):
        seen["reads"].append((path, session_id))
        return "<h1 id='t'>00:30</h1>" if path == "timer.html" else "# Why unit tests\n1. a\n2. b\n3. c"

    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "emit_event", fake_emit)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)

    async def go():
        c = M._V6StepCritic("write why unit tests matter", sid="sess-1", stream_id="")
        c.submit({"id": 1, "title": "write"}, {"id": 1, "summary": "wrote files", "ok": True, "history": HIST},
                 [], "")
        await asyncio.sleep(0.05)
        c.cancel()

    asyncio.run(go())
    assert seen["reads"] == [("timer.html", "sess-1"), ("unit_tests.md", "sess-1")]
    (p,) = seen["prompts"]
    assert "FILE unit_tests.md (current content):\n# Why unit tests" in p
    assert "<h1 id='t'>00:30</h1>" in p
