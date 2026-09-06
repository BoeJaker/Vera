"""Reduce one loop run's event list to a diagnostic digest — and make the step
accounting add up.

This is the pure half of `workshop.agent_loop.trace`. The cap keeps the Redis
reads; everything here is a fold over an already-decoded event list, so it can
be tested without the app.

## Why this was extracted: the counters did not add up

The census (12 goals, run serially, `run_census.py`) judges a run largely by
these counters, so a counter that quietly lies corrupts every conclusion drawn
from it. Run 10 produced two runs where `executed_steps` exceeded
`planned_steps` while `inserted_steps` stayed 0 — arithmetic that cannot be
right, and which was read as "something adds a step without recording it".

Both sightings are now explained, and neither was a miscount of executions.
They were two *blind spots* in this reduction:

  * `operate-exec` (`planned=2 executed=3 inserted=0`). The adaptive controller
    is not the only thing that can add a step. When the run ends, the
    **completion gate** re-reads the goal and can append remediation steps —
    `agent_loop_v6.gate` carries them as `follow_up`, and the executor then runs
    them for real. Step 3 there was the gate's "Write and save the report".
    The old reduction harvested insertions from `.assess` events ONLY
    (`e["steps"]`), so an entire second insertion channel was invisible.

  * `research-web` in run 15 (`planned=3 executed=5`, one step claimed by
    nothing). A THIRD channel: when a step fails, the `extra_step` recovery
    strategy mints a replacement step through `_v6_adjust_step`, pushes it onto
    the queue and runs it — emitting `agent_loop_v6.recovery_step` and nothing
    the accounting read. Two fired in that run, both because `web.research`
    returned zero sources; step 9 "Targeted vendor site search via web.search"
    was one of them. This is the producer the plan called "the third producer"
    and could not name for two passes.

  * `trivial-chat` (`planned=0 executed=1`). The v7 single-cap **fast path**
    short-circuits planning entirely: it emits a synthetic `step_start` /
    `tool_done` / `step_done` for step 1 so the shared renderer shows a real
    step card, and never emits a `.plan` event at all. `planned_steps=0` was
    therefore honest but unexplained — it reads as a planning failure when in
    fact no plan was ever supposed to exist.

A third defect fell out of the same read: the digest's gate record looked for
the keys `verdict` / `reason` / `met`, which the emitter has never written (it
writes `complete` / `missing` / `round` / `follow_up`). `plan["gate"]` was
therefore always `{}` — the gate's verdict, the reason a run was extended, was
being dropped on the floor.

## The rule this module now enforces

Every executed step must be accounted for by name:

    executed = planned + inserted(controller) + inserted(gate) + fast-path
               + unaccounted

`unaccounted_steps` exists so that the *next* thing that learns to add a step
cannot repeat this. Rather than the census silently disbelieving its own
arithmetic, an unattributed step is counted and named in `warnings`. A blind
spot that announces itself is a bug report; one that does not is a year of
mismeasured runs.

Pure: no Redis, no app imports.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

# Dual-spelled: Vera.vera.* resolves to the DEPLOYED checkout, which does not
# have a module until it lands there, so a NEW sibling must fall back to the
# plain package or this whole module fails to import.
try:
    from Vera.vera.dag import error_excerpt as _error_excerpt
except ImportError:                                        # pragma: no cover
    from vera.dag import error_excerpt as _error_excerpt

# Step ids travel as ints in the plan and as ints in the events, but they cross
# a JSON boundary and are re-parsed by several producers, so compare them as
# normalised strings rather than trusting the type to survive the round trip.
# An id-type mismatch here would silently make EVERY executed step look
# unaccounted, which is exactly the class of failure this module exists to stop.
def norm_id(v: Any) -> Optional[str]:
    """Canonical form of a step id, or None if there isn't one."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _clip_factory(include_text: bool):
    def _clip(v: Any, n: int = 220) -> str:
        s = str(v or "").strip()
        return s if include_text or len(s) <= n else s[:n] + "…"
    return _clip


