"""Stage-context audit records (vera/dag/loop_stage_audit.py) — Phase 0.

The guarantees worth pinning are the ones that make the record trustworthy as a
diagnostic AND safe to emit on every cycle: it must never carry prompt bodies,
it must distinguish two prompts of the same stage (the planner's full vs
minimal-schema retry — the drift that held a rule on some runs and not others),
and an unchanged input must be visibly unchanged so "same input, different
decision" is detectable.

Pure (no I/O, no app import), so it runs in the critical gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import loop_stage_audit as A  # noqa: E402
import json as _json  # noqa: E402


# ── the record never carries bodies ──────────────────────────────────────────
def test_record_carries_shas_not_prompt_text():
    secret = "SYSTEM PROMPT WITH SENSITIVE CONTENT " * 20
    r = A.stage_record("planner", system=secret, prompt="the goal text")
    blob = repr(r)
    assert "SENSITIVE" not in blob
    assert "the goal text" not in blob
    assert r["system_chars"] == len(secret)
    assert len(r["system_sha"]) == 12


def test_runtime_allowlist_drops_body_shaped_extras():
    r = A.stage_record("gate", runtime={
        "goal_chars": 120, "caps": ["code.author"],
        "ledger": "a whole ledger of prose that must not be copied",
        "file_bodies": {"index.html": "<html>...</html>"},
    })
    assert r["runtime"] == {"goal_chars": 120, "caps": ["code.author"]}


def test_runtime_lists_are_bounded():
    r = A.stage_record("planner", runtime={"caps": [f"cap.{i}" for i in range(200)]})
    assert len(r["runtime"]["caps"]) == 40


# ── same stage, two prompts — the planner drift case ─────────────────────────
def test_variant_separates_two_prompts_of_one_stage():
    full = A.stage_record("planner", variant="full", system="A" * 500)
    mini = A.stage_record("planner", variant="minimal", system="B" * 200)
    assert full["variant"] != mini["variant"]
    assert full["system_sha"] != mini["system_sha"]
    s = A.summarise([full, mini])
    assert {e["stage"] for e in s["stages"]} == {"planner:full", "planner:minimal"}


# ── identity / change detection ──────────────────────────────────────────────
def test_identical_input_is_reported_identical():
    a = A.stage_record("controller", system="S", prompt="P", cycle=3,
                       runtime={"ledger_steps": 2})
    b = A.stage_record("controller", system="S", prompt="P", cycle=4,
                       runtime={"ledger_steps": 2})
    d = A.diff_records(a, b)
    assert d["identical_input"] is True
    assert d["changed"] == [] and d["runtime_changed"] == {}
    assert d["from_cycle"] == 3 and d["to_cycle"] == 4


def test_changed_prompt_is_flagged_with_size_delta():
    a = A.stage_record("controller", system="S", prompt="P")
    b = A.stage_record("controller", system="S", prompt="P and more")
    d = A.diff_records(a, b)
    assert "prompt_sha" in d["changed"]
    assert d["identical_input"] is False
    assert d["prompt_chars_delta"] == len("P and more") - len("P")


def test_runtime_change_is_reported_field_by_field():
    a = A.stage_record("controller", runtime={"pending_steps": 3})
    b = A.stage_record("controller", runtime={"pending_steps": 1})
    d = A.diff_records(a, b)
    assert d["runtime_changed"]["pending_steps"] == {"before": 3, "after": 1}


def test_rule_id_movement_is_reported():
    """Phase 1 populates rule_ids; the diff must already surface them."""
    a = A.stage_record("planner", rule_ids=["criteria_settleable"])
    b = A.stage_record("planner", rule_ids=["criteria_settleable", "authored_verified"])
    d = A.diff_records(a, b)
    assert d["rules_added"] == ["authored_verified"] and d["rules_removed"] == []


# ── roll-up ──────────────────────────────────────────────────────────────────
def test_summarise_counts_calls_and_repeat_identical_inputs():
    recs = [A.stage_record("controller", system="same", cycle=c) for c in (1, 2, 3)]
    recs.append(A.stage_record("planner", system="p"))
    s = A.summarise(recs)
    ctrl = [e for e in s["stages"] if e["stage"] == "controller"][0]
    assert ctrl["calls"] == 3
    assert ctrl["repeat_identical_system"] == 2      # 3 calls, 1 distinct prompt
    assert s["total_calls"] == 4


def test_summarise_orders_by_run_sequence():
    s = A.summarise([A.stage_record("gate"), A.stage_record("tier"),
                     A.stage_record("planner")])
    assert [e["stage"] for e in s["stages"]] == ["tier", "planner", "gate"]


# ── boundaries ───────────────────────────────────────────────────────────────
def test_optional_fields_are_omitted_when_absent():
    r = A.stage_record("tier")
    for k in ("session_id", "stream_id", "cycle", "step_id"):
        assert k not in r


def test_cycle_zero_is_kept_not_dropped():
    r = A.stage_record("planner", cycle=0, step_id=0)
    assert r["cycle"] == 0 and r["step_id"] == 0


def test_handles_empty_and_none_safely():
    r = A.stage_record("", system=None, prompt=None, runtime=None)
    assert r["system_chars"] == 0 and r["runtime"] == {}
    assert A.diff_records({}, {})["identical_input"] is True
    assert A.summarise([])["total_calls"] == 0
    assert A.summarise([None, "junk"])["total_calls"] == 0


# â”€â”€ the roll-up must distinguish the two halves of a stage's context â”€â”€â”€â”€â”€â”€â”€â”€
def test_summary_splits_system_from_the_user_turn():
    """Totals alone hid WHICH half was growing.

    The executor's user turn carries pending_note (4KB) and _msg (2KB) on top of
    its system prompt, so a stage reported as "20,370 chars" could be a bigger
    system prompt or a bigger user turn - and nothing said which. Both are
    recorded per call; only the roll-up was collapsing them.
    """
    recs = [A.stage_record("executor", system="S" * 100, prompt="U" * 50),
            A.stage_record("executor", system="S" * 100, prompt="U" * 900)]
    st = A.summarise(recs)["stages"][0]
    assert st["max_system_chars"] == 100, "the system prompt did not grow"
    assert st["max_prompt_chars"] == 900, "the USER TURN is what grew"
    assert st["max_chars"] == 1000, "the total is still reported"


def test_a_repeated_user_turn_is_counted_separately():
    """A repeated SYSTEM prompt is normal; a repeated USER turn is the signal.

    It means the stage was asked the same question twice - which is what a
    dedupe or a stuck loop looks like from the outside.
    """
    same = [A.stage_record("controller", system="S" * 10, prompt="identical"),
            A.stage_record("controller", system="S" * 10, prompt="identical")]
    st = A.summarise(same)["stages"][0]
    assert st["repeat_identical_prompt"] == 1
    assert st["repeat_identical_system"] == 1

    differing = [A.stage_record("controller", system="S" * 10, prompt="first"),
                 A.stage_record("controller", system="S" * 10, prompt="second")]
    st2 = A.summarise(differing)["stages"][0]
    assert st2["repeat_identical_prompt"] == 0, "different questions are not repeats"
    assert st2["repeat_identical_system"] == 1, "the system prompt WAS identical"


def test_the_split_still_carries_no_prompt_bodies():
    """The standing rule: a record may carry sizes and hashes, never the text."""
    recs = [A.stage_record("executor", system="SECRETSYS", prompt="SECRETUSER")]
    blob = _json.dumps(A.summarise(recs))
    assert "SECRETSYS" not in blob and "SECRETUSER" not in blob
