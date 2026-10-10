"""Scoring for the info-seeking eval's retrieval check (info.seek packs). Pure: no network.

Per question: did it answer, how fast, how many sources were read and dated, did a source carry an expected term
(on target), and for 'latest' whether a source is recent enough (fresh). The summary groups by kind with p50/p95
latency, so a change that buys depth with time - or time with depth - shows in both columns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Sequence


def _day(s: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s or "")[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def score_pack(q: Dict[str, Any], pack: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    items = pack.get("items") or []
    expect = [e.lower() for e in q.get("expect_any") or []]
    read = [it for it in items if it.get("read")]
    dated = [it for it in items if _day(it.get("published"))]

    def hit(its):
        return bool(expect) and any(any(e in (str(it.get("title") or "") + " " + str(it.get("text") or "")).lower()
                                        for e in expect) for it in its)
    out = {
        "id": q["id"], "kind": q["kind"], "q": q["q"],
        "ok": not pack.get("error") and not pack.get("_error"),
        "need": pack.get("need"), "elapsed_ms": int(pack.get("elapsed_ms") or 0),
        "items": len(items), "read": len(read), "dated": len(dated),
        "on_target": hit(items), "on_target_read": hit(read),
        "gaps": list(pack.get("gaps") or []), "tracked": bool(pack.get("tracked")),
    }
    if q["kind"] == "latest":
        fd = int(q.get("fresh_days") or 14)
        ages = [(now - d).days for d in (_day(it.get("published")) for it in items) if d and d <= now]
        out["newest_age_days"] = min(ages) if ages else None
        out["fresh"] = bool(ages) and min(ages) <= fd
        out["need_ok"] = pack.get("need") == "latest"
    return out


def _pct(xs: Sequence[bool]) -> Optional[float]:
    return round(100.0 * sum(1 for x in xs if x) / len(xs), 1) if xs else None


def _p(xs: Sequence[float], q: float) -> Optional[int]:
    if not xs:
        return None
    s = sorted(xs)
    return int(s[min(len(s) - 1, int(round(q * (len(s) - 1))))])


def summarise(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    by: Dict[str, Dict[str, Any]] = {}
    for kind in sorted({r["kind"] for r in rows}):
        rs = [r for r in rows if r["kind"] == kind]
        ms = [r["elapsed_ms"] for r in rs if r["ok"]]
        s = {"n": len(rs), "ok_pct": _pct([r["ok"] for r in rs]),
             "on_target_pct": _pct([r["on_target"] for r in rs]),
             "on_target_read_pct": _pct([r["on_target_read"] for r in rs]),
             "mean_read": round(sum(r["read"] for r in rs) / len(rs), 1),
             "mean_dated_share": round(sum(r["dated"] / r["items"] for r in rs if r["items"]) /
                                       max(1, sum(1 for r in rs if r["items"])), 2),
             "p50_ms": _p(ms, 0.5), "p95_ms": _p(ms, 0.95)}
        if kind == "latest":
            s["fresh_pct"] = _pct([r.get("fresh") for r in rs])
            s["need_ok_pct"] = _pct([r.get("need_ok") for r in rs])
            ages = [r["newest_age_days"] for r in rs if r.get("newest_age_days") is not None]
            s["median_newest_age_days"] = median(ages) if ages else None
        by[kind] = s
    return by


def markdown(summary: Dict[str, Any], rows: Sequence[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    lines = [f"# info.seek retrieval check - {meta.get('started', '')}", "",
             f"Target: {meta.get('target', '')} · questions: {len(rows)} · gap between questions: {meta.get('gap_s')} s",
             "", "| kind | n | ok % | on target % | on target (read) % | fresh % | mean read | dated share | p50 ms | p95 ms |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in summary.items():
        lines.append(f"| {k} | {s['n']} | {s['ok_pct']} | {s['on_target_pct']} | {s['on_target_read_pct']} | "
                     f"{s.get('fresh_pct', '-')} | {s['mean_read']} | {s['mean_dated_share']} | {s['p50_ms']} | {s['p95_ms']} |")
    lines += ["", "## Misses", ""]
    for r in rows:
        bad = [w for w, b in (("error", not r["ok"]), ("off target", not r["on_target"]),
                              ("stale", r["kind"] == "latest" and not r.get("fresh")),
                              ("wrong need", r["kind"] == "latest" and not r.get("need_ok"))) if b]
        if bad:
            lines.append(f"- {r['id']} {r['q']!r}: {', '.join(bad)}; read {r['read']}, dated {r['dated']}/{r['items']}"
                         + (f"; gaps: {'; '.join(r['gaps'])}" if r["gaps"] else ""))
    return "\n".join(lines) + "\n"
