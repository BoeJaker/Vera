"""Phase-boundary history/journal carry-forward (the 2026-08-17 fix).

A phased step (explore/think/act/verify) runs each phase as its OWN
`_v5_run_step_inner` sub-agent call. Cycle numbers (`cycle_offset`/`gc`)
already thread continuously across those phases so the UI shows one
uninterrupted cycle count for the whole step -- but `history` used to start
fresh and empty for every phase, so a later phase's FIRST cycle built its
per-cycle prompt's "Your results so far:" slot (obs, from `history[-4:]`) as
empty even though the previous phase, one cycle earlier in the SAME step,
had just done real work. The model had no memory of its own recent action
and could reissue the exact same tool call verbatim -- observed live as
byte-identical intra-step cycles.

These tests pin `_v5_run_phased_step`'s threading behaviour by faking
`_v5_run_step` (never touching Ollama/Redis/emit_event for real):
  1. each phase after the first receives the running history as
     `prior_history`;
  2. the aggregated `history` on return has no duplicate entries (a naive
     `history.extend(r["history"])` would double-count every earlier phase's
     entries once per later phase, since each phase's OWN returned history
     already includes everything it was seeded with);
  3. `journal` -- previously dropped entirely for phased steps -- is passed
     through to every phase unchanged.

Imports the monolith module, so it runs in-container (see test_loop_cancel.py).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def _run_phased(monkeypatch, phases=("explore", "act"), journal=None):
    calls = []

    async def _fake_run_step(step, **kw):
        calls.append({"phase": kw.get("phase"),
                       "prior_history": kw.get("prior_history"),
                       "journal": kw.get("journal")})
        # Each phase contributes exactly ONE new entry on top of whatever
        # it was seeded with -- exactly what a real _v5_run_step_inner call
        # does now that `history` is seeded from `prior_history`.
        seeded = list(kw.get("prior_history") or [])
        new_entry = {"tool": f"tool_{kw.get('phase')}", "ok": True,
                     "preview": f"result from {kw.get('phase')}"}
        return {"id": step["id"], "title": step["title"], "ok": True,
                "summary": f"{kw.get('phase')} done",
                "outputs": {}, "cycle_end": kw.get("cycle_offset", 0) + 1,
                "history": seeded + [new_entry], "user_clarifications": []}

    async def _noop_event(*a, **kw):
        return None

    monkeypatch.setattr(W, "_v5_run_step", _fake_run_step)
    monkeypatch.setattr(W, "emit_event", _noop_event)

    step = {"id": 1, "title": "Test step", "goal": "do the thing", "caps": [], "skills": []}
    result = asyncio.run(W._v5_run_phased_step(
        step, list(phases), goal="goal", blackboard={}, artifacts={}, journal=journal,
        model="m", instance_id="i", prefer_gpu=False, session_id="s", stream_id="",
        trace_id=None, cycle_budget=3, cycle_offset=0, artifact_dir_path="",
        call_tool=None, build_ctx=None))
    return result, calls


def test_second_phase_receives_first_phases_history_as_prior_history(monkeypatch):
    _, calls = _run_phased(monkeypatch)
    assert not calls[0]["prior_history"]     # first phase: nothing yet to seed with
    assert calls[1]["prior_history"] == [
        {"tool": "tool_explore", "ok": True, "preview": "result from explore"}]


def test_final_history_has_no_duplicate_entries_across_phases(monkeypatch):
    result, _ = _run_phased(monkeypatch)
    tools = [h["tool"] for h in result["history"]]
    # A naive `history.extend(r["history"])` would yield
    # ["tool_explore", "tool_explore", "tool_act"] here (phase 2's returned
    # history already contains phase 1's seeded entry).
    assert tools == ["tool_explore", "tool_act"]


def test_three_phases_carry_forward_without_duplication(monkeypatch):
    result, calls = _run_phased(monkeypatch, phases=("explore", "think", "act"))
    assert [c["prior_history"] and len(c["prior_history"]) or 0 for c in calls] == [0, 1, 2]
    assert [h["tool"] for h in result["history"]] == ["tool_explore", "tool_think", "tool_act"]


def test_journal_is_threaded_into_every_phase_not_dropped(monkeypatch):
    j = [{"note": "shared run journal"}]
    _, calls = _run_phased(monkeypatch, journal=j)
    assert all(c["journal"] is j for c in calls)


def test_no_phases_returns_empty_history_without_error(monkeypatch):
    result, calls = _run_phased(monkeypatch, phases=())
    assert calls == []
    assert result["history"] == []
