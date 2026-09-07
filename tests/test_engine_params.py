"""A model override that is silently discarded is worse than one that errors.

`loops.run` filtered its assembled arguments down to the engine capability's
schema. v7 is `(goal, **kwargs)`, so its derived schema declares ONE property
and the filter threw away everything else. Measured on the running instance
(2026-09-07): v6 declares 69 properties, v7 declares 1.

So `bench.loop` - "benchmark a model by pinning it onto its node", profile
defaults to planning (v7) - passed `model` and `instance_id` into a filter that
dropped both, and benchmarked whatever the live routing happened to choose.

The counterweight test in here is just as important: the `planning` profile
pins `allowed_caps` to nine read-mostly caps, and those keys have never reached
a v7 engine. Widening the filter for the profile BODY as well would restrict
every existing v7 loop task to those nine caps and break every goal that writes
a file - a behaviour change smuggled in behind a bug fix.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag import engine_params as EP                   # noqa: E402


# The two engines, as they actually are.
def _v6(goal, allowed_caps="", model="", max_steps=8, session_id="", trace_id=None):
    return None


def _v7(goal, **kwargs):
    return None


V6_PROPS = ["goal", "allowed_caps", "model", "max_steps", "session_id"]
V7_PROPS = ["goal"]

# What loops.run assembles for a planning-profile run: the profile's own body,
# and separately the arguments the caller actually asked for.
PROFILE_BODY = {"goal": "g", "allowed_caps": "caps.search,web.search",
                "base_toolkit": "caps.search,web.search", "plan_tier": "auto",
                "enable_master_planner": True, "agent_name": "agentic-planner",
                "loop_profile": "planning", "version": "v7"}
CALLER = {"goal": "g", "model": "qwen3.5:9b", "max_steps": 6, "session_id": "s"}


def _sets(engine_props, fn):
    body_ok = EP.body_accepted(engine_props)
    caller_ok = EP.caller_accepted(engine_props,
                                   has_var_keyword=EP.takes_var_keyword(fn),
                                   delegate_props=V6_PROPS)
    return body_ok, caller_ok


# ── the bug ─────────────────────────────────────────────────────────────────
def test_an_explicit_model_override_reaches_a_kwargs_engine():
    body_ok, caller_ok = _sets(V7_PROPS, _v7)
    out = EP.merge_call_kwargs(PROFILE_BODY, CALLER, body_ok, caller_ok)
    assert out.get("model") == "qwen3.5:9b", "the bench.loop / census defect"


def test_an_explicit_max_steps_reaches_a_kwargs_engine():
    body_ok, caller_ok = _sets(V7_PROPS, _v7)
    out = EP.merge_call_kwargs(PROFILE_BODY, CALLER, body_ok, caller_ok)
    assert out.get("max_steps") == 6


# ── the counterweight: do not switch the profile body on ────────────────────
def test_the_profile_body_is_NOT_widened_by_the_delegate():
    """planning pins a nine-cap allowed_caps with no file-writing capability.
    It has never reached v7; letting it through here would break every build
    goal, which is a behaviour change and not this commit's business."""
    body_ok, caller_ok = _sets(V7_PROPS, _v7)
    out = EP.merge_call_kwargs(PROFILE_BODY, CALLER, body_ok, caller_ok)
    assert "allowed_caps" not in out
    assert "base_toolkit" not in out and "plan_tier" not in out


def test_a_caller_who_asks_for_allowed_caps_still_gets_it():
    """The distinction is body-vs-caller, not key-by-key: an explicit
    restriction from the caller is honoured."""
    body_ok, caller_ok = _sets(V7_PROPS, _v7)
    caller = dict(CALLER, allowed_caps="echo")
    body = dict(PROFILE_BODY)
    body.pop("allowed_caps")             # nothing to reconcile against
    out = EP.merge_call_kwargs(body, caller, body_ok, caller_ok)
    assert out.get("allowed_caps") == "echo"


def test_the_reconciled_body_value_wins_over_the_callers_raw_one():
    """By this point the body has been through the profile merge, so its value
    IS the reconciled one (an allowed_caps union, say). Restoring a dropped key
    must use that, not re-apply the caller's raw value and undo the merge."""
    body_ok, caller_ok = _sets(V7_PROPS, _v7)
    body = dict(PROFILE_BODY, allowed_caps="caps.search,web.search,echo")
    caller = dict(CALLER, allowed_caps="echo")
    out = EP.merge_call_kwargs(body, caller, body_ok, caller_ok)
    assert out["allowed_caps"] == "caps.search,web.search,echo"


