"""
planner_core.py — pure, dependency-free planner GUARDS (single source of truth)
===============================================================================

The agentic-loop planner had two live incidents (see
documentation/postmortems/2026-08-06-agentic-loop-planner-drift.md):

  1. an unrelated user domain skill (a "cryptocurrency … DeFi" skill) dumped
     into the planner's AVAILABLE-SKILLS list hijacked the plan, and
  2. the planner was pinned to temp-0 / a fixed seed, so a bad plan was produced
     identically every time and the drift-guard retry could never escape it.

The three small, pure functions that guard against those — the skill filter, the
drift detector, and the planner-sampling policy — were defined inline in the
21k-line dag_workshop_capabilities.py, which imports the whole orchestrator and
therefore can't be imported by a unit test without booting the app. They are
extracted here (verbatim) so tests/test_planner_guards.py can lock their
behaviour cheaply and deterministically. dag_workshop_capabilities.py imports
them from here, so there is ONE implementation, and the tests guard the code
that actually runs.

Nothing here imports anything beyond the stdlib.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

# Generic scaffolding / task words stripped before comparing a goal to a plan,
# so "create a report" and "create a scanner" don't look alike on the shared
# verbs. Kept deliberately small and conservative.
PLAN_DRIFT_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "with", "on", "at",
    "from", "by", "as", "is", "are", "be", "it", "that", "this", "into", "using",
    "create", "make", "build", "write", "generate", "produce", "get", "find",
    "list", "show", "data", "file", "files", "report", "detailed", "real", "look",
    "like", "add", "use", "new", "set", "run", "then", "all", "some", "your",
}


def plan_words(text: str) -> set:
    """Content words of a goal/title — lowercased, stemmed crudely to catch
    plural/singular pairs, with generic task verbs removed so 'create a report'
    and 'create a scanner' don't look alike."""
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", str(text or "").lower()):
        if w in PLAN_DRIFT_STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def plan_drifted(goal: str, steps: List[Dict[str, Any]]) -> bool:
    """True when a plan appears to be about something other than the goal.

    Deliberately conservative — it must never reject a legitimate plan that
    simply uses different words. It fires only when the plan's titles share NO
    content word with the goal at all, which is what a wholesale substitution
    (a crypto plan for a Pokédex request) looks like. A goal too short to have
    any content words of its own is never judged."""
    gw = plan_words(goal)
    if len(gw) < 2 or not steps:
        return False
    tw = set()
    for s in steps:
        if isinstance(s, dict):
            tw |= plan_words(s.get("title"))
            tw |= plan_words(s.get("goal"))
    return bool(tw) and not (gw & tw)


def filter_skills_for_goal(skills: List[Dict[str, Any]], goal: str,
                           *, catalog: Optional[set] = None) -> List[Dict[str, Any]]:
    """Keep only skills the planner should actually SEE for THIS goal.

    The planner is handed the eligible-skill list as "AVAILABLE SKILLS" and it
    treats them as relevant context — so an unrelated user domain skill (e.g.
    "knowledgeable about cryptocurrency … Bitcoin … DeFi") listed here HIJACKS
    the plan: observed live, an "AI/ML report" goal produced a crypto plan lifted
    verbatim from a crypto skill's description. Structural/teaching skills
    (sys-* = how to use caps, fmt-* = output shaping) are domain-neutral and
    always kept; a USER domain skill is kept only when it is actually relevant to
    the goal — it shares a content word with the goal, or it teaches a cap that
    is in the run's catalog. Everything else is dropped so it can't derail the
    planner."""
    gw = plan_words(goal)
    catalog = catalog or set()
    kept: List[Dict[str, Any]] = []
    for s in skills:
        sid = str(s.get("id", ""))
        if sid.startswith("sys-") or sid.startswith("fmt-"):
            kept.append(s)
            continue
        # Relevance by goal-word overlap over the skill's name/description/tags…
        sw = plan_words(" ".join([
            str(s.get("name", "")), str(s.get("description", "")),
            " ".join(str(t) for t in (s.get("tags") or []))]))
        if gw & sw:
            kept.append(s)
            continue
        # …or the skill teaches a capability that is actually in the catalog.
        if catalog and any(c in catalog for c in (s.get("applies_to_caps") or [])):
            kept.append(s)
    return kept


# PLANNING is the exception to loop determinism. Verdicts / tool-selection /
# journal records want one reproducible answer, but PLAN GENERATION is creative
# decomposition — pinning it to a fixed seed means a bad or off-goal plan is
# produced IDENTICALLY every time, so the drift-guard retry re-derives the same
# broken plan and can never recover ("detected but not corrected"). Give the
# planner real sampling + a FRESH seed per call so it explores and retries
# genuinely differ. Tunable / disable-able via env.
PLANNER_NONDET = os.getenv("VERA_PLANNER_NONDET", "1").strip() not in ("0", "false", "no")
try:
    PLANNER_TEMP = float(os.getenv("VERA_PLANNER_TEMP", "0.4") or 0.4)
except Exception:
    PLANNER_TEMP = 0.4


def planner_sampling(base: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Sampling options for a PLANNER LLM call: real temperature + a fresh random
    seed each call (so a re-plan differs from the plan that drifted). Returns the
    base options unchanged when planner non-determinism is disabled."""
    if not PLANNER_NONDET:
        return base
    opts = dict(base or {})
    opts.setdefault("temperature", PLANNER_TEMP)
    opts.setdefault("top_p", 0.95)
    opts["seed"] = int(time.time_ns() & 0x7FFFFFFF)   # fresh per call → retries differ
    return opts
