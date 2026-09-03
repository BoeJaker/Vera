"""The prompt documented an argument as `text!` and then rejected `text!`.

Census 28, build-browser-verified: five consecutive operator.run calls died,
every one of them on a malformed argument. The Redis event trace shows what the
model actually sent::

    action='type'  args={"text! ": "bad-email", "ref= ": "e1"}
    action='type'  args={"text!": "bad-email!", "ref": "e1"}
    action='press' args={"key!": "Enter"}
    action='type'  args={"ref": "e1", "text!": "bad-email", "clear?": true}

This was not the model being careless. action_space_text() rendered the action
space as ``type(text! ref? clear? submit?)`` and NOTHING anywhere said that "!"
meant required and "?" optional. Shown `text!` as the argument, the model used
`text!` as the key - and validate_action rejected it with "type requires
'text'". The history line then echoed the bad key back every turn, teaching it
the same wrong name again.

Fixed in two places on purpose: the prompt no longer shows punctuation as part
of a name, AND the parser forgives it anyway, because the next model will
invent its own notation.

Pure: no Redis, no browser, no LLM.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.operator import actions as A            # noqa: E402
from vera.operator import operator_loop as OL     # noqa: E402
from vera.operator import safety as _safety       # noqa: E402

pytestmark = pytest.mark.critical


# -- the exact payloads from the census-28 trace -----------------------------

CENSUS_28 = [
    ("type",  {"text! ": '"bad-email"', "ref= ": "e1"}),
    ("type",  {"text!": "bad-email!", "ref": "e1"}),
    ("type",  {"ref": "e1", "text!": "bad-email", "clear?": True}),
    ("press", {"key!": "Enter"}),
]


@pytest.mark.parametrize("action,args", CENSUS_28)
def test_the_calls_that_lost_five_operator_runs_now_validate(action, args):
    v = A.validate_action(action, args)
    assert v["ok"] is True, v["error"]


def test_the_repaired_args_are_the_ones_handed_back():
    """Validating a repaired call and returning the raw one would fix nothing."""
    v = A.validate_action("type", {"text! ": "bad-email", "ref= ": "e1"})
    assert v["args"] == {"text": "bad-email", "ref": "e1"}


# -- normalise_arg_keys ------------------------------------------------------

def test_only_keys_are_touched_never_values():
    """A value may legitimately contain any of these characters."""
    v = A.normalise_arg_keys({"text!": "is this it? yes! a=b"})
    assert v == {"text": "is this it? yes! a=b"}


def test_an_exactly_correct_key_wins_over_a_noisy_duplicate():
    """A model that emitted both meant the documented one."""
    assert A.normalise_arg_keys({"text": "real", "text!": "guess"}) == {"text": "real"}
    assert A.normalise_arg_keys({"text!": "guess", "text": "real"}) == {"text": "real"}


def test_a_key_that_is_only_noise_is_dropped_not_kept_as_empty():
    assert A.normalise_arg_keys({"!?": "x", "text": "y"}) == {"text": "y"}


def test_a_clean_call_is_returned_unchanged():
    clean = {"text": "hello", "ref": "e1", "clear": True, "submit": False}
    assert A.normalise_arg_keys(dict(clean)) == clean


def test_non_string_keys_do_not_crash_it():
    out = A.normalise_arg_keys({1: "a", None: "b", "text!": "c"})
    assert out[1] == "a" and out["text"] == "c"


def test_empty_and_none_are_fine():
    assert A.normalise_arg_keys(None) == {}
    assert A.normalise_arg_keys({}) == {}


def test_a_genuinely_missing_argument_is_still_rejected():
    """Forgiveness must not become blindness - the guard still has to work."""
    assert A.validate_action("type", {})["ok"] is False
    assert A.validate_action("type", {"ref": "e1"})["ok"] is False
    assert A.validate_action("press", {})["ok"] is False
    assert A.validate_action("click", {})["ok"] is False


# -- the prompt that caused it ----------------------------------------------

def test_the_action_space_never_shows_punctuation_glued_to_a_name():
    """The regression that started it: `text!` presented as the argument."""
    txt = A.action_space_text()
    for bad in ("text!", "key!", "clear?", "url!", "ref!", "direction!",
                "submit?", "summary?", "value?", "label?", "ms?", "selector?"):
        assert bad not in txt, f"the action space still shows {bad!r} as a name"


def test_the_action_space_still_says_which_arguments_are_required():
    """Stripping the markers must not lose the information they carried."""
    txt = A.action_space_text()
    assert "required: text" in txt
    assert "optional: ref, clear, submit" in txt
    assert "required: key" in txt
    assert "no args" in txt                      # screenshot


def test_every_action_is_still_listed_with_its_doc():
    txt = A.action_space_text()
    for name in A.ACTIONS:
        assert ("- " + name + " ") in txt


def test_the_either_or_actions_keep_reading_naturally():
    """click/hover take ref OR x,y - not a required/optional split."""
    txt = A.action_space_text()
    assert "ref | x,y" in txt


def test_split_arg_spec_separates_required_from_optional():
    assert A.split_arg_spec("text! ref? clear?") == (["text"], ["ref", "clear"], "")
    assert A.split_arg_spec("") == ([], [], "")
    assert A.split_arg_spec("ref | x,y") == ([], [], "ref | x,y")


# -- end to end: the repaired call must reach the browser --------------------

class _Session:
    def __init__(self):
        self.history = []
        self.session_id = "s1"


def test_a_noisy_call_reaches_the_executor_repaired():
    """Drives the real loop and inspects what act() actually received.

    actions.perform re-validates and would repair these itself, so this is not
    the only thing standing between the model and a working click. What it
    pins is that the DECISION carries repaired args - which is what stops
    "text!" being written into history and echoed back as the argument name on
    the next turn. See test_the_history_no_longer_teaches_the_bad_key_back.
    """
    seen = {}

    async def observe(_s, i):
        o = type("O", (), {})()
        o.url, o.title, o.text = "http://x/form", "Form", "step %d" % i
        o.elements, o.screenshot_path = [], ""
        return o

    async def think(_g, _o, _h, _c):
        # The REAL parse + repair path, not a reimplementation of it: this is
        # the code think() runs after the LLM replies.
        from vera.operator.thinker import parse_decision, finalise_decision
        import json as _j
        raw = _j.dumps({"thought": "type a bad address", "action": "type",
                        "args": {"text! ": "bad-email", "ref= ": "e1"},
                        "done": False})
        return finalise_decision(parse_decision(raw))

    async def act(_s, action, args):
        seen["action"], seen["args"] = action, dict(args)
        return {"ok": True}

    asyncio.run(OL.run_loop(
        "type a malformed address", _Session(),
        observe_fn=observe, think_fn=think, act_fn=act, max_steps=1,
        policy=_safety.SafetyPolicy(allowlist=["x"], allow_destructive=True,
                                    confirm=True)))
    assert seen.get("action") == "type", "the action never reached the executor"
    assert seen["args"] == {"text": "bad-email", "ref": "e1"}, \
        "the executor got the RAW args, not the repaired ones"


def test_finalise_decision_is_what_think_actually_runs():
    """Guard the wiring: if think() stops calling it, this file's end-to-end
    test would keep passing while the live path regressed."""
    import inspect
    from vera.operator import thinker
    src = inspect.getsource(thinker)
    assert "decision = finalise_decision(decision)" in src


def test_finalise_decision_marks_an_illegal_action_and_does_not_repair_it():
    from vera.operator.thinker import finalise_decision
    d = finalise_decision({"action": "type", "args": {}, "done": False})
    assert d.get("invalid"), "a missing required argument must still be flagged"


def test_finalise_decision_leaves_a_parse_error_alone():
    from vera.operator.thinker import finalise_decision
    d = finalise_decision({"error": "could not parse decision JSON: ..."})
    assert d.get("invalid") is None


def test_the_history_no_longer_teaches_the_bad_key_back():
    """The reinforcement loop, which is the real reason for the writeback.

    build_prompt drops the "text" argument from each history line to keep the
    prompt small. "text!" is not "text", so the bad key AND the typed value
    were both echoed back every turn, showing the model its own mistake as if
    it were the schema.
    """
    from vera.operator.thinker import build_prompt, finalise_decision
    d = finalise_decision({"action": "type",
                           "args": {"text! ": "bad-email", "ref= ": "e1"},
                           "done": False})
    obs = type("O", (), {})()
    obs.url, obs.title, obs.text, obs.elements = "http://x", "X", "", []
    prompt = build_prompt("goal", obs, history=[{"action": "type",
                                                 "args": d["args"],
                                                 "result": {"ok": True}}])
    blob = prompt["system"] + prompt["user"]
    assert "text!" not in blob, "the bad key is still being echoed back"
    assert "bad-email" not in blob, "the typed value leaked back into the prompt"
