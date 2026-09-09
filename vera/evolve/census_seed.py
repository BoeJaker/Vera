"""A census template IS a set of suite tasks sharing a tag.

Step 3 of flattening the census harness into the Loop Lab suite. The two
systems were never really different things:

    the census                        the suite
    ----------------------------      ------------------------------------
    a template of goals               a set of tasks sharing a tag
    run every goal in order           evolve.suite.run(tag=...)
    wait for a free box               _suite_wait_for_free  (landed, step 2)
    check the files it produced       _run_checks -> census.quality (landed)
    one JSONL per run                 one scoreboard per suite run
    a per-template timeline           evolve.suites filtered by tag

What was actually missing was a translation, not a second runner. This module
is that translation, and nothing else: template dict in, task records out.

PARITY IS THE POINT. Every historical census number was produced by
`dag.agent_loop_v7` called with the goal and nothing else, so a seeded task has
to reach the same engine the same way or the migration silently rebases the
timeline. Two facts, both measured on the running instance (2026-09-07), make
that work:

  * `planning` is the only profile whose engine is v7 (`_ENGINE_CAP`), so it is
    the only profile that can stand in for the harness's direct v7 call.
  * `loops.run` passes the caller's explicit arguments and drops the profile
    BODY for a v7 engine, so a seeded task gets a bare v7 run - which is what
    the harness did. (That asymmetry is deliberate and documented in
    `vera/dag/engine_params.py`; if the profile body is ever switched on for
    v7, seeded census tasks stop being comparable with runs 1-41 and the
    template needs a new name.)

Pure: dicts in, dicts out. No Redis, no engine, no clock.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

#: The harness's own ceiling (`WALL_CAP_S` in run_census.py). Half the default
#: goal set sits close to it, so it is the single most comparison-sensitive
#: number here - changing it changes what the census measures.
DEFAULT_WALL_CAP_S = 1800

#: The only profile whose engine is v7. Not a preference - a lookup.
CENSUS_PROFILE = "planning"

#: Every seeded task carries this, so the census set is one filter away from
#: the hand-written Loop Lab tasks it now lives beside.
CENSUS_TAG = "census"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Check keys the scorer can actually assert on (`_CENSUS_CHECK_KINDS` in
#: evolve_capabilities, plus the suite's own `type`). A check with none of
#: these asserts nothing, and a check that asserts nothing is worse than no
#: check: it inflates the denominator and reads as coverage.
ASSERTABLE = ("exists", "min_bytes", "contains", "absent", "any_of", "all_of",
              "regex", "answer_contains", "answer_regex", "answer_min_words",
              "type")


def slug(name: str) -> str:
    return _SLUG_RE.sub("-", str(name or "").strip().lower()).strip("-")


def tag_for(template_name: str) -> str:
    """The tag that selects exactly this template's tasks."""
    return "%s-%s" % (CENSUS_TAG, slug(template_name) or "unnamed")


def task_id_for(template_name: str, goal_id: str) -> str:
    return "%s-%s" % (tag_for(template_name), slug(goal_id) or "goal")


def check_is_assertable(check: Dict[str, Any]) -> bool:
    return any(k in (check or {}) for k in ASSERTABLE)


def profile_for(goal: Dict[str, Any], template_profile: Any = "") -> str:
    """Which loop profile runs this goal. Goal, then template, then planning.

    Same precedence shape as a wall cap, and the same reason: the most specific
    declaration wins. A SPECIALIST census is the case that needs it - one goal
    per specialist, each run by the profile that specialist actually uses, which
    a single template-wide profile cannot express.

    PARITY WARNING, and it is the whole reason this defaulted to a constant:
    `planning` is the only profile whose engine is v7, and every historical
    census number came from a bare v7 call. A template that sets anything else
    is measuring a DIFFERENT ENGINE and its numbers are not points on the
    default series' timeline. comparable() says so; this function does not stop
    you, because measuring the specialists is a legitimate thing to want.
    """
    for v in ((goal or {}).get("profile") if isinstance(goal, dict) else None,
              template_profile, CENSUS_PROFILE):
        name = str(v or "").strip()
        if name:
            return name
    return CENSUS_PROFILE


