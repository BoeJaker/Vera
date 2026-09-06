"""What the page DISPLAYED is the finding; the stop code is not.

Census 37, author-then-edit. Four operator.run calls, 1803s, wall-cap, and the
artifact scored 3/3 on its checks. Reading the real file afterwards settles what
happened - `code.edit` had changed the variable and left the display:

    let remainingSeconds = 90;                                  <- edited
    <div class="timer" id="timer">01:00</div>                   <- NOT edited
    resetTimer(): textContent = '01:00'                         <- NOT edited

So the page genuinely showed 01:00. The operator read it correctly on its very
first observation ("the current display shows '01:00'") and kept saying so. That
never reached the caller: `explain()` quoted the stop record, so the agentic loop
was handed "repeating_action" and answered by running the browser three more
times instead of fixing the file.

Two separate defects were being hidden by one silence: the file was half-edited,
and the goal it was being checked against was unobservable anyway (see
test_loop_prompt_rules for that half).

Pure: no browser, no LLM.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import operator_loop as OL              # noqa: E402
from vera.operator import safety as _safety                # noqa: E402
from vera.operator import stop_explanation as SE           # noqa: E402


# ── the explanation carries what was seen ───────────────────────────────────
def test_the_displayed_value_reaches_the_caller():
    steps = [{"i": 1, "phase": "act", "action": "click", "seen": "01:00"},
             {"i": 2, "phase": "repeating", "reason": "click was attempted 5 times"}]
    out = SE.explain("repeating_action", steps)
    assert "01:00" in out, "the census 37 finding must be in the sentence"


def test_the_stop_reason_is_still_there():
    """`reason` is the machine-readable code and the UI switches on it; the
    observation is added BESIDE it, never instead of it."""
    steps = [{"i": 1, "phase": "act", "seen": "01:00"},
             {"i": 2, "phase": "repeating", "reason": "click was attempted 5 times"}]
    out = SE.explain("repeating_action", steps)
    assert out.startswith("repeating_action:")
    assert "attempted 5 times" in out


def test_it_says_to_fix_the_file_rather_than_re_run():
    """The loop's actual next move was three more browser runs."""
    out = SE.explain("repeating_action", [{"seen": "01:00"}])
    assert "fix the file" in out


def test_the_most_recent_observation_wins():
    steps = [{"seen": "01:30"}, {"seen": "00:47"}, {"seen": "00:31"}]
    assert SE.last_seen(steps) == "00:31"


def test_nothing_is_invented_when_no_observation_was_recorded():
    """Runs recorded before `seen` was carried must get the old explanation,
    not a fabricated one."""
    out = SE.explain("max_steps", [{"i": 1, "phase": "act", "action": "click"}])
    assert "DISPLAYED" not in out
    assert out.startswith("max_steps")


def test_a_successful_run_still_explains_nothing():
    assert SE.explain("done", [{"seen": "01:30"}]) == ""
    assert SE.explain("cancelled", [{"seen": "01:30"}]) == ""


def test_the_quoted_text_is_bounded():
    out = SE.explain("max_steps", [{"seen": "x" * 5000}])
    assert len(out) < 800


def test_whitespace_in_the_observation_is_normalised():
    assert SE.last_seen([{"seen": "  01:00\n\n  remaining  "}]) == "01:00 remaining"


# ── it is a quote, not a verdict ────────────────────────────────────────────
def test_the_observation_is_quoted_not_judged():
    """A countdown caught mid-tick legitimately reads 00:47. Asserting that as
    'wrong' here would invent a defect as confidently as the old silence hid
    one - the text is reported and the comparison is left to the caller."""
    out = SE.explain("time_budget", [{"seen": "00:47"}])
    assert "00:47" in out
    for verdict in ("incorrect", "wrong value", "failed to", "is broken"):
        assert verdict not in out.lower()


# ── the loop records it ─────────────────────────────────────────────────────
class _Obs:
    def __init__(self, text):
        self.url = "http://x/timer.html"
        self.title = "Timer"
        self.elements = []
        self.text = text
        self.screenshot_path = ""


class _Session:
    def __init__(self):
        self.history = []
        self.session_id = "s1"


def test_the_loop_puts_the_observation_in_the_step_record():
    """It has always gone into `history` for the model's next prompt; it never
    reached `steps`, which is what the caller and explain() actually see."""
    async def observe(_s, _i):
        return _Obs("01:00")

    async def think(_g, _o, _h, _c):
        return {"action": "click", "args": {"ref": "e1"}, "thought": "t"}

    async def act(*_a, **_k):
        return {"ok": True}

    res = asyncio.run(OL.run_loop(
        "verify the countdown", _Session(), observe_fn=observe, think_fn=think,
        act_fn=act, max_steps=3,
        policy=_safety.SafetyPolicy(allowlist=["x"], allow_destructive=True,
                                    confirm=True)))
    acts = [s for s in res["steps"] if s.get("phase") == "act"]
    assert acts and acts[0].get("seen") == "01:00"


def test_the_finding_survives_all_the_way_into_the_error_the_loop_reads():
    """End to end: a run that stops on the repeat guard reports the displayed
    value in the `error` the agentic loop surfaces to its executor."""
    async def observe(_s, _i):
        return _Obs("01:00")

    async def think(_g, _o, _h, _c):
        return {"action": "click", "args": {"ref": "e1"}, "thought": "t"}

    async def act(*_a, **_k):
        return {"ok": True}

    res = asyncio.run(OL.run_loop(
        "confirm it starts at 01:30", _Session(), observe_fn=observe,
        think_fn=think, act_fn=act, max_steps=12,
        policy=_safety.SafetyPolicy(allowlist=["x"], allow_destructive=True,
                                    confirm=True)))
    assert res["reason"] == "repeating_action"
    assert "01:00" in res.get("error", ""), "the caller is told what it saw"
