"""Run ONE census goal (by template + id) on prod and score it with the template's own
file checks. python3 probe_goal.py <template> <goal_id> [budget]. Out: /tmp/probe4/<id>.json"""
import json, os, re, sys, uuid
P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
src = open(P).read()
exec(src.split("# ── Set A goals")[0].replace('OUT = "/tmp/probe"', 'OUT = "/tmp/probe4"')
     .replace("return (n.hour, n.minute) >= (23, 15)", "return False"))
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
    res["checks"].append((ok, why))
res["files"] = {k: v[:1500] for k, v in files.items()}
json.dump(res, open(OUT + "/%s.json" % gid, "w"), indent=1)
log("done %s wall=%s" % (gid, res["wall"]))
