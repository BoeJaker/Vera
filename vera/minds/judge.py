"""Nightly shadow scoring: a small model judges stored decisions (four-minds FM-2; see judge_core).

decide.shadow.score takes a question's recent live decisions that have their inputs in the training
store and no shadow from this model yet, asks the small model each as a one-token question (label
logprobs, thinking off, job_type idle_decide so it is background work), and records the answer as a
shadow decision linked to the live one. It stops when the Ollama gate is held. decide.shadow.report
reads agreement and AUC against the live answers.

The background-inference rule applies: the scorer is queued once a night (23:00-06:00 local) through
background.cap.enqueue, capped (VERA_DECIDE_SHADOW_NIGHT_CAP, default 40 per question), and never runs
intraday on its own. VERA_DECIDE_SHADOW=0 turns the nightly run off; the model is
VERA_DECIDE_SHADOW_MODEL (a comma list; default qwen3:4b-instruct-2507-q4_K_M,qwen3:0.6b - offline on 39
stored step_verify decisions the 4b ranked the live verdicts at AUC 0.91 and the 0.6b was confident on half of them
with 90% agreement, so they are judged side by side as the two tiers of the cascade).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import judge_core as JC
except Exception:                        # worktree / app-free import
    from vera.minds import judge_core as JC   # type: ignore

log = logging.getLogger("vera.minds.judge")
JOB_TYPE = "idle_decide"
KEY_NIGHT = "vera:decide:shadow:night:"


DEFAULT_MODELS = "qwen3:4b-instruct-2507-q4_K_M,qwen3:0.6b"


def _models() -> List[str]:
    raw = os.environ.get("VERA_DECIDE_SHADOW_MODEL", DEFAULT_MODELS) or DEFAULT_MODELS
    return [m.strip() for m in raw.split(",") if m.strip()] or [m for m in DEFAULT_MODELS.split(",")]


def _model() -> str:
    """The first (judge) model, for a call that names none."""
    return _models()[0]


def _impl(model: str) -> str:
    return f"llm:{model}"


async def _raw(name: str, **kw) -> Dict[str, Any]:
    cap = CAPABILITY_REGISTRY.get(name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"{name} not available"}
    try:
        out = await fn(**kw)
        return out if isinstance(out, dict) else {}
    except Exception as e:
        return {"error": str(e)}


async def _gate_busy() -> bool:
    st = await _raw("ollama.gate.status")
    return any(int(n.get("held") or 0) > 0 for n in (st.get("nodes") or []) if isinstance(n, dict))


@capability(
    "decide.shadow.score", http_method="POST", http_path="/decide/shadow/score", http_tags=["minds"],
    memory="off",
    description="Score stored loop decisions with a small model as a SHADOW judge (four-minds FM-2): each "
                "recent live decision of the question with stored inputs and no shadow from this model is "
                "put to the model as a one-token question; the label probability comes from logprobs and the "
                "answer is recorded as a shadow decision linked to the live one (detail.for). Background work "
                "(job_type idle_decide); stops when the Ollama gate is held. Queued nightly; run by hand only "
                "for a small check. Input: question_id (default loop.step_verify), model (default "
                "VERA_DECIDE_SHADOW_MODEL), limit (int, default 20, max 200), since_hours (int, default 72). "
                "Output: {ok, scored, skipped, stopped, answers: [{for, answer, p, live}]}.",
)
async def cap_decide_shadow_score(question_id: str = "loop.step_verify", model: str = "", limit: int = 20,
                                  since_hours: int = 72, trace_id=None) -> Dict[str, Any]:
    q = JC.QUESTIONS.get(question_id)
    if not q:
        return {"ok": False, "error": f"no shadow judge for {question_id!r}", "questions": list(JC.QUESTIONS)}
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    model = (model or _model()).strip()
    impl = _impl(model)
    limit = max(1, min(200, int(limit or 20)))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.decision_id, s.state, l.row FROM decision_states s
               JOIN decision_ledger l ON l.decision_id = s.decision_id
               WHERE s.question_id = $1 AND COALESCE(l.mode, '') <> 'shadow'
                 AND s.ts >= NOW() - make_interval(hours => $2)
                 AND NOT EXISTS (SELECT 1 FROM decision_ledger x WHERE x.mode = 'shadow'
                                 AND x.row->'detail'->>'for' = s.decision_id AND x.row->>'implementation' = $3)
               ORDER BY s.ts DESC LIMIT $4""", question_id, int(since_hours or 72), impl, limit)
    try:
        from Vera.vera.minds import ledger_capabilities as L
    except Exception:                    # pragma: no cover
        from vera.minds import ledger_capabilities as L  # type: ignore
    scored, skipped, stopped, answers = 0, 0, "", []
    for r in rows:
        if await _gate_busy():
            stopped = "the Ollama gate is held; the rest waits for the next run"
            break
        state = json.loads(r["state"]) if isinstance(r["state"], str) else (r["state"] or {})
        live = json.loads(r["row"]) if isinstance(r["row"], str) else (r["row"] or {})
        msg = JC.user_message(question_id, state)
        if not msg:
            skipped += 1
            continue
        meta: Dict[str, Any] = {}
        t0 = time.monotonic()
        try:
            await _orch.ollama_generate(msg, system=q["system"], model=model, job_type=JOB_TYPE, prefer_gpu=True,
                                        think=False, options={"num_predict": 1, "temperature": 0},
                                        logprobs=10, meta_out=meta, timeout=180)
        except Exception as e:
            log.debug("shadow judge call: %s", e)
            skipped += 1
            continue
        lps = meta.get("logprobs") or []
        probs, mass = JC.label_probs((lps[0] if lps else {}).get("top_logprobs") or [], q["labels"])
        answer, p = JC.verdict(question_id, probs)
        if not answer:
            skipped += 1
            continue
        row = JC.shadow_decision(live, answer, p, impl, mass=mass, ms=int((time.monotonic() - t0) * 1000))
        res = await L.append(row)
        if res.get("ok"):
            scored += 1
            answers.append({"for": live.get("decision_id"), "answer": answer, "p": p, "live": live.get("answer")})
    return {"ok": True, "question_id": question_id, "model": model, "candidates": len(rows), "scored": scored,
            "skipped": skipped, "stopped": stopped, "answers": answers[:50]}


