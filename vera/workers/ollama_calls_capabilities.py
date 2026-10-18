"""Reads of the durable per-call record (looplab-control B1+, 19 Oct 2026). Nothing here writes.

The record is written by capability_orchestration._ollama_call_record (every finished generate call, host and node
workers alike) to the Redis stream ollama_calls_core.STREAM_KEY; these capabilities read it back - the calls, and an
hourly rollup - so routing history and per-call speed outlive the in-process request-log ring.
"""
from __future__ import annotations

import time
from typing import Any, Dict

from Vera.vera.capability_orchestration import capability
import Vera.vera.capability_orchestration as _orch

try:
    from Vera.vera.workers import ollama_calls_core as core
except ImportError:                                        # pragma: no cover
    from vera.workers import ollama_calls_core as core     # type: ignore

MAX_SCAN = 50000


async def _scan(limit: int, since: float, **flt) -> list:
    """Newest first, until `limit` rows match or the record is older than `since` (or MAX_SCAN entries are read)."""
    r = _orch.REDIS
    if r is None:
        return []
    out, end, read = [], "+", 0
    while len(out) < limit and read < MAX_SCAN:
        batch = await r.xrevrange(core.STREAM_KEY, max=end, min="-", count=1000)
        if not batch:
            break
        rows = core.parse(batch)
        read += len(batch)
        old = False
        for row in rows:
            t = core._ts(row)
            if t is not None and since and t < since:
                old = True
                break
            if core.matches(row, **flt):
                out.append(row)
                if len(out) >= limit:
                    break
        if old:
            break
        last = batch[-1][0]
        last = last.decode() if isinstance(last, bytes) else str(last)
        ms, _, seq = last.partition("-")
        end = "%s-%d" % (ms, int(seq or 0) - 1) if int(seq or 0) > 0 else "%d" % (int(ms) - 1)
    return out


@capability("ollama.calls", memory="off", silent=True,
            http_method="GET", http_path="/ollama/calls", http_tags=["ollama"],
            description="READ-ONLY. The durable record of model calls (host and every node worker), newest first: each "
                        "finished generate call with its model, node, backend, job type, routing rule and stated reason, "
                        "the router's estimate vs what it took, queue / provider ms, tokens and tokens a second, GPU "
                        "residency and CPU spill, the options it ran with (temperature, top_p, num_ctx, num_predict), "
                        "think, json mode, keep_alive, background, and the writing process's load. Inputs: limit (int=200), "
                        "hours (float=24), model, instance, job_type, session_id (filters). Output: {calls[], count, "
                        "parameters{param: {value: n}}}.")
async def cap_ollama_calls(limit: int = 200, hours: float = 24, model: str = "", instance: str = "", job_type: str = "",
                           session_id: str = "", trace_id=None) -> Dict[str, Any]:
    lim = max(1, min(int(limit or 200), 5000))
    rows = await _scan(lim, time.time() - float(hours or 24) * 3600, model=model, instance=instance, job_type=job_type,
                       session_id=session_id)
    return {"calls": rows, "count": len(rows), "parameters": core.parameters(rows), "source": core.STREAM_KEY}


@capability("ollama.calls.rollup", memory="off", silent=True,
            http_method="GET", http_path="/ollama/calls/rollup", http_tags=["ollama"],
            description="READ-ONLY. The durable call record rolled up by hour x model x node x job type: calls, errors, "
                        "p50 / p95 seconds, mean tokens a second, how far the router's estimate was off, queue p50, CPU "
                        "spills, mean load of the writing process. Inputs: hours (float=24), model, instance, job_type. "
                        "Output: {rows[], calls, since}.")
async def cap_ollama_calls_rollup(hours: float = 24, model: str = "", instance: str = "", job_type: str = "",
                                  trace_id=None) -> Dict[str, Any]:
    since = time.time() - float(hours or 24) * 3600
    rows = await _scan(MAX_SCAN, since, model=model, instance=instance, job_type=job_type)
    return {"rows": core.rollup(rows), "calls": len(rows), "since": since}
