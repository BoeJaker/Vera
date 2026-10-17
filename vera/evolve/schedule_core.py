"""Loop Lab schedules - pure: records, windows, what is due, calendar projection.

A schedule says WHEN a piece of Loop Lab work may run: a census template, a
suite, a task, a pipeline step, a board item, or any capability. The first
one asked for (2026-09-21): "run censuses on weekdays from 5am to 5pm". That
is a weekly window - days of the week, a start and an end time in a
timezone - and a repeat rule inside it: `continuous` (start the next run as
soon as the previous one has finished), `once_per_window` (fire once when
the window opens) or `every` N minutes. A one-shot schedule has `once_at`
instead of days.

Nothing here does I/O, reads Redis, or knows the clock: every function takes
`now` (an aware datetime) and a `state` dict describing the box (is a census
in flight, are loops running, is the harness blocked). The capabilities
module gathers those and executes what `plan_tick` returns; the calendar in
Loop Lab draws what `project_events` returns.

Hour arithmetic is done in the schedule's own timezone via `zoneinfo`; on a
Python without it (3.8) only UTC and fixed offsets such as "+01:00" resolve,
which is enough for the tests and never for prod (3.11+).
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Tuple

KINDS = ("census", "suite", "task", "pipeline", "board", "loop", "tests", "cap")
# Kinds whose work outlives the tick that started it: run in the background, the run record closed when it ends,
# and the next start held while one is still going.
BACKGROUND_KINDS = ("loop", "tests")
#: Every kind but census now starts in the background (DEEP-21 D21-1): a run still going holds the
#: schedule's next start back, whatever the kind. BACKGROUND_KINDS keeps its narrower meaning for the
#: `continuous` repeat rule (cooldown vs interval) so existing schedules keep their cadence.
RUN_TRACKED_KINDS = ("suite", "task", "pipeline", "board", "loop", "tests", "cap")
STILL_RUNNING_HOURS = 6                 # a background run older than this no longer holds the next one back
REPEATS = ("continuous", "once_per_window", "every")
WINDOW_END = ("finish", "yield", "drop")
DEFAULT_TZ = "Europe/London"
DEFAULT_DAYS = [0, 1, 2, 3, 4]          # Mon..Fri
MAX_STARTS_PER_TICK = 3
MAX_PROJECTED_EVENTS = 600

COLORS = {"census": "#c9a35a", "suite": "#6db87a", "task": "#8fb87a",
          "pipeline": "#7aa2f7", "board": "#a78bfa", "loop": "#f472b6", "tests": "#38bdf8",
          "cap": "#9aa0a6"}

# The calendar's layers: what an event is, so a viewer can switch each on and off. A board item's schedule is its own
# layer - the work a board item asked for, whatever kind it is.
LAYERS = ("windows", "runs", "results", "board", "calendar")


def layer_of(ev: Dict[str, Any]) -> str:
    src = ev.get("source") or ""
    if src == "results":
        return "results"
    if src in ("loop-lab", "loop-lab-run"):
        if ev.get("board_id") or ev.get("kind") == "board":
            return "board"
        return "runs" if src == "loop-lab-run" else "windows"
    return "calendar"


def for_board(schedules: Iterable[Dict[str, Any]], board_id: str) -> List[Dict[str, Any]]:
    """The schedules tied to one board item (its own board-kind schedules and any other work linked to it)."""
    b = (board_id or "").strip()
    return [s for s in schedules if b and (s.get("board_id") == b
                                          or (s.get("kind") == "board" and (s.get("target") or {}).get("id") == b))]

# Capabilities a schedule may never run through the generic `cap` kind: the
# same fence the idle queue keeps, plus the one action that reaches prod.
CAP_DENY_PREFIXES = ("sys.", "background.", "evolve.bleeding_edge.", "evolve.schedule.",
                     "census.control", "loops.config", "evolve.sandbox.down",
                     "evolve.sandbox.up", "evolve.pipeline.promote",
                     "cluster.job.stop", "jobs.purge_pending")

#: DEEP-21 D21-2 (11 Oct 2026): a `cap` schedule may run only these families - read, measure, report and
#: model-training work. Anything else (release, exec, sandbox, secrets, autonomous...) is refused at save
#: AND at start; the deny-list above still wins inside an allowed family. Add a family here, with a reason,
#: when a new kind of scheduled job is wanted.
CAP_ALLOW_PREFIXES = ("decide.", "obs.", "evolve.suite.", "chat.corpus.", "worldview.", "intel.", "bench.", "census.",
                      "nlp.", "info.seek", "memory.seek", "memory.map", "caps.shortlist.",
                      "perf.", "evolve.loop.record", "evolve.result.", "agents.work.",
                      # four-minds FM-7.9 (17 Oct 2026): the memory census - the frozen known-item set through
                      # memory.seek on four-minds nights; read-only on memory, a row in its own table
                      "memory.perf.probe",
                      # looplab-control A4 (17 Oct 2026): every test family can be scheduled - ML Workshop
                      # training/eval and sweeps (never export or delete), memory retrieval evals, a dream
                      # trigger's cycle (A5: the nightly QA suite), the student nightly pass, and the
                      # allow-listed scripts (probes, TTS round-trip) through evolve.script.run
                      "ml.train", "ml.sweep", "retrieval.eval", "retrieval.known_items.run",
                      "dream.cycle.run", "evolve.script.run")

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

#: looplab-control A1 (17 Oct 2026): capabilities that start their work in the background and return at
#: once. The scheduler recorded them as finished the moment they returned, freed the exclusive slot and let a
#: census start beside work that was still running. Each says how to ask whether its job has ended:
#:   status - the status capability; id_key - the start result's field that names the job (passed to the
#:   status capability as id_arg); how - which reading of the status result applies (follow_state).
FOLLOW = {
    "evolve.suite.start": {"status": "evolve.suite.status", "id_key": "", "id_arg": "", "how": "running"},
    "chat.corpus.run": {"status": "chat.corpus.last", "id_key": "", "id_arg": "", "how": "running"},
    "dream.cycle.run": {"status": "dream.cycle.progress", "id_key": "", "id_arg": "", "how": "running"},
    "bench.run.start": {"status": "bench.status", "id_key": "run_id", "id_arg": "run_id", "how": "rows"},
    "bench.matrix.start": {"status": "bench.matrix.status", "id_key": "run_id", "id_arg": "run_id",
                           "how": "state"},
    "evolve.script.run": {"status": "evolve.script.status", "id_key": "job_id", "id_arg": "job_id",
                          "how": "state"},
}
FOLLOW_POLL_S = 30
_ENDED = ("done", "error", "failed", "cancelled", "canceled", "finished", "stopped", "exited")


def follow_args(cap: str, res: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(status capability, its arguments) when `cap` started a background job that is still to finish,
    else None (not a background starter, or it failed to start)."""
    spec = FOLLOW.get((cap or "").strip())
    if not spec or not isinstance(res, dict) or res.get("error") or res.get("ok") is False:
        return None
    args: Dict[str, Any] = {}
    if spec["id_key"]:
        jid = res.get(spec["id_key"])
        if not jid:
            return None
        args[spec["id_arg"]] = jid
    return spec["status"], args


