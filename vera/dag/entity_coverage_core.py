"""Does the final output cover what the goal named? A deterministic measure
from the NLP nodes' NER (nlp.ner), for reporting - not a success criterion.

User 2026-09-27: make better use of the NER / NLP node capabilities for
planning, step execution and reporting / final output. This is the first,
measuring slice (roadmap B4, .git/vera-work/shared-planning/compute-roles/
ROADMAP.md): the goal's named entities are extracted once, off the critical
path, and the run's final output is checked for each. The result is emitted
as agent_loop_v6.entity_coverage and lands in the trace digest (plan.
entity_coverage) and so in the Loop Lab record, so runs can show whether it tracks the
quality misses (census run1: author-then-edit never applied "90 seconds";
long-horizon left out "delete") before anything steers on it.

Pure: no I/O. The loop calls nlp.ner and hands the result here.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence

#: OntoNotes labels (nlp.ner task=ner) that name something a deliverable about
#: the goal should carry. Numeric labels only count when the text has a digit
#: ("90 seconds", "2024"), never spelled-out ordinals like "one" or "first".
NAMED = {"PERSON", "NORP", "FAC", "ORG", "GPE", "LOC", "PRODUCT", "EVENT",
         "WORK_OF_ART", "LAW", "LANGUAGE"}
NUMERIC = {"DATE", "TIME", "QUANTITY", "MONEY", "PERCENT", "CARDINAL"}
MIN_SCORE = 0.6
MAX_ENTITIES = 12
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")


def _norm(s: Any) -> str:
    return " ".join(_TOKEN.findall(str(s or "").lower().replace("##", "")))


def goal_entities(ner: Any, *, max_entities: int = MAX_ENTITIES) -> List[Dict[str, str]]:
    """The goal's entities worth checking for, from an nlp.ner result: named
    labels, plus numeric ones that contain a digit; score >= MIN_SCORE; de-
    duplicated by normalised text; at most `max_entities`, in goal order."""
    ents = (ner or {}).get("entities") if isinstance(ner, dict) else ner
    out: List[Dict[str, str]] = []
    seen = set()
    for e in ents or []:
        if not isinstance(e, dict):
            continue
        label = str(e.get("entity") or e.get("entity_group") or "").upper().split("-")[-1]
        word = str(e.get("word") or "").strip()
        norm = _norm(word)
        score = e.get("score")
        if not norm or len(norm) < 2 or (isinstance(score, (int, float)) and score < MIN_SCORE):
            continue
        if label in NUMERIC:
            if not re.search(r"\d", norm):
                continue
        elif label not in NAMED:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append({"text": word, "label": label, "norm": norm})
        if len(out) >= max_entities:
            break
    return out


def coverage(entities: Sequence[Dict[str, str]], output: str) -> Dict[str, Any]:
    """Which entities the output mentions (normalised, whole-token match), which
    it never does, and the covered ratio (None when there was nothing to check)."""
    hay = " %s " % _norm(output)
    covered: List[str] = []
    missing: List[str] = []
    for e in entities or []:
        n = str(e.get("norm") or _norm(e.get("text")))
        (covered if n and (" %s " % n) in hay else missing).append(str(e.get("text") or n))
    total = len(covered) + len(missing)
    return {"entities": total, "covered": covered, "missing": missing,
            "ratio": (round(len(covered) / total, 3) if total else None)}
