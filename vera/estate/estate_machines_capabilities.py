"""
estate_machines_capabilities.py -- every machine in the estate, in one list
=========================================================================

The Estate tab listed machines in four panes: Workers, Nodes, Proxmox and
Docker each showed part of the estate with actions of its own. estate.machines
joins nodes.list (SSH hosts, Proxmox nodes and enrolled guests, Docker hosts,
Ollama and vLLM nodes) with every guest Proxmox reports, so a stopped or
never-enrolled guest is still a row, an SSH host that is really a guest is
folded into it, and each row carries the actions that apply to it. The actions
run where they always did: power through proxmox.guest.action, consoles
through proxmox.console.ticket, terminals through conn.open.

The rules live in estate_machines_core.py. Other modules are reached through
the capability registry: capability modules load from _module_files under
their bare file name, so an import-path lookup finds nothing.

Capabilities
------------
  estate.machines   one row per machine with its state and available actions
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, now_iso
from Vera.vera.estate import estate_health_core as health_core
from Vera.vera.estate import estate_machines_core as core

log = logging.getLogger("vera.estate")

SOURCE_TIMEOUT_S = 25.0
_CONFIG_CONCURRENCY = 6       # guest config reads at once, for their static IPs


def _module_of(cap_name: str) -> Optional[Dict[str, Any]]:
    """The globals of the module that registered `cap_name`, or None."""
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return None
    return getattr(inspect.unwrap(fn), "__globals__", None)


async def _call(cap_name: str, **kwargs: Any) -> Dict[str, Any]:
    """Run another capability's function directly; any failure is {error}."""
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return {"error": f"{cap_name} is not loaded"}
    try:
        out = await fn(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out if isinstance(out, dict) else {"error": f"{cap_name} returned no result"}


async def _guests() -> Dict[str, Any]:
    """Every Proxmox guest with its static IPs, read once per API host through
    the first cluster record whose token works, tagged with that record's id."""
    px = _module_of("proxmox.status")
    if not px or not all(k in px for k in ("_all_raw", "_open", "_pve")):
        return {"guests": [], "errors": ["the Proxmox capabilities are not loaded"]}
    pve = px["_pve"]
    guests: List[Dict[str, Any]] = []
    errors: List[str] = []
    for host, group in health_core.group_clusters(await px["_all_raw"]()).items():
        rows, err, record, opened = None, "", None, None
        for candidate in group:
            opened = px["_open"](candidate)
            rows, err = await pve(opened, "GET", "/cluster/resources?type=vm")
            if rows is not None:
                record = candidate
                break
        if rows is None:
            errors.append(f"Proxmox at {host}: {err}")
            continue
        gate = asyncio.Semaphore(_CONFIG_CONCURRENCY)

        async def ips_of(g: Dict[str, Any], rec=opened, gate=gate) -> List[str]:
            if g.get("template"):
                return []
            async with gate:
                cfg, _ = await pve(rec, "GET", f"/nodes/{g.get('node')}/{g.get('type')}/{g.get('vmid')}/config")
            return core.guest_ips(cfg)

        ips = await asyncio.gather(*(ips_of(g) for g in rows))
        for g, addrs in zip(rows, ips):
            guests.append({"cluster_id": record.get("id", ""), "vmid": g.get("vmid"),
                           "name": g.get("name") or "", "type": g.get("type"),
                           "node": g.get("node"), "status": g.get("status"),
                           "template": bool(g.get("template")), "ips": addrs,
                           "maxcpu": g.get("maxcpu"), "maxmem": g.get("maxmem")})
    return {"guests": guests, "errors": errors}


async def _within(label: str, source, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return await asyncio.wait_for(source, SOURCE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return dict(fallback, error=f"{label} did not answer within {int(SOURCE_TIMEOUT_S)} s")


@capability(
    "estate.machines",
    http_method="GET", http_path="/estate/machines", http_tags=["estate", "nodes"],
    memory="off", silent=True,
    description="Every machine in the estate in one list: nodes.list (SSH hosts, Proxmox "
                "nodes and enrolled guests, Docker hosts, Ollama and vLLM nodes) joined with "
                "every guest Proxmox reports. Stopped and never-enrolled guests are rows too, "
                "an SSH host that is a guest (same static IP, else same name) is folded into "
                "it, and repeated logins for one machine collapse into one row. Each row: "
                "{id, label, kind: proxmox-node|guest|host|docker-host, status, addr, ips, "
                "cluster_id, node, vmid, type, template, ssh_host_id, docker_host_id, logins, "
                "hardware[], runs[], backends[], note, actions:[{id: console|shutdown|reboot|"
                "start|ssh|cpu|detect|containers, label, mode?}]}. Read-only; the actions run "
                "through proxmox.guest.action, proxmox.console.ticket and conn.open. Output: "
                "{machines, counts, errors, checked_at}.",
)
async def cap_estate_machines(trace_id=None) -> Dict[str, Any]:
    nodes_res, guest_res = await asyncio.gather(
        _within("nodes.list", _call("nodes.list"), {"nodes": []}),
        _within("Proxmox", _guests(), {"guests": []}))
    errors = list(guest_res.get("errors") or [])
    if guest_res.get("error"):
        errors.append(guest_res["error"])
    if nodes_res.get("error"):
        errors.append(f"nodes.list: {nodes_res['error']}")
    rows = core.build_machines(nodes_res.get("nodes") or [], guest_res.get("guests") or [])
    return {"machines": rows, "counts": core.counts(rows), "errors": errors, "checked_at": now_iso()}
