#!/usr/bin/env python3
"""Phase A census runner - STRICTLY SERIAL, yields to other users of the box.

One loop at a time (the GPU gate is capacity 1). Before each run it waits for the
gate to be free AND for no other loop session to be running, so this never
competes with a person or another agent. Each run's trace is appended to
census.jsonl; a run that exceeds the wall cap is cancelled and recorded as such,
because a timeout is data too.
"""
import calendar, json, subprocess, threading, time, sys, os

# Which Vera the goals run against, and where the rows go. Defaults are prod and
# the numbered series' files. CENSUS_BASE points a run at a sandbox (e.g. the
# bleeding-edge mirror, https://localhost:8981) to measure code BEFORE it reaches
# main; give it its own CENSUS_OUT/CENSUS_LOG so it never mixes into prod's
# series. The run-state files (census.control.json / census.active.json) stay
# shared on purpose: every run uses the one GPU, so prod's /health and the
# scheduler must see a sandbox census as busy too. (2026-09-27)
BASE   = os.environ.get("CENSUS_BASE", "").strip().rstrip("/") or "https://localhost:8999"
CODE = os.path.dirname(os.path.abspath(__file__))       # this harness's code (the repo)
# Its DATA - archives, logs, state, templates, stall dumps - is not the code folder:
HERE = os.path.expanduser(os.environ.get("VERA_CENSUS_DIR", "").strip() or "~/loop-census")
OUT    = os.environ.get("CENSUS_OUT", "").strip() or os.path.join(HERE, "census.jsonl")
LOG    = os.environ.get("CENSUS_LOG", "").strip() or os.path.join(HERE, "census.log")
# Planning style forced on EVERY goal (--plan-style / CENSUS_PLAN_STYLE):
# auto|flat|stepwise|detailed. Blank = each goal's own "plan_style" field, and a
# goal without one runs the loop's default (auto). A forced style is its own
# SERIES (default-style-stepwise-runN), numbered and ingested apart from the
# template's own runs, so a style experiment never lands in the baseline series.
# (2026-09-27)
PLAN_STYLE = os.environ.get("CENSUS_PLAN_STYLE", "").strip().lower()
# Intent core forced on every goal (--intent-core / CENSUS_INTENT_CORE): off|order.
# Blank = the goal's own "intent_core" field, else the loop default (off). Sent to
# the loop ONLY when set - a Vera without the option would refuse the argument.
# A forced core is its own SERIES (<template>-core-order-runN). (2026-09-28, roadmap G)
INTENT_CORE = os.environ.get("CENSUS_INTENT_CORE", "").strip().lower()
# A switchboard set forced on every goal - a census ARM (CENSUS_MINDS contract v1, four-minds
# ROADMAP section 4.1; the scheduler sets both from a schedule's target.minds / minds_label):
# CENSUS_MINDS is compact JSON {"<feature_id>": "<mode>"}, CENSUS_MINDS_LABEL names the arm
# ([a-z0-9-]{1,24}, not style-*/core-*, not the template). Validated with minds.validate against
# CENSUS_BASE before goal 1 - refused, unreachable or an unknown cap exits non-zero with no goal
# started. Sent to the loop ONLY when non-empty. An arm is its own SERIES
# (<template>[-style-s][-core-m]-<label>-runN) and every row carries minds_requested,
# minds_digest and minds_label. (2026-10-10)
MINDS_RAW = os.environ.get("CENSUS_MINDS", "").strip()
MINDS_LABEL = os.environ.get("CENSUS_MINDS_LABEL", "").strip()
MINDS = {}                     # parsed in main()
MINDS_DIGEST = "default"
WALL_CAP_S   = 1800          # 30 min per run, then cancel and record it.
                             # Raised from 1500 on 2026-08-31: run 21 capped
                             # research-web at 1512.5s while it was on its FINAL
                             # step having already written webgpu_summary.md v2
                             # (3037 bytes, cycle 27, prose.author ok) - a finished
                             # deliverable recorded as a timeout. author-then-edit
                             # capped the same way at 1513.0s. The longest HEALTHY
                             # completion in the same run was 1308.2s, so 1800 still
                             # separates "slow but finishing" from "stuck", and buys
                             # ~4 more prose.author cycles (51-74s each).
POLL_S       = 15            # session status over HTTP
# Stall watchdog (2026-10-04): a goal whose loop emits NO event for this long gets ONE
# diagnostic dump - prod's thread stacks (py-spy) and its asyncio await chains
# (sys.debug.tasks) - written to census_stalls/. 4 Oct 01:44-02:00 a writer step saved
# its report and the loop then sat 16 min, GPU idle, until the wall cap; nothing in the
# log named the wait. Diagnostic only: it never cancels or changes the goal.
# 600 not 300: 4 Oct 03:22 a writer step streamed 3,451 tokens over 248 s (GPU 100%) with
# no loop event - a 300 s threshold dumped a healthy goal. The real stall was 16 min.
STALL_DUMP_S = int(os.environ.get("CENSUS_STALL_DUMP_S", "600") or 600)
STALL_CHECK_S = 60
# A prod restart kills every in-process loop. Seen within a minute (process start time
# changed) instead of waiting ~20 min for the stale-session rule to call it interrupted
# (5 Oct 2026: research-report sat 21 min on a loop the 12:04 restart had killed).
RESTART_GRACE_S = int(os.environ.get("CENSUS_RESTART_GRACE_S", "60") or 60)
CONTROL_POLL_S = 3           # census.control.json, a local file (pause/drop must not be missed)

# ── Who is running this, and what drove them ─────────────────────────────────
# SESSION_ID was a hardcoded constant, so EVERY census ever run attributed to
# the same fake actor: Loop Lab could not show who operated a run, because they
# all looked like one. It is still the default so old attribution stays intact,
# but it is now overridable, and the provenance is recorded on every row.
#
# `caller_session` is what /mcp/call is told, and is what joins a census to a
# transcript already ingested by ide.claude_sessions.* — that is the whole
# point: the conversation and the run exist in Vera separately, and this is the
# key between them. Set it to the REAL Claude Code session id when a session
# drives a census, and the Dispatch panel's turn history becomes the log of why
# each goal was asked.
SESSION_ID   = (os.getenv("VERA_CENSUS_SESSION_ID")
                or "e917286d-37b1-4374-819c-0d90eac48b09")
OPERATOR     = os.getenv("VERA_CENSUS_OPERATOR") or ""   # who: claude / codex / user / a name
SKILL        = os.getenv("VERA_CENSUS_SKILL") or ""      # which skill: improve-vera-sandboxed
TOOL         = os.getenv("VERA_CENSUS_TOOL") or ""       # which tool drove it: /loop, manual, sched


HELPERS_FILE = os.path.join(HERE, "helpers.json")


