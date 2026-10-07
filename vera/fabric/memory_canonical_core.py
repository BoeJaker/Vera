"""One canonical record per repeated text: the copies are kept as its occurrences, recall returns it once.

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.fabric.memory_canonical_core`.

Owner, 2026-10-04: similar records are compressed into one canonical record; every original occurrence stays as a
relation to it, so the store stays complete and the graph gains paths; context delivers the canonical record once.
This is the exact-copy slice: measured 2026-10-06 over the records recall can return, 4,206 of 13,824 (30%) were
copies of another record's text - each one a candidate slot a different memory could have had.

  text_key(text)        the key two records share when their text is the same (case and spacing ignored)
  eligible(record)      whether a record takes part: not the kinds recall leaves out, not empty
  plan(rows)            for stored records: {occurrence id: canonical id}, the oldest copy of each text in each
                        record type being the canonical one
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, Tuple

_WS = re.compile(r"\s+")
_TRANSCRIPT = re.compile(r"^\[(?:Claude Code|Codex)\b")
SKIP_TYPES = ("cap_call", "dag_step")


def _get(rec: Any, key: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(key, default)
    return getattr(rec, key, default)


def text_key(text: Any) -> str:
    """sha1 of the text, lower-cased with runs of whitespace as one space; "" for no text."""
    norm = _WS.sub(" ", str(text or "")).strip().lower()
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest() if norm else ""


def eligible(rec: Any, skip_types: Tuple[str, ...] = SKIP_TYPES) -> bool:
    """Tool calls and imported transcript lines are outside recall already; an empty record has nothing to share."""
    if str(_get(rec, "record_type") or "").lower() in skip_types:
        return False
    text = str(_get(rec, "text") or "")
    return bool(text.strip()) and not _TRANSCRIPT.match(text.lstrip())


def plan(rows: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """{occurrence id: canonical id} over `rows` ({id, record_type, text_key, created_at, canonical_id?}).

    Within one record type, the oldest record of each key is canonical (ties to the smaller id, so a rerun gives the
    same answer) and every other record points straight at it - never at another occurrence, so there are no chains
    even where two writes raced and each made itself canonical. Rows already pointing at it are left out."""
    groups: Dict[Tuple[str, str], list] = {}
    for r in rows:
        k = str(r.get("text_key") or "")
        if not k:
            continue
        groups.setdefault((str(r.get("record_type") or ""), k), []).append(r)
    out: Dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (str(m.get("created_at") or ""), str(m["id"])))
        canon = str(members[0]["id"])
        for m in members[1:]:
            if str(m.get("canonical_id") or "") != canon:
                out[str(m["id"])] = canon
    return out


def unmark(rows: Iterable[Dict[str, Any]]) -> list:
    """Ids whose canonical_id should be cleared: the oldest of their group (it is the canonical itself) - e.g. after
    the record it pointed at was archived by fading and left the live set."""
    groups: Dict[Tuple[str, str], list] = {}
    for r in rows:
        k = str(r.get("text_key") or "")
        if k:
            groups.setdefault((str(r.get("record_type") or ""), k), []).append(r)
    out = []
    for members in groups.values():
        members.sort(key=lambda m: (str(m.get("created_at") or ""), str(m["id"])))
        if members[0].get("canonical_id"):
            out.append(str(members[0]["id"]))
    return out
