"""Every executed step must be attributable to something that admits adding it.

Run 10 of the 12-goal census produced two runs whose counters could not both be
right — `executed_steps` above `planned_steps` with `inserted_steps` at 0:

    operate-exec    planned=2  executed=3  inserted=0
    trivial-chat    planned=0  executed=1

Neither was a miscount of executions. Both were blind spots in the digest, and
these tests pin the two real event shapes taken from those runs (session
4ea9e6af… and 9a94bb7d…) so the blind spots cannot come back:

  * the **completion gate** appends `follow_up` steps that really execute, and
    the digest only ever harvested insertions from `.assess`;
  * the **fast path** runs a synthetic step and never emits a plan at all.

The last test is the one that matters most: a step produced by something NOBODY
declares must be counted and named, so the next producer that learns to add a
step announces itself instead of quietly skewing a year of census numbers.

Pure: no Redis, no app import - so it runs anywhere, including the host venv.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag.loop_trace_core import (          # noqa: E402
    accounting_is_consistent, digest_events, norm_id,
)


# ── fixtures: the real event shapes, trimmed to what the digest reads ────────
def _plan(*step_ids):
    return {"type": "agent_loop_v6.plan", "done_when": "both commands ran",
            "steps": [{"id": i, "title": f"planned step {i}", "caps": ["exec.bash.run"],
                       "success": "ok"} for i in step_ids]}


def _ran(step_id, *, ok=True, title=""):
    """The three events a step really emits when it executes."""
    return [
        {"type": "agent_loop_v5.step_start", "step_id": step_id,
         "title": title or f"step {step_id}"},
        {"type": "agent_loop_v5.tool_call", "step_id": step_id, "cycle": 1,
         "tool": "exec.bash.run"},
        {"type": "agent_loop_v5.step_done", "step_id": step_id, "ok": ok},
    ]


def _assess(after_step, *, action="continue", inserted=()):
    return {"type": "agent_loop_v6.assess", "after_step": after_step,
            "action": action, "goal_met": False, "assessment": "…",
            "direction": "…",
            "steps": [{"id": i, "title": f"inserted step {i}",
                       "caps": ["code.author"]} for i in inserted]}


def _gate(round_, *, complete, missing=(), follow_up=()):
    return {"type": "agent_loop_v6.gate", "round": round_, "complete": complete,
            "missing": list(missing),
            "follow_up": [{"id": i, "title": f"follow-up step {i}"} for i in follow_up]}


# ── O1 sighting 1: operate-exec — the gate is a second insertion channel ─────
OPERATE_EXEC = (
    [{"type": "agent_loop_v6.tier", "tier": "simple"}, _plan(1, 2)]
    + _ran(1) + [_assess(1)]
    + _ran(2) + [_assess(2, action="stop")]
    + [_gate(1, complete=False,
             missing=["the goal implies a written report, but no document exists"],
             follow_up=[3])]
    + _ran(3, title="Write and save the report")
    + [_gate(2, complete=True)]
)


def test_a_gate_follow_up_is_an_insertion():
    """Step 3 was appended by the completion gate and really ran."""
    c = digest_events(OPERATE_EXEC)["counters"]
    assert c["planned_steps"] == 2
    assert c["executed_steps"] == 3
    # Before this fix the digest read insertions from `.assess` only, so a
    # gate-extended run reported inserted_steps=0 and the arithmetic broke.
    assert c["inserted_steps"] == 1
    assert c["gate_inserted_steps"] == 1
    assert c["controller_inserted_steps"] == 0


def test_the_gate_extended_run_accounts_for_every_step():
    d = digest_events(OPERATE_EXEC)
    assert d["counters"]["unaccounted_steps"] == 0
    assert accounting_is_consistent(d["counters"])
    c = d["counters"]
    assert c["planned_steps"] + c["inserted_steps"] == c["executed_steps"]


def test_the_gate_says_why_it_extended_the_run():
    """The verdict was previously dropped: the digest read `verdict`/`reason`/
    `met`, keys the emitter has never written."""
    d = digest_events(OPERATE_EXEC)
    assert [g["round"] for g in d["gates"]] == [1, 2]
    assert d["gates"][0]["complete"] is False
    assert "no document exists" in d["gates"][0]["missing"][0]
    assert d["gates"][0]["follow_up"] == [{"id": 3, "title": "follow-up step 3"}]
    assert d["plan"]["gate"]["complete"] is True          # last gate wins
    assert any("completion gate" in w and "APPENDED step 3" in w
               for w in d["warnings"])


# ── O1 sighting 2: trivial-chat — the fast path never plans ─────────────────
TRIVIAL_CHAT = (
    [{"type": "agent_loop_v6.tier", "tier": "single"},
     {"type": "agent_loop_v6.fast_path", "cap": "llm.generate"}]
    + _ran(1, title="Ran llm.generate")
)


def test_the_fast_path_explains_its_own_zero_plan():
    d = digest_events(TRIVIAL_CHAT)
    c = d["counters"]
    assert c["planned_steps"] == 0 and c["executed_steps"] == 1
    assert c["fast_path_steps"] == 1
    # planned=0/executed=1 is correct here, so it must NOT be flagged as a step
    # nothing accounts for - but it must be explained.
    assert c["unaccounted_steps"] == 0
    assert d["plan"]["fast_path"] is True
    assert d["plan"]["fast_path_cap"] == "llm.generate"
    assert any("FAST PATH" in w for w in d["warnings"])


def test_the_fast_path_flag_cannot_excuse_a_planned_run():
    """A run that DID plan must not have unaccounted steps waved through just
    because a fast_path event is somehow present."""
    events = TRIVIAL_CHAT + [_plan(1)] + _ran(7)
    c = digest_events(events)["counters"]
    assert c["fast_path_steps"] == 0
    assert c["unaccounted_steps"] == 1


# ── the third producer, finally named (run 15, research-web) ───────────────
def _recovery(from_step, new_id, title, reason="web.research returned count: 0"):
    return {"type": "agent_loop_v6.recovery_step", "from_step": from_step,
            "step": {"id": new_id, "title": title}, "adjusted": True,
            "caps": ["web.research", "web.search"], "reason": reason}


# The real shape from session 9d1236a3: planned [1,2,3]; step 1 failed and
# recovery minted 4; assess inserted 5-8; step 6 failed and recovery minted 9;
# assess inserted 10. Executed 1, 5, 6, 10, 9 — and 9 was claimed by nothing.
RESEARCH_WEB = (
    [_plan(1, 2, 3)]
    + _ran(1)
    + [_recovery(1, 4, "Fetch WebGPU stats directly via HTTP GET"),
       _assess(1, action="insert", inserted=[5, 6, 7, 8])]
    + _ran(5) + _ran(6)
    + [_recovery(6, 9, "Targeted vendor site search via web.search"),
       _assess(6, action="insert", inserted=[10])]
    + _ran(10) + _ran(9)
)


def test_a_failure_recovery_step_is_an_insertion():
    """The third channel. When a step fails, the extra_step strategy mints a
    replacement through _v6_adjust_step and RUNS it — emitting
    agent_loop_v6.recovery_step and nothing the accounting read."""
    c = digest_events(RESEARCH_WEB)["counters"]
    assert c["recovery_inserted_steps"] == 2          # steps 4 and 9
    assert c["controller_inserted_steps"] == 5        # 5,6,7,8,10


def test_the_run_15_unaccounted_step_is_now_accounted_for():
    """Step 9 executed and was claimed by nothing. That single unaccounted step
    is what the plan called 'the third producer' for two passes."""
    d = digest_events(RESEARCH_WEB)
    assert d["counters"]["unaccounted_steps"] == 0
    assert accounting_is_consistent(d["counters"])
    assert 9 in [r["id"] for r in d["recoveries"]]


def test_a_recovery_says_which_step_it_replaced_and_why():
    """A replacement whose cause is not recorded just moves the mystery."""
    d = digest_events(RESEARCH_WEB)
    r = [x for x in d["recoveries"] if x["id"] == 9][0]
    assert r["from_step"] == 6
    assert "count: 0" in r["reason"]
    assert any("REPLACED step 6 with step 9" in w for w in d["warnings"])


def test_a_recovery_step_that_never_ran_is_not_double_counted():
    """Step 4 was minted but superseded before running. It must count as an
    insertion channel without inventing an executed step."""
    d = digest_events(RESEARCH_WEB)
    assert 4 not in [s["step_id"] for s in d["steps"]]
    assert d["counters"]["executed_steps"] == 5


# ── the durable guard: a third producer must announce itself ────────────────
def test_a_step_nothing_declares_is_counted_and_named():
    """The whole point. Step 9 executed; the plan does not contain it, no
    controller inserted it, no gate appended it."""
    events = [_plan(1)] + _ran(1) + _ran(9, title="where did this come from")
    d = digest_events(events)
    assert d["counters"]["unaccounted_steps"] == 1
    assert not accounting_is_consistent(d["counters"])
    assert any("accounted for by NOTHING" in w and "9" in w for w in d["warnings"])


# ── regression cover for the channel that already worked ───────────────────
def test_a_controller_insertion_is_still_counted():
    """`underspecified` was self-consistent (5 -> 6, inserted=1); keep it so."""
    events = [_plan(1)] + _ran(1) + [_assess(1, action="insert", inserted=[2])] + _ran(2)
    c = digest_events(events)["counters"]
    assert c["controller_inserted_steps"] == 1
    assert c["gate_inserted_steps"] == 0
    assert c["inserted_steps"] == 1
    assert c["unaccounted_steps"] == 0


def test_step_ids_are_compared_across_json_types():
    """Ids cross a JSON boundary and are re-parsed by several producers. If an
    int plan id stopped matching a str event id, EVERY step would look
    unaccounted - the loudest possible false alarm."""
    assert norm_id(3) == norm_id("3") == "3"
    assert norm_id(None) is None and norm_id("  ") is None
    events = [_plan(1, 2)] + _ran("1") + _ran("2")
    assert digest_events(events)["counters"]["unaccounted_steps"] == 0


def test_a_call_with_no_step_id_is_not_a_step():
    """It is still real work (it counts in tool_calls), but it is not a step and
    must never inflate executed_steps."""
    events = [_plan(1)] + _ran(1) + [
        {"type": "agent_loop_v5.tool_call", "step_id": None, "cycle": 1,
         "tool": "llm.generate"}]
    d = digest_events(events)
    assert d["counters"]["executed_steps"] == 1
    assert d["counters"]["tool_calls"] == 2
    assert d["counters"]["unattributed_calls"] == 1
    assert any("no step_id" in w for w in d["warnings"])


def test_a_step_that_only_reported_done_is_not_dropped():
    """It entered by_step but never `order`, so it vanished from the step list."""
    events = [_plan(1, 2)] + _ran(1) + [
        {"type": "agent_loop_v5.step_done", "step_id": 2, "ok": True}]
    d = digest_events(events)
    assert d["counters"]["executed_steps"] == 2
    assert [s["step_id"] for s in d["steps"]] == [1, 2]


# ── the parts that must not have changed ────────────────────────────────────
def test_the_existing_digest_shape_is_preserved():
    d = digest_events(OPERATE_EXEC)
    for k in ("plan", "steps", "control", "counters", "warnings"):
        assert k in d
    for k in ("events", "planned_steps", "executed_steps", "inserted_steps",
              "tool_calls", "think_deltas", "think_ratio", "cycles_per_step",
              "code_version", "stage_calls"):
        assert k in d["counters"], k
    # `control` still means "the controller's assess rounds", unchanged - the
    # gate rounds live in their own list rather than being mixed in.
    assert [c["after_step"] for c in d["control"]] == [1, 2]
    assert d["counters"]["cycles_per_step"] == {"1": 1, "2": 1, "3": 1}


def test_include_text_controls_clipping():
    long = "x" * 400
    events = [_gate(1, complete=False, missing=[long])]
    assert digest_events(events)["gates"][0]["missing"][0].endswith("…")
    assert digest_events(events, include_text=True)["gates"][0]["missing"][0] == long


def test_an_empty_run_does_not_explode():
    d = digest_events([])
    assert d["counters"]["executed_steps"] == 0
    assert d["counters"]["unaccounted_steps"] == 0
    assert accounting_is_consistent(d["counters"])
