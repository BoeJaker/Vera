"""Named entities from a step's RESULT into the run journal (roadmap B3, compute-roles 11 Oct 2026).

The journal entry's `entities` come from the journal LLM reading the step's prose summary - a summary
of a summary, which drops the specific things a later step needs (an org, a version's date, a price).
With per-run `journal_ner` on, the node NER (OntoNotes, ~60 ms, CPU) reads the step's own result text
beside the run and the new names are merged into the entry, so the next step's digest shows WHAT was
found. Pure: result in, text out; NER result in, entity strings out.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

#: OntoNotes types worth carrying forward (CARDINAL / ORDINAL / QUANTITY are too noisy alone).
KEEP = ("ORG", "PRODUCT", "DATE", "MONEY", "PERCENT", "GPE", "PERSON", "EVENT", "LAW",
        "WORK_OF_ART", "FAC", "NORP", "TIME")
TEXT_CHARS = 4000
MAX_NEW = 10
MIN_SCORE = 0.6
ENTITIES_CAP = 22        # the entry's list after the merge (the digest prints the first 8)


def text_of(res: Dict[str, Any]) -> str:
    """The step's own result text: its summary, then its output values, bounded."""
    parts = [str(res.get("summary") or res.get("raw_summary") or "")]
    for k, v in list((res.get("outputs") or {}).items())[:8]:
        parts.append("%s: %s" % (k, str(v)[:800]))
    return "\n".join(p for p in parts if p.strip())[:TEXT_CHARS]


def pick(ner: Any, existing: Sequence[str], max_new: int = MAX_NEW) -> List[str]:
    """New "name (TYPE)" strings from an nlp.ner result, kept types only, score >= MIN_SCORE,
    not already in `existing` (case-insensitive, by name), in order of appearance."""
    if not isinstance(ner, dict) or ner.get("error"):
        return []
    have = {str(e).split(" (")[0].strip().lower() for e in existing or []}
    out: List[str] = []
    for e in ner.get("entities") or []:
        if not isinstance(e, dict):
            continue
        label = re.sub(r"^[BI]-", "", str(e.get("entity") or "").upper())
        name = " ".join(str(e.get("word") or "").split()).strip(" ,;:")
        try:
            score = float(e.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if label not in KEEP or score < MIN_SCORE or len(name) < 2 or name.lower() in have:
            continue
        have.add(name.lower())
        out.append("%s (%s)" % (name[:60], label))
        if len(out) >= max_new:
            break
    return out


def merge(entry: Dict[str, Any], new: Sequence[str]) -> int:
    """Append `new` to entry["entities"] in place (bounded); the number added."""
    ents = list(entry.get("entities") or [])
    room = max(0, ENTITIES_CAP - len(ents))
    add = list(new)[:room]
    if add:
        entry["entities"] = ents + add
    return len(add)
