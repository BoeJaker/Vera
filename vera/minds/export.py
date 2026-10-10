"""decide.export: the gold-labelled training and evaluation set of the decision questions (four-minds FM-2.2;
see export_core).

It reads the live decisions that have their inputs in the training store, their outcomes, and the goal of
each run (for the held-out split), and writes a DatasetSnapshot off the repo:
<state root>/datasets/decide/<snapshot_id>/{examples.jsonl, manifest.json}. The states were redacted when they
were stored. The capability returns counts and the path, never the rows. decide.export.list lists the
snapshots written so far.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import export_core as EX
    from Vera.vera.fabric.dataset_provider import DatasetSnapshot
    from Vera.vera.state_paths import state_dir
except Exception:                        # worktree / app-free import
    from vera.minds import export_core as EX   # type: ignore
    from vera.fabric.dataset_provider import DatasetSnapshot  # type: ignore
    from vera.state_paths import state_dir  # type: ignore

log = logging.getLogger("vera.minds.export")
MAX_ROWS = 20000


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def _root():
    return state_dir("datasets", "decide")


def _write(snap: Dict[str, Any], body: str) -> str:
    d = _root() / snap["snapshot_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "examples.jsonl").write_text(body, encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
    return str(d)


@capability(
    "decide.export", http_method="POST", http_path="/decide/export", http_tags=["minds"],
    memory="off",
    description="Write the gold-labelled training/evaluation set of the decision questions (four-minds FM-2.2) "
                "as a DatasetSnapshot under the state root (datasets/decide/<snapshot_id>/examples.jsonl + "
                "manifest.json; off the repo). One example per live decision with stored inputs: {question "
                "{id, type, instructions, options}, state (redacted), gold, gold_source, gold_strength, weight, "
                "live_answer, origin, group, split}. Gold comes from the strongest outcome that settles the "
                "answer (a named right answer, e.g. a human label; 'correct' = the live answer; 'wrong' = the "
                "other label of a two-label question; on a question with more labels, 'wrong' only rules the live answer "
                "out and is kept as not_label). Whole goals are held out (census templates rerun the "
                "same goal), so no run is split across train and test. Input: question_id (default 'all'), "
                "days (int, default 90), origin (all | non_census | census, default all), min_strength "
                "(weak | medium | strong, default medium), include_unlabelled (bool, default false), holdout "
                "(0-1, default 0.2). Output: {ok, snapshot_id, path, record_count, counts: {question: "
                "{examples, gold, known_wrong, train, test, by_source, by_origin, labels}}}. Rows are never returned.",
)
async def cap_decide_export(question_id: str = "all", days: int = 90, origin: str = "all",
                            min_strength: str = "medium", include_unlabelled: bool = False,
                            holdout: float = EX.HOLDOUT, trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    if origin not in EX.ORIGIN_FILTERS:
        return {"ok": False, "error": f"origin must be one of {list(EX.ORIGIN_FILTERS)}"}
    if min_strength not in EX.LC.STRENGTHS:
        return {"ok": False, "error": f"min_strength must be one of {list(EX.LC.STRENGTHS)}"}
    q = "" if question_id in ("", "all") else str(question_id)
    days = max(1, min(365, int(days or 90)))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT l.decision_id, l.row, s.state FROM decision_states s
               JOIN decision_ledger l ON l.decision_id = s.decision_id
               WHERE ($1 = '' OR s.question_id = $1) AND COALESCE(l.mode, '') <> 'shadow'
                 AND l.ts >= NOW() - make_interval(days => $2)
               ORDER BY l.ts DESC LIMIT $3""", q, days, MAX_ROWS)
        ids = [r["decision_id"] for r in rows]
        outs = await conn.fetch("SELECT decision_id, row FROM decision_outcomes WHERE decision_id = ANY($1::text[])",
                                ids) if ids else []
        lives = [_json(r["row"]) or {} for r in rows]
        runs = sorted({lv.get("run_id") for lv in lives if lv.get("run_id")})
        run_states = await conn.fetch(
            """SELECT l.run_id, s.state FROM decision_states s JOIN decision_ledger l ON l.decision_id = s.decision_id
               WHERE l.run_id = ANY($1::text[]) ORDER BY l.ts""", runs) if runs else []
    by_decision: Dict[str, List[Dict[str, Any]]] = {}
    for o in outs:
        by_decision.setdefault(o["decision_id"], []).append(_json(o["row"]) or {})
    goals = EX.run_goals({"run_id": r["run_id"], "state": _json(r["state"]) or {}} for r in run_states)
    examples = []
    for r, live in zip(rows, lives):
        if not EX.origin_ok(live.get("origin"), origin):
            continue
        e = EX.example(live, _json(r["state"]) or {}, by_decision.get(r["decision_id"], []),
                       goal=goals.get(live.get("run_id"), ""), min_strength=min_strength, holdout=holdout)
        if e["gold"] is not None or e["not_label"] is not None or include_unlabelled:
            examples.append(e)
    body = EX.to_jsonl(examples)
    filters = {"question_id": question_id or "all", "days": days, "origin": origin, "min_strength": min_strength,
               "include_unlabelled": bool(include_unlabelled), "holdout": float(holdout)}
    counts = EX.counts(examples)
    snap, _ = DatasetSnapshot.create(
        dataset_id=EX.DATASET_ID, created_at=datetime.now(timezone.utc).isoformat(),
        records=[json.loads(line) for line in body.splitlines()],
        schema={"example": EX.EXAMPLE_SCHEMA, "format": "jsonl"},
        provenance={"source": "decision_ledger+decision_states+decision_outcomes", "filters": filters,
                    "counts": counts})
    manifest = snap.to_dict()
    path = await asyncio.to_thread(_write, manifest, body)
    log.info("decide.export %s: %d examples (%s)", snap.snapshot_id, snap.record_count, filters)
    return {"ok": True, "snapshot_id": snap.snapshot_id, "path": path, "record_count": snap.record_count,
            "counts": counts}


def _list() -> List[Dict[str, Any]]:
    out = []
    for m in sorted(_root().glob("snap_*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            continue
        prov = d.get("provenance") or {}
        out.append({"snapshot_id": d.get("snapshot_id"), "created_at": d.get("created_at"),
                    "record_count": d.get("record_count"), "filters": prov.get("filters"),
                    "path": str(m.parent)})
    return out


@capability(
    "decide.export.list", http_method="GET", http_path="/decide/export/list", http_tags=["minds"],
    memory="off", silent=True,
    description="The decision-set snapshots decide.export has written (newest first): {snapshots: "
                "[{snapshot_id, created_at, record_count, filters, path}]}. Input: limit (int, default 20).",
)
async def cap_decide_export_list(limit: int = 20, trace_id=None) -> Dict[str, Any]:
    snaps = await asyncio.to_thread(_list)
    return {"snapshots": snaps[:max(1, min(200, int(limit or 20)))]}
