"""
components_capabilities.py — deploy Vera's own edge components onto a host
==========================================================================

`software_capabilities.py` installs third-party *runtimes* (Ollama/vLLM/Docker)
from the internet. This module is the sibling that ships **Vera's own bundled
artifacts** to a reachable host and runs them — the "download/provision" the user
asked for:

  • gpu_inference   edge/GPU_inference.py     Whisper STT + SD + TTS GPU server
  • onnx_runtime    edge/onnx_runtime.py      edge ONNX model server (CUDA→DML→CPU)
  • ollama_wrapper  edge/ollama_wrapper.sh    Ollama launch/manage wrapper (+unit)
  • mesh_gateway    vera/mesh/mesh_gateway.py LAN→Vera forwarder for ESP32 nodes
  • vera-worker     the orchestrator itself, joined to the cluster as a worker

It is deliberately thin, reusing the SSH execution + credential store
(`exec.ssh.run` / `exec.ssh.hosts.list`, the same store the Provision panel and
Docker use). Files are read from THIS repo, base64-pushed over SSH into
`~/.vera/edge/`, deps optionally installed into a shared venv, then launched
either with **nohup + pidfile** (no root) or as a **systemd unit** (survives
reboot, needs sudo) — the user picks per deploy.

Capabilities (group `provision.*`)
──────────────────────────────────
  provision.components        — catalog of deployable components
  provision.deploy            — push files (+deps) and (optionally) launch one
  provision.component.status  — is it running? (pid / systemctl)
  provision.component.stop    — stop it (kill pid / systemctl stop)
  provision.worker            — provision a Vera worker (docker | native)
"""

from __future__ import annotations

import base64
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, register_ui

log = logging.getLogger("vera.provision.components")
_HERE = Path(__file__).parent
# .../Vera/vera/provisioning/components_capabilities.py → parents[2] == repo root
_REPO = Path(__file__).resolve().parents[2]

_EDGE_DIR = "$HOME/.vera/edge"          # remote working dir for deployed components
_VENV = "$HOME/.vera/edge/venv"         # shared venv for the python components


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


# ═════════════════════════════════════════════════════════════════════════════
#  CATALOG  — each component lists its bundled file(s) (repo-relative → remote
#  basename), how to install deps, and how to run it. {py}=python, {port},
#  {vera_url} are filled at deploy time.
# ═════════════════════════════════════════════════════════════════════════════
_COMPONENTS: Dict[str, Dict[str, Any]] = {
    "gpu_inference": {
        "label": "GPU Inference Server", "port": 8765, "python": True,
        "files": [("edge/GPU_inference.py", "GPU_inference.py")],
        "requirements": "edge/requirements.txt",       # heavy (torch/diffusers/whisper/TTS)
        "run": "{py} GPU_inference.py",
        "env": {"SERVER_PORT": "{port}"},
        "heavy": True,
        "desc": "Whisper STT + Stable Diffusion + TTS server. Needs a GPU and large "
                "Python deps — enable 'install deps' and expect a long first run.",
    },
    "onnx_runtime": {
        "label": "ONNX Runtime", "port": 8770, "python": True,
        "files": [("edge/onnx_runtime.py", "onnx_runtime.py")],
        "pip": ["onnxruntime", "onnx", "numpy", "fastapi", "uvicorn"],
        "run": "{py} onnx_runtime.py serve --host 0.0.0.0 --port {port}",
        "desc": "Edge ONNX model server (CUDAExecutionProvider→DML→CPU). Serves the "
                ".onnx artifacts produced by ml.export.onnx.",
    },
    "ollama_wrapper": {
        "label": "Ollama Wrapper", "port": 0, "python": False,
        "files": [("edge/ollama_wrapper.sh", "ollama_wrapper.sh"),
                  ("edge/ollama.service", "ollama.service")],
        "run": "bash ollama_wrapper.sh",
        "desc": "Ollama launch/management wrapper script (ships its systemd unit "
                "alongside). Run directly, or deploy as a service.",
    },
    "mesh_gateway": {
        "label": "Mesh Gateway", "port": 8088, "python": True,
        "files": [("vera/mesh/mesh_gateway.py", "mesh_gateway.py")],
        "pip": [],                                      # stdlib only
        "run": "{py} mesh_gateway.py --target {vera_url} --port {port}",
        "needs_vera_url": True,
        "desc": "LAN→Vera forwarder so firewalled ESP32 mesh nodes can reach Vera "
                "through this box. Stdlib only — no deps to install.",
    },
}