@capability(
    "decide.shadow.report", http_method="GET", http_path="/decide/shadow/report", http_tags=["minds"],
    memory="off", silent=True,
    description="How a shadow judge compares with the live decisions it shadows (four-minds FM-2): agreement "
                "rate and the AUC of its probability against the live answer, per implementation. Input: "
                "question_id (default loop.step_verify), days (int, default 30). Output: {question_id, "
                "by_implementation: {impl: {n, agreement, auc (binary questions), live_positive, live_negative, "
                "vs_outcomes: {n, accuracy, ece, at_confidence: {threshold: {coverage, precision}}} (binary questions, "
                "against the true label from the live decision's strong/medium outcome)}}}. "
                "With question_id 'all': one entry per judged question.",
)
async def cap_decide_shadow_report(question_id: str = "loop.step_verify", days: int = 30,
                                   trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"error": "postgres unavailable"}
    if question_id == "all":
        return {"questions": {qid: (await cap_decide_shadow_report(question_id=qid, days=days)).get("by_implementation", {})
                              for qid in JC.QUESTIONS}}
    positive = JC.positive_label(question_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT x.row->>'implementation' AS impl, x.row->'answer' AS sa, x.p AS sp, l.row->'answer' AS la,
                      (SELECT o.label FROM decision_outcomes o WHERE o.decision_id = l.decision_id
                         AND o.strength IN ('strong', 'medium') ORDER BY (o.strength = 'strong') DESC, o.ts DESC
                         LIMIT 1) AS ol
               FROM decision_ledger x JOIN decision_ledger l ON l.decision_id = x.row->'detail'->>'for'
               WHERE x.mode = 'shadow' AND x.question_id = $1 AND x.ts >= NOW() - make_interval(days => $2)""",
            question_id, max(1, min(365, int(days or 30))))
    by: Dict[str, List] = {}
    truth: Dict[str, List] = {}
    for r in rows:
        la = json.loads(r["la"]) if isinstance(r["la"], str) else r["la"]
        sa = json.loads(r["sa"]) if isinstance(r["sa"], str) else r["sa"]
        ol = json.loads(r["ol"]) if isinstance(r["ol"], str) else r["ol"]
        impl = r["impl"] or "?"
        by.setdefault(impl, []).append((la, sa, r["sp"]))
        y = JC.true_label(question_id, la, ol)
        if y is not None:
            truth.setdefault(impl, []).append((r["sp"], y))
    out = {}
    for impl, pairs in by.items():
        out[impl] = JC.agreement(pairs, positive)
        if positive is not None:
            out[impl]["vs_outcomes"] = JC.calibration(truth.get(impl, []), positive)
    return {"question_id": question_id, "by_implementation": out}


_DDL_VEC = ("""CREATE TABLE IF NOT EXISTS decision_vectors (
                 decision_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, vec JSONB NOT NULL,
                 ts TIMESTAMPTZ NOT NULL DEFAULT NOW())""",)


async def _embed(texts: List[str]) -> List[Optional[List[float]]]:
    out: List[Optional[List[float]]] = []
    for i in range(0, len(texts), 64):
        r = await _raw("nlp.embed", texts=texts[i:i + 64])
        vecs = r.get("embeddings") if isinstance(r.get("embeddings"), list) else []
        out += vecs if len(vecs) == len(texts[i:i + 64]) else [None] * len(texts[i:i + 64])
    return out


@capability(
    "decide.shadow.knn", http_method="POST", http_path="/decide/shadow/knn", http_tags=["minds"],
    memory="off",
    description="A kNN shadow judge (four-minds FM-2, tier 2 of the cascade): each stored loop decision of the "
                "question is answered by the similarity-weighted vote of the live answers of its 7 nearest past "
                "decisions (MiniLM over the same text the LLM judge sees; decisions of the same run excluded) and "
                "recorded as a shadow decision (implementation knn:minilm) linked to the live one. Embeddings only, "
                "no LLM; runs nightly beside decide.shadow.score. Input: question_id (default loop.step_verify; "
                "'all' for every judged question), limit (int, default 200), since_hours (int, default 72). "
                "Output: {ok, scored, skipped, pool}.",
)
async def cap_decide_shadow_knn(question_id: str = "loop.step_verify", limit: int = 200, since_hours: int = 72,
                                trace_id=None) -> Dict[str, Any]:
    if question_id == "all":
        return {"ok": True, "questions": {q: await cap_decide_shadow_knn(question_id=q, limit=limit,
                                                                          since_hours=since_hours)
                                          for q in JC.QUESTIONS}}
    if question_id not in JC.QUESTIONS:
        return {"ok": False, "error": f"no shadow judge for {question_id!r}"}
    pool_db = getattr(_orch, "PG_POOL", None)
    if pool_db is None:
        return {"ok": False, "error": "postgres unavailable"}
    async with pool_db.acquire() as conn:
        for s in _DDL_VEC:
            await conn.execute(s)
        rows = await conn.fetch(
            """SELECT s.decision_id, s.state, l.row, v.vec FROM decision_states s
               JOIN decision_ledger l ON l.decision_id = s.decision_id
               LEFT JOIN decision_vectors v ON v.decision_id = s.decision_id
               WHERE s.question_id = $1 AND COALESCE(l.mode, '') <> 'shadow'
               ORDER BY s.ts DESC LIMIT 3000""", question_id)
        done = {r["f"] for r in await conn.fetch(
            """SELECT x.row->'detail'->>'for' AS f FROM decision_ledger x
               WHERE x.mode = 'shadow' AND x.question_id = $1 AND x.row->>'implementation' = $2""",
            question_id, JC.KNN_IMPL)}
        recent = {r["decision_id"] for r in await conn.fetch(
            "SELECT decision_id FROM decision_states WHERE question_id = $1 AND ts >= NOW() - make_interval(hours => $2)",
            question_id, int(since_hours or 72))}
    items = []
    for r in rows:
        state = json.loads(r["state"]) if isinstance(r["state"], str) else (r["state"] or {})
        live = json.loads(r["row"]) if isinstance(r["row"], str) else (r["row"] or {})
        vec = json.loads(r["vec"]) if isinstance(r["vec"], str) else r["vec"]
        items.append({"id": r["decision_id"], "live": live, "text": JC.user_message(question_id, state), "vec": vec})
    missing = [it for it in items if it["vec"] is None and it["text"]]
    if missing:
        vecs = await _embed([it["text"][:2000] for it in missing])
        async with pool_db.acquire() as conn:
            for it, v in zip(missing, vecs):
                if v:
                    it["vec"] = JC._unit(v)
                    await conn.execute(
                        "INSERT INTO decision_vectors (decision_id, question_id, vec) VALUES ($1, $2, $3::jsonb) "
                        "ON CONFLICT (decision_id) DO NOTHING", it["id"], question_id,
                        json.dumps([round(x, 5) for x in it["vec"]]))
    pool = [{"vec": it["vec"], "answer": it["live"].get("answer"), "run": it["live"].get("run_id")}
            for it in items if it["vec"]]
    try:
        from Vera.vera.minds import ledger_capabilities as L
    except Exception:                    # pragma: no cover
        from vera.minds import ledger_capabilities as L  # type: ignore
    scored = skipped = 0
    for it in items:
        if scored >= int(limit or 200):
            break
        if it["id"] in done or it["id"] not in recent or not it["vec"]:
            continue
        answer, p, n = JC.knn_vote(question_id, it["vec"], pool, exclude_run=it["live"].get("run_id") or "")
        if not answer:
            skipped += 1
            continue
        res = await L.append(JC.shadow_decision(it["live"], answer, p, JC.KNN_IMPL, mass=1.0))
        scored += 1 if res.get("ok") else 0
    return {"ok": True, "question_id": question_id, "scored": scored, "skipped": skipped, "pool": len(pool)}


async def _night_job() -> None:
    """Queue tonight's scoring through the background queue (the background-inference rule), once a night.

    Prod only: a node worker shares the Redis lock but not prod's background queue, and keeps local time while
    prod keeps UTC - on 2026-10-09 a worker took the night key an hour before prod's window and queued nothing
    anywhere. The night key is set only once every job is queued, so a failure retries on the next tick; the
    job ids are fixed per night, model and question, so a retry replaces rather than duplicates."""
    try:
        if getattr(_orch, "_IS_WORKER", False):
            return
        if str(os.environ.get("VERA_DECIDE_SHADOW", "1")).strip().lower() in ("0", "false", "no", "off"):
            return
        now = datetime.now()
        if not JC.in_window(now.hour):
            return
        night = (now.date() if now.hour >= 12 else now.date().fromordinal(now.date().toordinal() - 1)).isoformat()
        r = getattr(_orch, "REDIS", None)
        if r is None or await r.get(KEY_NIGHT + night):
            return
        cap = int(os.environ.get("VERA_DECIDE_SHADOW_NIGHT_CAP", "40") or 40)
        jobs = [("decide.shadow.score", f"shadow judge: {qid} ({model})",
                 {"question_id": qid, "model": model, "limit": cap, "since_hours": 48}, f"{qid}|{model}")
                for model in _models() for qid in JC.QUESTIONS]
        # the kNN judge: embeddings only, every question, beside the LLM judge
        jobs.append(("decide.shadow.knn", "shadow judge: kNN, all questions",
                     {"question_id": "all", "limit": 400, "since_hours": 48}, "knn"))
        queued, failed = 0, []
        for name, title, args, key in jobs:
            jid = "shadow-" + night + "-" + "".join(c if c.isalnum() else "-" for c in key)[:60]
            res = await _raw("background.cap.enqueue", name=name, title=title, arguments=args, timeout_s=1800,
                             id=jid, submitted_by="decide.shadow")
            if res.get("ok"):
                queued += 1
            else:
                failed.append(f"{key}: {str(res.get('error') or res.get('reason') or res)[:80]}")
        if not failed:
            await r.set(KEY_NIGHT + night, "1", ex=86400)
        log.info("shadow judge night %s: queued %d of %d%s", night, queued, len(jobs),
                 f"; failed {failed[:3]} (retrying next tick)" if failed else "")
    except Exception as e:
        log.warning("shadow night job: %s", e)


schedule(_night_job, interval=900, name="decide_shadow_night", singleton=True, skip_in_sandbox=True)