def load_helpers():
    """Helper scripts the operator wrote to drive or observe this census.

    They are usually written to /tmp, run once, and gone — which is exactly why
    they need recording. A helper that restarts prod, cancels a run or launches
    a goal is part of how the numbers were produced, and afterwards nothing says
    it existed. Name and purpose only: the row should say WHAT ran and WHY, not
    carry the script.

    Best-effort. A missing or unreadable manifest yields no helpers rather than
    failing a census over bookkeeping.
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
        purpose = " ".join(str(h.get("purpose") or "").split())
        if name:
            out.append({"name": name, "purpose": purpose[:400]})
    return out


def provenance():
    """Who ran this census, what drove them, and what tooling they wrote.

    Blank fields are left blank rather than guessed: an unattributed run should
    look unattributed, not be quietly credited to whoever edited the harness
    last.
    """
    return {"caller_session": SESSION_ID, "operator": OPERATOR,
            "skill": SKILL, "tool": TOOL, "helpers": load_helpers()}

# ── Per-goal wall caps ───────────────────────────────────────────────────────
# ONE cap for every goal was wrong in both directions. trivial-chat finishes in
# ~100s and never needs 1800; the research family routinely spends its whole
# budget GATHERING and gets recorded as a timeout with a finished deliverable on
# disk (run 21 capped research-web at 1512.5s having already written
# webgpu_summary.md v2 — the reason the global cap was raised to 1800 in the
# first place, which then had to be paid by all twelve goals).
#
# A goal may now declare `wall_cap_s`, and a template may declare a default for
# its goals. Resolution order, most specific first:
#     goal["wall_cap_s"]  ->  template["wall_cap_s"]  ->  WALL_CAP_S
#
# The cap that APPLIED is recorded on every row. Without that, a 3000s cap and
# an 1800s cap are indistinguishable in the archive, and a later comparison
# across templates would be measuring the harness rather than the loop.
def goal_wall_cap(g, template_default=0):
    for v in (g.get("wall_cap_s"), template_default, WALL_CAP_S):
        try:
            n = int(v or 0)
        except Exception:
            n = 0
        if n > 0:
            return n
    return WALL_CAP_S

# ── Templates ────────────────────────────────────────────────────────────────
# A census is a TEMPLATE (a named set of goals) run against a MODEL. Both are
# recorded on every row, because a score is meaningless without them: comparing
# run 39 against run 41 tells you nothing if one asked twelve questions of a 9B
# and the other asked five of a 14B.
#
# templates/<name>.json:
#   {"name": ..., "description": ..., "model": "<optional default>",
#    "goals": [ {id, intent, tier, output, shape, goal, checks:[...]}, ... ]}
#
# goals.json remains the fallback so an older invocation still works.
TEMPLATES_DIR = os.path.join(HERE, "templates")


def load_template(name):
    """Return (template_name, model_default, goals, wall_cap_default).

    Falls back to goals.json. `wall_cap_s` on the template is the default for
    its goals; 0 means "use the global WALL_CAP_S".
    """
    path = os.path.join(TEMPLATES_DIR, "%s.json" % name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            t = json.load(fh)
        if isinstance(t, list):                 # a bare goal list is a template too
            return name, "", t, 0
        return (t.get("name") or name, t.get("model") or "",
                t.get("goals") or [], int(t.get("wall_cap_s") or 0))
    if name in ("default", ""):
        with open(os.path.join(HERE, "goals.json"), encoding="utf-8") as fh:
            return "default", "", json.load(fh), 0
    raise SystemExit("no such template: %s (looked in %s)" % (name, TEMPLATES_DIR))


def list_templates():
    out = []
    if os.path.isdir(TEMPLATES_DIR):
        for f in sorted(os.listdir(TEMPLATES_DIR)):
            if f.endswith(".json"):
                out.append(f[:-5])
    if not out and os.path.exists(os.path.join(HERE, "goals.json")):
        out = ["default"]
    return out


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


def _preview_file(sid, path):
    """A file the run produced, via the session preview endpoint. None if absent.

    The preview server is the only view of a run's workspace that does not
    require the loop to still be alive, which is exactly when the census reads
    it - after the run has finished or been cancelled.
    """
    rel = str(path or "").lstrip("/")
    for cand in (rel, "workspace/" + rel):
        try:
            out = subprocess.run(
                ["curl", "-sk", "-m", "20", "-o", "-", "-w", "\n%{http_code}",
                 "%s/remote/sandbox/preview/%s/%s" % (BASE, sid, cand)],
                capture_output=True, text=True, timeout=30).stdout
        except Exception:
            continue
        if not out:
            continue
        body, _, code = out.rpartition("\n")
        if code.strip() == "200" and body.strip():
            return body
    return None


def _answer_text(resp):
    """The prose a run answered with - deliverable, then final, then summary.

    Checked in that order because a run that produced a deliverable has said
    its piece there; `summary` is the loop's own gloss and is the weakest
    evidence of what the user would actually read.
    """
    if not isinstance(resp, dict):
        return ""
    for key in ("deliverable", "final", "handover_output", "summary"):
        v = resp.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            for k2 in ("text", "content", "body", "markdown"):
                if isinstance(v.get(k2), str) and v[k2].strip():
                    return v[k2]
    return ""


def parse_minds(raw, label, template):
    """(minds, label) from CENSUS_MINDS / CENSUS_MINDS_LABEL; ({}, "") when no set is asked for.
    Raises ValueError on a set that is not a JSON object of strings or a label the contract refuses."""
    import re
    raw = str(raw or "").strip()
    if not raw:
        return {}, ""
    try:
        minds = json.loads(raw)
    except ValueError as e:
        raise ValueError("CENSUS_MINDS is not JSON: %s" % e)
    if not isinstance(minds, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in minds.items()):
        raise ValueError("CENSUS_MINDS must be an object of {feature_id: mode} strings")
    if not minds:
        return {}, ""
    label = str(label or "").strip()
    if not re.fullmatch(r"[a-z0-9-]{1,24}", label):
        raise ValueError("CENSUS_MINDS_LABEL must match [a-z0-9-]{1,24}")
    if label.startswith(("style-", "core-")) or label == str(template or ""):
        raise ValueError("CENSUS_MINDS_LABEL must not start with style-/core- or equal the template")
    return {k: minds[k] for k in sorted(minds)}, label


def minds_digest(minds):
    """12 hex of sha256 over the canonical sorted set (switchboard_core.digest); 'default' when empty."""
    import hashlib
    if not minds:
        return "default"
    canon = json.dumps({str(k): str(v) for k, v in sorted(minds.items())}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def series_name(template, plan_style="", intent_core="", minds_label=""):
    """<template>[-style-<s>][-core-<mode>][-<label>] (contract rule 4: existing names unchanged)."""
    series = template if not plan_style else "%s-style-%s" % (template, plan_style)
    if intent_core and intent_core != "off":
        series = "%s-core-%s" % (series, intent_core)
    if minds_label:
        series = "%s-%s" % (series, minds_label)
    return series


def mcp(name, arguments, timeout=60):
    body = json.dumps({"name": name, "arguments": arguments,
                       "caller_kind": "claude", "session_id": SESSION_ID})
    out = curl(["-X", "POST", BASE + "/mcp/call", "-H", "Content-Type: application/json",
                "-d", body], timeout=timeout)
    try:
        return json.loads(out).get("content", {})
    except Exception:
        return {}


# ── Pause / resume / drop across prod restarts ───────────────────────────────
# A prod restart kills the in-flight loop; its status derives to 'interrupted'
# once stale. Before this, a census could not survive one: the harness recorded
# the interruption as the goal's result and moved on, so restarting prod meant
# waiting hours for the set to finish. Now Vera (sys.dev.restart) or a person
# writes census.control.json and the harness honours it at its next poll:
#
#   {"pause": true,  "reason": "restart"}   cancel the current goal, wait until
#                                            prod is healthy AND pause is cleared,
#                                            then RE-RUN that goal from scratch
#   {"drop": true,   "reason": "..."}        cancel the current goal and EXIT 3;
#                                            the driver archives the partial and
#                                            stops the set
#   {"pause": true, "after_goal": true}      a YIELD (2026-09-19): finish the goal
#                                            in flight untouched, then park before
#                                            the next one exactly as a pause does.
#                                            For running other tests on the box
#                                            without tainting a row or costing a
#                                            re-run; resume lifts it as usual.
#
# A goal that ends 'interrupted' with no pause on file (a restart nobody
# announced) is re-run too, up to MAX_RERUNS. Attempts that were abandoned are
# logged and counted on the row (`reruns`), never recorded as results.
CONTROL_FILE = os.path.join(HERE, "census.control.json")
ACTIVE_FILE = os.path.join(HERE, "census.active.json")
MAX_RERUNS = 2
EXIT_DROPPED = 3


def _hold_expired(d):
    """A pause/yield with `until` in the past is over (census.control.set hours=...)."""
    until = str((d or {}).get("until") or "").strip()
    if not until or d.get("drop") or not d.get("pause"):
        return False
    try:
        import calendar as _cal
        return time.time() >= _cal.timegm(time.strptime(until[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return False


def read_control():
    try:
        with open(CONTROL_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        d = d if isinstance(d, dict) else {}
    except Exception:
        return {}
    if _hold_expired(d):
        log("  hold expired at %s - resuming (it was: %s)" % (d.get("until"), d.get("reason") or "-"))
        try:
            with open(CONTROL_FILE, "w", encoding="utf-8") as fh:
                json.dump({"pause": False, "after_goal": False, "drop": False,
                           "reason": "hold expired (%s)" % d.get("until"), "by": "harness",
                           "resume_on_start": False, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                          fh, indent=1)
        except Exception:
            pass
        return {}
    return d


# -- The census as a driver in Vera's own task system ---------------------------
# A census goal is the seeded task census-<template>-<goal>; until 2026-09-10
# its result lived only in this directory's JSONL, invisible to evolve.runs,
# the suite scoreboards and the task-through-time index unless they read the
# archive. Now every finished goal is ALSO posted to evolve.result.ingest and
# becomes a run record in the suite store (idempotent on the loop session), and
# the driver posts the archived run whole as a suite scoreboard
# (ingest_census.py). The row here stays the record of truth: a prod that
# predates the capability just logs once and carries on.
#
# For that, the run's ARCHIVE id has to be known before the driver renames the
# file at the end, so it is reserved here at start with the driver's own rule
# (census_all.sh next_n) and written to the active file, where the driver reads
# it back. Every row carries it too (`census_run`).
def reserve_census_run(template, done_rows):
    for r in done_rows:
        if isinstance(r, dict) and r.get("census_run"):
            return str(r["census_run"])          # resuming: keep the id the rows have
    import re
    t = template or "default"
    pat = re.compile(r"^census\.run(\d+)" if t == "default" else r"^census\.%s-run(\d+)" % re.escape(t))
    ns = [int(m.group(1)) for f in os.listdir(HERE) for m in [pat.match(f)] if m]
    n = (max(ns) + 1) if ns else (51 if t == "default" else 1)
    return "run%d" % n if t == "default" else "%s-run%d" % (t, n)


# `pending`: rows whose ingest found prod down (no response - a restart, a release). Roadmap E3
# (28 Sep, done 11 Oct): one silent prod restart used to switch ingest off for the rest of the
# census; now such rows are queued and retried after the next good ingest and at the end.
# Only a prod WITHOUT the capability ("unknown capability") turns ingest off.
_INGEST = {"unavailable": False, "pending": []}


def _ingest_once(rec, template, census_run):
    """One post. 'ok' | 'down' (no answer - retry later) | 'missing' (prod lacks the cap) |
    'refused' (prod answered with an error)."""
    try:
        body = json.dumps({"name": "evolve.result.ingest",
                           "arguments": {"template": template, "census_run": census_run, "row": rec},
                           "caller_kind": "claude", "session_id": SESSION_ID})
        out = curl(["-X", "POST", BASE + "/mcp/call", "-H", "Content-Type: application/json",
                    "-d", body], timeout=90)
        resp = json.loads(out) if out else {}
    except Exception as e:
        log("  ingest failed: %s" % e)
        return "down"
    res = resp.get("content") if isinstance(resp, dict) else None
    if isinstance(res, dict) and res.get("ok"):
        log("  ingested as run record %s (%s) in %s" % (
            rec.get("session_id") or "-", "added" if res.get("added") else "replaced", census_run))
        return "ok"
    if not resp:
        return "down"
    # A prod that predates the capability answers {"detail": "Unknown capability"}.
    err = str((res or {}).get("error") if isinstance(res, dict) else
              (resp.get("detail") if isinstance(resp, dict) else "") or "no response")[:160]
    log("  ingest unavailable: %s (rows stay the record; ingest_census.py can post the archive later)" % err)
    if "unknown" in err.lower() or "not available" in err.lower() or "not found" in err.lower():
        return "missing"
    return "refused"


def flush_pending():
    """Retry every queued row once; the ones still not taken stay queued."""
    left = []
    for rec, template, census_run in list(_INGEST["pending"]):
        st = _ingest_once(rec, template, census_run)
        if st == "missing":
            _INGEST["unavailable"] = True
            return
        if st == "down":
            left.append((rec, template, census_run))
    _INGEST["pending"] = left


def ingest_row(rec, template, census_run):
    """Post one finished goal to evolve.result.ingest. Best effort, never raises; a row that found
    prod down is queued and retried (E3)."""
    if _INGEST["unavailable"] or not isinstance(rec, dict) or not rec.get("id"):
        return
    st = _ingest_once(rec, template, census_run)
    if st == "missing":
        _INGEST["unavailable"] = True
    elif st == "down":
        _INGEST["pending"].append((rec, template, census_run))
        log("  ingest queued (prod did not answer); %d row(s) waiting" % len(_INGEST["pending"]))
    elif st == "ok" and _INGEST["pending"]:
        flush_pending()


_ACTIVE = {}
_ACTIVE_LOCK = threading.Lock()
_HEARTBEAT = {"started": False}
#: DEEP-21 D21-3: the active file is refreshed this often while running or parked, so a long goal or a
#: long park never reads as a dead harness (readers call it stale after control.ACTIVE_STALE_S).
HEARTBEAT_S = 60


def write_active(**fields):
    """What this harness is doing right now, for Vera and the UI to read.

    Rewritten on every state change (goal start, pause, resume, goal end, exit)
    so a reader never has to infer progress from the log."""
    with _ACTIVE_LOCK:
        _ACTIVE.update(fields)
        _ACTIVE["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _ACTIVE["pid"] = os.getpid()
        try:
            tmp = ACTIVE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(_ACTIVE, fh, indent=1)
            os.replace(tmp, ACTIVE_FILE)
        except Exception as e:
            log("  (active file not written: %s)" % e)


def _heartbeat_loop(every=HEARTBEAT_S):
    while True:
        time.sleep(every)
        if _ACTIVE.get("state") in ("running", "paused"):
            write_active()


def start_heartbeat():
    """A daemon thread that refreshes the active file while the harness runs (D21-3)."""
    t = threading.Thread(target=_heartbeat_loop, name="census-heartbeat", daemon=True)
    t.start()
    return t


def prod_healthy():
    try:
        r = subprocess.run(["curl", "-sk", "--max-time", "10", "-o", "/dev/null",
                            "-w", "%{http_code}", BASE + "/health"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() == "200"
    except Exception:
        return False


def cancel_loop(sid):
    if not sid:
        return
    mcp("dag.agent_loop.cancel", {"session_id": sid}, timeout=30)
    curl(["-X", "POST", BASE + "/workshop/agent_loop/cancel",
          "-H", "Content-Type: application/json",
          "-d", json.dumps({"session_id": sid})], timeout=30)


def wait_while_paused(goal_id):
    """Block until the pause is lifted and prod answers. Returns 'drop' if a
    drop arrived meanwhile, else 'resume'."""
    kind = "yield" if read_control().get("after_goal") else "pause"
    write_active(state="paused", current_goal=goal_id, pause_kind=kind,
                 yield_requested=False,
                 paused_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    t0 = time.time()
    while True:
        c = read_control()
        if c.get("drop"):
            return "drop"
        if not c.get("pause") and prod_healthy():
            log("  resumed after %.0fs paused (%s)" % (time.time() - t0, kind))
            write_active(state="running", paused_at="", pause_kind="")
            return "resume"
        time.sleep(15)


# ── Effective LLM routing, recorded per goal ─────────────────────────────────
# A census row without its routing is a number with no instrument attached.
# Census run 51 (2026-09-10) read as a collapse of the loop and was in fact the
# coder model 80% VRAM-resident on the GPU node: 0.5 tok/s for every authoring
# call. Nothing on the row said so. Now each row carries the routing that was in
# force when the goal started and every LLM call the goal made - node, model,
# rule that chose it, tokens/sec, GPU residency - with the re-routes named.
def routing_snapshot():
    """The loop profile's effective roles at this moment, with the user layer
    called out - the declared coder and the coder actually in force differ
    exactly when someone has overridden the Model Routing page."""
    d = mcp("ollama.role_profiles.get", {}, timeout=30) or {}
    eff = ((d.get("effective") or {}).get("loop") or {}).get("roles") or {}
    usr = ((d.get("user") or {}).get("loop") or {}).get("roles") or {}
    dec = ((d.get("declared") or {}).get("loop") or {}).get("roles") or {}
    roles = {}
    for r, v in eff.items():
        roles[r] = {"model": v.get("model") or "", "prefer_gpu": bool(v.get("prefer_gpu")),
                    "deny_gpu": bool(v.get("deny_gpu")), "pin": v.get("pin") or "",
                    "overridden": r in usr,
                    "declared_model": (dec.get(r) or {}).get("model") or ""}
    return {"profile": "loop", "roles": roles, "user_overrides": sorted(usr),
            "nodes": {n.get("node"): {"has_gpu": n.get("has_gpu")}
                      for n in (mcp("ollama.gate.status", {}, timeout=30).get("nodes") or [])}}


# ── Which code a goal ran on ─────────────────────────────────────────────────
# A census that crosses a prod restart has run on two different codebases,
# and a row that does not say which one it ran on cannot be compared with
# anything. Every row records prod's git sha + branch (obs.provenance) at the
# goal's start and, if it differs, at its end - so a restart mid-run is
# visible on the exact goal where it happened, not inferred from timestamps.
def code_snapshot():
    p = mcp("obs.provenance", {}, timeout=30) or {}
    if not p.get("git_sha"):
        return {}
    return {"sha": p.get("git_sha") or "", "sha_short": p.get("git_sha_short") or "",
            "branch": p.get("branch") or "", "dirty": bool(p.get("dirty")),
            "process_started_at": p.get("started_at") or "", "pid": p.get("pid")}


def code_record(start, end):
    """The row's `code` field. `changed_during_goal` is the restart tell."""
    rec = dict(start or {})
    if end and start and (end.get("sha") != start.get("sha")
                          or end.get("process_started_at") != start.get("process_started_at")):
        rec["at_end"] = end
        rec["changed_during_goal"] = True
    elif end and not start:
        rec = dict(end); rec["seen_at"] = "end"
    else:
        rec["changed_during_goal"] = False
    return rec


