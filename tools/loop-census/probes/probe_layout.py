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
  L7  L3 + answer check - the CPU MoE also checks the FINAL answer against the files
  L8  L7 + the warm CPU coder
  I1  info-first: info.seek leads the catalogue for a goal that needs the web
  I2  I1 + answer check (the CPU MoE checks the final answer against the files)
  C1  verifier + final gate on the CPU MoE (qwen3.6:35b-a3b, cpu-246) - stronger checks, off the GPU
  C2  journal/finalize/adjust/strategist on the 9B on gpu-250-cpu - same model, off the GPU slot
  C3  C1 + C2
Out: ~/loop-census/probe_layout/<layout>-<goal_id>.json (+ ev/<sid>.jsonl; CENSUS_PROBE_LAYOUT_OUT).
    python3 probe_layout.py --rescore <sid> <template> <goal_id> [layout]
rebuilds a probe's result from its records (Redis events + sandbox files) without running
anything - for results lost with their output dir (8 Oct 2026). Added 4 Oct 2026."""
import collections, json, os, re, subprocess, sys, time, uuid

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
src = open(P).read()
exec(src.split("# ── Set A goals")[0]
     .replace("return (n.hour, n.minute) >= (23, 15)", "return False"))
OUT = os.environ.get("CENSUS_PROBE_LAYOUT_OUT", "").strip() or os.path.expanduser("~/loop-census/probe_layout")
os.makedirs(OUT + "/ev", exist_ok=True)
# Events from Redis (vera:loop:events:<sid>), not the SSE reattach - that returned
# NOTHING for a 24-min session (4 Oct). rev.py sits beside this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rev as _rev
events = _rev.events
# Score with the census's own evaluator (vera/census/quality.py, pure), loaded by path:
# a hand copy here lacked all_of/contains/min_bytes and scored those checks None
# (4 Oct 2026: build-multifile read 0.667 in every layout with all three functions present).
import importlib.util as _ilu
_qs = _ilu.spec_from_file_location("census_quality", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "vera", "census", "quality.py"))
_quality = _ilu.module_from_spec(_qs)
_qs.loader.exec_module(_quality)

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
    # the CPU MoE also checks the GPU's FINAL answer against the files + unresolved findings
    "L7": {"deliverable_review": True, "answer_check": True},
    "L8": {"deliverable_review": True, "answer_check": True,
           "coder_model": "qwen3-coder:30b", "coder_node": "cpu-246"},
    # info-first (compute-roles P1a, 10 Oct 2026)
    "I1": {"info_first": True},
    "I2": {"info_first": True, "answer_check": True},
    # goal constraints into done_when + the final gate (plan.goal_frame, B2, 11 Oct 2026)
    "G1": {"minds": {"plan.goal_frame": "on"}},
    "G2": {"info_first": True, "answer_check": True, "minds": {"plan.goal_frame": "on"}},
    "W2": {"minds": {"plan.goal_frame": "writer"}},            # R-B2: constraints to the writer (four-minds)
    # NLI claim check (compute-roles P3, 11 Oct 2026): I2 + claim_nli shadow (recorded) / hint (to the checker)
    "N0": {"info_first": True, "answer_check": True, "claim_nli": "shadow"},
    "N1": {"info_first": True, "answer_check": True, "claim_nli": "hint"},
    # P1b known dated facts from intel.lookup before planning (11 Oct 2026): alone / on top of I2
    "F1": {"intel_prefacts": True},
    "F2": {"info_first": True, "answer_check": True, "intel_prefacts": True},
    # A3 plan critique (11 Oct 2026): the CPU 35B reviews the whole plan beside the run
    "R1": {"plan_critique": True},
    # A6 one journal view for the executor (11 Oct 2026): the live block shows only what is new
    "J1": {"journal_delta": True},
    # B3 step-result entities into the journal (node NER), alone / with the A6 journal view
    "E1": {"journal_ner": True},
    "E2": {"journal_ner": True, "journal_delta": True},
    # gather-then-write steps end at their author call (prose-only 380-1,148 s on re-checks, 11 Oct 2026)
    "W1": {"author_gather": True},
    # the loop's role-less stages placed on CPU nodes (stage_models, 10 Oct 2026)
    "C1": {"stage_models": "verifier=qwen3.6:35b-a3b@cpu-246,gate=qwen3.6:35b-a3b@cpu-246"},
    "C2": {"stage_models": "journal=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu,"
                           "finalize=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu,"
                           "adjust=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu,"
                           "strategist=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu"},
    "C3": {"stage_models": "verifier=qwen3.6:35b-a3b@cpu-246,gate=qwen3.6:35b-a3b@cpu-246,"
                           "journal=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu,"
                           "finalize=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu,"
                           "adjust=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu,"
                           "strategist=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu"},
}
RESCORE = ""
if len(sys.argv) > 2 and sys.argv[1] == "--rescore":
    RESCORE, sys.argv = sys.argv[2], [sys.argv[0]] + sys.argv[3:]


class _Finished:
    """Stands in for the curl process when rescoring: nothing is running."""
    def poll(self):
        return 0


def loop_seconds(ev):
    """First event -> v6.done, from the events (the probe's own wall includes its tail)."""
    import datetime as _dt
    ts = [str(e.get("ts") or "") for e in ev if e.get("ts")]
    done = [str(e.get("ts") or "") for e in ev if str(e.get("type", "")).endswith("v6.done") and e.get("ts")]
    try:
        p = lambda s: _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return round((p(done[-1]) - p(ts[0])).total_seconds()) if ts and done else None
    except Exception:
        return None


tpl, gid = sys.argv[1], sys.argv[2]
layout = sys.argv[3] if len(sys.argv) > 3 else "L0"
budget = int(sys.argv[4]) if len(sys.argv) > 4 else 1800
g = next(x for x in json.load(open(os.path.expanduser("~/loop-census/templates/%s.json" % tpl)))["goals"]
         if x["id"] == gid)
if not RESCORE and not box_free():
    log("box busy - not starting"); sys.exit(1)
sid = RESCORE or str(uuid.uuid4())
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
if not RESCORE:
    _declare(budget + 600)
proc = _Finished() if RESCORE else start(sid, dict({"goal": g["goal"]}, **LAYOUTS[layout]), budget)
t0 = time.time()
done_seen_at = None
post_done_dump = None
while not RESCORE and time.time() - t0 < budget:
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
            # Unfiltered: the tail is in the /mcp/call wrapper, whose tasks are not named
            # agent_loop (4 Oct: match=agent_loop matched 0 of 159).
            post_done_dump = http("GET", "/sys/debug/tasks?limit=200", timeout=60)
            with open("%s/postdone-%s.json" % (OUT, sid[:8]), "w") as fh:
                json.dump(post_done_dump, fh, indent=1)
            log("%s done but still running 60 s later - await chains saved" % sid)
        except Exception:
            pass
        break
# Redis says done -> not capped, whatever the (slow) HTTP list says.
capped = not RESCORE and not _rev.finished(sid) and sid in (running() or [sid])
if capped:
    log("%s over budget - cancelling" % sid)
if not RESCORE:
    stop(sid, proc)
wall = time.time() - t0
# A loop still listed as running after stop() is an orphan that blocks every later
# probe AND the census scheduler (4 Oct, 67d0975e): set its cancel flag.
if not RESCORE and not _rev.finished(sid) and sid in (running() or [sid]):
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
files = {n: fetch(n) for n in _quality.files_wanted(g.get("checks"))}
_q = _quality.evaluate(g.get("checks"), {k: v for k, v in files.items() if v.strip()}, final)
res["checks"] = [(r.get("ok"), r.get("label") or r.get("detail") or "") for r in _q["results"]]
res["score"] = _q["score"]
res["files"] = {k: v[:1500] for k, v in files.items()}
res["final"] = final[:1500]
res["loop_s"] = loop_seconds(ev)
if RESCORE:
    res["rescored"] = True
    res["wall"] = res["loop_s"] if res["loop_s"] is not None else res["wall"]
json.dump(res, open("%s/%s-%s.json" % (OUT, layout, gid), "w"), indent=1)
try:
    http("POST", "/mcp/call", {"name": "sys.activity.clear", "arguments": {"id": ACT_ID}}, timeout=20)
except Exception:
    pass
log("done %s %s wall=%s score=%s flags=%s/%s" % (layout, gid, res["wall"], res["score"],
                                                 res["critic_flags"], res["critiques"]))
