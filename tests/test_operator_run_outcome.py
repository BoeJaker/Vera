"""An operator run that never reached its goal must not report success.

Census build-browser-verified (2026-08-29) wall-capped without finishing. Four
operator.run calls consumed 21m24s of the 25-minute budget:

    9m16s  reason=max_steps      ok=true
      28s  reason=think_error    ok=true
    7m45s  reason=max_steps      ok=true
    3m53s  reason=too_many_errors ok=false

`reason` carried the truth the whole time; only `ok` disagreed with it, so the
agentic loop saw nothing wrong and simply called it again. The think_error one
was a decision the model had actually made, thrown away because it used curly
quotes instead of ASCII ones.

Step COUNT is deliberately not restricted here - a long run is legitimate; a long
run that lies about its outcome is not.
"""
import asyncio

import pytest

try:
    from Vera.vera.operator import operator_loop as L
    from Vera.vera.operator import thinker as T
except Exception:                                      # pragma: no cover
    L = T = None

pytestmark = pytest.mark.skipif(L is None or T is None,
                                reason="operator modules not importable here")

Q_OPEN, Q_CLOSE = chr(0x201C), chr(0x201D)      # curly double quotes, ASCII source


def test_this_module_actually_imported_the_app():
    assert L is not None and hasattr(L, "run_loop")
    assert T is not None and hasattr(T, "parse_decision")


# ── the decision parser ──────────────────────────────────────────────────────

def test_a_decision_in_curly_quotes_is_still_a_decision():
    text = ("{" + Q_OPEN + "thought" + Q_CLOSE + ": " + Q_OPEN + "click it" + Q_CLOSE +
            ", " + Q_OPEN + "action" + Q_CLOSE + ": " + Q_OPEN + "click" + Q_CLOSE +
            ", " + Q_OPEN + "args" + Q_CLOSE + ": {}, " + Q_OPEN + "done" + Q_CLOSE +
            ": false}")
    d = T.parse_decision(text)
    assert "error" not in d, d
    assert d["action"] == "click"
    assert d["thought"] == "click it"
    assert d["done"] is False


def test_ordinary_json_is_unaffected():
    d = T.parse_decision('{"thought": "t", "action": "type", "args": {"text": "x"}, "done": false}')
    assert d["action"] == "type" and d["args"] == {"text": "x"} and d["done"] is False


def test_a_done_decision_still_reads_as_done():
    d = T.parse_decision('{"thought": "", "action": "done", "args": {}, "done": true}')
    assert d["done"] is True


def test_genuinely_unparseable_output_still_errors():
    d = T.parse_decision("I think we should probably click the button now.")
    assert "error" in d and "could not parse" in d["error"]


def test_the_action_only_fallback_still_works():
    d = T.parse_decision('some prose then action: click and more prose')
    assert d.get("action") == "click"


# ── the run outcome ──────────────────────────────────────────────────────────

class _Obs:
    url = ""
    screenshot_path = ""


class _Session:
    def __init__(self):
        self.history = []
        self.target = {}


async def _observe(session, i):
    return _Obs()


def _thinks(decision):
    async def _think(*a, **k):
        return dict(decision)
    return _think


def _run(decision, max_steps):
    return asyncio.run(L.run_loop(
        "a goal", _Session(), max_steps=max_steps,
        observe_fn=_observe, think_fn=_thinks(decision),
        act_fn=None))


def test_exhausting_the_step_budget_is_not_success():
    """The headline case: two calls did this for 9 and 8 minutes, reporting ok."""
    res = _run({"invalid": "no such action", "action": "bogus",
                "args": {}, "thought": ""}, max_steps=2)
    assert res["reason"] == "max_steps"
    assert res["done"] is False
    assert res["ok"] is False, (
        "a run that hit the step ceiling without finishing still reports ok - "
        "the caller cannot tell it failed and simply calls it again")


def test_reaching_the_goal_is_success():
    res = _run({"done": True, "action": "done",
                "args": {"summary": "all good"}, "thought": "t"}, max_steps=5)
    assert res["reason"] == "done"
    assert res["done"] is True
    assert res["ok"] is True


def test_a_run_that_gives_up_on_errors_is_not_success():
    res = _run({"invalid": "no such action", "action": "bogus",
                "args": {}, "thought": ""}, max_steps=20)
    assert res["reason"] == "too_many_errors"
    assert res["ok"] is False


def test_the_step_count_itself_is_not_capped_by_this_change():
    """A long run stays legitimate - only its honesty changed."""
    res = _run({"invalid": "x", "action": "bogus", "args": {}, "thought": ""},
               max_steps=3)
    assert res["step_count"] == 3
