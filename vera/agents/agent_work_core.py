"""The agent work registry (compute-roles L1, owner 11 Oct 2026): when an agent session is triggered to
run, and on what - and work the owner schedules FOR a session from Vera.

Owner: "a way for your scheduled operations to be visible in the Vera UI like working windows ... and
the user can set them up from there to schedule work with you - could even select sessions and board
items to work on." A Claude session's schedules were invisible to Vera: session crons live inside the
session, host jobs only as processes and a log file. Each entry here is one trigger or one request:

  kind     session_cron | host_job | loop_lab | one_shot   (an agent's own trigger)
           request                                          (work the owner asked a session to do)
  when     next fire (ISO, UTC) - and optional window_end / repeat text
  what     the prompt / script / brief, board_items, branch
  session  the session's name (as ListAgents shows it) and id
  status   armed | running | done | failed | cancelled | expired      (triggers)
           pending | claimed | done | declined | expired             (requests)

It is a READ model for the box: nothing here marks it busy (sys.activity.declare does - a planned run
must not hold back a census). Pure: dicts in, validated dicts / views out.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

TRIGGER_KINDS = ("session_cron", "host_job", "loop_lab", "one_shot")
KINDS = TRIGGER_KINDS + ("request",)
TRIGGER_STATUSES = ("armed", "running", "done", "failed", "cancelled", "expired")
REQUEST_STATUSES = ("pending", "claimed", "done", "declined", "expired")
#: a session cron or host job with no heartbeat for this long after its fire time is shown expired
#: (a session cron dies with its session; nothing else would ever say so)
STALE_AFTER_FIRE_S = 3 * 3600
MAX_TEXT = 2000


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: Any) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _strs(x: Any, n: int = 20, cap: int = 200) -> List[str]:
    if isinstance(x, str):
        x = [p for p in x.replace(";", ",").split(",")]
    return [str(i).strip()[:cap] for i in (x or []) if str(i).strip()][:n] if isinstance(x, (list, tuple)) else []


def make_id(kind: str, session: str, title: str, when: str) -> str:
    return "w" + hashlib.sha1(("%s|%s|%s|%s" % (kind, session, title, when)).encode()).hexdigest()[:11]


def normalize(item: Dict[str, Any], *, now: Optional[float] = None, prev: Optional[Dict[str, Any]] = None
              ) -> Dict[str, Any]:
    """A validated registry entry (raises ValueError on what cannot be stored)."""
    if not isinstance(item, dict):
        raise ValueError("an entry must be an object")
    kind = str(item.get("kind") or (prev or {}).get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % ", ".join(KINDS))
    title = str(item.get("title") or (prev or {}).get("title") or "").strip()[:160]
    if not title:
        raise ValueError("title is required")
    session = str(item.get("session") or (prev or {}).get("session") or "").strip()[:80]
    if kind == "request" and not session:
        raise ValueError("a request names the session to do it ('any' for whichever picks it up)")
    when = str(item.get("when") or (prev or {}).get("when") or "").strip()
    if when and _epoch(when) is None:
        raise ValueError("when must be an ISO date-time")
    statuses = REQUEST_STATUSES if kind == "request" else TRIGGER_STATUSES
    status = str(item.get("status") or (prev or {}).get("status") or statuses[0]).strip()
    if status not in statuses:
        raise ValueError("status must be one of %s" % ", ".join(statuses))
    now = time.time() if now is None else now
    out = dict(prev or {})
    out.update({
        "kind": kind, "title": title, "session": session,
        "session_id": str(item.get("session_id") or out.get("session_id") or "")[:80],
        "when": when, "window_end": str(item.get("window_end") or out.get("window_end") or "")[:40],
        "repeat": str(item.get("repeat") or out.get("repeat") or "")[:80],
        "tz": str(item.get("tz") or out.get("tz") or DEFAULT_TZ)[:40],
        "what": str(item.get("what") or out.get("what") or "")[:MAX_TEXT],
        "board_items": _strs(item.get("board_items") if "board_items" in item else out.get("board_items")),
        "branch": str(item.get("branch") or out.get("branch") or "")[:120],
        "log": str(item.get("log") or out.get("log") or "")[:300],
        "limits": dict(item.get("limits") or out.get("limits") or {}),
        "owner": str(item.get("owner") or out.get("owner") or "")[:80],
        "status": status, "note": str(item.get("note") or out.get("note") or "")[:400],
        "updated_at": _iso(now),
    })
    out.setdefault("created_at", _iso(now))
    out["repeat_ok"] = bool(out["repeat"]) and repeat_rule(out["repeat"]) is not None
    out["id"] = str(item.get("id") or out.get("id") or make_id(kind, session, title, when))
    return out


def view_status(entry: Dict[str, Any], now: Optional[float] = None) -> str:
    """The status to SHOW: an armed trigger or a pending request long past its time with no heartbeat
    reads 'expired' (its session or job died without saying so)."""
    st = str(entry.get("status") or "")
    if st not in ("armed", "pending", "running", "claimed"):
        return st
    now = time.time() if now is None else now
    fire = _epoch(entry.get("window_end")) or _epoch(entry.get("when"))
    beat = _epoch(entry.get("updated_at")) or 0
    if fire is not None and now > fire + STALE_AFTER_FIRE_S and beat < fire:
        return "expired"
    return st


def listing(entries: Iterable[Dict[str, Any]], *, session: str = "", kind: str = "", include_done: bool = False,
            now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Entries for the UI, soonest first, with `view_status`."""
    out = []
    for e in entries or []:
        if session and e.get("session") not in (session, "any"):
            continue
        if kind and e.get("kind") != kind:
            continue
        v = dict(e, view_status=view_status(e, now))
        if not include_done and v["view_status"] in ("done", "cancelled", "declined", "expired"):
            continue
        out.append(v)
    out.sort(key=lambda e: (_epoch(e.get("when")) or float("inf"), e.get("title", "")))
    return out


