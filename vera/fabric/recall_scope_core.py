"""The time a question asks about, so memory is searched there first and then wider.

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.fabric.recall_scope_core`.

Owner, 2026-10-04 (roadmap CU-M.9): retrieval runs in widening tiers until the turn's need is met - scoped to the
time the turn implies, then widened, then complete, so nothing becomes unfindable. Measured cause, 2026-10-06:
Postgres text search ANDs every word of the query, so "what did we discuss yesterday" only found records that
contain the word "yesterday", and nothing limited the search to yesterday.

  time_scope(text, now)   {since, until, phrase} for the first time phrase found, or None
  strip(text, phrase)     the query without it (what the text search should look for)
  widen(scope, now, x)    the same window grown x times around its middle, never past now
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), start=1)}
_MON_RE = "|".join(list(_MONTHS) + [m[:3] for m in _MONTHS if m != "may"])
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10,
        "couple of": 2, "few": 3}
_N = r"(\d{1,3}|a|an|one|two|three|four|five|six|seven|ten|couple of|few)"

_PATTERNS = [
    ("earlier_today", re.compile(r"\b(earlier today|this morning|this afternoon|today)\b", re.I)),
    ("yesterday", re.compile(r"\b(yesterday|last night)\b", re.I)),
    ("ago", re.compile(r"\b" + _N + r"\s+(day|days|week|weeks|month|months)\s+ago\b", re.I)),
    ("last_n", re.compile(r"\b(?:in\s+)?(?:the\s+)?(?:last|past)\s+" + _N + r"\s+(day|days|week|weeks|month|months)\b",
                          re.I)),
    ("this_week", re.compile(r"\bthis week\b", re.I)),
    ("last_week", re.compile(r"\blast week\b", re.I)),
    ("this_month", re.compile(r"\bthis month\b", re.I)),
    ("last_month", re.compile(r"\blast month\b", re.I)),
    ("iso_date", re.compile(r"\b(?:on\s+)?(\d{4})-(\d{2})-(\d{2})\b", re.I)),
    ("month", re.compile(r"\b(?:in|back in|during|from)\s+(" + _MON_RE + r")\b(?:\s+(\d{4}))?", re.I)),
    ("recently", re.compile(r"\b(recently|lately|the other day)\b", re.I)),
]


def _day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _n(word: str) -> int:
    w = (word or "").lower()
    return int(w) if w.isdigit() else _NUM.get(w, 1)


def _unit_days(unit: str) -> int:
    u = unit.lower().rstrip("s")
    return {"day": 1, "week": 7, "month": 30}.get(u, 1)


def time_scope(text: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """{since, until, phrase, kind} (aware datetimes, UTC) for the first time phrase in `text`, else None."""
    s = str(text or "")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = _day(now)
    best = None
    for kind, rx in _PATTERNS:
        m = rx.search(s)
        if m and (best is None or m.start() < best[1].start()):
            best = (kind, m)
    if best is None:
        return None
    kind, m = best
    since, until = None, now
    if kind == "earlier_today":
        since = today
    elif kind == "yesterday":
        since, until = today - timedelta(days=1), today
    elif kind == "ago":
        d = _n(m.group(1)) * _unit_days(m.group(2))
        mid = today - timedelta(days=d)
        pad = max(1, d // 4)
        since, until = mid - timedelta(days=pad), min(now, mid + timedelta(days=pad + 1))
    elif kind == "last_n":
        since = today - timedelta(days=_n(m.group(1)) * _unit_days(m.group(2)))
    elif kind == "this_week":
        since = today - timedelta(days=today.weekday())
    elif kind == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        since, until = start, start + timedelta(days=7)
    elif kind == "this_month":
        since = today.replace(day=1)
    elif kind == "last_month":
        first = today.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        since, until = prev, first
    elif kind == "iso_date":
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
        since, until = d, d + timedelta(days=1)
    elif kind == "month":
        key = m.group(1).lower()
        mon = _MONTHS.get(key) or next(v for k, v in _MONTHS.items() if k.startswith(key))
        year = int(m.group(2)) if m.group(2) else (now.year if mon <= now.month else now.year - 1)
        since = datetime(year, mon, 1, tzinfo=timezone.utc)
        until = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=timezone.utc)
    elif kind == "recently":
        since = today - timedelta(days=14)
    if since is None or since >= until:
        return None
    return {"since": since, "until": min(until, now), "phrase": m.group(0), "kind": kind}


_WS = re.compile(r"\s+")


def strip(text: Any, phrase: str) -> str:
    """The query without the time phrase; the original when nothing would be left."""
    s = str(text or "")
    if not phrase:
        return s
    out = _WS.sub(" ", s.replace(phrase, " ")).strip(" ,.?!")
    return out if out else s


def widen(scope: Dict[str, Any], now: Optional[datetime] = None, factor: int = 4) -> Dict[str, Any]:
    """The window grown `factor` times around its middle (at least one more day each side), never past now."""
    now = now or datetime.now(timezone.utc)
    span = scope["until"] - scope["since"]
    grow = max(timedelta(days=1), span * (factor - 1) / 2)
    return dict(scope, since=scope["since"] - grow, until=min(now, scope["until"] + grow), kind="widened")
