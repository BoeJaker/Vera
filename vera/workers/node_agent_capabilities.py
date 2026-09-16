"""
node_agent_capabilities.py — see and control the compute workers, from Vera
=============================================================================

Vera could route work to the ollama nodes but could not see what they were
DOING, and could not stop it. Ollama's API reports which models are RESIDENT,
never which are COMPUTING, and the CPU nodes are ungated (capacity 0) so
nothing tracked their work at all.

The gap had teeth (2026-09-16): a runner on cpu-247 sat at 1200% CPU — twelve of
the Proxmox host's forty-eight cores — for eighty minutes with nothing waiting
on it. The orchestrator had restarted half an hour into the generation, so the
client was gone; `llama-server` only discovers that when it finishes and tries
to write, and at the ~0.05 tok/s a CPU node manages against a 24,576-token
window, finishing was days away. Diagnosing it took root on the Proxmox host and
`pct exec`; so did killing it. Neither generalises past Proxmox, and neither is
something Vera could do for itself.

`_sweep_stuck_running` (job_persistance) already fail-marks the RECORD of such a
job. This module is the other half — it tells the NODE to stop.

Capabilities
------------
  nodes.agent.status   host facts for every node running the agent
  nodes.runner.list    compute-worker processes across the estate, with `stuck`
  nodes.runner.kill    terminate one runner (explicit, gated, audited)
  nodes.runner.reap    apply the stuck rule now; also runs on a timer

The stuck rule is Vera's own existing reasoning, applied to the process rather
than the bookkeeping: a generation cannot outlive the client waiting for it,
because that client aborts at OLLAMA_GEN_TIMEOUT. A runner still burning CPU at
twice that has, by definition, no one left to answer. Decision logic is pure and
shared with the agent (edge/node_runner_core.py), pinned by
tests/test_node_runner_reap.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import (
    capability, emit_event, schedule,
)
from Vera.vera import capability_orchestration as _orch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "edge"))
from node_runner_core import (  # noqa: E402
    DEFAULT_STUCK_S, Runner, reap_plan,
)

log = logging.getLogger("vera.nodes.agent")

#: Port the agent listens on, per node.
AGENT_PORT = int(os.getenv("VERA_NODE_AGENT_PORT", "8770") or 8770)
#: Shared secret for mutating calls; must match VERA_NODE_TOKEN on the nodes.
AGENT_TOKEN = os.getenv("VERA_NODE_TOKEN", "").strip()
#: A runner computing for longer than this has outlived every client.
STUCK_S = float(os.getenv("VERA_RUNNER_STUCK_S", "0") or 0) or max(
    2.0 * float(getattr(_orch, "OLLAMA_GEN_TIMEOUT", 900.0) or 900.0), DEFAULT_STUCK_S)
#: Automatic reaping is OFF unless asked for. It kills work on other machines;
#: that should be a decision someone made, not a default they inherited.
REAP_ENABLED = (os.getenv("VERA_RUNNER_REAP", "0") or "0").strip().lower() in (
    "1", "true", "yes", "on")
#: Never kill more than this in one pass — a misconfigured threshold should
#: read as a slow drip in the log, not an estate-wide cull.
REAP_MAX = int(os.getenv("VERA_RUNNER_REAP_MAX", "2") or 2)


def _agent_url(inst: Dict) -> str:
    """The agent's address, derived from the ollama node's own URL so a node is
    configured once. http://192.168.0.247:11435 -> http://192.168.0.247:8770"""
    url = str(inst.get("url") or "")
    if not url:
        return ""
    try:
        host = url.split("//", 1)[1].split(":", 1)[0].split("/", 1)[0]
    except Exception:
        return ""
    return f"http://{host}:{AGENT_PORT}"


def _nodes() -> Dict[str, Dict]:
    return dict(getattr(_orch, "OLLAMA_INSTANCES", {}) or {})


async def _agent_get(url: str, path: str, timeout: float = 10.0) -> Optional[Dict]:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{url}{path}")
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.debug("node agent GET %s%s: %s", url, path, e)
        return None


async def _agent_post(url: str, path: str, body: Dict,
                      timeout: float = 30.0) -> Dict:
    try:
        import httpx
        headers = {"X-Vera-Node-Token": AGENT_TOKEN} if AGENT_TOKEN else {}
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{url}{path}", json=body, headers=headers)
            try:
                payload = r.json()
            except Exception:
                payload = {"error": r.text[:300]}
            if r.status_code != 200:
                return {"ok": False, "status": r.status_code, **(payload or {})}
            return payload
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _collect_runners(node_filter: str = "") -> Dict[str, Any]:
    """Ask every node's agent what it is running. Nodes without an agent are
    reported as such rather than silently missing — a node Vera cannot see is a
    finding, not an absence."""
    out: List[Dict] = []
    unreachable: List[Dict] = []
    for nid, inst in _nodes().items():
        if node_filter and nid != node_filter:
            continue
        url = _agent_url(inst)
        if not url:
            continue
        data = await _agent_get(url, f"/node/runners?stuck_s={int(STUCK_S)}")
        if data is None:
            unreachable.append({"node": nid, "agent": url,
                                "error": "no agent response"})
            continue
        for r in data.get("runners") or []:
            out.append({**r, "node": nid, "agent": url})
    return {"runners": out, "unreachable": unreachable}


