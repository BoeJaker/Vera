"""Decisions on the nodes: the host side of the NLP servers' POST /decide (four-minds FM-9.1).

The worker nodes are the compute pool; the host orchestrates. A System 1 decision that needs real compute (today:
the nearest-neighbour search behind the kNN judge and the tool shortlist) runs on an NLP node, not in this
process. The host keeps the data (Postgres), pushes an index to a node the first time the node lacks it (or holds
an older version), and does only O(k) label maths over the k neighbours it gets back.

Placement is nlp_dispatch's (the same /health discovery every 20 s, the same node scoring and pin, the same
`nlp.local` switch), narrowed to nodes whose /health advertises the method - the handshake: a node on an older
server never receives a call it cannot answer. With no such node, a caller gets {"error": ...} and decides what
to do; it may compute on the host ONLY when `nlp.local` is true (local_allowed()), and then off the event loop.
See shared-planning/four-minds/DISTRIBUTED-COMPUTE.md.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

try:
    from Vera.vera.research import nlp_dispatch as ND
except Exception:                       # worktree / app-free import
    from vera.research import nlp_dispatch as ND  # type: ignore

log = logging.getLogger("vera.minds.decide_dispatch")
SCHEMA = "vera.decide/v1"
TIMEOUT_S = 15.0
PUSH_TIMEOUT_S = 90.0
#: what the last calls did, for decide.nodes (no secrets, no payloads)
STATS: Dict[str, Any] = {"calls": 0, "pushes": 0, "errors": 0, "last": {}}


def index_version(ids: Sequence[Any]) -> str:
    """A version for an index from its row ids (order included): changes whenever a row is added or removed."""
    h = hashlib.sha1(str(len(ids)).encode())
    for i in ids:
        h.update(b"\x00" + str(i).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def capable(servers: Sequence[Dict[str, Any]], method: str, pinned: str = "") -> List[Dict[str, Any]]:
    """The NLP servers whose /health advertises this decide method (and the pinned node, when one is pinned)."""
    out = []
    for s in servers or []:
        d = s.get("decide") or {}
        if d.get("schema") == SCHEMA and method in (d.get("methods") or []):
            if not pinned or str(s.get("node_id")) == pinned:
                out.append(s)
    return out


async def local_allowed() -> bool:
    """May this host compute a decision itself (the degraded path)? Only with nlp.local=true."""
    try:
        return bool((await ND.get_config()).get("nlp_local"))
    except Exception:
        return False


async def knn(index_id: str, version: str, rows_fn: Callable[[], Awaitable[List[Dict[str, Any]]]], *,
              text: str = "", vector: Optional[Sequence[float]] = None, k: int = 15,
              exclude_run: str = "") -> Dict[str, Any]:
    """The k nearest rows of an index, computed on a node: {ok, neighbours: [{id, score, run, payload}],
    eligible, node, ms} or {error}. `rows_fn` gives the rows ({id, vec, run, payload}) to push when the node
    lacks the index or holds another version; `text` is embedded on the node, or pass `vector`."""
    STATS["calls"] += 1
    try:
        cfg = await ND.get_config()
        servers = capable(await ND.discover(), "knn", str(cfg.get("node") or "").strip())
    except Exception as e:
        servers, cfg = [], {}
        log.debug("decide discovery: %s", e)
    if not servers:
        STATS["errors"] += 1
        return {"error": "no NLP node serves /decide knn", "local_allowed": bool(cfg.get("nlp_local"))}
    node, why = ND.pick_nlp_node(servers)
    url = str((node or {}).get("nlp_url") or "")
    body = {"method": "knn", "index_id": index_id, "version": version, "k": int(k), "exclude_run": exclude_run or ""}
    if vector is not None:
        body["vector"] = [float(x) for x in vector]
    else:
        body["text"] = str(text or "")[:2000]
    t0 = time.monotonic()
    res = await ND._post_json(url, "/decide", body, timeout=TIMEOUT_S) or {}
    if isinstance(res, dict) and res.get("error") == "index_missing":
        rows = await rows_fn()
        STATS["pushes"] += 1
        put = await ND._post_json(url, "/decide/index", {"index_id": index_id, "version": version, "rows": rows},
                                  timeout=PUSH_TIMEOUT_S) or {}
        if not isinstance(put, dict) or put.get("error") or not put.get("ok"):
            STATS["errors"] += 1
            return {"error": f"index push to {node.get('node_id')} failed: {str((put or {}).get('error'))[:200]}"}
        res = await ND._post_json(url, "/decide", body, timeout=TIMEOUT_S) or {}
    if not isinstance(res, dict) or res.get("error") or not res.get("ok"):
        STATS["errors"] += 1
        return {"error": str((res or {}).get("error") or "no answer")[:300], "node": node.get("node_id")}
    res["node"] = str(node.get("node_id") or "")
    res["routed"] = why
    res["round_trip_ms"] = int((time.monotonic() - t0) * 1000)
    STATS["last"] = {"index_id": index_id, "node": res["node"], "ms": res.get("ms"),
                     "round_trip_ms": res["round_trip_ms"], "at": time.time()}
    return res
