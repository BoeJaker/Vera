"""caps.shortlist: tools from what similar past steps used (four-minds FM-3; see tool_shortlist_core).

The index (`tool_step_index`, Postgres) holds one row per executed loop step: run, step, title, its
MiniLM vector and the tools it called successfully. It is built from run traces (embeddings only, no
LLM): nightly in the 23:00-06:00 window, or on demand with caps.shortlist.index. Each finished loop step
is recorded in the background as a `loop.caps_shortlist` shadow ranking decision with the tools it really
used as outcomes; caps.shortlist.report reads hit@5 / recall@10 / MRR from them. Nothing reads the
shortlist to change behaviour yet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import tool_shortlist_core as TC
    from Vera.vera.minds import judge_core as JC
except Exception:                        # worktree / app-free import
    from vera.minds import tool_shortlist_core as TC   # type: ignore
    from vera.minds import judge_core as JC            # type: ignore
try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import decide_dispatch as DD
except Exception:                        # worktree / app-free import
    from vera.minds import decide_dispatch as DD       # type: ignore

log = logging.getLogger("vera.minds.tool_shortlist")
_PENDING: set = set()
_INDEX: Dict[str, Any] = {"rows": [], "loaded": 0.0, "version": "", "prior": None}
INDEX_TTL_S = 3600
MAX_ROWS = 20000
KEY_NIGHT = "vera:caps:shortlist:night:"
KEY_REPLAY = "vera:caps:shortlist:replay"      # newest first, one snapshot per night (FM-3.2)
REPLAY_KEEP = 90
_DDL = (
    """CREATE TABLE IF NOT EXISTS tool_step_index (
           run TEXT NOT NULL, step TEXT NOT NULL, title TEXT NOT NULL, vec JSONB NOT NULL,
           tools JSONB NOT NULL, task TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           PRIMARY KEY (run, step))""",
)
_DDL_DONE = False


async def _raw(name: str, /, timeout: float = 30.0, **kw) -> Dict[str, Any]:
    cap = CAPABILITY_REGISTRY.get(name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"{name} not available"}
    try:
        out = await asyncio.wait_for(fn(**kw), timeout)
        return out if isinstance(out, dict) else {}
    except Exception as e:
        return {"error": f"{name}: {type(e).__name__}"}


async def _ensure(conn) -> None:
    global _DDL_DONE
    if not _DDL_DONE:
        for s in _DDL:
            await conn.execute(s)
        _DDL_DONE = True


async def _embed(texts: List[str]) -> List[Optional[List[float]]]:
    out: List[Optional[List[float]]] = []
    for i in range(0, len(texts), 64):
        r = await _raw("nlp.embed", 30.0, texts=texts[i:i + 64])
        vecs = r.get("embeddings") if isinstance(r.get("embeddings"), list) else []
        out += vecs if len(vecs) == len(texts[i:i + 64]) else [None] * len(texts[i:i + 64])
    return out


async def _rows() -> List[Dict[str, Any]]:
    """The index, cached in process for an hour."""
    if _INDEX["rows"] and time.time() - _INDEX["loaded"] < INDEX_TTL_S:
        return _INDEX["rows"]
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return _INDEX["rows"]
    async with pool.acquire() as conn:
        await _ensure(conn)
        recs = await conn.fetch("SELECT run, vec, tools, title FROM tool_step_index ORDER BY ts DESC LIMIT $1",
                                MAX_ROWS)
    rows = []
    for r in recs:
        vec = json.loads(r["vec"]) if isinstance(r["vec"], str) else r["vec"]
        tools = json.loads(r["tools"]) if isinstance(r["tools"], str) else r["tools"]
        rows.append({"run": r["run"], "vec": vec, "tools": tools, "title": r["title"]})
    _INDEX.update(rows=rows, loaded=time.time(), prior=TC.prior(rows),
                  version=DD.index_version([f"{r['run']}:{len(r['tools'] or [])}" for r in rows]))
    return rows


async def _push_rows() -> List[Dict[str, Any]]:
    """The index as a node keeps it (POST /decide/index): the tools ride as each row's payload."""
    return [{"id": i, "vec": r["vec"], "run": r["run"], "payload": r["tools"]}
            for i, r in enumerate(_INDEX["rows"])]


