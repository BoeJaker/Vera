"""How recalled memory is presented: who it came from, and one hit per repeated text.

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.fabric.recall_core`.

Two defects this answers, both measured on prod on 2026-10-04:

  * ROLE. The formatters that inject recalled memory into a prompt labelled a record "User" unless its
    `human_text` flag was false. That flag defaults to True on MemoryRecord, so every record that never set it
    - capability calls, dream reports, events - reached the model as something the user had said. The first two
    lines of a live "Relevant memories" block were board and pipeline calls marked "User:". The role is read from
    what the record IS (record_type, source_type), and `human_text` only decides between the two chat roles.

  * REPEATS. One search returned the same transcript line twice, twice over ("Watching for the final gate." x2),
    so four of twelve slots carried two facts. Hits are collapsed by their text: the best-scored copy stays, with
    how many copies it stood for.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List

# What a non-message record is called when it is shown to a model. A record whose type is not here is named by
# its type ("skill_output" -> "Skill output"), never "User".
_TYPE_LABEL = {
    "cap_call": "Tool call",
    "dag_step": "Tool call",
    "dag": "Workflow",
    "cap_result": "Tool result",
    "cap_output": "Tool result",
    "dream": "Dream",
    "dream_report": "Dream",
    "event": "Event",
    "observation": "Observation",
    "note": "Note",
    "fact": "Fact",
    "preference": "Preference",
    "summary": "Summary",
    "entity": "Entity",
    "relationship": "Relation",
    "plan": "Plan",
    "code": "Code",
    "error": "Error",
    "feedback": "Feedback",
    "extraction_result": "Extraction",
}

# Lines imported from coding-agent transcripts (ide.claude_sessions) are stored as messages but were said by an
# agent working on Vera, not by the user to this assistant.
_TRANSCRIPT_RE = re.compile(r"^\[(?:Claude Code|Codex)\b")

_WS_RE = re.compile(r"\s+")


def _get(rec: Any, key: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(key, default)
    return getattr(rec, key, default)


def role_label(rec: Any) -> str:
    """The speaker or kind to show for a recalled record (a dict or a MemoryRecord)."""
    rtype = str(_get(rec, "record_type") or "").strip().lower()
    if rtype and rtype != "message":
        return _TYPE_LABEL.get(rtype) or rtype.replace("_", " ").replace(".", " ").strip().capitalize()
    text = str(_get(rec, "text") or "").lstrip()
    if _TRANSCRIPT_RE.match(text):
        return "Agent transcript"
    source = str(_get(rec, "source_type") or "").strip().lower()
    if source == "ai" or (_get(rec, "ai_output") is True and source != "human"):
        return "Assistant"
    if source in ("tool", "system", "sensor", "document"):
        return {"tool": "Tool", "system": "System", "sensor": "Sensor", "document": "Document"}[source]
    # A plain message: the two chat roles, decided by the authorship flag.
    return "User" if _get(rec, "human_text", True) is not False else "Assistant"


def content_key(rec: Any) -> str:
    """A key equal for two records carrying the same text (case and spacing ignored).

    The stored content hash is not used: it covers more than the text, so two copies of one sentence written by two
    turns hash differently. Records with no text key by id, so they never collapse into each other.
    """
    text = _get(rec, "text") or _get(rec, "full_text") or ""
    norm = _WS_RE.sub(" ", str(text)).strip().lower()
    if not norm:
        return "id:" + str(_get(rec, "id") or "")
    return "t:" + hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


def collapse_repeats(hits: List[Dict[str, Any]], limit: int = 0) -> List[Dict[str, Any]]:
    """One hit per distinct text, in the order given (callers pass best-first).

    `hits` are search results shaped {"record": {...}, "score": float, ...}. The first hit of each text is kept and
    gains "repeats" (how many hits it stands for, itself included) and, when more than one, "repeat_ids" (the
    others' ids) so nothing found is lost from view. With `limit` > 0 at most that many distinct hits are returned.
    """
    out: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for hit in hits or []:
        rec = hit.get("record") if isinstance(hit, dict) else None
        if rec is None:
            rec = hit
        key = content_key(rec)
        # A hit that was already collapsed upstream brings its own count, so counts add up across passes.
        count = int(hit.get("repeats", 1) or 1) if isinstance(hit, dict) else 1
        first = by_key.get(key)
        if first is not None:
            first["repeats"] = int(first.get("repeats", 1)) + count
            rid = _get(rec, "id")
            ids = first.setdefault("repeat_ids", [])
            if rid:
                ids.append(str(rid))
            if isinstance(hit, dict):
                ids.extend(str(x) for x in (hit.get("repeat_ids") or []))
            continue
        kept = dict(hit) if isinstance(hit, dict) else {"record": hit}
        kept["repeats"] = count
        if isinstance(hit, dict) and hit.get("repeat_ids"):
            kept["repeat_ids"] = list(hit["repeat_ids"])
        by_key[key] = kept
        out.append(kept)
    if limit and limit > 0:
        return out[:int(limit)]
    return out
