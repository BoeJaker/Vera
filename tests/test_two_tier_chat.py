"""A two-tier reply must read as one message, and the marker must never show.

Time to first token is dominated by work done before the model sees anything:
session memory, ontology fragments, retrieved Q&A, and a blocking web.search the
main generation is gated on. Tier 1 answers from the question alone; tier 2
continues it with the full context, but only if the model says it needs to.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.agents import two_tier as tt              # noqa: E402
from vera.agents import two_tier_stream as tts      # noqa: E402

pytestmark = pytest.mark.critical


PREFIX = """You are Aide, a careful assistant.

## Session memory
The user prefers metric units.

## Ontology: billing
invoice -> customer

## Live context (auto-loaded)
current_branch = main
"""


# ── what tier 1 is starved of ───────────────────────────────────────────────

def test_fetched_drops_retrieved_material_and_keeps_the_persona():
    """Starving tier 1 of who it is would change its voice, and the two halves
    have to read as one message."""
    out = tt.tier1_system_prefix(PREFIX, "fetched")
    assert "You are Aide" in out
    for gone in ("Session memory", "Ontology", "Live context", "metric units"):
        assert gone not in out, gone


def test_message_level_drops_everything():
    assert tt.tier1_system_prefix(PREFIX, "message") == ""
    assert tt.tier1_history([{"role": "user"}], "message") == []


def test_fetched_keeps_the_conversation_so_followups_work():
    """"what about the second one" is unanswerable without history."""
    h = [{"role": "user", "content": "hi"}]
    assert tt.tier1_history(h, "fetched") == h


def test_off_is_todays_behaviour():
    p = tt.plan("off", PREFIX, [{"role": "user"}])
    assert p["split"] is False
    assert p["system_prefix"] == PREFIX


def test_an_unknown_level_means_off():
    """A typo in a tuning value must not silently start starving prompts."""
    for bad in ("aggressive", "", None, "MESSAGE_"):
        assert tt.normalise_level(bad) == "off"
        assert tt.plan(bad, PREFIX)["split"] is False


def test_levels_are_case_insensitive():
    assert tt.normalise_level("FETCHED") == "fetched"


def test_a_prefix_with_no_headings_survives_intact():
    assert tt.strip_fetched_context("Just an instruction.") == "Just an instruction."


def test_a_non_fetched_heading_is_kept():
    """Only RETRIEVED material goes; an agent's own structure stays."""
    src = "Persona.\n\n## House style\nBe terse.\n\n## Session memory\nsecrets\n"
    out = tt.strip_fetched_context(src)
    assert "House style" in out and "Be terse" in out
    assert "Session memory" not in out and "secrets" not in out


# ── tier 1 must know what it is missing ─────────────────────────────────────

def test_tier1_is_told_precisely_what_it_lacks():
    """A model told only "be quick" guesses at remembered facts instead of
    admitting it was not given them. True under either decider."""
    m = tt.tier1_instruction("message", "tier1")
    f = tt.tier1_instruction("fetched", "tier1")
    assert "conversation so far" in m
    assert "conversation so far IS included" in f


def test_tier1_is_told_not_to_guess_to_avoid_continuing():
    """Under the tier1 decider only - it is the pass that has to judge."""
    instr = tt.tier1_instruction("fetched", "tier1")
    assert "Do not guess" in instr
    assert "confident wrong answer is worse" in instr


def test_tier1_is_told_never_to_mention_the_marker():
    """Under the tier1 decider only - tier2 gives it no marker at all."""
    assert "Never mention" in tt.tier1_instruction("message", "tier1")


def test_off_has_no_instruction():
    assert tt.tier1_instruction("off") == ""


# ── the continuation reads as one message ───────────────────────────────────

def test_tier2_is_given_what_the_user_already_sees():
    p = tt.tier2_system_prefix(PREFIX, "Partial answer so far." + tt.CONTINUE_MARK, "tier1")
    assert "Partial answer so far." in p
    assert tt.CONTINUE_MARK not in p, "the marker must not reach tier 2's prompt"


def test_tier2_is_forbidden_the_tells_of_a_second_message():
    p = tt.tier2_system_prefix(PREFIX, "x")
    for forbidden in ("Do NOT greet", "as I mentioned", "restate"):
        assert forbidden in p


