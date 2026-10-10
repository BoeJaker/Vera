"""A pre-trained student answering a live decision in-band (four-minds FM-2.5, the System 1 cascade).

The student gets exactly the prompt the big model gets (for loop.step_verify: the live verifier's evidence and
rules, asked for one word) and answers with one token; the label probabilities come from that token's logprobs
(see judge_core). The loop decides what to do with the answer, per the switchboard
(`decide.student.step_verify`, `decide.student.controller_action`):

- shadow: the student runs beside the big model and is recorded as a shadow of the live decision;
- first: the student answers first; its answer stands when it is an allowed label at confidence >= tau,
  otherwise the big model decides as before (and the student is recorded as a shadow).

Settings: VERA_DECIDE_STUDENT_MODEL (default the shadow judge's first model, Qwen3-4B-Instruct),
VERA_DECIDE_STUDENT_TAU (default 0.95), VERA_DECIDE_STUDENT_VERIFY_LABELS (default "met": a "not met" verdict
needs the big model's reason, which the retry that follows is built from), VERA_DECIDE_STUDENT_CONTROLLER_LABELS
(default "continue": the others need the big model's new steps; a student's continue carries no steer for the
next step), VERA_DECIDE_STUDENT_TIMEOUT_S (default 20). Never raises: any failure is None and the big model decides.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

try:
    from Vera.vera.minds import judge_core as JC, switchboard_core as SB
except Exception:                       # worktree / app-free import
    from vera.minds import judge_core as JC, switchboard_core as SB  # type: ignore

log = logging.getLogger("vera.minds.student")
DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
FEATURE = {"loop.step_verify": "decide.student.step_verify",
           "loop.controller_action": "decide.student.controller_action"}


def model() -> str:
    return (os.environ.get("VERA_DECIDE_STUDENT_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def tau() -> float:
    try:
        return max(0.5, min(1.0, float(os.environ.get("VERA_DECIDE_STUDENT_TAU") or 0.95)))
    except ValueError:
        return 0.95


def allowed(question_id: str) -> List[str]:
    if question_id == "loop.step_verify":
        raw = os.environ.get("VERA_DECIDE_STUDENT_VERIFY_LABELS", "met")
        return [x.strip() for x in raw.split(",") if x.strip()]
    if question_id == "loop.controller_action":
        raw = os.environ.get("VERA_DECIDE_STUDENT_CONTROLLER_LABELS", "continue")
        return [x.strip() for x in raw.split(",") if x.strip()]
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
              timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """{answer, p, confidence, mass, ms, impl, model} or None. One token, thinking off, interactive priority."""
    q = JC.QUESTIONS.get(question_id)
    if not q or not user:
        return None
    m = (model_name or model()).strip()
    meta: Dict[str, Any] = {}
    t0 = time.monotonic()
    try:
        import Vera.vera.capability_orchestration as _orch
        await _orch.ollama_generate(user, system=system, model=m, prefer_gpu=True, think=False,
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
            "ms": int((time.monotonic() - t0) * 1000), "impl": f"llm:{m}", "model": m}


def decides(question_id: str, result: Optional[Dict[str, Any]]) -> bool:
    """Under `first`: may this answer stand without the big model?"""
    return bool(result) and JC.takes(question_id, result.get("answer"), result.get("p"), tau(), allowed(question_id))
