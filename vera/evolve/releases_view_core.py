"""Releases in Loop Lab - pure: where each pipeline stands (adopted, gating, on bleeding-edge, released, failed),
what each release carried, and the calendar's releases & promotions layer.

Owner, 18 Oct 2026: "add better vis over the gating and releases via the loop lab ui, and also the schedule -
releases and promotions could be on their own layer and have a section of the ui". The data was there - a
pipeline's steps (adopt, gate, gate-ui, test-pool, critical-tests, perf, merge) and the release history - but
only as rows in the Ship table. No I/O here: the capability gathers the records and the git facts.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

COLUMNS = ("adopted", "gating", "ready", "edge", "released", "failed")
GATE_STAGES = ("gate", "gate-ui", "test-pool", "critical-tests", "perf", "unit", "tests")
_MERGE_SHA = re.compile(r"@\s*([0-9a-f]{7,40})")
_TESTS = re.compile(r"(\d+) passed, (\d+) failed(?:, (\d+) errors)?")


def _ts(s: Any) -> Optional[datetime]:
    t = str(s or "").strip()
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def merge_step(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for s in reversed(p.get("steps") or []):
        if s.get("stage") == "merge" and s.get("ok"):
            return s
    return None


def merge_sha(p: Dict[str, Any]) -> str:
    m = merge_step(p)
    hit = _MERGE_SHA.search(str((m or {}).get("detail") or ""))
    return hit.group(1) if hit else ""


def tests_of(p: Dict[str, Any]) -> Dict[str, Any]:
    """The critical-tests result: {passed, failed, errors, detail}, or {}."""
    for s in reversed(p.get("steps") or []):
        if s.get("stage") == "critical-tests":
            m = _TESTS.search(str(s.get("detail") or ""))
            out = {"ok": bool(s.get("ok")), "detail": str(s.get("detail") or "")[:200]}
            if m:
                out.update(passed=int(m.group(1)), failed=int(m.group(2)), errors=int(m.group(3) or 0))
            return out
    return {}


def column(p: Dict[str, Any], released: bool = False) -> str:
    """Where a pipeline stands. `released`: its merge commit is on main (the caller asked git)."""
    if merge_step(p) or p.get("decision") == "promoted":
        return "released" if released else "edge"
    steps = p.get("steps") or []
    if any(s.get("ok") is False for s in steps) or p.get("gate_passed") is False and p.get("status") == "tested":
        return "failed"
    if (tests_of(p) or {}).get("ok"):
        return "ready"                        # the gate passed; not promoted yet
    if any(s.get("stage") in GATE_STAGES for s in steps) or p.get("live"):
        return "gating"
    return "adopted"


def card(p: Dict[str, Any], released: bool = False) -> Dict[str, Any]:
    steps = p.get("steps") or []
    m = merge_step(p)
    first = _ts((steps[0] if steps else {}).get("ts")) or _ts(p.get("created_at"))
    last = _ts((steps[-1] if steps else {}).get("ts"))
    return {"id": p.get("id"), "branch": p.get("branch") or "", "to": p.get("to") or "bleeding-edge",
            "column": column(p, released), "decision": p.get("decision") or "", "status": p.get("status") or "",
            "commits": len(p.get("commits") or []) if isinstance(p.get("commits"), list) else p.get("commits"),
            "changed_files": len(p.get("changed_files") or []) if isinstance(p.get("changed_files"), list)
            else p.get("changed_files"),
            "stages": [{"stage": s.get("stage"), "ok": s.get("ok"), "ts": s.get("ts"),
                        "detail": str(s.get("detail") or "")[:160]} for s in steps],
            "tests": tests_of(p), "merge_sha": merge_sha(p), "promoted_at": (m or {}).get("ts") or "",
            "started_at": _iso(first) if first else "", "last_at": _iso(last) if last else "",
            "minutes": round((last - first).total_seconds() / 60, 1) if first and last else None}


def board(pipelines: Iterable[Dict[str, Any]], released_shas: Iterable[str] = ()) -> Dict[str, List[Dict[str, Any]]]:
    rel = set(released_shas or ())
    cols: Dict[str, List[Dict[str, Any]]] = {c: [] for c in COLUMNS}
    for p in pipelines or []:
        sha = merge_sha(p)
        c = card(p, released=bool(sha and any(sha.startswith(r) or r.startswith(sha) for r in rel)))
        cols[c["column"]].append(c)
    for c in cols.values():
        c.sort(key=lambda x: x.get("last_at") or x.get("started_at") or "", reverse=True)
    return cols


def release_row(h: Dict[str, Any], carried: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    st, fin = _ts(h.get("requested_at")), _ts(h.get("finished_at"))
    merge = h.get("merge") or {}
    return {"id": h.get("id") or "", "status": h.get("status") or "", "mode": h.get("mode") or "",
            "reason": str(h.get("reason") or "")[:200], "requested_at": h.get("requested_at") or "",
            "finished_at": h.get("finished_at") or "", "waits": h.get("waits"),
            "waited_for": str(h.get("last_why") or "")[:120], "commit": str(merge.get("commit") or "")[:12],
            "nodes": "synced" if h.get("sync_nodes") else "", "census_resumed": h.get("census_resumed"),
            "minutes": round((fin - st).total_seconds() / 60, 1) if st and fin else None,
            "carried": list(carried or [])}


def carried_from_log(lines: Iterable[str]) -> List[Dict[str, str]]:
    """`git log --first-parent --format=%h %s` lines between two releases -> the merges they carried."""
    out = []
    for ln in lines or []:
        sha, _, subj = str(ln).strip().partition(" ")
        if not sha:
            continue
        m = re.match(r"Loop Lab: merge (\S+) \(pipeline (\w+)\)", subj)
        out.append({"sha": sha, "subject": subj[:160], "branch": m.group(1) if m else "",
                    "pipeline": m.group(2) if m else ""})
    return out


def events(cards: Iterable[Dict[str, Any]], releases: Iterable[Dict[str, Any]], start: datetime,
           end: datetime) -> List[Dict[str, Any]]:
    """The calendar's releases & promotions layer: a promotion at its merge time, a release from request to finish."""
    out: List[Dict[str, Any]] = []
    for c in cards or []:
        t = _ts(c.get("promoted_at"))
        if t and start <= t <= end:
            out.append({"id": "promo:%s" % c.get("id"), "title": "◆ %s → %s" % (c.get("branch"), c.get("to")),
                        "start": _iso(t), "end": _iso(t + timedelta(minutes=15)), "source": "releases",
                        "kind": "promotion", "read_only": True, "color": "#7aa2f7", "pipeline_id": c.get("id"),
                        "detail": "pipeline %s · %s" % (c.get("id"), (c.get("tests") or {}).get("detail") or "")})
    for r in releases or []:
        st = _ts(r.get("requested_at"))
        if not st:
            continue
        fin = _ts(r.get("finished_at")) or (st + timedelta(minutes=10))
        if fin < start or st > end:
            continue
        if (fin - st) < timedelta(minutes=10):
            fin = st + timedelta(minutes=10)
        out.append({"id": "release:%s" % r.get("id"), "start": _iso(st), "end": _iso(fin), "source": "releases",
                    "title": "▲ release → prod · %s%s" % (r.get("status") or "", (" · " + r["reason"][:60]) if r.get("reason") else ""),
                    "kind": "release", "read_only": True, "color": "#4ade80", "release_id": r.get("id"),
                    "detail": "waited for: %s · main @ %s · %d change(s)" % (r.get("waited_for") or "-", r.get("commit") or "?",
                                                                            len(r.get("carried") or []))})
    out.sort(key=lambda e: e["start"])
    return out