# Phrases the router writes ONLY when a call did not go where its rule would
# have put it (capability_orchestration route notes). An ordinary placement
# reads "job-type rule 'chat' (deny_gpu=False, ...); prefer_gpu + model: picked
# 'gpu-250'" - so a bare "deny_gpu" or "busy" matched every GPU call and every
# row said reroutes=N for N calls (rows before 2026-09-10T16:45Z; fix_reroutes.py
# recomputes archived rows from the same text).
_REROUTE_WORDS = ("demoted", "fallback", "failover", "spill", "deny_gpu: gpu nodes excluded",
                  "deny_gpu=true", "escalat", "unreachable", "offline", "no gpu has the model",
                  "model nowhere", "alternatives busy", "no confirmed-online", "no online",
                  "pinned model missing", "retry")


def backend_label(e):
    """ollama | vllm | api:<provider> | other[:<what>] (vera/census/control.py)."""
    be = str(e.get("backend") or "").strip()
    inst = str(e.get("instance") or "")
    prov = str(e.get("provider") or "").strip()
    if not be:
        be = "api" if inst.startswith("provider:") else ("vllm" if inst.startswith("vllm") else "ollama")
    if be == "api":
        return "api:" + (prov or inst.split(":", 1)[-1] or "?")
    if be == "other" and prov:
        return "other:" + prov
    return be


