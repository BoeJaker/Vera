"""
operator_capabilities.py — Vera Operator system (Phase 3)
=========================================================

Turns a saved connection (remote_capabilities.conn.*) into an administer-able
target with high-level, agent- and UI-friendly verbs that work the SAME way on
a Docker container, an LXC/VM guest, or a bare SSH host. Every verb is a thin,
OS-portable wrapper over `conn.exec` (which already unifies docker exec / ssh
run), so a specialist loop can "operate this VM" without hand-rolling shell.

  operator.sysinfo    — OS, kernel, uptime, CPU/mem, disk usage (one round-trip)
  operator.processes  — top processes by CPU/mem
  operator.ports      — listening TCP ports (ss / netstat)
  operator.services   — list system services (systemd or SysV)
  operator.service    — start/stop/restart/status a service
  operator.pkg        — detect the package manager and install/update/remove pkgs

Paired with the `operator` loop profile (dag/loop_profiles.py), which pins the
whole remote toolkit onto the `infra-operator` specialist agent so a goal like
"update packages on debian-vm and restart nginx" runs end-to-end.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, enum_schema,
)

log = logging.getLogger("vera.remote.operator")


def _cap_raw(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


async def _op_exec(conn_id: str, command: str, *, kind: str = "",
                   docker_host_id: str = "", container: str = "",
                   ssh_host_id: str = "", timeout: int = 60) -> Dict:
    """Run a command against a connection/target via conn.exec."""
    fn = _cap_raw("conn.exec")
    if not fn:
        return {"ok": False, "error": "conn.exec unavailable (remote module not loaded)"}
    if not (conn_id or container or ssh_host_id):
        return {"ok": False, "error": "conn_id (or a target) required"}
    return await fn(conn_id=conn_id, kind=kind, docker_host_id=docker_host_id,
                    container=container, ssh_host_id=ssh_host_id,
                    command=command, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
_TARGET_DOC = ("conn_id (str) OR ad-hoc target (kind + docker_host_id/container "
               "or ssh_host_id)")


@capability(
    "operator.sysinfo",
    http_method="POST", http_path="/remote/operator/sysinfo", http_tags=["remote", "operator"],
    memory="off",
    description="One-shot health snapshot of a target: OS, kernel, uptime, CPU "
                "count, memory and disk usage. Inputs: " + _TARGET_DOC + ". "
                "Output: {ok, os, kernel, uptime, cpus, mem, disks:[...], raw}.",
)
async def cap_operator_sysinfo(conn_id: str = "", kind: str = "",
                               docker_host_id: str = "", container: str = "",
                               ssh_host_id: str = "", trace_id=None) -> Dict:
    # Single script — a series of labelled sections we split server-side. Uses
    # only widely-available tools; each line tolerates absence.
    script = (
        "echo '#OS'; (. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\") || uname -s; "
        "echo '#KERNEL'; uname -r 2>/dev/null; "
        "echo '#UPTIME'; uptime 2>/dev/null | sed 's/^ *//'; "
        "echo '#CPUS'; (nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null); "
        "echo '#MEM'; (free -m 2>/dev/null | awk '/Mem:/{print $2\" \"$3\" \"$7}'); "
        "echo '#DISK'; df -hP 2>/dev/null | awk 'NR>1{print $6\" \"$2\" \"$5}'"
    )
    res = await _op_exec(conn_id, script, kind=kind, docker_host_id=docker_host_id,
                         container=container, ssh_host_id=ssh_host_id, timeout=30)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "failed"}
    out = res.get("stdout", "") or ""
    sect: Dict[str, List[str]] = {}
    cur = ""
    for line in out.splitlines():
        if line.startswith("#") and line[1:] in ("OS", "KERNEL", "UPTIME", "CPUS", "MEM", "DISK"):
            cur = line[1:]
            sect[cur] = []
        elif cur:
            if line.strip():
                sect[cur].append(line.strip())
    mem = {}
    if sect.get("MEM"):
        parts = sect["MEM"][0].split()
        if len(parts) >= 3:
            mem = {"total_mb": _int(parts[0]), "used_mb": _int(parts[1]),
                   "available_mb": _int(parts[2])}
    disks = []
    for d in sect.get("DISK", []):
        p = d.split()
        if len(p) >= 3:
            disks.append({"mount": p[0], "size": p[1], "use_pct": p[2]})
    return {"ok": True,
            "os": (sect.get("OS") or [""])[0], "kernel": (sect.get("KERNEL") or [""])[0],
            "uptime": (sect.get("UPTIME") or [""])[0],
            "cpus": _int((sect.get("CPUS") or ["0"])[0]), "mem": mem, "disks": disks,
            "raw": out[:2000]}


def _int(s):
    try:
        return int(re.sub(r"[^0-9-]", "", str(s)) or 0)
    except Exception:
        return 0


@capability(
    "operator.processes",
    http_method="POST", http_path="/remote/operator/processes", http_tags=["remote", "operator"],
    memory="off",
    description="Top processes on a target by CPU. Inputs: " + _TARGET_DOC + ", "
                "sort ('cpu'|'mem'), limit (int=15). Output: {ok, processes:[{pid,"
                "user,cpu,mem,command}]}.",
    schema=enum_schema(sort=["cpu", "mem"]),
)
async def cap_operator_processes(conn_id: str = "", kind: str = "",
                                 docker_host_id: str = "", container: str = "",
                                 ssh_host_id: str = "", sort: str = "cpu",
                                 limit: int = 15, trace_id=None) -> Dict:
    key = "-%mem" if sort == "mem" else "-%cpu"
    n = max(1, min(int(limit or 15), 100))
    # `ps aux --sort` is GNU; busybox ps has no sort → fall back to plain ps.
    cmd = (f"ps aux --sort={key} 2>/dev/null | head -n {n + 1} || "
           f"ps -o pid,user,pcpu,pmem,args 2>/dev/null | head -n {n + 1} || ps aux | head -n {n + 1}")
    res = await _op_exec(conn_id, cmd, kind=kind, docker_host_id=docker_host_id,
                         container=container, ssh_host_id=ssh_host_id, timeout=25)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "failed"}
    procs = []
    lines = (res.get("stdout", "") or "").splitlines()
    for line in lines[1:]:  # skip header
        p = line.split(None, 10)
        if len(p) < 5:
            continue
        # ps aux: USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND
        if len(p) >= 11:
            procs.append({"pid": p[1], "user": p[0], "cpu": p[2], "mem": p[3],
                          "command": p[10][:200]})
        else:
            procs.append({"pid": p[0], "user": p[1] if len(p) > 1 else "",
                          "cpu": p[2] if len(p) > 2 else "", "mem": p[3] if len(p) > 3 else "",
                          "command": p[-1][:200]})
    return {"ok": True, "processes": procs}


@capability(
    "operator.ports",
    http_method="POST", http_path="/remote/operator/ports", http_tags=["remote", "operator"],
    memory="off",
    description="Listening TCP ports on a target (ss or netstat). Inputs: "
                + _TARGET_DOC + ". Output: {ok, ports:[{proto,local,process}]}.",
)
async def cap_operator_ports(conn_id: str = "", kind: str = "",
                             docker_host_id: str = "", container: str = "",
                             ssh_host_id: str = "", trace_id=None) -> Dict:
    cmd = ("ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null || ss -tln 2>/dev/null "
           "|| netstat -tln 2>/dev/null")
    res = await _op_exec(conn_id, cmd, kind=kind, docker_host_id=docker_host_id,
                         container=container, ssh_host_id=ssh_host_id, timeout=25)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "failed"}
    ports = []
    for line in (res.get("stdout", "") or "").splitlines():
        if "LISTEN" not in line:
            continue
        m = re.search(r"(\d+\.\d+\.\d+\.\d+|\[[0-9a-f:]+\]|\*|::):(\d+)", line)
        proc = ""
        pm = re.search(r'users:\(\("([^"]+)"', line) or re.search(r"(\d+)/(\S+)\s*$", line)
        if pm:
            proc = pm.group(1) if pm.lastindex == 1 else pm.group(2)
        if m:
            ports.append({"proto": "tcp", "local": m.group(0), "port": _int(m.group(2)),
                          "process": proc})
    ports.sort(key=lambda p: p.get("port", 0))
    return {"ok": True, "ports": ports}


@capability(
    "operator.services",
    http_method="POST", http_path="/remote/operator/services", http_tags=["remote", "operator"],
    memory="off",
    description="List system services on a target (systemd units, else SysV). "
                "Inputs: " + _TARGET_DOC + ", running_only (bool=false). "
                "Output: {ok, manager, services:[{name,state,desc}]}.",
)
async def cap_operator_services(conn_id: str = "", kind: str = "",
                                docker_host_id: str = "", container: str = "",
                                ssh_host_id: str = "", running_only: bool = False,
                                trace_id=None) -> Dict:
    cmd = ("if command -v systemctl >/dev/null 2>&1; then echo '#systemd'; "
           "systemctl list-units --type=service --all --no-pager --plain 2>/dev/null "
           "| awk 'NF>=4 && $1 ~ /\\.service$/ {d=\"\"; for(i=5;i<=NF;i++)d=d$i\" \"; "
           "print $1\"|\"$3\"|\"$4\"|\"d}'; "
           "else echo '#sysv'; service --status-all 2>&1; fi")
    res = await _op_exec(conn_id, cmd, kind=kind, docker_host_id=docker_host_id,
                         container=container, ssh_host_id=ssh_host_id, timeout=30)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "failed"}
    lines = (res.get("stdout", "") or "").splitlines()
    manager = "systemd" if (lines and lines[0].strip() == "#systemd") else "sysv"
    services = []
    for line in lines[1:]:
        if manager == "systemd" and "|" in line:
            parts = line.split("|")
            name = parts[0].strip()
            active = parts[1].strip() if len(parts) > 1 else ""
            sub = parts[2].strip() if len(parts) > 2 else ""
            desc = parts[3].strip() if len(parts) > 3 else ""
            if running_only and sub != "running":
                continue
            services.append({"name": name, "state": sub or active, "desc": desc})
        elif manager == "sysv" and line.strip():
            m = re.search(r"\[\s*([+\-?])\s*\]\s+(\S+)", line)
            if m:
                st = {"+": "running", "-": "stopped"}.get(m.group(1), "unknown")
                if running_only and st != "running":
                    continue
                services.append({"name": m.group(2), "state": st, "desc": ""})
    return {"ok": True, "manager": manager, "services": services}


@capability(
    "operator.service",
    http_method="POST", http_path="/remote/operator/service", http_tags=["remote", "operator"],
    description="Control a system service on a target (systemctl, else `service`). "
                "Inputs: " + _TARGET_DOC + ", name (str!), action "
                "('status'|'start'|'stop'|'restart'|'reload'|'enable'|'disable'). "
                "Output: {ok, rc, stdout, stderr}.",
    schema=enum_schema(action=["status", "start", "stop", "restart", "reload",
                               "enable", "disable"]),
)
async def cap_operator_service(conn_id: str = "", kind: str = "",
                               docker_host_id: str = "", container: str = "",
                               ssh_host_id: str = "", name: str = "",
                               action: str = "status", trace_id=None) -> Dict:
    if not name:
        return {"ok": False, "error": "service name required"}
    if action not in ("status", "start", "stop", "restart", "reload", "enable", "disable"):
        return {"ok": False, "error": "invalid action"}
    q = shlex.quote(name)
    cmd = (f"if command -v systemctl >/dev/null 2>&1; then systemctl {action} {q}; "
           f"else service {q} {action}; fi")
    res = await _op_exec(conn_id, cmd, kind=kind, docker_host_id=docker_host_id,
                         container=container, ssh_host_id=ssh_host_id, timeout=60)
    await emit_event({"type": "remote.operator.service", "name": name, "action": action,
                      "ok": res.get("ok", False)})
    return {"ok": res.get("ok", False), "rc": res.get("rc"),
            "stdout": (res.get("stdout", "") or "")[:4000],
            "stderr": (res.get("stderr", "") or "")[:2000]}


_PKG_MGRS = [
    ("apt-get", {"install": "apt-get install -y", "remove": "apt-get remove -y",
                 "update": "apt-get update && apt-get upgrade -y"}),
    ("dnf", {"install": "dnf install -y", "remove": "dnf remove -y", "update": "dnf upgrade -y"}),
    ("yum", {"install": "yum install -y", "remove": "yum remove -y", "update": "yum update -y"}),
    ("apk", {"install": "apk add", "remove": "apk del", "update": "apk update && apk upgrade"}),
    ("pacman", {"install": "pacman -S --noconfirm", "remove": "pacman -R --noconfirm",
                "update": "pacman -Syu --noconfirm"}),
    ("zypper", {"install": "zypper -n install", "remove": "zypper -n remove", "update": "zypper -n update"}),
]


@capability(
    "operator.pkg",
    http_method="POST", http_path="/remote/operator/pkg", http_tags=["remote", "operator"],
    description="Install / remove / update packages on a target, auto-detecting "
                "the package manager (apt/dnf/yum/apk/pacman/zypper). Prefixes sudo "
                "when not root. Inputs: " + _TARGET_DOC + ", action "
                "('install'|'remove'|'update'|'detect'), packages (str — space-sep, "
                "not needed for update/detect), timeout (int=600). "
                "Output: {ok, manager, rc, stdout, stderr}.",
    schema=enum_schema(action=["install", "remove", "update", "detect"]),
)
async def cap_operator_pkg(conn_id: str = "", kind: str = "", docker_host_id: str = "",
                           container: str = "", ssh_host_id: str = "",
                           action: str = "detect", packages: str = "",
                           timeout: int = 600, trace_id=None) -> Dict:
    tgt = dict(kind=kind, docker_host_id=docker_host_id, container=container,
               ssh_host_id=ssh_host_id)
    # Detect manager + whether we are root.
    probe = ("id -u; " + "; ".join(
        f"command -v {m} >/dev/null 2>&1 && echo MGR={m}" for m, _ in _PKG_MGRS))
    det = await _op_exec(conn_id, probe, timeout=20, **tgt)
    if not det.get("ok"):
        return {"ok": False, "error": det.get("stderr") or det.get("error") or "detect failed"}
    dout = (det.get("stdout", "") or "").splitlines()
    uid = _int(dout[0]) if dout else 1000
    mgr = ""
    for line in dout:
        if line.startswith("MGR="):
            mgr = line[4:].strip()
            break
    if action == "detect":
        return {"ok": bool(mgr), "manager": mgr, "root": uid == 0}
    if not mgr:
        return {"ok": False, "error": "no supported package manager found"}
    verbs = dict(_PKG_MGRS)[mgr]
    if action in ("install", "remove") and not packages.strip():
        return {"ok": False, "error": f"packages required for {action}"}
    base = verbs[action]
    if action in ("install", "remove"):
        base = f"{base} {packages.strip()}"
    sudo = "" if uid == 0 else "sudo "
    cmd = sudo + base
    res = await _op_exec(conn_id, cmd, timeout=int(timeout), **tgt)
    await emit_event({"type": "remote.operator.pkg", "manager": mgr, "action": action,
                      "packages": packages, "ok": res.get("ok", False)})
    return {"ok": res.get("ok", False), "manager": mgr, "rc": res.get("rc"),
            "stdout": (res.get("stdout", "") or "")[-6000:],
            "stderr": (res.get("stderr", "") or "")[-2000:]}


log.info("operator_capabilities loaded — sysinfo/processes/ports/services/service/pkg")
