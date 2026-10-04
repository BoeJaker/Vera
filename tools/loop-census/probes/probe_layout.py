"""Run ONE census goal on prod under a role LAYOUT and score it with the template's checks.

    python3 probe_layout.py <template> <goal_id> [layout=L0] [budget=1800]

Layouts are per-run loop overrides (no config change):
  L0  baseline - routing defaults (GPU drives, CPU critic)
  L1  warm CPU coder   - coder_model=qwen3-coder:30b on cpu-246
  L2  CPU MoE executor - executor_model=qwen3.6:35b-a3b on cpu-247
  L3  deliverable review - the CPU MoE attacks the finished output once (opt-in flag)
  L4  L3 + the warm CPU coder
  L5  the CPU MoE (qwen3.6:35b-a3b, cpu-247) drives the loop: controller + planner
  L6  the agentic MoE (Qwen-AgentWorld-35B-A3B, cpu-246) drives AND executes
Out: /tmp/probe_layout/<layout>-<goal_id>.json (+ ev/<sid>.jsonl). Added 4 Oct 2026."""
import collections, json, os, re, subprocess, sys, time, uuid

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
src = open(P).read()
exec(src.split("# ── Set A goals")[0].replace('OUT = "/tmp/probe"', 'OUT = "/tmp/probe_layout"')
     .replace("return (n.hour, n.minute) >= (23, 15)", "return False"))
os.makedirs(OUT + "/ev", exist_ok=True)
# Events from Redis (vera:loop:events:<sid>), not the SSE reattach - that returned
# NOTHING for a 24-min session (4 Oct). rev.py sits beside this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rev as _rev
events = _rev.events

LAYOUTS = {
    "L0": {},
    "L1": {"coder_model": "qwen3-coder:30b", "coder_node": "cpu-246"},
    "L2": {"executor_model": "qwen3.6:35b-a3b", "executor_node": "cpu-247"},
    # adversarial deliverable review by the CPU MoE (feat/deliverable-review)
    "L3": {"deliverable_review": True},
    "L4": {"deliverable_review": True, "coder_model": "qwen3-coder:30b", "coder_node": "cpu-246"},
    # an MoE / specialised agentic model DRIVES the loop: controller + planner (controller_model)
    "L5": {"controller_model": "qwen3.6:35b-a3b", "controller_node": "cpu-247"},
    "L6": {"controller_model": "hf.co/unsloth/Qwen-AgentWorld-35B-A3B-GGUF:MXFP4_MOE",
           "controller_node": "cpu-246",
           "executor_model": "hf.co/unsloth/Qwen-AgentWorld-35B-A3B-GGUF:MXFP4_MOE",
           "executor_node": "cpu-246"},
}
tpl, gid = sys.argv[1], sys.argv[2]
layout = sys.argv[3] if len(sys.argv) > 3 else "L0"
budget = int(sys.argv[4]) if len(sys.argv) > 4 else 1800
g = next(x for x in json.load(open(os.path.expanduser("~/loop-census/templates/%s.json" % tpl)))["goals"]
         if x["id"] == gid)
if not box_free():
    log("box busy - not starting"); sys.exit(1)
sid = str(uuid.uuid4())
log("start %s %s %s" % (layout, gid, sid))
# Say so on /health (activity.declared) for everyone else on the box; cleared at the end.
ACT_ID = "probe-" + sid[:8]
def _declare(ttl):
    try:
        http("POST", "/mcp/call", {"name": "sys.activity.declare", "arguments": {
            "id": ACT_ID, "kind": "probe", "label": "%s %s (%s)" % (layout, gid, tpl),
            "by": "claude:0c68dd8c compute-roles", "ttl_s": ttl, "session_ids": sid}}, timeout=20)
    except Exception:
        pass
_declare(budget + 600)
proc = start(sid, dict({"goal": g["goal"]}, **LAYOUTS[layout]), budget)
t0 = time.time()
done_seen_at = None
post_done_dump = None
while time.time() - t0 < budget:
    time.sleep(10)
    if proc.poll() is not None and sid not in (running() or [sid]):
        break
    # A loop that emitted done but is still 'running' 60 s later is stuck in its tail
    # (stream_complete / the memory-recording wrapper). 4 Oct: 25 min there. Capture
    # where (asyncio await chains), then stop waiting.
    if done_seen_at is None:
        try:
            if any(str(e.get("type", "")).endswith("v6.done") for e in events(sid)[-12:]):
                done_seen_at = time.time()
        except Exception:
            pass
    elif time.time() - done_seen_at > 60:
        try:
            post_done_dump = http("GET", "/sys/debug/tasks?match=agent_loop&limit=20", timeout=60)
            with open("%s/postdone-%s.json" % (OUT, sid[:8]), "w") as fh:
                json.dump(post_done_dump, fh, indent=1)
            log("%s done but still running 60 s later - await chains saved" % sid)
        except Exception:
            pass
        break