def routing_calls(t_start, t_end):
    """Every LLM call prod logged in [t_start, t_end], from the in-process
    request log. The census holds the box alone (wait_for_free), so a time
    window is the goal. Truncation is reported, never hidden."""
    d = mcp("ollama.request_log", {"limit": 500}, timeout=60) or {}
    entries = d.get("entries") or []
    lo = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start - 2))
    hi = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_end + 2))
    calls, reroutes, nodes, models, backends, spill = [], [], {}, {}, {}, 0
    oldest = None
    for e in entries:
        ts = str(e.get("ts") or "")[:19]
        if oldest is None or ts < oldest:
            oldest = ts
        if not (lo <= ts <= hi):
            continue
        why = []
        rr = str(e.get("route_reason") or "")
        if e.get("escalated"):
            why.append("escalated")
        if e.get("cpu_spill"):
            why.append("cpu spill (GPU %s%% resident)" % e.get("gpu_resident_pct"))
        if any(w in rr.lower() for w in _REROUTE_WORDS):
            why.append(rr[:160])
        c = {"ts": ts, "caller": "%s:%s" % (e.get("caller_file"), e.get("caller_func")),
             "job": e.get("job_type"), "role": e.get("role") or "", "rule": e.get("rule_source"),
             "model": e.get("model"), "node": e.get("instance"), "status": e.get("status"),
             "backend": backend_label(e),
             "s": e.get("elapsed_s"), "tok_s": e.get("tok_per_s"),
             "gpu_pct": e.get("gpu_resident_pct"), "num_ctx": e.get("num_ctx"),
             "prompt_chars": e.get("prompt_chars")}
        if why:
            c["reroute"] = "; ".join(why)
            reroutes.append(c)
        calls.append(c)
        nodes[c["node"]] = nodes.get(c["node"], 0) + 1
        models[c["model"]] = models.get(c["model"], 0) + 1
        backends[c["backend"]] = backends.get(c["backend"], 0) + 1
        if e.get("cpu_spill"):
            spill += 1
    calls.sort(key=lambda c: c["ts"])
    return {"calls": calls, "reroutes": reroutes, "nodes": nodes, "models": models,
            "backends": backends,
            "spill_calls": spill, "n": len(calls),
            "truncated": bool(oldest and oldest > lo),
            "log_total": d.get("total")}


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def gate_windows(pipe, now=None):
    """[(start, end)] epoch seconds of a pipeline's critical tier: its first test-pool (or
    critical-queue) step to its critical-tests step; still running = until `now`."""
    steps = pipe.get("steps") or []
    st = next((_epoch(x.get("ts")) for x in steps
               if x.get("stage") in ("test-pool", "critical-queue", "critical-tier")), None)
    if st is None:
        return []
    en = next((_epoch(x.get("ts")) for x in steps if x.get("stage") == "critical-tests"), None)
    if en is None:
        if str(pipe.get("ended_at") or ""):
            return []
        en = now if now is not None else time.time()
    return [(st, max(st, en))]


def overlap_seconds(windows, t0, t1):
    """Seconds of [t0, t1] covered by any of `windows` (overlapping windows counted once)."""
    cut = sorted((max(a, t0), min(b, t1)) for a, b in windows if min(b, t1) > max(a, t0))
    total, cur_a, cur_b = 0, None, None
    for a, b in cut:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return int(total)


def gate_overlap(t0, t1):
    """{gate_overlap_s, gates}: how long a pipeline gate's test tier ran while this goal ran
    (compute-roles K4, owner 11 Oct 2026). A gate shares the nodes' CPU with the census's
    inference, so wall-time comparisons flag such rows; quality checks do not depend on it.
    None when prod could not say."""
    d = mcp("evolve.pipeline.list", {"limit": 40}, timeout=30) or {}
    pipes = d.get("pipelines") if isinstance(d, dict) else None
    if not isinstance(pipes, list):
        return None
    wins, ids = [], []
    for p in pipes:
        c = _epoch(p.get("created_at"))
        if c is None or c > t1 or c < t0 - 3600:
            continue
        full = (mcp("evolve.pipeline.get", {"id": p.get("id")}, timeout=30) or {}).get("pipeline") or {}
        w = gate_windows(full, now=t1)
        if w and overlap_seconds(w, t0, t1):
            wins += w
            ids.append(p.get("id"))
    return {"gate_overlap_s": overlap_seconds(wins, t0, t1), "gates": ids}