def test_tier2_still_gets_the_full_context():
    p = tt.tier2_system_prefix(PREFIX, "x")
    assert "Session memory" in p and "metric units" in p


def test_the_marker_is_stripped_with_its_whitespace():
    assert tt.strip_marker("Answer.\n\n" + tt.CONTINUE_MARK) == "Answer."


def test_wants_continuation_detects_the_marker():
    assert tt.wants_continuation("text " + tt.CONTINUE_MARK) is True
    assert tt.wants_continuation("a complete answer") is False


# ── the marker must never reach the screen ──────────────────────────────────

def test_a_marker_split_across_chunks_never_leaks():
    """The tokeniser decides the split, not us: "[[NEEDS" and "-CONTEXT]]" can
    arrive separately, and a per-chunk replace() would emit the first half."""
    f = tts.MarkerFilter()
    shown = "".join(f.feed(c) for c in ["Some answer.", " [[NEEDS", "-CONT", "EXT]]"])
    shown += f.flush()
    assert "[[" not in shown and "NEEDS" not in shown
    assert f.seen is True
    assert shown.strip() == "Some answer."


def test_a_marker_arriving_whole_is_removed():
    f = tts.MarkerFilter()
    shown = f.feed("Done. " + tts.MARKER) + f.flush()
    assert tts.MARKER not in shown and f.seen is True


def test_text_that_never_contains_the_marker_is_not_delayed():
    """Buffering everything would remove the marker and the point of the
    feature with it."""
    f = tts.MarkerFilter()
    assert f.feed("The answer is 42.") == "The answer is 42."
    assert f.seen is False


def test_only_a_genuine_prefix_of_the_marker_is_held_back():
    f = tts.MarkerFilter()
    emitted = f.feed("cost is [[")          # could begin the marker - hold it
    assert emitted == "cost is "
    emitted += f.feed("a] bracket")         # it was not - release it
    assert "[[a] bracket" in emitted
    assert f.seen is False


def test_a_held_back_tail_is_released_on_flush():
    f = tts.MarkerFilter()
    f.feed("ends with [[NEE")
    assert f.flush() == "[[NEE"
    assert f.seen is False


def test_text_reports_what_the_user_actually_saw():
    """tier2 is handed this, so it must match the screen exactly."""
    f = tts.MarkerFilter()
    f.feed("Half an answer. ")
    f.feed(tts.MARKER)
    f.flush()
    assert f.text.strip() == "Half an answer."


def test_filter_junk_does_not_raise():
    f = tts.MarkerFilter()
    assert f.feed(None) == "" and f.feed("") == ""
    assert f.flush() == ""


# ── the call site ───────────────────────────────────────────────────────────

def _agents_src():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "vera", "agents", "agents.py"),
              encoding="utf-8") as fh:
        return fh.read()


def _pump_two_tier_code():
    """The BODY of _pump_two_tier with its docstring removed.

    The docstring says "does NOT await _web_task", so a naive substring check
    matches the prose that documents the property rather than the code.
    """
    src = _agents_src()
    body = src[src.index("async def _pump_two_tier():"):]
    body = body[:body.index("async def _pump_opener():")]
    opened = body.index('"""')
    closed = body.index('"""', opened + 3) + 3
    return body[closed:]


def test_tier1_does_not_wait_for_the_web_search():
    """Gating tier 1 on a search would reinstate the latency this removes."""
    code = _pump_two_tier_code()
    first = code.index("AGENT_RUNNER.run_stream")
    assert "await _web_task" not in code[:first]


def test_tier2_still_gates_on_the_web_search():
    code = _pump_two_tier_code()
    first = code.index("AGENT_RUNNER.run_stream")
    assert "await _web_task" in code[first:]


def test_the_split_is_opt_in_at_the_call_site():
    src = _agents_src()
    assert '_tt_plan.get("split")' in src
    assert "_pump_two_tier() if (_two_tier is not None" in src


def test_a_failed_import_leaves_chat_single_pass():
    src = _agents_src()
    assert "_two_tier = _tt_stream = None" in src
    assert '{"split": False} if _two_tier is None' in src
