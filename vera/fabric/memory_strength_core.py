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

POLICY_VERSION = "2026-10-05.3"   # .2 classed prod's dream_report/extraction_result/cap_result/dag; .3 a transcript line is a transcript

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
    # an imported coding-agent transcript line first: they are stored as record_type "message", and on prod they are
    # 76,130 of the 82,137 "message" records - read as conversation they hid the real chat in the fade report
    text = str((record or {}).get("text") or "").lstrip()
    if text.startswith(("[Claude Code", "[Codex")):
        return "transcript"
    for key in ("record_type", "source_type", "category"):
        c = _TYPE_CLASS.get(str((record or {}).get(key) or "").strip().lower())
        if c:
            return c
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


# ── the user's controls (owner, 2026-10-05: "add controls for the user to select what gets forgotten and how quickly") ──
HALF_LIFE_RANGE = (0.5, 3650.0)      # days
FLOOR_RANGE = (0.0, 0.5)


def policy_from(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """The policy in force: every class's {enabled (may it be forgotten at all), half_life_days (None = no decay),
    floor}, the defaults above with the user's overrides laid on. Unknown classes and malformed values are ignored;
    numbers are clamped, so no setting can make every memory fade in a day."""
    out: Dict[str, Dict[str, Any]] = {}
    for cls, (hl, floor) in HALF_LIFE.items():
        out[cls] = {"enabled": hl is not None and floor > 0, "half_life_days": hl, "floor": floor}
    for cls, o in (overrides or {}).items() if isinstance(overrides, dict) else []:
        if cls not in out or not isinstance(o, dict):
            continue
        cur = out[cls]
        if isinstance(o.get("enabled"), bool):
            cur["enabled"] = o["enabled"]
        if "half_life_days" in o:
            v = o["half_life_days"]
            if v is None:
                cur["half_life_days"] = None
            else:
                try:
                    cur["half_life_days"] = max(HALF_LIFE_RANGE[0], min(HALF_LIFE_RANGE[1], float(v)))
                except (TypeError, ValueError):
                    pass
        if "floor" in o:
            try:
                cur["floor"] = max(FLOOR_RANGE[0], min(FLOOR_RANGE[1], float(o["floor"])))
            except (TypeError, ValueError):
                pass
        if cur["half_life_days"] is None or cur["floor"] <= 0:
            cur["enabled"] = False          # nothing to fade by
    return out


_UNSET = object()


def effective_half_life(cls: str, used_n: int = 0, injected_n: int = 0, base: Any = _UNSET) -> Optional[float]:
    """Days. Each use (up to 10 counted) adds half the base; each injection that was ignored (up to 20) takes a tenth
    off - so a memory the model keeps drawing on lasts, and one it keeps being given and ignoring goes sooner.
    `base` is the class's half-life in force (the user's setting); unset reads the default table."""
    if base is _UNSET:
        base = HALF_LIFE.get(cls, HALF_LIFE[DEFAULT_CLASS])[0]
    if base is None:
        return None
    used = max(0, min(int(used_n or 0), 10))
    ignored = max(0, min(int(injected_n or 0) - int(used_n or 0), 20))
    return base * (1.0 + 0.5 * used) / (1.0 + 0.1 * ignored)


def strength(record: Dict[str, Any], usage: Optional[Dict[str, Any]] = None, now: Optional[float] = None,
             policy: Optional[Dict[str, Dict[str, Any]]] = None) -> float:
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
    if policy and cls in policy:
        hl = effective_half_life(cls, use.get("used_n", 0), use.get("injected_n", 0), policy[cls].get("half_life_days"))
    else:
        hl = effective_half_life(cls, use.get("used_n", 0), use.get("injected_n", 0))
    if hl is None:
        return importance
    since = _ts(use.get("last_used")) or _ts(rec.get("created_at")) or now
    age_days = max(0.0, (now - since) / _DAY)
    return round(importance * math.pow(0.5, age_days / max(hl, 1e-6)), 6)


def fades(record: Dict[str, Any], usage: Optional[Dict[str, Any]] = None, now: Optional[float] = None,
          policy: Optional[Dict[str, Dict[str, Any]]] = None) -> bool:
    """Below its class's floor, in a class the user lets be forgotten: the fading job would archive it - softly, and a
    strong match can still bring it back (owner, 2026-10-05)."""
    cls = class_of(record or {})
    if policy and cls in policy:
        if not policy[cls].get("enabled"):
            return False
        floor = float(policy[cls].get("floor") or 0.0)
    else:
        floor = HALF_LIFE.get(cls, HALF_LIFE[DEFAULT_CLASS])[1]
    return floor > 0 and strength(record, usage, now, policy) < floor
