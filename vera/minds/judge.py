"""Nightly shadow scoring: a small model judges stored decisions (four-minds FM-2; see judge_core).

decide.shadow.score takes a question's recent live decisions that have their inputs in the training
store and no shadow from this model yet, asks the small model each as a one-token question (label
logprobs, thinking off, job_type idle_decide so it is background work), and records the answer as a
shadow decision linked to the live one. It stops when the Ollama gate is held. decide.shadow.report
reads agreement and AUC against the live answers.

The background-inference rule applies: the scorer is queued once a night (23:00-06:00 local) through
background.cap.enqueue, capped (VERA_DECIDE_SHADOW_NIGHT_CAP, default 40 per question), and never runs
intraday on its own. VERA_DECIDE_SHADOW=0 turns the nightly run off; the model is
VERA_DECIDE_SHADOW_MODEL (default qwen3.5:2b).
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


def _model() -> str:
    return os.environ.get("VERA_DECIDE_SHADOW_MODEL", "qwen3.5:2b").strip() or "qwen3.5:2b"


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
                "by_implementation: {impl: {n, agreement, auc (binary questions), live_positive, live_negative}}}. "
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
            """SELECT x.row->>'implementation' AS impl, x.row->'answer' AS sa, x.p AS sp, l.row->'answer' AS la
               FROM decision_ledger x JOIN decision_ledger l ON l.decision_id = x.row->'detail'->>'for'
               WHERE x.mode = 'shadow' AND x.question_id = $1 AND x.ts >= NOW() - make_interval(days => $2)""",
            question_id, max(1, min(365, int(days or 30))))
    by: Dict[str, List] = {}
    for r in rows:
        la = json.loads(r["la"]) if isinstance(r["la"], str) else r["la"]
        sa = json.loads(r["sa"]) if isinstance(r["sa"], str) else r["sa"]
        by.setdefault(r["impl"] or "?", []).append((la, sa, r["sp"]))
    return {"question_id": question_id, "by_implementation": {k: JC.agreement(v, positive) for k, v in by.items()}}


async def _night_job() -> None:
    """Queue tonight's scoring once, through the background queue (the background-inference rule)."""
    try:
        if str(os.environ.get("VERA_DECIDE_SHADOW", "1")).strip().lower() in ("0", "false", "no", "off"):
            return
        now = datetime.now()
        if not JC.in_window(now.hour):
            return
        night = (now.date() if now.hour >= 12 else now.date().fromordinal(now.date().toordinal() - 1)).isoformat()
        r = getattr(_orch, "REDIS", None)
        if r is None or not await r.set(KEY_NIGHT + night, "1", nx=True, ex=86400):
            return
        cap = int(os.environ.get("VERA_DECIDE_SHADOW_NIGHT_CAP", "40") or 40)
        for qid in JC.QUESTIONS:
            await _raw("background.cap.enqueue", name="decide.shadow.score", title=f"shadow judge: {qid}",
                       arguments={"question_id": qid, "limit": cap, "since_hours": 48},
                       timeout_s=1800, submitted_by="decide.shadow")
    except Exception as e:
        log.debug("shadow night job: %s", e)


schedule(_night_job, interval=900, name="decide_shadow_night", singleton=True, skip_in_sandbox=True)