def pending_for(entries: Iterable[Dict[str, Any]], session: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Requests this session should pick up now: pending, for it (or 'any'), due (no time = now)."""
    now = time.time() if now is None else now
    out = []
    for e in entries or []:
        if e.get("kind") != "request" or view_status(e, now) != "pending":
            continue
        if e.get("session") not in (session, "any"):
            continue
        due = _epoch(e.get("when"))
        if due is None or due <= now:
            out.append(e)
    return sorted(out, key=lambda e: _epoch(e.get("when")) or 0)


# ── repeat rules (looplab-control B2, 17 Oct 2026) ──────────────────────────
# A trigger's `repeat` was free text nothing read. Two forms are now understood, so a repeating trigger
# rolls its `when` forward each time it fires and the central view knows its next fire:
#   every N (s|m|h|d), hourly, daily        - an interval from the last fire
#   cron: M H DOM MON DOW  (or the bare 5 fields) - standard cron in the entry's tz (default Europe/London)
DEFAULT_TZ = "Europe/London"
_EVERY = re.compile(r"^every\s+(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$", re.I)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _cron_field(text: str, lo: int, hi: int) -> Optional[set]:
    out = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                return None
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                return None
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
            if step > 1:
                b = hi
        else:
            return None
        if a < lo or b > hi or a > b:
            return None
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> Optional[tuple]:
    f = expr.split()
    if len(f) != 5:
        return None
    sets = [_cron_field(t, lo, hi) for t, (lo, hi) in zip(f, _CRON_RANGES)]
    if any(s is None for s in sets):
        return None
    dow = {0 if d == 7 else d for d in sets[4]}
    # cron's rule: when both day fields are restricted, a day matches EITHER
    dom_any, dow_any = f[2] == "*", f[4] == "*"
    return sets[0], sets[1], sets[2], sets[3], dow, dom_any, dow_any


def repeat_rule(text: str) -> Optional[tuple]:
    """('every', seconds) | ('cron', parsed) | None for text this does not understand."""
    t = str(text or "").strip()
    low = t.lower()
    if low == "hourly":
        return ("every", 3600)
    if low == "daily":
        return ("every", 86400)
    m = _EVERY.match(t)
    if m:
        n = int(m.group(1)) * _UNIT[m.group(2)[0].lower()]
        return ("every", n) if n >= 60 else None
    expr = t[5:].strip() if low.startswith("cron:") else t
    c = parse_cron(expr)
    return ("cron", c) if c else None


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return timezone.utc


def next_fire(text: str, after: float, tz_name: str = DEFAULT_TZ) -> Optional[float]:
    """The first fire strictly after `after` (epoch seconds), or None."""
    rule = repeat_rule(text)
    if rule is None:
        return None
    if rule[0] == "every":
        return after + rule[1]
    mins, hours, doms, mons, dows, dom_any, dow_any = rule[1]
    from datetime import timedelta
    tz = _tz(tz_name)
    start = datetime.fromtimestamp(after, tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.replace(hour=0, minute=0)
    for i in range(400):
        d = day + timedelta(days=i)
        if d.month not in mons:
            continue
        cron_dow = (d.weekday() + 1) % 7                  # cron: 0 = Sunday
        dom_ok, dow_ok = d.day in doms, cron_dow in dows
        if not (dom_ok and dow_ok if (dom_any or dow_any) else dom_ok or dow_ok):
            continue
        for h in sorted(hours):
            for mi in sorted(mins):
                cand = d.replace(hour=h, minute=mi)
                if cand >= start:
                    return cand.timestamp()
    return None


def roll(entry: Dict[str, Any], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A repeating trigger that fired (or a repeating request that was done): the same entry, armed /
    pending again at its next fire. None when it does not repeat (it stays done)."""
    now = time.time() if now is None else now
    text = entry.get("repeat") or ""
    rule = repeat_rule(text)
    if rule is None:
        return None
    # an interval counts from the planned fire when that is later than now (done early), else from now
    base = max(now, _epoch(entry.get("when")) or now) if rule[0] == "every" else now
    nxt = next_fire(text, base, entry.get("tz") or DEFAULT_TZ)
    if nxt is None:
        return None
    out = dict(entry)
    out["when"] = _iso(nxt)
    out["window_end"] = ""
    out["status"] = "pending" if entry.get("kind") == "request" else "armed"
    out["fired"] = int(entry.get("fired") or 0) + 1
    out["last_fired_at"] = _iso(now)
    out["updated_at"] = _iso(now)
    return out
