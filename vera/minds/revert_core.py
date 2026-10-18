"""FM-11 automatic revert (four-minds §4: "Automatic kills write the mode back with a reason and emit minds.killed";
§12 kill register: rolling precision -3 pts / escalation > 60% / drift -> revert). Pure: no I/O.

plan() decides which decide.student.<q> switches go back to `off`: only a switch whose student is TAKING decisions
(a first* mode, or sampled), and only on a revert trigger - a precision drop of that question's student, escalation
or drift. ECE alone is an alert, not a revert (the plan's kill list does not include it).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

REVERT_KINDS = ("precision_drop", "escalation", "drift")
OFF = "off"


def taking(mode: Any) -> bool:
    m = str(mode or "")
    return m.startswith("first") or m == "sampled"


def switch_of(question_id: str) -> str:
    """decide.student.<q> for a loop question loop.<q>."""
    return "decide.student." + str(question_id).split(".", 1)[-1]


def plan(alert_items: Iterable[Mapping[str, Any]], modes: Mapping[str, Any],
         students: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """[{switch, question, from, to, reasons}] - one per switch to revert. `modes` = the global switch modes
    {switch_id: mode}; `students` = {question: impl} from the cascade plan (a precision drop counts only for the
    question's own student when it is known)."""
    students = dict(students or {})
    out: Dict[str, Dict[str, Any]] = {}
    for a in alert_items or []:
        if a.get("kind") not in REVERT_KINDS:
            continue
        q = str(a.get("question") or "")
        sw = switch_of(q)
        if not q or not taking(modes.get(sw)):
            continue
        if a.get("kind") == "precision_drop" and students.get(q) and a.get("impl") != students[q]:
            continue
        rec = out.setdefault(sw, {"switch": sw, "question": q, "from": modes.get(sw), "to": OFF, "reasons": []})
        rec["reasons"].append(str(a.get("text") or a.get("kind")))
    return list(out.values())


def reason(rec: Mapping[str, Any]) -> str:
    return ("auto-revert (FM-11): " + "; ".join(rec.get("reasons") or []))[:300]
