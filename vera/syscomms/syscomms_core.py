"""
syscomms_core.py — pure logic for the System Comms pane
=======================================================

Vera talks to its operator through several mouths — Telegram notifications, the
action list, the nightly n8n review, archived reports — and each has its own
shape, its own timestamp field and its own idea of what "important" means.
Reading them meant opening four places and mentally merging them.

This normalises all of it into ONE timeline entry shape so the Comms page can
show a single ordered stream:

    {ts, source, kind, severity, title, detail, ref}

Severity is derived, never trusted from the source: a Telegram message
containing "DOWN:" is a warning whether or not anything labelled it one.

Pure — no I/O, no app import — so it is unit-testable without booting Vera
(`tests/test_syscomms_core.py` imports it as `vera.syscomms.syscomms_core`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

SEVERITIES = ("critical", "warning", "info")
_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}

# Phrases that mean something is wrong, wherever they appear. Ordered: the
# first match wins, so the more serious patterns come first.
_CRITICAL_RE = re.compile(
    r"\b(down|failed|failure|crashed|unreachable|error|blocked|refused)\b", re.I)
_WARNING_RE = re.compile(
    r"\b(warn|warning|stale|inactive|missing|degraded|pending|overdue)\b", re.I)


def parse_ts(value: Any) -> str:
    """Normalise assorted timestamp shapes to a sortable ISO-8601 string.

    Sources disagree: Telegram sends unix seconds, the fabric stores ISO
    strings, some rows carry nothing at all. Sorting a mixed list without
    normalising puts 1758000000 above 2026-09-09 and the newest item vanishes
    to the bottom.
    """
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)\
                .isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            return ""
    s = str(value).strip()
    if s.isdigit() and len(s) >= 9:            # unix seconds as a string
        return parse_ts(int(s))
    return s


def derive_severity(text: str, explicit: str = "") -> str:
    """Severity from content, with an explicit level only as a starting point."""
    lvl = (explicit or "").strip().lower()
    if lvl in ("high", "critical", "error"):
        return "critical"
    if lvl in ("medium", "warning", "warn"):
        return "warning"
    blob = text or ""
    if _CRITICAL_RE.search(blob):
        return "critical"
    if _WARNING_RE.search(blob):
        return "warning"
    return "info"


def entry(ts: Any, source: str, kind: str, title: str, detail: str = "",
          severity: str = "", ref: str = "") -> Dict[str, Any]:
    title = _clip(title, 200)
    return {"ts": parse_ts(ts), "source": source, "kind": kind,
            "severity": severity or derive_severity(f"{title} {detail}"),
            "title": title, "detail": _clip(detail, 600), "ref": ref}


def _clip(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


# ═════════════════════════════════════════════════════════════════════════════
#  Per-source normalisers
# ═════════════════════════════════════════════════════════════════════════════

def from_telegram(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Telegram traffic — what Vera told you, and what you replied."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        text = m.get("text") or m.get("message") or ""
        if not text:
            continue
        direction = m.get("direction") or ("out" if m.get("from_bot") else "in")
        out.append(entry(
            m.get("ts") or m.get("date") or m.get("created"),
            "telegram", "sent" if direction == "out" else "received",
            text.splitlines()[0] if text else "",
            text, ref=str(m.get("chat_id") or m.get("message_id") or "")))
    return out


def from_actions(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The action list — only what is still open; closed items are noise here."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("status") == "done":
            continue
        text = r.get("text") or ""
        if not text:
            continue
        out.append(entry(r.get("created"), "actions", "open",
                         text, "", severity=derive_severity(text,
                                                            r.get("priority", "")),
                         ref=str(r.get("id") or "")))
    return out


def from_n8n_health(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The nightly workflow review — surface only what is actually wrong."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        name = r.get("workflow") or ""
        if not name:
            continue
        errs = int(r.get("err") or 0)
        if errs <= 0:
            continue                          # a healthy workflow is not news
        ok = int(r.get("ok") or 0)
        out.append(entry(
            r.get("checked_at"), "n8n", "workflow",
            f"{name}: {errs} of {errs + ok} recent runs failed",
            f"error rate {r.get('error_rate')}", ref=str(r.get("id") or "")))
    return out


def from_reports(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Archived briefs and digests, so the pane shows what was delivered."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        text = r.get("text") or r.get("summary") or ""
        if not text:
            continue
        first = text.strip().splitlines()[0] if text.strip() else "report"
        out.append(entry(r.get("created") or r.get("created_at"),
                         "reports", r.get("category") or "report",
                         first, text, severity="info",
                         ref=str(r.get("id") or "")))
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Merge
# ═════════════════════════════════════════════════════════════════════════════

def merge_feed(streams: Iterable[Iterable[Dict[str, Any]]], *, limit: int = 100,
               sources: Optional[Iterable[str]] = None,
               min_severity: str = "") -> List[Dict[str, Any]]:
    """Merge, filter and order the streams into one timeline, newest first.

    Entries with no usable timestamp sort last rather than first — an unknown
    time is not the same as "just now", and letting them lead would bury the
    genuinely recent items.
    """
    want = {s.strip().lower() for s in (sources or []) if s and s.strip()}
    floor = _SEV_RANK.get((min_severity or "").lower(), None)

    merged: List[Dict[str, Any]] = []
    seen = set()
    for stream in streams or []:
        for e in stream or []:
            if not isinstance(e, dict) or not e.get("title"):
                continue
            if want and e.get("source", "") not in want:
                continue
            if floor is not None and _SEV_RANK.get(e.get("severity", "info"),
                                                   2) > floor:
                continue
            key = (e.get("source"), e.get("title"), e.get("ts"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(e)

    merged.sort(key=lambda e: (e.get("ts") or "", ), reverse=True)
    # Undated entries would otherwise sort to the top of a reversed list.
    dated = [e for e in merged if e.get("ts")]
    undated = [e for e in merged if not e.get("ts")]
    return (dated + undated)[:max(1, int(limit or 100))]


def summarise(feed: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts for the pane header, so the state is legible without reading."""
    feed = list(feed or [])
    by_sev = {s: 0 for s in SEVERITIES}
    by_source: Dict[str, int] = {}
    for e in feed:
        by_sev[e.get("severity", "info")] = by_sev.get(e.get("severity", "info"), 0) + 1
        src = e.get("source", "?")
        by_source[src] = by_source.get(src, 0) + 1
    return {"total": len(feed), "by_severity": by_sev,
            "by_source": dict(sorted(by_source.items())),
            "newest": feed[0]["ts"] if feed else ""}
