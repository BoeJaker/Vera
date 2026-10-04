#!/usr/bin/env python3
"""Operator census - STRICTLY SERIAL browser runs against the live instance.

The sibling of run_census.py, for operator.run. It exists because the browser was
the most expensive part of the loop and the least measured: across loop-census
runs 12 and 13, every goal with an operator.run step consumed its full 25-minute
cap while every goal without one finished inside seven minutes.

Same discipline as the loop census: one run at a time (the GPU gate is capacity
1), wait for a free box before each goal so this never competes with a person or
another agent, cap each run, and record a timeout as data rather than losing it.

Each goal's record comes from operator.trace - so every row here can be opened
back into its own step-by-step, which is the reason operator observability had to
land first.
"""
import argparse, json, os, subprocess, sys, time

BASE = os.environ.get("CENSUS_BASE", "").strip().rstrip("/") or "https://localhost:8999"
CODE = os.path.dirname(os.path.abspath(__file__))       # this harness's code (the repo)
# Its DATA - archives, logs, state, templates, stall dumps - is not the code folder:
HERE = os.path.expanduser(os.environ.get("VERA_CENSUS_DIR", "").strip() or "~/loop-census")
OUT = os.path.join(HERE, "operator-census.jsonl")
LOG = os.path.join(HERE, "operator-census.log")
GOALS = os.path.join(HERE, "operator-goals.json")
WALL_CAP_S = 900           # 15 min per browser run
POLL_S = 10
# See run_census.py: a hardcoded session id made every run look like the same
# actor, so Loop Lab could not show who operated one. Overridable, and the
# provenance is recorded on every row. `caller_session` is the key that joins a
# run to a transcript already ingested by ide.claude_sessions.*.
SESSION_ID = (os.getenv("VERA_CENSUS_SESSION_ID") or "operator-census")
OPERATOR = os.getenv("VERA_CENSUS_OPERATOR") or ""
SKILL = os.getenv("VERA_CENSUS_SKILL") or ""
TOOL = os.getenv("VERA_CENSUS_TOOL") or ""


HELPERS_FILE = os.path.join(HERE, "helpers.json")


def load_helpers():
    """Helper scripts the operator wrote to drive or observe this run.

    Shared manifest with the loop census — see run_census.load_helpers for why
    ephemeral /tmp tooling has to be recorded. Best-effort: a missing manifest
    yields no helpers rather than failing the run.
    """
    try:
        with open(HELPERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    out = []
    for h in (data.get("helpers") if isinstance(data, dict) else data) or []:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or h.get("path") or "").strip()
        if name:
            out.append({"name": name,
                        "purpose": " ".join(str(h.get("purpose") or "").split())[:400]})
    return out


def provenance():
    """Who ran this operator census, what drove them, and what tooling they wrote."""
    return {"caller_session": SESSION_ID, "operator": OPERATOR,
            "skill": SKILL, "tool": TOOL, "helpers": load_helpers()}

# ── Templates ────────────────────────────────────────────────────────────────
# The loop census learned this first: a run's numbers are meaningless without
# knowing WHICH goals were asked, so the template is named on every row. Same
# shape here, in its own directory because operator goals are a different schema
# (kind/panel_id/url/max_steps rather than intent/tier/output/shape).
#
# operator-templates/<name>.json:
#   {"name": ..., "description": ..., "wall_cap_s": <optional default>,
#    "goals": [ {id, kind, url|panel_id, max_steps, goal}, ... ]}
#
# operator-goals.json remains the fallback so an older invocation still works.
OP_TEMPLATES_DIR = os.path.join(HERE, "operator-templates")


