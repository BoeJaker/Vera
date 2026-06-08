"""
memory_second_order.py  —  Second-order context retrieval for Vera memory
=========================================================================

`memory.recall` returns vector-similar memories — useful for "find documents
about X". This module adds a different shape of recall:

    "When the user has asked something LIKE this before, what answer did
     the system give, and what other knowledge was attached to that
     answer?"

Pipeline
────────
    user_query
      → (1) vector search restricted to USER turns
    similar_questions  [{q, score, session, ts, id}]
      → (2) for each question: find the paired ASSISTANT answer
    paired_answers     [{q, a, score, ...}]
      → (3) for each answer: pull graph neighbours (1-2 hops)
    related_knowledge  [{neighbour_text, relation, kind}]

The resulting context is small, structured, and immediately usable as:
  • injectable system-prompt context (rendered as Markdown by
    context.related_qa_block)
  • a list/graph in the chat right-rail Full-Ctx tab
  • a programmatic API for third-party plugins

Capabilities registered
───────────────────────
  memory.find_similar_questions  — step 1 only
  memory.recall_2nd_order        — full pipeline
  context.related_qa_block       — render the result as a system-prompt
                                    fragment with a stable, parseable shape

The `context.assemble` capability (in context.py) calls
`context.related_qa_block` directly when given `attach_related_qa='auto'`
or a numeric limit.

Pairing strategy
────────────────
"Paired answer" is resolved in this order:
  1. assistant memory record whose parent_id == user_record.id
  2. graph traversal from user_record.id at depth 1, taking the first
     neighbour that looks like an assistant turn
  3. nearest-following assistant record in the same session

Excluding the current session
─────────────────────────────
By default, callers pass `exclude_session=session_id` so the user doesn't
see echoes of the conversation they're currently in.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import capability

log = logging.getLogger("vera.memory.2nd_order")


# ─────────────────────────────────────────────────────────────────────────────
# MEMORY accessor — lazy so we tolerate any import order
# ─────────────────────────────────────────────────────────────────────────────
def _memory():
    """Return the live MEMORY singleton from the memory module, or None."""
    import sys as _sys
    mod = (_sys.modules.get("memory")
            or _sys.modules.get("memory"))
    if not mod:
        return None
    return getattr(mod, "MEMORY", None)


def _record_dict(rec_or_wrapper: Any) -> Optional[Dict[str, Any]]:
    """Search results may come back as either MemoryRecord, dict, or
    {record: ..., score: ...} wrappers. Normalise to a plain dict."""
    if rec_or_wrapper is None:
        return None
    if isinstance(rec_or_wrapper, dict):
        if "record" in rec_or_wrapper and isinstance(rec_or_wrapper["record"], (dict, object)):
            inner = rec_or_wrapper["record"]
            if isinstance(inner, dict):
                return inner
            return _record_to_dict(inner)
        return rec_or_wrapper
    return _record_to_dict(rec_or_wrapper)


def _record_to_dict(rec: Any) -> Dict[str, Any]:
    if hasattr(rec, "to_dict"):
        try:
            return rec.to_dict()
        except Exception:
            pass
    out: Dict[str, Any] = {}
    for attr in ("id","session_id","created_at","text","summary",
                  "human_text","ai_output","source_type","record_type",
                  "category","tags","parent_id"):
        if hasattr(rec, attr):
            try:
                out[attr] = getattr(rec, attr)
            except Exception:
                pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pairing — find the assistant record that answered a given user record
# ─────────────────────────────────────────────────────────────────────────────
async def _find_paired_answer(MEM, user_rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rec_id     = user_rec.get("id") or ""
    session_id = user_rec.get("session_id") or ""
    created    = user_rec.get("created_at") or ""

    # Strategy 1 — parent_id link
    if rec_id:
        try:
            results = await MEM.search(
                "", limit=5,
                filters={"parent_id": rec_id, "source_type": "agent"},
            )
            for r in results:
                d = _record_dict(r)
                if d:
                    return d
        except Exception as e:
            log.debug("paired by parent_id failed: %s", e)

    # Strategy 2 — graph traversal
    if rec_id and hasattr(MEM, "traverse"):
        try:
            hops = await MEM.traverse(rec_id, depth=1, limit=8)
            for h in hops or []:
                node = h.get("node") if isinstance(h, dict) else None
                if not isinstance(node, dict):
                    continue
                if (node.get("source_type") == "agent"
                        or node.get("ai_output") is True):
                    return node
        except Exception as e:
            log.debug("paired by traverse failed: %s", e)

    # Strategy 3 — nearest-following assistant record in session
    if session_id and created:
        try:
            results = await MEM.search(
                "", limit=30,
                filters={"session_id": session_id, "source_type": "agent"},
            )
            best: Optional[Dict[str, Any]] = None
            for r in results:
                d = _record_dict(r)
                if not d:
                    continue
                ts = d.get("created_at") or ""
                if not ts or ts <= created:
                    continue
                if best is None or ts < (best.get("created_at") or "9999"):
                    best = d
            if best:
                return best
        except Exception as e:
            log.debug("paired by time fallback failed: %s", e)

    return None


async def _neighbours_of(MEM, record_id: str, depth: int, limit: int) -> List[Dict[str, Any]]:
    if not record_id or not hasattr(MEM, "traverse"):
        return []
    try:
        hops = await MEM.traverse(record_id, depth=depth, limit=limit)
    except Exception as e:
        log.debug("neighbours_of(%s) failed: %s", record_id, e)
        return []
    out: List[Dict[str, Any]] = []
    for h in hops or []:
        if not isinstance(h, dict):
            continue
        node = h.get("node") or {}
        out.append({
            "id":       node.get("id") or h.get("to_id", ""),
            "text":     (node.get("text", "") or "")[:240],
            "kind":     node.get("record_type") or node.get("category") or "",
            "relation": h.get("relation", ""),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "memory.find_similar_questions", memory="off",
    http_method="POST", http_path="/memory/similar_questions",
    http_tags=["memory"],
    description=(
        "Vector-search past USER turns only. Building block for second-order "
        "recall. Inputs: query (str!), limit (int 5), session_id (str — "
        "restrict), min_score (float 0.0). "
        "Output: {questions: [{q, id, score, session_id, ts}], count}."
    ),
)
async def memory_find_similar_questions(
    query:      str,
    limit:      int   = 5,
    session_id: str   = "",
    min_score:  float = 0.0,
    trace_id=None,
):
    MEM = _memory()
    if not MEM:
        return {"error": "memory module not loaded", "questions": []}
    if not (query or "").strip():
        return {"error": "query is empty", "questions": []}

    filters: Dict[str, Any] = {"source_type": "human"}
    if session_id:
        filters["session_id"] = session_id

    try:
        results = await MEM.search(query, limit=int(limit) * 2, filters=filters)
    except Exception as e:
        return {"error": f"search failed: {e}", "questions": []}

    out: List[Dict[str, Any]] = []
    for r in results:
        rec   = _record_dict(r) or {}
        score = (r.get("score", 0.0) if isinstance(r, dict) else 0.0)
        if score < float(min_score or 0):
            continue
        out.append({
            "id":         rec.get("id", ""),
            "q":          (rec.get("text") or "")[:600],
            "score":      score,
            "session_id": rec.get("session_id", ""),
            "ts":         rec.get("created_at", ""),
        })
        if len(out) >= int(limit):
            break

    return {"questions": out, "count": len(out)}


@capability(
    "memory.recall_2nd_order", memory="off",
    http_method="POST", http_path="/memory/recall/2nd_order",
    http_tags=["memory"],
    description=(
        "Second-order recall: similar past USER questions → paired ASSISTANT "
        "answers → graph-neighbour knowledge attached to those answers. "
        "Inputs: query (str!), limit (int 5), graph_depth (int 1), "
        "neighbours_per (int 3), session_id (str — restrict source pool), "
        "exclude_session (str — drop hits from this session), min_score "
        "(float 0.0). "
        "Output: {pairs: [{q,a,score,session_id,ts,neighbours:[{text,relation,kind}]}], "
        "related_knowledge: [{text,relation,kind}], count, neighbour_count}."
    ),
)
async def memory_recall_2nd_order(
    query:           str,
    limit:           int   = 5,
    graph_depth:     int   = 1,
    neighbours_per:  int   = 3,
    session_id:      str   = "",
    exclude_session: str   = "",
    min_score:       float = 0.0,
    trace_id=None,
):
    MEM = _memory()
    if not MEM:
        return {"error": "memory module not loaded", "pairs": []}
    if not (query or "").strip():
        return {"error": "query is empty", "pairs": []}

    qres = await memory_find_similar_questions(
        query=query, limit=int(limit) * 2,
        session_id=session_id, min_score=min_score,
    )
    questions = qres.get("questions", [])
    if exclude_session:
        questions = [q for q in questions if q.get("session_id") != exclude_session]

    pairs: List[Dict[str, Any]] = []
    seen_neighbour_ids: set = set()
    related_knowledge: List[Dict[str, Any]] = []

    for q in questions:
        user_rec = {
            "id":         q.get("id"),
            "session_id": q.get("session_id"),
            "created_at": q.get("ts"),
        }
        ans = await _find_paired_answer(MEM, user_rec)
        if not ans:
            continue
        ans_text = (ans.get("text") or ans.get("summary") or "")[:1000]
        if not ans_text.strip():
            continue

        nbrs = await _neighbours_of(
            MEM, ans.get("id", ""),
            depth=int(graph_depth), limit=int(neighbours_per),
        )
        unique_nbrs: List[Dict[str, Any]] = []
        for n in nbrs:
            nid = n.get("id") or n.get("text", "")[:80]
            if nid in seen_neighbour_ids:
                continue
            seen_neighbour_ids.add(nid)
            unique_nbrs.append(n)
            related_knowledge.append(n)

        pairs.append({
            "q":          q.get("q", ""),
            "a":          ans_text,
            "score":      q.get("score", 0.0),
            "session_id": q.get("session_id", ""),
            "ts":         q.get("ts", ""),
            "neighbours": unique_nbrs,
        })

        if len(pairs) >= int(limit):
            break

    return {
        "pairs":             pairs,
        "related_knowledge": related_knowledge,
        "count":             len(pairs),
        "neighbour_count":   len(related_knowledge),
    }


@capability(
    "context.related_qa_block", memory="off", silent=True,
    http_method="POST", http_path="/context/related_qa_block",
    http_tags=["context", "memory"],
    description=(
        "Run memory.recall_2nd_order and render the result as a Markdown "
        "system-prompt fragment for direct LLM injection. Returns block='' "
        "when no related Q&A is found. "
        "Inputs: query (str!), limit (int 4), graph_depth (int 1), "
        "neighbours_per (int 2), session_id (str), exclude_session (str), "
        "min_score (float 0.4), max_chars (int 2400). "
        "Output: {block, pairs, char_count}."
    ),
)
async def context_related_qa_block(
    query:           str,
    limit:           int   = 4,
    graph_depth:     int   = 1,
    neighbours_per:  int   = 2,
    session_id:      str   = "",
    exclude_session: str   = "",
    min_score:       float = 0.4,
    max_chars:       int   = 2400,
    trace_id=None,
):
    out = await memory_recall_2nd_order(
        query=query, limit=int(limit), graph_depth=int(graph_depth),
        neighbours_per=int(neighbours_per), session_id=session_id,
        exclude_session=exclude_session, min_score=float(min_score),
    )
    pairs = out.get("pairs", [])
    if not pairs:
        return {"block": "", "pairs": [], "char_count": 0}

    lines: List[str] = [
        "## Related past Q&A (from memory)",
        "_The user has asked similar questions before. These are the prior "
        "answers and any knowledge attached to them. Use them only if "
        "actually relevant to the current question._",
    ]
    used = sum(len(s) for s in lines) + len(lines)
    cap_chars = int(max_chars)

    for p in pairs:
        block_lines = [
            "",
            f"### Prior question (similarity {p.get('score', 0):.2f})",
            f"> {(p.get('q') or '').strip()[:400]}",
            f"**Then-answered:** {(p.get('a') or '').strip()[:600]}",
        ]
        nbrs = p.get("neighbours") or []
        if nbrs:
            block_lines.append("**Linked knowledge:**")
            for n in nbrs:
                rel = (n.get("relation") or "RELATED").upper()
                txt = (n.get("text") or "").strip()[:240]
                if txt:
                    block_lines.append(f"  - [{rel}] {txt}")
        chunk = "\n".join(block_lines)
        if used + len(chunk) > cap_chars:
            lines.append("")
            lines.append("_(more matches truncated for length)_")
            break
        lines.append(chunk)
        used += len(chunk)

    block = "\n".join(lines)
    return {
        "block":      block,
        "pairs":      pairs,
        "char_count": len(block),
    }


log.info("memory_second_order: registered (memory.find_similar_questions, "
          "memory.recall_2nd_order, context.related_qa_block)")