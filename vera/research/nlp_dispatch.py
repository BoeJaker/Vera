"""
nlp_dispatch.py  —  finding a node that will run NLP, and the switch that says
                    whether this host is allowed to instead
==============================================================================

The runtime half of off-host NLP. The decisions themselves are pure and live in
`nlp_dispatch_core.py`; this file supplies them with live facts (which nodes
answer, what their agents report) and carries the result over HTTP.

  nlp.config.get / nlp.config.set   the `nlp.local` switch, runtime-mutable
  nlp.nodes                         which nodes serve NLP, and which would win

WHY NOT AN ENV VAR
──────────────────
`nlp.local` is an operational control — it is flipped while diagnosing, and the
answer to "is the host running NLP right now?" must be changeable without
restarting Vera. It is stored in Redis and read per call.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

# ⚠ ABSOLUTE, never relative. This file is a loader entry point in
# `_module_files`, and the loader imports it BY PATH — so it has no parent
# package and `from .nlp_dispatch_core import …` raises, which silently
# unregisters every capability in the module. That exact mistake has already
# cost this repo the operator once (see "a loader entry point has no parent
# package"). The plain-`vera.` fallback is what lets tests import this without
# the app on sys.path.
try:
    from Vera.vera.research.nlp_dispatch_core import (
        FAIL, LOCAL, REMOTE, normalize_node_inventory, pick_nlp_node, resolve_placement,
    )
except ImportError:  # pragma: no cover - test/standalone import path
    from vera.research.nlp_dispatch_core import (
        FAIL, LOCAL, REMOTE, normalize_node_inventory, pick_nlp_node, resolve_placement,
    )

log = logging.getLogger("vera.nlp.dispatch")

try:
    from Vera.vera import capability_orchestration as _orch
    from Vera.vera.capability_orchestration import capability
    _CAP_AVAILABLE = True
except ImportError:  # pragma: no cover - importable without the app
    _orch = None
    _CAP_AVAILABLE = False

#: Port the edge NLP server listens on. NOT 8770 — the node agent owns that on
#: every node, and the `onnx_runtime` component already collides with it.
NLP_PORT = int(os.getenv("VERA_NLP_PORT", "8771") or 8771)

#: Redis key holding the runtime config for this subsystem.
KEY_NLP_CONFIG = "vera:nlp:config"

#: The host is NOT allowed to run NLP unless this is turned on.
DEFAULT_CONFIG: Dict[str, Any] = {
    "nlp_local": False,
    "node": "",          # pin to one node id; "" = choose by signals
    "timeout_s": 120.0,
}

#: Discovery is cached briefly: an NLP call must not cost three node probes,
#: but a node that has just come up should be usable within a few seconds.
_DISCOVERY_TTL_S = 20.0
_discovery_cache: Dict[str, Any] = {"at": 0.0, "nodes": []}


def _redis():
    return getattr(_orch, "REDIS", None) if _orch else None


async def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_NLP_CONFIG)
            if raw:
                cfg.update(json.loads(
                    raw.decode() if isinstance(raw, bytes) else raw))
        except Exception as e:
            log.debug("nlp config read failed, using defaults: %s", e)
    return cfg


async def set_config(**fields) -> Dict[str, Any]:
    cfg = await get_config()
    for k, v in fields.items():
        if v is not None and k in DEFAULT_CONFIG:
            cfg[k] = v
    r = _redis()
    if r:
        await r.set(KEY_NLP_CONFIG, json.dumps(cfg))
    return cfg


def _nodes() -> Dict[str, Dict]:
    return dict(getattr(_orch, "OLLAMA_INSTANCES", {}) or {}) if _orch else {}


def _nlp_url(inst: Dict) -> str:
    """The NLP server's address, derived from the node's ollama URL so a node
    stays configured in exactly one place — the same trick
    node_agent_capabilities._agent_url uses for the agent on 8770."""
    url = str(inst.get("url") or "")
    if not url:
        return ""
    try:
        host = url.split("//", 1)[1].split(":", 1)[0].split("/", 1)[0]
    except Exception:
        return ""
    return f"http://{host}:{NLP_PORT}"


async def _get_json(url: str, path: str, timeout: float = 5.0) -> Optional[Dict]:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{url}{path}")
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.debug("nlp GET %s%s: %s", url, path, e)
        return None


def _origin(path: str) -> Dict[str, str]:
    """X-Vera-Origin for an NLP call (the node's activity recorder files it
    under this Vera instead of 'external'). Empty outside the app."""
    try:
        from Vera.vera.capability_orchestration import vera_origin_header
    except Exception:                                    # pragma: no cover - tests
        return {}
    return vera_origin_header(job_type="nlp", cap="nlp" + str(path or "").replace("/", "."))


async def _post_json(url: str, path: str, body: Dict,
                     timeout: float) -> Dict[str, Any]:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{url}{path}", json=body, headers=_origin(path))
            try:
                payload = r.json()
            except Exception:
                payload = {"error": r.text[:300]}
            if r.status_code != 200:
                return {"error": f"nlp server {r.status_code}",
                        **(payload or {})}
            return payload
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def discover(force: bool = False) -> List[Dict[str, Any]]:
    """Nodes whose NLP server answers /health, enriched with the agent facts
    the router scores on (runners, memory, GPU).

    A node that does not answer is simply not a candidate. It is not an error:
    the estate is expected to have nodes without the component deployed.
    """
    now = time.monotonic()
    if not force and (now - float(_discovery_cache["at"])) < _DISCOVERY_TTL_S:
        return list(_discovery_cache["nodes"])

    # Agent facts, keyed by node id, for scoring. Best-effort: a node whose
    # agent is down can still serve NLP, it just scores on less information.
    agent_facts: Dict[str, Dict] = {}
    try:
        from Vera.vera.workers import node_agent_capabilities as _na
        status = await _na.cap_nodes_agent_status()
        for n in status.get("nodes") or []:
            agent_facts[str(n.get("node_id"))] = n
    except Exception as e:
        log.debug("node agent facts unavailable: %s", e)

    found: List[Dict[str, Any]] = []
    for nid, inst in _nodes().items():
        url = _nlp_url(inst)
        if not url:
            continue
        health = await _get_json(url, "/health")
        if not health or not health.get("ok"):
            continue
        facts = dict(agent_facts.get(str(nid)) or {})
        inventory = normalize_node_inventory(health)
        # Keep task detail for inventory consumers and derive the two compact
        # compatibility summaries used by older routing/UI readers.
        facts.update({"node_id": str(nid), "nlp_url": url,
                      "threads": health.get("threads"),
                      # the deployed nlp_server version (provision.component.version)
                      "component": health.get("component") or {},
                      **inventory})
        found.append(facts)

    _discovery_cache["at"] = now
    _discovery_cache["nodes"] = found
    return list(found)


async def placement() -> Dict[str, Any]:
    """Where the next `nlp.*` call may run, and which node would serve it."""
    cfg = await get_config()
    servers = await discover()

    pinned = str(cfg.get("node") or "").strip()
    if pinned:
        servers = [s for s in servers if str(s.get("node_id")) == pinned]

    decision = dict(resolve_placement(bool(cfg.get("nlp_local")), servers))
    node, why = (None, "")
    if decision["where"] == REMOTE:
        node, why = pick_nlp_node(servers)
        if node is None:                     # ranked away every candidate
            decision = dict(resolve_placement(bool(cfg.get("nlp_local")), []))
    decision["node"] = node
    decision["why"] = why
    decision["pinned"] = pinned
    decision["nlp_local"] = bool(cfg.get("nlp_local"))
    decision["timeout_s"] = float(cfg.get("timeout_s") or 120.0)
    decision["candidates"] = [str(s.get("node_id")) for s in servers]
    return decision


async def remote_call(path: str, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """Run one NLP op on a node. Returns (handled, result).

    `handled` is False ONLY when the caller should run it in-process — that is,
    when the switch explicitly permits local execution and no node is serving.
    A refusal (switch off, no node) comes back as handled=True with an error, so
    the caller cannot mistake it for "fall back to the host".
    """
    plan = await placement()

    if plan["where"] == LOCAL:
        return False, {}

    if plan["where"] == FAIL:
        return True, {"error": "no NLP node available", "reason": plan["reason"],
                      "nlp_local": plan["nlp_local"],
                      "candidates": plan["candidates"],
                      "hint": ("deploy the nlp_server component to a node "
                               "(nodes.provision), or nlp.config.set "
                               "nlp_local=true to allow this host to run it")}

    node = plan["node"] or {}
    url = str(node.get("nlp_url") or "")
    result = await _post_json(url, path, body, timeout=plan["timeout_s"])
    if isinstance(result, dict) and "error" not in result:
        result["node"] = str(node.get("node_id") or "")
        result["routed"] = plan["why"]
    elif isinstance(result, dict):
        result.setdefault("node", str(node.get("node_id") or ""))
        result["routed"] = plan["why"]
    return True, result


# ── Capabilities ─────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "nlp.config.get", memory="off", silent=True,
        http_method="GET", http_path="/nlp/config", http_tags=["nlp"],
        description=("Off-host NLP config. Output: {nlp_local (may this host "
                     "run NLP at all — default false), node (pin), timeout_s}."),
    )
    async def cap_nlp_config_get(trace_id=None):
        return {"config": await get_config(), "nlp_port": NLP_PORT}

    @capability(
        "nlp.config.set", memory="off",
        http_method="POST", http_path="/nlp/config/set", http_tags=["nlp"],
        description=("Update off-host NLP config. nlp_local (bool): allow the "
                     "Vera host to run nlp.* in-process — default false, and "
                     "off means it NEVER does, even with no node available. "
                     "node (str): pin to one node id, '' to choose by signals. "
                     "timeout_s (float)."),
    )
    async def cap_nlp_config_set(nlp_local: bool = None, node: str = None,
                                 timeout_s: float = None, trace_id=None):
        cfg = await set_config(nlp_local=nlp_local, node=node,
                               timeout_s=timeout_s)
        _discovery_cache["at"] = 0.0          # a pin change takes effect now
        return {"ok": True, "config": cfg}

    @capability(
        "nlp.nodes", memory="off", silent=True,
        http_method="GET", http_path="/nlp/nodes", http_tags=["nlp", "nodes"],
        description=("Which nodes serve NLP, which one the router would pick, "
                     "and why. Output: {where, node, why, candidates, reason}."),
    )
    async def cap_nlp_nodes(refresh: bool = False, trace_id=None):
        if refresh:
            await discover(force=True)
        plan = await placement()
        node = plan.get("node") or {}
        return {"where": plan["where"], "reason": plan["reason"],
                "node": str(node.get("node_id") or ""), "why": plan["why"],
                "candidates": plan["candidates"],
                "nlp_local": plan["nlp_local"], "pinned": plan["pinned"],
                "nlp_port": NLP_PORT,
                "nodes": await discover()}

    log.info("nlp_dispatch ready — nlp port %d", NLP_PORT)