def test_a_present_body_value_is_never_rewritten():
    """For a v6 engine nothing is dropped, so the merge must be a no-op."""
    body_ok, caller_ok = _sets(V6_PROPS, _v6)
    body = dict(PROFILE_BODY, model="from-merge")
    out = EP.merge_call_kwargs(body, dict(CALLER, model="raw"), body_ok, caller_ok)
    assert out["model"] == "from-merge"


def test_stream_only_keys_never_get_through():
    """The filter's original job: a UI key reaching the engine raises the
    TypeError the filter exists to prevent."""
    body_ok, caller_ok = _sets(V7_PROPS, _v7)
    out = EP.merge_call_kwargs(PROFILE_BODY, CALLER, body_ok, caller_ok)
    for k in ("loop_profile", "version", "agent_name"):
        assert k not in out


# ── what must NOT change for a fully-declared engine ────────────────────────
def test_a_fully_declared_engine_keeps_its_own_filter():
    """v1-v6 declare every parameter. Consulting a delegate there would widen
    the filter for an engine that cannot absorb the extra keys."""
    acc = EP.caller_accepted(V6_PROPS, has_var_keyword=False,
                             delegate_props=["something_else"])
    assert "something_else" not in acc


def test_a_v6_profile_run_is_unchanged():
    body_ok, caller_ok = _sets(V6_PROPS, _v6)
    out = EP.merge_call_kwargs(PROFILE_BODY, CALLER, body_ok, caller_ok)
    assert out.get("allowed_caps") == "caps.search,web.search"  # body applies
    assert out.get("model") == "qwen3.5:9b"
    assert "loop_profile" not in out


def test_no_delegate_means_no_widening():
    acc = EP.caller_accepted(V7_PROPS, has_var_keyword=True, delegate_props=None)
    assert acc == {"goal", "trace_id", "session_id"}


def test_session_id_and_trace_id_are_always_accepted():
    """Dropping session_id made the loop mint its own session and orphaned the
    Loop Lab timeline. That fix must survive this one."""
    assert {"session_id", "trace_id"} <= EP.body_accepted([])


# ── detecting the shape ─────────────────────────────────────────────────────
def test_var_keyword_is_detected():
    assert EP.takes_var_keyword(_v7) is True
    assert EP.takes_var_keyword(_v6) is False


def test_an_uninspectable_callable_is_treated_as_fully_declared():
    """Fail closed: widening the filter for something we cannot read would push
    unknown keys at it.

    `int` and `dict` are here because inspect.signature genuinely RAISES on
    them ("no signature found for builtin type"); `len` and `print` do not -
    they carry a text signature and return normally, so they never exercise the
    except branch and prove nothing about it.
    """
    assert EP.takes_var_keyword(None) is False
    assert EP.takes_var_keyword(int) is False        # ValueError
    assert EP.takes_var_keyword(dict) is False       # ValueError
    assert EP.takes_var_keyword("not callable") is False   # TypeError


def test_the_delegate_is_read_from_the_function():
    assert EP.delegate_of(_v6) == ""
    _v6.delegates_to = "dag.agent_loop_v6"
    try:
        assert EP.delegate_of(_v6) == "dag.agent_loop_v6"
    finally:
        del _v6.delegates_to


# ── visibility ──────────────────────────────────────────────────────────────
def test_what_is_dropped_can_be_logged():
    """The root cause was not the filter, it was the SILENCE. A discarded
    argument has to be nameable or this recurs."""
    lost = EP.dropped(PROFILE_BODY, EP.body_accepted(V7_PROPS))
    assert "allowed_caps" in lost and "loop_profile" in lost
    assert "goal" not in lost


def test_nothing_dropped_reads_empty():
    assert EP.dropped({"goal": "g", "session_id": "s"}, EP.body_accepted(V7_PROPS)) == []


def test_filtering_an_empty_body_is_not_an_error():
    acc = EP.body_accepted(V6_PROPS)
    assert EP.filter_body({}, acc) == {}
    assert EP.dropped({}, acc) == []
    assert EP.merge_call_kwargs({}, {}, acc, acc) == {}
