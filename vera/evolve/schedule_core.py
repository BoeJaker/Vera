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

KINDS = ("census", "suite", "task", "pipeline", "board", "cap")
REPEATS = ("continuous", "once_per_window", "every")
WINDOW_END = ("finish", "yield", "drop")
DEFAULT_TZ = "Europe/London"
DEFAULT_DAYS = [0, 1, 2, 3, 4]          # Mon..Fri
MAX_STARTS_PER_TICK = 3
MAX_PROJECTED_EVENTS = 600

COLORS = {"census": "#c9a35a", "suite": "#6db87a", "task": "#8fb87a",
          "pipeline": "#7aa2f7", "board": "#a78bfa", "cap": "#9aa0a6"}

# Capabilities a schedule may never run through the generic `cap` kind: the
# same fence the idle queue keeps, plus the one action that reaches prod.
CAP_DENY_PREFIXES = ("sys.", "background.", "evolve.bleeding_edge.", "evolve.schedule.",
                     "census.control", "loops.config", "evolve.sandbox.down",
                     "evolve.sandbox.up", "evolve.pipeline.promote")

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


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


def parse_iso(s: str) -> Optional[datetime]:
    """An aware datetime from ISO text (a trailing Z accepted); naive text is UTC."""
    t = str(s or "").strip()
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
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
        if parse_iso(once_at) is None:
            raise ValueError("once_at must be an ISO datetime")
        r["once_at"] = iso(parse_iso(once_at))
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
        if start >= end:
            raise ValueError("the window must end after it starts (overnight windows are not supported)")
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


def cap_denied(name: str) -> bool:
    n = (name or "").strip()
    return any(n == p.rstrip(".") or n.startswith(p) for p in CAP_DENY_PREFIXES)


def _default_title(kind: str, t: Dict[str, Any]) -> str:
    return {"census": f"Census · {t.get('template', 'default')}",
            "suite": f"Suite · {t.get('tag', '')}",
            "task": f"Task · {t.get('id', '')}",
            "pipeline": f"Pipeline {t.get('action', '')} · {t.get('id') or t.get('branch', '')}",
            "board": f"Board · {t.get('id', '')}",
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
    return start, end


def current_window(rec: Dict[str, Any], now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """The window `now` falls inside, or None."""
    if rec.get("once_at"):
        return None
    tz = resolve_tz(rec.get("timezone") or DEFAULT_TZ)
    w = window_on(rec, now.astimezone(tz).date())
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
        elif repeat == "continuous":
            if kind == "census":
                if state.get("census_running"):
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
    # Box gating. A census is one loop on prod's GPU; nothing else may run
    # beside it (HANDOVER §4.1-4.2), and a census needs the box to itself.
    if kind == "census":
        if state.get("census_running"):
            return False, "census in flight"
        if int(state.get("loops_running") or 0) > 0:
            return False, "an agent loop is running"
        if state.get("harness_blocked"):
            return False, str(state["harness_blocked"])
    elif rec.get("exclusive", True) and state.get("census_running"):
        return False, "census in flight"
    return True, "due"


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
        if rec.get("kind") == "census" and census_taken:
            continue
        due, why = is_due(rec, now, st)
        if not due:
            continue
        out.append({"schedule_id": rec["id"], "kind": rec["kind"], "target": rec["target"],
                    "title": rec.get("title", ""), "reason": why})
        if rec.get("kind") == "census":
            census_taken = True
            st["census_running"] = True     # exclusive kinds see the census we just planned
    return out


def window_end_action(rec: Dict[str, Any], now: datetime, state: Dict[str, Any]
                      ) -> Optional[Dict[str, Any]]:
    """For a census schedule whose window has closed while ITS census is still
    running: the control action it asked for (yield or drop), else None."""
    if rec.get("kind") != "census" or rec.get("at_window_end", "finish") == "finish":
        return None
    if not state.get("census_running") or state.get("census_owner") != rec.get("id"):
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
