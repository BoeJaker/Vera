"""Four-minds' tests, as the Loop Lab Tests page shows them (owner, 18 Oct 2026: "make sure all your tests are well
integrated into the loop lab for monitoring ... there should be a part of the ui dedicated to monitoring your tests on
its own page").

A test is a family of Loop Lab schedule entries (matched by the capability they call, or a census arm by its title)
with a report capability that says what it found. status() joins each test's entries with the run history: when it
fires next, how its last run ended, whether a run a restart killed is being recovered, whether a window passed with no
run (missed), and its last runs.

Pure: no I/O.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

#: id -> name, roadmap package, the schedule match (cap names, or a title regex for census arms), the report cap
TESTS: List[Dict[str, Any]] = [
    {"id": "matrix", "name": "Candidate matrix (judges)", "package": "FM-2", "caps": ["decide.shadow.night"],
     "report": "decide.matrix.report"},
    {"id": "claims", "name": "Claims vs evidence harvest", "package": "FM-5.2", "caps": ["output.claims.harvest"],
     "report": ""},
    {"id": "shortlist", "name": "Tool choice: index + replay matrix", "package": "FM-3", "caps": ["caps.shortlist.nightly"],
     "report": "caps.shortlist.replay"},
    {"id": "precedent", "name": "Precedent index", "package": "FM-4.2", "caps": ["plan.precedent.index"], "report": ""},
    {"id": "cascade", "name": "Cascade plan (who may answer first)", "package": "FM-2.5", "caps": ["decide.cascade.plan"],
     "report": ""},
    {"id": "calibration", "name": "Calibration snapshot", "package": "FM-11", "caps": ["decide.calibration.weekly"],
     "report": "decide.calibration.history"},
    {"id": "memory", "name": "Memory census (speed + quality)", "package": "FM-7.9", "caps": ["memory.perf.probe"],
     "report": "memory.perf.versions"},
    {"id": "chat_corpus", "name": "Chat corpus run", "package": "§5.4", "caps": ["chat.corpus.run"],
     "report": "chat.corpus.last"},
    {"id": "arms", "name": "Census arms (students, precedent, tier, writer)", "package": "FM-2.5 / FM-4",
     "title_re": r"\barm\b", "report": ""},
]
HEALTH_ORDER = ("failed", "interrupted", "missed", "recovering", "running", "never", "ok", "disabled", "scheduled")
FAILED_WORDS = ("error", "failed", "refused", "stopped")


def _p(s: Any) -> Optional[datetime]:
    t = str(s or "").strip()
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def matches(test: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    if test.get("caps"):
        return entry.get("kind") == "cap" and str((entry.get("target") or {}).get("name") or "") in test["caps"]
    if test.get("title_re"):
        return entry.get("kind") == "census" and bool(re.search(test["title_re"], str(entry.get("title") or ""), re.I))
    return False


def entry_health(entry: Mapping[str, Any], runs: List[Mapping[str, Any]], now: datetime,
                 next_start: Optional[str]) -> str:
    """One entry's state now."""
    if not entry.get("enabled", True):
        return "disabled"
    if entry.get("recover_due"):
        return "recovering"
    open_runs = [r for r in runs if not r.get("finished_at")]
    if open_runs:
        return "running"
    last = runs[0] if runs else None
    if last is None:
        if entry.get("once_at") and _p(entry["once_at"]) and _p(entry["once_at"]) < now - timedelta(minutes=30):
            return "missed"
        return "never" if not entry.get("once_at") else "scheduled"
    word = str(last.get("result") or "").lower()
    if word == "interrupted":
        return "interrupted"
    if any(w in word for w in FAILED_WORDS):
        return "failed"
    # a recurring entry whose last run is older than one full period past its next window: a window was missed
    if not entry.get("once_at") and next_start:
        nxt, lst = _p(next_start), _p(last.get("started_at"))
        days = entry.get("days") or list(range(7))
        period = timedelta(days=7 if len(days) == 1 else (2 if len(days) <= 4 else 1))
        if nxt and lst and nxt - lst > period + timedelta(hours=2):
            return "missed"
    return "ok"


def worst(states: Iterable[str]) -> str:
    st = list(states)
    for s in HEALTH_ORDER:
        if s in st:
            return s
    return "scheduled"


def status(entries: Iterable[Mapping[str, Any]], runs: Iterable[Mapping[str, Any]], nexts: Mapping[str, Any],
           now: datetime, headlines: Optional[Mapping[str, Any]] = None, keep_runs: int = 8) -> Dict[str, Any]:
    """{tests: [{id, name, package, health, entries: [...], runs: [...], headline}], counts}."""
    entries = [e for e in entries if isinstance(e, Mapping)]
    runs = sorted([r for r in runs if isinstance(r, Mapping)], key=lambda r: str(r.get("started_at") or ""), reverse=True)
    out = []
    for t in TESTS:
        mine = [e for e in entries if matches(t, e)]
        ids = {e.get("id") for e in mine}
        t_runs = [r for r in runs if r.get("schedule_id") in ids]
        rows = []
        for e in mine:
            e_runs = [r for r in t_runs if r.get("schedule_id") == e.get("id")]
            nx = (nexts.get(e.get("id")) or {}).get("start") if isinstance(nexts.get(e.get("id")), Mapping) else None
            rows.append({"id": e.get("id"), "title": e.get("title"), "enabled": e.get("enabled", True),
                         "when": e.get("once_at") or f"{e.get('start')}-{e.get('end')} days {e.get('days')}",
                         "next": nx, "last_started_at": e.get("last_started_at") or "",
                         "last_result": e.get("last_result") or "", "recover": bool(e.get("recover")),
                         "protect": bool(e.get("protect")), "recover_due": e.get("recover_due") or "",
                         "health": entry_health(e, e_runs, now, nx)})
        rows.sort(key=lambda r: (HEALTH_ORDER.index(r["health"]) if r["health"] in HEALTH_ORDER else 99,
                                 str(r.get("next") or "")))
        live = [r for r in rows if r["enabled"]]
        health = worst(r["health"] for r in live) if live else ("disabled" if rows else "never")
        out.append({"id": t["id"], "name": t["name"], "package": t["package"], "report": t.get("report", ""),
                    "health": health, "entries": rows,
                    "runs": [{"title": r.get("title"), "started_at": r.get("started_at"),
                              "finished_at": r.get("finished_at"), "result": r.get("result"),
                              "detail": str(r.get("detail") or "")[:160]} for r in t_runs[:keep_runs]],
                    "headline": (headlines or {}).get(t["id"])})
    counts: Dict[str, int] = {}
    for t in out:
        counts[t["health"]] = counts.get(t["health"], 0) + 1
    return {"tests": out, "counts": counts, "at": now.isoformat(timespec="seconds")}