def follow_state(cap: str, st: Any) -> Tuple[bool, str, str]:
    """(ended, result word, detail) from a status capability's answer. An unreadable status is NOT the
    end: the follower keeps asking until STILL_RUNNING_HOURS."""
    spec = FOLLOW.get((cap or "").strip()) or {}
    if not isinstance(st, dict) or (st.get("error") and not st.get("state") and "running" not in st):
        return False, "", ""            # unreadable (no state at all)
    how = spec.get("how")
    if how == "running":
        if st.get("running"):
            return False, "", ""
        sst = st.get("status") if isinstance(st.get("status"), dict) else {}
        summ = st.get("summary") or sst.get("avg_combined") or ""
        if isinstance(summ, dict):
            summ = " · ".join("%s %s" % (k, summ[k]) for k in list(summ)[:6])
        return True, "done", str(summ)[:200]
    row = st
    if how == "rows":
        rows = st.get("runs") or st.get("rows") or st.get("items") or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
    state = str(row.get("state") or "").lower()
    if state in _ENDED:
        word = "error" if (state in ("error", "failed") or row.get("error")) else "done"
        return True, word, str(row.get("error") or row.get("summary") or state)[:200]
    return False, "", ""


# ── A2: quiet hours ───────────────────────────────────────────────────────────
#: NIGHT-BOOKINGS (owner, 17 Oct 2026): 04:00-05:00 is the background queue's quiet hour, every night.
#: No schedule STARTS in a quiet window unless it says quiet_ok (what is already running carries on).
DEFAULT_QUIET = [{"start": "04:00", "end": "05:00"}]


def in_quiet(now: datetime, quiet: Optional[List[Dict[str, Any]]], tz_name: str = DEFAULT_TZ) -> str:
    """The quiet window `now` falls in ("04:00-05:00"), or ""."""
    tz = resolve_tz(tz_name or DEFAULT_TZ)
    local = now.astimezone(tz)
    for q in (quiet if quiet is not None else DEFAULT_QUIET) or []:
        try:
            a, b = parse_hhmm(q.get("start")), parse_hhmm(q.get("end"))
        except Exception:
            continue
        t = local.time().replace(second=0, microsecond=0)
        inside = (a <= t < b) if a < b else (t >= a or t < b)
        if inside:
            return "%s-%s" % (a.strftime("%H:%M"), b.strftime("%H:%M"))
    return ""


