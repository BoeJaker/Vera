"""Server-side filters for the live event stream (DA-03a, DESK-SPINE-DESIGN.md §3.2).

A `/ws/mcp` client that subscribes with

    {"action": "subscribe_events", "filter": {"sid": "chat-1", "trace": ["t-chat-1-4"], "types": ["info.seek.", "cap."]}}

gets only the events that match - the session that triggered them, the calls it made (their trace ids), the event types
it reads (prefixes) - instead of the whole firehose. Every given part must match; an absent or empty part matches
anything. A subscribe with no filter gets everything, as before. Pure: no I/O, no orchestrator import.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

MAX_TRACES = 200      # a page follows its own calls; more than this is a client bug, not a filter
MAX_TYPES = 50


def _strs(value: Any, limit: int) -> tuple:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return ()
    out = []
    for item in value:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return tuple(out)


def normalize_event_filter(raw: Any) -> Optional[Dict[str, Any]]:
    """A subscriber's filter in one shape - {sid, trace (set), types (tuple of prefixes)} - or None for no filter
    (a missing, empty or unusable filter: the subscriber gets every event, as before)."""
    if not isinstance(raw, Mapping):
        return None
    sid = str(raw.get("sid") or "").strip()
    trace = frozenset(_strs(raw.get("trace"), MAX_TRACES))
    types = _strs(raw.get("types"), MAX_TYPES)
    if not sid and not trace and not types:
        return None
    return {"sid": sid, "trace": trace, "types": types}


def event_matches(event: Mapping[str, Any], flt: Optional[Mapping[str, Any]]) -> bool:
    """Does this event pass the subscriber's filter? No filter: yes."""
    if not flt:
        return True
    if not isinstance(event, Mapping):
        return False
    sid = flt.get("sid") or ""
    if sid and str(event.get("sid") or event.get("session_id") or "") != sid:
        return False
    trace = flt.get("trace") or ()
    if trace and str(event.get("trace_id") or event.get("trace") or "") not in trace:
        return False
    types = flt.get("types") or ()
    if types:
        t = str(event.get("type") or "")
        if not any(t.startswith(p) for p in types):
            return False
    return True


def filter_summary(flt: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the server says back on subscribe: the filter as it applies it (lists, not sets)."""
    if not flt:
        return {}
    return {"sid": flt.get("sid") or "", "trace": sorted(flt.get("trace") or ()), "types": list(flt.get("types") or ())}