async def build_index(limit_runs: int = 400) -> Dict[str, Any]:
    """Index the steps of recent runs not indexed yet. Embeddings only."""
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    runs = (await _raw("evolve.runs", 60.0, source="all", limit=int(limit_runs))).get("runs") or []
    async with pool.acquire() as conn:
        await _ensure(conn)
        done = {r["run"] for r in await conn.fetch("SELECT DISTINCT run FROM tool_step_index")}
    new_rows, traced = [], 0
    for r in runs:
        sid = str(r.get("run_id") or "")
        if not sid or sid in done or sid.startswith("chat:"):
            continue
        tr = await _raw("workshop.agent_loop.trace", 60.0, session_id=sid)
        traced += 1
        for step_id, title, used in TC.steps_from_trace(tr):
            new_rows.append((sid, str(step_id), title[:500], used, str(r.get("task") or "")[:120]))
    vecs = await _embed([t for _, _, t, _, _ in new_rows])
    added = 0
    async with pool.acquire() as conn:
        for (sid, step, title, used, task), vec in zip(new_rows, vecs):
            if not vec:
                continue
            await conn.execute(
                "INSERT INTO tool_step_index (run, step, title, vec, tools, task) VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6) "
                "ON CONFLICT (run, step) DO NOTHING", sid, step, title,
                json.dumps([round(float(x), 5) for x in TC._unit(vec)]), json.dumps(used), task)
            added += 1
        await conn.execute("DELETE FROM tool_step_index WHERE ts < NOW() - INTERVAL '120 days'")
    _INDEX["loaded"] = 0.0
    return {"ok": True, "runs_seen": len(runs), "traced": traced, "steps_added": added}


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.model_binding_core import binding as _bind
except Exception:                        # worktree / app-free import
    from vera.model_binding_core import binding as _bind   # type: ignore

_SERVES_SHORTLIST = _bind(
    "shortlist", "knn", io_in="text", io_out="labels", tier=2, placement="nlp",
    questions=["loop.caps_shortlist"],
    uses="Name the tools a piece of work will most likely need, from what the nearest past loop steps "
         "actually used (MiniLM kNN on an NLP node, filled with the usage prior).",
    limits="work unlike any past step; picking a model (use model.pick).")


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "caps.shortlist", serves=_SERVES_SHORTLIST, http_method="POST", http_path="/caps/shortlist", http_tags=["minds", "caps"],
    contract=_MC.contract("tool.shortlist", effects=["read", "model"], idempotency="idempotent"),
    memory="off", silent=True,
    description="The tools most likely needed for a piece of work, from what similar PAST loop steps actually "
                "used (four-minds FM-3): the similarity-weighted vote of the 15 nearest indexed steps, filled with "
                "the usage prior. Measured on 342 census steps: hit@5 0.91 vs 0.08 for caps.search. Input: text "
                "(str!), k (int, default 10), exclude_run (str: skip one run's own steps). Output: {tools: [{name, "
                "score, by}], index_rows}.",
)
async def cap_caps_shortlist(text: str = "", k: int = 10, exclude_run: str = "", trace_id=None) -> Dict[str, Any]:
    if not str(text or "").strip():
        return {"error": "text is required"}
    rows = await _rows()
    k = max(1, min(30, int(k or 10)))
    if not rows:
        return {"tools": [], "index_rows": 0, "degraded": True}
    # FM-9.1: the nearest past steps are found on an NLP node (one round trip: embed + search there)
    res = await DD.knn("tool_steps", _INDEX["version"], _push_rows, text=str(text)[:1000],
                       k=TC.NEIGHBOURS, exclude_run=exclude_run)
    if res.get("ok"):
        near = [(n["score"], n.get("payload") or []) for n in res.get("neighbours") or []]
        return {"tools": TC.rank_from_neighbours(near, _INDEX["prior"] or TC.prior(rows), k),
                "index_rows": len(rows), "degraded": False, "node": res.get("node"), "ms": res.get("ms")}
    if not await DD.local_allowed():
        return {"tools": [], "index_rows": len(rows), "degraded": True, "error": res.get("error")}
    vec = (await _embed([str(text)[:1000]]) or [None])[0]      # nlp.local: the host, off the event loop
    tools = await asyncio.to_thread(TC.shortlist, vec or [], rows, k, TC.NEIGHBOURS, exclude_run)
    return {"tools": tools, "index_rows": len(rows), "degraded": vec is None, "node": "host"}


