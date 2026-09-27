"""A zero-shot guess at a goal's INTENT, from the NLP nodes, recorded beside the
intent the loop actually used - measurement only (roadmap B1).

v7 decides intent (build / research / action / mixed) with a keyword heuristic
and, when that says 'mixed', an LLM pass on the loop's planner route. nlp.
zeroshot on an NLP node answers the same question in ~100 ms with no GPU. This
records what it would have said, so censuses can show whether it agrees with
the heuristic, with the LLM, and with what the run needed - before anything is
allowed to steer on it (user, 2026-09-27: use the NLP node capabilities for
planning). Nothing reads the guess back.

Pure: labels in, comparison out. The loop calls nlp.zeroshot.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Zero-shot models score DESCRIPTIONS far better than bare category words; each
#: says what the loop's intent means (see _v7_classify_intent's system prompt).
#: 'mixed' is NOT a candidate: offered as "look up, then build" it absorbed 10
#: of the 12 census goals (measured 2026-09-27, DeBERTa-v3 mnli on cpu-246).
#: The three are scored INDEPENDENTLY (multi_label) and mixed is read off them.
LABELS: Dict[str, str] = {
    "build": "creating or writing something from existing knowledge, such as an app, a script or a document",
    "research": "looking up external, current or factual information",
    "action": "running, deploying, configuring or fixing a live system",
}
MULTI_LABEL = True
#: build AND research both at least this -> mixed (the research goals scored
#: research 0.97-1.0 with build 0.63-0.76: gather, then write it up).
MIXED_BOTH = 0.5
_BY_TEXT = {v: k for k, v in LABELS.items()}


def label_texts() -> List[str]:
    return list(LABELS.values())


def parse(res: Any) -> Tuple[Optional[str], Dict[str, float]]:
    """(intent, {intent: score}) from an nlp.zeroshot result; (None, {}) when
    it failed or answered with labels this module did not ask for. mixed when
    build and research both reach MIXED_BOTH, else the top-scoring label."""
    if not isinstance(res, dict) or res.get("error"):
        return None, {}
    scores: Dict[str, float] = {}
    for item in res.get("labels") or []:
        if not isinstance(item, dict):
            continue
        key = _BY_TEXT.get(str(item.get("label") or ""))
        sc = item.get("score")
        if key and isinstance(sc, (int, float)):
            scores[key] = round(float(sc), 3)
    if not scores:
        return None, {}
    if scores.get("build", 0) >= MIXED_BOTH and scores.get("research", 0) >= MIXED_BOTH:
        return "mixed", scores
    return max(scores, key=scores.get), scores


def compare(zeroshot: Optional[str], scores: Dict[str, float], *, used: str,
            heuristic: str = "", llm: str = "") -> Dict[str, Any]:
    """What the record holds: the zero-shot guess and its margin, the intents
    the run had, and whether they agree."""
    ranked = sorted(scores.values(), reverse=True)
    margin = round(ranked[0] - ranked[1], 3) if len(ranked) > 1 else None
    return {"zeroshot": zeroshot, "scores": scores, "margin": margin,
            "confidence": ranked[0] if ranked else None,
            "used": used, "heuristic": heuristic, "llm": llm,
            "agrees_used": (zeroshot == used) if zeroshot else None,
            "agrees_llm": (zeroshot == llm) if (zeroshot and llm) else None}
