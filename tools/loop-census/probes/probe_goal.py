"""Run ONE census goal (by template + id) on prod and score it with the template's own
file checks. python3 probe_goal.py <template> <goal_id> [budget]. Out: ~/loop-census/probe4/<id>.json"""
import json, os, re, sys, uuid
P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
src = open(P).read()
exec(src.split("# ── Set A goals")[0]
     .replace("return (n.hour, n.minute) >= (23, 15)", "return False"))
OUT = os.environ.get("CENSUS_PROBE_GOAL_OUT", "").strip() or os.path.expanduser("~/loop-census/probe4")
os.makedirs(OUT + "/ev", exist_ok=True)
# Score with the census's own evaluator (vera/census/quality.py, pure), loaded by path:
# a hand copy here lacked all_of/contains/min_bytes and scored those checks None
# (4 Oct 2026: build-multifile read 0.667 in every layout with all three functions present).
import importlib.util as _ilu
_qs = _ilu.spec_from_file_location("census_quality", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "vera", "census", "quality.py"))
_quality = _ilu.module_from_spec(_qs)
_qs.loader.exec_module(_quality)
tpl, gid = sys.argv[1], sys.argv[2]
budget = int(sys.argv[3]) if len(sys.argv) > 3 else 1200
g = next(x for x in json.load(open(os.path.expanduser("~/loop-census/templates/%s.json" % tpl)))["goals"] if x["id"] == gid)
if not box_free():
    log("box busy - not starting"); sys.exit(1)
sid = str(uuid.uuid4())
log("start %s %s" % (gid, sid))
wall = full(sid, g["goal"], budget)
ev = events(sid)
def fetch(name):
    for cand in (name, "workspace/" + name):
        t = subprocess.run(["curl", "-sk", "-m", "20", "%s/remote/sandbox/preview/%s/%s" % (BASE, sid, cand)],
                           capture_output=True, text=True).stdout
        if t and "not found" not in t[:80].lower():
            return t
    return ""
final = ""
for e in ev:
    if str(e.get("type", "")).endswith(("v6.done", "v5.done", "final_answer")):
        final = str(e.get("summary") or e.get("answer") or e.get("final") or final)
res = {"sid": sid, "goal": gid, "wall": round(wall), "checks": [],
       "corrections": [e.get("note") for e in ev if str(e.get("type", "")).endswith("arg_correction")],
       "tools": [(e.get("tool"), e.get("ok"), str(e.get("error") or "")[:160]) for e in ev
                 if str(e.get("type", "")).endswith("tool_done")],
       "critiques": [str(e.get("note") or "")[:300] for e in ev if str(e.get("type", "")).endswith("step_critique")],
       "verify": [str(e.get("reason") or "")[:200] for e in ev if str(e.get("type", "")).endswith("v6.verify")],
       "done": any(str(e.get("type", "")).endswith("v6.done") for e in ev)}
files = {n: fetch(n) for n in _quality.files_wanted(g.get("checks"))}
_q = _quality.evaluate(g.get("checks"), {k: v for k, v in files.items() if v.strip()}, final)
res["checks"] = [(r.get("ok"), r.get("label") or r.get("detail") or "") for r in _q["results"]]
res["score"] = _q["score"]
res["files"] = {k: v[:1500] for k, v in files.items()}
json.dump(res, open(OUT + "/%s.json" % gid, "w"), indent=1)
log("done %s wall=%s" % (gid, res["wall"]))