@capability(
    "nodes.agent.status", memory="off", silent=True,
    http_method="GET", http_path="/nodes/agent/status", http_tags=["nodes", "obs"],
    description="Host facts from every compute node running the Vera node agent "
                "— cores, load, memory, GPU (name/total/used/free), runner count. "
                "Nodes with no agent are listed under `unreachable` rather than "
                "omitted. Output: {nodes:[...], unreachable:[...]}.")
async def cap_nodes_agent_status(trace_id=None) -> Dict[str, Any]:
    nodes, unreachable = [], []
    for nid, inst in _nodes().items():
        url = _agent_url(inst)
        if not url:
            continue
        data = await _agent_get(url, "/node/status")
        if data is None:
            unreachable.append({"node": nid, "agent": url})
        else:
            # Feed real VRAM back to the window arithmetic. The catalog is the
            # primary source but had not probed gpu-250, so _auto_ctx_for saw
            # vram_gb=None and offered a 12.3 GB card the model's full 262,144
            # window. The agent reads nvidia-smi on the node itself, so this is
            # measured rather than assumed.
            gpu = data.get("gpu") or {}
            total_mb = int(gpu.get("total_mb") or 0)
            if total_mb > 0:
                try:
                    _orch._NODE_VRAM_OBSERVED[nid] = round(total_mb / 1024.0, 2)
                except Exception:
                    pass
            nodes.append({**data, "node_id": nid, "agent": url})
    return {"nodes": nodes, "unreachable": unreachable, "count": len(nodes)}


@capability(
    "nodes.runner.list", memory="off", silent=True,
    http_method="GET", http_path="/nodes/runner/list", http_tags=["nodes", "obs"],
    description="Every compute-worker process across the estate (ollama's "
                "llama-server today), with pid, model blob, state, CPU seconds, "
                "age, RSS — and whether it is STUCK: still computing past "
                "2x OLLAMA_GEN_TIMEOUT, by which point every client that asked "
                "has already aborted. Read-only. Inputs: node (str filter). "
                "Output: {runners:[...], stuck_count, unreachable:[...]}.")
async def cap_nodes_runner_list(node: str = "", trace_id=None) -> Dict[str, Any]:
    got = await _collect_runners(node)
    stuck = [r for r in got["runners"] if r.get("stuck")]
    return {"runners": got["runners"], "stuck_count": len(stuck),
            "stuck": stuck, "unreachable": got["unreachable"],
            "stuck_after_s": STUCK_S}


@capability(
    "nodes.runner.kill", memory="on",
    http_method="POST", http_path="/nodes/runner/kill", http_tags=["nodes"],
    description="Terminate ONE compute-worker process on a node. SIGTERM by "
                "default — ollama reaps the runner and unloads its model "
                "cleanly. Requires VERA_NODE_TOKEN to match the node's. This is "
                "deliberately explicit and per-pid: `nodes.runner.reap` is the "
                "automatic path, and it applies a rule rather than a guess. "
                "Inputs: node (str!), pid (int!), force (bool), reason (str). "
                "Output: {ok, gone, runner, node}.")
async def cap_nodes_runner_kill(node: str = "", pid: int = 0, force: bool = False,
                                reason: str = "", trace_id=None) -> Dict[str, Any]:
    if not node or not pid:
        return {"ok": False, "error": "node and pid are required"}
    inst = _nodes().get(node)
    if not inst:
        return {"ok": False, "error": f"unknown node '{node}'",
                "available": list(_nodes().keys())}
    url = _agent_url(inst)
    if not url:
        return {"ok": False, "error": f"no agent address for '{node}'"}
    res = await _agent_post(url, "/node/runner/kill",
                            {"pid": int(pid), "force": bool(force),
                             "reason": reason or "requested via nodes.runner.kill"})
    await emit_event({"type": "nodes.runner.killed", "node": node, "pid": int(pid),
                      "ok": bool(res.get("ok")), "reason": reason,
                      "forced": bool(force)})
    return {**res, "node": node}


