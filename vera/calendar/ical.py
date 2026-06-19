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
  serialize(event)                    -> str          (a VCALENDAR with 1 VEVENT)

Normalised event dict keys:
  uid, title, start (ISO8601), end (ISO8601 or ""), all_day (bool),
  location, description, rrule (raw string or "")
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
                   "rrule": ""}
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
        elif name == "DTEND":
            iso, _ = _parse_dt(value, params)
            cur["end"] = iso
        elif name == "RRULE":
            cur["rrule"] = value.strip()
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
