"""
ical.py — Minimal, dependency-free iCalendar (RFC-5545) parser & serializer
============================================================================

Just enough of RFC-5545 to pull VEVENTs out of ICS feeds and CalDAV REPORT
responses, and to emit a single VEVENT for future write-back. No third-party
deps — `dateutil` is used opportunistically for fuzzy parsing if importable,
else stdlib `datetime` handles the standard ICS date forms.

Public API
──────────
  parse(text)                         -> list[dict]   (normalised event dicts)
  expand(event, window_start, window_end) -> list[dict]   (simple RRULE expand)
  occurrences(events, window_start, window_end) -> list[dict]
                                      (every concrete event in the window: series
                                       expanded with dateutil.rrule, EXDATEs dropped,
                                       moved/cancelled instances applied)
  serialize(event)                    -> str          (a VCALENDAR with 1 VEVENT)

Normalised event dict keys:
  uid, title, start (ISO8601), end (ISO8601 or ""), all_day (bool),
  location, description, rrule (raw string or ""), recurrence_id (ISO or ""),
  exdates (list of ISO), status ("" | CONFIRMED | TENTATIVE | CANCELLED)
A DTSTART/DTEND with a TZID the stdlib knows becomes an aware ISO time (offset).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import uuid
from typing import Dict, List, Optional

log = logging.getLogger("vera.calendar.ical")

try:                                   # optional, already in the environment
    from dateutil import parser as _du_parser  # type: ignore
except Exception:                      # pragma: no cover
    _du_parser = None

try:                                   # python-dateutil (BSD/Apache, in prod's venv and the gate image)
    from dateutil import rrule as _du_rrule  # type: ignore
except Exception:                      # pragma: no cover
    _du_rrule = None

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except Exception:                      # pragma: no cover
    _ZoneInfo = None


def _zone(tzid: str):
    if not tzid or _ZoneInfo is None:
        return None
    try:
        return _ZoneInfo(tzid.strip('"'))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# LINE UNFOLDING + TOKENISING
# ─────────────────────────────────────────────────────────────────────────────

def _unfold(text: str) -> List[str]:
    """RFC-5545 §3.1: a CRLF followed by space/tab is a line continuation."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split_prop(line: str):
    """'DTSTART;TZID=Europe/London:20240115T130000' -> (name, params, value)."""
    if ":" not in line:
        return None, {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: Dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name, params, value


def _unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


# ─────────────────────────────────────────────────────────────────────────────
# DATE PARSING
# ─────────────────────────────────────────────────────────────────────────────

def _parse_dt(value: str, params: Dict[str, str]):
    """Return (iso_string, all_day_bool). Best-effort, never raises."""
    value = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or (
        len(value) == 8 and value.isdigit())
    try:
        if is_date:                                   # YYYYMMDD (all-day)
            d = _dt.datetime.strptime(value[:8], "%Y%m%d")
            return d.date().isoformat(), True
        if value.endswith("Z"):                       # UTC
            d = _dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return d.replace(tzinfo=_dt.timezone.utc).isoformat(), False
        if "T" in value and len(value) >= 15:         # local / TZID floating
            d = _dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
            z = _zone(params.get("TZID", ""))
            if z is not None:                         # a named zone: an aware time
                d = d.replace(tzinfo=z)
            return d.isoformat(), False
    except Exception:
        pass
    # Fallback: let dateutil try, else give up gracefully.
    if _du_parser:
        try:
            return _du_parser.parse(value).isoformat(), is_date
        except Exception:
            pass
    log.debug("ical: could not parse date %r", value)
    return value, is_date


# ─────────────────────────────────────────────────────────────────────────────
# PARSE
# ─────────────────────────────────────────────────────────────────────────────

def parse(text: str) -> List[Dict]:
    """Extract VEVENT blocks from ICS / VCALENDAR text into normalised dicts."""
    if not text:
        return []
    events: List[Dict] = []
    cur: Optional[Dict] = None
    for line in _unfold(text):
        u = line.strip()
        if u == "BEGIN:VEVENT":
            cur = {"uid": "", "title": "", "start": "", "end": "",
                   "all_day": False, "location": "", "description": "",
                   "rrule": "", "recurrence_id": "", "exdates": [], "status": ""}
            continue
        if u == "END:VEVENT":
            if cur is not None:
                if not cur.get("uid"):
                    cur["uid"] = str(uuid.uuid4())
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        name, params, value = _split_prop(line)
        if not name:
            continue
        if name == "SUMMARY":
            cur["title"] = _unescape(value)
        elif name == "UID":
            cur["uid"] = value.strip()
        elif name == "LOCATION":
            cur["location"] = _unescape(value)
        elif name == "DESCRIPTION":
            cur["description"] = _unescape(value)
        elif name == "DTSTART":
            cur["start"], cur["all_day"] = _parse_dt(value, params)
            if _zone(params.get("TZID", "")) is not None:
                cur["tzid"] = params["TZID"].strip('"')   # an ISO offset is fixed; a series needs the zone
        elif name == "DTEND":
            iso, _ = _parse_dt(value, params)
            cur["end"] = iso
        elif name == "RRULE":
            cur["rrule"] = value.strip()
        elif name == "RECURRENCE-ID":                 # this VEVENT moves/cancels one instance
            cur["recurrence_id"], _ = _parse_dt(value, params)
        elif name == "EXDATE":                        # may repeat, and may hold a comma list
            for v in value.split(","):
                if v.strip():
                    cur["exdates"].append(_parse_dt(v, params)[0])
        elif name == "STATUS":
            cur["status"] = value.strip().upper()
    return events


# ─────────────────────────────────────────────────────────────────────────────
# SIMPLE RECURRENCE EXPANSION (DAILY / WEEKLY only, within a window)
# ─────────────────────────────────────────────────────────────────────────────

def _rrule_parts(rrule: str) -> Dict[str, str]:
    return {k.upper(): v for k, v in
            (p.split("=", 1) for p in rrule.split(";") if "=" in p)}


def expand(event: Dict, window_start: _dt.datetime,
           window_end: _dt.datetime) -> List[Dict]:
    """Expand a simple DAILY/WEEKLY RRULE into concrete instances in a window.

    Anything more exotic (MONTHLY, BYDAY lists, etc.) is returned as the single
    base event — we deliberately do not attempt full RFC-5545 recurrence here.
    """
    rrule = event.get("rrule") or ""
    if not rrule:
        return [event]
    parts = _rrule_parts(rrule)
    freq = parts.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        return [event]
    try:
        base = _dt.datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
    except Exception:
        return [event]
    base_naive = base.replace(tzinfo=None)
    step = _dt.timedelta(days=1 if freq == "DAILY" else 7) * int(parts.get("INTERVAL", "1") or 1)
    count_cap = int(parts.get("COUNT", "0") or 0)
    until = None
    if parts.get("UNTIL"):
        try:
            until = _dt.datetime.strptime(parts["UNTIL"][:15], "%Y%m%dT%H%M%S")
        except Exception:
            until = None

    out: List[Dict] = []
    cur = base_naive
    guard = 0
    while cur <= window_end.replace(tzinfo=None) and guard < 1000:
        guard += 1
        if until and cur > until:
            break
        if cur >= window_start.replace(tzinfo=None):
            inst = dict(event)
            inst["start"] = cur.isoformat()
            inst["rrule"] = ""           # instance is concrete
            inst["uid"] = f"{event['uid']}_{cur.date().isoformat()}"
            out.append(inst)
        if count_cap and len(out) >= count_cap:
            break
        cur += step
    return out or [event]


# ─────────────────────────────────────────────────────────────────────────────
# OCCURRENCES  (full RFC-5545 recurrence via dateutil.rrule, within a window)
# ─────────────────────────────────────────────────────────────────────────────
# The sync used to store each VEVENT as it came: a weekly series appeared once (at its first DTSTART), and a moved
# instance (same UID + RECURRENCE-ID) overwrote the whole series (S23 / D24-4 C-1). dateutil.rrule is used rather
# than a new dependency: it is already in prod's venv and the gate image (icalendar / recurring-ical-events are not).

MAX_INSTANCES = 2000                   # per series, per window


def _to_dt(iso: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None


def _wall(d: _dt.datetime, tz) -> _dt.datetime:
    """A naive wall-clock time in the series' zone (tz None: UTC for aware times), to match instances by."""
    if d.tzinfo is None:
        return d
    return d.astimezone(tz or _dt.timezone.utc).replace(tzinfo=None)


def _instance_key(wall: _dt.datetime, all_day: bool) -> str:
    return wall.strftime("%Y%m%d") if all_day else wall.strftime("%Y%m%dT%H%M%S")


def _fix_until(rrule: str, aware: bool) -> str:
    """dateutil refuses an UNTIL whose UTC-ness differs from DTSTART's; feeds get this wrong often."""
    def sub(m):
        v = m.group(1)
        if aware and not v.endswith("Z") and "T" in v:
            v += "Z"
        elif not aware and v.endswith("Z"):
            v = v[:-1]
        return "UNTIL=" + v
    return re.sub(r"UNTIL=([0-9TZ]+)", sub, rrule)


def _master_start(master: Dict) -> Optional[_dt.datetime]:
    """The series' first start, in its named zone when it has one (so instances keep their wall time across a
    clock change: 09:00 stays 09:00 in GMT and in BST)."""
    start = _to_dt(master.get("start", ""))
    z = _zone(master.get("tzid", ""))
    if start is not None and z is not None:
        start = start.astimezone(z) if start.tzinfo else start.replace(tzinfo=z)
    return start


def _series(master: Dict, ws: _dt.datetime, we: _dt.datetime) -> List[tuple]:
    """(wall start, start datetime) of each instance of a master in the window, or None when it cannot expand."""
    start = _master_start(master)
    if start is None or _du_rrule is None:
        return None
    aware = start.tzinfo is not None
    end = _to_dt(master.get("end", "")) or start
    dur = max(end - start, _dt.timedelta(0))
    lo, hi = ws - dur, we
    if aware:
        lo, hi = lo.astimezone(start.tzinfo), hi.astimezone(start.tzinfo)
    else:
        lo, hi = lo.replace(tzinfo=None), hi.replace(tzinfo=None)
    try:
        rule = _du_rrule.rrulestr(_fix_until(master["rrule"], aware), dtstart=start)
        hits = []
        for d in rule.xafter(lo, inc=True):
            if d > hi or len(hits) >= MAX_INSTANCES:
                break
            hits.append(d)
    except Exception as e:
        log.debug("ical: rrule %r on %s: %s", master.get("rrule"), master.get("uid"), e)
        return None
    tz = start.tzinfo
    return [(_wall(d, tz), d) for d in hits]


def occurrences(events: List[Dict], window_start: _dt.datetime, window_end: _dt.datetime) -> List[Dict]:
    """Every concrete event the parsed feed puts in the window.

    A series (RRULE) becomes one event per instance, id'd `<uid>_<wall start>` so a re-sync updates the same rows;
    EXDATEs are left out; a VEVENT with the series' UID and a RECURRENCE-ID replaces that one instance (or removes
    it when CANCELLED); a cancelled event or series is left out. A one-off event is returned as before, window or
    not. A series that cannot be expanded is returned as its one base event (the old behaviour)."""
    ws = window_start if window_start.tzinfo else window_start.replace(tzinfo=_dt.timezone.utc)
    we = window_end if window_end.tzinfo else window_end.replace(tzinfo=_dt.timezone.utc)
    masters: Dict[str, Dict] = {}
    overrides: Dict[str, List[Dict]] = {}
    out: List[Dict] = []
    for ev in events:
        if ev.get("recurrence_id"):
            overrides.setdefault(ev.get("uid", ""), []).append(ev)
        elif ev.get("rrule"):
            masters[ev.get("uid", "")] = ev
        elif ev.get("status") != "CANCELLED":
            out.append(ev)

    for uid, m in masters.items():
        if m.get("status") == "CANCELLED":
            continue
        inst = _series(m, ws, we)
        if inst is None:
            out.append(m)
            continue
        start = _master_start(m)
        tz, all_day = start.tzinfo, bool(m.get("all_day"))
        dur = max((_to_dt(m.get("end", "")) or start) - start, _dt.timedelta(0))
        skip = {_instance_key(_wall(d, tz), all_day) for d in (_to_dt(x) for x in m.get("exdates") or []) if d}
        moved = {}
        for o in overrides.pop(uid, []):
            rid = _to_dt(o["recurrence_id"])
            if rid is not None:
                moved[_instance_key(_wall(rid, tz), all_day)] = o
        for wall, d in inst:
            key = _instance_key(wall, all_day)
            if key in skip:
                continue
            o = moved.pop(key, None)
            if o is not None:
                if o.get("status") != "CANCELLED":
                    out.append({**m, **o, "rrule": "", "exdates": [], "uid": f"{uid}_{key}"})
                continue
            row = {**m, "rrule": "", "exdates": [], "uid": f"{uid}_{key}"}
            if all_day:
                row["start"] = d.date().isoformat()
                row["end"] = (d + dur).date().isoformat() if m.get("end") else ""
            else:
                row["start"] = d.isoformat()
                row["end"] = (d + dur).isoformat() if m.get("end") else ""
            out.append(row)
        for key, o in moved.items():           # moved INTO the window from an instance outside it
            if o.get("status") != "CANCELLED":
                out.append({**m, **o, "rrule": "", "exdates": [], "uid": f"{uid}_{key}"})

    for uid, os_ in overrides.items():        # instances whose series is not in this feed
        for o in os_:
            if o.get("status") == "CANCELLED":
                continue
            rid = _to_dt(o["recurrence_id"])
            key = _instance_key(_wall(rid, None), bool(o.get("all_day"))) if rid else o.get("recurrence_id", "")
            out.append({**o, "uid": f"{uid}_{key}"})
    return out


def series_uids(events: List[Dict]) -> set:
    """UIDs of the recurring series in a parsed feed (their old one-row copies are superseded by instances)."""
    return {e.get("uid", "") for e in events if e.get("rrule") and not e.get("recurrence_id")}


# ─────────────────────────────────────────────────────────────────────────────
# SERIALIZE  (single VEVENT — used for future CalDAV write-back)
# ─────────────────────────────────────────────────────────────────────────────

def _esc(v: str) -> str:
    return (str(v or "").replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _fmt_dt(iso: str, all_day: bool) -> str:
    try:
        d = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return iso
    if all_day:
        return d.strftime("%Y%m%d")
    if d.tzinfo:
        return d.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return d.strftime("%Y%m%dT%H%M%S")


def serialize(event: Dict) -> str:
    uid = event.get("uid") or str(uuid.uuid4())
    all_day = bool(event.get("all_day"))
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vera//Calendar//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{_esc(event.get('title'))}",
    ]
    if event.get("start"):
        prefix = "DTSTART;VALUE=DATE:" if all_day else "DTSTART:"
        lines.append(prefix + _fmt_dt(event["start"], all_day))
    if event.get("end"):
        prefix = "DTEND;VALUE=DATE:" if all_day else "DTEND:"
        lines.append(prefix + _fmt_dt(event["end"], all_day))
    if event.get("location"):
        lines.append(f"LOCATION:{_esc(event['location'])}")
    if event.get("description"):
        lines.append(f"DESCRIPTION:{_esc(event['description'])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"
