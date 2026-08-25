"""Stage-context audit records for the agentic loop — Phase 0.

WHY. The loop drives 11 LLM stages (tier, intent, planner, master plan, split,
piecewise, executor, controller, verifier, gate, adjust) assembling 23 system
prompts from ~84k characters of text. Exactly ONE of them — the step executor,
via `agent_loop_v5.step_context` — ever exposed what it was actually given. For
the other ten there was no way to answer "what prompt did this stage get?",
"which shared blocks were in it?", "what context did it inherit, and from
where?", or "did its input change between cycle 3 and cycle 4?".

Diagnosing therefore meant reading the assembling code and simulating it by eye,
which is how a rule got added to one of two planner prompts and missed the
other, and how two instructions that contradicted each other both survived.
See documentation/PLAN-agentic-loop-prompt-architecture.md §1b.

WHAT THIS IS NOT. It changes no prompt text and gates nothing. It is the
instrument that lets the later phases prove they held behaviour.

PRIVACY. Records carry SHAs, sizes and provenance ids — not prompt bodies. The
roadmap explicitly excludes prompts and result bodies from activity records, and
a stage record is emitted on every cycle, so bodies would be both a leak and a
volume problem. `agent_loop_v5.step_context` keeps carrying the executor's full
prompt as it does today; that one is opt-in per session and already gated.

Pure (no I/O, no app imports) so it can be unit-tested directly.
"""

import hashlib
from typing import Any, Dict, List, Optional

# The stages, in the order a run reaches them. Used to order a trace and to spot
# a stage that never reported.
STAGE_ORDER = (
    "triage", "tier", "intent",
    "master_plan", "plan_split", "plan_piecewise", "planner",
    "executor", "verifier", "controller", "adjust", "gate",
)


def text_sha(text: str) -> str:
    """Short, stable digest of a prompt. Comparable across cycles and runs."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def stage_record(stage: str, *,
                 system: str = "",
                 prompt: str = "",
                 model: str = "",
                 role: str = "",
                 session_id: str = "",
                 stream_id: str = "",
                 cycle: Optional[int] = None,
                 step_id: Optional[Any] = None,
                 variant: str = "",
                 rule_ids: Optional[List[str]] = None,
                 runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build one stage-context record.

    `variant` distinguishes two prompts belonging to the SAME stage — the
    planner's full-schema and minimal-schema retry are the case that motivated
    it: a rule added to one and not the other held on some runs and not others,
    and nothing in the event stream showed which path a run took.

    `rule_ids` is empty in Phase 0; the rule registry arrives in Phase 1 and will
    populate it without changing this shape.
    """
    rec: Dict[str, Any] = {
        "stage": str(stage or "")[:40],
        "variant": str(variant or "")[:40],
        "system_sha": text_sha(system),
        "system_chars": len(system or ""),
        "prompt_sha": text_sha(prompt),
        "prompt_chars": len(prompt or ""),
        "total_chars": len(system or "") + len(prompt or ""),
        "model": str(model or "")[:80],
        "role": str(role or "")[:40],
        "rule_ids": list(rule_ids or []),
        "runtime": _clean_runtime(runtime),
    }
    if session_id:
        rec["session_id"] = str(session_id)
    if stream_id:
        rec["stream_id"] = str(stream_id)
    if cycle is not None:
        rec["cycle"] = cycle
    if step_id is not None:
        rec["step_id"] = step_id
    return rec


_RUNTIME_SCALARS = ("goal_chars", "ledger_steps", "pending_steps", "executed_steps",
                    "caps_count", "context_chars", "files_count")
_RUNTIME_LISTS = ("caps", "file_register", "prior_context_from", "skills")


def _clean_runtime(runtime: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep provenance and sizes; drop anything body-shaped.

    Deliberately an allowlist: a caller that hands in a whole ledger or a file's
    text should not silently turn every cycle's record into a copy of it.
    """
    out: Dict[str, Any] = {}
    if not isinstance(runtime, dict):
        return out
    for k in _RUNTIME_SCALARS:
        v = runtime.get(k)
        if isinstance(v, (int, float)):
            out[k] = v
    for k in _RUNTIME_LISTS:
        v = runtime.get(k)
        if isinstance(v, (list, tuple)):
            out[k] = [str(x)[:120] for x in list(v)[:40]]
    return out


def diff_records(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """What changed in a stage's INPUT between two cycles.

    The question this exists for: "why did the controller decide differently on
    cycle 4?" — answerable only by comparing what it was given, not what it said.
    A `changed: []` with differing decisions is itself the finding: the stage saw
    the same thing twice and answered differently.
    """
    before = before or {}
    after = after or {}
    changed: List[str] = []
    for k in ("system_sha", "prompt_sha", "model", "role", "variant"):
        if before.get(k) != after.get(k):
            changed.append(k)
    rb, ra = before.get("runtime") or {}, after.get("runtime") or {}
    runtime_changed = {}
    for k in set(rb) | set(ra):
        if rb.get(k) != ra.get(k):
            runtime_changed[k] = {"before": rb.get(k), "after": ra.get(k)}
    rules_b, rules_a = set(before.get("rule_ids") or []), set(after.get("rule_ids") or [])
    return {
        "stage": after.get("stage") or before.get("stage"),
        "from_cycle": before.get("cycle"),
        "to_cycle": after.get("cycle"),
        "changed": changed,
        "runtime_changed": runtime_changed,
        "rules_added": sorted(rules_a - rules_b),
        "rules_removed": sorted(rules_b - rules_a),
        "prompt_chars_delta": (after.get("total_chars") or 0) - (before.get("total_chars") or 0),
        "identical_input": (not changed and not runtime_changed
                            and rules_a == rules_b),
    }


def summarise(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-stage roll-up for a whole run: how often each stage ran, prompt sizes,
    and whether a stage was ever handed a byte-identical input twice."""
    by: Dict[str, Dict[str, Any]] = {}
    for r in records or []:
        if not isinstance(r, dict):
            continue
        key = r.get("stage") or "?"
        if r.get("variant"):
            key = f"{key}:{r['variant']}"
        e = by.setdefault(key, {"stage": key, "calls": 0, "chars": [], "shas": []})
        e["calls"] += 1
        e["chars"].append(r.get("total_chars") or 0)
        e["shas"].append(r.get("system_sha"))
    out = []
    for e in by.values():
        chars = e.pop("chars")
        shas = e.pop("shas")
        e["max_chars"] = max(chars) if chars else 0
        e["min_chars"] = min(chars) if chars else 0
        e["repeat_identical_system"] = len(shas) - len(set(shas))
        out.append(e)
    order = {s: i for i, s in enumerate(STAGE_ORDER)}
    out.sort(key=lambda e: (order.get(str(e["stage"]).split(":")[0], 99), e["stage"]))
    return {"stages": out, "total_calls": sum(e["calls"] for e in out)}
