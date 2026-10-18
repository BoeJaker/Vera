"""Memory performance log (four-minds FM-7.9): how fast memory is, per code version and switch set.

Agent loops are measured as census rows, each tagged with the code that ran and the switch
modes, so census.compare can say what a change did. Memory had only process windows (the
last 500 calls, emptied by every restart) and hourly snapshots of them that overlap and say
nothing about which code produced them. This gives memory the same shape:

- a live row every interval: only the calls since the last row (Window.since), per source
  (retrieval = memory.seek/search/context.recall envelopes; chat = chat turns, whose TTFT
  carries the memory tiers), tagged with the running sha and the memory-relevant switches;
- a probe row: the frozen known-item set through memory.seek - the same queries every time,
  so a change shows up without the traffic mix moving underneath it (the census of memory);
- compare(base rows, head rows): per path and stage, the pooled p50/p95 and the change.

Pooling rows: a row holds percentiles, not samples, so pooled values are call-weighted means
of the rows' p50s and p95s - an approximation, close when the rows are similar. Pure: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

# Switches whose modes change how memory is served (their ids, by prefix); every row keeps
# these modes in full, and the digest of the whole switch set.
MEMORY_SWITCH_PREFIXES = ("memory.", "fabric.", "discovery.", "chat.memory")
# A change is reported as slower/faster only past both of these, and with enough calls.
MIN_CALLS = 20
MIN_PCT = 15.0
MIN_MS = 20.0
KINDS = ("live", "probe")


def memory_modes(modes: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    return {k: str(v) for k, v in sorted((modes or {}).items())
            if any(k.startswith(p) for p in MEMORY_SWITCH_PREFIXES)}


def version(prov: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    p = prov or {}
    sha = str(p.get("git_sha") or "")
    return {"sha": sha, "sha_short": str(p.get("git_sha_short") or sha[:8]),
            "branch": str(p.get("branch") or ""), "dirty": bool(p.get("dirty"))}


def row(kind: str, source: str, paths: Mapping[str, Any], prov: Optional[Mapping[str, Any]],
        modes: Optional[Mapping[str, Any]], digest: str = "", interval_s: float = 0.0,
        extra: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """One row, or None when there is nothing to keep (no calls)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    paths = {p: s for p, s in (paths or {}).items() if (s or {}).get("calls")}
    if not paths:
        return None
    out = dict(version(prov), kind=kind, source=source, minds_digest=str(digest or ""),
               memory_modes=memory_modes(modes), interval_s=round(float(interval_s or 0.0), 1),
               calls=sum(int(s.get("calls") or 0) for s in paths.values()), paths=dict(paths))
    if extra:
        out["extra"] = dict(extra)
    return out


def _wmean(pairs: List[tuple]) -> Optional[float]:
    n = sum(w for _, w in pairs)
    return round(sum(v * w for v, w in pairs) / n, 1) if n else None


def pool(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """{source|path: {calls, total: {p50, p95}, stages: {name: {n, p50, p95, outcomes}}}} over rows."""
    acc: Dict[str, Any] = {}
    for r in rows:
        for path, s in (r.get("paths") or {}).items():
            key = f"{r.get('source', '')}|{path}"
            a = acc.setdefault(key, {"calls": 0, "p50": [], "p95": [], "stages": {}})
            c = int(s.get("calls") or 0)
            a["calls"] += c
            tot = s.get("total") or {}
            a["p50"].append((float(tot.get("p50") or 0.0), c))
            a["p95"].append((float(tot.get("p95") or 0.0), c))
            for name, st in (s.get("stages") or {}).items():
                b = a["stages"].setdefault(name, {"n": 0, "p50": [], "p95": [], "outcomes": {}})
                n = int(st.get("n") or 0)
                b["n"] += n
                b["p50"].append((float(st.get("p50") or 0.0), n))
                b["p95"].append((float(st.get("p95") or 0.0), n))
                for o, k in (st.get("outcomes") or {}).items():
                    b["outcomes"][o] = b["outcomes"].get(o, 0) + int(k)
    return {key: {"calls": a["calls"], "total": {"p50": _wmean(a["p50"]), "p95": _wmean(a["p95"])},
                  "stages": {n: {"n": b["n"], "p50": _wmean(b["p50"]), "p95": _wmean(b["p95"]),
                                 "outcomes": b["outcomes"]} for n, b in a["stages"].items()}}
            for key, a in acc.items()}


def _delta(base: Optional[float], head: Optional[float], nb: int, nh: int) -> Dict[str, Any]:
    if base is None or head is None:
        return {"base": base, "head": head}
    d = round(head - base, 1)
    pct = round(100.0 * d / base, 1) if base else None
    verdict = "same"
    if min(nb, nh) < MIN_CALLS:
        verdict = "too_few"
    elif abs(d) >= MIN_MS and pct is not None and abs(pct) >= MIN_PCT:
        verdict = "slower" if d > 0 else "faster"
    return {"base": base, "head": head, "ms": d, "pct": pct, "verdict": verdict}


def compare(base_rows: Iterable[Mapping[str, Any]], head_rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Per source|path: the total's p50/p95 change and each shared stage's, plus a one-line headline."""
    b, h = pool(base_rows), pool(head_rows)
    out: Dict[str, Any] = {}
    slower, faster = [], []
    for key in sorted(set(b) | set(h)):
        if key not in b or key not in h:
            out[key] = {"only_in": "base" if key in b else "head",
                        "calls": (b.get(key) or h.get(key))["calls"]}
            continue
        x, y = b[key], h[key]
        ent = {"calls": {"base": x["calls"], "head": y["calls"]},
               "p50": _delta(x["total"]["p50"], y["total"]["p50"], x["calls"], y["calls"]),
               "p95": _delta(x["total"]["p95"], y["total"]["p95"], x["calls"], y["calls"]),
               "stages": {}}
        for name in sorted(set(x["stages"]) & set(y["stages"])):
            sx, sy = x["stages"][name], y["stages"][name]
            ent["stages"][name] = {"p50": _delta(sx["p50"], sy["p50"], sx["n"], sy["n"]),
                                   "p95": _delta(sx["p95"], sy["p95"], sx["n"], sy["n"]),
                                   "outcomes": {"base": sx["outcomes"], "head": sy["outcomes"]}}
        v = ent["p50"].get("verdict")
        (slower if v == "slower" else faster if v == "faster" else []).append(f"{key} {ent['p50'].get('pct')}%")
        out[key] = ent
    head = ("slower: " + ", ".join(slower) if slower else "") + \
           ("; " if slower and faster else "") + ("faster: " + ", ".join(faster) if faster else "")
    return {"paths": out, "headline": head or "no p50 change past the noise floor"}


def versions(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Each (sha, minds_digest, kind) seen, with its first/last ts and calls - what compare can pick from."""
    g: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        k = (r.get("sha_short") or "", r.get("minds_digest") or "", r.get("kind") or "")
        e = g.setdefault(k, {"sha_short": k[0], "minds_digest": k[1], "kind": k[2], "rows": 0, "calls": 0,
                             "first": r.get("ts"), "last": r.get("ts"), "branch": r.get("branch") or ""})
        e["rows"] += 1
        e["calls"] += int(r.get("calls") or 0)
        ts = r.get("ts")
        if ts and (not e["first"] or ts < e["first"]):
            e["first"] = ts
        if ts and (not e["last"] or ts > e["last"]):
            e["last"] = ts
    return sorted(g.values(), key=lambda e: e["last"] or "", reverse=True)
