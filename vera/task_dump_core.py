"""Where every asyncio task is waiting - the await chain, not just the outer frame.

4 Oct 2026, 01:44-02:00: a census goal's writer step generated and saved its report, then the
loop sat 16 minutes with no events and the GPU idle until the wall cap. Nothing in the log named
the wait, and py-spy shows thread stacks only - an awaiting coroutine is invisible there.
Task.get_stack() returns one frame for a suspended coroutine; following cr_await from the task's
coroutine gives the whole chain down to the future it is parked on. Pure: hand it tasks.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

MAX_DEPTH = 40


def await_chain(coro: Any, max_depth: int = MAX_DEPTH) -> List[str]:
    """'file:line in func' for each frame from the task's coroutine down its await chain,
    then what the innermost awaits on (a Future, a sleep, ...)."""
    out: List[str] = []
    seen = 0
    cur = coro
    while cur is not None and seen < max_depth:
        seen += 1
        frame = getattr(cur, "cr_frame", None) or getattr(cur, "gi_frame", None) or getattr(cur, "ag_frame", None)
        if frame is not None:
            co = frame.f_code
            out.append("%s:%d in %s" % (co.co_filename, frame.f_lineno, co.co_name))
        nxt = getattr(cur, "cr_await", None)
        if nxt is None:
            nxt = getattr(cur, "gi_yieldfrom", None)
        if nxt is None:
            nxt = getattr(cur, "ag_await", None)
        if nxt is None:
            break
        if not any(hasattr(nxt, a) for a in ("cr_frame", "gi_frame", "ag_frame")):
            out.append("awaiting %s" % (type(nxt).__name__ + (" " + repr(nxt)[:160] if nxt is not None else "")))
            break
        cur = nxt
    return out


def describe(task: Any) -> Dict[str, Any]:
    try:
        coro = task.get_coro()
    except Exception:
        coro = None
    try:
        name = task.get_name()
    except Exception:
        name = ""
    chain = await_chain(coro) if coro is not None else []
    return {"name": name, "coro": getattr(getattr(coro, "cr_code", None), "co_name", "") or type(coro).__name__,
            "done": bool(getattr(task, "done", lambda: False)()), "chain": chain}


def dump(tasks: Iterable[Any], match: str = "", limit: int = 40) -> Dict[str, Any]:
    """Tasks (optionally only those whose chain or name contains `match`), deepest first."""
    rows = [describe(t) for t in tasks]
    total = len(rows)
    if match:
        m = match.lower()
        rows = [r for r in rows if m in r["name"].lower() or any(m in f.lower() for f in r["chain"])]
    rows.sort(key=lambda r: -len(r["chain"]))
    return {"total": total, "matched": len(rows), "tasks": rows[:max(1, int(limit))]}