# ═════════════════════════════════════════════════════════════════════════════
#  SSH HELPERS  (reuse the execution module's store + runner)
# ═════════════════════════════════════════════════════════════════════════════
async def _host_rec(host_id: str) -> Optional[Dict]:
    lst = _cap("exec.ssh.hosts.list")
    if not lst:
        return None
    try:
        for h in (await lst()).get("hosts", []):
            if h.get("id") == host_id:
                return h
    except Exception:
        return None
    return None


async def _ssh(host_id: str, command: str, timeout: int = 120) -> Dict:
    run = _cap("exec.ssh.run")
    if not run:
        return {"ok": False, "error": "exec.ssh.run unavailable (execution module not loaded)",
                "rc": -1, "stdout": "", "stderr": ""}
    return await run(command=command, host_id=host_id, timeout=timeout) or \
        {"ok": False, "error": "no response from exec.ssh.run", "rc": -1, "stdout": "", "stderr": ""}


def _sudo_for(rec: Optional[Dict], want: bool) -> str:
    return "" if ((rec or {}).get("user") == "root" or not want) else "sudo "


def _read_local(rel: str) -> Optional[bytes]:
    p = _REPO / rel
    try:
        return p.read_bytes()
    except Exception as e:
        log.warning("components: cannot read %s: %s", p, e)
        return None


def _push_cmd(content: bytes, dest: str) -> str:
    """A shell snippet that recreates `content` at remote `dest` (base64 is shell-safe)."""
    b64 = base64.b64encode(content).decode()
    return f"printf %s {shlex.quote(b64)} | base64 -d > {dest}"


def _py_bin(install_deps: bool) -> str:
    # Use the shared venv when we created/maintain one, else the system python3.
    return f'"{_VENV}/bin/python"' if install_deps else "python3"


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "provision.components",
    http_method="GET", http_path="/provision/components", http_tags=["provision"],
    memory="off", silent=True,
    description="List Vera's bundled edge components that can be deployed to a host "
                "over SSH. Output: {components:[{key,label,desc,port,python,heavy,"
                "needs_vera_url}]}.",
)
async def cap_components(trace_id=None) -> Dict:
    return {"components": [
        {"key": k, "label": c["label"], "desc": c["desc"],
         "port": c["port"], "python": c["python"],
         "heavy": bool(c.get("heavy")), "needs_vera_url": bool(c.get("needs_vera_url"))}
        for k, c in _COMPONENTS.items()
    ]}


