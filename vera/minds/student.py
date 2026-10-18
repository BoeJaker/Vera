"""A pre-trained student answering a live decision in-band (four-minds FM-2.5, the System 1 cascade).

The student gets exactly the prompt the big model gets (for loop.step_verify: the live verifier's evidence and
rules, asked for one word) and answers with one token; the label probabilities come from that token's logprobs
(see judge_core). The loop decides what to do with the answer, per the switchboard
(`decide.student.step_verify`, `decide.student.controller_action`):

- shadow: the student runs beside the big model and is recorded as a shadow of the live decision;
- first: the student answers first; its answer stands when it is an allowed label at confidence >= tau,
  otherwise the big model decides as before (and the student is recorded as a shadow).

Settings: VERA_DECIDE_STUDENT_MODEL (default qwen3:1.7b - 11 Oct 2026: CPU nodes too slow for an inline check
(15-78 s), and on gpu-250 only a 1.7B fits beside the 9b, at 6k ctx, 83.5% of the V100),
VERA_DECIDE_STUDENT_TAU (default 0.95), VERA_DECIDE_STUDENT_VERIFY_LABELS (default "met": a "not met" verdict
needs the big model's reason, which the retry that follows is built from), VERA_DECIDE_STUDENT_CONTROLLER_LABELS
(default "continue": the others need the big model's new steps; a student's continue carries no steer for the
next step), VERA_DECIDE_STUDENT_TIMEOUT_S (default 20), VERA_DECIDE_STUDENT_INSTANCE (an Ollama node id; blank =
routing with the GPU preferred). On gpu-250 the student must sit IN the warm plan (a second slot), or each call
evicts the 9B - measured 2026-10-10, the next big call took 36.5 s against 18.3 s. Never raises: any failure is None and the big model decides.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

try:
    from Vera.vera.minds import judge_core as JC, switchboard_core as SB
except Exception:                       # worktree / app-free import
    from vera.minds import judge_core as JC, switchboard_core as SB  # type: ignore

log = logging.getLogger("vera.minds.student")
DEFAULT_MODEL = "qwen3:1.7b"
FEATURE = {"loop.step_verify": "decide.student.step_verify",
           "loop.controller_action": "decide.student.controller_action",
           # every question at once (owner, 17 Oct): each has a switch; which student answers it, and from what
           # confidence, comes from the cascade plan (decide.cascade.plan / cascade_core), not from settings here
           "loop.step_done": "decide.student.step_done",
           "loop.prestep_needed": "decide.student.prestep_needed",
           "loop.intent": "decide.student.intent",
           "loop.tier": "decide.student.tier"}
#: the two questions that had their own settings before the plan (an LLM student, VERA_DECIDE_STUDENT_TAU and the
#: label lists below) keep them, so their switches mean what they meant
LEGACY = frozenset({"loop.step_verify", "loop.controller_action"})
PLAN_KEY = "vera:decide:cascade:plan"
PLAN_TTL_S = 600.0
KNN_VERSION_KEY = "vera:decide:knn:version:"
PLAN: Dict[str, Any] = {"plan": {}, "at": -1e9}


def set_plan(plan: Optional[Dict[str, Any]]) -> None:
    PLAN.update(plan=dict(plan or {}), at=time.monotonic())


async def load_plan() -> Dict[str, Any]:
    """The cascade plan, from Redis at most every 10 min; {} (the LLM student, the default tau) when unreadable."""
    if time.monotonic() - PLAN["at"] < PLAN_TTL_S:
        return PLAN["plan"]
    try:
        import Vera.vera.capability_orchestration as _orch
        r = getattr(_orch, "REDIS", None)
        raw = await r.get(PLAN_KEY) if r is not None else None
        if raw:
            set_plan(json.loads(raw))
            return PLAN["plan"]
    except Exception as e:
        log.debug("cascade plan: %s", e)
    PLAN["at"] = time.monotonic()
    return PLAN["plan"]


#: implementation id -> the NLP family that answers it live (matrix_core.NLP_FAMILY)
_NLP = {"zeroshot:deberta-base": "zeroshot", "nli:deberta-large": "nli", "rerank:msmarco-minilm": "rerank"}


def impl_for(question_id: str) -> str:
    """The plan's student for this question: 'knn', 'card', 'nlp:<family>', 'onnx:<slug>' or 'llm' (an LLM for the
    legacy two and when the plan names none)."""
    if question_id in LEGACY:
        return "llm"
    i = str(((PLAN["plan"] or {}).get(question_id) or {}).get("impl") or "")
    if i == JC.KNN_IMPL:
        return "knn"
    if i == JC.CARD_IMPL:
        return "card"
    if i in _NLP:
        return "nlp:" + _NLP[i]
    if i.startswith("onnx:"):
        return i
    return "llm"


def tau_for(question_id: str) -> float:
    """The confidence the plan found the student matches the live model from; the default tau otherwise."""
    if question_id in LEGACY:
        return tau()
    p = (PLAN["plan"] or {}).get(question_id) or {}
    if p.get("eligible") and p.get("tau") is not None:
        return max(0.5, min(1.0, float(p["tau"])))
    return tau()


def model() -> str:
    return (os.environ.get("VERA_DECIDE_STUDENT_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def instance() -> str:
    """The node the student runs on ('' = routing, GPU preferred)."""
    return (os.environ.get("VERA_DECIDE_STUDENT_INSTANCE") or "").strip()


def tau() -> float:
    try:
        return max(0.5, min(1.0, float(os.environ.get("VERA_DECIDE_STUDENT_TAU") or 0.95)))
    except ValueError:
        return 0.95


#: a first mode that allows exactly one label, whatever the environment says
FIRST_MODES = {"first_met": ["met"], "first_not_met": ["not_met"]}


def is_first(mode_name: str) -> bool:
    return mode_name == "first" or mode_name in FIRST_MODES


def cosign_rate() -> float:
    """The share of a first mode's standing answers the big model decides instead, the student beside it."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("VERA_DECIDE_STUDENT_COSIGN") or 0.1)))
    except ValueError:
        return 0.1


