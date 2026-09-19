"""Who is registered where - one table for the whole estate.

Measured on 19 Sep 2026: 59 machines, 15 with an SSH login, 7 in the
directory, 2 on the mesh, and no machine in every plane; the directory still
listed a container retired that morning. Four registries, each only partly
filled, and nothing that put them side by side. This does: every machine
against every plane (through the same joins the entity drawer uses), the
entries that no machine answers to, and the plan that would remove them.

Pure over estate_entity_core.Sources (tests/test_registration_core.py).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .estate_entity_core import PLANES, Sources, _lower, machine_planes
from .estate_nav_core import entity_ref

# Logins that are not machines of the estate and must never count as stale.
_KEEP_LOGINS = ("localhost", "127.0.0.1")


def machine_ref(m: Mapping[str, Any]) -> str:
    if m.get("kind") == "guest" and m.get("vmid") is not None:
        return entity_ref("guest", m.get("vmid"))
    return entity_ref("host", m.get("ssh_host_id") or m.get("id"))


def coverage(src: Sources) -> Dict[str, Any]:
    """Every machine with its five planes, and the counts."""
    rows: List[Dict[str, Any]] = []
    for m in src.machines:
        if m.get("template"):
            continue
        planes = machine_planes(src, m)
        applicable = [p for p in PLANES if planes[p]["state"] != "n/a"]
        full = bool(applicable) and all(planes[p]["state"] == "yes" for p in applicable)
        rows.append({"ref": machine_ref(m), "label": m.get("label") or "", "kind": m.get("kind") or "",
                     "status": m.get("status") or "", "addr": m.get("addr") or "",
                     "vmid": m.get("vmid"), "node": m.get("node"), "type": m.get("type"),
                     "cluster_id": m.get("cluster_id"), "planes": planes, "complete": full})
    counts = {p: {"yes": 0, "no": 0, "unknown": 0, "n/a": 0} for p in PLANES}
    for r in rows:
        for p in PLANES:
            counts[p][r["planes"][p]["state"]] = counts[p].get(r["planes"][p]["state"], 0) + 1
    rows.sort(key=lambda r: (r["complete"], sum(1 for p in PLANES if r["planes"][p]["state"] == "yes"), r["label"].lower()))
    return {"rows": rows, "counts": counts, "machines": len(rows),
            "complete": sum(1 for r in rows if r["complete"])}


def stale(src: Sources) -> List[Dict[str, Any]]:
    """Registry entries no machine answers to: a directory host, an SSH login,
    a mesh member or a door device whose machine is gone. Each carries the
    action that would remove it, for prune() to run or a person to read."""
    out: List[Dict[str, Any]] = []
    addrs = {a for m in src.machines for a in src.addrs_of(m)}
    names = {_lower(m.get("label")).split(".")[0] for m in src.machines if m.get("label")}
    for h in src.identity:
        fqdn = _lower(h.get("fqdn"))
        if fqdn and fqdn.split(".")[0] not in names:
            out.append({"kind": "identity", "id": h.get("fqdn"), "label": h.get("fqdn"),
                        "why": "no machine in the estate answers to this name",
                        "action": {"cap": "identity.host.delete", "args": {"fqdn": h.get("fqdn"), "updatedns": True}}})
    for h in src.ssh_hosts:
        host = _lower(h.get("host"))
        if not host or host in _KEEP_LOGINS:
            continue
        if host not in addrs and _lower(h.get("label")).split(".")[0] not in names:
            out.append({"kind": "host", "id": h.get("id"), "label": f"{h.get('label')} ({h.get('host')})",
                        "why": "no machine has this address",
                        "action": {"cap": "exec.ssh.hosts.delete", "args": {"id": h.get("id")}}})
    for x in src.mesh:
        host = _lower(x.get("host"))
        if x.get("host_id") and not src.machine_by_host_id(x["host_id"]) and host not in addrs:
            out.append({"kind": "mesh", "id": x.get("host_id"), "label": x.get("label") or x.get("host"),
                        "why": "its login is gone and no machine has its address",
                        "action": {"cap": "netsec.mesh.leave", "args": {"host_id": x.get("host_id")}}})
    return out


def forget_plan(src: Sources, vmid: Any = None, name: str = "", addrs: Any = None) -> List[Dict[str, Any]]:
    """What to remove when one guest is destroyed: its logins (by guest tag,
    label or address), its directory host (by name), its mesh membership."""
    v = str(vmid or "").strip()
    n = _lower(name).split(".")[0]
    want_addrs = {_lower(a) for a in (addrs or []) if a}
    out: List[Dict[str, Any]] = []
    for h in src.ssh_hosts:
        tags = [str(t) for t in h.get("tags") or []]
        hit = (v and any(t.startswith("guest:") and t.split(":")[-1] == v for t in tags)) \
            or (v and _lower(h.get("label")).startswith(f"pve:{v}@")) \
            or (n and _lower(h.get("label")).split(".")[0].split(" ")[0] == n) \
            or (_lower(h.get("host")) in want_addrs and _lower(h.get("host")) not in _KEEP_LOGINS)
        if hit:
            out.append({"kind": "host", "id": h.get("id"), "label": h.get("label"),
                        "action": {"cap": "exec.ssh.hosts.delete", "args": {"id": h.get("id")}}})
    for h in src.identity:
        if n and _lower(h.get("fqdn")).split(".")[0] == n:
            out.append({"kind": "identity", "id": h.get("fqdn"), "label": h.get("fqdn"),
                        "action": {"cap": "identity.host.delete", "args": {"fqdn": h.get("fqdn"), "updatedns": True}}})
    login_ids = {o["id"] for o in out if o["kind"] == "host"}
    for x in src.mesh:
        if x.get("host_id") in login_ids or (_lower(x.get("host")) in want_addrs):
            out.append({"kind": "mesh", "id": x.get("host_id"), "label": x.get("label"),
                        "action": {"cap": "netsec.mesh.leave", "args": {"host_id": x.get("host_id")}}})
    # the mesh must leave before its login is deleted: leave() tears the tunnel down over that login
    out.sort(key=lambda o: {"mesh": 0, "identity": 1, "host": 2}[o["kind"]])
    return out
