"""What a loop engine actually accepts — not what its schema happens to declare.

`loops.run` filters the arguments it has assembled down to the engine
capability's schema before calling it, so a stream-only UI key (`loop_profile`,
`version`) can't raise TypeError on an engine that never declared it. That is
correct for v1-v6, which declare every parameter.

It is WRONG for v7 (and any future engine written the same way). v7 is
``async def cap_dag_agent_loop_v7(goal, **kwargs)`` - it sets the V7-defining
defaults and forwards everything else to the shared v6 runner. Its derived
schema therefore declares exactly one property, ``goal``, so the filter kept
``goal`` and threw away every other argument. Measured on the running instance
(2026-09-07):

    dag.agent_loop_v6  schema properties: 69  (model, allowed_caps, max_steps, ...)
    dag.agent_loop_v7  schema properties: 1   (goal)                **kwargs: yes

Consequences, all silent:

  * `bench.loop` ("benchmark a model by pinning it onto its node") defaults to
    profile=planning and passes model + instance_id - both dropped, so it
    benchmarked whatever the live routing chose, not the model asked for.
  * A per-run model override (census/suite model tracking) went the same way.
  * So did an explicit allowed_caps or max_steps from the caller.

A schema is a description of a signature; when the signature ends in
``**kwargs`` the description is incomplete by construction, and the fix is to
ask where the arguments really go rather than to trust the description. An
engine names its delegate with a ``delegates_to`` attribute and the delegate's
schema completes the picture.

WHY TWO SETS, NOT ONE
---------------------
The obvious fix - widen the filter and pass the whole assembled body - is a
bigger change than it looks. The `planning` profile (the only v7 profile) pins
``allowed_caps`` to nine read-mostly caps with no file-writing capability in
them. Those keys have NEVER reached a v7 engine, so every v7 run to date is a
bare v7 run. Letting the profile body through in the same commit would restrict
every existing v7 loop task to those nine caps and break every goal that writes
a file - a behaviour change nobody asked for, smuggled in behind a bug fix.

So the profile BODY keeps today's narrow, schema-only filter, and only what the
CALLER explicitly passed is widened by the delegate. That fixes the reported
harm (an override the caller set is honoured) and changes nothing else. Whether
the planning profile's toolkit should apply to v7 is a real question, but it is
a separate one, and it needs its own evidence.

`dropped()` exists because the root cause here was not the filter - it was that
the filter was SILENT. A discarded argument should be visible in a log line.

Pure: the caller supplies the schemas.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

#: Accepted by every engine regardless of what its schema says. session_id is
#: here because v7/v8 take it through **kwargs and dropping it made the loop
#: mint its own session, orphaning the UI timeline (fixed once, in-line, before
#: this module existed); trace_id because every engine takes it.
ALWAYS = ("trace_id", "session_id")


def takes_var_keyword(fn: Optional[Callable[..., Any]]) -> bool:
    """True when ``fn`` ends in ``**kwargs`` — i.e. its schema is incomplete."""
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):                       # builtins, C funcs
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def delegate_of(fn: Optional[Callable[..., Any]]) -> str:
    """The capability name ``fn`` forwards its **kwargs to, or ""."""
    return str(getattr(fn, "delegates_to", "") or "")


def body_accepted(engine_props: Iterable[str],
                  always: Iterable[str] = ALWAYS) -> Set[str]:
    """What the assembled PROFILE BODY may contain — the schema, unchanged.

    Deliberately not widened by the delegate. See the module docstring: the
    profile body has never reached a **kwargs engine, and switching it on is a
    behaviour change, not a bug fix.
    """
    return {str(p) for p in (engine_props or ())} | {str(p) for p in (always or ())}


def caller_accepted(engine_props: Iterable[str], *,
                    has_var_keyword: bool = False,
                    delegate_props: Optional[Iterable[str]] = None,
                    always: Iterable[str] = ALWAYS) -> Set[str]:
    """What an EXPLICIT caller argument may be — widened by the delegate.

    The delegate's schema is consulted only when the engine actually forwards
    **kwargs; an engine with a complete signature keeps its own filter, or a
    stream-only key would reach it and raise the TypeError the filter exists to
    prevent.
    """
    out = body_accepted(engine_props, always)
    if has_var_keyword and delegate_props:
        out |= {str(p) for p in delegate_props}
    return out


def filter_body(body: Dict[str, Any], accepted: Iterable[str]) -> Dict[str, Any]:
    ok = set(accepted or ())
    return {k: v for k, v in (body or {}).items() if k in ok}


def merge_call_kwargs(body: Dict[str, Any], caller: Dict[str, Any],
                      body_ok: Iterable[str], caller_ok: Iterable[str]) -> Dict[str, Any]:
    """The final kwargs: the filtered body, plus any key the caller named that
    the narrow body filter dropped.

    Strictly ADDITIVE — it restores keys, it never rewrites a value the body
    already carries. The body has been through the profile merge by this point,
    so its value for a key is the reconciled one (an ``allowed_caps`` union, for
    instance); overwriting that with the caller's raw value would quietly undo
    the merge for every engine, including the v1-v6 ones this is not about.
    """
    out = filter_body(body, body_ok)
    ok = set(caller_ok or ())
    for k, v in (caller or {}).items():
        if k in ok and k not in out:
            out[k] = (body or {}).get(k, v)
    return out


def dropped(body: Dict[str, Any], accepted: Iterable[str]) -> List[str]:
    """Argument names being discarded, sorted. Log this — a silent drop is how
    a model override goes missing for months without anyone noticing."""
    ok = set(accepted or ())
    return sorted(k for k in (body or {}) if k not in ok)