@capability(
    "provision.deploy",
    http_method="POST", http_path="/provision/deploy", http_tags=["provision"],
    memory="off",
    description="Push a bundled component's file(s) to a stored host (into "
                "~/.vera/edge) and optionally install deps + launch it. Inputs: "
                "host_id (str!), component (gpu_inference|onnx_runtime|ollama_wrapper|"
                "mesh_gateway), port (int — override), install_deps (bool=false), "
                "launch (bool=true), systemd (bool=false — install as a service, "
                "needs sudo), sudo (bool=true), vera_url (str — required for "
                "mesh_gateway), timeout (int=900). Output: {ok, pushed, installed, "
                "launched, mode, port, url, log}.",
)
async def cap_deploy(host_id: str = "", component: str = "", port: int = 0,
                     install_deps: bool = False, launch: bool = True,
                     systemd: bool = False, sudo: bool = True, vera_url: str = "",
                     timeout: int = 900, trace_id=None) -> Dict:
    comp = _COMPONENTS.get(component)
    if not host_id or not comp:
        return {"ok": False, "error": "host_id and a valid component are required",
                "components": list(_COMPONENTS)}
    rec = await _host_rec(host_id)
    if not rec:
        return {"ok": False, "error": f"host_id not found: {host_id}"}
    if comp.get("needs_vera_url") and not vera_url:
        return {"ok": False, "error": f"{component} requires vera_url (the Vera base "
                                      "URL reachable from the target, e.g. http://host:8999)"}
    port = int(port or comp["port"])
    out: Dict[str, Any] = {"component": component, "host": rec.get("host", ""),
                           "pushed": [], "installed": False, "launched": False}

    await emit_event({"type": "provision.deploy.start", "host": rec.get("host", ""),
                      "component": component})

    # 1) push files ───────────────────────────────────────────────────────────
    parts = [f"mkdir -p {_EDGE_DIR}"]
    for rel, dest in comp["files"]:
        content = _read_local(rel)
        if content is None:
            return {"ok": False, "error": f"bundled file missing in repo: {rel}"}
        parts.append(_push_cmd(content, f"{_EDGE_DIR}/{dest}"))
        out["pushed"].append(dest)
    res = await _ssh(host_id, " && ".join(parts), timeout=120)
    if not res.get("ok"):
        out["ok"] = False
        out["error"] = res.get("stderr") or res.get("error") or "file push failed"
        return out

    # 2) install deps (optional) ───────────────────────────────────────────────
    if install_deps and comp["python"]:
        steps = [f'python3 -m venv "{_VENV}" --system-site-packages',
                 f'"{_VENV}/bin/pip" install -U pip wheel']
        if comp.get("requirements"):
            req = _read_local(comp["requirements"])
            if req is not None:
                steps.append(_push_cmd(req, f"{_EDGE_DIR}/requirements.txt"))
                steps.append(f'"{_VENV}/bin/pip" install -r {_EDGE_DIR}/requirements.txt')
        elif comp.get("pip"):
            steps.append(f'"{_VENV}/bin/pip" install ' + " ".join(shlex.quote(p) for p in comp["pip"]))
        steps.append("echo VERA_DEPS_DONE")
        dres = await _ssh(host_id, " && ".join(steps), timeout=int(timeout or 900))
        out["installed"] = "VERA_DEPS_DONE" in (dres.get("stdout", "") or "")
        out["install_log"] = ((dres.get("stdout", "") or "") + "\n" +
                              (dres.get("stderr", "") or ""))[-3000:]
        if not out["installed"]:
            out["ok"] = False
            out["error"] = "dependency install failed (see install_log)"
            return out

    # 3) launch (optional) ─────────────────────────────────────────────────────
    if launch:
        py = _py_bin(install_deps)
        run_cmd = comp["run"].format(py=py, port=port, vera_url=shlex.quote(vera_url) if vera_url else "")
        env = {k: v.format(port=port) for k, v in (comp.get("env") or {}).items()}
        if systemd:
            lres = await _launch_systemd(host_id, rec, component, comp, run_cmd, env, sudo)
            out["mode"] = "systemd"
        else:
            lres = await _launch_nohup(host_id, component, run_cmd, env)
            out["mode"] = "nohup"
        out["launched"] = bool(lres.get("ok"))
        out["launch_log"] = lres.get("log", "")
        if not out["launched"]:
            out["ok"] = False
            out["error"] = lres.get("error", "launch failed")
            return out
        if port:
            out["url"] = f"http://{rec.get('host','')}:{port}"

    out["ok"] = True
    await emit_event({"type": "provision.deploy.done", "host": rec.get("host", ""),
                      "component": component, "ok": True})
    return out


async def _launch_nohup(host_id: str, key: str, run_cmd: str, env: Dict[str, str]) -> Dict:
    envp = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
    cmd = (
        f"cd {_EDGE_DIR} && "
        f"{{ {envp}nohup {run_cmd} > {key}.log 2>&1 & echo $! > {key}.pid ; }} && "
        f'sleep 1 && echo "VERA_LAUNCHED pid=$(cat {key}.pid 2>/dev/null)"'
    )
    res = await _ssh(host_id, cmd, timeout=40)
    log_tail = ((res.get("stdout", "") or "") + "\n" + (res.get("stderr", "") or ""))[-2000:]
    ok = bool(res.get("ok")) and "VERA_LAUNCHED" in (res.get("stdout", "") or "")
    return {"ok": ok, "log": log_tail, "error": "" if ok else (res.get("stderr") or res.get("error") or "")}


