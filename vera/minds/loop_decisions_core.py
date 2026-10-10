"""The agent loop's typed decisions as ledger rows, and the outcomes a run's end can judge
(four-minds FM-0.2a, ROADMAP §5.1).

Questions, recorded where the v6/v7 runner already knows the answer (no new model call):
- `loop.controller_action`: after a step, the controller chose continue | replan | insert | stop;
- `loop.step_verify`: a step's result met its success criterion, or did not;
- `loop.tier` / `loop.intent`: how the run was classed (FM-0.2b);
- `loop.step_done`: a rule ended a step early (sources in hand, a repeat, the browser done...), FM-0.2b;
- `loop.prestep_needed`: the pre-step pass found gaps to gather first, or none (FM-0.2b).

Outcomes, judged at run end from what the run itself saw (deliberately modest; FM-11
calibration reports how often each fires):
- a `not_met` verdict was right when a later attempt of the same step was met (the retry
  changed something and passed): strong;
- a `continue` was right when the next step to be verified was met, wrong when it was not: medium;
- a `stop` was right when the final gate found nothing missing, wrong when it named gaps: strong;
- a rule's `end` (step_done) was right when that attempt was then verified met, wrong when not: medium;
- an intent was right or wrong by the KIND of work the run then did (`families`: how many executed
  steps used a research cap, how many authored something), weak, as the roadmap rates intent: build is
  wrong when only research ran and nothing was authored, right when something was authored with no
  research and the gate found nothing missing; research mirrors it; mixed is right when both kinds ran
  (and the gate passed), wrong when only one did. action, and a shadow intent, get none here;
- a `not_needed` prestep was right when the step's first attempt was verified met: medium. One-sided:
  a failed first attempt cannot be pinned on a missing gap, and `needed` has no outcome here.
Nothing else gets an outcome here; the ledger's sweeper (a later slice) marks runs that never
finished as interrupted.

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

try:
    from Vera.vera.minds import ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.minds import ledger_core as LC  # type: ignore

CONTROLLER_Q = "loop.controller_action"
VERIFY_Q = "loop.step_verify"
TIER_Q = "loop.tier"
INTENT_Q = "loop.intent"
TIER_LABELS = ["single", "simple", "complex", "strategic"]
INTENT_LABELS = ["build", "research", "action", "mixed"]
# The steps a run of each tier should take, for the tier's outcome (ROADMAP §5.1: "executed step
# count inside the tier's band"). The code has no bands of its own; these are the measuring rule, v0.
TIER_BANDS = {"simple": (1, 3), "complex": (2, 12), "strategic": (3, 10 ** 6)}
CONTROLLER_LABELS = ["continue", "replan", "insert", "stop"]
STEP_DONE_Q = "loop.step_done"
PRESTEP_Q = "loop.prestep_needed"
STEP_DONE_LABELS = ["end", "continue"]          # only a rule's `end` is recorded (one row per firing)
PRESTEP_LABELS = ["needed", "not_needed"]
VERIFY_LABELS = ["met", "not_met"]

_ORIGIN_BY_CALLER = {"census": "census", "claude": "delegate", "codex": "delegate", "mcp": "delegate",
                     "schedule": "schedule", "scheduler": "schedule", "dream": "background",
                     "background": "background"}


def origin_for(caller_kind: Any, explicit: Any = "") -> str:
    """The Decision v0 origin of a run: what the caller declared (decision_origin=, e.g. the
    census harness says "census") when it is a v0 origin, else derived from the caller kind
    (a person by default)."""
    e = str(explicit or "").strip().lower()
    if e in LC.ORIGINS:
        return e
    return _ORIGIN_BY_CALLER.get(str(caller_kind or "").strip().lower(), "user")


def _short(x: Any, n: int = 120) -> str:
    return str(x or "")[:n]


def controller_decision(sid: str, step_id: Any, action: str, ctrl: Dict[str, Any], *,
                        model: str = "", minds_digest: str = "default",
                        origin: str = "user", by: str = "llm",
                        student: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`by` says who decided (see verify_by; "fallback" = the call failed and the run continued)."""
    action = action if action in CONTROLLER_LABELS else "continue"
    decider, impl, mode = verify_by(by, model or "controller", student)
    steps = ctrl.get("steps") or []
    state = {"after_step": step_id, "goal_met": bool(ctrl.get("goal_met")),
             "alignment": _short(ctrl.get("goal_alignment"), 60), "new_steps": len(steps)}
    return LC.make_decision(
        CONTROLLER_Q, action, origin=origin, surface="loop", qtype="choice",
        labels=CONTROLLER_LABELS, state=state,
        state_ref={"kind": "loop_step", "id": f"{_short(sid, 80)}:{step_id}"},
        decider=decider, implementation=impl, mode=mode,
        p=(student or {}).get("p") if decider == "student" else None,
        minds_digest=minds_digest or "default", run_id=_short(sid, 80), session_id=_short(sid, 80),
        detail={"after_step": step_id, "goal_met": state["goal_met"], "new_steps": len(steps),
                "by": _short(by, 80)})


