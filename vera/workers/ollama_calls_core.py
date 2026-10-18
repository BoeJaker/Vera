"""The durable per-call record of model calls (looplab-control B1+, 19 Oct 2026). Pure.

Owner, 18 Oct: "id like model benchmarking to happen on every call" and "need to make sure we are logging every
parameter set for a model - temperature, cores, backend (ollama/vllm/other) the node it ran on other system load at
the time on that node, any of the settings for the node and the model, is thought enabled and so on, exhaustive".

Every generate call - in the host process and in every node worker - writes its finished request-log entry (routing
reason, rule, estimate, timings, tokens, GPU residency, the options it ran with, think, background, the writing
process's load) to the Redis stream `vera:ollama:calls`. These functions read that record: filter it, and roll it up by
hour x model x node x job type so speed and accuracy of the router's estimate can be followed over days, not minutes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

STREAM_KEY = "vera:ollama:calls"


def parse(entries: Iterable[Tuple[Any, Dict[Any, Any]]]) -> List[Dict[str, Any]]:
    """XRANGE/XREVRANGE pairs -> rows (the stream id kept as `_id`); an unreadable entry is skipped."""
    out = []
    for sid, fields in entries or []:
        raw = (fields or {}).get(b"data", (fields or {}).get("data"))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            row["_id"] = sid.decode() if isinstance(sid, bytes) else str(sid)
            out.append(row)
    return out


def _ts(row: Dict[str, Any]) -> Optional[float]:
    v = row.get("ts")
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def matches(row: Dict[str, Any], model: str = "", instance: str = "", job_type: str = "", session_id: str = "",
            since: Optional[float] = None) -> bool:
    if model and model not in str(row.get("model") or ""):
        return False
    if instance and str(row.get("instance") or "") != instance:
        return False
    if job_type and str(row.get("job_type") or "") != job_type:
        return False
    if session_id and str(row.get("session_id") or "") != session_id:
        return False
    if since is not None:
        t = _ts(row)
        if t is None or t < since:
            return False
    return True


def _pct(vals: Sequence[float], q: float) -> Optional[float]:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = max(0, min(len(v) - 1, int(round(q * (len(v) - 1)))))
    return round(v[k], 2)


def rollup(rows: Iterable[Dict[str, Any]], bucket_s: int = 3600) -> List[Dict[str, Any]]:
    """Per (hour, model, node, job type): calls, errors, p50 / p95 seconds, mean tokens a second, how far the
    router's estimate was off (mean |took - est| / est), queue p50, CPU spills and the mean load of the writing
    process. Newest bucket first."""
    groups: Dict[Tuple[int, str, str, str], List[Dict[str, Any]]] = {}
    for r in rows or []:
        t = _ts(r)
        if t is None:
            continue
        b = int(t // bucket_s * bucket_s)
        groups.setdefault((b, str(r.get("model") or ""), str(r.get("instance") or ""), str(r.get("job_type") or "")), []).append(r)
    out = []
    for (b, model, inst, job), rs in groups.items():
        el = [float(r["elapsed_s"]) for r in rs if isinstance(r.get("elapsed_s"), (int, float))]
        tps = [float(r["tok_per_s"]) for r in rs if isinstance(r.get("tok_per_s"), (int, float)) and r["tok_per_s"] > 0]
        est = [abs(float(r["elapsed_s"]) - float(r["est_seconds"])) / float(r["est_seconds"]) for r in rs
               if isinstance(r.get("elapsed_s"), (int, float)) and isinstance(r.get("est_seconds"), (int, float)) and r["est_seconds"] > 0]
        load = [float(r["writer_load1"]) for r in rs if isinstance(r.get("writer_load1"), (int, float))]
        out.append({"bucket": datetime.fromtimestamp(b, tz=timezone.utc).isoformat(timespec="minutes"), "model": model, "instance": inst,
                    "job_type": job, "calls": len(rs), "errors": sum(1 for r in rs if str(r.get("status") or "").startswith(("error", "unfinished"))),
                    "p50_s": _pct(el, 0.5), "p95_s": _pct(el, 0.95), "tps": round(sum(tps) / len(tps), 2) if tps else None,
                    "est_err": round(sum(est) / len(est), 3) if est else None, "queue_p50_ms": _pct([r.get("queue_ms") for r in rs if isinstance(r.get("queue_ms"), (int, float))], 0.5),
                    "spills": sum(1 for r in rs if r.get("cpu_spill")), "load1": round(sum(load) / len(load), 2) if load else None})
    out.sort(key=lambda x: (x["bucket"], x["calls"]), reverse=True)
    return out


def parameters(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """How often each parameter value was used (temperature 0.2: 41 calls ...) - what the calls actually ran with."""
    seen: Dict[str, Dict[str, int]] = {}
    for r in rows or []:
        opts = dict(r.get("options") or {})
        for k in ("think", "json_mode", "keep_alive", "background"):
            if r.get(k) not in (None, ""):
                opts[k] = r.get(k)
        for k, v in opts.items():
            seen.setdefault(k, {})
            seen[k][str(v)] = seen[k].get(str(v), 0) + 1
    return seen
