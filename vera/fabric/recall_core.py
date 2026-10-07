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

import asyncio
import hashlib
import re
import time
from typing import Any, Callable, Dict, List, Tuple

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


# ── What recall leaves out by default (owner, 2026-10-04: tool calls and imported transcript lines "leave default
#    recall and lose their vectors; still stored, still in the activity graph; a deep or 'what did I run' search can
#    still find them"). Measured that day: tool-call records were 74% of memory and 44% of what recall returned,
#    imported transcript lines another 19%. ──
ACTIVITY_TYPES = ("cap_call", "dag_step")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def is_transcript(text: Any) -> bool:
    """A line imported from a coding-agent transcript (said by an agent working on Vera, not to this assistant)."""
    return bool(_TRANSCRIPT_RE.match(str(text or "").lstrip()))


def parse_exclusions(spec: Any) -> "tuple":
    """`cap_call,dag_step,transcript` -> (("cap_call", "dag_step"), True). Unknown or malformed names are dropped, so
    a typo cannot turn into a query fragment; an empty spec excludes nothing."""
    names = [s.strip().lower() for s in str(spec or "").split(",") if s.strip()]
    types = tuple(n for n in names if n != "transcript" and _NAME_RE.match(n))
    return types, ("transcript" in names)


def default_recall_filters(filters: Any, types: "tuple" = ACTIVITY_TYPES, transcripts: bool = True) -> Dict[str, Any]:
    """The filters a recall search runs with. The caller's own filters are kept; on top of them the excluded kinds are
    left out - unless the caller asked for a record type by name, or passed include_activity (the "what did I run"
    search). include_activity is consumed here and never reaches a backend."""
    f: Dict[str, Any] = dict(filters or {})
    wants_all = bool(f.pop("include_activity", False))
    if wants_all or f.get("record_type"):
        return f
    if types:
        f["exclude_record_types"] = list(types)
    if transcripts:
        f["exclude_transcripts"] = True
    return f


def embed_worthy(record_type: Any, text: Any, types: "tuple" = ACTIVITY_TYPES, transcripts: bool = True) -> bool:
    """Whether a record should get a vector at all. Excluded kinds are never searched by meaning, so embedding them
    only queues work on the shared embed nodes - work that chat's own query embeddings then wait behind."""
    if str(record_type or "").lower() in types:
        return False
    if transcripts and is_transcript(text):
        return False
    return True


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


async def fast_then_vector(search: Callable[[float], Any], budget_s: float = 1.5,
                           clock: Callable[[], float] = time.monotonic,
                           info: Any = None) -> Tuple[List[Any], bool]:
    """A search held to one budget: the text tier always, the vector tier only if it lands inside the budget.

    `search(fast_timeout)` is a two-tier search (HybridMemoryStore.search_fast_and_slow) returning
    (fast_hits, slow_task); the slow task resolves to the merged result with vectors. Returns (hits, vector):
    the slow result when it arrived in what is left of the budget, else the fast hits. A slow task that missed
    is cancelled, so nothing is left running for a result no one will read. Never raises.
    `info`, a dict when given, is filled with what happened: fast_ms, fast_hits, error."""
    budget_s = max(0.2, float(budget_s or 1.5))
    note = info if isinstance(info, dict) else {}
    t0 = clock()
    hits: List[Any] = []
    slow = None
    try:
        hits, slow = await search(budget_s)
    except Exception as e:
        note.update(fast_ms=round((clock() - t0) * 1000, 1), fast_hits=0, error=str(e)[:120])
        return [], False
    note.update(fast_ms=round((clock() - t0) * 1000, 1), fast_hits=len(hits or []))
    if slow is None:
        return list(hits or []), False
    vector = False
    left = budget_s - (clock() - t0)
    if left > 0.05:
        try:
            full = await asyncio.wait_for(asyncio.shield(slow), timeout=left)
            if full:
                hits, vector = full, True
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
    if not slow.done():
        slow.cancel()
    return list(hits or []), vector


async def gather_within(coros: Dict[str, Any], timeout: float) -> List[Any]:
    """Run named searches together and keep what finished inside `timeout`, in the order given.

    A search still running at the deadline is cancelled and stands as a TimeoutError; one that failed stands as its
    exception. Unlike wait_for(gather(...)), one slow backend does not throw away the others' hits (the deep memory
    search on prod got 0 hits from a text tier whose two backends each answer in 0.3-0.5 s on their own)."""
    names = list(coros)
    tasks = {n: asyncio.ensure_future(coros[n]) for n in names}
    if not tasks:
        return []
    done, pending = await asyncio.wait(list(tasks.values()), timeout=max(0.0, float(timeout or 0)))
    for t in pending:
        t.cancel()
    out: List[Any] = []
    for n in names:
        t = tasks[n]
        if t in pending:
            out.append(TimeoutError("%s did not answer in %.1fs" % (n, float(timeout or 0))))
        elif t.cancelled():   # an Exception, not CancelledError (a BaseException), so callers' isinstance checks hold
            out.append(TimeoutError("%s was cancelled" % n))
        elif t.exception() is not None:
            out.append(t.exception())
        else:
            out.append(t.result())
    return out


def retitle_block(block: str, heading: str) -> str:
    """A formatted memory block under another heading (its first line replaced); empty stays empty."""
    block = str(block or "")
    if not block.strip() or "\n" not in block:
        return ""
    return heading + block[block.index("\n"):]


RESURFACE_FLOOR = 0.05      # a faded hit's ts_rank when nothing live matched: a clear match, not a stray word
RESURFACE_MAX = 3


def resurface(live: Any, faded: Any, ratio: float = 1.0, floor: float = RESURFACE_FLOOR,
              limit: int = RESURFACE_MAX) -> List[Any]:
    """The faded (soft-archived) hits that come back into recall: those matching at least as strongly as the best live
    hit from the same backend (ratio 1.0), or past `floor` when nothing live matched - at most `limit`, strongest
    first. Owner, 2026-10-05: 'if something is a strong match and obviously useful id like it to be able to surface
    again and not get locked out forever'. `live` and `faded` are [(record, score)] on the same scale (Postgres
    ts_rank); a hit with no usable score never comes back."""
    def _s(p):
        try:
            return float(p[1])
        except (TypeError, ValueError, IndexError):
            return float("nan")
    best = max((x for x in (_s(p) for p in (live or [])) if x == x), default=0.0)
    bar = max(float(floor), best * float(ratio)) if best > 0 else float(floor)
    keep = [p for p in (faded or []) if _s(p) == _s(p) and _s(p) >= bar]
    keep.sort(key=lambda p: -_s(p))
    return keep[:max(0, int(limit))]
