"""
netgraph_capabilities.py — Vera Network graph, policy & monitoring (Iteration 5)
================================================================================

A visual, editable topology of Proxmox + Docker networking:

  • netgraph.topology  — aggregate PVE nodes/guests (from proxmox.status) and
    Docker networks/containers (Engine API) into a Cytoscape node/edge model.
  • netgraph.edge.allow / .deny — "draw a line to connect / cut a line to
    disconnect": for Docker this connects/disconnects a container ↔ network; for
    Proxmox it adds/removes a guest firewall ACCEPT rule (reusing proxmox.fw.*).
  • netgraph.docker.connect / .disconnect — explicit Docker network membership.
  • netmon.snapshot — compact per-node/guest/Docker metrics for the Ollama panel.

Decoupled by design: everything is reached through the capability registry
(`proxmox.*`) or the already-loaded Docker module's Engine helpers, so this
module hard-depends on neither.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, register_ui

log = logging.getLogger("vera.netgraph")
_HERE = Path(__file__).parent


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


def _dmod():
    return sys.modules.get("docker_capabilities")


async def _first_cluster_id(cluster_id: str) -> str:
    if cluster_id:
        return cluster_id
    lst = _cap("proxmox.cluster.list")
    if lst:
        cs = (await lst()).get("clusters", [])
        if cs:
            return cs[0].get("id", "")
    return ""


# ═════════════════════════════════════════════════════════════════════════════
#  DOCKER ENGINE HELPERS  (reuse the Docker module; add a body-capable POST)
# ═════════════════════════════════════════════════════════════════════════════
async def _engine_get(host_id: str, path: str) -> Tuple[int, Any]:
    dmod = _dmod()
    if not dmod:
        return 503, {"message": "docker module not loaded"}
    rec = dmod._get_host(host_id)
    if not rec:
        return 404, {"message": f"unknown docker host: {host_id}"}
    st, body, _ = await dmod._engine_request(rec, "GET", path)
    try:
        return st, json.loads(body or b"null")
    except Exception:
        return st, {"_raw": (body or b"").decode("utf-8", "replace")}


async def _engine_post(host_id: str, path: str, body: Dict) -> Tuple[int, Any]:
    """POST with a JSON body (Engine connect/disconnect). Supports tcp + local
    socket hosts; ssh hosts return a clear not-supported error."""
    dmod = _dmod()
    if not dmod:
        return 503, {"message": "docker module not loaded"}
    rec = dmod._get_host(host_id)
    if not rec:
        return 404, {"message": f"unknown docker host: {host_id}"}
    kind = rec.get("kind", "local")
    try:
        if kind == "tcp":
            base = dmod._normalize_tcp(rec.get("url", ""))
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(base + path, json=body)
                return r.status_code, (r.json() if r.text else {})
        sock = rec.get("socket") or getattr(dmod, "_LOCAL_SOCK", "/var/run/docker.sock")
        if os.path.exists(sock):
            tr = httpx.AsyncHTTPTransport(uds=sock)
            async with httpx.AsyncClient(transport=tr, timeout=15) as c:
                r = await c.post("http://localhost" + path, json=body)
                return r.status_code, (r.json() if r.text else {})
        return 501, {"message": f"docker host kind '{kind}' not supported for this op"}
    except Exception as e:
        return 502, {"message": f"{type(e).__name__}: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
#  TOPOLOGY
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "netgraph.topology",
    http_method="POST", http_path="/netgraph/topology", http_tags=["netgraph"],
    memory="off", silent=True,
    description="Aggregate a Cytoscape topology of Proxmox + Docker. Inputs: "
                "cluster_id (str — defaults to first cluster), docker_host_id "
                "(str — ONE docker host; blank = ALL registered docker hosts "
                "when all_docker is true), all_docker (bool=true), with_fw "
                "(bool=true — include guest firewall rules as policy edges, "
                "capped). Docker nodes carry host_id so edge ops route to the "
                "right engine. Output: {nodes:[{id,label,kind,parent,host_id}], "
                "edges:[{id,source,target,rel,policy,meta}], clusters, docker_hosts}.",
)
async def cap_topology(cluster_id: str = "", docker_host_id: str = "",
                       all_docker: bool = True, with_fw: bool = True,
                       trace_id=None) -> Dict:
    nodes: List[Dict] = []
    edges: List[Dict] = []

    # ── Proxmox ──────────────────────────────────────────────────────────────
    cid = await _first_cluster_id(cluster_id)
    status = _cap("proxmox.status")
    pve_guests = []
    if cid and status:
        snap = await status(cluster_id=cid)
        for n in snap.get("nodes", []):
            nodes.append({"id": f"n:{n['node']}", "label": n["node"], "kind": "pve_node"})
        for g in snap.get("guests", []):
            if g.get("template"):
                continue
            gid = f"g:{g['vmid']}"
            nodes.append({"id": gid, "label": g.get("name") or f"vm{g['vmid']}",
                          "kind": "guest", "gtype": g["type"], "status": g["status"],
                          "node": g["node"], "vmid": g["vmid"]})
            edges.append({"id": f"{gid}-host", "source": gid, "target": f"n:{g['node']}",
                          "rel": "hosted_on"})
            pve_guests.append(g)

    # Firewall rules → policy edges (capped to keep it responsive)
    if with_fw and cid:
        fw = _cap("proxmox.fw.rules.list")
        running = [g for g in pve_guests if g.get("status") == "running"][:25]
        async def _fetch(g):
            r = await fw(cluster_id=cid, scope="guest", node=g["node"],
                         guest_type=g["type"], vmid=g["vmid"])
            return g, (r.get("rules") or [])
        if fw and running:
            for g, rules in await asyncio.gather(*[_fetch(g) for g in running]):
                for r in rules:
                    src = r.get("source") or "any"
                    snode = f"src:{src}"
                    if not any(n["id"] == snode for n in nodes):
                        nodes.append({"id": snode, "label": src, "kind": "cidr"})
                    edges.append({
                        "id": f"fw:{g['vmid']}:{r.get('pos')}",
                        "source": snode, "target": f"g:{g['vmid']}",
                        "rel": "fw", "policy": (r.get("action") or "").lower(),
                        "meta": {"pos": r.get("pos"), "proto": r.get("proto", ""),
                                 "dport": r.get("dport", ""), "type": r.get("type", "in"),
                                 "node": g["node"], "gtype": g["type"], "vmid": g["vmid"]},
                    })

    # ── Docker ───────────────────────────────────────────────────────────────
    clusters, dhosts = [], []
    clst = _cap("proxmox.cluster.list")
    if clst:
        clusters = [{"id": c.get("id"), "label": c.get("label")}
                    for c in (await clst()).get("clusters", [])]
    dl = _cap("docker.hosts.list")
    if dl:
        try:
            res = await dl()
            for h in (res.get("hosts") or res.get("items") or []):
                dhosts.append({"id": h.get("id"), "label": h.get("label") or h.get("id")})
        except Exception:
            pass

    # One explicit host, or ALL registered hosts (each engine gets its own
    # docker_host anchor node; every docker node carries host_id so the edge
    # ops — connect/disconnect — route to the right engine).
    if docker_host_id:
        target_hosts = [h for h in dhosts if h["id"] == docker_host_id] \
            or [{"id": docker_host_id, "label": docker_host_id}]
    elif all_docker:
        target_hosts = dhosts
    else:
        target_hosts = []

    multi = len(target_hosts) > 1
    for th in target_hosts:
        hid = th["id"]
        pfx = f"{hid[:8]}:" if multi else ""      # id-collision guard across engines
        hnode = f"dh:{hid}"
        st, nets = await _engine_get(hid, "/networks")
        if st != 200 or not isinstance(nets, list):
            continue                               # unreachable engine — skip quietly
        nodes.append({"id": hnode, "label": th.get("label") or hid,
                      "kind": "docker_host", "host_id": hid})
        for net in nets:
            nid = f"dn:{pfx}{net.get('Id','')[:12]}"
            nodes.append({"id": nid, "label": net.get("Name", ""),
                          "kind": "dnet", "driver": net.get("Driver", ""),
                          "net_id": net.get("Id", ""), "host_id": hid})
            edges.append({"id": f"{nid}-{hnode}", "source": nid,
                          "target": hnode, "rel": "on_host"})
        st, conts = await _engine_get(hid, "/containers/json?all=false")
        if st == 200 and isinstance(conts, list):
            for ct in conts:
                cid2 = ct.get("Id", "")[:12]
                cnid = f"dc:{pfx}{cid2}"
                name = (ct.get("Names") or ["/?"])[0].lstrip("/")
                nodes.append({"id": cnid, "label": name, "kind": "container",
                              "container_id": ct.get("Id", ""), "host_id": hid})
                for nname, ninfo in ((ct.get("NetworkSettings") or {}).get("Networks") or {}).items():
                    tnid = f"dn:{pfx}{(ninfo.get('NetworkID') or '')[:12]}"
                    if any(n["id"] == tnid for n in nodes):
                        edges.append({"id": f"{cnid}-{tnid}", "source": cnid,
                                      "target": tnid, "rel": "member",
                                      "meta": {"network": nname}})

    return {"nodes": nodes, "edges": edges, "clusters": clusters,
            "docker_hosts": dhosts, "cluster_id": cid}


# ═════════════════════════════════════════════════════════════════════════════
#  EDGE OPS — draw to connect / cut to disconnect
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "netgraph.docker.connect",
    http_method="POST", http_path="/netgraph/docker/connect", http_tags=["netgraph"],
    memory="off",
    description="Connect a container to a Docker network. Inputs: host_id (str!), "
                "container (str!), network (str!). Output: {ok} or {error}.",
)
async def cap_docker_connect(host_id: str = "", container: str = "",
                             network: str = "", trace_id=None) -> Dict:
    if not (host_id and container and network):
        return {"error": "host_id, container, network required"}
    st, body = await _engine_post(host_id, f"/networks/{network}/connect",
                                  {"Container": container})
    if st >= 400:
        return {"error": body.get("message", f"HTTP {st}")}
    await emit_event({"type": "netgraph.docker.connected",
                      "container": container, "network": network})
    return {"ok": True}


@capability(
    "netgraph.docker.disconnect",
    http_method="POST", http_path="/netgraph/docker/disconnect", http_tags=["netgraph"],
    memory="off",
    description="Disconnect a container from a Docker network. Inputs: host_id "
                "(str!), container (str!), network (str!). Output: {ok} or {error}.",
)
async def cap_docker_disconnect(host_id: str = "", container: str = "",
                                network: str = "", trace_id=None) -> Dict:
    if not (host_id and container and network):
        return {"error": "host_id, container, network required"}
    st, body = await _engine_post(host_id, f"/networks/{network}/disconnect",
                                  {"Container": container, "Force": True})
    if st >= 400:
        return {"error": body.get("message", f"HTTP {st}")}
    await emit_event({"type": "netgraph.docker.disconnected",
                      "container": container, "network": network})
    return {"ok": True}


@capability(
    "netgraph.edge.allow",
    http_method="POST", http_path="/netgraph/edge/allow", http_tags=["netgraph"],
    memory="off",
    description="Allow a connection (draw a line). kind='docker': connect "
                "container↔network (host_id, container, network). kind='pve': add "
                "a guest firewall ACCEPT rule (cluster_id, node, guest_type, vmid, "
                "source, proto, dport). Output: {ok} or {error}.",
)
async def cap_edge_allow(kind: str = "pve", host_id: str = "", container: str = "",
                         network: str = "", cluster_id: str = "", node: str = "",
                         guest_type: str = "", vmid: int = 0, source: str = "",
                         proto: str = "", dport: str = "", trace_id=None) -> Dict:
    if kind == "docker":
        return await cap_docker_connect(host_id=host_id, container=container,
                                        network=network)
    add = _cap("proxmox.fw.rule.add")
    if not add:
        return {"error": "proxmox firewall capability unavailable"}
    return await add(cluster_id=cluster_id, scope="guest", node=node,
                     guest_type=guest_type, vmid=vmid, action="ACCEPT", type="in",
                     source=source, proto=proto, dport=dport,
                     comment="vera-netgraph allow")


@capability(
    "netgraph.edge.deny",
    http_method="POST", http_path="/netgraph/edge/deny", http_tags=["netgraph"],
    memory="off",
    description="Cut a connection (delete a line). kind='docker': disconnect "
                "container↔network. kind='pve': delete a firewall rule by pos "
                "(cluster_id, node, guest_type, vmid, pos). Output: {ok} or {error}.",
)
async def cap_edge_deny(kind: str = "pve", host_id: str = "", container: str = "",
                        network: str = "", cluster_id: str = "", node: str = "",
                        guest_type: str = "", vmid: int = 0, pos: int = -1,
                        trace_id=None) -> Dict:
    if kind == "docker":
        return await cap_docker_disconnect(host_id=host_id, container=container,
                                           network=network)
    dele = _cap("proxmox.fw.rule.delete")
    if not dele:
        return {"error": "proxmox firewall capability unavailable"}
    return await dele(cluster_id=cluster_id, scope="guest", node=node,
                      guest_type=guest_type, vmid=vmid, pos=pos)


# ═════════════════════════════════════════════════════════════════════════════
#  MONITORING  (consumed by the Ollama panel)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "netmon.snapshot",
    http_method="POST", http_path="/netmon/snapshot", http_tags=["netgraph"],
    memory="off", silent=True,
    description="Compact network/cluster metrics for the Ollama monitor. Inputs: "
                "cluster_id (str — defaults to first), docker_host_id (str). "
                "Output: {nodes:[{node,cpu,mem}], totals:{guests,running,netin,"
                "netout}, docker:{containers,networks}}.",
)
async def cap_netmon(cluster_id: str = "", docker_host_id: str = "",
                     trace_id=None) -> Dict:
    out: Dict[str, Any] = {"nodes": [], "totals": {}, "docker": {}}
    cid = await _first_cluster_id(cluster_id)
    status = _cap("proxmox.status")
    if cid and status:
        snap = await status(cluster_id=cid)
        for n in snap.get("nodes", []):
            mem = (n.get("mem", 0) / n["maxmem"]) if n.get("maxmem") else 0
            out["nodes"].append({"node": n["node"],
                                 "cpu": round((n.get("cpu", 0) or 0) * 100, 1),
                                 "mem": round(mem * 100, 1),
                                 "status": n.get("status", "")})
        guests = [g for g in snap.get("guests", []) if not g.get("template")]
        out["totals"] = {
            "guests": len(guests),
            "running": sum(1 for g in guests if g.get("status") == "running"),
            "netin": sum(g.get("netin", 0) or 0 for g in guests),
            "netout": sum(g.get("netout", 0) or 0 for g in guests),
        }
    if docker_host_id:
        st, conts = await _engine_get(docker_host_id, "/containers/json?all=false")
        st2, nets = await _engine_get(docker_host_id, "/networks")
        out["docker"] = {
            "containers": len(conts) if isinstance(conts, list) else 0,
            "networks": len(nets) if isinstance(nets, list) else 0,
        }
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/netgraph/panel", include_in_schema=False)
async def _netgraph_panel():
    p = _HERE / "netgraph_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>netgraph_panel.html not found</p>")


# Standalone top-level tab retired — embedded as the Net Policy sub-tab of the
# workers/Ollama panel's Proxmox pane. The /netgraph/panel route is kept.
register_ui = (lambda *a, **k: None)
register_ui(
    "netgraph-panel",
    "Net Policy",
    "🕸",
    """<div id="netgraph-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/netgraph/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "netgraph.topology", "netgraph.edge.allow", "netgraph.edge.deny",
        "netgraph.docker.connect", "netgraph.docker.disconnect", "netmon.snapshot",
        "proxmox.cluster.list", "proxmox.fw.rules.list", "docker.hosts.list",
    ],
    mode="tab",
    tab_order=58,
)


log.info("netgraph_capabilities ready")