async def _launch_systemd(host_id: str, rec: Dict, key: str, comp: Dict,
                          run_cmd: str, env: Dict[str, str], sudo: bool) -> Dict:
    s = _sudo_for(rec, sudo)
    envlines = "".join(f"Environment={k}={v}\\n" for k, v in env.items())
    # Heredoc expands $HOME on the host so ExecStart/WorkingDirectory are absolute.
    unit = (
        "[Unit]\\n"
        f"Description=Vera {comp['label']}\\nAfter=network-online.target\\n\\n"
        "[Service]\\nType=simple\\n"
        f"WorkingDirectory=$HOME/.vera/edge\\n"
        f"{envlines}"
        f"ExecStart={run_cmd}\\n"
        "Restart=on-failure\\nRestartSec=3\\n\\n"
        "[Install]\\nWantedBy=multi-user.target\\n"
    )
    svc = f"vera-{key}.service"
    cmd = (
        f'printf "{unit}" | {s}tee /etc/systemd/system/{svc} >/dev/null && '
        f"{s}systemctl daemon-reload && {s}systemctl enable --now {svc} && "
        f'echo "VERA_LAUNCHED service={svc}"'
    )
    res = await _ssh(host_id, cmd, timeout=60)
    log_tail = ((res.get("stdout", "") or "") + "\n" + (res.get("stderr", "") or ""))[-2000:]
    ok = bool(res.get("ok")) and "VERA_LAUNCHED" in (res.get("stdout", "") or "")
    return {"ok": ok, "log": log_tail, "error": "" if ok else (res.get("stderr") or res.get("error") or "")}


@capability(
    "provision.component.status",
    http_method="POST", http_path="/provision/component/status", http_tags=["provision"],
    memory="off", silent=True,
    description="Check whether a deployed component is running. Inputs: host_id "
                "(str!), component (str!), systemd (bool=false). Output: {ok, "
                "running, detail}.",
)
async def cap_component_status(host_id: str = "", component: str = "",
                               systemd: bool = False, trace_id=None) -> Dict:
    if not host_id or component not in _COMPONENTS:
        return {"ok": False, "error": "host_id and a valid component are required"}
    if systemd:
        res = await _ssh(host_id, f"systemctl is-active vera-{component}.service", timeout=20)
        state = (res.get("stdout", "") or "").strip()
        return {"ok": True, "running": state == "active", "detail": state or "unknown"}
    res = await _ssh(
        host_id,
        f'PID=$(cat {_EDGE_DIR}/{component}.pid 2>/dev/null); '
        f'if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then echo "running pid=$PID"; '
        f'else echo stopped; fi',
        timeout=20)
    out = (res.get("stdout", "") or "").strip()
    return {"ok": True, "running": out.startswith("running"), "detail": out or "unknown"}


@capability(
    "provision.component.stop",
    http_method="POST", http_path="/provision/component/stop", http_tags=["provision"],
    memory="off",
    description="Stop a deployed component. Inputs: host_id (str!), component "
                "(str!), systemd (bool=false), sudo (bool=true). Output: {ok, detail}.",
)
async def cap_component_stop(host_id: str = "", component: str = "",
                             systemd: bool = False, sudo: bool = True, trace_id=None) -> Dict:
    if not host_id or component not in _COMPONENTS:
        return {"ok": False, "error": "host_id and a valid component are required"}
    rec = await _host_rec(host_id)
    if systemd:
        s = _sudo_for(rec, sudo)
        res = await _ssh(host_id, f"{s}systemctl disable --now vera-{component}.service && echo VERA_STOPPED", timeout=30)
    else:
        res = await _ssh(
            host_id,
            f'PID=$(cat {_EDGE_DIR}/{component}.pid 2>/dev/null); '
            f'[ -n "$PID" ] && kill "$PID" 2>/dev/null; rm -f {_EDGE_DIR}/{component}.pid; echo VERA_STOPPED',
            timeout=20)
    ok = "VERA_STOPPED" in (res.get("stdout", "") or "")
    await emit_event({"type": "provision.component.stop", "component": component, "ok": ok})
    return {"ok": ok, "detail": (res.get("stdout", "") or res.get("stderr", "")).strip()}


