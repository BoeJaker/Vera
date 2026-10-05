#!/usr/bin/env python3
"""Qualitative digest of a census run: the plan, every step's cycles and errors,
the controller's verdicts, and the answer - what the KPIs do not show.

    python3 review_run.py census.run72.jsonl > review/run72.md
"""
import json, subprocess, sys, re

def rc(*args):
    return subprocess.run(["redis-cli", *args], capture_output=True, text=True).stdout

def clip(s, n):
    s = str(s or "").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n] + "..."

def digest(row):
    sid = row["session_id"]
    goal = rc("hget", f"vera:loop:run:{sid}", "goal").strip()
    ev = [json.loads(l) for l in rc("lrange", f"vera:loop:events:{sid}", "0", "-1").splitlines() if l.strip()]
    out = []
    q = row.get("quality") or {}
    out.append(f"## {row['id']}  [{row['status']}, {round(row.get('wall_s') or 0)} s, q={q.get('score')}]")
    out.append(f"GOAL: {clip(goal, 400)}")
    plan_steps = {}
    for e in ev:
        t = e.get("type")
        if t == "agent_loop_v6.tier":
            out.append(f"TIER: {e.get('tier')} (heuristic {e.get('heuristic')}, llm {e.get('llm')}) - {clip(e.get('reason'), 200)}")
        elif t == "agent_loop_v6.intent":
            out.append(f"INTENT: {e.get('intent')} - {clip(e.get('reason'), 120)}")
        elif t == "agent_loop_v6.plan":
            steps = e.get("steps")
            if isinstance(steps, str):
                try: steps = eval(steps, {"__builtins__": {}}, {})
                except Exception: steps = []
            out.append(f"PLAN done_when: {clip(e.get('done_when'), 300)}")
            for s in steps or []:
                plan_steps[str(s.get("id"))] = s
                out.append(f"  step {s.get('id')}: {clip(s.get('title'), 90)} | caps={s.get('caps')} | success: {clip(s.get('success'), 220)}")
    out.append("EXECUTION:")
    cur_step = None
    for e in ev:
        t = e.get("type"); st = str(e.get("step_id") or "")
        if t == "agent_loop_v5.step_start":
            cur_step = st
            title = clip((plan_steps.get(st) or {}).get("title") or e.get("title"), 80)
            out.append(f"  -- step {st}: {title}")
        elif t == "agent_loop_v5.think":
            out.append(f"     think c{e.get('cycle')}: {clip(e.get('thought'), 200)}")
        elif t == "agent_loop_v5.tool_call":
            out.append(f"     call  c{e.get('cycle')}: {e.get('tool')} {clip(json.dumps(e.get('args')) if not isinstance(e.get('args'), str) else e.get('args'), 220)}")
        elif t == "agent_loop_v5.tool_done":
            ok = str(e.get("ok")); err = clip(e.get("error"), 260); pv = clip(e.get("preview"), 160)
            ms = e.get("elapsed_ms")
            out.append(f"     done  c{e.get('cycle')}: {e.get('tool')} ok={ok} {int(ms)//1000 if str(ms).isdigit() else ms}s " + (f"ERROR: {err}" if err else f"-> {pv}"))
        elif t == "agent_loop_v5.arg_correction":
            out.append(f"     heal: {clip(e.get('note'), 200)}")
        elif t in ("agent_loop.error_recovery_start", "agent_loop.error_recovery_done", "agent_loop.error_recovery_attempt", "agent_loop.error_recovery_arg_correction"):
            out.append(f"     {t.split('.')[-1]}: {clip(e.get('reason') or e.get('note') or e.get('error') or json.dumps(e.get('args')), 200)}")
        elif t == "agent_loop_v5.scope_widened":
            out.append(f"     scope widened: {clip(e.get('reason'), 120)}")
        elif t == "agent_loop_v5.step_done":
            out.append(f"     step_done ok={e.get('ok')}: {clip(e.get('summary'), 260)}")
        elif t == "agent_loop_v6.verify":
            out.append(f"     verify met={e.get('met')}: {clip(e.get('reason'), 220)}")
        elif t == "agent_loop_v6.assess":
            out.append(f"     assess [{e.get('goal_alignment')}] {clip(e.get('action') or '', 60)} -> {clip(e.get('direction'), 220)}")
        elif t == "agent_loop_v6.step_finalized":
            out.append(f"     finalized: {clip(json.dumps({k: v for k, v in e.items() if k not in ('type','ts','ver','br','dirty','via','stream_id','session_id')}), 200)}")
        elif t == "agent_loop_v6.done":
            out.append(f"DONE ({e.get('reason')}, {e.get('steps_run')} steps, {e.get('cycles')} cycles). ANSWER:")
            for line in str(e.get("summary") or "")[:1400].splitlines()[:18]:
                out.append("    | " + line[:200])
    for w in row.get("warnings") or []:
        out.append(f"WARN: {clip(w, 220)}")
    for r in q.get("results") or []:
        out.append(f"CHECK {'ok ' if r.get('ok') else 'FAIL'}: {r.get('label')} {('- ' + clip(r.get('detail'), 120)) if r.get('detail') else ''}")
    return "\n".join(out)

def main():
    f = sys.argv[1]
    rows = [json.loads(l) for l in open(f)]
    print(f"# {f}: {len(rows)} goals\n")
    for r in rows:
        try:
            print(digest(r))
        except Exception as e:
            print(f"## {r.get('id')}: digest failed: {e!r}")
        print()

if __name__ == "__main__":
    main()