def goal_to_task(goal: Dict[str, Any], template_name: str,
                 wall_cap_s: int = DEFAULT_WALL_CAP_S,
                 model: str = "", profile: str = "") -> Dict[str, Any]:
    """One census goal as one suite task record."""
    gid = str(goal.get("id") or "")
    task: Dict[str, Any] = {
        "id": task_id_for(template_name, gid),
        "label": "Census — %s" % (goal.get("label") or gid),
        "type": "loop",
        "profile": profile_for(goal, profile),
        "goal": str(goal.get("goal") or ""),
        # Empty on purpose: the harness restricts nothing, and a restriction
        # here would be an extra variable in a comparison that exists to have
        # none.
        "allowed_caps": "",
        "timeout_s": int(wall_cap_s or DEFAULT_WALL_CAP_S),
        "enabled": True,
        "checks": list(goal.get("checks") or []),
        "tags": [CENSUS_TAG, tag_for(template_name)],
        # The census's own axes, carried through so the panel can still group by
        # them. They are not suite fields; nothing breaks if they are ignored.
        "census": {"template": template_name, "goal_id": gid,
                   "intent": goal.get("intent", ""), "tier": goal.get("tier", ""),
                   "output": goal.get("output", ""), "shape": goal.get("shape", "")},
    }
    if goal.get("rubric"):
        task["rubric"] = str(goal["rubric"])
    # A model pin rides as an override so it reaches loops.run as an explicit
    # caller argument - the only kind a v7 engine still receives.
    m = str(model or goal.get("model") or "").strip()
    if m:
        task["overrides"] = {"model": m}
    return task


def template_to_tasks(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = str((template or {}).get("name") or "")
    cap = int((template or {}).get("wall_cap_s") or DEFAULT_WALL_CAP_S)
    model = str((template or {}).get("model") or "")
    prof = str((template or {}).get("profile") or "")
    return [goal_to_task(g, name, cap, model, prof)
            for g in ((template or {}).get("goals") or [])]


def problems(template: Optional[Dict[str, Any]]) -> List[str]:
    """Everything wrong with this template, in plain words.

    Checked BEFORE anything is seeded, because the cost of a bad template is
    not a stack trace - it is half a day of GPU spent producing numbers that
    turn out not to mean anything.
    """
    out: List[str] = []
    if not isinstance(template, dict):
        return ["not a template object"]
    if not str(template.get("name") or "").strip():
        out.append("template has no name")
    goals = template.get("goals")
    if not isinstance(goals, list) or not goals:
        out.append("template has no goals")
        return out
    seen = set()
    for i, g in enumerate(goals):
        where = "goal %d" % (i + 1)
        if not isinstance(g, dict):
            out.append("%s is not an object" % where)
            continue
        gid = str(g.get("id") or "").strip()
        if not gid:
            out.append("%s has no id" % where)
        elif gid in seen:
            out.append("duplicate goal id %r — its runs would overwrite each "
                       "other's task" % gid)
        else:
            seen.add(gid)
        if not str(g.get("goal") or "").strip():
            out.append("%s (%s) has no goal text" % (where, gid or "?"))
        checks = g.get("checks")
        if not isinstance(checks, list) or not checks:
            out.append("%s (%s) has no checks — it can only ever report what "
                       "the run SAID" % (where, gid or "?"))
            continue
        for j, c in enumerate(checks):
            if not isinstance(c, dict) or not check_is_assertable(c):
                out.append("%s (%s) check %d asserts nothing"
                           % (where, gid or "?", j + 1))
    cap = template.get("wall_cap_s", DEFAULT_WALL_CAP_S)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = -1
    if cap < 60:
        out.append("wall_cap_s must be at least 60s")
    return out


def comparable(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> List[str]:
    """Why two templates' numbers should NOT be compared, or [] if they may be.

    The census's whole value is the timeline, and a timeline is only a timeline
    while the questions stay the same. Editing a template is allowed; comparing
    across an edit silently is not.
    """
    a, b = a or {}, b or {}
    out: List[str] = []
    ga = [str(g.get("id") or "") for g in (a.get("goals") or [])]
    gb = [str(g.get("id") or "") for g in (b.get("goals") or [])]
    # Membership, not order: which questions are asked is what has to hold
    # still, not the sequence they are asked in.
    added, gone = sorted(set(gb) - set(ga)), sorted(set(ga) - set(gb))
    if added:
        out.append("goals added: %s" % ", ".join(added))
    if gone:
        out.append("goals removed: %s" % ", ".join(gone))
    # A different profile is a different ENGINE (planning is the only v7), so
    # the runs are not points on one timeline however similar the goals look.
    for gid in sorted(x for x in (set(ga) & set(gb)) if x):
        ja = next(g for g in a["goals"] if str(g.get("id") or "") == gid)
        jb = next(g for g in b["goals"] if str(g.get("id") or "") == gid)
        pa = profile_for(ja, str(a.get("profile") or ""))
        pb = profile_for(jb, str(b.get("profile") or ""))
        if pa != pb:
            out.append("%s runs under a different loop profile (%s vs %s) - a "
                       "different engine, so these are not the same experiment"
                       % (gid, pa, pb))
    if str(a.get("model") or "") != str(b.get("model") or ""):
        out.append("different model (%s vs %s)"
                   % (a.get("model") or "routing default",
                      b.get("model") or "routing default"))
    ca = int(a.get("wall_cap_s") or DEFAULT_WALL_CAP_S)
    cb = int(b.get("wall_cap_s") or DEFAULT_WALL_CAP_S)
    if ca != cb:
        out.append("different wall cap (%ds vs %ds)" % (ca, cb))
    return out
