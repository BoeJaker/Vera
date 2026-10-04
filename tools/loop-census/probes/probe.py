#!/usr/bin/env python3
"""Targeted loop probes (user 2026-10-01: tests, not a census, to rank the action items).
Set A: plan-only - start a v7 loop, cancel as soon as it has planned; keep its events.
Set B: short full loops with exact, checkable content requirements.
One loop at a time; nothing starts after 23:15 Europe/London. Out: /tmp/probe/."""
import datetime, glob, json, os, subprocess, time
from zoneinfo import ZoneInfo

BASE = os.environ.get("CENSUS_BASE", "").strip().rstrip("/") or "https://127.0.0.1:8999"
LON = ZoneInfo("Europe/London")
OUT = "/tmp/probe"
os.makedirs(OUT + "/ev", exist_ok=True)
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import rev as _rev   # loop events from Redis (vera:loop:events:<sid>), not SSE


def log(m):
    with open(OUT + "/probe.log", "a") as f:
        f.write("%s %s\n" % (datetime.datetime.now(LON).strftime("%H:%M:%S"), m))


def http(method, path, body=None, timeout=60):
    a = ["curl", "-sk", "-m", str(timeout), "-X", method, BASE + path, "-H", "Content-Type: application/json"]
    if body is not None:
        a += ["--data-binary", json.dumps(body)]
    o = subprocess.run(a, capture_output=True, timeout=timeout + 15).stdout
    try:
        return json.loads(o) if o else {}
    except Exception:
        return {}


def running():
    """Running loop ids, or None when the list could not be read (prod restarting):
    unknown is NOT 'nothing running'."""
    h = http("GET", "/workshop/agent_loop/sessions?status=running", timeout=30)
    if not isinstance(h, dict) or "sessions" not in h:
        return None
    return [s.get("session_id") for s in h.get("sessions") or []]


def box_free():
    h = http("GET", "/health", timeout=20) or {}
    return bool(h.get("redis")) and not (h.get("census") or {}).get("busy") and running() == []


def events(sid):
    return _rev.events(sid)


def deadline():
    n = datetime.datetime.now(LON)
    return (n.hour, n.minute) >= (23, 15)


def stop(sid, proc):
    http("POST", "/workshop/agent_loop/cancel", {"session_id": sid})
    for _ in range(90):
        if sid not in (running() or [sid]):
            break
        time.sleep(5)
    try:
        proc.kill()
    except Exception:
        pass


def start(sid, args, timeout):
    body = {"name": "dag.agent_loop_v7", "arguments": dict(args, session_id=sid)}
    return subprocess.Popen(["curl", "-sk", "-m", str(timeout), "-X", "POST", BASE + "/mcp/call",
                             "-H", "Content-Type: application/json", "--data-binary", json.dumps(body)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def plan_only(sid, goal):
    proc = start(sid, {"goal": goal}, 900)
    t0 = time.time()
    while time.time() - t0 < 600:
        time.sleep(4)
        ev = events(sid)
        kinds = {e.get("type", "").split(".", 1)[-1] for e in ev}
        if kinds & {"plan", "fast_path", "done"}:
            break
    stop(sid, proc)
    ev = events(sid)
    with open("%s/ev/%s.jsonl" % (OUT, sid), "w") as f:
        for e in ev:
            f.write(json.dumps(e) + "\n")
    return time.time() - t0


def full(sid, goal, budget):
    proc = start(sid, {"goal": goal}, budget)
    t0 = time.time()
    while time.time() - t0 < budget:
        time.sleep(10)
        if proc.poll() is not None and sid not in (running() or [sid]):
            break
    if sid in (running() or [sid]):
        log("%s over budget - cancelling" % sid)
    stop(sid, proc)
    ev = events(sid)
    with open("%s/ev/%s.jsonl" % (OUT, sid), "w") as f:
        for e in ev:
            f.write(json.dumps(e) + "\n")
    return time.time() - t0


# ── Set A goals: every default + family census goal, once ────────────────────
goals = []
seen = set()
for tf in ["default", "business-family", "code-family", "data-family", "exec-family", "infra-family",
           "markets-family", "mesh-family", "prose-family", "research-family", "operator-family"]:
    d = json.load(open(os.path.expanduser("~/loop-census/templates/%s.json" % tf)))
    for g in d.get("goals") or []:
        if g["id"] in seen:
            continue
        seen.add(g["id"])
        goals.append((tf, g["id"], g["goal"], g.get("intent"), g.get("tier")))
json.dump(goals, open(OUT + "/setA_goals.json", "w"))

# ── Set B: exact, checkable content requirements ─────────────────────────────
SET_B = [
    ("b-stats-names", "Create /workspace/stats.py with three functions named exactly mean, median and mode (each takes a list of numbers), then run a quick check that mean([1,2,3]) is 2."),
    ("b-config-keys", "Create /workspace/config.json containing exactly the keys host, port and debug, with port set to 8080 and debug set to false."),
    ("b-readme-sections", "Write /workspace/README.md for a CLI tool called tally with exactly three sections headed Install, Usage and License."),
    ("b-edit-preserve", "Create /workspace/greet.py with a function greet(name) that returns 'Hello, ' + name, then change it so it returns 'Hi, ' + name while keeping everything else identical."),
    ("b-data-count", "Generate 50 random integers between 1 and 100 into /workspace/n.csv, one per line, then report how many of them are above 50."),
]

log("start: %d plan-only goals, %d full loops" % (len(goals), len(SET_B)))
while not box_free():
    if deadline():
        raise SystemExit("box never free")
    time.sleep(30)
for i, (tf, gid, goal, gi, gt) in enumerate(goals):
    if deadline():
        log("deadline - stopping set A at %d" % i)
        break
    while not box_free():
        time.sleep(15)
    sid = "probeA-%s" % gid
    dt = plan_only(sid, goal)
    log("A %d/%d %s %.0fs" % (i + 1, len(goals), gid, dt))
for sid, goal in SET_B:
    if deadline():
        log("deadline - stopping set B before %s" % sid)
        break
    while not box_free():
        time.sleep(15)
    dt = full(sid, goal, 900)
    log("B %s %.0fs" % (sid, dt))
log("DONE")