def box_is_busy():
    """True if anyone else is mid-generation or running a loop."""
    g = mcp("ollama.gate.status", {}, timeout=30)
    for n in (g.get("nodes") or []):
        if n.get("gated") and (n.get("held") or 0) > 0:
            return "gate held by %s" % (n.get("owners") or "?")
    out = curl([BASE + "/workshop/agent_loop/sessions?status=running"], timeout=30)
    try:
        if json.loads(out).get("count", 0) > 0:
            return "another loop is running"
    except Exception:
        pass
    return ""


def wait_for_free(max_wait=3600):
    """Wait for the GPU gate / other loops to clear. Reads the control file
    each turn: a pause written while we wait was not seen for up to an hour
    (2026-09-12: paused by the user at 18:46Z, the harness sat in this loop
    reporting 'running' with no goal to cancel), a drop likewise.
    Returns True (free), 'paused' (a pause is on file - the caller parks), or
    False (still busy at max_wait); raises Dropped on a drop."""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        c = read_control()
        if c.get("drop"):
            raise Dropped(c.get("reason") or "drop requested")
        if c.get("pause"):
            return "paused"
        why = box_is_busy()
        if not why:
            return True
        log("  waiting: %s" % why)
        time.sleep(30)
    return False


def newest_session(before):
    out = curl([BASE + "/workshop/agent_loop/sessions?limit=50"], timeout=30)
    try:
        ss = json.loads(out).get("sessions", [])
    except Exception:
        return None
    for s in ss:
        if s.get("session_id") not in before:
            return s.get("session_id")
    return None


def session_status(sid):
    out = curl([BASE + "/workshop/agent_loop/sessions?limit=50"], timeout=30)
    try:
        for s in json.loads(out).get("sessions", []):
            if s.get("session_id") == sid:
                return s.get("status")
    except Exception:
        pass
    return None


def event_count(sid):
    """How many events the session has emitted. A rising count is proof of life
    that does not depend on the status field being right."""
    tr = mcp("workshop.agent_loop.trace", {"session_id": sid}, timeout=60)
    try:
        return int((tr.get("counters") or {}).get("events") or
                   len(tr.get("steps") or []))
    except Exception:
        return -1


