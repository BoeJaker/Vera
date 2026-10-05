"""How strong a memory still is: a half-life per kind of record, renewed by use (context-and-ui CU-M.3).

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.fabric.memory_strength_core`.

Owner, 2026-10-04: "memory feedback with half-lives - useless session memories fade, useful ones stay". A record's
strength halves every half-life since it was last USED (or made, if never used); each use renews it and makes its
half-life longer; being put in front of the model again and again and ignored makes it shorter. Facts and preferences
do not decay. The fading job (CU-M.4) archives below a class's floor - softly, with a reason, revived on use - and
only after a dry run the owner has read. Four-minds FM-7.2 owns the ranking that consumes this; on a conflicting
number theirs wins (POLICY_VERSION says which table is in force).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

POLICY_VERSION = "2026-10-05.2"   # .2: dream_report, extraction_result, cap_result and dag classed (prod's types)

# class -> (half-life in days, floor). None = no decay (importance alone decides). From the roadmap's CU-M.3 table.
HALF_LIFE: Dict[str, tuple] = {
    "conversation": (14.0, 0.05),
    "session": (7.0, 0.05),
    "observation": (30.0, 0.05),
    "fact": (None, 0.0),
    "preference": (None, 0.0),
    "research": (60.0, 0.05),
    "dream": (21.0, 0.05),
    "estate": (1.0, 0.02),
    "tool": (3.0, 0.02),
    "transcript": (14.0, 0.05),
}
DEFAULT_CLASS = "conversation"

# record_type / source_type -> class
_TYPE_CLASS = {
    "message": "conversation", "chat": "conversation", "turn": "conversation",
    "session": "session", "summary": "session", "note": "session",
    "observation": "observation", "event": "observation", "insight": "observation",
    "fact": "fact", "entity": "fact", "preference": "preference", "profile": "preference",
    "research": "research", "report": "research", "document": "research",
    "dream": "dream", "dream_report": "dream",
    "extraction_result": "observation",
    "estate": "estate", "telemetry": "estate", "metric": "estate", "health": "estate",
    "cap_call": "tool", "dag_step": "tool", "tool": "tool", "cap_result": "tool", "dag": "tool",
    "transcript": "transcript",
}
_DAY = 86400.0


def class_of(record: Dict[str, Any]) -> str:
    """A record's half-life class, from what it is (record_type, then source_type, then category)."""
    for key in ("record_type", "source_type", "category"):
        c = _TYPE_CLASS.get(str((record or {}).get(key) or "").strip().lower())
        if c:
            return c
    text = str((record or {}).get("text") or "").lstrip()
    if text.startswith(("[Claude Code", "[Codex")):
        return "transcript"
    return DEFAULT_CLASS


def _ts(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp()
    try:
        s = str(v).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def effective_half_life(cls: str, used_n: int = 0, injected_n: int = 0) -> Optional[float]:
    """Days. Each use (up to 10 counted) adds half the base; each injection that was ignored (up to 20) takes a tenth
    off - so a memory the model keeps drawing on lasts, and one it keeps being given and ignoring goes sooner."""
    base = HALF_LIFE.get(cls, HALF_LIFE[DEFAULT_CLASS])[0]
    if base is None:
        return None
    used = max(0, min(int(used_n or 0), 10))
    ignored = max(0, min(int(injected_n or 0) - int(used_n or 0), 20))
    return base * (1.0 + 0.5 * used) / (1.0 + 0.1 * ignored)


def strength(record: Dict[str, Any], usage: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> float:
    """0..1: the record's importance, halved every effective half-life since it was last used (or made)."""
    rec = record or {}
    use = usage or {}
    now = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    try:
        importance = float(rec.get("importance", 0.5) if rec.get("importance") is not None else 0.5)
    except (TypeError, ValueError):
        importance = 0.5
    importance = max(0.0, min(1.0, importance))
    cls = class_of(rec)
    hl = effective_half_life(cls, use.get("used_n", 0), use.get("injected_n", 0))
    if hl is None:
        return importance
    since = _ts(use.get("last_used")) or _ts(rec.get("created_at")) or now
    age_days = max(0.0, (now - since) / _DAY)
    return round(importance * math.pow(0.5, age_days / max(hl, 1e-6)), 6)


def fades(record: Dict[str, Any], usage: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> bool:
    """Below its class's floor: the fading job (CU-M.4) would archive it - softly, revived on use."""
    cls = class_of(record or {})
    floor = HALF_LIFE.get(cls, HALF_LIFE[DEFAULT_CLASS])[1]
    return floor > 0 and strength(record, usage, now) < floor
