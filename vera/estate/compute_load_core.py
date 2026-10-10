"""Compute load - each model-serving node with the cores and GPUs it runs on.

Pure functions over the readers' answers (tests/test_compute_load_core.py runs
them without Vera). `build(src)` takes {reader name: answer | {error}} and
joins, per ollama instance:

  instance url's IP  -> the estate.machines row that owns that IP (the guest,
                        its Proxmox node, its SSH login)
  that SSH login     -> the obs.node_temps host probed through it: per-core load
                        as the machine itself reports it (a container reports
                        the cores it is given, numbered as it sees them)
  the instance id    -> nodes.agent.status: every GPU on the node
  the Proxmox node   -> its own obs.node_temps row: the whole host, every core

Two instances on one machine (gpu-250 and gpu-250-cpu) share its cores and
GPUs, and say so in `same_machine`.

What this does NOT claim: that a container's core N is the host's core N, or
that host load minus container load is someone else's. Each machine's figures
are its own probe's, sampled at its own moment; they are shown side by side and
never subtracted.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

HOT_PCT = 85.0          # a core at or above this counts as hot
_CPU_KEY = re.compile(r"^cpu(\d+)$", re.I)


def _ok(src: Mapping[str, Any], name: str) -> Optional[Any]:
    v = src.get(name)
    if v is None or (isinstance(v, Mapping) and v.get("error")):
        return None
    return v


def _err(src: Mapping[str, Any], name: str) -> str:
    v = src.get(name)
    if v is None:
        return "not read"
    if isinstance(v, Mapping) and v.get("error"):
        return str(v["error"])[:160]
    return "ok"


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cores_of(percpu: Any) -> List[Dict[str, Any]]:
    """{cpuN: pct} -> [{cpu: N, load: pct}] in core order; anything else dropped."""
    out = []
    for k, v in (percpu.items() if isinstance(percpu, Mapping) else []):
        m = _CPU_KEY.match(str(k))
        n = _num(v)
        if m and n is not None:
            out.append({"cpu": int(m.group(1)), "load": round(n, 1)})
    return sorted(out, key=lambda c: c["cpu"])


def summary(cores: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not cores:
        return {"n": 0, "avg": None, "max": None, "hot": 0}
    loads = [c["load"] for c in cores]
    out = {"n": len(loads), "avg": round(sum(loads) / len(loads), 1), "max": max(loads),
           "hot": sum(1 for x in loads if x >= HOT_PCT)}
    temps = [c["temp"] for c in cores if c.get("temp") is not None]
    if temps:
        out.update(temp_max=max(temps), temp_avg=round(sum(temps) / len(temps), 1))
    hl = [c["host_load"] for c in cores if c.get("host_load") is not None]
    if hl:
        out["host_load_avg"] = round(sum(hl) / len(hl), 1)
    return out


def _ip_of(url: str) -> str:
    m = re.search(r"//([^:/]+)", str(url or ""))
    return m.group(1) if m else ""


def _machines(src: Mapping[str, Any]) -> Dict[str, Any]:
    m = _ok(src, "estate.machines")
    rows = (m.get("machines") or []) if isinstance(m, Mapping) else []
    by_ip: Dict[str, Mapping[str, Any]] = {}
    pve: Dict[str, Mapping[str, Any]] = {}
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        if r.get("kind") == "guest" and (r.get("template") or str(r.get("status") or "running") != "running"):
            continue
        if r.get("kind") == "proxmox-node" and r.get("node"):
            pve[str(r["node"])] = r
        for ip in [str(i) for i in (r.get("ips") or []) if i] + ([str(r["addr"])] if r.get("addr") else []):
            # a guest owns its IP over an SSH-host row that merely points at it
            if ip not in by_ip or (r.get("kind") == "guest" and by_ip[ip].get("kind") != "guest"):
                by_ip[ip] = r
    return {"by_ip": by_ip, "pve": pve}


def _ref(r: Mapping[str, Any]) -> str:
    if r.get("kind") == "guest" and r.get("vmid") is not None:
        return "guest:" + str(r["vmid"])
    if r.get("kind") == "proxmox-node":
        return "pve:" + str(r.get("node") or r.get("label") or "")
    return "host:" + str(r.get("ssh_host_id") or r.get("label") or "")


def _probe(temps_by_id: Mapping[str, Mapping[str, Any]], ssh_id: str) -> Dict[str, Any]:
    h = temps_by_id.get(ssh_id) if ssh_id else None
    if not h:
        return {"cores": [], "at": "", "error": "no core probe for this machine" if ssh_id else "no SSH login for this machine",
                "row": {}}
    cores = cores_of(h.get("percpu"))
    err = str(h.get("error") or "")
    if not cores and not err:
        err = "the probe returned no per-core load"
    return {"cores": cores, "at": str(h.get("updated_at") or ""), "error": err, "row": h}


def _num_map(d: Any) -> Dict[str, float]:
    return {str(k): float(v) for k, v in (d.items() if isinstance(d, Mapping) else []) if _num(v) is not None}


def _pin_rows(src: Mapping[str, Any]) -> Dict[Tuple[str, int], Mapping[str, Any]]:
    """pxstore.cpu.map answers, {pve node: answer} -> {(pve node, vmid): guest row}."""
    out: Dict[Tuple[str, int], Mapping[str, Any]] = {}
    maps = src.get("pxstore.cpu.map")
    for pnode, ans in (maps.items() if isinstance(maps, Mapping) else []):
        if not isinstance(ans, Mapping) or ans.get("error"):
            continue
        for g in ans.get("guests") or []:
            if isinstance(g, Mapping) and g.get("vmid") is not None:
                try:
                    out[(str(pnode), int(g["vmid"]))] = g
                except (TypeError, ValueError):
                    continue
    return out


def pin_targets(src: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """(cluster_id, pve node) for every Proxmox node a model-serving guest runs on -
    the pxstore.cpu.map reads the join needs, each once."""
    M = _machines(src)
    inst = _ok(src, "ollama.instances") or {}
    out: List[Tuple[str, str]] = []
    for q in (inst.values() if isinstance(inst, Mapping) else []):
        row = M["by_ip"].get(_ip_of(q.get("url") if isinstance(q, Mapping) else "")) or {}
        key = (str(row.get("cluster_id") or ""), str(row.get("node") or ""))
        if row.get("kind") == "guest" and all(key) and key not in out:
            out.append(key)
    return out


def _sharers(pins: Mapping[Tuple[str, int], Mapping[str, Any]], pnode: str, vmid: int, cpus: List[int]) -> List[Dict[str, Any]]:
    """Every other running guest on the same Proxmox node whose cpus overlap these."""
    mine = set(cpus)
    out = []
    for (pn, v), g in pins.items():
        if pn != pnode or v == vmid or str(g.get("status") or "") != "running":
            continue
        over = sorted(mine & {int(c) for c in (g.get("cpus") or []) if _num(c) is not None})
        if over:
            out.append({"vmid": v, "name": str(g.get("name") or f"guest-{v}"), "type": str(g.get("type") or ""),
                        "pinned": bool(g.get("pinned")), "cpus": over})
    return sorted(out, key=lambda s: (-len(s["cpus"]), s["name"].lower()))


def build(src: Mapping[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    M = _machines(src)
    t = _ok(src, "obs.node_temps")
    t = t if isinstance(t, Mapping) else {}
    temps_by_id = {str(h.get("host_id")): h for h in (t.get("hosts") or []) if isinstance(h, Mapping) and h.get("host_id")}
    ag = _ok(src, "nodes.agent.status")
    ag = ag if isinstance(ag, Mapping) else {}
    agents = {str(a.get("node_id")): a for a in (ag.get("nodes") or []) if isinstance(a, Mapping) and a.get("node_id")}
    unreach = {str(u.get("node")) for u in (ag.get("unreachable") or []) if isinstance(u, Mapping)}
    inst = _ok(src, "ollama.instances") or {}
    pins = _pin_rows(src)

    nodes: List[Dict[str, Any]] = []
    hosts: Dict[str, Dict[str, Any]] = {}
    host_rows: Dict[str, Mapping[str, Any]] = {}       # pve node -> its own obs.node_temps row
    for iid, q in (inst.items() if isinstance(inst, Mapping) else []):
        if not isinstance(q, Mapping):
            continue
        iid = str(iid)
        ip = _ip_of(q.get("url"))
        row = M["by_ip"].get(ip) or {}
        notes: List[str] = []
        machine = None
        if row:
            machine = {"label": str(row.get("label") or ""), "kind": str(row.get("kind") or ""), "vmid": row.get("vmid"),
                       "type": str(row.get("type") or ""), "pve_node": str(row.get("node") or ""),
                       "ssh_host_id": str(row.get("ssh_host_id") or ""), "ref": _ref(row)}
        else:
            notes.append(f"no estate machine owns {ip or 'its address'}")
        probe = _probe(temps_by_id, machine["ssh_host_id"] if machine else "")
        if probe["error"]:
            notes.append("cores: " + probe["error"])
        a = agents.get(iid)
        gpus: List[Dict[str, Any]] = []
        if a:
            gpus = [g for g in (a.get("gpus") or []) if isinstance(g, Mapping)]
            if not gpus and isinstance(a.get("gpu"), Mapping):
                gpus = [dict(a["gpu"], index=0)]      # an agent from before `gpus`
        elif iid in unreach:
            notes.append("gpus: the node agent did not answer")
        elif q.get("has_gpu"):
            notes.append("gpus: no node agent on this node")
        host = None
        pnode = machine["pve_node"] if machine else ""
        if pnode and pnode in M["pve"]:
            pr = M["pve"][pnode]
            ref = _ref(pr)
            host = {"label": str(pr.get("label") or pnode), "pve_node": pnode, "ref": ref}
            if ref not in hosts:
                hp = _probe(temps_by_id, str(pr.get("ssh_host_id") or ""))
                host_rows[pnode] = hp["row"]
                ht = _num_map(hp["row"].get("cpu_temps"))
                hcores = [dict(c, temp=ht["cpu" + str(c["cpu"])]) if ("cpu" + str(c["cpu"])) in ht else dict(c) for c in hp["cores"]]
                hosts[ref] = {**host, "cores": hcores, "summary": summary(hcores), "at": hp["at"],
                              "error": hp["error"], "nodes": []}
            hosts[ref]["nodes"].append(iid)
        # A container reports the HOST's core numbers (checked against each Ollama CT's Proxmox pin, 2026-10-02),
        # so its cores take the host's temperature for that physical core and the host's whole load on it. A VM
        # numbers its own vCPUs (LLM is pinned to 36-47 and reports 0-11): nothing is mapped for one.
        cores = probe["cores"]
        lxc = bool(machine and machine["kind"] == "guest" and machine["type"] == "lxc")
        hrow = host_rows.get(pnode) or {}
        pin = pins.get((pnode, int(machine["vmid"]))) if (machine and machine.get("vmid") is not None) else None
        sharers: List[Dict[str, Any]] = []
        pinning = None
        if pin is not None:
            gcpus = sorted({int(c) for c in (pin.get("cpus") or []) if _num(c) is not None})
            sharers = _sharers(pins, pnode, int(machine["vmid"]), gcpus)
            pinning = {"pinned": str(pin.get("pinned") or ""), "live": str(pin.get("live") or ""), "cpus": gcpus,
                       "numa": list(pin.get("numa") or []), "flags": list(pin.get("flags") or []),
                       "shared_by": len(sharers)}
        if lxc:
            ht, hl = _num_map(hrow.get("cpu_temps")), _num_map(hrow.get("percpu"))
            by_cpu: Dict[int, List[str]] = {}
            for s in sharers:
                for c in s["cpus"]:
                    by_cpu.setdefault(c, []).append(s["name"])
            def _more(c: Dict[str, Any]) -> Dict[str, Any]:
                k = "cpu" + str(c["cpu"])
                x = {"temp": ht.get(k), "host_load": hl.get(k), "shared_with": by_cpu.get(c["cpu"])}
                return dict(c, **{a: b for a, b in x.items() if b is not None})
            cores = [_more(c) for c in cores]
            if hrow and not ht:
                notes.append("core temps: the host's probe has no per-core temperatures yet")
        elif machine and machine["type"] == "qemu":
            notes.append("a VM numbers its own vCPUs: host core temperatures and load are not mapped onto it")
        nodes.append({
            "id": iid, "label": str(q.get("label") or iid), "url": str(q.get("url") or ""), "ip": ip,
            "has_gpu": bool(q.get("has_gpu")), "status": str(q.get("status") or ""),
            "backend": str(q.get("backend") or "ollama"),       # "vllm": switched, served by its vLLM server
            "machine": machine, "host": host,
            "cores": cores, "summary": summary(cores), "cores_at": probe["at"],
            "pinning": pinning, "sharers": sharers,
            "gpus": gpus, "load": (a or {}).get("load"), "runners": (a or {}).get("runners"),
            "same_machine": [], "notes": notes,
        })
    by_ip: Dict[str, List[str]] = {}
    for n in nodes:
        if n["ip"]:
            by_ip.setdefault(n["ip"], []).append(n["id"])
    for n in nodes:
        n["same_machine"] = [o for o in by_ip.get(n["ip"], []) if o != n["id"]]
    nodes.sort(key=lambda n: (not n["gpus"], n["id"]))
    return {
        "nodes": nodes, "hosts": sorted(hosts.values(), key=lambda h: h["label"].lower()),
        "hot_pct": HOT_PCT,
        "sources": {**{k: _err(src, k) for k in ("ollama.instances", "estate.machines", "obs.node_temps", "nodes.agent.status")},
                    **{"pxstore.cpu.map " + str(pn): ("ok" if isinstance(a, Mapping) and not a.get("error") else str((a or {}).get("error") if isinstance(a, Mapping) else "not read")[:160])
                       for pn, a in ((src.get("pxstore.cpu.map") or {}).items() if isinstance(src.get("pxstore.cpu.map"), Mapping) else [])}},
        "ts": now.isoformat(),
    }
