"""retrieval.known_items.build / .run: the known-item eval set for memory.seek (four-minds FM-7.0c; see
known_item_core for what it measures and why).

build: one pass over the fabric's SQLite records (every Nth rowid), stratified by family, frozen as JSON under
state_dir("eval", "known_items") - the same set every time it is run, so arms are comparable.
run: each item's query through memory.seek (fabric only, the result cache forced off, any switch modes given for this
run applied through the switchboard's run layer), scored by where the record lands; kept beside the set.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.fabric import known_item_core as KI
    from Vera.vera.minds import switchboard_core as SB
except Exception:                        # worktree / app-free import
    from vera.fabric import known_item_core as KI          # type: ignore
    from vera.minds import switchboard_core as SB          # type: ignore

log = logging.getLogger("vera.fabric.known_item")


def _df():
    """The LIVE data_fabric module (the one the loader connected), never a fresh import of a second copy."""
    import sys
    m = sys.modules.get("data_fabric") or sys.modules.get("Vera.vera.fabric.data_fabric")
    if m is None:
        raise RuntimeError("data_fabric is not loaded yet")
    return m
SAMPLE_EVERY = 500


def _dir() -> Path:
    from Vera.vera.state_paths import state_dir
    return Path(state_dir("eval", "known_items"))


async def _candidates_pg(percent: float) -> List[Dict[str, Any]]:
    """A random block sample of the fabric's Postgres records - the table memory.seek searches and hydrates from, so
    every item is one seek can return (11 Oct 2026: a SQLite sample was mostly records seek cannot see)."""
    df = _df()
    pool = getattr(getattr(df, "FABRIC_PG", None), "_pool", None)
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, dataset_id, left(text, 600) AS text FROM fabric_records "
            f"TABLESAMPLE SYSTEM ({float(percent)}) WHERE text IS NOT NULL")
    return [{"id": r["id"], "dataset_id": r["dataset_id"], "text": r["text"]} for r in rows]


def _candidates(every: int) -> List[Dict[str, Any]]:
    df = _df()
    conn = df._sqlite_conn()
    rows = conn.execute("SELECT id, dataset_id, substr(text, 1, 600) FROM fabric_records "
                        "WHERE rowid % ? = 0 AND text IS NOT NULL", (int(every),)).fetchall()
    return [{"id": r[0], "dataset_id": r[1], "text": r[2]} for r in rows]


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "retrieval.known_items.build", http_method="POST", http_path="/retrieval/known_items/build",
    contract=_MC.contract("retrieval.eval.known_items.build", effects=["read", "filesystem"], idempotency="non_idempotent", filesystem="state_dir"),
    http_tags=["fabric", "memory", "eval"], memory="off",
    description="Build and freeze a known-item eval set for memory.seek (four-minds FM-7.0c): n real fabric records, "
                "a block sample of the fabric's Postgres records (what seek searches), stratified by collector family "
                "(none above 15%, self-telemetry left out), each with a query made of the distinctive words it opens "
                "with. Inputs: n (int=200), name (str='v1'), source ('pg' default | 'sqlite' - every Nth SQLite row, "
                "which includes records seek cannot see), percent (float=0.4, pg sample), every (int=500, sqlite). "
                "Output: {ok, name, source, items, sampled, families, path}.",
)
async def cap_known_items_build(n: int = 200, name: str = KI.VERSION, every: int = SAMPLE_EVERY,
                                source: str = "pg", percent: float = 0.4, trace_id=None) -> Dict[str, Any]:
    if source == "sqlite":
        cands = await asyncio.to_thread(_candidates, max(1, int(every)))
    else:
        cands = await _candidates_pg(max(0.01, min(10.0, float(percent or 0.4))))
    items = KI.pick(cands, max(1, int(n)), seed=str(name))
    doc = {"name": str(name), "version": KI.VERSION, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "source": source, "sampled": len(cands), "items": items}
    p = _dir() / f"{name}.json"
    p.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    fams: Dict[str, int] = {}
    for it in items:
        fams[it["family"]] = fams.get(it["family"], 0) + 1
    return {"ok": True, "name": str(name), "source": source, "items": len(items), "sampled": len(cands),
            "families": fams, "path": str(p)}


@capability(
    "retrieval.known_items.run", http_method="POST", http_path="/retrieval/known_items/run",
    contract=_MC.contract("retrieval.eval.known_items.run", effects=["read", "model", "filesystem"], idempotency="at_least_once", filesystem="state_dir"),
    http_tags=["fabric", "memory", "eval"], memory="off",
    description="Score memory.seek on a frozen known-item set: each query searched (fabric only, the result cache "
                "off), the record's rank turned into MRR, nDCG@10, hit@1/5/10 and latency p50/p95. Inputs: name "
                "(str='v1'), limit (int=100 - items run), minds (dict - switch modes for this run only, e.g. "
                "{\"memory.rerank\": \"on\"}), label (str). Output: {ok, summary, label, minds, path}.",
)
async def cap_known_items_run(name: str = KI.VERSION, limit: int = 100, minds: Optional[Dict[str, str]] = None,
                              label: str = "", trace_id=None) -> Dict[str, Any]:
    p = _dir() / f"{name}.json"
    if not p.is_file():
        return {"ok": False, "error": f"no known-item set {name!r}: build it first"}
    items = json.loads(p.read_text(encoding="utf-8")).get("items") or []
    items = items[:max(1, int(limit))]
    seek = (CAPABILITY_REGISTRY.get("memory.seek") or {}).get("func")
    if not seek:
        return {"ok": False, "error": "memory.seek not loaded"}
    modes = dict(minds or {})
    modes["memory.seek_cache"] = "off"           # measure the search, not the cache
    v = SB.validate(modes)
    if not v.get("ok"):
        return {"ok": False, "error": SB.explain(v)}
    token = SB.RUN_MINDS.set({"modes": dict(v.get("minds") or modes)})
    ranks, lat, rows = [], [], []
    try:
        for it in items:
            t = time.monotonic()
            try:
                res = await seek(query=it["query"], k=10, include_memory=False)
            except Exception as e:
                res = {"error": str(e)}
            ms = round((time.monotonic() - t) * 1000.0, 1)
            ids = [r.get("id") for r in (res or {}).get("results") or []]
            r = KI.rank_of(it["id"], ids)
            ranks.append(r)
            lat.append(ms)
            rows.append({"id": it["id"], "family": it["family"], "rank": r, "ms": ms,
                         "error": (res or {}).get("error", "")})
    finally:
        SB.RUN_MINDS.reset(token)
    summary = KI.summarize(ranks, lat)
    out = {"ok": True, "name": name, "label": label or ("+".join(f"{k}={v}" for k, v in sorted((minds or {}).items()))
                                                       or "default"),
           "minds": modes, "summary": summary, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           # the same run in the W5-03 evaluation contract (backbone review 11 Oct)
           "evaluation_report": KI.evaluation_report(KI.set_revision(items), minds, summary)}
    runs = _dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(dict(out, rows=rows), indent=1), encoding="utf-8")
    return dict(out, path=str(path))