def verify_by(by: Any, model: str = "", student: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    """(decider, implementation, mode) for a verdict from `by`: "llm" (the big model), "student" (a student
    answered under student_first), "fallback" (the judge failed and the step's own ok flag stood) or
    "rule:<name>" (a deterministic check - a parsed authored file, a test run, a missing file...)."""
    by = str(by or "llm")
    if by == "student":
        return "student", _short((student or {}).get("impl"), 80) or "student", "student_first"
    if by == "llm":
        return "llm", _short(model, 80) or "verifier", "llm_only"
    if by == "fallback":
        return "rule", "fallback:ok_flag", "rule"
    return "rule", _short(by, 80), "rule"


def verify_decision(sid: str, step_id: Any, met: Any, attempt: int, *, model: str = "",
                    minds_digest: str = "default", origin: str = "user", by: str = "llm",
                    student: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """None when verification did not run (met is None). `by` says who decided (see verify_by); a verdict a
    student gave carries its p."""
    if met is None:
        return None
    answer = "met" if met else "not_met"
    decider, impl, mode = verify_by(by, model, student)
    return LC.make_decision(
        VERIFY_Q, answer, origin=origin, surface="loop", qtype="binary", labels=VERIFY_LABELS,
        state={"step": step_id, "attempt": int(attempt)},
        state_ref={"kind": "loop_step_attempt", "id": f"{_short(sid, 80)}:{step_id}:a{int(attempt)}"},
        decider=decider, implementation=impl, mode=mode,
        p=(student or {}).get("p") if decider == "student" else None,
        minds_digest=minds_digest or "default", run_id=_short(sid, 80), session_id=_short(sid, 80),
        detail={"step": step_id, "attempt": int(attempt), "by": _short(by, 80)})


def tier_decision(sid: str, tier: str, info: Dict[str, Any], *, model: str = "",
                  minds_digest: str = "default", origin: str = "user") -> Dict[str, Any]:
    tier = tier if tier in TIER_LABELS else "simple"
    heur, llm = _short(info.get("heuristic"), 20), _short(info.get("llm"), 20)
    return LC.make_decision(
        TIER_Q, tier, origin=origin, surface="loop", qtype="choice", labels=TIER_LABELS,
        state={"heuristic": heur, "llm": llm, "suggested": _short(info.get("suggested"), 20)},
        state_ref={"kind": "loop_run", "id": _short(sid, 80)},
        decider="llm" if llm else "rule", implementation=_short(model, 80) or "tier",
        mode="llm_only" if llm else "rule", minds_digest=minds_digest or "default",
        run_id=_short(sid, 80), session_id=_short(sid, 80),
        detail={"heuristic": heur, "llm": llm,
                "escalation_suppressed": bool(info.get("escalation_suppressed"))})


def intent_decision(sid: str, intent: str, info: Dict[str, Any], *, model: str = "",
                    minds_digest: str = "default", origin: str = "user") -> Dict[str, Any]:
    """Recorded for imitation only: the roadmap rates intent outcomes weak, so none is judged here."""
    intent = intent if intent in INTENT_LABELS else "mixed"
    heur, llm = _short(info.get("heuristic"), 20), _short(info.get("llm"), 20)
    return LC.make_decision(
        INTENT_Q, intent, origin=origin, surface="loop", qtype="choice", labels=INTENT_LABELS,
        state={"heuristic": heur, "llm": llm}, state_ref={"kind": "loop_run", "id": _short(sid, 80)},
        decider="llm" if llm else "rule", implementation=_short(model, 80) or "intent",
        mode="llm_only" if llm else "rule", minds_digest=minds_digest or "default",
        run_id=_short(sid, 80), session_id=_short(sid, 80), detail={"heuristic": heur, "llm": llm})


def step_done_decision(sid: str, step_id: Any, done_by: str, attempt: int, *,
                       minds_digest: str = "default", origin: str = "user") -> Optional[Dict[str, Any]]:
    """A rule ended this step attempt early; `done_by` names the rule. None when no rule did."""
    rule = _short(done_by, 40)
    if not rule:
        return None
    return LC.make_decision(
        STEP_DONE_Q, "end", origin=origin, surface="loop", qtype="binary", labels=STEP_DONE_LABELS,
        state={"step": step_id, "attempt": int(attempt), "rule": rule},
        state_ref={"kind": "loop_step_attempt", "id": f"{_short(sid, 80)}:{step_id}:a{int(attempt)}"},
        decider="rule", implementation=rule, mode="rule", minds_digest=minds_digest or "default",
        run_id=_short(sid, 80), session_id=_short(sid, 80),
        detail={"step": step_id, "attempt": int(attempt), "rule": rule})


def prestep_decision(sid: str, step_id: Any, gaps: List[Any], known_steps: int, *, model: str = "",
                     minds_digest: str = "default", origin: str = "user") -> Dict[str, Any]:
    n = len([g for g in (gaps or []) if str(g).strip()])
    return LC.make_decision(
        PRESTEP_Q, "needed" if n else "not_needed", origin=origin, surface="loop", qtype="binary",
        labels=PRESTEP_LABELS, state={"step": step_id, "known_steps": int(known_steps)},
        state_ref={"kind": "loop_step", "id": f"{_short(sid, 80)}:{step_id}"},
        decider="llm", implementation=_short(model, 80) or "prestep", mode="llm_only",
        minds_digest=minds_digest or "default", run_id=_short(sid, 80), session_id=_short(sid, 80),
        detail={"step": step_id, "gaps": n, "known_steps": int(known_steps)})


def intent_shadow_decision(sid: str, rec: Dict[str, Any], *, minds_digest: str = "default",
                           origin: str = "user", for_id: str = "") -> Optional[Dict[str, Any]]:
    """The zero-shot model's intent for the same goal, recorded as a SHADOW of the live
    loop.intent decision: same question, same state (heuristic + llm), same run, its own
    confidence and margin, and whether it agrees. A second, independent labeller of the same
    input is what a student for this question is compared against. None without an answer."""
    zs = rec.get("zeroshot") if isinstance(rec, dict) else None
    if zs not in INTENT_LABELS:
        return None
    heur, llm = _short(rec.get("heuristic"), 20), _short(rec.get("llm"), 20)

    def _num(v):
        try:
            return round(float(v), 3)
        except (TypeError, ValueError):
            return None
    scores = {str(k)[:12]: _num(v) for k, v in list((rec.get("scores") or {}).items())[:6]}
    return LC.make_decision(
        INTENT_Q, zs, origin=origin, surface="loop", qtype="choice", labels=INTENT_LABELS,
        state={"heuristic": heur, "llm": llm}, state_ref={"kind": "loop_run", "id": _short(sid, 80)},
        decider="student", implementation="nlp.zeroshot", mode="shadow",
        p=_num(rec.get("confidence")), margin=_num(rec.get("margin")),
        minds_digest=minds_digest or "default", run_id=_short(sid, 80), session_id=_short(sid, 80),
        detail={"used": _short(rec.get("used"), 20), "agrees_used": rec.get("agrees_used"),
                "agrees_llm": rec.get("agrees_llm"), "scores": scores,
                # the live decision it shadows - every other shadow carries it; without it the report, the
                # calibration and the night scorer cannot pair the two (decide.calibration, 2026-10-10)
                **({"for": _short(for_id, 80)} if for_id else {})})


def tier_outcome(decision: Dict[str, Any], steps_run: Optional[int], fast_path: Optional[bool],
                 gate: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """single: right when the fast path answered it, wrong when it fell through to planning.
    Other tiers: right when the executed steps sit in the tier's band and the final gate found
    nothing missing. None when the run said too little to judge."""
    tier = decision["answer"]
    if tier == "single":
        if fast_path is None:
            return None
        return LC.make_outcome(decision["decision_id"], "correct" if fast_path else "wrong",
                               "run.fast_path", "medium", detail={"steps": steps_run})
    band = TIER_BANDS.get(tier)
    if band is None or steps_run is None:
        return None
    missing = [m for m in ((gate or {}).get("missing") or []) if str(m).strip()]
    ok = band[0] <= int(steps_run) <= band[1] and not missing
    return LC.make_outcome(decision["decision_id"], "correct" if ok else "wrong", "run.tier_band", "medium",
                           detail={"steps": int(steps_run), "band": list(band), "missing": len(missing)})


def intent_outcome(decision: Dict[str, Any], families: Optional[Dict[str, Any]],
                   gate: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Judge a live intent by the kind of work the run did (see the module notes). None when the
    run said too little, for action, and for a shadow (the run's shape followed the live intent,
    so judging the shadow on it would be biased)."""
    if not families or decision.get("mode") == "shadow":
        return None
    try:
        res, bld = int(families.get("research") or 0), int(families.get("build") or 0)
    except (TypeError, ValueError):
        return None
    if res == 0 and bld == 0:
        return None
    passed = not [m for m in ((gate or {}).get("missing") or []) if str(m).strip()] and bool(gate)
    intent, label = decision["answer"], None
    if intent == "build":
        label = "wrong" if (res and not bld) else ("correct" if (bld and not res and passed) else None)
    elif intent == "research":
        label = "wrong" if (bld and not res) else ("correct" if (res and passed) else None)
    elif intent == "mixed":
        label = "wrong" if not (res and bld) else ("correct" if passed else None)
    if label is None:
        return None
    return LC.make_outcome(decision["decision_id"], label, "run.cap_families", "weak",
                           detail={"research_steps": res, "build_steps": bld, "gate_passed": passed})


def run_outcomes(decisions: List[Dict[str, Any]], gate: Optional[Dict[str, Any]],
                 steps_run: Optional[int] = None, fast_path: Optional[bool] = None,
                 families: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Outcome v0 rows for the decisions of one run, in the order they were made."""
    out: List[Dict[str, Any]] = []
    for d in decisions:
        if d["question_id"] == TIER_Q:
            o = tier_outcome(d, steps_run, fast_path, gate)
            if o is not None:
                out.append(o)
        elif d["question_id"] == INTENT_Q:
            o = intent_outcome(d, families, gate)
            if o is not None:
                out.append(o)
    verifies = [(i, d) for i, d in enumerate(decisions) if d["question_id"] == VERIFY_Q]

    # a not_met verdict that a later attempt of the same step turned into met
    for i, d in verifies:
        if d["answer"] != "not_met":
            continue
        step = d["detail"].get("step")
        later_met = any(j > i and e["detail"].get("step") == step and e["answer"] == "met" for j, e in verifies)
        if later_met:
            out.append(LC.make_outcome(d["decision_id"], "correct", "run.retry_passed", "strong",
                                       detail={"step": step}))

    # a rule's step end: that attempt's verdict
    for d in decisions:
        if d["question_id"] != STEP_DONE_Q:
            continue
        step, att = d["detail"].get("step"), d["detail"].get("attempt")
        v = next((e for _j, e in verifies
                  if e["detail"].get("step") == step and e["detail"].get("attempt") == att), None)
        if v is not None:
            out.append(LC.make_outcome(d["decision_id"], "correct" if v["answer"] == "met" else "wrong",
                                       "run.step_verify", "medium",
                                       detail={"step": step, "attempt": att, "rule": d["detail"].get("rule")}))

    # no gaps to gather, and the step's first attempt was met
    for i, d in enumerate(decisions):
        if d["question_id"] != PRESTEP_Q or d["answer"] != "not_needed":
            continue
        step = d["detail"].get("step")
        v = next((e for j, e in verifies if j > i and e["detail"].get("step") == step), None)
        if v is not None and v["answer"] == "met":
            out.append(LC.make_outcome(d["decision_id"], "correct", "run.first_attempt_met", "medium",
                                       detail={"step": step}))

    for i, d in enumerate(decisions):
        if d["question_id"] != CONTROLLER_Q:
            continue
        if d["answer"] == "continue":
            after = d["detail"].get("after_step")
            nxt = next((e for j, e in verifies if j > i and e["detail"].get("step") != after), None)
            if nxt is not None:
                ok = nxt["answer"] == "met"
                out.append(LC.make_outcome(d["decision_id"], "correct" if ok else "wrong",
                                           "run.next_step_verify", "medium",
                                           detail={"next_step": nxt["detail"].get("step")}))
        elif d["answer"] == "stop" and gate:
            missing = [m for m in (gate.get("missing") or []) if str(m).strip()]
            complete = bool(gate.get("complete")) and not missing
            out.append(LC.make_outcome(d["decision_id"], "correct" if complete else "wrong",
                                       "run.gate_complete" if complete else "run.gate_missing", "strong",
                                       detail={"missing": len(missing)}))
    return out


def run_digest(decisions: List[Dict[str, Any]]) -> str:
    """A short fingerprint of a run's decisions (for logs and tests)."""
    raw = json.dumps([(d["question_id"], d["answer"]) for d in decisions])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