def allowed(question_id: str, mode_name: str = "") -> List[str]:
    if mode_name in FIRST_MODES and question_id == "loop.step_verify":
        return list(FIRST_MODES[mode_name])
    if question_id == "loop.step_verify":
        raw = os.environ.get("VERA_DECIDE_STUDENT_VERIFY_LABELS", "met")
        return [x.strip() for x in raw.split(",") if x.strip()]
    if question_id == "loop.controller_action":
        raw = os.environ.get("VERA_DECIDE_STUDENT_CONTROLLER_LABELS", "continue")
        return [x.strip() for x in raw.split(",") if x.strip()]
    if question_id in FEATURE:          # a planned question: any of its labels, at the plan's threshold
        return list(((JC.QUESTIONS.get(question_id) or {}).get("labels") or {}).values())
    return []


def timeout_s() -> float:
    try:
        return max(2.0, float(os.environ.get("VERA_DECIDE_STUDENT_TIMEOUT_S") or 20))
    except ValueError:
        return 20.0


def mode(question_id: str) -> str:
    """off | shadow | first, from this run's switchboard."""
    fid = FEATURE.get(question_id)
    if not fid:
        return "off"
    try:
        return SB.mode_of(fid)          # the run's modes, published by the loop on SB.RUN_MINDS
    except Exception:
        return "off"


