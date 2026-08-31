"""A stated length should bound the generation.

`llm.generate` sets num_predict to the whole context window on the reasoning
that a model "still stops early at a natural EOS for short answers". Census run
18 disproved it and it cost a goal: prose-only asked for a 200-word explainer
and produced eval_count=16384 - the ceiling itself - in a single 949-second
call, three times over, wall-capping the goal.

The safe direction throughout is DOING NOTHING: an unrecognised request keeps
the existing full window, because a wrong small budget truncates real work,
which is the bug the full window was introduced to fix.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.capabilities.output_budget import (       # noqa: E402
    FLOOR_TOKENS, HEADROOM, budget_tokens, describe, requested_words,
)

CEIL = 16384


# --- the case from run 18 ---------------------------------------------------

def test_the_prose_only_request_is_bounded_far_below_the_ceiling():
    """"Write a 200-word explainer..." must not license 16384 tokens."""
    goal = ("Write a 200-word explainer of what a race condition is, aimed at "
            "a junior developer.")
    b = budget_tokens(goal, ceiling=CEIL)
    assert b is not None
    assert b < CEIL / 8, f"budget {b} is not meaningfully below the ceiling"
    assert b >= 200 * 2, "must still allow the full requested length"


def test_the_budget_leaves_generous_headroom_over_the_estimate():
    """Slightly-over is fine; the goal is stopping a 60x overrun."""
    b = budget_tokens("write 100 words about x", ceiling=CEIL)
    assert b >= int(100 * 2 * HEADROOM) * 0.9


# --- what counts as a stated length ----------------------------------------

@pytest.mark.parametrize("text,words", [
    ("a 200-word explainer", 200),
    ("write 200 words", 200),
    ("in about 500 words please", 500),
    ("three paragraphs on latency", 3 * 120),
    ("write two sentences", 2 * 25),
    ("one paragraph", 120),
])
def test_explicit_lengths_are_recognised(text, words):
    assert requested_words(text) == words


@pytest.mark.parametrize("text", [
    "write an explainer about race conditions",
    "summarise this file",
    "produce a full report on the findings",
    "refactor the module and add tests",
    "",
])
def test_no_stated_length_means_no_change(text):
    """The full window must survive for long structured output."""
    assert requested_words(text) is None
    assert budget_tokens(text, ceiling=CEIL) is None


def test_the_largest_stated_figure_wins():
    """Two separate figures must budget for the larger, never the smaller -
    under-budgeting truncates. Both must really match the pattern, or this
    test passes without exercising the choice at all (it did, at first)."""
    text = "write 2 paragraphs, or 5 paragraphs if the topic needs it"
    from vera.capabilities.output_budget import _PARA_RE
    assert len(_PARA_RE.findall(text)) == 2, "test does not exercise the choice"
    assert requested_words(text) == 5 * 120


def test_a_word_count_outranks_an_incidental_paragraph_mention():
    assert requested_words("a 300-word note, one paragraph") == 300


# --- bounds -----------------------------------------------------------------

def test_the_budget_never_exceeds_the_ceiling():
    assert budget_tokens("write 100000 words", ceiling=CEIL) == CEIL


def test_a_tiny_request_still_gets_a_workable_floor():
    """20 words estimates to 160 tokens; the floor keeps it usable."""
    assert budget_tokens("write 20 words", ceiling=CEIL) == FLOOR_TOKENS


def test_the_floor_never_exceeds_a_small_ceiling():
    assert budget_tokens("write 20 words", ceiling=128) == 128


def test_a_single_digit_numeral_is_deliberately_ignored():
    """"the top 5 words in the file" is a request ABOUT words, not FOR five of
    them. A false positive here truncates real work, so bare single digits do
    not count as a stated length and the full window is left alone."""
    assert requested_words("list the top 5 words in the file") is None
    assert budget_tokens("write 5 words", ceiling=CEIL) is None


def test_a_zero_or_negative_ceiling_yields_nothing():
    assert budget_tokens("write 200 words", ceiling=0) is None


def test_none_input_is_survivable():
    assert requested_words(None) is None
    assert budget_tokens(None, ceiling=CEIL) is None


# --- observability ----------------------------------------------------------

def test_a_bounded_generation_explains_itself():
    """A short output must never be a mystery in the log."""
    msg = describe("a 200-word explainer", ceiling=CEIL)
    assert "200 words" in msg and "num_predict" in msg


def test_an_unbounded_request_logs_nothing():
    assert describe("write a report", ceiling=CEIL) == ""


# --- the callsite -----------------------------------------------------------

def test_llm_generate_actually_applies_the_budget():
    path = os.path.join(os.path.dirname(__file__), "..", "vera",
                        "capabilities", "capabilities.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "_output_budget.budget_tokens(" in src
    assert '_gen_opts["num_predict"] = _bud' in src
    imp = src.index("import output_budget as _output_budget")
    use = src.index("_output_budget.budget_tokens(")
    assert imp < use, "the import must precede its use in file order"


def test_caller_supplied_options_still_win():
    """code.author and friends pass explicit sampling options; the budget must
    not override an option the caller set deliberately."""
    path = os.path.join(os.path.dirname(__file__), "..", "vera",
                        "capabilities", "capabilities.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    budget_at = src.index('_gen_opts["num_predict"] = _bud')
    merge_at = src.index("_gen_opts.update(options)")
    assert budget_at < merge_at, "caller options must be merged AFTER the budget"
