"""Who to message about a board item or plan (agent contacts).

Board claims and comments carry an agent identity (`claude:4dfcd766`, `codex:<run>`,
`loop-lab-scheduler`). That says who did something, not how to reach them. A Claude Code
session is reached by its session name (`vera-62`, via SendMessage/ListAgents); a Codex run
by whatever handle it publishes. A contact ties the two together:

    {"agent": "claude:4dfcd766", "session_name": "vera-62", "tool": "claude-code",
     "units": ["four-minds", "recon-fixes"], "note": "...", "last_seen": "<ISO>"}

so any session reading a board item (board.item.get / board.context) or a plan's README
"Reach:" line can message the owner directly.

Pure: no I/O.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

TOOLS = ("claude-code", "codex", "vera-delegate", "human", "service")
STALE_DAYS = 7
_AGENT_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{2,80}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_agent(agent: Any) -> str:
    """The identity a board comment would carry, trimmed. '' when unusable."""
    a = str(agent or "").strip()
    return a if _AGENT_RE.match(a) else ""


def parse_units(units: Any) -> List[str]:
    """Units as given: a list, a JSON list string, or comma-separated text. A Codex session sent
    a JSON list as text, which comma-splitting turned into entries like "['roadmap-activation'"."""
    if isinstance(units, str):
        s = units.strip()
        if s.startswith("["):
            try:
                import json as _json
                units = _json.loads(s)
            except Exception:
                units = s.strip("[]").split(",")
        else:
            units = s.split(",")
    out = []
    for u in units or []:
        u = str(u).strip().strip("'\"[] ").strip()
        if u and _UNIT_RE.match(u):
            out.append(u)
    return out


_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")


def make_contact(agent: str, session_name: str, tool: str = "claude-code",
                 units: Optional[Iterable[str]] = None, note: str = "",
                 last_seen: str = "") -> Dict[str, Any]:
    """A checked contact record. Raises ValueError with the reason when a field is unusable."""
    a = norm_agent(agent)
    if not a:
        raise ValueError("agent must be the board identity, e.g. claude:4dfcd766")
    s = str(session_name or "").strip()
    if not _SESSION_RE.match(s):
        raise ValueError("session_name must be the name others message, e.g. vera-62")
    t = str(tool or "claude-code").strip().lower()
    if t not in TOOLS:
        raise ValueError(f"tool must be one of {list(TOOLS)}")
    us = sorted(set(parse_units(units)))[:20]
    return {"agent": a, "session_name": s, "tool": t, "units": us,
            "note": str(note or "")[:200], "last_seen": last_seen or now_iso()}


def merge(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """A refresh keeps units seen before (a session's units accumulate) and a note
    unless a new one is given."""
    if not old:
        return new
    out = dict(new)
    out["units"] = sorted(set(parse_units(old.get("units") or [])) | set(new.get("units") or []))[:20]
    if not new.get("note"):
        out["note"] = old.get("note", "")
    return out


def stale(contact: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True when the contact was last seen more than STALE_DAYS ago (the session may be gone)."""
    try:
        seen = datetime.fromisoformat(str(contact.get("last_seen", "")).replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - seen).days > STALE_DAYS


def identities_on_item(item: Dict[str, Any]) -> List[str]:
    """Every agent identity on a board item, owner first: the owner, the agent:<x> labels,
    then each comment's sender in order."""
    seen: List[str] = []

    def add(a: Any) -> None:
        a = norm_agent(a)
        if a and a not in seen:
            seen.append(a)

    add(item.get("agent"))
    for lab in item.get("labels") or []:
        if str(lab).startswith("agent:"):
            add(str(lab)[6:])
    for c in item.get("comments") or []:
        add(c.get("from") or c.get("frm"))
    return seen


def contacts_for(identities: Iterable[str], registry: Dict[str, Dict[str, Any]],
                 now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """{identity: {session_name, tool, last_seen, stale}} for the identities that have a contact."""
    out: Dict[str, Dict[str, Any]] = {}
    for a in identities:
        c = registry.get(a)
        if c:
            out[a] = {"session_name": c.get("session_name"), "tool": c.get("tool"),
                      "last_seen": c.get("last_seen"), "stale": stale(c, now)}
    return out


def reach_line(contact: Dict[str, Any]) -> str:
    """The README / handover line other sessions read: 'Reach: vera-62 (claude-code, claude:4dfcd766)'."""
    return f"Reach: {contact['session_name']} ({contact['tool']}, {contact['agent']})"
