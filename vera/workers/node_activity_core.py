"""node_activity_core.py - shaping the node-activity records for the Estate pane.

Records come from the node-side taps (edge/ollama_tap.py) through the shared
Redis stream `vera:node_activity`: one per generate/chat/embed call a node's
Ollama served, from ANY caller. This module turns them into what the single
pane shows: who the caller was (prod Vera, a Loop Lab sandbox, or an external
client), per-node summaries, and filtered rows.

Pure: records in, views out. Tested in tests/test_node_activity.py.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

STREAM = "vera:node_activity"
INFLIGHT_PREFIX = "vera:node_activity:inflight:"


def caller_class(rec: Dict[str, Any], vera_ips: Iterable[str] = ()) -> str:
    """prod | sandbox | vera | external, from the X-Vera-Origin a Vera sends.

    `vera` is a call with no origin that came from an address a Vera runs on
    (`vera_ips`): one of ours that did not say which Vera it was - prod and
    the sandboxes share the host's address, so the address alone cannot tell
    them apart. Only a call from anywhere else is `external`."""
    origin = str(rec.get("origin") or "")
    who = origin.split("|", 1)[0]
    if who == "prod":
        return "prod"
    if who.startswith("sandbox:"):
        return "sandbox"
    if who:
        return "other"
    if str(rec.get("caller") or "") in set(vera_ips or ()):
        return "vera"
    return "external"


def origin_parts(rec: Dict[str, Any]) -> Dict[str, str]:
    bits = (str(rec.get("origin") or "").split("|") + ["", "", "", ""])[:4]
    return {"who": bits[0], "job_type": bits[1], "req_id": bits[2], "cap": bits[3]}


def matches(rec: Dict[str, Any], node: str = "", service: str = "", caller: str = "",
            kind: str = "", text: str = "", since: float = 0.0,
            vera_ips: Iterable[str] = ()) -> bool:
    if node and rec.get("node") != node:
        return False
    if service and rec.get("service") != service:
        return False
    if caller and caller_class(rec, vera_ips) != caller:
        return False
    if kind and rec.get("kind") != kind:
        return False
    if since and float(rec.get("end") or rec.get("start") or 0) < since:
        return False
    if text:
        t = text.lower()
        hay = " ".join(str(rec.get(k) or "") for k in
                       ("model", "prompt", "response", "origin", "caller", "path")).lower()
        if t not in hay:
            return False
    return True


def row(rec: Dict[str, Any], preview: int = 240, vera_ips: Iterable[str] = ()) -> Dict[str, Any]:
    """A table row: everything but the full texts, which a click fetches."""
    o = origin_parts(rec)
    return {"id": rec.get("id"), "node": rec.get("node"), "port": rec.get("port"),
            "service": rec.get("service", "ollama"), "kind": rec.get("kind"),
            "model": rec.get("model"), "caller": rec.get("caller"),
            "caller_class": caller_class(rec, vera_ips), "who": o["who"], "job_type": o["job_type"],
            "cap": o["cap"], "start": rec.get("start"), "end": rec.get("end"),
            "duration_s": rec.get("duration_s"), "status": rec.get("status"),
            "eval_count": rec.get("eval_count"), "prompt_eval_count": rec.get("prompt_eval_count"),
            "tps": rec.get("tps"), "vectors": rec.get("vectors"), "error": rec.get("error", ""),
            "prompt_preview": str(rec.get("prompt") or "")[:preview],
            "response_preview": str(rec.get("response") or "")[:preview],
            "prompt_chars": len(str(rec.get("prompt") or "")),
            "response_chars": len(str(rec.get("response") or ""))}


def summarize(records: Iterable[Dict[str, Any]], inflight: Dict[str, List[Dict[str, Any]]],
              window_s: float = 900.0, now: Optional[float] = None,
              vera_ips: Iterable[str] = ()) -> Dict[str, Dict[str, Any]]:
    """Per node over the last `window_s`: calls by kind and by caller class,
    tokens out, mean tok/s of generations, busy seconds, errors, and what is
    running now."""
    now = time.time() if now is None else now
    out: Dict[str, Dict[str, Any]] = {}

    def node(n):
        return out.setdefault(n, {"node": n, "calls": 0, "by_kind": {}, "by_caller": {},
                                  "tokens_out": 0, "busy_s": 0.0, "errors": 0,
                                  "tps": [], "models": {}, "running": []})
    for r in records:
        end = float(r.get("end") or 0)
        if end and end < now - window_s:
            continue
        s = node(r.get("node") or "?")
        s["calls"] += 1
        k = r.get("kind") or "?"
        s["by_kind"][k] = s["by_kind"].get(k, 0) + 1
        c = caller_class(r, vera_ips)
        s["by_caller"][c] = s["by_caller"].get(c, 0) + 1
        s["tokens_out"] += int(r.get("eval_count") or 0)
        s["busy_s"] += float(r.get("duration_s") or 0)
        if r.get("error") or int(r.get("status") or 200) >= 400:
            s["errors"] += 1
        if r.get("tps"):
            s["tps"].append(float(r["tps"]))
        m = r.get("model") or "?"
        s["models"][m] = s["models"].get(m, 0) + 1
    for n, items in (inflight or {}).items():
        s = node(n)
        s["running"] = [dict(i, running_s=round(now - float(i.get("start") or now), 1),
                             caller_class=caller_class(i, vera_ips)) for i in items]
    for s in out.values():
        t = s.pop("tps")
        s["mean_tps"] = round(sum(t) / len(t), 2) if t else None
        s["busy_s"] = round(s["busy_s"], 1)
        s["busy_pct"] = round(100 * min(1.0, s["busy_s"] / window_s), 1)
    return out