def digest_events(events: Sequence[Dict[str, Any]],
                  *,
                  include_text: bool = False,
                  stage_audit: Any = None) -> Dict[str, Any]:
    """Fold a run's events into {plan, steps, control, gates, counters, warnings}.

    `stage_audit` is the optional `loop_stage_audit` module; when absent the
    stage-context summary is simply omitted rather than failing the digest.
    """
    _clip = _clip_factory(include_text)

    plan: Dict[str, Any] = {}
    by_step: Dict[Any, Dict[str, Any]] = {}
    control: List[Dict[str, Any]] = []
    gates: List[Dict[str, Any]] = []
    recoveries: List[Dict[str, Any]] = []
    order: List[Any] = []
    n_think = n_act = 0
    # Calls that name no step at all. They are not steps and must never inflate
    # `executed_steps`, but dropping them silently would hide real work.
    unattributed_calls = 0
    fast_path = False
    ver: Set[str] = set()

    for e in events:
        t = str(e.get("type") or "")
        if e.get("ver"):
            ver.add(f"{e.get('ver')}@{e.get('br')}")
        if t.endswith(".tier"):
            plan["tier"] = e.get("tier")
        elif t.endswith(".intent"):
            plan["intent"] = e.get("intent")
        elif t.endswith(".fast_path"):
            # The single-cap shortcut. Recorded so a reader can tell "no plan was
            # made" apart from "planning produced nothing".
            fast_path = True
            plan["fast_path"] = True
            plan["fast_path_cap"] = e.get("cap")
        elif t.endswith(".plan"):
            plan["done_when"] = _clip(e.get("done_when"), 300)
            plan["steps"] = [{"id": s.get("id"), "title": s.get("title"),
                              "caps": s.get("caps"), "phases": s.get("phases") or [],
                              "success": _clip(s.get("success"), 200)}
                             for s in (e.get("steps") or [])]
        elif t.endswith("think_delta"):
            n_think += 1
        elif t.endswith(".tool_call"):
            sid_k = e.get("step_id")
            n_act += 1
            if norm_id(sid_k) is None:
                unattributed_calls += 1
                continue
            if sid_k not in by_step:
                by_step[sid_k] = {"step_id": sid_k, "calls": [], "title": ""}
                order.append(sid_k)
            by_step[sid_k]["calls"].append({"cycle": e.get("cycle"),
                                            "tool": e.get("tool"),
                                            "repeat": bool(e.get("repeat"))})
        elif t.endswith(".tool_done"):
            sid_k = e.get("step_id")
            calls = (by_step.get(sid_k) or {}).get("calls") or []
            for c in reversed(calls):
                if c.get("tool") == e.get("tool") and "ok" not in c:
                    c["ok"] = bool(e.get("ok"))
                    c["ms"] = e.get("elapsed_ms")
                    if e.get("cached"):
                        c["served_from"] = e.get("cached")
                    if not e.get("ok"):
                        # HEAD AND TAIL, not the first 160 characters. pytest,
                        # tracebacks and compilers all put the diagnosis at the
                        # END, so a leading clip recorded the banner and dropped
                        # the verdict - census 39's build-multifile stored
                        # "test session starts / platform linux …" for every
                        # failing run and nothing about what failed. See
                        # error_excerpt.
                        c["error"] = _error_excerpt.excerpt(
                            e.get("error") or e.get("preview"))
                    break
        elif t.endswith(".step_start"):
            sid_k = e.get("step_id")
            if norm_id(sid_k) is None:
                continue
            by_step.setdefault(sid_k, {"step_id": sid_k, "calls": []})
            if sid_k not in order:
                order.append(sid_k)
            by_step[sid_k]["title"] = e.get("title") or by_step[sid_k].get("title", "")
        elif t.endswith(".step_done"):
            sid_k = e.get("step_id")
            if norm_id(sid_k) is None:
                continue
            by_step.setdefault(sid_k, {"step_id": sid_k, "calls": []})["ok"] = bool(e.get("ok"))
            # A step that only ever reported DONE (no start, no calls) still ran;
            # without this it entered `by_step` but never `order`, so it was
            # dropped from `steps` and under-counted.
            if sid_k not in order:
                order.append(sid_k)
        elif t.endswith(".assess"):
            control.append({"after_step": e.get("after_step"),
                            "action": e.get("action"),
                            "goal_met": bool(e.get("goal_met")),
                            "assessment": _clip(e.get("assessment")),
                            "direction": _clip(e.get("direction")),
                            "inserted": [{"id": s.get("id"), "title": s.get("title"),
                                          "caps": s.get("caps")}
                                         for s in (e.get("steps") or [])]})
        elif t.endswith(".recovery_step"):
            # The `extra_step` failure-recovery path: a failed step is replaced
            # by an adjusted one that is pushed onto the queue and RUN. Third
            # insertion channel, and the one that produced run 15's unaccounted
            # step.
            _s = e.get("step") if isinstance(e.get("step"), dict) else {}
            recoveries.append({"from_step": e.get("from_step"),
                               "id": _s.get("id"), "title": _s.get("title"),
                               "adjusted": bool(e.get("adjusted")),
                               "caps": e.get("caps") or [],
                               "reason": _clip(e.get("reason"))})
        elif t.endswith(".gate"):
            # The completion gate's REAL shape. It was previously read for keys
            # (`verdict`/`reason`/`met`) the emitter has never written, so the
            # verdict — and the follow-up steps it appends — were both lost.
            rec = {"round": e.get("round"),
                   "complete": bool(e.get("complete")),
                   "missing": [_clip(m) for m in (e.get("missing") or [])],
                   "follow_up": [{"id": s.get("id"), "title": s.get("title")}
                                 for s in (e.get("follow_up") or [])]}
            gates.append(rec)
            plan["gate"] = rec

    steps = [by_step[k] for k in order if k in by_step]

    planned_ids = {norm_id(s.get("id")) for s in (plan.get("steps") or [])}
    planned_ids.discard(None)
    control_inserted = {norm_id(i.get("id")) for c in control for i in c.get("inserted") or []}
    control_inserted.discard(None)
    gate_inserted = {norm_id(i.get("id")) for g in gates for i in g.get("follow_up") or []}
    gate_inserted.discard(None)
    recovery_inserted = {norm_id(r.get("id")) for r in recoveries}
    recovery_inserted.discard(None)
    inserted_ids = control_inserted | gate_inserted | recovery_inserted
    executed_ids = {norm_id(s.get("step_id")) for s in steps}
    executed_ids.discard(None)

    # The fast path runs one synthetic step and never plans. It only applies when
    # planning genuinely never happened — the emitter returns immediately after
    # the shortcut, so a run cannot have both a plan and a fast-path step.
    fast_path_ids: Set[Optional[str]] = set()
    if fast_path and not (plan.get("steps") or []):
        fast_path_ids = set(executed_ids)

    unaccounted = sorted(executed_ids - planned_ids - inserted_ids - fast_path_ids,
                         key=lambda x: (len(x), x))

    warnings: List[str] = []
    stage_summary: Dict[str, Any] = {}
    stage_diffs: List[Dict[str, Any]] = []
    stage_recs = [e for e in events if str(e.get("type") or "") == "agent_loop.stage_context"]
    if stage_recs and stage_audit is not None:
        try:
            stage_summary = stage_audit.summarise(stage_recs)
            seen: Dict[str, Dict[str, Any]] = {}
            for r in stage_recs:
                key = f"{r.get('stage')}:{r.get('variant') or ''}"
                if key in seen:
                    d = stage_audit.diff_records(seen[key], r)
                    if d.get("identical_input") or d.get("changed") or d.get("runtime_changed"):
                        stage_diffs.append(d)
                seen[key] = r
        except Exception as _se:
            # The stage audit is a nice-to-have; it must never fail the digest.
            # But it must not fail SILENTLY either — a diagnostic that quietly
            # stops diagnosing is how a blind spot lasts a year.
            stage_summary, stage_diffs = {}, []
            warnings.append(f"stage-context summary failed: {type(_se).__name__}: {_se}")
    for d in stage_diffs:
        if d.get("identical_input"):
            warnings.append(
                f"stage {d.get('stage')} was given a BYTE-IDENTICAL prompt again "
                f"(cycle {d.get('from_cycle')} -> {d.get('to_cycle')}) — if its answer "
                f"changed, nothing in its input explains why")

    for c in control:
        for i in c.get("inserted") or []:
            warnings.append(
                f"controller INSERTED step {i.get('id')} '{i.get('title')}' "
                f"after step {c.get('after_step')} (caps={i.get('caps')})")
    for r in recoveries:
        warnings.append(
            f"failure-recovery REPLACED step {r.get('from_step')} with step "
            f"{r.get('id')} '{r.get('title')}' (caps={r.get('caps')}) — because: "
            f"{r.get('reason') or 'no reason recorded'}")
    for g in gates:
        for i in g.get("follow_up") or []:
            warnings.append(
                f"completion gate (round {g.get('round')}) APPENDED step "
                f"{i.get('id')} '{i.get('title')}' — the run was extended because: "
                f"{'; '.join(g.get('missing') or []) or 'no reason recorded'}")
    if fast_path_ids:
        warnings.append(
            "this run took the single-cap FAST PATH: no plan was made, so "
            "planned_steps=0 is expected here rather than a planning failure")
    if unaccounted:
        warnings.append(
            f"{len(unaccounted)} executed step(s) {unaccounted} are accounted for by "
            f"NOTHING — not the plan, not a controller insertion, not a gate "
            f"follow-up. Some producer is adding steps without recording them, and "
            f"every counter-based judgement of this run is unsafe until it is named")
    if unattributed_calls:
        warnings.append(
            f"{unattributed_calls} tool call(s) carried no step_id — counted in "
            f"tool_calls but belonging to no step")

    for s in steps:
        calls = s.get("calls") or []
        served = [c for c in calls if c.get("served_from")]
        if served:
            warnings.append(f"step {s.get('step_id')}: {len(served)} read(s) served from "
                            f"the artifact registry (re-read of an unchanged file)")
        seen_tools: Dict[str, int] = {}
        for c in calls:
            seen_tools[str(c.get("tool"))] = seen_tools.get(str(c.get("tool")), 0) + 1
        for tool, n in seen_tools.items():
            if n >= 3:
                warnings.append(f"step {s.get('step_id')}: {tool} called {n}x")
        for c in calls:
            if c.get("ok") is False:
                warnings.append(f"step {s.get('step_id')}: {c.get('tool')} FAILED — "
                                f"{c.get('error') or ''}")

    counters = {
        "events": len(events),
        "planned_steps": len(plan.get("steps") or []),
        "executed_steps": len(steps),
        # Both insertion channels. Kept as one number because that is what the
        # census and loop_run_history already read; the split is alongside it.
        "inserted_steps": len(inserted_ids - planned_ids),
        "controller_inserted_steps": len(control_inserted - planned_ids),
        "gate_inserted_steps": len(gate_inserted - planned_ids),
        "recovery_inserted_steps": len(recovery_inserted - planned_ids),
        "fast_path_steps": len(fast_path_ids),
        "unaccounted_steps": len(unaccounted),
        "unattributed_calls": unattributed_calls,
        "tool_calls": n_act,
        "think_deltas": n_think,
        "think_ratio": (round(n_think / max(1, n_think + n_act), 3)),
        "cycles_per_step": {str(s.get("step_id")): len(s.get("calls") or []) for s in steps},
        "code_version": sorted(ver),
    }
    counters["stage_calls"] = (stage_summary or {}).get("total_calls", 0)

    return {"plan": plan, "steps": steps, "control": control, "gates": gates,
            "recoveries": recoveries,
            "stages": (stage_summary or {}).get("stages", []),
            "stage_diffs": stage_diffs, "counters": counters, "warnings": warnings}


def accounting_is_consistent(counters: Dict[str, Any]) -> bool:
    """True when every executed step is attributable to a named producer.

    The identity the digest guarantees. A census can assert this per run instead
    of hand-checking that `planned + inserted` reaches `executed`.
    """
    return int(counters.get("unaccounted_steps") or 0) == 0