def window_end_stop(rec: Dict[str, Any], now: datetime) -> bool:
    """A3: a NON-census run whose window has closed and whose schedule asked to stop it (at_window_end
    drop or yield - a capability cannot be parked, so yield stops it too)."""
    if rec.get("kind") == "census" or rec.get("once_at"):
        return False
    if rec.get("at_window_end", "finish") == "finish":
        return False
    return current_window(rec, now) is None


# ── time helpers ─────────────────────────────────────────────────────────────

def resolve_tz(name: str) -> tzinfo:
    """A tzinfo for `name`. zoneinfo when available; otherwise UTC and fixed
    offsets ("+01:00") only, so a 3.8 test box can still exercise the maths."""
    n = (name or "").strip() or "UTC"
    if n.upper() in ("UTC", "Z"):
        return timezone.utc
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", n)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    try:
        from zoneinfo import ZoneInfo  # py3.9+
        return ZoneInfo(n)
    except Exception:
        raise ValueError(f"unknown timezone: {n}")


def parse_hhmm(s: str) -> time:
    m = _HHMM.match(str(s or "").strip())
    if not m:
        raise ValueError(f"not a HH:MM time: {s!r}")
    return time(int(m.group(1)), int(m.group(2)))


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(s: str, *, default_tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """An aware datetime from ISO text (a trailing Z accepted); naive text is
    taken in `default_tz` (UTC when not given)."""
    t = str(s or "").strip()
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=default_tz or timezone.utc)
    return d


# ── records ──────────────────────────────────────────────────────────────────

def new_id() -> str:
    return uuid.uuid4().hex[:10]