async def _reap(dry_run: bool = True, limit: int = 0) -> Dict[str, Any]:
    got = await _collect_runners()
    runners = [Runner(pid=int(r.get("pid") or 0), model=r.get("model", ""),
                      port=int(r.get("port") or 0), state=r.get("state", ""),
                      cpu_seconds=float(r.get("cpu_seconds") or 0),
                      age_s=float(r.get("age_s") or 0),
                      rss_mb=int(r.get("rss_mb") or 0), node=r.get("node", ""))
               for r in got["runners"]]
    verdict = reap_plan(runners, stuck_s=STUCK_S)
    cap = limit or REAP_MAX
    killed, planned = [], []
    for r in verdict.stuck[:cap]:
        entry = {"node": r.node, "pid": r.pid, "model": r.model,
                 "age_s": round(r.age_s), "cpu_seconds": round(r.cpu_seconds),
                 "reason": verdict.reasons.get(r.pid, "")}
        planned.append(entry)
        if dry_run:
            continue
        inst = _nodes().get(r.node)
        url = _agent_url(inst) if inst else ""
        if not url:
            entry["error"] = "no agent address"
            continue
        res = await _agent_post(url, "/node/runner/kill",
                                {"pid": r.pid, "force": False,
                                 "reason": entry["reason"]})
        entry["killed"] = bool(res.get("ok"))
        entry["gone"] = bool(res.get("gone"))
        if res.get("error"):
            entry["error"] = res["error"]
        killed.append(entry)
    return {"stuck": len(verdict.stuck), "planned": planned, "killed": killed,
            "capped_at": cap, "dry_run": dry_run,
            "unreachable": got["unreachable"], "stuck_after_s": STUCK_S}


@capability(
    "nodes.runner.reap", memory="on",
    http_method="POST", http_path="/nodes/runner/reap", http_tags=["nodes"],
    description="Find compute workers that have outlived every client waiting "
                "on them and stop them. A runner is stuck when it is ACTIVELY "
                "computing (state R with real CPU time, not merely resident on "
                "keep_alive) and has been for longer than 2x OLLAMA_GEN_TIMEOUT "
                "— past that no caller is still listening, because they all "
                "abort at the timeout. DRY RUN by default: it reports what it "
                "would kill and why. Inputs: dry_run (bool=true), limit (int — "
                "per-pass cap, default VERA_RUNNER_REAP_MAX=2). Output: "
                "{stuck, planned:[...], killed:[...], unreachable:[...]}.")
async def cap_nodes_runner_reap(dry_run: bool = True, limit: int = 0,
                                trace_id=None) -> Dict[str, Any]:
    res = await _reap(dry_run=bool(dry_run), limit=int(limit or 0))
    if not dry_run and res.get("killed"):
        await emit_event({"type": "nodes.runner.reaped",
                          "killed": len(res["killed"]), "stuck": res["stuck"]})
    return res


async def _reap_tick() -> None:
    """Scheduler tick: stop runners nothing is waiting for.

    OFF unless VERA_RUNNER_REAP=1 — this kills work on other machines, which
    should be a decision someone made rather than a default they inherited.
    While off it still REPORTS, so the estate gets the warning either way and
    the rule can be watched before it is trusted.
    """
    try:
        # Refresh observed VRAM on the same tick — it costs one extra call and
        # keeps _auto_ctx_for's window ceiling grounded in what the cards
        # actually have.
        try:
            await cap_nodes_agent_status()
        except Exception:
            pass
        res = await _reap(dry_run=not REAP_ENABLED)
        if not res.get("stuck"):
            return
        if REAP_ENABLED:
            log.warning("node reaper: stopped %d stuck runner(s) of %d found: %s",
                        len(res.get("killed") or []), res["stuck"],
                        "; ".join(f"{e['node']}/{e['pid']} {e['model'][:24]} "
                                  f"({e['age_s']}s)" for e in res.get("killed") or []))
            await emit_event({"type": "nodes.runner.reaped",
                              "killed": len(res.get("killed") or []),
                              "stuck": res["stuck"], "automatic": True})
        else:
            log.warning("node reaper: %d stuck runner(s) found and NOT stopped "
                        "(set VERA_RUNNER_REAP=1 to act): %s", res["stuck"],
                        "; ".join(f"{e['node']}/{e['pid']} ({e['age_s']}s)"
                                  for e in res.get("planned") or []))
            await emit_event({"type": "nodes.runner.stuck",
                              "stuck": res["stuck"], "planned": res.get("planned"),
                              "acting": False})
    except Exception as e:
        log.debug("node reaper tick failed: %s", e)


try:
    schedule(_reap_tick, 300, name="nodes_runner_reap")
except Exception as _e:  # pragma: no cover
    log.debug("could not register node reaper: %s", _e)

log.info("node agent capabilities registered (stuck>%.0fs, auto-reap=%s)",
         STUCK_S, REAP_ENABLED)
