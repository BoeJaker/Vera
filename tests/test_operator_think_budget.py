"""One browser decision must not be allowed to cost 199 seconds.

Census 35, author-then-edit run 9c81d67747. Twelve steps averaging 31s and 545
tokens, then step 13:

    ollama_done 199.25s eval_count=3382 tok/s=17.0 caller=capabilities.py:operator.think

The run's whole budget is 480s, so that single answer is what turned a run which
fitted into one which overran (581s). The model was not slow - 17 tok/s is its
normal rate - it simply kept writing.

It was allowed to because `decide` had a `max_tokens` argument that it only ever
passed to providers.chat. The ollama path - the one the cluster actually runs -
sent no cap, and llm.generate grants the full VERA_LLM_GEN_CTX window (16384)
whenever the prompt does not state a length. This prompt asks for one small JSON
object, so five times worse than 3382 was available.

Pure: the cap dispatcher is a stub, so this asserts what `decide` SENDS.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import thinker as TH                    # noqa: E402


class _Obs:
    url = "http://x/timer.html"
    title = "Timer"
    elements = []
    text = "01:30"

    def compact(self, max_elements=60):
        return "TEXT: 01:30"


_REPLY = '{"thought":"t","action":"click","args":{"ref":"e1"},"done":false}'


def _decide(provider="ollama", **kw):
    """Run the real decide() against a recording stub."""
    seen = {}

    async def call_cap(name, **kwargs):
        seen["name"] = name
        seen["kwargs"] = kwargs
        return {"text": _REPLY}

    out = asyncio.run(TH.decide("goal", _Obs(), [], call_cap,
                                provider=provider, **kw))
    return seen, out


# ── the fix ─────────────────────────────────────────────────────────────────
def test_the_ollama_path_sends_an_output_cap():
    """The gap: this call carried no num_predict at all."""
    seen, _ = _decide()
    assert seen["name"] == "llm.generate"
    opts = seen["kwargs"].get("options") or {}
    assert opts.get("num_predict") == TH.THINK_MAX_TOKENS


def test_the_cap_would_have_bounded_the_observed_runaway():
    """3382 tokens is the measured failure; the cap must be well under it."""
    assert TH.THINK_MAX_TOKENS < 3382


def test_the_cap_clears_the_largest_legitimate_decision():
    """The biggest decision that PARSED across all three census 35 runs was 816
    tokens. A cap under that would truncate working answers - a worse bug than
    the one being fixed."""
    assert TH.THINK_MAX_TOKENS >= 816


def test_the_cap_is_far_under_the_window_it_used_to_inherit():
    """VERA_LLM_GEN_CTX defaults to 16384 and num_predict rode that ceiling."""
    assert TH.THINK_MAX_TOKENS <= 16384 // 4


# ── the decision still works ────────────────────────────────────────────────
def test_the_decision_is_still_parsed_and_returned():
    """A cap that broke the happy path would be no fix at all."""
    _seen, out = _decide()
    assert out["action"] == "click" and out["args"] == {"ref": "e1"}
    assert not out.get("error")


def test_the_other_arguments_are_unchanged():
    """job_type drives cluster routing and caller drives the logs that made this
    diagnosable; neither may be lost to the edit."""
    seen, _ = _decide()
    kw = seen["kwargs"]
    # "loop_executor" since 2026-09-22: the think shares the loop executor's
    # runner (model + num_ctx) instead of reloading the model on the 12 GB card
    # as job "code" did (plan item 1, loop-census-improvement).
    assert kw["job_type"] == "loop_executor"
    assert kw["caller"] == "operator.think"
    assert kw["system"] and kw["prompt"]


def test_think_is_still_forwarded():
    seen, _ = _decide(think=False)
    assert seen["kwargs"]["think"] is False


# ── no regression for the hosted providers ──────────────────────────────────
def test_the_provider_path_is_untouched():
    """providers.chat has its own max_tokens argument and no options; sending
    ollama's key there would be meaningless at best."""
    seen, _ = _decide(provider="anthropic:claude-x", max_tokens=333)
    assert seen["name"] == "providers.chat"
    assert seen["kwargs"]["max_tokens"] == 333
    assert "options" not in seen["kwargs"]


# ── truncation is survivable ────────────────────────────────────────────────
def test_a_response_cut_off_by_the_cap_still_yields_an_action():
    """The cap can truncate a rambling answer mid-object. parse_decision's
    last-resort regex is what keeps that from costing the whole step."""
    d = TH.parse_decision('{"thought": "I will start by clicking the start '
                          'button because the timer shows 01:30 and I need '
                          '"action": "click", "args": {"ref": "e1"')
    assert d.get("action") == "click"
    assert not d.get("error")