def normalize(rec: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Validate and fill a schedule record. Raises ValueError with a plain
    reason on anything a person must fix; never invents a target."""
    if not isinstance(rec, dict):
        raise ValueError("schedule must be an object")
    r = dict(rec)
    r["id"] = str(r.get("id") or new_id()).strip()
    r["title"] = str(r.get("title") or "").strip()
    kind = str(r.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    r["kind"] = kind
    target = r.get("target")
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    r["target"] = _normalize_target(kind, target)
    if not r["title"]:
        r["title"] = _default_title(kind, r["target"])
    r["enabled"] = bool(r.get("enabled", True))
    r["timezone"] = str(r.get("timezone") or DEFAULT_TZ).strip()
    resolve_tz(r["timezone"])                      # fail early on a bad zone
    once_at = str(r.get("once_at") or "").strip()
    if once_at:
        at = parse_iso(once_at, default_tz=resolve_tz(r["timezone"]))
        if at is None:
            raise ValueError("once_at must be an ISO datetime")
        r["once_at"] = iso(at)
        r["days"] = []
    else:
        r["once_at"] = ""
        days = r.get("days")
        if days is None:
            days = list(DEFAULT_DAYS)
        try:
            days = sorted({int(d) for d in days})
        except Exception:
            raise ValueError("days must be weekday numbers 0 (Mon) .. 6 (Sun)")
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("days must be weekday numbers 0 (Mon) .. 6 (Sun)")
        r["days"] = days
        start, end = parse_hhmm(r.get("start") or "05:00"), parse_hhmm(r.get("end") or "17:00")
        if start == end:
            raise ValueError("the window must not start and end at the same time")
        # A6 (17 Oct 2026): end before start = an overnight window, ending the next day (22:00-02:00)
        r["start"], r["end"] = start.strftime("%H:%M"), end.strftime("%H:%M")
    repeat = str(r.get("repeat") or ("continuous" if kind == "census" else "once_per_window"))
    if repeat not in REPEATS:
        raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")
    r["repeat"] = repeat
    every = int(r.get("every_minutes") or 60)
    if repeat == "every" and every < 1:
        raise ValueError("every_minutes must be at least 1")
    r["every_minutes"] = max(1, every)
    awe = str(r.get("at_window_end") or "finish")
    if awe not in WINDOW_END:
        raise ValueError(f"at_window_end must be one of {', '.join(WINDOW_END)}")
    r["at_window_end"] = awe
    r["exclusive"] = bool(r.get("exclusive", True))
    r["cooldown_minutes"] = max(0, int(r.get("cooldown_minutes") or 2))
    r["notes"] = str(r.get("notes") or "")
    r["quiet_ok"] = bool(r.get("quiet_ok", False))           # A2: may start in a quiet hour
    # The board item this work is for: a board-kind schedule's own item, or any schedule linked to one (a census run
    # for an item that asked for one, a loop working an item's goal). The scheduler notes each start on the item.
    r["board_id"] = str(r.get("board_id") or (r["target"].get("id") if kind == "board" else "") or "").strip()
    stamp = iso(now) if now else ""
    r["created"] = str(r.get("created") or stamp)
    r["updated"] = stamp or str(r.get("updated") or "")
    for k in ("last_started_at", "last_finished_at", "last_result"):
        r[k] = str(r.get(k) or "")
    r["runs"] = int(r.get("runs") or 0)
    return r


def _normalize_target(kind: str, t: Dict[str, Any]) -> Dict[str, Any]:
    t = {k: v for k, v in t.items()}
    if kind == "census":
        t["template"] = str(t.get("template") or "default").strip()
        t["mode"] = "harness"
        # The planning style forced on every goal (the harness's
        # CENSUS_PLAN_STYLE): its own archive series, default-style-<s>-runN.
        # "" = each goal's own style (auto) - the baseline series.
        style = str(t.get("plan_style") or "").strip().lower()
        if style and style not in census_plan_styles():
            raise ValueError("census plan_style must be one of: %s" % ", ".join(census_plan_styles()))
        t["plan_style"] = style
        # The loop's intent core forced on every goal (the harness's
        # CENSUS_INTENT_CORE, roadmap G): its own series, <template>-core-order-runN.
        # "" / "off" = the loop default - the baseline series.
        core = str(t.get("intent_core") or "").strip().lower()
        if core and core not in CENSUS_INTENT_CORES:
            raise ValueError("census intent_core must be one of: %s" % ", ".join(CENSUS_INTENT_CORES))
        t["intent_core"] = "" if core in ("", "off") else core
        _census_minds(t)
    elif kind == "suite":
        if not str(t.get("tag") or "").strip():
            raise ValueError("a suite schedule needs target.tag")
        t["tag"] = str(t["tag"]).strip()
        t["profile"] = str(t.get("profile") or "")
        t["assess"] = bool(t.get("assess", False))
    elif kind == "task":
        if not str(t.get("id") or "").strip():
            raise ValueError("a task schedule needs target.id")
        t["id"] = str(t["id"]).strip()
        t["assess"] = bool(t.get("assess", False))
    elif kind == "pipeline":
        action = str(t.get("action") or "test").strip()
        if action not in ("test", "promote", "adopt"):
            raise ValueError("pipeline action must be test, promote or adopt")
        t["action"] = action
        if action == "adopt":
            if not str(t.get("branch") or "").strip():
                raise ValueError("a pipeline adopt schedule needs target.branch")
            t["branch"] = str(t["branch"]).strip()
        else:
            if not str(t.get("id") or "").strip():
                raise ValueError("a pipeline schedule needs target.id")
            t["id"] = str(t["id"]).strip()
        # A schedule may reach bleeding-edge, never main: that is a person's act.
        t["to"] = "bleeding-edge"
    elif kind == "board":
        if not str(t.get("id") or "").strip():
            raise ValueError("a board schedule needs target.id")
        t["id"] = str(t["id"]).strip()
        t["executor"] = str(t.get("executor") or "deterministic")
        t["agent"] = str(t.get("agent") or "orchestrator")
    elif kind == "loop":
        # An agentic loop on a goal (loops.run): the profile picks the loop's shape (default coding).
        if not str(t.get("goal") or "").strip():
            raise ValueError("a loop schedule needs target.goal")
        t["goal"] = str(t["goal"]).strip()
        t["profile"] = str(t.get("profile") or "coding").strip()
        t["model"] = str(t.get("model") or "").strip()
    elif kind == "tests":
        # A branch's unit tests in an ephemeral container (evolve.unittest.run): the red/green the matrices show.
        t["branch"] = str(t.get("branch") or "bleeding-edge").strip()
        t["paths"] = str(t.get("paths") or "tests").strip()
        t["markers"] = str(t.get("markers") or "").strip()
        t["repo"] = str(t.get("repo") or "").strip()
    elif kind == "cap":
        name = str(t.get("name") or "").strip()
        if not name:
            raise ValueError("a cap schedule needs target.name")
        if cap_denied(name):
            raise ValueError(f"{name} may not run from a schedule")
        t["name"] = name
        args = t.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError("target.arguments must be an object")
        t["arguments"] = args
    return t


#: A census arm's label (CENSUS_MINDS contract v1, four-minds ROADMAP §4.1, rule 2).
MINDS_LABEL_RE = re.compile(r"^[a-z0-9-]{1,24}$")


def _switchboard():
    try:
        from Vera.vera.minds import switchboard_core as _sb
    except Exception:                                  # pragma: no cover
        from vera.minds import switchboard_core as _sb
    return _sb


def _census_minds(t: Dict[str, Any]) -> None:
    """Validate a census arm's switchboard set at SAVE time (contract rule 6).

    target.minds   {feature_id: mode} - strict, census-settable switches only
    minds_label    required with minds; [a-z0-9-]{1,24}; not 'style-*'/'core-*'; not the template
    pair_of        the control schedule this arm is compared with (contract rule 7)
    """
    minds = t.get("minds") or {}
    if not isinstance(minds, dict):
        raise ValueError("census minds must be an object of {feature_id: mode}")
    label = str(t.get("minds_label") or "").strip()
    if minds:
        sb = _switchboard()
        v = sb.validate(minds, census=True)
        if not v["ok"]:
            raise ValueError("census minds: " + sb.explain(v))
        if not MINDS_LABEL_RE.match(label):
            raise ValueError("census minds needs minds_label matching [a-z0-9-]{1,24}")
        if label.startswith(("style-", "core-")) or label == str(t.get("template") or ""):
            raise ValueError("census minds_label must not start with style-/core- or equal the template name")
        t["minds"] = {str(k): str(val) for k, val in sorted(minds.items())}
        t["minds_label"] = label
    else:
        t["minds"] = {}
        t["minds_label"] = ""
    t["pair_of"] = str(t.get("pair_of") or "").strip()


#: The intent-core modes a census may force (dag/intent_core_core.MODES).
CENSUS_INTENT_CORES = ("off", "order")


#: is_due's reason when a due census displaces another schedule's parked one.
DISPLACE = "displace parked"


def census_plan_styles() -> List[str]:
    """The loop's planning styles a census may force (planner_styles.LOOP_STYLES)."""
    try:
        from Vera.vera.planning import planner_styles as _ps
    except Exception:                                  # pragma: no cover
        from vera.planning import planner_styles as _ps
    return list(_ps.LOOP_STYLES.keys())


def cap_allowed(name: str) -> bool:
    """Whether a schedule may run this capability: an allowed family and not a denied one."""
    n = (name or "").strip()
    if not n or any(n == p.rstrip(".") or n.startswith(p) for p in CAP_DENY_PREFIXES):
        return False
    return any(n == p.rstrip(".") or n.startswith(p) for p in CAP_ALLOW_PREFIXES)


def cap_denied(name: str) -> bool:
    """A schedule may NOT run this capability (allow-list, D21-2; was a deny-list only)."""
    return not cap_allowed(name)


def _default_title(kind: str, t: Dict[str, Any]) -> str:
    return {"census": f"Census · {t.get('template', 'default')}"
                      + (f" · {t['plan_style']} planning" if t.get("plan_style") else "")
                      + (f" · intent core {t['intent_core']}" if t.get("intent_core") else "")
                      + (f" · minds {t['minds_label']}" if t.get("minds_label") else ""),
            "suite": f"Suite · {t.get('tag', '')}",
            "task": f"Task · {t.get('id', '')}",
            "pipeline": f"Pipeline {t.get('action', '')} · {t.get('id') or t.get('branch', '')}",
            "board": f"Board · {t.get('id', '')}",
            "loop": f"Loop · {str(t.get('goal', ''))[:48]}",
            "tests": f"Tests · {t.get('branch', '')}" + (f" · {t['markers']}" if t.get("markers") else ""),
            "cap": f"Cap · {t.get('name', '')}"}[kind]


# ── windows ──────────────────────────────────────────────────────────────────

def window_on(rec: Dict[str, Any], day: date) -> Optional[Tuple[datetime, datetime]]:
    """The schedule's window on `day` (a local date) as aware datetimes, or
    None when the schedule does not run that day."""
    if rec.get("once_at"):
        at = parse_iso(rec["once_at"])
        if at is None:
            return None
        tz = resolve_tz(rec.get("timezone") or DEFAULT_TZ)
        local = at.astimezone(tz)
        if local.date() != day:
            return None
        return at, at + timedelta(minutes=int(rec.get("every_minutes") or 60))
    if day.weekday() not in (rec.get("days") or []):
        return None
    tz = resolve_tz(rec.get("timezone") or DEFAULT_TZ)
    start = datetime.combine(day, parse_hhmm(rec.get("start") or "05:00"), tzinfo=tz)
    end = datetime.combine(day, parse_hhmm(rec.get("end") or "17:00"), tzinfo=tz)
    if end <= start:                                   # A6: an overnight window ends the next day
        end = datetime.combine(day + timedelta(days=1), parse_hhmm(rec.get("end") or "17:00"), tzinfo=tz)
    return start, end


def current_window(rec: Dict[str, Any], now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """The window `now` falls inside, or None."""
    if rec.get("once_at"):
        return None
    tz = resolve_tz(rec.get("timezone") or DEFAULT_TZ)
    today = now.astimezone(tz).date()
    for d in (today, today - timedelta(days=1)):      # yesterday's overnight window may still be open
        w = window_on(rec, d)
        if w and w[0] <= now < w[1]:
            return w
    return None


def next_window(rec: Dict[str, Any], now: datetime, *, horizon_days: int = 14
                ) -> Optional[Tuple[datetime, datetime]]:
    """The first window that starts after `now` (or the current one)."""
    cur = current_window(rec, now)
    if cur:
        return cur
    tz = resolve_tz(rec.get("timezone") or DEFAULT_TZ)
    d = now.astimezone(tz).date()
    for i in range(horizon_days + 1):
        w = window_on(rec, d + timedelta(days=i))
        if w and w[0] > now:
            return w
    return None


# ── due / planning ───────────────────────────────────────────────────────────

def is_due(rec: Dict[str, Any], now: datetime, state: Dict[str, Any]) -> Tuple[bool, str]:
    """(due, reason). `state`: census_running (bool), loops_running (int),
    harness_blocked (str, why a census cannot start), census_owner (the
    schedule id that started the census in flight, if any)."""
    if not rec.get("enabled", True):
        return False, "disabled"
    kind = rec.get("kind")
    last_started = parse_iso(rec.get("last_started_at") or "")
    if rec.get("once_at"):
        at = parse_iso(rec["once_at"])
        if at is None or now < at:
            return False, "not yet"
        if last_started and last_started >= at:
            return False, "already fired"
        window = None
    else:
        window = current_window(rec, now)
        if not window:
            return False, "outside window"
        repeat = rec.get("repeat") or "once_per_window"
        if repeat == "once_per_window":
            if last_started and last_started >= window[0]:
                return False, "fired this window"
        elif repeat == "every":
            every = timedelta(minutes=int(rec.get("every_minutes") or 60))
            if last_started and now - last_started < every:
                return False, "interval not elapsed"
        if kind in RUN_TRACKED_KINDS and last_started:
            last_fin = parse_iso(rec.get("last_finished_at") or "")
            if (not last_fin or last_fin < last_started) and now - last_started < timedelta(hours=STILL_RUNNING_HOURS):
                return False, "previous run still going"
        if repeat == "continuous" and kind in BACKGROUND_KINDS:
            cool = timedelta(minutes=int(rec.get("cooldown_minutes") or 0))
            last_fin = parse_iso(rec.get("last_finished_at") or "")
            if last_fin and now - last_fin < cool:
                return False, "cooling down"
        elif repeat == "continuous":
            if kind == "census":
                if state.get("census_running") and not (
                        state.get("census_parked_by_us") and state.get("census_owner") == rec.get("id")):
                    return False, "census in flight"
                cool = timedelta(minutes=int(rec.get("cooldown_minutes") or 0))
                last_fin = parse_iso(rec.get("last_finished_at") or "")
                ref = max([d for d in (last_started, last_fin) if d], default=None)
                if ref and now - ref < cool:
                    return False, "cooling down"
            else:
                every = timedelta(minutes=int(rec.get("every_minutes") or 60))
                if last_started and now - last_started < every:
                    return False, "interval not elapsed"
    # A2: nothing starts in the quiet hour (NIGHT-BOOKINGS) unless the schedule says quiet_ok.
    if state.get("quiet") and not rec.get("quiet_ok"):
        return False, "quiet hour %s (background queue)" % state["quiet"]
    # A2: a declared activity (a probe, a manual benchmark) holds the box like a loop does.
    if (kind == "census" or rec.get("exclusive", True)) and state.get("declared_busy"):
        return False, "declared activity: %s" % state["declared_busy"]
    # Box gating. A census is one loop on prod's GPU; nothing else may run
    # beside it (HANDOVER §4.1-4.2), and a census needs the box to itself.
    if kind == "census":
        if state.get("census_parked_by_us") and state.get("census_owner") == rec.get("id"):
            return True, "resume"          # parked on this schedule's own yield
        if (state.get("census_running") and state.get("census_parked_by_us")
                and state.get("window_end_applied")
                and state.get("census_owner") != rec.get("id")):
            # Parked on ANOTHER schedule's window-end yield: that run resumes only
            # when its own window reopens (the next day, or days later), and a
            # parked census counts as in flight - 1 Oct 2026 the operator-family
            # run parked at 07:00 and blocked the baseline and every style slot.
            # A due census displaces it (it is dropped and archived partial).
            # Never a person's pause: parked_by_us is the scheduler's own yield.
            return True, DISPLACE
        if state.get("census_running"):
            return False, "census in flight"
        if int(state.get("loops_running") or 0) > 0:
            return False, "an agent loop is running"
        if int(state.get("exclusive_running") or 0) > 0:
            return False, "an exclusive scheduled cap is running"      # RF-09
        if state.get("harness_blocked"):
            return False, str(state["harness_blocked"])
    elif rec.get("exclusive", True) and state.get("census_running"):
        return False, "census in flight"
    elif kind == "cap" and rec.get("exclusive", True) and int(state.get("loops_running") or 0) > 0:
        return False, "an agent loop is running"                      # RF-09: caps wait for loops
    elif rec.get("exclusive", True) and int(state.get("exclusive_running") or 0) > 0:
        return False, "another exclusive scheduled run is going"      # A3: one exclusive run at a time
    return True, "due"


def interrupted_runs(runs: List[Dict[str, Any]], process_started: datetime) -> List[Dict[str, Any]]:
    """Background runs a restart killed (DEEP-21 D21-5): not census, no finished_at, started before
    this process - nothing is left alive to close them, and each holds its schedule back."""
    out = []
    for r in runs or []:
        if not isinstance(r, dict) or r.get("kind") == "census" or r.get("finished_at"):
            continue
        st = parse_iso(r.get("started_at") or "")
        if st is not None and st < process_started:
            out.append(r)
    return out


def plan_tick(schedules: Iterable[Dict[str, Any]], now: datetime, state: Dict[str, Any],
              *, max_starts: int = MAX_STARTS_PER_TICK) -> List[Dict[str, Any]]:
    """What to start this tick, in schedule order: at most one census (the
    box has one GPU) and at most `max_starts` actions in all. Each action
    carries the schedule id, kind, target and the reason it is due."""
    out: List[Dict[str, Any]] = []
    census_taken = bool(state.get("census_running"))
    st = dict(state)
    for rec in schedules:
        if len(out) >= max_starts:
            break
        if rec.get("kind") == "census" and census_taken and not (
                st.get("census_parked_by_us") and (st.get("census_owner") == rec.get("id")
                                                  or st.get("window_end_applied"))):
            continue                    # (a window-end park is judged by is_due: DISPLACE)
        due, why = is_due(rec, now, st)
        if not due:
            continue
        out.append({"schedule_id": rec["id"], "kind": rec["kind"], "target": rec["target"],
                    "title": rec.get("title", ""), "reason": why})
        if rec.get("kind") != "census" and rec.get("exclusive", True):
            st["exclusive_running"] = int(st.get("exclusive_running") or 0) + 1   # A3: one per tick too
        if rec.get("kind") == "census":
            census_taken = True
            st["census_running"] = True     # exclusive kinds see the census we just planned
            st["census_parked_by_us"] = False
    return out


def window_end_action(rec: Dict[str, Any], now: datetime, state: Dict[str, Any]
                      ) -> Optional[Dict[str, Any]]:
    """For a census schedule whose window has closed while ITS census is still
    running: the control action it asked for (yield or drop), else None."""
    if rec.get("kind") != "census" or rec.get("at_window_end", "finish") == "finish":
        return None
    if rec.get("once_at"):
        return None
    if not state.get("census_running") or state.get("census_owner") != rec.get("id"):
        return None
    if state.get("window_end_applied"):
        return None
    if current_window(rec, now):
        return None
    return {"schedule_id": rec["id"], "action": rec["at_window_end"],
            "reason": f"window ended at {rec.get('end')} {rec.get('timezone')}"}


# ── calendar projection ──────────────────────────────────────────────────────

def project_events(schedules: Iterable[Dict[str, Any]], start: datetime, end: datetime,
                   *, limit: int = MAX_PROJECTED_EVENTS) -> List[Dict[str, Any]]:
    """Calendar events for every window of every schedule between `start`
    and `end` (aware datetimes): one event per day per schedule, read-only,
    coloured by kind, id `sched:<schedule>:<YYYY-MM-DD>`."""
    out: List[Dict[str, Any]] = []
    for rec in schedules:
        tz = resolve_tz(rec.get("timezone") or DEFAULT_TZ)
        d0 = (start.astimezone(tz).date() - timedelta(days=1))
        d1 = end.astimezone(tz).date() + timedelta(days=1)
        d = d0
        while d <= d1 and len(out) < limit:
            w = window_on(rec, d)
            d += timedelta(days=1)
            if not w or w[1] < start or w[0] > end:
                continue
            out.append({
                "id": f"sched:{rec['id']}:{w[0].astimezone(tz).date().isoformat()}",
                "title": rec.get("title") or rec["id"],
                "start": iso(w[0]), "end": iso(w[1]), "all_day": False,
                "source": "loop-lab", "read_only": True,
                "color": COLORS.get(rec.get("kind"), COLORS["cap"]),
                "kind": rec.get("kind"), "schedule_id": rec["id"],
                "enabled": bool(rec.get("enabled", True)),
                "repeat": rec.get("repeat"), "label": _default_title(rec["kind"], rec["target"]),
                "board_id": rec.get("board_id", ""),
            })
    out.sort(key=lambda e: e["start"])
    return out


def history_events(runs: Iterable[Dict[str, Any]], start: datetime, end: datetime
                   ) -> List[Dict[str, Any]]:
    """Calendar events for runs the scheduler actually started."""
    out: List[Dict[str, Any]] = []
    for r in runs:
        s = parse_iso(r.get("started_at") or "")
        if not s:
            continue
        f = parse_iso(r.get("finished_at") or "") or (s + timedelta(minutes=30))
        if f < start or s > end:
            continue
        out.append({
            "id": f"run:{r.get('id') or r.get('started_at')}",
            "title": f"▶ {r.get('title') or r.get('kind', 'run')}"
                     + (f" · {r['result']}" if r.get("result") else ""),
            "start": iso(s), "end": iso(f), "all_day": False,
            "source": "loop-lab-run", "read_only": True,
            "color": COLORS.get(r.get("kind"), COLORS["cap"]),
            "kind": r.get("kind"), "schedule_id": r.get("schedule_id"),
            "result": r.get("result", ""), "detail": r.get("detail", ""),
            "board_id": r.get("board_id", ""),
        })
    out.sort(key=lambda e: e["start"])
    return out


def weekday_census(template: str = "default", *, start: str = "05:00", end: str = "17:00",
                   timezone_name: str = DEFAULT_TZ, now: Optional[datetime] = None
                   ) -> Dict[str, Any]:
    """The schedule first asked for: censuses back to back on weekdays inside
    a daytime window, the census in flight allowed to finish at the end."""
    return normalize({"title": f"Census · {template} · weekdays {start}-{end}",
                      "kind": "census", "target": {"template": template},
                      "days": list(DEFAULT_DAYS), "start": start, "end": end,
                      "timezone": timezone_name, "repeat": "continuous",
                      "at_window_end": "finish", "cooldown_minutes": 2}, now=now)


# ── results on the calendar ───────────────────────────────────────────────────
# A census run or a suite is a span of time with an outcome; laid on the same
# calendar as the windows, the series reads at a glance - which days ran clean,
# which capped, whether a change moved the numbers (asked 2026-09-22).

# bright and opaque on the calendar (drawn solid, over the faint window bands) so the results layer reads at a glance
RESULT_COLORS = {"good": "#4ade80", "mixed": "#fbbf24", "bad": "#f87171", "none": "#9aa0a6"}


def result_grade(pass_rate: Optional[float]) -> str:
    if pass_rate is None:
        return "none"
    try:
        p = float(pass_rate)
    except (TypeError, ValueError):
        return "none"
    return "good" if p >= 0.85 else ("mixed" if p >= 0.6 else "bad")


def results_events(suites: Iterable[Dict[str, Any]], start: datetime, end: datetime, *,
                   granularity: str = "runs", limit: int = MAX_PROJECTED_EVENTS) -> List[Dict[str, Any]]:
    """Calendar events for archived census runs and suites (`evolve.suites`
    records: suite_id, tag, template, started_at, ts (end), tasks_n, done,
    capped, pass_rate, results[]). One event per run, or one per task when
    `granularity` is "goals" (each task's end is placed by its elapsed time
    from the run's start, in order). Read-only, coloured by pass rate."""
    out: List[Dict[str, Any]] = []
    for s in suites:
        sid = str(s.get("suite_id") or "")
        st = parse_iso(str(s.get("started_at") or ""))
        en = parse_iso(str(s.get("ts") or "")) or (st + timedelta(hours=1) if st else None)
        if not st and en:
            st = en - timedelta(hours=1)
        if not st or not en:
            continue
        if en < start or st > end:
            continue
        n = int(s.get("tasks_n") or len(s.get("results") or []) or 0)
        done = s.get("done")
        if done is None:
            done = sum(1 for r in (s.get("results") or []) if str(r.get("status")) == "done")
        capped = s.get("capped")
        if capped is None:
            capped = sum(1 for r in (s.get("results") or []) if r.get("hit_cap"))
        pr = s.get("pass_rate")
        grade = result_grade(pr)
        tag = str(s.get("tag") or s.get("template") or "")
        kind = "census" if (tag.startswith("census") or s.get("census_run")) else "suite"
        pct = f" · {round(float(pr) * 100)}%" if pr is not None else ""
        base = {"source": "results", "read_only": True, "kind": kind, "suite_id": sid, "tag": tag,
                "grade": grade, "color": RESULT_COLORS[grade], "pass_rate": pr,
                "done": done, "capped": capped, "tasks": n, "all_day": False}
        if granularity == "goals":
            cur = st
            for r in s.get("results") or []:
                el = float(r.get("elapsed_s") or 0)
                r_end = cur + timedelta(seconds=el) if el else cur + timedelta(minutes=5)
                st_r = str(r.get("status") or "")
                g = "good" if st_r == "done" and not r.get("hit_cap") else ("mixed" if st_r == "done" else "bad")
                out.append(dict(base, id=f"result:{sid}:{r.get('label') or r.get('task')}",
                                title=f"{r.get('label') or r.get('task')} · {st_r}"
                                      + (" (cap)" if r.get("hit_cap") else "") + (f" · {r.get('checks')}" if r.get("checks") else ""),
                                start=iso(cur), end=iso(r_end), color=RESULT_COLORS[g], grade=g,
                                task=r.get("task"), status=st_r, elapsed_s=el, checks=r.get("checks", "")))
                cur = r_end
                if len(out) >= limit:
                    break
        else:
            out.append(dict(base, id=f"result:{sid}", title=f"{sid} · {done}/{n}{pct}" + (f" · {capped} capped" if capped else ""),
                            start=iso(st), end=iso(en),
                            rows=[{"label": r.get("label") or r.get("task"), "status": r.get("status"),
                                   "checks": r.get("checks", ""), "elapsed_s": r.get("elapsed_s"),
                                   "hit_cap": bool(r.get("hit_cap"))} for r in (s.get("results") or [])][:40]))
        if len(out) >= limit:
            break
    out.sort(key=lambda e: e["start"])
    return out