async def ask(question_id: str, system: str, user: str, *, model_name: str = "",
              timeout: Optional[float] = None, inputs: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """{answer, p, confidence, mass, ms, impl, model} or None. The plan's student for the question: the kNN vote
    (inputs: the decision's stored inputs, so the text matches the index), else one LLM token, thinking off."""
    q = JC.QUESTIONS.get(question_id)
    if not q or not user:
        return None
    if question_id not in LEGACY and not model_name:
        await load_plan()
        fam = impl_for(question_id)
        if fam == "knn":
            return await ask_knn(question_id, inputs, user, timeout=timeout)
        if fam != "llm":
            return await ask_fast(question_id, fam, inputs, user, timeout=timeout)
    m = (model_name or model()).strip()
    node = instance()
    meta: Dict[str, Any] = {}
    t0 = time.monotonic()
    try:
        import Vera.vera.capability_orchestration as _orch
        await _orch.ollama_generate(user, system=system, model=m, instance_id=node or None, prefer_gpu=not node,
                                    think=False,
                                    options={"num_predict": 1, "temperature": 0}, logprobs=10, meta_out=meta,
                                    timeout=timeout or timeout_s(), request_stage="student")
    except Exception as e:
        log.debug("student %s: %s", question_id, e)
        return None
    lps = meta.get("logprobs") or []
    probs, mass = JC.label_probs((lps[0] if lps else {}).get("top_logprobs") or [], q["labels"])
    answer, p = JC.verdict(question_id, probs)
    if not answer:
        return None
    return {"answer": answer, "p": p, "confidence": JC.confidence(question_id, answer, p), "mass": round(mass, 4),
            "ms": int((time.monotonic() - t0) * 1000), "impl": f"llm:{m}" + (f"@{node}" if node else ""),
            "model": m}


def decides(question_id: str, result: Optional[Dict[str, Any]], mode_name: str = "") -> bool:
    """Under a first mode: may this answer stand without the big model? A planned question only when the plan
    found its student eligible - otherwise 'first' is a shadow."""
    if question_id not in LEGACY and not ((PLAN["plan"] or {}).get(question_id) or {}).get("eligible"):
        return False
    return bool(result) and JC.takes(question_id, result.get("answer"), result.get("p"), tau_for(question_id),
                                     allowed(question_id, mode_name))


async def ask_fast(question_id: str, fam: str, inputs: Optional[Dict[str, Any]], text: str = "",
                   timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A matrix winner answering live: the label cards ('card'), an NLP-node model ('nlp:<family>') or a trained head
    ('onnx:<slug>') over the decision's text (built from its inputs exactly as the nightly judges saw it). None (the
    big model decides) on any failure. Never raises."""
    t0 = time.monotonic()
    try:
        body = JC.user_message(question_id, inputs) if isinstance(inputs, dict) and inputs else text
        q = JC.QUESTIONS.get(question_id)
        if not body or not q:
            return None
        import sys
        import Vera.vera.capability_orchestration as _orch
        try:
            from Vera.vera.minds import matrix_core as MX
        except Exception:               # worktree / app-free import
            from vera.minds import matrix_core as MX  # type: ignore
        judge = sys.modules.get("judge") or sys.modules.get("Vera.vera.minds.judge")
        if judge is None:
            return None
        budget = timeout or timeout_s()
        if fam.startswith("nlp:"):
            probs = await asyncio.wait_for(judge._nlp_probs(fam[4:], str(body)[:2000], JC.CARDS.get(question_id) or {}),
                                           budget)
            answer, p = MX.vote(q, probs)
            impl = {v: k for k, v in _NLP.items()}.get(fam[4:], fam)
        else:
            emb = (_orch.CAPABILITY_REGISTRY.get("nlp.embed") or {}).get("func")
            e = await asyncio.wait_for(emb(texts=[str(body)[:2000]]), budget) if emb else {}
            vec = ((e or {}).get("embeddings") or [None])[0]
            if not vec:
                return None
            if fam == "card":
                answer, p = JC.card_vote(JC._unit(vec), await judge._card_vecs(question_id))
                impl = JC.CARD_IMPL
            elif fam.startswith("onnx:"):
                cap = _orch.CAPABILITY_REGISTRY.get("ml.onnx.model." + fam[5:]) or {}
                fn = cap.get("raw") or cap.get("func")
                out = await asyncio.wait_for(fn(X=json.dumps([vec])), budget) if fn else {}
                preds = (out or {}).get("predictions") or []
                answer, p = MX.head_vote(q, preds[0]) if preds else (None, None)
                impl = fam
            else:
                return None
        if not answer:
            return None
        return {"answer": answer, "p": p, "confidence": JC.confidence(question_id, answer, p), "mass": 1.0,
                "ms": int((time.monotonic() - t0) * 1000), "impl": impl, "model": impl}
    except Exception as e:
        log.debug("student %s %s: %s", fam, question_id, e)
        return None


async def ask_knn(question_id: str, inputs: Optional[Dict[str, Any]], text: str = "",
                  timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The kNN judge, live: the decision's text (built from its inputs exactly as the nightly index was) embedded
    on an NLP node and voted by its nearest labelled past decisions there. None (the big model decides) without a
    nightly index version, an embedding or enough neighbours. Never raises."""
    t0 = time.monotonic()
    try:
        body = JC.user_message(question_id, inputs) if isinstance(inputs, dict) and inputs else text
        if not body:
            return None
        import sys
        import Vera.vera.capability_orchestration as _orch
        try:
            from Vera.vera.minds import decide_dispatch as DD
        except Exception:               # worktree / app-free import
            from vera.minds import decide_dispatch as DD  # type: ignore
        r = getattr(_orch, "REDIS", None)
        version = await r.get(KNN_VERSION_KEY + question_id) if r is not None else None
        version = version.decode() if isinstance(version, bytes) else version
        emb = (_orch.CAPABILITY_REGISTRY.get("nlp.embed") or {}).get("func")
        if not version or not emb:
            return None
        e = await asyncio.wait_for(emb(texts=[str(body)[:2000]]), timeout or timeout_s())
        vec = ((e or {}).get("embeddings") or [None])[0]
        if not vec:
            return None
        judge = sys.modules.get("judge") or sys.modules.get("Vera.vera.minds.judge")

        async def _rows():
            return await judge.knn_rows(question_id) if judge is not None and hasattr(judge, "knn_rows") else []
        res = await DD.knn(f"decisions:{question_id}", str(version), _rows, vector=JC._unit(vec), k=JC.KNN_K)
        if not res.get("ok"):
            return None
        answer, p, n = JC.vote_from_neighbours(
            question_id, [(x["score"], x.get("payload")) for x in res.get("neighbours") or []],
            int(res.get("eligible") or 0))
        if not answer:
            return None
        return {"answer": answer, "p": p, "confidence": JC.confidence(question_id, answer, p), "mass": 1.0,
                "ms": int((time.monotonic() - t0) * 1000), "impl": JC.KNN_IMPL, "model": "minilm",
                "neighbours": n}
    except Exception as e:
        log.debug("student knn %s: %s", question_id, e)
        return None


def cosigned(rng=random.random) -> bool:
    """Draw for the co-sign sample: True = the big model decides this one, the student beside it."""
    return rng() < cosign_rate()