@capability(
    "caps.shortlist.index", http_method="POST", http_path="/caps/shortlist/index", http_tags=["minds", "caps"],
    contract=_MC.contract("tool.shortlist.index", effects=["read", "write", "model"], idempotency="idempotent"),
    memory="off",
    description="Index the steps of recent loop runs for caps.shortlist (title, MiniLM vector, tools used, from "
                "run traces; embeddings only, no LLM). Runs nightly by itself; use this for a first fill. Input: "
                "limit_runs (int, default 400). Output: {ok, runs_seen, traced, steps_added}.",
)
async def cap_caps_shortlist_index(limit_runs: int = 400, trace_id=None) -> Dict[str, Any]:
    return await build_index(max(1, min(2000, int(limit_runs or 400))))


@capability(
    "caps.shortlist.report", http_method="GET", http_path="/caps/shortlist/report", http_tags=["minds", "caps"],
    contract=_MC.contract("tool.shortlist.report", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="How the caps.shortlist shadow did on live loop steps (four-minds FM-3): hit@5, recall@10 and MRR "
                "of the tools each step really used, from the loop.caps_shortlist decisions and their outcomes. "
                "Input: days (int, default 14). Output: {summary, index_rows}.",
)
async def cap_caps_shortlist_report(days: int = 14, trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"error": "postgres unavailable"}
    async with pool.acquire() as conn:
        recs = await conn.fetch(
            """SELECT l.decision_id,
                      COALESCE(array_agg((o.row->'detail'->>'rank')::int) FILTER (WHERE o.label = '"used"'), '{}') AS ranks,
                      count(o.decision_id) AS used_n
               FROM decision_ledger l LEFT JOIN decision_outcomes o ON o.decision_id = l.decision_id AND o.source = 'step.tools'
               WHERE l.question_id = $1 AND l.ts >= NOW() - make_interval(days => $2)
               GROUP BY l.decision_id""", TC.QUESTION, max(1, min(365, int(days or 14))))
    rows = [([int(x) for x in (r["ranks"] or []) if x is not None], int(r["used_n"] or 0)) for r in recs]
    return {"summary": TC.summarize([x for x in rows if x[1] > 0]), "index_rows": len(await _rows())}


def on_step(run_id: str, step_id: Any, title: str, used: List[str], origin: str = "user",
            failed: Optional[List[str]] = None) -> None:
    """A finished loop step: record what the shortlist would have said, what the step really used, and what it
    called that failed (FM-3.5: a negative label). Background, never raises, never waits."""
    try:
        if not run_id or not str(title or "").strip() or not (used or failed):
            return
        task = asyncio.get_running_loop().create_task(
            _record(run_id, step_id, str(title), list(used or []), origin, list(failed or [])))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as e:
        log.debug("tool shortlist hook: %s", e)


def _ledger():
    try:
        from Vera.vera.minds import ledger_capabilities as L
    except Exception:                # pragma: no cover
        from vera.minds import ledger_capabilities as L  # type: ignore
    return L


async def _record(run_id: str, step_id: Any, title: str, used: List[str], origin: str,
                  failed: Optional[List[str]] = None) -> None:
    try:
        res = await cap_caps_shortlist(text=title, k=TC.K, exclude_run=run_id)   # keyword only: a @capability wrapper
        names = [t["name"] for t in res.get("tools") or []]
        if not names or res.get("degraded"):
            return
        d = TC.decision(names, run_id=run_id, step_id=step_id, origin=origin)
        L = _ledger()
        for row in [d] + TC.outcomes(d, used, failed or ()):
            await L.append(row)
    except Exception as e:
        log.debug("tool shortlist record: %s", e)


def on_chat_turn(session_id: str, turn_id: str, query: str, server: List[str], used: List[str]) -> None:
    """A chat turn (tools.shortlist.chat=shadow) onto the ledger as the same ranking question, surface chat
    (FM-3.5): the server's shortlist as the answer, the reply's tools as outcomes. The words go to the training
    store, which keeps them only when its `chat` surface is enabled. Background, never raises, never waits."""
    try:
        if not session_id or not server or not used:
            return
        task = asyncio.get_running_loop().create_task(
            _record_chat(str(session_id), str(turn_id or ""), str(query or ""), list(server), list(used)))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as e:
        log.debug("tool shortlist chat hook: %s", e)


async def _record_chat(session_id: str, turn_id: str, query: str, server: List[str], used: List[str]) -> None:
    try:
        d = TC.chat_decision(server, session_id=session_id, turn_id=turn_id)
        L = _ledger()
        for row in [d] + TC.outcomes(d, used):
            await L.append(row)
        ts = sys.modules.get("training_state") or sys.modules.get("Vera.vera.minds.training_state")
        if ts is not None and query.strip():
            ts.put(d, {"text": query})
    except Exception as e:
        log.debug("tool shortlist chat record: %s", e)


async def _night_job() -> None:
    """Rebuild the step index once a night. Prod only (a node worker keeps local time and has no business indexing);
    the night key is set only after the build succeeded, so a failure retries on the next tick."""
    try:
        if getattr(_orch, "_IS_WORKER", False):
            return
        now = datetime.now()
        if not JC.in_window(now.hour):
            return
        night = now.date().isoformat() if now.hour >= 12 else datetime.fromordinal(now.toordinal() - 1).date().isoformat()
        r = getattr(_orch, "REDIS", None)
        if r is None or await r.get(KEY_NIGHT + night):
            return
        out = await build_index()
        if out.get("ok"):
            await r.set(KEY_NIGHT + night, "1", ex=86400)
            try:
                snap = await run_replay(night)
                log.info("caps.shortlist nightly replay %s: %s", night, snap.get("loop"))
            except Exception as e:
                log.warning("tool shortlist replay: %s", e)
        log.info("caps.shortlist nightly index %s: %s", night, out)
    except Exception as e:
        log.warning("tool shortlist night job: %s", e)


schedule(_night_job, interval=900, name="caps_shortlist_index_night", singleton=True, skip_in_sandbox=True)


RERANK_SAMPLE = 150          # steps re-ranked by the cross-encoder per replay (an NLP-node call each)
RERANK_POOL = 30             # how far down the kNN list the cross-encoder looks


def _catalogue() -> Dict[str, Dict[str, Any]]:
    out = {}
    for name, e in list(CAPABILITY_REGISTRY.items()):
        if isinstance(e, dict):
            out[name] = {"tags": list(e.get("tags") or []) + list(e.get("http_tags") or []),
                         "description": str(e.get("description") or "")}
    return out


async def _rerank_sample(rows: List[Dict[str, Any]], catalogue: Dict[str, Dict[str, Any]]) -> Dict[int, List[str]]:
    """The kNN list of a sample of held-out steps, reordered by the cross-encoder (nlp.rerank on the NLP nodes)
    scoring each candidate's description against the step's title. {} when no node answers."""
    rr = (CAPABILITY_REGISTRY.get("nlp.rerank") or {}).get("func")
    if not rr:
        return {}
    usable = [i for i, r in enumerate(rows) if r.get("vec") and r.get("tools") and r.get("title")]
    step = max(1, len(usable) // RERANK_SAMPLE) if usable else 1
    out: Dict[int, List[str]] = {}
    for i in usable[::step][:RERANK_SAMPLE]:
        r = rows[i]
        names = [x["name"] for x in TC.shortlist(r["vec"], rows, RERANK_POOL, exclude_run=r["run"])]
        docs = [f"{n}: {str((catalogue.get(n) or {}).get('description') or '')[:300]}" for n in names]
        try:
            res = await rr(query=str(r["title"])[:500], documents=docs)
        except Exception as e:
            log.debug("replay rerank: %s", e)
            continue
        ranked = res.get("ranked") if isinstance(res, dict) else None
        if not ranked:
            continue
        out[i] = [names[int(x["index"])] for x in ranked if 0 <= int(x.get("index", -1)) < len(names)]
    return out


async def run_replay(night: str = "") -> Dict[str, Any]:
    """The offline replay (FM-3.2) over the whole index, off the event loop (dot products, no embedding, no LLM), with
    the chat shadow's summary beside it; kept as one snapshot per night."""
    _INDEX["loaded"] = 0.0                       # the index may have just been rebuilt
    rows = await _rows()
    t0 = time.monotonic()
    loop = await asyncio.to_thread(TC.replay, rows)
    chat = (await cap_shortlist_chat_report()).get("summary")
    # the tool-choice row of the candidate matrix: every ranker on the same held-out steps
    catalogue = _catalogue()
    reranked = await _rerank_sample(rows, catalogue)
    matrix = await asyncio.to_thread(TC.replay_matrix, rows, catalogue, reranked)
    snap = {"night": night or datetime.now().date().isoformat(), "ts": datetime.now().isoformat(timespec="seconds"),
            "index_rows": len(rows), "loop": loop, "chat": chat, "matrix": matrix,
            "ms": round((time.monotonic() - t0) * 1000)}
    r = getattr(_orch, "REDIS", None)
    if r is not None:
        await r.lpush(KEY_REPLAY, json.dumps(snap))
        await r.ltrim(KEY_REPLAY, 0, REPLAY_KEEP - 1)
    return snap


# ── FM-3.3 'on' (owner, 17 Oct): the chat agent reaches every SAFE capability through a shortlist ───────────────────
try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import chat_caps_core as _CC
except Exception:                        # worktree / app-free import
    from vera.minds import chat_caps_core as _CC   # type: ignore
_SAFE: Dict[str, Any] = {"at": 0.0, "cat": {}}
SAFE_TTL_S = 600
CHAT_RERANK_POOL = 40
CHAT_RERANK_TIMEOUT_S = 0.6


def safe_catalogue() -> Dict[str, Dict[str, Any]]:
    """The safe capabilities (chat_caps_core.safe), rebuilt every 10 min."""
    if not _SAFE["cat"] or time.time() - _SAFE["at"] > SAFE_TTL_S:
        _SAFE.update(cat=_CC.catalogue(dict(CAPABILITY_REGISTRY)), at=time.time())
    return _SAFE["cat"]


async def _chat_mode(minds: Optional[Dict[str, Any]] = None) -> str:
    try:
        eff = (CAPABILITY_REGISTRY.get("minds.effective") or {}).get("func")
        if eff:
            return str((((await eff(minds=minds or None)) or {}).get("modes") or {}).get("tools.shortlist.chat")
                       or "legacy")
    except Exception as e:
        log.debug("chat shortlist mode: %s", e)
    return "legacy"


@capability(
    "caps.chat_shortlist", http_method="POST", http_path="/caps/chat_shortlist", http_tags=["minds", "caps"],
    contract=_MC.contract("tool.shortlist.chat", effects=["read", "model"], idempotency="idempotent"),
    memory="off", silent=True,
    description="The capabilities a chat agent should be offered for a message, from every SAFE capability (four-minds "
                "FM-3.3; switch tools.shortlist.chat=on): what similar past steps used and the keyword scorer over the "
                "safe catalogue, fused, the head reordered by the cross-encoder when it answers within 0.6 s. Safe = "
                "exposed, read / network / model effects with no approval (or a lookup-like name when none are "
                "declared), never shells, secrets, deploys or system controls. Inputs: text (str!), k (int=24, max "
                "40), force (bool - answer whatever the switch says, for a check). Output: {mode, tools: [{name, "
                "score, by}], safe_total, reranked, ms}.",
)
async def cap_caps_chat_shortlist(text: str = "", k: int = 24, force: bool = False, minds: Optional[Dict[str, Any]] = None,
                                  trace_id=None) -> Dict[str, Any]:
    t0 = time.monotonic()
    mode = await _chat_mode(minds)
    if mode != "on" and not force:
        return {"mode": mode, "tools": []}
    q = str(text or "").strip()
    if not q:
        return {"mode": mode, "tools": [], "error": "text is required"}
    k = max(1, min(40, int(k or 24)))
    cat = safe_catalogue()
    lists: Dict[str, List[str]] = {}
    try:
        knn = await cap_caps_shortlist(text=q, k=30)
        lists["past_steps"] = [t["name"] for t in knn.get("tools") or []]
    except Exception as e:
        log.debug("chat shortlist knn: %s", e)
    lists["keywords"] = await asyncio.to_thread(TC.lexical_ranking, q, cat, 60)
    fused = _CC.fuse(lists, cat, max(k, CHAT_RERANK_POOL))
    reranked = False
    rr = (CAPABILITY_REGISTRY.get("nlp.rerank") or {}).get("func")
    if rr and len(fused) > 1:
        names = [f["name"] for f in fused]
        try:
            res = await asyncio.wait_for(rr(query=q[:500], documents=[_CC.card(n, cat[n]) for n in names]),
                                         CHAT_RERANK_TIMEOUT_S)
            order = [names[int(x["index"])] for x in (res or {}).get("ranked") or []
                     if 0 <= int(x.get("index", -1)) < len(names)]
            if order:
                pos = {n: i for i, n in enumerate(order)}
                fused.sort(key=lambda f: pos.get(f["name"], len(order)))
                for f in fused:
                    f["by"] += "+rerank"
                reranked = True
        except Exception as e:
            log.debug("chat shortlist rerank: %s", e)
    return {"mode": mode, "tools": fused[:k], "safe_total": len(cat), "reranked": reranked,
            "ms": int((time.monotonic() - t0) * 1000)}


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import chat_tools_core as _CT
except Exception:                        # worktree / app-free import
    from vera.minds import chat_tools_core as _CT   # type: ignore


@capability(
    "caps.shortlist.chat_report", http_method="GET", http_path="/caps/shortlist/chat_report",
    contract=_MC.contract("tool.shortlist.chat_report", effects=["read"], idempotency="idempotent"),
    http_tags=["minds", "caps"], memory="off", silent=True,
    description="The chat tool shortlist in shadow (four-minds FM-3.2/3.3, switch tools.shortlist.chat=shadow): over "
                "recorded chat turns, recall@16/24/28 of the capabilities the reply used for the page's own list and "
                "for caps.shortlist, the median position of a used capability in each, and how often the model used "
                "one the page never offered. Input: limit (int=2000 - newest rows). Output: {rows, summary, examples}.",
)
async def cap_shortlist_chat_report(limit: int = 2000, trace_id=None) -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"rows": 0, "error": "redis unavailable"}
    raw = await r.lrange("vera:chat:tools:shadow", 0, max(1, min(2000, int(limit or 2000))) - 1)
    rows = []
    for x in raw:
        try:
            rows.append(json.loads(x))
        except Exception:
            continue
    return {"rows": len(rows), "summary": _CT.summarize(rows),
            "examples": [r for r in rows if r.get("used")][:5]}


@capability(
    "caps.shortlist.replay", http_method="POST", http_path="/caps/shortlist/replay", http_tags=["minds", "caps"],
    contract=_MC.contract("tool.shortlist.replay", effects=["read", "write"], idempotency="at_least_once"),
    memory="off", silent=True,
    description="The tool shortlist's offline replay (four-minds FM-3.2): every indexed loop step asks caps.shortlist "
                "again with its own run held out - recall@5/10/16/24/28, hit@5, MRR, median position and the "
                "unknown-tool rate (tools no other run used) - with the chat shadow's summary beside it. Runs nightly "
                "after the index rebuild (dot products only, no model). Input: run (bool, default false: return the "
                "kept snapshots; true: replay now), history (int, default 14). Output: {latest, history}.",
)
async def cap_caps_shortlist_replay(run: bool = False, history: int = 14, trace_id=None) -> Dict[str, Any]:
    if isinstance(run, str):
        run = run.strip().lower() in ("1", "true", "yes")
    if run:
        snap = await run_replay()
        return {"latest": snap, "history": []}
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"error": "redis unavailable"}
    raw = await r.lrange(KEY_REPLAY, 0, max(1, min(REPLAY_KEEP, int(history or 14))) - 1)
    snaps = []
    for x in raw:
        try:
            snaps.append(json.loads(x))
        except Exception:
            continue
    return {"latest": snaps[0] if snaps else None, "history": snaps}


