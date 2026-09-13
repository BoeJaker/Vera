"""One list of every machine in the estate, for the Estate tab's Machines pane.

Pure rules, no app imports (tests/test_estate_machines_core.py). The input is
nodes.list's rows plus every guest Proxmox reports (with the static IPs from
each guest's config); the output is one row per machine with its state and the
actions that apply to it.

Why a join and not nodes.list alone: nodes.list only links guests that were
enrolled through Proxmox, and it carries no run state. On 12 Sep 2026 it linked
none of the 57 guests, while 27 SSH host rows described many of those same
machines (VFS-02, LLM, the Ollama containers), and several machines had two or
three saved logins. So SSH hosts are folded into their guest by IP, then by
name, and repeated logins for one machine collapse into one row.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

KINDS = ("proxmox-node", "guest", "host", "docker-host")
KIND_LABELS = {"proxmox-node": "Proxmox node", "guest": "Guest", "host": "Host",
               "docker-host": "Docker host"}

_IP = re.compile(r"(?:^|,)ip=(\d{1,3}(?:\.\d{1,3}){3})")
_NET_KEY = re.compile(r"^(net|ipconfig)\d+$")


def kind_of(node: Mapping[str, Any]) -> str:
    px = node.get("proxmox") or {}
    if px.get("kind") == "node":
        return "proxmox-node"
    if px.get("kind") == "guest":
        return "guest"
    if node.get("docker_host_id") and not node.get("ssh_host_id"):
        return "docker-host"
    return "host"


def guest_ips(config: Any) -> List[str]:
    """Static IPv4 addresses from a guest config: LXC netN ip=..., QEMU
    cloud-init ipconfigN ip=... DHCP guests have none."""
    if not isinstance(config, Mapping):
        return []
    found = set()
    for key, value in config.items():
        if _NET_KEY.match(str(key)):
            found.update(_IP.findall(str(value)))
    return sorted(found)


def _norm_name(name: Any) -> str:
    s = str(name or "").strip().lower()
    s = re.sub(r"\s*\(.*\)$", "", s)
    s = re.sub(r"\.(vera\.(int|lab)|local|lan)$", "", s)
    return s.strip()


def hardware(hw: Mapping[str, Any]) -> List[str]:
    hw = hw or {}
    bits = []
    if hw.get("gpu_name"):
        count = hw.get("gpu_count") or 0
        bits.append(str(hw["gpu_name"]) + (f" x{count}" if count > 1 else ""))
    if hw.get("cpu_cores"):
        bits.append(f"{hw['cpu_cores']} cores")
    if hw.get("ram_gb"):
        bits.append(f"{round(float(hw['ram_gb']))} GB RAM")
    return bits


def _guest_hardware(g: Mapping[str, Any]) -> List[str]:
    bits = []
    if g.get("maxcpu"):
        bits.append(f"{g['maxcpu']} cores")
    if g.get("maxmem"):
        bits.append(f"{round(int(g['maxmem']) / 2 ** 30)} GB RAM")
    return bits


def runs(node: Mapping[str, Any]) -> List[str]:
    out = [f"ollama {o.get('label') or o.get('id')}" for o in node.get("ollama") or []]
    out += [f"vLLM {v.get('label') or v.get('id')}" for v in node.get("vllm") or []]
    return out


def actions(row: Mapping[str, Any]) -> List[Dict[str, str]]:
    """What the Machines pane may offer for this row, in display order."""
    kind, status = row.get("kind"), row.get("status")
    if kind == "guest" and (row.get("template") or status == "template"):
        return []
    acts: List[Dict[str, str]] = []
    manageable = (kind == "guest" and row.get("cluster_id") and row.get("node")
                  and row.get("type") in ("lxc", "qemu") and row.get("vmid"))
    if manageable:
        if status == "running":
            acts += [{"id": "console", "label": "Console",
                      "mode": "term" if row["type"] == "lxc" else "vnc"},
                     {"id": "shutdown", "label": "Shut down"},
                     {"id": "reboot", "label": "Reboot"}]
        elif status == "stopped":
            acts.append({"id": "start", "label": "Start"})
    reachable = not (kind == "guest" and status == "stopped")
    if reachable and (row.get("ssh_host_id") or (manageable and status == "running")):
        acts.append({"id": "ssh", "label": "Terminal"})
    if kind == "proxmox-node":
        acts.append({"id": "cpu", "label": "CPU / NUMA"})
    if reachable and row.get("ssh_host_id"):
        acts.append({"id": "detect", "label": "Detect"})
    if row.get("docker_host_id"):
        acts.append({"id": "containers", "label": "Containers"})
    return acts


def _node_row(n: Mapping[str, Any]) -> Dict[str, Any]:
    px = n.get("proxmox") or {}
    return {"id": n.get("id"), "label": n.get("label") or n.get("id"), "kind": kind_of(n),
            "status": "", "addr": n.get("addr") or "",
            "cluster_id": px.get("cluster_id") or "", "node": px.get("node") or "",
            "vmid": px.get("vmid"), "type": "", "template": False, "ips": [],
            "ssh_host_id": n.get("ssh_host_id") or "", "docker_host_id": n.get("docker_host_id") or "",
            "ssh_label": n.get("label") or "", "logins": 1 if n.get("ssh_host_id") else 0,
            "hardware": hardware(n.get("hw")), "runs": runs(n),
            "backends": list(n.get("backends") or []), "note": "", "merged_ids": []}


def _guest_row(g: Mapping[str, Any]) -> Dict[str, Any]:
    vmid = int(g["vmid"])
    template = bool(g.get("template"))
    # LAN addresses first: VFS-02 also holds 10.33.33.11 on the storage network.
    ips = sorted(g.get("ips") or [], key=lambda ip: (not ip.startswith("192.168."), ip))
    return {"id": f"pve:{g.get('cluster_id') or ''}:{vmid}", "label": g.get("name") or f"guest {vmid}",
            "kind": "guest", "status": "template" if template else (g.get("status") or ""),
            "addr": ips[0] if ips else "", "cluster_id": g.get("cluster_id") or "",
            "node": g.get("node") or "", "vmid": vmid, "type": g.get("type") or "",
            "template": template, "ips": ips, "ssh_host_id": "", "docker_host_id": "",
            "ssh_label": "", "logins": 0, "hardware": _guest_hardware(g), "runs": [],
            "backends": ["proxmox"], "note": "not enrolled for SSH", "merged_ids": []}


def _absorb(target: Dict[str, Any], row: Mapping[str, Any]) -> None:
    """Fold another record of the same machine into `target`."""
    if row.get("ssh_host_id") and not target["ssh_host_id"]:
        target["ssh_host_id"] = row["ssh_host_id"]
        target["ssh_label"] = row.get("label") or ""
    target["logins"] += int(row.get("logins") or 0)
    target["docker_host_id"] = target["docker_host_id"] or row.get("docker_host_id") or ""
    if row.get("addr") and (not target["addr"] or row["addr"] in target.get("ips", [])):
        target["addr"] = row["addr"]
    if row.get("hardware") and (target["kind"] == "guest" or not target["hardware"]):
        target["hardware"] = list(row["hardware"])
    for r in row.get("runs") or []:
        if r not in target["runs"]:
            target["runs"].append(r)
    for b in row.get("backends") or []:
        if b not in target["backends"]:
            target["backends"].append(b)
    if target["kind"] == "guest" and target["ssh_host_id"]:
        target["note"] = ""
    target["merged_ids"].append(row.get("id"))


def _pick(candidates: List[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if not candidates:
        return None
    return sorted(candidates, key=lambda g: (g.get("status") != "running", int(g["vmid"])))[0]


def _match_guest(host: Mapping[str, Any], by_ip: Dict[str, List], by_name: Dict[str, List]):
    addr = host.get("addr") or ""
    if addr in by_ip:
        return _pick(by_ip[addr])
    same_name = [g for g in by_name.get(_norm_name(host.get("label")), [])
                 if not g.get("ips") or addr in g["ips"]]
    return _pick(same_name)


def _rank(row: Mapping[str, Any]):
    status = row.get("status")
    state = 0 if status == "running" else 1 if status in ("stopped", "online") else 2
    return (KINDS.index(row["kind"]), state, str(row.get("label") or "").lower())


def build_machines(nodes: Iterable[Mapping[str, Any]],
                   guests: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """nodes: nodes.list rows. guests: Proxmox guests, each carrying the id of
    the cluster record that answered for its API host and `ips` from its config."""
    guests = [g for g in guests if g.get("vmid") is not None and not g.get("template")] + \
             [g for g in guests if g.get("vmid") is not None and g.get("template")]
    guest_rows: Dict[int, Dict[str, Any]] = {}
    by_ip: Dict[str, List] = {}
    by_name: Dict[str, List] = {}
    for g in guests:
        vmid = int(g["vmid"])
        if vmid in guest_rows:
            continue
        guest_rows[vmid] = _guest_row(g)
        if g.get("template"):
            continue
        for ip in g.get("ips") or []:
            by_ip.setdefault(ip, []).append(g)
        by_name.setdefault(_norm_name(g.get("name")), []).append(g)

    others: List[Dict[str, Any]] = []
    for n in nodes:
        row = _node_row(n)
        if row["kind"] == "guest":
            vmid = int(row.get("vmid") or 0)
            if vmid in guest_rows:
                _absorb(guest_rows[vmid], row)
                continue
            row["status"] = "unknown"
            row["note"] = "enrolled for SSH, but Proxmox does not list this guest"
        elif row["kind"] == "host":
            g = _match_guest(row, by_ip, by_name)
            if g is not None:
                _absorb(guest_rows[int(g["vmid"])], row)
                continue
        others.append(row)

    hosts: Dict[tuple, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = list(guest_rows.values())
    for row in others:
        if row["kind"] != "host":
            rows.append(row)
            continue
        key = (_norm_name(row["label"]), row["addr"])
        if key in hosts:
            _absorb(hosts[key], row)
        else:
            hosts[key] = row
            rows.append(row)

    for row in rows:
        extra = []
        # An address is reused: a login saved for an old machine now reaches this one,
        # and its credentials may not work here.
        if (row["kind"] == "guest" and row["ssh_label"] and not row["ssh_label"].startswith("pve")
                and _norm_name(row["ssh_label"]) != _norm_name(row["label"])):
            extra.append(f"SSH login saved as {row['ssh_label']}")
        if row["logins"] > 1:
            extra.append(f"{row['logins']} saved logins")
        if extra:
            row["note"] = "; ".join(([row["note"]] if row["note"] else []) + extra)
        row["actions"] = actions(row)
    rows.sort(key=_rank)
    return rows


def counts(rows: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    rows = list(rows)
    out = {k: 0 for k in KINDS}
    for r in rows:
        out[r["kind"]] += 1
    guests = [r for r in rows if r["kind"] == "guest"]
    out.update(machines=len(rows),
               running_guests=sum(1 for r in guests if r.get("status") == "running"),
               stopped_guests=sum(1 for r in guests if r.get("status") == "stopped"),
               templates=sum(1 for r in guests if r.get("template")))
    return out
