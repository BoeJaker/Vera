"""Who decides whether the second pass is needed - both options, kept to compare.

  tier1  the first pass self-reports by emitting a marker. Cheap when the answer
         was complete, but it is judging whether material it CANNOT SEE would
         have changed its answer.
  tier2  the second pass decides with the context in front of it, and says so
         when the context adds nothing. Always costs the second generation.

They fail in opposite directions, which is why both exist rather than one being
picked on argument.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.agents import two_tier as tt              # noqa: E402
from vera.agents import two_tier_stream as tts      # noqa: E402

pytestmark = pytest.mark.critical


# ── choosing a decider ──────────────────────────────────────────────────────

def test_both_deciders_exist():
    assert set(tt.DECIDERS) == {"tier1", "tier2"}


def test_an_unknown_decider_falls_back_to_the_default():
    for bad in ("", None, "tier3", "whoever"):
        assert tt.normalise_decider(bad) == tt.DEFAULT_DECIDER


def test_tier2_runs_the_second_pass_unconditionally():
    """It has to run in order to decide."""
    assert tt.plan("fetched", "P", [], "tier2")["always_run_tier2"] is True


def test_tier1_runs_the_second_pass_only_when_asked():
    assert tt.plan("fetched", "P", [], "tier1")["always_run_tier2"] is False


def test_off_never_runs_a_second_pass_whoever_decides():
    for d in tt.DECIDERS:
        p = tt.plan("off", "P", [], d)
        assert p["split"] is False and p["always_run_tier2"] is False


# ── the two first passes are told different things ──────────────────────────

def test_under_tier2_the_first_pass_is_not_asked_to_judge():
    """Asking a starved model to judge what it cannot see is the weakness this
    decider exists to avoid - so the marker protocol must be absent."""
    instr = tt.tier1_instruction("fetched", "tier2")
    assert tt.CONTINUE_MARK not in instr
    assert "Just answer." in instr


def test_under_tier2_the_first_pass_does_not_hedge_or_stall():
    """It must not tell the user to wait - the continuation is automatic."""
    instr = tt.tier1_instruction("fetched", "tier2")
    assert "do not hedge" in instr.lower()
    assert "ask the user to wait" in instr


def test_under_tier1_the_first_pass_still_gets_the_marker_protocol():
    instr = tt.tier1_instruction("fetched", "tier1")
    assert tt.CONTINUE_MARK in instr


def test_both_first_passes_are_told_what_they_lack():
    for d in tt.DECIDERS:
        assert "NOT" in tt.tier1_instruction("message", d)


# ── tier 2 carries the decision under the tier2 decider ─────────────────────

def test_tier2_is_told_how_to_say_the_context_added_nothing():
    p = tt.tier2_system_prefix("CTX", "already shown", "tier2")
    assert tt.NO_ADDITION_MARK in p
    assert "and nothing else" in p


def test_tier2_is_told_not_to_pad_just_to_look_thorough():
    """Without this the decider always says "yes" - continuing looks like work."""
    p = tt.tier2_system_prefix("CTX", "x", "tier2")
    assert "Do not continue merely to look thorough" in p


def test_tier2_is_told_that_stopping_is_a_good_outcome():
    p = tt.tier2_system_prefix("CTX", "x", "tier2")
    assert "stands as the whole answer" in p


def test_under_tier1_tier2_gets_no_decision_to_make():
    """The decision was taken before it ran; offering it again would let it
    silently overrule the first pass."""
    p = tt.tier2_system_prefix("CTX", "x", "tier1")
    assert tt.NO_ADDITION_MARK not in p
    assert "FIRST, DECIDE" not in p


def test_tier2_always_gets_the_context_and_the_shown_text():
    for d in tt.DECIDERS:
        p = tt.tier2_system_prefix("SECRET-CTX", "shown so far", d)
        assert "SECRET-CTX" in p and "shown so far" in p


# ── nothing reaches the screen before the decision ──────────────────────────

def test_a_no_addition_verdict_shows_the_user_nothing():
    g = tts.NoAdditionGate()
    out = "".join(g.feed(c) for c in ["[[NO-", "ADDITION]]"]) + g.flush()
    assert out == "" and g.suppressed is True


def test_a_verdict_arriving_whole_is_suppressed():
    g = tts.NoAdditionGate()
    assert g.feed(tts.NO_ADDITION) == "" and g.flush() == ""
    assert g.suppressed is True


def test_leading_whitespace_does_not_defeat_the_gate():
    g = tts.NoAdditionGate()
    out = g.feed("\n  ") + g.feed(tts.NO_ADDITION) + g.flush()
    assert out == "" and g.suppressed is True


def test_a_real_continuation_is_released_in_full():
    """Nothing may be lost while the gate was undecided."""
    g = tts.NoAdditionGate()
    out = g.feed(" And the branch is main.") + g.flush()
    assert out == " And the branch is main."
    assert g.suppressed is False


def test_a_continuation_that_merely_starts_like_the_sentinel_is_released():
    g = tts.NoAdditionGate()
    out = g.feed("[[NO") + g.feed("TE]] see below") + g.flush()
    assert "[[NOTE]] see below" in out
    assert g.suppressed is False


def test_once_released_the_gate_stops_buffering():
    g = tts.NoAdditionGate()
    g.feed("Real text. ")
    assert g.feed("More.") == "More."


def test_once_suppressed_nothing_further_escapes():
    g = tts.NoAdditionGate()
    g.feed(tts.NO_ADDITION)
    assert g.feed(" trailing rubbish") == ""


def test_gate_junk_does_not_raise():
    g = tts.NoAdditionGate()
    assert g.feed(None) == "" and g.feed("") == ""
    assert g.flush() == ""


# ── the call site ───────────────────────────────────────────────────────────

def _code():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "vera", "agents", "agents.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("async def _pump_two_tier():"):]
    body = body[:body.index("async def _pump_opener():")]
    opened = body.index('"""')
    closed = body.index('"""', opened + 3) + 3
    return body[closed:]


def test_the_second_pass_runs_when_the_decider_is_tier2():
    """Pin the CONDITION, not the mere presence of the flag - it also appears on
    the gate line below, so `if not filt.seen:` would still contain the word."""
    code = _code()
    assert 'if not (_tt_plan.get("always_run_tier2") or filt.seen):' in code


def test_the_gate_only_applies_under_the_tier2_decider():
    """Under tier1 there is no verdict to withhold, and buffering would delay
    the continuation for nothing."""
    code = _code()
    assert "NoAdditionGate()" in code
    assert 'if _tt_plan.get("always_run_tier2") else None' in code


def test_the_decider_reaches_the_tier2_prompt():
    assert '_tt_plan.get("decider")' in _code()