# ═════════════════════════════════════════════════════════════════════════════
#  VERA WORKER  — docker container (reuse docker.worker.spawn) OR native process
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "provision.worker",
    http_method="POST", http_path="/provision/worker", http_tags=["provision"],
    memory="off",
    description="Provision a Vera worker that joins the cluster (consumes the task "
                "stream via the shared REDIS_URL). Inputs: host_id (str!), mode "
                "('docker'|'native'), name (str), image (str — docker), gpus (str "
                "— 'all'), repo_url (str — native: git URL, else env VERA_REPO_URL), "
                "port (int=8990 — native orchestrator port), redis_url (str — "
                "default this orchestrator's), timeout (int=1200). "
                "docker → registers the host as an SSH Docker host then "
                "docker.worker.spawn. native → git-clone Vera + venv + run "
                "'python -m Vera.vera.capability_orchestration'. Output: {ok, mode, ...}.",
)
async def cap_worker(host_id: str = "", mode: str = "docker", name: str = "",
                     image: str = "", gpus: str = "", repo_url: str = "",
                     port: int = 8990, redis_url: str = "", timeout: int = 1200,
                     trace_id=None) -> Dict:
    rec = await _host_rec(host_id)
    if not rec:
        return {"ok": False, "error": f"host_id not found: {host_id}"}
    mode = (mode or "docker").strip().lower()
    redis_url = redis_url or os.getenv("REDIS_URL", "") or \
        getattr(_orch.cfg, "REDIS_URL", "redis://localhost:6379")

    if mode == "docker":
        save = _cap("docker.hosts.save")
        spawn = _cap("docker.worker.spawn")
        if not (save and spawn):
            return {"ok": False, "error": "docker module not loaded (need docker.hosts.save + docker.worker.spawn)"}
        reg = await save(kind="ssh", ssh_host_id=host_id, label=f"{rec.get('host','')} (vera-worker)")
        dhost = (reg.get("host") or {}).get("id") if isinstance(reg, dict) else None
        if not dhost:
            return {"ok": False, "error": "could not register host as a Docker host", "register": reg}
        sp = await spawn(host_id=dhost, name=name, image=image, redis_url=redis_url, gpus=gpus)
        await emit_event({"type": "provision.worker", "mode": "docker", "host": rec.get("host", ""),
                          "ok": bool(sp.get("ok"))})
        return {"ok": bool(sp.get("ok")), "mode": "docker", "docker_host": dhost, "spawn": sp}

    if mode == "native":
        repo = repo_url or os.getenv("VERA_REPO_URL", "")
        if not repo:
            return {"ok": False, "mode": "native",
                    "error": "native mode needs a git repo_url (or set VERA_REPO_URL on the Vera host). "
                             "Provide the URL of your Vera repository."}
        # Mirror the Dockerfile layout: code lives under <root>/Vera/vera so that
        # `python -m Vera.vera.capability_orchestration` imports cleanly.
        root = "$HOME/.vera/app"
        pkg = f"{root}/Vera/vera"
        backend_env = " ".join(
            f"{k}={shlex.quote(os.getenv(k, ''))}"
            for k in ("POSTGRES_URL", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASS", "OLLAMA_BASE_URL")
            if os.getenv(k))
        cmd = (
            f"mkdir -p {root}/Vera && "
            f"( [ -d {pkg}/.git ] && git -C {pkg} pull --ff-only || "
            f"  git clone --depth 1 {shlex.quote(repo)} {pkg} ) && "
            f"touch {root}/Vera/__init__.py {pkg}/__init__.py && "
            f'python3 -m venv {pkg}/venv --system-site-packages && '
            f'"{pkg}/venv/bin/pip" install -U pip wheel && '
            f'"{pkg}/venv/bin/pip" install -r {pkg}/requirements.txt && '
            f'cd {pkg} && '
            f'{{ PYTHONPATH={root} REDIS_URL={shlex.quote(redis_url)} '
            f'ORCHESTRATOR_HOST=0.0.0.0 ORCHESTRATOR_PORT={int(port)} {backend_env} '
            f'nohup "{pkg}/venv/bin/python" -m Vera.vera.capability_orchestration '
            f'> {root}/worker.log 2>&1 & echo $! > {root}/worker.pid ; }} && '
            f'sleep 2 && echo "VERA_LAUNCHED pid=$(cat {root}/worker.pid 2>/dev/null)"'
        )
        res = await _ssh(host_id, cmd, timeout=int(timeout or 1200))
        ok = bool(res.get("ok")) and "VERA_LAUNCHED" in (res.get("stdout", "") or "")
        await emit_event({"type": "provision.worker", "mode": "native", "host": rec.get("host", ""), "ok": ok})
        return {"ok": ok, "mode": "native", "port": int(port),
                "log": ((res.get("stdout", "") or "") + "\n" + (res.get("stderr", "") or ""))[-3000:],
                "error": "" if ok else (res.get("stderr") or res.get("error") or "native worker launch failed")}

    return {"ok": False, "error": f"unknown mode: {mode} (use 'docker' or 'native')"}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL  (served for completeness; the UI lives in the Provision tab)
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/provision/components/panel", include_in_schema=False)
async def _components_panel():
    p = _HERE / "provision_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>provision_panel.html not found</p>")


log.info("components_capabilities ready — %d deployable components", len(_COMPONENTS))