def load_template(name):
    """Return (template_name, goals, wall_cap_default). Falls back to the flat file."""
    path = os.path.join(OP_TEMPLATES_DIR, "%s.json" % name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            t = json.load(fh)
        if isinstance(t, list):
            return name, t, 0
        return (t.get("name") or name, t.get("goals") or [],
                int(t.get("wall_cap_s") or 0))
    if name in ("default", ""):
        with open(GOALS, encoding="utf-8") as fh:
            return "default", json.load(fh), 0
    raise SystemExit("no such operator template: %s (looked in %s)"
                     % (name, OP_TEMPLATES_DIR))


def list_templates():
    out = []
    if os.path.isdir(OP_TEMPLATES_DIR):
        for fn in sorted(os.listdir(OP_TEMPLATES_DIR)):
            if fn.endswith(".json"):
                out.append(fn[:-5])
    if "default" not in out and os.path.exists(GOALS):
        out.append("default")
    return out


def goal_wall_cap(g, template_default=0):
    """Per-goal cap, most specific first. A browser goal that only reads a
    static page has no business holding the box for fifteen minutes, and one
    that drives a slow panel may legitimately need more."""
    for v in (g.get("wall_cap_s"), template_default, WALL_CAP_S):
        try:
            n = int(v or 0)
        except Exception:
            n = 0
        if n > 0:
            return n
    return WALL_CAP_S


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def curl(args, timeout=60):
    try:
        r = subprocess.run(["curl", "-sk", "--max-time", str(timeout)] + args,
                           capture_output=True, text=True, timeout=timeout + 20)
        return r.stdout
    except Exception as e:
        log("  curl failed: %s" % e)
        return ""


def mcp(name, arguments, timeout=60):
    body = json.dumps({"name": name, "arguments": arguments,
                       "caller_kind": "claude", "session_id": SESSION_ID})
    out = curl([BASE + "/mcp/call", "-X", "POST",
                "-H", "Content-Type: application/json", "-d", body], timeout=timeout)
    try:
        return json.loads(out).get("content", {})
    except Exception:
        return {"error": "unparseable response", "raw": out[:400]}


def box_busy():
    """Never start while anything else holds the GPU or a loop is running - the
    same courtesy run_census.py extends, for the same reason: a timing taken
    while something else was generating is contention-confounded and worthless."""
    try:
        s = json.loads(curl([BASE + "/workshop/agent_loop/sessions?status=running"]))
        if (s.get("sessions") or []):
            return "a loop is running"
    except Exception:
        pass
    g = mcp("ollama.gate.status", {})
    for n in (g.get("nodes") or []):
        if n.get("has_gpu") and int(n.get("held") or 0) > 0:
            return "gate held by %s" % (n.get("owners") or [])
    return ""


def wait_for_free_box(limit_s=1800):
    t0 = time.time()
    while time.time() - t0 < limit_s:
        why = box_busy()
        if not why:
            return True
        log("  waiting: %s" % why)
        time.sleep(20)
    return False


def run_goal(g, template="", wall_cap_default=0):
    wall_cap = goal_wall_cap(g, wall_cap_default)
    log("== %s ==%s" % (g["id"],
                        "" if wall_cap == WALL_CAP_S else "  [cap %ds]" % wall_cap))
    if not wait_for_free_box():
        log("  box never freed - skipping")
        return None
    t0 = time.time()
    res = mcp("operator.run", {"goal": g["goal"], "url": g.get("url", ""),
                               "kind": g.get("kind", ""), "max_steps": g.get("max_steps", 12),
                               "dry_run": bool(g.get("dry_run", False))},
              timeout=wall_cap)
    wall = round(time.time() - t0, 1)
    run_id = res.get("run_id", "")
    log("  run_id %s in %ss" % (run_id or "(none)", wall))
    if not run_id:
        # No run_id means operator.run never got as far as starting a run. That is
        # data too - record it rather than dropping the goal.
        return {"id": g["id"], "run_id": "", "goal": g["goal"], "completed": False,
                "steps": 0, "errors": 0, "repeated_actions": 0, "ceiling_hit": False,
                "duration_s": wall, "error": str(res.get("error") or "no run_id")[:300],
                "template": template, "wall_cap_s": wall_cap,
        "provenance": provenance(),
                "provenance": provenance()}
    tr = mcp("operator.trace", {"run_id": run_id}, timeout=120)
    rec = {
        "id": g["id"], "run_id": run_id,
        # WHICH goals were asked, and the cap that applied. Same reason as the
        # loop census: a duration means different things under different caps,
        # and a row that does not say which template it came from cannot be
        # compared with one that came from another.
        "template": template, "wall_cap_s": wall_cap,
        "goal": (tr.get("run") or {}).get("goal") or g["goal"],
        "target": (tr.get("run") or {}).get("target"),
        "reason": (tr.get("run") or {}).get("reason"),
        "steps": int((tr.get("counters") or {}).get("steps") or 0),
        "errors": int((tr.get("counters") or {}).get("errors") or 0),
        "screenshots": int((tr.get("counters") or {}).get("screenshots") or 0),
        "repeated_actions": int((tr.get("counters") or {}).get("repeated_actions") or 0),
        "ceiling_hit": bool((tr.get("counters") or {}).get("ceiling_hit")),
        "duration_s": tr.get("duration_s"),
        "completed": bool(tr.get("ended_at")),
        "wall_s": wall,
        "warnings": (tr.get("warnings") or [])[:12],
    }
    log("  -> %s | steps=%s errors=%s thrash=%s ceiling=%s" % (
        "finished" if (rec["completed"] and not rec["ceiling_hit"] and not rec["errors"])
        else ("incomplete" if not rec["completed"] else
              ("ceiling" if rec["ceiling_hit"] else "errored")),
        rec["steps"], rec["errors"], rec["repeated_actions"], rec["ceiling_hit"]))
    return rec


def main():
    ap = argparse.ArgumentParser(description="Operator census runner")
    ap.add_argument("--template", default="default",
                    help="named template in operator-templates/ (default: default)")
    ap.add_argument("--list", action="store_true", help="list templates and exit")
    ap.add_argument("--operator", default=OPERATOR, help="who is running this")
    ap.add_argument("--skill", default=SKILL, help="the skill driving it")
    ap.add_argument("--tool", default=TOOL, help="what drove it, e.g. /loop")
    ap.add_argument("--session-id", default=SESSION_ID, dest="session_id",
                    help="the REAL caller session id - joins this run to its transcript")
    args = ap.parse_args()
    globals()["OPERATOR"] = args.operator
    globals()["SKILL"] = args.skill
    globals()["TOOL"] = args.tool
    globals()["SESSION_ID"] = args.session_id
    if args.list:
        for t in list_templates():
            print(t)
        return

    template, goals, tmpl_cap = load_template(args.template)
    done = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                done.add(json.loads(line)["id"])
            except Exception:
                pass
    log("operator census start: template=%s | %d goals, %d already done"
        % (template, len(goals), len(done)))
    for g in goals:
        if g["id"] in done:
            log("skip %s (already recorded)" % g["id"])
            continue
        try:
            rec = run_goal(g, template=template, wall_cap_default=tmpl_cap)
        except Exception as e:
            log("  goal raised: %s" % e)
            rec = {"id": g["id"], "run_id": "", "completed": False, "steps": 0,
                   "errors": 0, "repeated_actions": 0, "ceiling_hit": False,
                   "error": str(e)[:300],
                   "template": template, "wall_cap_s": goal_wall_cap(g, tmpl_cap),
                   "provenance": provenance()}
        if rec:
            with open(OUT, "a") as f:
                f.write(json.dumps(rec) + "\n")
        time.sleep(POLL_S)
    log("operator census complete")


if __name__ == "__main__":
    sys.exit(main())
