"""Known dated facts before planning (compute-roles P1b, rules agreed with intelligence 11 Oct 2026).

A research / "latest" goal asks intel.lookup (milliseconds, no web) what Vera's own tracking already
has on the subject. A tracked topic's dated signals go to the planner as a KNOWN block and into the
run's evidence ledger (so the writer can cite them and the grounding check counts them). Rules from
the intelligence unit (vera-03):
  * tracked:true only - near-miss `matches` are never facts;
  * "latest" goals ask since now-7d (the lookup's default is 14d);
  * an item is dated by `published`; with no publication date it is labelled "seen <date>" (a crawl
    date is never presented as a publication date); with neither, a "latest" goal drops it;
  * the topic's snapshot headline is intel's own ASSESSMENT, not a source: shown only when
    age_days <= 7, labelled as such, never added as evidence;
  * rows come ordered by score - the block reads newest first.

Pure: a lookup view in, evidence items / a planner block out.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

LATEST_DAYS = 7
ASSESSMENT_MAX_AGE_DAYS = 7
MAX_ITEMS = 8
_LATEST = re.compile(r"\b(latest|recent(ly)?|news|this (week|month)|today|current(ly)?|right now|"
                     r"as of|since|so far|up[- ]to[- ]date|newest|just (released|announced))\b", re.I)


def is_latest(goal: str) -> bool:
    """Whether the goal asks for what is new / current."""
    return bool(_LATEST.search(str(goal or "")))


def since_for(goal: str, now: Optional[datetime] = None) -> str:
    """The lookup's `since`: now-7d for a "latest" goal, "" (the lookup's own 14d) otherwise."""
    if not is_latest(goal):
        return ""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=LATEST_DAYS)).strftime("%Y-%m-%d")


def items(view: Dict[str, Any], latest: bool) -> List[Dict[str, Any]]:
    """Evidence items (the ledger's info.seek item shape) from a lookup view, newest first;
    [] unless the topic is tracked."""
    if not isinstance(view, dict) or view.get("tracked") is not True:
        return []
    rows = []
    for s in view.get("signals") or []:
        if not isinstance(s, dict) or not s.get("url"):
            continue
        pub, seen = str(s.get("published") or "")[:10], str(s.get("seen") or "")[:10]
        if pub:
            date, key = pub, pub
        elif seen:
            date, key = "seen " + seen, seen
        elif latest:
            continue
        else:
            date, key = "", ""
        rows.append((key, {"title": str(s.get("title") or "")[:160], "url": s["url"], "published": date,
                           "text": str(s.get("summary") or "")[:600], "source": "intel"}))
    rows.sort(key=lambda r: r[0], reverse=True)
    out = []
    for k, (_key, it) in enumerate(rows[:MAX_ITEMS]):
        it["n"] = k + 1
        out.append(it)
    return out


def assessment(view: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The topic's own assessment when it is fresh enough to show (never evidence)."""
    snap = (view or {}).get("snapshot") if isinstance(view, dict) else None
    if not isinstance(snap, dict) or not str(snap.get("headline") or "").strip():
        return None
    age = snap.get("age_days")
    if not isinstance(age, (int, float)) or age > ASSESSMENT_MAX_AGE_DAYS:
        return None
    return {"headline": str(snap["headline"])[:400], "ts": str(snap.get("ts") or "")[:10]}


def block(its: List[Dict[str, Any]], topic: str = "", assess: Optional[Dict[str, str]] = None) -> str:
    """The planner's KNOWN block ("" when there is nothing to show)."""
    if not its and not assess:
        return ""
    lines = ["KNOWN (dated, from Vera's own tracking%s - plan around what is already known, look up "
             "what is missing or newer, and cite these as evidence where used):" %
             (" of '%s'" % topic if topic else "")]
    for it in its:
        lines.append("- [%s] %s - %s (%s)" % (it.get("published") or "undated", it.get("title"),
                                             str(it.get("text") or "")[:200], it.get("url")))
    if assess:
        lines.append("INTEL ASSESSMENT (%s; Vera's own view of the topic, not a source - do not cite "
                     "it): %s" % (assess.get("ts") or "undated", assess.get("headline")))
    return "\n".join(lines)
