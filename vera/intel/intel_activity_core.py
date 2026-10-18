"""Intel's own events for Vera's unified activity timeline (pure).

The Activity panel composes what Vera did on its own - dream cycles, loop runs,
programs, the narrator. Intel's work belongs there too: the night batch, each
assessment, the alerts it raised, the briefs it built and where they went, the
actions people ran from them. Intel builds these events from its store
(`intel.activity`); the activity module only composes them, so neither imports
the other.

Every event has the activity timeline's shape: kind, ts, title, summary, status,
ref, source, ui (where it opens), extra.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence


def action_ref(token: Any) -> str:
    """A non-secret, stable id for an action, derived from (never revealing) its token."""
    t = str(token or "")
    return ("act-" + hashlib.sha256(t.encode()).hexdigest()[:12]) if t else ""

PANEL = "/intel/panel"


def _ev(kind: str, ts: str, title: str, *, summary: str = "", status: str = "", ref: str = "",
        url: str = PANEL, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"kind": kind, "ts": ts or "", "title": title[:200], "summary": (summary or "")[:1200], "status": status,
            "session_id": "", "cap": "", "ref": ref, "source": "intel",
            "ui": {"label": "Intelligence", "url": url, "element": ""}, "extra": extra or {}}


def events(*, snapshots: Sequence[Dict[str, Any]] = (), reports: Sequence[Dict[str, Any]] = (),
           deliveries: Sequence[Dict[str, Any]] = (), alerts: Sequence[Dict[str, Any]] = (),
           actions: Sequence[Dict[str, Any]] = (), night: Optional[Dict[str, Any]] = None,
           names: Optional[Dict[str, str]] = None, topic_id: str = "", limit: int = 60) -> List[Dict[str, Any]]:
    """Newest first. With topic_id, only that topic's events (briefs: those that cover it)."""
    names = names or {}
    nm = lambda tid: names.get(tid, tid or "")            # noqa: E731
    out: List[Dict[str, Any]] = []
    for s in snapshots:
        if topic_id and s.get("topic_id") != topic_id:
            continue
        changed = s.get("changed") or []
        out.append(_ev("intel.assessment", str(s.get("ts") or ""), f"Assessed {nm(s.get('topic_id'))}: {s.get('headline') or ''}",
                       summary=s.get("state") or "", status=str(s.get("confidence") or ""), ref=str(s.get("topic_id") or ""),
                       extra={"topic_id": s.get("topic_id"), "changed": len(changed)}))
    for a in alerts:
        if topic_id and a.get("topic") != topic_id:
            continue
        out.append(_ev("intel.alert", str(a.get("ts") or ""), f"Alert on {nm(a.get('topic'))}: {a.get('headline') or ''}",
                       summary=str(a.get("why") or ""), status="alert", ref=str(a.get("topic") or ""),
                       extra={"topic_id": a.get("topic")}))
    sent: Dict[str, List[Dict[str, Any]]] = {}
    for d in deliveries:
        sent.setdefault(str(d.get("report_id") or ""), []).append(d)
    for r in reports:
        if topic_id and topic_id not in (r.get("topic_ids") or []):
            continue
        rid = str(r.get("id") or "")
        chans = sorted({str(d.get("channel")) for d in sent.get(rid, []) if d.get("ok")})
        failed = sorted({str(d.get("channel")) for d in sent.get(rid, []) if not d.get("ok")})
        counts = r.get("counts") or {}
        out.append(_ev("intel.report", str(r.get("ts") or ""), f"Brief: {r.get('title') or rid}",
                       summary=(f"{counts.get('topics', 0)} topics, {counts.get('signals', 0)} signals"
                                + (f" · sent to {', '.join(chans)}" if chans else " · not sent")
                                + (f" · failed: {', '.join(failed)}" if failed else "")),
                       status="sent" if chans else ("failed" if failed else "built"), ref=rid,
                       url=f"/intel/report/view/{rid}", extra={"channels": chans, "failed": failed}))
    for a in actions:
        if a.get("status") in (None, "", "offered"):
            continue
        if topic_id and a.get("topic_id") != topic_id:
            continue
        out.append(_ev("intel.action", str(a.get("finished") or a.get("created") or ""),
                       f"Action: {a.get('label') or a.get('kind') or 'run'}",
                       summary=str(a.get("query") or a.get("target") or ""), status=str(a.get("status") or ""),
                       # never the token: it is the one-time key that RUNS the action, and timeline events are
                       # shown and stored widely (review 2026-10-10) - a stable non-secret id instead
                       ref=action_ref(a.get("token")), extra={"topic_id": a.get("topic_id")}))
    if night and night.get("ts") and not topic_id:
        st = night.get("stats") or {}
        out.append(_ev("intel.night", str(night["ts"]), f"Night batch: {st.get('topics', night.get('topics', 0))} topics",
                       summary=f"{st.get('searched', 0)} searches, {st.get('new', 0)} new signals, "
                               f"{st.get('assessed', 0)} summarised", status="done", extra={"stats": st}))
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out[:max(1, int(limit))]