capped = sid in (running() or [sid])
if capped:
    log("%s over budget - cancelling" % sid)
stop(sid, proc)
wall = time.time() - t0
# A loop still listed as running after stop() is an orphan that blocks every later
# probe AND the census scheduler (4 Oct, 67d0975e): set its cancel flag.
if sid in (running() or [sid]):
    http("POST", "/workshop/agent_loop/cancel", {"session_id": sid}, timeout=30)
    log("%s still running after stop - cancel flag set" % sid)
ev = events(sid)
with open("%s/ev/%s.jsonl" % (OUT, sid), "w") as f:
    for e in ev:
        f.write(json.dumps(e) + "\n")


def T(e):
    return str(e.get("type", ""))


def fetch(name):
    for cand in (name, "workspace/" + name):
        t = subprocess.run(["curl", "-sk", "-m", "20", "%s/remote/sandbox/preview/%s/%s" % (BASE, sid, cand)],
                           capture_output=True, text=True).stdout
        if t and "not found" not in t[:80].lower():
            return t
    return ""


final = ""
for e in ev:
    if T(e).endswith(("v6.done", "v5.done", "final_answer")):
        final = str(e.get("summary") or e.get("answer") or e.get("final") or final)
crit = [e for e in ev if T(e).endswith("step_critique")]
tools = [e for e in ev if T(e).endswith("tool_done")]
res = {
    "sid": sid, "goal": gid, "template": tpl, "layout": layout, "args": LAYOUTS[layout],
    "wall": round(wall), "capped": capped,
    "done": any(T(e).endswith("v6.done") for e in ev),
    "fast_path": any("fast_path" in T(e) for e in ev),
    "steps_started": len({e.get("step_id") for e in ev if T(e).endswith("step_start")}),
    "tool_calls": len(tools),
    "tool_fails": [(e.get("tool"), str(e.get("error") or "")[:140]) for e in tools if e.get("ok") is False],
    "critiques": len(crit),
    "critic_flags": sum(1 for e in crit if e.get("ok") is False),
    "critic_flag_notes": [str(e.get("note") or "")[:240] for e in crit if e.get("ok") is False],
    "verify_fails": [str(e.get("reason") or "")[:200] for e in ev
                     if T(e).endswith("verify") and (e.get("ok") is False or e.get("met") is False)],
    "corrections": [str(e.get("note") or "")[:160] for e in ev if T(e).endswith("arg_correction")],
    "nav_only_warnings": sum(1 for e in tools if '"warning"' in str(e.get("preview") or "")[:80]),
    "stuck_after_done": done_seen_at is not None and post_done_dump is not None,
    "deliverable_review": [{k: e.get(k) for k in ("flag", "elapsed_s", "error", "note")}
                           for e in ev if T(e).endswith("deliverable_review")],
    "event_types": dict(collections.Counter(T(e).split(".", 1)[-1] for e in ev).most_common(14)),
    "checks": [],
}
files = {}
for c in g.get("checks") or []:
    ok, why = None, c.get("why", "")
    if "file" in c:
        body = files.setdefault(c["file"], fetch(c["file"]))
        if c.get("exists"):
            ok = bool(body.strip())
        elif "regex" in c:
            ok = bool(re.search(c["regex"], body))
        elif "absent" in c:
            ok = bool(body.strip()) and c["absent"] not in body
        elif "any_of" in c:
            ok = any(a in body for a in c["any_of"])
    elif "answer_regex" in c:
        ok = bool(re.search(c["answer_regex"], final, re.I))
    elif "answer_contains" in c:
        ok = str(c["answer_contains"]).lower() in final.lower()
    elif "answer_min_words" in c:
        ok = len(final.split()) >= int(c["answer_min_words"])
    res["checks"].append((ok, why))
ok_n = sum(1 for o, _ in res["checks"] if o)
res["score"] = round(ok_n / len(res["checks"]), 3) if res["checks"] else None
res["files"] = {k: v[:1500] for k, v in files.items()}
res["final"] = final[:1500]
json.dump(res, open("%s/%s-%s.json" % (OUT, layout, gid), "w"), indent=1)
try:
    http("POST", "/mcp/call", {"name": "sys.activity.clear", "arguments": {"id": ACT_ID}}, timeout=20)
except Exception:
    pass
log("done %s %s wall=%s score=%s flags=%s/%s" % (layout, gid, res["wall"], res["score"],
                                                 res["critic_flags"], res["critiques"]))
