"""
software_capabilities.py — Vera remote software provisioning
============================================================

Install runtimes on an *already-reachable* host over SSH and wire the result
straight into Vera's cluster — "install vLLM/Ollama/Docker on a box and connect
to it" from one screen.

It is deliberately thin: it reuses the existing SSH execution + credential
store (`exec.ssh.run` / `exec.ssh.hosts.*`, the same store Docker's ssh hosts
use) and the existing runtime registries, so nothing here re-implements SSH or
node management:

  • detect/install run over `exec.ssh.run` with a stored `host_id`.
  • Ollama nodes register via `ollama.add_instance` (persisted, pinged).
  • vLLM endpoints register via `vllm.instances.add`.
  • Docker hosts register via `docker.hosts.save(kind='ssh', ssh_host_id=…)`.

Capabilities (group `provision.*`)
──────────────────────────────────
  provision.targets   — catalog of installable runtimes (+ command preview)
  provision.detect    — SSH probe: OS, package manager, GPU, what's installed
  provision.install   — run the install script for one target over SSH
  provision.serve     — (vLLM) launch an OpenAI server for a model over SSH
  provision.connect   — register the resulting endpoint into Vera's cluster
  provision.run       — install (+ optional serve) then connect, one shot

The host must already exist in the SSH credential store (Proxmox › Enroll, or
the "+ SSH host" form in this panel). Installs need root or passwordless sudo.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, register_ui,
)

log = logging.getLogger("vera.provision")
_HERE = Path(__file__).parent


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


# ═════════════════════════════════════════════════════════════════════════════
#  CATALOG
# ═════════════════════════════════════════════════════════════════════════════
# Each target: how to install it, the port it listens on, and how (if at all) it
# gets registered into a Vera runtime registry after install. Command templates
# take {sudo} ('' or 'sudo ') and {port}. They end with VERA_PROVISION_DONE so
# we can confirm the script reached the end even when a trailing step warns.
_OLLAMA_INSTALL = (
    "curl -fsSL https://ollama.com/install.sh | sh && "
    "{sudo}mkdir -p /etc/systemd/system/ollama.service.d && "
    "printf '[Service]\\nEnvironment=\"OLLAMA_HOST=0.0.0.0:{port}\"\\n' "
    "| {sudo}tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null && "
    "{sudo}systemctl daemon-reload && {sudo}systemctl restart ollama && "
    "echo VERA_PROVISION_DONE"
)
_DOCKER_INSTALL = (
    "curl -fsSL https://get.docker.com | sh && "
    "{sudo}systemctl enable --now docker && "
    "echo VERA_PROVISION_DONE"
)
# vLLM in a self-contained user venv — no sudo, no system Python pollution.
_VLLM_INSTALL = (
    'python3 -m venv "$HOME/.vera/vllm" && '
    '"$HOME/.vera/vllm/bin/pip" install -U pip wheel setuptools && '
    '"$HOME/.vera/vllm/bin/pip" install vllm && '
    '"$HOME/.vera/vllm/bin/vllm" --version && '
    'echo VERA_PROVISION_DONE'
)
# NVIDIA container toolkit (Debian/Ubuntu) — lets Docker use the GPU.
_NVIDIA_INSTALL = (
    "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey "
    "| {sudo}gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && "
    "curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list "
    "| sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' "
    "| {sudo}tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null && "
    "{sudo}apt-get update && {sudo}apt-get install -y nvidia-container-toolkit && "
    "{sudo}nvidia-ctk runtime configure --runtime=docker && {sudo}systemctl restart docker && "
    "echo VERA_PROVISION_DONE"
)

_TARGETS: Dict[str, Dict[str, Any]] = {
    "ollama": {
        "label": "Ollama", "default_port": 11434, "register": "ollama",
        "install": _OLLAMA_INSTALL, "needs_sudo": True,
        "desc": "Ollama LLM server, bound to 0.0.0.0 and registered as a cluster node.",
    },
    "vllm": {
        "label": "vLLM", "default_port": 8001, "register": "vllm",
        "install": _VLLM_INSTALL, "needs_sudo": False,
        "desc": "vLLM in a user venv (~/.vera/vllm). Use provision.serve to launch a model, then connect.",
    },
    "docker": {
        "label": "Docker", "default_port": 2375, "register": "docker",
        "install": _DOCKER_INSTALL, "needs_sudo": True,
        "desc": "Docker Engine, registered as an SSH-managed Docker host (no exposed TCP socket needed).",
    },
    "nvidia": {
        "label": "NVIDIA Container Toolkit", "default_port": 0, "register": "",
        "install": _NVIDIA_INSTALL, "needs_sudo": True,
        "desc": "GPU support for Docker (Debian/Ubuntu). Install Docker first.",
    },
}

_DETECT = (
    "echo \"os=$(. /etc/os-release 2>/dev/null; echo ${ID:-unknown} ${VERSION_ID:-})\"; "
    "echo \"kernel=$(uname -sr)\"; "
    "echo \"arch=$(uname -m)\"; "
    "echo \"pkg=$(command -v apt-get>/dev/null&&echo apt||command -v dnf>/dev/null&&echo dnf"
    "||command -v yum>/dev/null&&echo yum||command -v zypper>/dev/null&&echo zypper"
    "||command -v pacman>/dev/null&&echo pacman||echo unknown)\"; "
    "echo \"sudo=$(command -v sudo>/dev/null&&echo yes||echo no)\"; "
    "echo \"gpu=$(command -v nvidia-smi>/dev/null&&nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null|head -1||echo none)\"; "
    "echo \"docker=$(command -v docker>/dev/null&&docker --version 2>/dev/null||echo no)\"; "
    "echo \"ollama=$(command -v ollama>/dev/null&&ollama --version 2>/dev/null||echo no)\"; "
    "echo \"vllm=$([ -x \"$HOME/.vera/vllm/bin/vllm\" ]&&\"$HOME/.vera/vllm/bin/vllm\" --version 2>/dev/null|head -1||command -v vllm>/dev/null&&vllm --version 2>/dev/null|head -1||echo no)\"; "
    "echo \"python=$(command -v python3>/dev/null&&python3 --version 2>/dev/null||echo no)\""
)


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════
async def _host_rec(host_id: str) -> Optional[Dict]:
    """Resolve a stored SSH host (redacted) by id from the exec SSH store."""
    lst = _cap("exec.ssh.hosts.list")
    if not lst:
        return None
    try:
        hosts = (await lst()).get("hosts", [])
    except Exception:
        return None
    for h in hosts:
        if h.get("id") == host_id:
            return h
    return None


def _sudo_for(rec: Optional[Dict], want_sudo: bool) -> str:
    """'' when logging in as root (or sudo unwanted), else 'sudo '."""
    user = (rec or {}).get("user", "")
    if user == "root" or not want_sudo:
        return ""
    return "sudo "


async def _ssh(host_id: str, command: str, timeout: int = 120) -> Dict:
    run = _cap("exec.ssh.run")
    if not run:
        return {"ok": False, "error": "exec.ssh.run unavailable (execution module not loaded)",
                "rc": -1, "stdout": "", "stderr": ""}
    res = await run(command=command, host_id=host_id, timeout=timeout)
    return res or {"ok": False, "error": "no response from exec.ssh.run",
                   "rc": -1, "stdout": "", "stderr": ""}


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "provision.targets",
    http_method="GET", http_path="/provision/targets", http_tags=["provision"],
    memory="off", silent=True,
    description="List installable runtimes Vera can provision over SSH. Output: "
                "{targets:[{key,label,desc,default_port,register,needs_sudo}]}.",
)
async def cap_targets(trace_id=None) -> Dict:
    return {"targets": [
        {"key": k, "label": t["label"], "desc": t["desc"],
         "default_port": t["default_port"], "register": t["register"],
         "needs_sudo": t["needs_sudo"]}
        for k, t in _TARGETS.items()
    ]}


@capability(
    "provision.detect",
    http_method="POST", http_path="/provision/detect", http_tags=["provision"],
    memory="off",
    description="SSH-probe a stored host for OS, package manager, GPU and which "
                "runtimes are already installed. Input: host_id (str!). Output: "
                "{ok, host, info:{os,kernel,arch,pkg,sudo,gpu,docker,ollama,vllm,"
                "python}, raw}.",
)
async def cap_detect(host_id: str = "", trace_id=None) -> Dict:
    if not host_id:
        return {"ok": False, "error": "host_id required"}
    rec = await _host_rec(host_id)
    if not rec:
        return {"ok": False, "error": f"host_id not found: {host_id}"}
    res = await _ssh(host_id, _DETECT, timeout=30)
    info = _parse_kv(res.get("stdout", ""))
    return {"ok": bool(res.get("ok")), "host": rec.get("host", ""),
            "info": info, "raw": res.get("stdout", ""),
            "error": res.get("error") or res.get("stderr", "")}


@capability(
    "provision.install",
    http_method="POST", http_path="/provision/install", http_tags=["provision"],
    memory="off",
    description="Install a runtime on a stored host over SSH. Inputs: host_id "
                "(str!), target ('ollama'|'vllm'|'docker'|'nvidia'), sudo (bool="
                "true — prefix privileged steps with sudo unless the login is "
                "root), port (int — override the listen port), timeout (int=900). "
                "Output: {ok, rc, target, done(reached end marker), log(tail)}. "
                "Long downloads may exceed the timeout — re-run is idempotent.",
)
async def cap_install(host_id: str = "", target: str = "", sudo: bool = True,
                      port: int = 0, timeout: int = 900, trace_id=None) -> Dict:
    if not host_id or target not in _TARGETS:
        return {"ok": False, "error": "host_id and a valid target are required",
                "targets": list(_TARGETS)}
    rec = await _host_rec(host_id)
    if not rec:
        return {"ok": False, "error": f"host_id not found: {host_id}"}
    t = _TARGETS[target]
    cmd = t["install"].format(sudo=_sudo_for(rec, sudo),
                              port=int(port or t["default_port"]))
    await emit_event({"type": "provision.install.start", "host": rec.get("host", ""),
                      "target": target})
    res = await _ssh(host_id, cmd, timeout=int(timeout or 900))
    out = res.get("stdout", "") or ""
    done = "VERA_PROVISION_DONE" in out
    ok = bool(res.get("ok")) and done
    await emit_event({"type": "provision.install.done", "host": rec.get("host", ""),
                      "target": target, "ok": ok})
    return {"ok": ok, "rc": res.get("rc"), "target": target, "done": done,
            "log": (out + "\n" + (res.get("stderr", "") or ""))[-4000:],
            "error": res.get("error", "") if not ok else ""}


@capability(
    "provision.serve",
    http_method="POST", http_path="/provision/serve", http_tags=["provision"],
    memory="off",
    description="Launch a vLLM OpenAI-compatible server for a model on a stored "
                "host over SSH (nohup, logs to ~/.vera/vllm/serve.log). Inputs: "
                "host_id (str!), model (str! — HF id or path), port (int=8001), "
                "gpu_mem_util (float=0.90), extra (str — extra CLI flags). Output: "
                "{ok, port, log_path}. Pair with provision.connect target='vllm'.",
)
async def cap_serve(host_id: str = "", model: str = "", port: int = 8001,
                    gpu_mem_util: float = 0.90, extra: str = "", trace_id=None) -> Dict:
    if not host_id or not model:
        return {"ok": False, "error": "host_id and model required"}
    rec = await _host_rec(host_id)
    if not rec:
        return {"ok": False, "error": f"host_id not found: {host_id}"}
    port = int(port or 8001)
    cmd = (
        'mkdir -p "$HOME/.vera/vllm" && '
        'nohup "$HOME/.vera/vllm/bin/vllm" serve ' + model +
        ' --host 0.0.0.0 --port ' + str(port) +
        ' --gpu-memory-utilization ' + str(gpu_mem_util) +
        (' ' + extra if extra else '') +
        ' > "$HOME/.vera/vllm/serve.log" 2>&1 & '
        'sleep 1 && echo "VERA_SERVE_STARTED pid=$!"'
    )
    res = await _ssh(host_id, cmd, timeout=30)
    out = res.get("stdout", "") or ""
    ok = bool(res.get("ok")) and "VERA_SERVE_STARTED" in out
    await emit_event({"type": "provision.vllm.serve", "host": rec.get("host", ""),
                      "model": model, "port": port, "ok": ok})
    return {"ok": ok, "port": port, "log_path": "~/.vera/vllm/serve.log",
            "stdout": out, "error": res.get("error") or res.get("stderr", "")}


@capability(
    "provision.connect",
    http_method="POST", http_path="/provision/connect", http_tags=["provision"],
    memory="off",
    description="Register a provisioned endpoint into Vera's cluster. Inputs: "
                "host_id (str!), target ('ollama'|'vllm'|'docker'), port (int — "
                "defaults per target), has_gpu (bool), id (str — node id), label "
                "(str), api_key (str — vLLM). Ollama→ollama.add_instance, "
                "vLLM→vllm.instances.add, Docker→docker.hosts.save(kind=ssh). "
                "Output: {ok, kind, id, url, result}.",
)
async def cap_connect(host_id: str = "", target: str = "", port: int = 0,
                      has_gpu: bool = False, id: str = "", label: str = "",
                      api_key: str = "", trace_id=None) -> Dict:
    rec = await _host_rec(host_id)
    if not rec:
        return {"ok": False, "error": f"host_id not found: {host_id}"}
    t = _TARGETS.get(target)
    if not t:
        return {"ok": False, "error": f"unknown target: {target}"}
    reg = t["register"]
    addr = rec.get("host", "")
    port = int(port or t["default_port"])
    iid = id or f"prov-{addr}-{port}".replace(".", "-")
    lbl = label or f"{addr} ({target})"

    if reg == "ollama":
        add = _cap("ollama.add_instance")
        if not add:
            return {"ok": False, "error": "ollama module not loaded"}
        url = f"http://{addr}:{port}"
        node = await add(id=iid, url=url, has_gpu=has_gpu, label=lbl)
        await emit_event({"type": "provision.connect", "kind": "ollama", "url": url})
        return {"ok": True, "kind": "ollama", "id": iid, "url": url, "result": node}

    if reg == "vllm":
        add = _cap("vllm.instances.add")
        if not add:
            return {"ok": False, "error": "vllm module not loaded"}
        url = f"http://{addr}:{port}"
        res = await add(id=iid, url=url, label=lbl, has_gpu=has_gpu, api_key=api_key)
        await emit_event({"type": "provision.connect", "kind": "vllm", "url": url})
        return {"ok": bool(res.get("ok", True)), "kind": "vllm", "id": iid,
                "url": url, "result": res}

    if reg == "docker":
        save = _cap("docker.hosts.save")
        if not save:
            return {"ok": False, "error": "docker module not loaded"}
        res = await save(kind="ssh", ssh_host_id=host_id, label=lbl)
        await emit_event({"type": "provision.connect", "kind": "docker", "host": addr})
        return {"ok": bool(res.get("ok", True)), "kind": "docker",
                "id": (res.get("host") or {}).get("id", host_id), "result": res}

    return {"ok": False, "error": f"target '{target}' is not connectable"}


@capability(
    "provision.run",
    http_method="POST", http_path="/provision/run", http_tags=["provision"],
    memory="off",
    description="One-shot: install a runtime on a stored host over SSH, then "
                "register it into the cluster. Inputs: host_id (str!), target, "
                "sudo (bool=true), port (int), has_gpu (bool), connect (bool=true "
                "— register after install), serve_model (str — vLLM: start this "
                "model before connecting), timeout (int=900). Output: {ok, install, "
                "serve, connect}.",
)
async def cap_run(host_id: str = "", target: str = "", sudo: bool = True,
                  port: int = 0, has_gpu: bool = False, connect: bool = True,
                  serve_model: str = "", timeout: int = 900, trace_id=None) -> Dict:
    inst = await cap_install(host_id=host_id, target=target, sudo=sudo,
                             port=port, timeout=timeout)
    out: Dict[str, Any] = {"install": inst}
    if not inst.get("ok"):
        out["ok"] = False
        return out
    if target == "vllm" and serve_model:
        out["serve"] = await cap_serve(host_id=host_id, model=serve_model,
                                       port=int(port or _TARGETS["vllm"]["default_port"]))
    if connect and _TARGETS.get(target, {}).get("register"):
        out["connect"] = await cap_connect(host_id=host_id, target=target, port=port,
                                            has_gpu=has_gpu)
        out["ok"] = bool(out["connect"].get("ok"))
    else:
        out["ok"] = True
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL  (embedded as the Provision sub-tab of the workers Proxmox pane)
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/provision/panel", include_in_schema=False)
async def _provision_panel():
    p = _HERE / "provision_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>provision_panel.html not found</p>")


# Embedded sub-tab only (no standalone top-level tab); /provision/panel is kept.
register_ui = (lambda *a, **k: None)
register_ui(
    "provision-panel", "Provision", "⤓",
    """<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/provision/panel" style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#181614)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["provision.targets", "provision.detect", "provision.install",
             "provision.serve", "provision.connect", "provision.run",
             "exec.ssh.hosts.list", "exec.ssh.hosts.save"],
    mode="tab", tab_order=56,
)

log.info("software_capabilities ready — %d targets", len(_TARGETS))
