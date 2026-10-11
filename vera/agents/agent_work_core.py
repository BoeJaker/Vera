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