def stall_dump(sid, goal_id, quiet_s):
    """Write prod's thread stacks and asyncio await chains to census_stalls/; log where."""
    d = os.path.join(HERE, "census_stalls")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s-%s-%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), goal_id, sid[:8]))
    parts = ["goal %s session %s: no loop events for %ds" % (goal_id, sid, quiet_s)]
    try:
        pid = subprocess.run("ss -ltnp 'sport = :8999' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2",
                             shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
        spy = os.path.join(os.path.dirname(sys.executable), "py-spy")
        if pid and os.path.exists(spy):
            r = subprocess.run([spy, "dump", "--pid", pid], capture_output=True, text=True, timeout=90)
            parts.append("=== py-spy dump (pid %s) ===\n%s%s" % (pid, r.stdout, r.stderr[-2000:]))
        else:
            parts.append("=== py-spy unavailable (pid=%r) ===" % pid)
    except Exception as e:
        parts.append("=== py-spy failed: %s ===" % e)
    try:
        tasks = mcp("sys.debug.tasks", {"limit": 60}, timeout=60) or {}
        parts.append("=== sys.debug.tasks ===\n" + json.dumps(tasks, indent=1, default=str)[:400000])
    except Exception as e:
        parts.append("=== sys.debug.tasks failed: %s ===" % e)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n\n".join(parts))
        log("  STALL: no loop events for %ds - stacks written to %s" % (quiet_s, path))
    except Exception as e:
        log("  STALL: no loop events for %ds - dump failed: %s" % (quiet_s, e))


def confirm_terminal(sid, status, settle_s=45):
    """Believe a terminal status only if the run has actually stopped.

    Census 23 recorded build-browser-verified as 'interrupted' at 1381s while it
    was mid-operator.run and had 100 more minutes of work; the runner moved on
    and the abandoned loop held the GPU for every goal that followed. The status
    was derived from an activity marker that only moves when an event is
    emitted, so one long tool call looked identical to a dead process.

    Cheap corroboration: wait, then check the status again AND whether the event
    log advanced. Either still-running signal means the terminal reading was
    wrong. Costs one settle per goal, only on a terminal reading.
    """
    before = event_count(sid)
    time.sleep(settle_s)
    again = session_status(sid)
    after = event_count(sid)
    if again not in ("done", "error", "cancelled", "failed", "interrupted"):
        log("  status '%s' was premature - now '%s', still running" % (status, again))
        return False
    if after > before >= 0:
        log("  status '%s' but the event log advanced %d -> %d - still running"
            % (status, before, after))
        return False
    return True


class Dropped(Exception):
    """A drop arrived: stop the census without recording the current goal."""


def restart_check(code_start, snap, seen_t, seen_ev, n_ev, now, grace=None):
    """Did prod restart under the goal, and is its loop gone? {seen: t, process, dead}.
    `snap` is called for prod's current process (code_snapshot); a prod that cannot be
    read (restarting) says nothing yet. Dead = restarted AND no new loop event for
    `grace` seconds since the restart was seen (a loop that somehow survived would
    show events). Pure apart from `snap`."""
    grace = RESTART_GRACE_S if grace is None else grace
    was = (code_start or {}).get("process_started_at") or ""
    if not was:
        return {}
    if seen_t is not None:
        if n_ev is None or n_ev < 0 or seen_ev is None or seen_ev < 0:
            return {}                              # events unreadable: unknown, not dead
        if n_ev != seen_ev:
            return {}                              # events after the restart: alive
        return {"dead": True} if now - seen_t >= grace else {}
    try:
        cur = (snap() or {}).get("process_started_at") or ""
    except Exception:
        cur = ""
    if cur and cur != was:
        return {"seen": now, "process": cur}
    return {}


def run_goal(g, model="", template="", wall_cap_default=0):
    """Run one goal, re-running it after a pause or an unannounced interruption.
    Returns the row; raises Dropped."""
    reruns = []
    while True:
        c = read_control()
        if c.get("drop"):
            raise Dropped(c.get("reason") or "drop requested")
        if c.get("pause"):
            log("  %s before %s: %s" % ("yield: parked" if c.get("after_goal") else "paused",
                                        g["id"], c.get("reason") or ""))
            if wait_while_paused(g["id"]) == "drop":
                raise Dropped(read_control().get("reason") or "drop requested")
        rec = _run_goal_once(g, model=model, template=template,
                             wall_cap_default=wall_cap_default)
        if rec is None or rec.get("_rerun"):
            why = (rec or {}).get("_rerun") or "paused"
            if why == "paused before start":
                continue      # nothing was attempted: not an abandoned run
            reruns.append({"why": why, "session_id": (rec or {}).get("session_id"),
                           "wall_s": (rec or {}).get("wall_s")})
            if len(reruns) > MAX_RERUNS:
                log("  giving up on %s after %d abandoned attempts" % (g["id"], len(reruns)))
                rec = {"id": g["id"], "status": "abandoned", "reruns": reruns,
                       **{k: g[k] for k in ("intent", "tier", "output", "shape")},
                       "template": template, "wall_cap_s": goal_wall_cap(g, wall_cap_default),
                       "provenance": provenance()}
                return rec
            log("  re-running %s (attempt %d, previous: %s)" % (g["id"], len(reruns) + 1, why))
            continue
        if reruns:
            rec["reruns"] = reruns
        return rec


def _run_goal_once(g, model="", template="", wall_cap_default=0):
    wall_cap = goal_wall_cap(g, wall_cap_default)
    log("== %s (%s/%s)%s ==" % (g["id"], g["intent"], g["tier"],
                                "" if wall_cap == WALL_CAP_S
                                else "  [cap %ds]" % wall_cap))
    if not _HEARTBEAT.get("started"):
        _HEARTBEAT["started"] = bool(start_heartbeat())
    write_active(state="running", current_goal=g["id"], session_id="",
                 goal_started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 wall_cap_s=wall_cap)
    free = wait_for_free()
    if free == "paused":
        # run_goal's loop reads the control file next and parks in
        # wait_while_paused (which writes state=paused - the ack a restart
        # waits for); the goal re-runs from scratch once resumed.
        log("  paused while waiting for the box: %s" % (read_control().get("reason") or ""))
        return {"_rerun": "paused before start", "session_id": "", "wall_s": 0}
    if not free:
        log("  SKIPPED - box stayed busy")
        return {"id": g["id"], "skipped": "box busy"}
    routing = {"at_start": routing_snapshot()}
    code_start = code_snapshot()

    out = curl([BASE + "/workshop/agent_loop/sessions?limit=50"], timeout=30)
    try:
        before = {s.get("session_id") for s in json.loads(out).get("sessions", [])}
    except Exception:
        before = set()

    # An explicit model overrides the cluster's routing for this run. Left out
    # entirely when blank, so the default census still exercises whatever the
    # live Model Routing says - which is the configuration prod actually uses.
    _args = {"goal": g["goal"]}
    # the decision ledger's origin for every decision this run records (FM-0.2a)
    _args["decision_origin"] = "census"
    if model:
        _args["model"] = model
    # A census run is a measurement, not a goal to keep. v7 reads a long goal
    # as STRATEGIC and hands it to a V8 loop program that runs for days on
    # prod's GPU (markets-symbol-series did, 2026-09-11: five hours, four
    # templates lost). Off here, and the engine refuses it for a census-owned
    # session regardless (census_owns_session_sync).
    _args["enable_dream_persistence"] = False
    # Planning style: forced for the whole census, else the goal's own, else the
    # loop's default. Recorded on the row as asked; the trace says what RAN.
    _style = PLAN_STYLE or str(g.get("plan_style") or "").strip().lower()
    if _style:
        _args["plan_style"] = _style
        log("  plan_style %s" % _style)
    _core = INTENT_CORE or str(g.get("intent_core") or "").strip().lower()
    if _core and _core != "off":
        _args["intent_core"] = _core
        log("  intent_core %s" % _core)
    if MINDS:                                   # a census arm: only when one is asked for
        _args["minds"] = dict(MINDS)
    # A goal that names a loop PROFILE (the specialist-loops template: "each
    # run by that profile rather than by planning") runs through loops.run,
    # which resolves the profile's engine, agent and toolkit; every other goal
    # is the bare v7 loop the default series has always measured. Until
    # 2026-09-10 the profile field was read by nobody, so that template
    # measured the generic planner under six different names.
    _profile = str(g.get("profile") or "").strip()
    _cap = "dag.agent_loop_v7"
    if _profile and _profile != "planning":
        _cap = "loops.run"
        _args["profile"] = _profile
    body = json.dumps({"name": _cap,
                       "arguments": _args,
                       "caller_kind": "claude", "session_id": SESSION_ID})
    if _cap == "loops.run":
        log("  profile %s via loops.run" % _profile)
    t0 = time.time()
    resp_path = os.path.join(HERE, "resp_%s.json" % g["id"])
    proc = subprocess.Popen(
        ["curl", "-sk", "--max-time", str(wall_cap + 120), "-X", "POST",
         BASE + "/mcp/call", "-H", "Content-Type: application/json", "-d", body,
         "-o", resp_path, "-w", "%{http_code}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    sid = None
    for _ in range(20):
        time.sleep(5)
        sid = newest_session(before)
        if sid:
            break
    if not sid:
        # Do not guess. Read what the cap actually said - a missing session is
        # either the trivial-chat gate declining to plan, a cap error, or a wedged
        # loop subsystem, and those need different fixes.
        detail, http = "", ""
        try:
            http = (proc.communicate(timeout=180)[0] or "").strip()
        except Exception:
            proc.kill()
        try:
            with open(resp_path, encoding="utf-8") as fh:
                detail = fh.read()[:600]
        except Exception:
            detail = "(no response body)"
        log("  no session appeared - http=%s body=%s" % (http, detail[:300]))
        return {"id": g["id"], "no_session": True, "wall_s": round(time.time() - t0, 1),
                "http_code": http, "response": detail,
                **{k: g[k] for k in ("intent", "tier", "output", "shape")}}

    log("  session %s" % sid[:12])
    write_active(session_id=sid)
    status, cancelled = None, False
    # The control file is read every CONTROL_POLL_S, the session status every
    # POLL_S. sys.dev.restart writes a pause ~10s before the new process lifts
    # it on startup; at a 15s poll the harness missed that window twice on
    # 2026-09-10 and sat 30 minutes on a loop the restart had killed, headed
    # for a false wall-cap. A local file read every 3s costs nothing.
    next_status = 0.0
    yielded = False
    last_ev, last_ev_t, next_ev_check, stall_dumped = None, time.time(), 0.0, False
    restart_seen_t, restart_ev = None, None
    while time.time() - t0 < wall_cap:
        time.sleep(CONTROL_POLL_S)
        c = read_control()
        if c.get("drop"):
            log("  DROP requested (%s) - cancelling %s" % (c.get("reason") or "", sid[:12]))
            cancel_loop(sid)
            try: proc.kill()
            except Exception: pass
            raise Dropped(c.get("reason") or "drop requested")
        if c.get("pause") and c.get("after_goal"):
            # A yield: someone wants the box BETWEEN goals, not now. This goal
            # runs to its end untouched (the row stays a measurement) and
            # run_goal parks before the next one, where a restart's ack and
            # the UI already look for state=paused.
            if not yielded:
                yielded = True
                log("  YIELD requested (%s) - finishing %s, parking after it"
                    % (c.get("reason") or "", g["id"]))
                write_active(yield_requested=True)
        elif c.get("pause"):
            log("  PAUSE requested (%s) - cancelling %s, will re-run" % (c.get("reason") or "", sid[:12]))
            cancel_loop(sid)
            try: proc.kill()
            except Exception: pass
            if wait_while_paused(g["id"]) == "drop":
                raise Dropped(read_control().get("reason") or "drop requested")
            return {"_rerun": "paused: %s" % (c.get("reason") or ""), "session_id": sid,
                    "wall_s": round(time.time() - t0, 1)}
        if time.time() < next_status:
            continue
        next_status = time.time() + POLL_S
        status = session_status(sid)
        # Stall watchdog - see STALL_DUMP_S. Diagnostic only.
        if time.time() >= next_ev_check:
            next_ev_check = time.time() + STALL_CHECK_S
            try:
                n_ev = event_count(sid)
            except Exception:
                n_ev = -1
            if n_ev >= 0:
                if n_ev != last_ev:
                    last_ev, last_ev_t = n_ev, time.time()
                elif not stall_dumped and time.time() - last_ev_t >= STALL_DUMP_S:
                    stall_dumped = True
                    try:
                        stall_dump(sid, g["id"], int(time.time() - last_ev_t))
                    except Exception as _se:
                        log("  STALL: dump error %s" % _se)
            # Prod restarted under this goal? Its loop died with the old process.
            _restart = restart_check(code_start, code_snapshot, restart_seen_t, restart_ev,
                                     n_ev, time.time())
            if _restart.get("seen") and restart_seen_t is None:
                restart_seen_t, restart_ev = _restart["seen"], n_ev
                log("  prod restarted under this goal (process %s) - the loop has %ds to show "
                    "it is alive" % (_restart.get("process") or "?", RESTART_GRACE_S))
            if _restart.get("dead"):
                log("  loop gone with the restart (no events for %ds after it) - cancelling "
                    "%s and re-running" % (RESTART_GRACE_S, sid[:12]))
                cancel_loop(sid)                  # the orphan record must not stay "running"
                try:
                    proc.kill()
                except Exception:
                    pass
                status = "interrupted"
                break
        if status in ("done", "error", "cancelled", "failed", "interrupted"):
            # Do not take a terminal reading at face value - see confirm_terminal.
            if confirm_terminal(sid, status):
                break
            status = "running"
    else:
        log("  WALL CAP hit - cancelling")
        mcp("dag.agent_loop.cancel", {"session_id": sid}, timeout=30)
        curl(["-X", "POST", BASE + "/workshop/agent_loop/cancel",
              "-H", "Content-Type: application/json",
              "-d", json.dumps({"session_id": sid})], timeout=30)
        cancelled = True
        status = "wall-cap"

    wall = round(time.time() - t0, 1)
    try: proc.wait(timeout=60)
    except Exception: proc.kill()

    # An interruption nobody announced (a restart without a pause on file): the
    # loop died under us. That is not the goal's result - re-run it.
    if status == "interrupted" and not cancelled:
        log("  interrupted after %ss (prod restarted?) - waiting for prod, then re-running" % wall)
        while not prod_healthy():
            time.sleep(15)
        time.sleep(30)
        return {"_rerun": "interrupted", "session_id": sid, "wall_s": wall}

    try:
        routing["calls"] = routing_calls(t0, time.time())
    except Exception as e:
        routing["calls"] = {"error": str(e)[:200]}
    code = code_record(code_start, code_snapshot())

    # ── QUALITY ────────────────────────────────────────────────────────────
    # Completion is not quality. A run can finish having written a clock.html
    # with no clock in it and the census would call that a pass. Score the
    # goal's DECLARED expectations against what it actually produced.
    quality = {"score": None, "passed": 0, "failed": 0, "total": 0, "results": []}
    try:
        sys.path.insert(0, os.path.abspath(os.path.join(CODE, "..", "..")))   # the repo root
        from vera.census.quality import evaluate as _q_eval, files_wanted as _q_files
        wanted = _q_files(g.get("checks"))
        got = {}
        for fname in wanted:
            body = _preview_file(sid, fname)
            if body is not None:
                got[fname] = body
        _resp_obj = {}
        try:
            with open(resp_path, encoding="utf-8") as _fh:
                _raw = json.load(_fh)
            _resp_obj = _raw.get("content", _raw) if isinstance(_raw, dict) else {}
        except Exception:
            _resp_obj = {}
        answer = _answer_text(_resp_obj)
        # the tools the run called (trace steps' calls) - for `tool_called` checks
        _tools = None
        if any("tool_called" in (c or {}) for c in (g.get("checks") or [])):
            try:
                _tr0 = mcp("workshop.agent_loop.trace", {"session_id": sid}, timeout=120) or {}
                _tools = [str(c.get("tool")) for s in (_tr0.get("steps") or []) for c in (s.get("calls") or [])
                          if c.get("tool")]
            except Exception:
                _tools = None
        quality = _q_eval(g.get("checks"), got, answer, tools=_tools)
        quality["files_found"] = sorted(got)
        quality["files_missing"] = [f for f in wanted if f not in got]
    except Exception as e:
        quality["error"] = str(e)[:200]

    tr = mcp("workshop.agent_loop.trace", {"session_id": sid}, timeout=120)
    co = tr.get("counters", {}) or {}
    try:
        _gate_ov = gate_overlap(t0, time.time())
    except Exception:
        _gate_ov = None
    rec = {
        "id": g["id"], "session_id": sid, "status": status, "wall_s": wall,
        "cancelled": cancelled,
        # When, in UTC. Rows had no timestamp until 2026-09-10; a task's history
        # across runs needs one, and the archive's mtime is not it.
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # a pipeline gate's tests ran beside this goal for this long (K4): its wall time is
        # not comparable with a quiet run's; None = unknown
        "gate_overlap": _gate_ov,
        "intent": g["intent"], "tier": g["tier"], "output": g["output"], "shape": g["shape"],
        "planned": co.get("planned_steps"), "executed": co.get("executed_steps"),
        "inserted": co.get("inserted_steps"), "tool_calls": co.get("tool_calls"),
        # Added 2026-08-30. Until L9 the counters could not be trusted: a
        # completion-gate follow_up executed without being counted as an
        # insertion, and the fast path ran a step with no plan. `unaccounted`
        # is the one that matters - anything above 0 means a step ran that no
        # producer claims, and every counter-based reading of that run is unsafe.
        "controller_inserted": co.get("controller_inserted_steps"),
        "gate_inserted": co.get("gate_inserted_steps"),
        "fast_path": co.get("fast_path_steps"),
        "unaccounted": co.get("unaccounted_steps"),
        "unattributed_calls": co.get("unattributed_calls"),
        "cycles_per_step": co.get("cycles_per_step"),
        "stage_calls": co.get("stage_calls"),
        "plan_tier": (tr.get("plan") or {}).get("tier"),
        # plan.tier_lower (FM-4.3): on, and whether it changed this goal's tier - an arm whose goals
        # never qualify is otherwise blind (12 Oct 2026)
        "tier_lower": (tr.get("plan") or {}).get("tier_lower"),
        "tier_lower_applied": (tr.get("plan") or {}).get("tier_lower_applied"),
        "plan_intent": (tr.get("plan") or {}).get("intent"),
        # Planning style: what this census ASKED for, and what the loop says it
        # USED (they differ on a fallback). A row from a loop that predates
        # styles has no plan_style - read that as auto, not as unknown.
        # "default" = no style forced: the loop's own default (v7: stepwise-reviewed
        # since 2026-10-02). `plan_style` below is what actually RAN.
        "plan_style_requested": _style or "default",
        "intent_core_requested": _core or "off",
        # the switchboard set this arm asked for (contract rule 5): {} / "default" / "" off-arm
        "minds_requested": dict(MINDS),
        "minds_digest": MINDS_DIGEST,
        "minds_label": MINDS_LABEL if MINDS else "",
        "plan_style": (tr.get("plan") or {}).get("style"),
        "plan_style_reason": (tr.get("plan") or {}).get("style_reason"),
        "stages": {s.get("stage"): s.get("calls") for s in (tr.get("stages") or [])},
        "warnings": (tr.get("warnings") or [])[:12],
        "quality": quality,
        # WHAT was asked and by WHICH model. Without these a score cannot be
        # compared across runs - the same number means different things when the
        # goal set or the model has moved underneath it.
        "template": template,
        "model": model or "(routing default)",
        # The loop profile the goal named, when it named one: those rows ran
        # through loops.run (the specialist's engine, agent and toolkit), not
        # the bare v7 loop, and are a different instrument.
        "profile": _profile or "",
        "runner": _cap,
        # The cap that APPLIED to this goal. A wall-cap row means nothing
        # without it once templates set their own: 1805s is a timeout under an
        # 1800s cap and a healthy finish under a 3600s one.
        "wall_cap_s": wall_cap,
        # WHO ran it and WHAT drove them, so a run in Loop Lab is attributable
        # to an operator, a skill and a tool - and, through caller_session, to
        # the ingested transcript that explains it.
        "provenance": provenance(),
        # WHICH node and model served every call, under which rule, at what
        # speed - and where the router moved a call away from the role's
        # nominal choice. See routing_snapshot / routing_calls.
        "routing": routing,
        # WHICH CODE: prod's sha + branch at the goal's start (and end, when a
        # restart changed it mid-goal), plus the version the loop's own events
        # were stamped with. A run that crossed a restart is two instruments.
        "code": code,
        "code_version": co.get("code_version"),
    }
    if code.get("changed_during_goal"):
        log("  CODE CHANGED during this goal: %s@%s -> %s@%s" % (
            code.get("sha_short"), code.get("branch"),
            (code.get("at_end") or {}).get("sha_short"), (code.get("at_end") or {}).get("branch")))
    _rc = routing.get("calls") or {}
    if _rc.get("n"):
        _coder = (routing.get("at_start") or {}).get("roles", {}).get("coder", {}).get("model") or "-"
        log("  routing: %d calls | nodes=%s | models=%s | reroutes=%d spill=%d | coder=%s%s"
            % (_rc["n"], _rc.get("nodes"), _rc.get("models"), len(_rc.get("reroutes") or []),
               _rc.get("spill_calls", 0), _coder, "  (log truncated)" if _rc.get("truncated") else ""))
    extra = ""
    if rec.get("gate_inserted"):
        extra += " gate=%s" % rec["gate_inserted"]
    if rec.get("fast_path"):
        extra += " fast=%s" % rec["fast_path"]
    if rec.get("unaccounted"):
        extra += "  *** UNACCOUNTED=%s ***" % rec["unaccounted"]
    log("  -> %s in %ss | planned=%s executed=%s inserted=%s%s cycles=%s warns=%d" % (
        status, wall, rec["planned"], rec["executed"], rec["inserted"], extra,
        rec["cycles_per_step"], len(rec["warnings"])))
    return rec


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Run one census: a TEMPLATE of goals against a MODEL.")
    ap.add_argument("--template", default=os.getenv("CENSUS_TEMPLATE", "default"),
                    help="name under templates/ (default: %(default)s)")
    ap.add_argument("--model", default=os.getenv("CENSUS_MODEL", ""),
                    help="ollama model for the loop; blank = live Model Routing")
    ap.add_argument("--list", action="store_true", help="list templates and exit")
    ap.add_argument("--plan-style", default=PLAN_STYLE, dest="plan_style",
                    help="force a loop planning style on every goal: auto|flat|stepwise|"
                         "detailed|broad (blank = each goal's own). Runs as its own series.")
    ap.add_argument("--intent-core", default=INTENT_CORE, dest="intent_core",
                    help="off|order: the loop's intent core on every goal (blank = each "
                         "goal's own, else off). A forced core runs as its own series.")
    # Provenance. Defaults come from the environment so a scheduled or
    # Vera-triggered run can set them once without rewriting the command.
    ap.add_argument("--operator", default=OPERATOR,
                    help="who is running this: claude / codex / user / a name")
    ap.add_argument("--skill", default=SKILL,
                    help="the skill driving it, e.g. improve-vera-sandboxed")
    ap.add_argument("--tool", default=TOOL,
                    help="what drove it, e.g. /loop, manual, sched")
    ap.add_argument("--session-id", default=SESSION_ID, dest="session_id",
                    help="the REAL caller session id - joins this run to its "
                         "ingested transcript via ide.claude_sessions.*")
    args = ap.parse_args()
    globals()["OPERATOR"] = args.operator
    globals()["SKILL"] = args.skill
    globals()["TOOL"] = args.tool
    globals()["SESSION_ID"] = args.session_id
    globals()["PLAN_STYLE"] = str(args.plan_style or "").strip().lower()
    globals()["INTENT_CORE"] = str(args.intent_core or "").strip().lower()

    if args.list:
        for t in list_templates():
            print(t)
        return

    template, tmpl_model, goals, tmpl_cap = load_template(args.template)
    # A census arm: parse, then validate against the build under test BEFORE goal 1 (rule 3).
    try:
        _minds, _label = parse_minds(MINDS_RAW, MINDS_LABEL, template)
    except ValueError as e:
        log("census refused: %s" % e)
        sys.exit(2)
    globals()["MINDS"], globals()["MINDS_LABEL"] = _minds, _label
    globals()["MINDS_DIGEST"] = minds_digest(_minds)
    if _minds:
        _v = mcp("minds.validate", {"minds": _minds, "census": True}, timeout=30)
        if not (isinstance(_v, dict) and _v.get("ok") is True):
            log("census refused: minds %s not valid on %s: %s" % (json.dumps(_minds), BASE, json.dumps(_v)[:300]))
            sys.exit(2)
        log("  minds arm %s: %s (digest %s)" % (_label, json.dumps(_minds), MINDS_DIGEST))
    # An explicit --model beats the template's own default.
    model = args.model or tmpl_model

    done = set()
    done_rows = []
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                _r = json.loads(line); done.add(_r["id"]); done_rows.append(_r)
            except Exception: pass
    # A forced planning style is its own series: numbered and ingested apart from
    # the template's baseline runs (the goal set - `template` on each row - is
    # unchanged, so rows still compare goal by goal).
    series = series_name(template, PLAN_STYLE, INTENT_CORE, MINDS_LABEL if MINDS else "")
    census_run = reserve_census_run(series, done_rows)
    log("census start: template=%s model=%s plan_style=%s | %d goals, %d already done | will archive as %s"
        % (template, model or "(routing default)", PLAN_STYLE or "(per goal / auto)",
           len(goals), len(done), census_run))
    log("  operated by: %s | skill=%s tool=%s | caller_session=%s"
        % (OPERATOR or "(unattributed)", SKILL or "-", TOOL or "-", SESSION_ID))
    for h in load_helpers():
        log("  helper: %s - %s" % (h["name"], h["purpose"][:110]))
    _code0 = code_snapshot()
    log("  code: %s@%s%s (process up since %s)" % (
        _code0.get("sha_short") or "?", _code0.get("branch") or "?",
        " DIRTY" if _code0.get("dirty") else "", _code0.get("process_started_at") or "?"))
    write_active(template=template, model=model or "(routing default)",
                 code=_code0, census_run=census_run,
                 goals_total=len(goals), goals_done=len(done), state="running",
                 started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 operator=OPERATOR, skill=SKILL, tool=TOOL, caller_session=SESSION_ID,
                 goal_ids=[g["id"] for g in goals], current_goal="", session_id="")
    for g in goals:
        if g["id"] in done:
            log("skip %s (already recorded)" % g["id"]); continue
        try:
            rec = run_goal(g, model=model, template=template,
                           wall_cap_default=tmpl_cap)
            if rec.get("cancelled"):
                # A cancel is known-unreliable here; every goal after the first
                # wall-cap previously failed to start. Give the subsystem a moment
                # and record whether it actually came back.
                time.sleep(30)
                log("  post-cancel: box_busy=%r" % (box_is_busy() or "free"))
        except Dropped as e:
            log("DROPPED: %s - nothing recorded for %s; exiting %d" % (e, g["id"], EXIT_DROPPED))
            write_active(state="dropped", current_goal=g["id"], session_id="",
                         dropped_reason=str(e)[:200])
            raise SystemExit(EXIT_DROPPED)
        except Exception as e:
            rec = {"id": g["id"], "error": str(e)[:200]}
            log("  ERROR %s" % e)
        rec.setdefault("template", template)
        rec["census_run"] = census_run
        with open(OUT, "a") as f:
            f.write(json.dumps(rec) + "\n")
        done.add(g["id"])
        write_active(goals_done=len(done), current_goal="", session_id="")
        ingest_row(rec, series, census_run)
    if _INGEST["pending"] and not _INGEST["unavailable"]:
        flush_pending()
        if _INGEST["pending"]:
            log("  %d row(s) never ingested (prod down at the end too) - post the archive with "
                "ingest_census.py" % len(_INGEST["pending"]))
    write_active(state="done", current_goal="", session_id="")
    log("census complete")


if __name__ == "__main__":
    main()
