"""
syslog_summary_core.py
======================
Pure summary of the system log's warnings and errors over a time window - what the Observe page leads with (owner,
2026-09-28: "the widgets in observe panel are not focused on errors and they must be").

No Redis, no app imports: syslog.error_summary reads the entries and hands them here, so the counting is testable on its
own (tests/test_syslog_error_summary.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

ERROR_LEVELS = ("ERROR", "CRITICAL")
WARN_LEVELS = ("WARNING",)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def entry_ms(redis_id: str) -> int:
    """A Redis stream id is '<ms>-<seq>'; its first half is when the entry was written."""
    try:
        return int(str(redis_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return 0


def summarise(entries: Iterable[Tuple[str, Dict[str, Any]]], now_ms: int, window_s: int = 3600,
              bucket_s: int = 300, limit: int = 50) -> Dict[str, Any]:
    """entries: (redis_id, record) newest first, any levels. Returns counts over the window, by capability and by
    category, a per-bucket series, and the newest `limit` warnings/errors."""
    window_s = max(60, int(window_s or 3600))
    bucket_s = max(30, min(int(bucket_s or 300), window_s))
    lo = now_ms - window_s * 1000
    nb = max(1, -(-window_s // bucket_s))
    series = [{"t": _iso(lo + i * bucket_s * 1000), "errors": 0, "warnings": 0} for i in range(nb)]
    by_cap: Dict[str, Dict[str, Any]] = {}
    by_cat: Dict[str, int] = {}
    errors = warnings = critical = 0
    newest: List[Dict[str, Any]] = []
    recent_errors: List[Dict[str, Any]] = []          # ERROR / CRITICAL only - an empty list is "none this hour"
    last_error: Optional[Dict[str, Any]] = None
    for rid, rec in entries:
        ms = entry_ms(rid)
        if ms and ms < lo:
            break                                   # newest first: everything after this is older still
        lvl = str(rec.get("level", "")).upper()
        is_err, is_warn = lvl in ERROR_LEVELS, lvl in WARN_LEVELS
        if not (is_err or is_warn):
            continue
        if is_err:
            errors += 1
            critical += lvl == "CRITICAL"
        else:
            warnings += 1
        name = rec.get("cap_name") or rec.get("category") or "system"
        c = by_cap.setdefault(name, {"name": name, "errors": 0, "warnings": 0, "count": 0, "last": ""})
        c["errors" if is_err else "warnings"] += 1
        c["count"] += 1
        c["last"] = c["last"] or rec.get("ts", "")
        cat = rec.get("category") or "system"
        by_cat[cat] = by_cat.get(cat, 0) + 1
        if ms:
            i = min(nb - 1, max(0, (ms - lo) // (bucket_s * 1000)))
            series[i]["errors" if is_err else "warnings"] += 1
        row = {"ts": rec.get("ts", ""), "level": lvl, "category": rec.get("category", ""),
               "cap_name": rec.get("cap_name", ""), "message": str(rec.get("message", ""))[:300],
               "_redis_id": rec.get("_redis_id", rid)}
        if is_err and last_error is None:
            last_error = row
        if is_err and len(recent_errors) < limit:
            recent_errors.append(row)
        if len(newest) < limit:
            newest.append(row)
    caps = sorted(by_cap.values(), key=lambda c: (-c["errors"], -c["count"], c["name"]))
    return {
        "window_s": window_s,
        "errors": errors,
        "warnings": warnings,
        "critical": critical,
        "total": errors + warnings,
        "by_cap": caps,
        "by_category": [{"name": k, "count": v} for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])],
        "series": series,
        "entries": newest,
        "recent_errors": recent_errors,
        "last_error": last_error,
    }