# ── FM-3.5: the tool-ranking training set ─────────────────────────────────────────────────────────────────────────
def _labels_root():
    try:
        from Vera.vera.state_paths import state_dir
    except Exception:                    # pragma: no cover
        from vera.state_paths import state_dir  # type: ignore
    return state_dir("datasets", "caps_shortlist")


@capability(
    "caps.shortlist.labels", http_method="POST", http_path="/caps/shortlist/labels", http_tags=["minds", "caps"],
    contract=_MC.contract("tool.shortlist.labels", effects=["read", "filesystem"], idempotency="non_idempotent",
                          filesystem="state_dir"),
    memory="off",
    description="Write the tool-ranking training set (four-minds FM-3.5): every recorded loop.caps_shortlist decision "
                "(loop steps and chat turns) as {text, offered (in order), used, failed, labels: [{tool, rank, label "
                "1|0, why: used | offered_unused | failed | used_not_offered}], split by run/session}, the text from "
                "the step index (loop) or the training store (chat, only when its chat surface is on). For a learned "
                "ranker trained on outcomes, not on the model's choice (the ML Workshop, MW-6). Inputs: days (int=90, "
                "max 365), surface (all | loop | chat), holdout (float=0.2). Output: {ok, path, counts}; never the rows.",
)
async def cap_caps_shortlist_labels(days: int = 90, surface: str = "all", holdout: float = 0.2,
                                    trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    if surface not in ("all", "loop", "chat"):
        return {"ok": False, "error": "surface must be all, loop or chat"}
    days = max(1, min(365, int(days or 90)))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT decision_id, row FROM decision_ledger WHERE question_id = $1
                 AND ($2 = 'all' OR surface = $2) AND ts >= NOW() - make_interval(days => $3)
               ORDER BY ts DESC LIMIT $4""", TC.QUESTION, surface, days, MAX_ROWS)
        ids = [r["decision_id"] for r in rows]
        outs = await conn.fetch("SELECT decision_id, row FROM decision_outcomes WHERE decision_id = ANY($1::text[])",
                                ids) if ids else []
        try:
            states = await conn.fetch("SELECT decision_id, state FROM decision_states WHERE decision_id = ANY($1::text[])",
                                      ids) if ids else []
        except Exception:                # the training store may not exist yet
            states = []
        lives = [json.loads(r["row"]) if isinstance(r["row"], str) else dict(r["row"]) for r in rows]
        steps = []
        for lv in lives:
            ref = (lv.get("state_ref") or {})
            if ref.get("kind") == "loop_step" and ":" in str(ref.get("id") or ""):
                run, step = str(ref["id"]).rsplit(":", 1)
                steps.append((run, step))
        titles: Dict[tuple, str] = {}
        if steps:
            try:
                await _ensure(conn)
                got = await conn.fetch("SELECT run, step, title FROM tool_step_index WHERE run = ANY($1::text[])",
                                       sorted({s[0] for s in steps}))
                titles = {(g["run"], str(g["step"])): g["title"] for g in got}
            except Exception as e:
                log.debug("labels: step titles: %s", e)
    by_d: Dict[str, List[Dict[str, Any]]] = {}
    for o in outs:
        by_d.setdefault(o["decision_id"], []).append(json.loads(o["row"]) if isinstance(o["row"], str) else dict(o["row"]))
    texts = {}
    for st in states:
        v = json.loads(st["state"]) if isinstance(st["state"], str) else dict(st["state"] or {})
        if v.get("text"):
            texts[st["decision_id"]] = str(v["text"])
    examples = []
    for lv in lives:
        ref = lv.get("state_ref") or {}
        text = texts.get(lv.get("decision_id"), "")
        if not text and ref.get("kind") == "loop_step" and ":" in str(ref.get("id") or ""):
            text = titles.get(tuple(str(ref["id"]).rsplit(":", 1)), "")
        examples.append(TC.ranking_example(lv, by_d.get(lv.get("decision_id"), []), text,
                                           holdout=max(0.0, min(0.5, float(holdout)))))
    counts = TC.ranking_counts(examples)
    snap_id = "snap_" + datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    manifest = {"snapshot_id": snap_id, "dataset_id": TC.RANK_DATASET_ID, "schema": TC.RANK_SCHEMA,
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "filters": {"days": days, "surface": surface, "holdout": holdout}, "counts": counts}

    def _write() -> str:
        d = _labels_root() / snap_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "examples.jsonl").write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in examples),
                                          encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
        return str(d)
    path = await asyncio.to_thread(_write)
    return {"ok": True, "snapshot_id": snap_id, "path": path, "counts": counts}
