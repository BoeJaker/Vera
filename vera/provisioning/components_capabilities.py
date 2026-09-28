"""
components_capabilities.py — deploy Vera's own edge components onto a host
==========================================================================

`software_capabilities.py` installs third-party *runtimes* (Ollama/vLLM/Docker)
from the internet. This module is the sibling that ships **Vera's own bundled
artifacts** to a reachable host and runs them — the "download/provision" the user
asked for:

  • gpu_inference   edge/GPU_inference.py     Whisper STT + SD + TTS GPU server
  • onnx_runtime    edge/onnx_runtime.py      edge ONNX model server (CUDA→DML→CPU)
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

import asyncio
import base64
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, register_ui, schedule
from Vera.vera.integrations.infrastructure_effects import observe_infrastructure_effect
from Vera.vera.provisioning.components_core import (
    rewrite_host, native_worker_cmd, worker_backend_env, WORKER_BACKEND_KEYS,
    EDGE_DIR_CANDIDATES as _EDGE_DIR_CANDIDATES,
    WORKER_DIR_CANDIDATES as _WORKER_DIR_CANDIDATES,
    edge_dir_probe_cmd, parse_edge_dir, pidfile_lookup_cmd,
    component_version, version_file, version_json, version_lookup_cmd,
    parse_version, compare_versions, component_sync_plan, media_env_profile,
)
from Vera.vera.workers import worker_placement_core as _placement
from Vera.vera.security import redis_auth_core as _redis_auth_core
from Vera.vera.provisioning import node_sync_core as _node_sync

log = logging.getLogger("vera.provision.components")
_HERE = Path(__file__).parent
# .../Vera/vera/provisioning/components_capabilities.py → parents[2] == repo root
_REPO = Path(__file__).resolve().parents[2]

_EDGE_DIR = "$HOME/.vera/edge"          # preferred remote working dir
_VENV = "$HOME/.vera/edge/venv"         # shared venv for the python components

async def _resolve_edge_dir(host_id: str) -> Dict[str, Any]:
    """Pick a writable working directory on the target, in preference order.

    Returns {ok, dir, venv, tried}. The candidate list and the probe itself live
    in components_core so they can be unit-tested without booting Vera, and so
    status/stop provably search the same places the deploy wrote to.
    """
    res = await _ssh(host_id, edge_dir_probe_cmd(), timeout=40)
    chosen = parse_edge_dir(res.get("stdout") or "")
    if chosen:
        return {"ok": True, "dir": chosen, "venv": f"{chosen}/venv",
                "tried": list(_EDGE_DIR_CANDIDATES)}
    # Distinguish "could not ask" from "asked, and nowhere was writable".
    # Collapsing the two sends the reader hunting for a permissions problem on
    # the target when the real answer is that SSH never ran (observed: a
    # sandbox without asyncssh reported an unwritable filesystem it had never
    # reached).
    if not res.get("ok"):
        return {"ok": False, "dir": "", "venv": "",
                "tried": list(_EDGE_DIR_CANDIDATES),
                "error": "could not probe the target over SSH: "
                         f"{res.get('error') or res.get('stderr') or 'no response'}"}
    return {"ok": False, "dir": "", "venv": "",
            "tried": list(_EDGE_DIR_CANDIDATES),
            "error": "no writable working directory on the target "
                     f"(tried {', '.join(_EDGE_DIR_CANDIDATES)}) — "
                     "$HOME is often unwritable on an unprivileged LXC node"}


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


def _primary_lan_ip() -> str:
    """The orchestrator's own LAN address, so a remote worker's backend URLs can be
    re-pointed away from 'localhost' (see components_core.rewrite_host)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no packet sent; just picks the egress iface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ═════════════════════════════════════════════════════════════════════════════
#  CATALOG  — each component lists its bundled file(s) (repo-relative → remote
#  basename), how to install deps, and how to run it. {py}=python, {port},
#  {vera_url} are filled at deploy time.
# ═════════════════════════════════════════════════════════════════════════════
_COMPONENTS: Dict[str, Dict[str, Any]] = {
    "gpu_inference": {
        # The media server, in the layout every node already runs it in (see
        # edge/gpu-inference.service): an install dir with its own ./env,
        # start.sh, the gpu-inference.service unit and /etc/default/vera-inference.
        # It used to deploy to ~/.vera/edge with a second venv, a layout no node
        # ran, so the nodes were installed by hand and drifted (2026-09-28: the
        # CPU nodes ran GPU_inference.py from 60f87f39, the GPU node 631948e2).
        "label": "Media server (STT · TTS · diffusion)", "port": 8765, "python": True,
        "install_dir": "/home/Servers/StableDiffustionWhisper",
        "venv": "env",
        "files": [("edge/GPU_inference.py", "GPU_inference.py"),
                  ("edge/gpu_residency_core.py", "gpu_residency_core.py"),
                  ("edge/media_store_core.py", "media_store_core.py"),
                  # reports every call to the Estate Activity pane
                  ("edge/activity_record.py", "activity_record.py"),
                  ("edge/gpu_inference_start.sh", "start.sh", "755")],
        "unit_file": ("edge/gpu-inference.service", "gpu-inference.service"),
        "service": "gpu-inference",
        # rendered per node by components_core.media_env_profile
        "env_file": "/etc/default/vera-inference",
        # torch first, from the index that matches the node - a CPU node must not
        # pull the CUDA build (>1 GB of nvidia libs it cannot use)
        "torch_index": {"gpu": "https://download.pytorch.org/whl/cu121",
                        "cpu": "https://download.pytorch.org/whl/cpu"},
        "pip_steps": [["torch", "torchvision", "torchaudio", "--index-url", "{torch_index}"]],
        "requirements": "edge/requirements.txt",
        # tiers the server degrades without; one failing must not fail the deploy
        "pip_optional": [["rembg>=2.0.50", "onnxruntime>=1.16"], ["controlnet-aux>=0.0.7"],
                         ["compel>=2.0.2"], ["opencv-python-headless>=4.8"],
                         ["realesrgan>=0.3.0"], ["TTS>=0.22"]],
        "precheck": {
            "cmd": ('[ -d /opt/vera-store/models/sd ] && [ -d /opt/vera-store/models/tts ] '
                    '&& echo VERA_PRECHECK_OK'),
            "why": ("the shared model store is not mounted at /opt/vera-store/models on this "
                    "node. On the Proxmox host: pct set <ctid> -mpN /tank_sdh/vera-store/models,"
                    "mp=/opt/vera-store/models,ro=1 (see specialist.store.mount)."),
        },
        "health": "/health",
        "start_timeout": 900,
        "heavy": True, "sync": True, "systemd": True,
        "desc": "Whisper STT + Kokoro/Coqui TTS + Stable Diffusion (ControlNet, IP-Adapter, "
                "rembg, upscale). Every node runs it; models load from the shared store, a "
                "CPU node runs them on CPU, and routing stays GPU-first.",
    },
    "onnx_runtime": {
        # ⚠ 8772, NOT 8770. The node agent (edge/vera_node_agent.py) listens on
        # 8770 on EVERY node, so this component declared a port it could never
        # have bound on any node running the agent — a latent collision that
        # would have surfaced as a confusing deploy failure rather than as the
        # configuration error it is.
        "label": "ONNX Runtime", "port": 8772, "python": True,
        "files": [("edge/onnx_runtime.py", "onnx_runtime.py")],
        "pip": ["onnxruntime", "onnx", "numpy", "fastapi", "uvicorn"],
        "run": "{py} onnx_runtime.py serve --host 0.0.0.0 --port {port}",
        "desc": "Edge ONNX model server (CUDAExecutionProvider→DML→CPU). Serves the "
                ".onnx artifacts produced by ml.export.onnx. Tensor-level: takes "
                "and returns tensors. For text (NER/classify/rerank) use nlp_server.",
    },
    "nlp_server": {
        # 8771 — clear of the node agent (8770) and onnx_runtime (8772).
        "label": "NLP Server", "port": 8771, "python": True,
        "files": [("edge/nlp_server.py", "nlp_server.py"),
                  # reports every call to the Estate Activity pane
                  ("edge/activity_record.py", "activity_record.py"),
                  # Shipped so the node and the Vera host share ONE registry and
                  # ONE implementation of chunking and offset merging.
                  ("vera/research/nlp_dispatch_core.py", "nlp_dispatch_core.py"),
                  # The exporter that populates the shared model store. It runs
                  # once, on a box with torch and internet — not at serve time.
                  ("edge/nlp_export_models.py", "nlp_export_models.py")],
        # `optimum[onnxruntime]` provides the ORTModelFor* classes that LOAD the
        # pre-exported ONNX, and it pulls torch as a hard dependency — measured,
        # not assumed: installing it fetched torch 2.14 (a 554MB wheel). So the
        # runtime is not torch-free. What pre-exporting still buys is the thing
        # that actually matters here: no CONVERSION and no network at request
        # time, which is what makes a read-only model store workable at all.
        #
        # transformers is pinned to the major version the models were exported
        # under. Left unpinned, this install pulled transformers 5.x against
        # artifacts built with 4.x — a mismatch that is not worth discovering
        # on a production node.
        # ORDERED, separate pip invocations. A single flat list cannot express
        # this: torch MUST come from the CPU index first, because
        # optimum[onnxruntime] depends on torch and pip would otherwise resolve
        # the default CUDA build — measured at 554MB for torch plus another
        # 553MB of nvidia cudnn, for a server that runs ONNX on CPU only.
        # transformers is pinned to the major version the models were exported
        # under: optimum 2.1.0 against transformers 5.x fails at load with
        # "cannot import name FLAX_WEIGHTS_NAME".
        "pip_steps": [
            ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"],
            ["optimum[onnxruntime]", "transformers>=4.57,<5", "onnxruntime",
             "sentencepiece", "protobuf", "numpy", "fastembed",
             "fastapi", "uvicorn", "redis"],
        ],
        "run": "{py} nlp_server.py serve --host 0.0.0.0 --port {port}",
        # The model store is a Proxmox bind-mount, which SSH cannot create.
        # Check it BEFORE installing ~2GB of wheels, and say exactly how to fix
        # it: otherwise the first request fails with a model-not-found long
        # after the deploy reported success.
        "precheck": {
            "cmd": ('[ -d /opt/nlp-models ] && ls -d /opt/nlp-models/*/ '
                    '>/dev/null 2>&1 && echo VERA_PRECHECK_OK'),
            "why": ("the shared NLP model store is not mounted at "
                    "/opt/nlp-models on this node. On the Proxmox host: "
                    "pct set <ctid> -mp2 /tank_sdh/vera-store/models/nlp,"
                    "mp=/opt/nlp-models,ro=1 && pct reboot <ctid>. "
                    "Populate it once with edge/nlp_export_models.py."),
        },
        "env": {
            # ONNX Runtime will otherwise take every core it can see and starve
            # the ollama runner sharing this container. This is the setting that
            # decides whether NLP on the GPU node is free or ruinous.
            "VERA_NLP_THREADS": "4",
            "VERA_NLP_PORT": "{port}",
            # The shared ZFS model store, bind-mounted read-only into the node
            # alongside ollama's own blobs.
            "VERA_NLP_MODEL_DIR": "/opt/nlp-models",
            # /root is unreachable on these unprivileged LXC nodes (nobody:root
            # 0700), so anything that touches a default cache under $HOME dies
            # with a permission error. ollama-vera.service sets HOME=/ for the
            # same reason.
            "HOME": "/",
        },
        "heavy": True,
        # provision.component.sync may redeploy it: every node runs it the same
        # way (this deploy path, as vera-nlp_server.service).
        "sync": True, "systemd": True,
        "desc": "Text-level NLP so the 2-core Vera host never runs it: NER "
                "(OntoNotes-v5, has DATE, plus multilingual), sentiment, "
                "zero-shot classification, extractive QA, language id, "
                "embeddings and reranking. Loads pre-exported ONNX from the "
                "shared read-only model store, chunks whole documents rather "
                "than truncating, and caps its thread count so the node keeps "
                "inferring.",
    },
    # ollama_wrapper was removed as a deployable component. It proxied :11435 in
    # front of Ollama to make requests visible, but it was never deployed, it
    # collides with the ollama-vera unit that owns that port, its watchdog fell
    # back to killing every ollama process on the node, and its default stop
    # tokens included "###" — which would truncate Vera's own markdown output.
    # The visibility it was written for now comes from bench.node_requests, which
    # reads each node's own access log and adds nothing to the request path.
    "mesh_gateway": {
        "label": "Mesh Gateway", "port": 8088, "python": True,
        "files": [("vera/mesh/mesh_gateway.py", "mesh_gateway.py")],
        "pip": [],                                      # stdlib only
        "run": "{py} mesh_gateway.py --target {vera_url} --port {port}",
        "needs_vera_url": True,
        "desc": "LAN→Vera forwarder so firewalled ESP32 mesh nodes can reach Vera "
                "through this box. Stdlib only — no deps to install.",
    },
    "model_builder": {
        # 8773 - clear of the node agent (8770), nlp_server (8771), onnx_runtime (8772).
        # Deployed to ONE box, the builder CT (vera-model-builder, CT 131), whose
        # mount of the shared store is read-write; every serving node's is ro.
        "label": "Specialist model builder", "port": 8773, "python": True,
        "files": [("edge/model_builder.py", "model_builder.py"),
                  # the exporter that built the NLP store, and its registry
                  ("edge/nlp_export_models.py", "nlp_export_models.py"),
                  ("vera/research/nlp_dispatch_core.py", "nlp_dispatch_core.py"),
                  # content-verified packages for the NLP manifest, as a
                  # standalone package (vera/models/__init__ imports far more)
                  ("edge/vmodels_init.py", "vmodels/__init__.py"),
                  ("vera/models/nlp_inventory.py", "vmodels/nlp_inventory.py"),
                  ("vera/models/model_package.py", "vmodels/model_package.py")],
        # a minimal ubuntu CT has no ensurepip
        "apt": ["python3-venv"],
        # Same CPU-torch-first ordering and transformers pin as nlp_server: the
        # exports must load in the server that reads them.
        "pip_steps": [
            ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"],
            ["optimum[onnxruntime]", "transformers>=4.57,<5", "onnxruntime",
             "sentencepiece", "protobuf", "numpy", "fastembed", "huggingface_hub",
             "openai-whisper", "fastapi", "uvicorn"],
        ],
        "run": "{py} model_builder.py serve --host 0.0.0.0 --port {port}",
        "precheck": {
            "cmd": ('[ -d /opt/vera-store/models/nlp ] && [ -w /opt/vera-store/models/nlp ] '
                    '&& echo VERA_PRECHECK_OK'),
            "why": ("the shared store is not mounted READ-WRITE at /opt/vera-store/models. "
                    "On the Proxmox host: pct set <ctid> -mp0 /tank_sdh/vera-store/models,"
                    "mp=/opt/vera-store/models, and (unprivileged CT) chown the family "
                    "dirs to 100000:100000."),
        },
        "env": {"VERA_STORE_DIR": "/opt/vera-store/models",
                # the download cache stays on the CT's own disk, not in the store
                "HF_HOME": "/var/lib/vera-builder/hf", "HOME": "/",
                "OMP_NUM_THREADS": "4"},
        "heavy": True, "sync": True, "systemd": True,
        # the hosts it runs on: stored SSH hosts with this tag, not the ollama nodes
        "hosts_tag": "model-builder",
        "desc": "Fills the shared specialist-model store on demand: ONNX exports for "
                "nlp_server, Hugging Face snapshots (GLiNER, diffusion), Whisper "
                "checkpoints, curated TTS files. One job at a time. Runs only on the "
                "builder CT, the one box with the store mounted read-write.",
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


def _deps_spec(comp: Dict[str, Any]) -> bytes:
    """What a deploy installs, as bytes, so a pin change moves the version too."""
    req = _read_local(comp["requirements"]) if comp.get("requirements") else None
    return json.dumps({"requirements": (req or b"").decode("utf-8", "replace"),
                       "pip_steps": comp.get("pip_steps") or [],
                       "pip": comp.get("pip") or [],
                       "pip_optional": comp.get("pip_optional") or [],
                       "apt": comp.get("apt") or []}, sort_keys=True).encode("utf-8")


def _shipped_files(comp: Dict[str, Any]) -> Optional[List[tuple]]:
    """(dest, content) for every file a deploy pushes, plus the deps spec;
    None when a bundled file is missing from the repo."""
    files = []
    for rel, dest, *_mode in comp["files"]:
        content = _read_local(rel)
        if content is None:
            return None
        files.append((dest, content))
    if comp.get("unit_file"):
        content = _read_local(comp["unit_file"][0])
        if content is None:
            return None
        files.append(("<unit>", content))
    files.append(("<deps>", _deps_spec(comp)))
    return files


def host_component_version(key: str) -> Dict[str, Any]:
    """The version a deploy of `key` from this host would install now."""
    comp = _COMPONENTS.get(key)
    files = _shipped_files(comp) if comp else None
    return component_version(key, files) if files is not None else {}


def _push_cmd(content: bytes, dest: str) -> str:
    """A shell snippet that recreates `content` at remote `dest` (base64 is shell-safe)."""
    b64 = base64.b64encode(content).decode()
    return f"printf %s {shlex.quote(b64)} | base64 -d > {dest}"


#: Largest file pushed inline in a command (its base64 is 4/3 bigger, and one
#: argument may not exceed 128 KB); anything bigger goes over stdin.
_INLINE_PUSH_MAX = 64 * 1024


def _push_cmd_as(content: bytes, dest: str, sudo_prefix: str = "") -> str:
    """_push_cmd into a root-owned path (via `sudo tee` when not root)."""
    if not sudo_prefix:
        return _push_cmd(content, dest)
    b64 = base64.b64encode(content).decode()
    return f"printf %s {shlex.quote(b64)} | base64 -d | {sudo_prefix}tee {dest} >/dev/null"


def _py_bin(install_deps: bool, venv: str = "") -> str:
    # Use the shared venv when we created/maintain one, else the system python3.
    return f'"{venv or _VENV}/bin/python"' if install_deps else "python3"


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
    redact_args=["host_id", "component", "vera_url", "idempotency_key",
                 "approval_receipt_ref"],
    redact_result=True,
    description="Push a bundled component's file(s) to a stored host (into "
                "~/.vera/edge) and optionally install deps + launch it. Inputs: "
                "host_id (str!), component (gpu_inference|onnx_runtime|nlp_server|"
                "mesh_gateway), port (int — override), install_deps (bool=false), "
                "launch (bool=true), systemd (bool=false — install as a service, "
                "needs sudo), sudo (bool=true), vera_url (str — required for "
                "mesh_gateway), timeout (int=900). Output: {ok, pushed, installed, "
                "launched, mode, port, url, log, version, effect_shadow}. Writes "
                "<component>.version.json beside the files (see "
                "provision.component.version). Optional "
                "idempotency, approval, and retry inputs are observe-only and "
                "never sent to SSH.",
)
async def cap_deploy(host_id: str = "", component: str = "", port: int = 0,
                     install_deps: bool = False, launch: bool = True,
                     systemd: bool = False, sudo: bool = True, vera_url: str = "",
                     timeout: int = 900, idempotency_key: str = "",
                     approval_receipt_ref: str = "", retry: bool = False,
                     trace_id=None) -> Dict:
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
    # The effect shadow is recorded BEFORE any SSH, and exactly once. The
    # deploy's intent is fully known here, and the invariant is enforced by
    # test_component_deploy_observes_before_ssh_without_forwarding_controls —
    # the working-directory probe below is itself an SSH call, so it must not
    # run first.
    shadow = observe_infrastructure_effect(
        provider="ssh", target_ref=host_id, resource_ref=component,
        operation_ref=json.dumps({
            "install_deps": bool(install_deps), "launch": bool(launch),
            "port": port, "systemd": bool(systemd),
        }, sort_keys=True, separators=(",", ":")), mode="component_deploy",
        idempotency_key=idempotency_key,
        approval_receipt_ref=approval_receipt_ref, retry=retry)
    out["effect_shadow"] = shadow

    if comp.get("install_dir"):
        # a component with a fixed layout (the media server): its own dir + venv
        edge_dir = comp["install_dir"]
        venv = f"{edge_dir}/{comp.get('venv', 'env')}"
    else:
        # Never assume $HOME is writable — see _EDGE_DIR_CANDIDATES for why two of
        # the three ollama nodes cannot use it at all.
        edge = await _resolve_edge_dir(host_id)
        if not edge.get("ok"):
            out["ok"] = False
            out["error"] = edge.get("error", "no writable working directory")
            out["edge_dir_tried"] = edge.get("tried")
            return out
        edge_dir, venv = edge["dir"], edge["venv"]
    out["edge_dir"] = edge_dir
    has_gpu = _node_has_gpu(rec.get("host", ""))

    # 0b) component precheck — fail before spending the install, not after
    pre = comp.get("precheck") or {}
    if pre.get("cmd"):
        pres = await _ssh(host_id, pre["cmd"], timeout=40)
        if "VERA_PRECHECK_OK" not in (pres.get("stdout") or ""):
            out["ok"] = False
            out["error"] = f"precheck failed: {pre.get('why', 'unmet requirement')}"
            out["precheck"] = False
            return out
        out["precheck"] = True

    parts = [f"mkdir -p {edge_dir}"]
    large: List[tuple] = []
    for rel, dest, *mode in comp["files"]:
        content = _read_local(rel)
        if content is None:
            return {"ok": False, "error": f"bundled file missing in repo: {rel}"}
        if "/" in dest:                      # a file shipped as part of a package
            parts.append(f"mkdir -p {edge_dir}/{shlex.quote(dest.rsplit('/', 1)[0])}")
        if len(content) > _INLINE_PUSH_MAX:
            # Linux caps ONE argument at 128 KB and the whole command reaches the
            # remote shell as one argument: GPU_inference.py (144 KB, ~195 KB as
            # base64) failed with "Argument list too long". Stream it on stdin.
            large.append((content, f"{edge_dir}/{dest}"))
        else:
            parts.append(_push_cmd(content, f"{edge_dir}/{dest}"))
        if mode:                             # e.g. start.sh, which the unit executes
            parts.append(f"chmod {shlex.quote(mode[0])} {edge_dir}/{dest}")
        out["pushed"].append(dest)
    if large:
        runner = _ssh_stored_with_input()
        if runner is None:
            return {**out, "ok": False,
                    "error": "a bundled file is too large for a command line and the exec "
                             "module has no ssh_run_stored (stdin transport)"}
        for content, dest in large:
            d = dest.rsplit("/", 1)[0]
            res = await runner(host_id, f"mkdir -p {d} && base64 -d > {dest}.part && "
                                        f"mv {dest}.part {dest} && echo VERA_PUSHED",
                               timeout=120, input=base64.b64encode(content).decode())
            if "VERA_PUSHED" not in (res.get("stdout") or ""):
                return {**out, "ok": False,
                        "error": f"push of {dest} failed: "
                                 f"{res.get('stderr') or res.get('error') or 'no confirmation'}"}
    # The version is written only once the deploy has succeeded (below), so a
    # half-finished deploy never claims the new version.
    version = host_component_version(component)
    parts.append(f"rm -f {edge_dir}/{version_file(component)}")
    res = await _ssh(host_id, " && ".join(parts), timeout=120)
    if not res.get("ok"):
        out["ok"] = False
        out["error"] = res.get("stderr") or res.get("error") or "file push failed"
        return out

    # 2) install deps (optional) ───────────────────────────────────────────────
    if install_deps and comp["python"]:
        steps = []
        if comp.get("apt"):
            # system packages the venv itself needs (a minimal CT has no ensurepip)
            s = _sudo_for(rec, sudo)
            steps.append(f"{s}env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "
                         + " ".join(shlex.quote(p) for p in comp["apt"])
                         + f" || {{ {s}apt-get update -q && {s}env DEBIAN_FRONTEND=noninteractive "
                         + "apt-get install -y -q " + " ".join(shlex.quote(p) for p in comp["apt"])
                         + "; }")
        if comp.get("install_dir"):
            # its own venv - keep an existing one (the nodes' ./env holds GBs of wheels)
            steps += [f'[ -x "{venv}/bin/python" ] || python3 -m venv "{venv}"',
                      f'"{venv}/bin/pip" install -U pip wheel']
        else:
            steps += [f'python3 -m venv "{venv}" --system-site-packages',
                      f'"{venv}/bin/pip" install -U pip wheel']
        torch_index = (comp.get("torch_index") or {}).get("gpu" if has_gpu else "cpu", "")
        # Separate invocations, in order — some components need an index or a
        # constraint applied to one package and not the rest; then the
        # requirements file, which must not be the one to choose torch's build.
        for group in comp.get("pip_steps") or []:
            steps.append(f'"{venv}/bin/pip" install ' +
                         " ".join(shlex.quote(p.format(torch_index=torch_index)) for p in group))
        if comp.get("requirements"):
            req = _read_local(comp["requirements"])
            if req is not None:
                steps.append(_push_cmd(req, f"{edge_dir}/requirements.txt"))
                steps.append(f'"{venv}/bin/pip" install -r {edge_dir}/requirements.txt')
        elif comp.get("pip"):
            steps.append(f'"{venv}/bin/pip" install ' + " ".join(shlex.quote(p) for p in comp["pip"]))
        # optional tiers: a failure is reported, never fatal
        for group in comp.get("pip_optional") or []:
            steps.append(f'{{ "{venv}/bin/pip" install ' + " ".join(shlex.quote(p) for p in group)
                         + f' || echo VERA_OPTIONAL_FAILED={shlex.quote(group[0])}; }}')
        steps.append("echo VERA_DEPS_DONE")
        dres = await _ssh(host_id, " && ".join(steps), timeout=int(timeout or 900))
        out["installed"] = "VERA_DEPS_DONE" in (dres.get("stdout", "") or "")
        out["optional_failed"] = [ln.split("=", 1)[1] for ln in (dres.get("stdout", "") or "").splitlines()
                                  if ln.startswith("VERA_OPTIONAL_FAILED=")]
        out["install_log"] = ((dres.get("stdout", "") or "") + "\n" +
                              (dres.get("stderr", "") or ""))[-3000:]
        if not out["installed"]:
            out["ok"] = False
            out["error"] = "dependency install failed (see install_log)"
            return out

    # 2b) record the version - files and deps are in place; before launch, so a
    # component that reads its own version at startup sees this one.
    if version:
        rec_v = dict(version, deps_installed=bool(install_deps))
        vres = await _ssh(host_id, _push_cmd(version_json(rec_v),
                                             f"{edge_dir}/{version_file(component)}"),
                          timeout=30)
        out["version"] = version.get("version") if vres.get("ok") else ""

    # 3) launch (optional) ─────────────────────────────────────────────────────
    if launch and comp.get("unit_file"):
        lres = await _launch_layout(host_id, rec, comp, port, has_gpu, sudo, edge_dir)
        out.update(mode="systemd", launched=bool(lres.get("ok")),
                   launch_log=lres.get("log", ""), health=lres.get("health"))
        if not out["launched"]:
            out["ok"] = False
            out["error"] = lres.get("error", "launch failed")
            return out
        out["url"] = f"http://{rec.get('host','')}:{port}"
    elif launch:
        py = _py_bin(install_deps, venv)
        run_cmd = comp["run"].format(py=py, port=port, vera_url=shlex.quote(vera_url) if vera_url else "")
        env = {k: v.format(port=port) for k, v in (comp.get("env") or {}).items()}
        if systemd:
            lres = await _launch_systemd(host_id, rec, component, comp, run_cmd, env, sudo, edge_dir)
            out["mode"] = "systemd"
        else:
            lres = await _launch_nohup(host_id, component, run_cmd, env, edge_dir)
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


async def _launch_layout(host_id: str, rec: Dict, comp: Dict, port: int, has_gpu: bool,
                         sudo: bool, install_dir: str) -> Dict:
    """Launch a fixed-layout component (the media server): write its profile
    (keeping the first hand-written one as .pre-vera), install the repo's unit,
    restart it, and wait for its health endpoint - a model load can take
    minutes, and "the unit started" is not "the server serves"."""
    s = _sudo_for(rec, sudo)
    env_path = comp["env_file"]
    profile = media_env_profile(has_gpu, port=port, app_dir=install_dir)
    unit_src, unit_name = comp["unit_file"]
    unit = _read_local(unit_src)
    if unit is None:
        return {"ok": False, "error": f"unit file missing in repo: {unit_src}"}
    svc = comp.get("service") or unit_name.rsplit(".", 1)[0]
    cmd = " && ".join([
        f"{{ [ -f {env_path} ] && [ ! -f {env_path}.pre-vera ] && {s}cp {env_path} {env_path}.pre-vera || true; }}",
        _push_cmd_as(profile.encode("utf-8"), f"{env_path}.new", s),
        f"{s}mv {env_path}.new {env_path}",
        _push_cmd_as(unit, f"/etc/systemd/system/{unit_name}", s),
        f"{s}systemctl daemon-reload", f"{s}systemctl enable {svc}",
        f"{s}systemctl restart {svc}", f'echo "VERA_LAUNCHED service={svc}"'])
    res = await _ssh(host_id, cmd, timeout=90)
    log_tail = ((res.get("stdout", "") or "") + "\n" + (res.get("stderr", "") or ""))[-2000:]
    if not (res.get("ok") and "VERA_LAUNCHED" in (res.get("stdout") or "")):
        return {"ok": False, "log": log_tail,
                "error": res.get("stderr") or res.get("error") or "unit install failed"}
    health = None
    if comp.get("health"):
        deadline = int(comp.get("start_timeout") or 300)
        probe = (f'for i in $(seq 1 {max(1, deadline // 10)}); do '
                 f'H=$(curl -sf -m 5 http://127.0.0.1:{port}{comp["health"]} 2>/dev/null) && '
                 f'{{ echo "VERA_HEALTH $H"; exit 0; }}; sleep 10; done; echo VERA_HEALTH_TIMEOUT')
        hres = await _ssh(host_id, probe, timeout=deadline + 60)
        line = next((ln for ln in (hres.get("stdout") or "").splitlines()
                     if ln.startswith("VERA_HEALTH")), "")
        if not line.startswith("VERA_HEALTH "):
            return {"ok": False, "log": log_tail,
                    "error": f"{svc} did not answer {comp['health']} within {deadline}s "
                             f"(see /var/log/gpu_inference.log on the node)"}
        try:
            health = json.loads(line[len("VERA_HEALTH "):])
        except ValueError:
            health = {"raw": line[:300]}
    return {"ok": True, "log": log_tail, "health": health}


async def _launch_nohup(host_id: str, key: str, run_cmd: str, env: Dict[str, str],
                        edge_dir: str = "") -> Dict:
    envp = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
    cmd = (
        f"cd {edge_dir or _EDGE_DIR} && "
        f"{{ {envp}nohup {run_cmd} > {key}.log 2>&1 & echo $! > {key}.pid ; }} && "
        f'sleep 1 && echo "VERA_LAUNCHED pid=$(cat {key}.pid 2>/dev/null)"'
    )
    res = await _ssh(host_id, cmd, timeout=40)
    log_tail = ((res.get("stdout", "") or "") + "\n" + (res.get("stderr", "") or ""))[-2000:]
    ok = bool(res.get("ok")) and "VERA_LAUNCHED" in (res.get("stdout", "") or "")
    return {"ok": ok, "log": log_tail, "error": "" if ok else (res.get("stderr") or res.get("error") or "")}


async def _launch_systemd(host_id: str, rec: Dict, key: str, comp: Dict,
                          run_cmd: str, env: Dict[str, str], sudo: bool,
                          edge_dir: str = "") -> Dict:
    s = _sudo_for(rec, sudo)
    envlines = "".join(f"Environment={k}={v}\\n" for k, v in env.items())
    # Heredoc expands $HOME on the host so ExecStart/WorkingDirectory are absolute.
    unit = (
        "[Unit]\\n"
        f"Description=Vera {comp['label']}\\nAfter=network-online.target\\n\\n"
        "[Service]\\nType=simple\\n"
        f"WorkingDirectory={edge_dir or _EDGE_DIR}\\n"
        f"{envlines}"
        f"ExecStart={run_cmd}\\n"
        "Restart=on-failure\\nRestartSec=3\\n\\n"
        "[Install]\\nWantedBy=multi-user.target\\n"
    )
    svc = f"vera-{key}.service"
    cmd = (
        f'printf "{unit}" | {s}tee /etc/systemd/system/{svc} >/dev/null && '
        # enable + RESTART, not `enable --now`: --now starts a stopped unit but
        # leaves a running one on the old code, so a redeploy changed nothing.
        f"{s}systemctl daemon-reload && {s}systemctl enable {svc} && "
        f"{s}systemctl restart {svc} && "
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
    svc = _COMPONENTS[component].get("service") or f"vera-{component}"
    if systemd or _COMPONENTS[component].get("service"):
        res = await _ssh(host_id, f"systemctl is-active {svc}.service", timeout=20)
        state = (res.get("stdout", "") or "").strip()
        return {"ok": True, "running": state == "active", "detail": state or "unknown"}
    res = await _ssh(
        host_id,
        pidfile_lookup_cmd(component) +
        f'if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then echo "running pid=$PID"; '
        f'else echo stopped; fi',
        timeout=20)
    out = (res.get("stdout", "") or "").strip()
    return {"ok": True, "running": out.startswith("running"), "detail": out or "unknown"}


@capability(
    "provision.component.version",
    http_method="POST", http_path="/provision/component/version", http_tags=["provision"],
    memory="off", silent=True,
    description="The version of a bundled component: what a deploy from this host "
                "would install now (a content hash of its files and deps), and, with "
                "host_id, what that node has deployed, read over SSH. Inputs: "
                "component (str!), host_id (str). Output: {ok, component, host:{version, "
                "files}, node:{version, files, deps_installed}|null, state: current|"
                "behind|unversioned|absent, changed:[file]}.",
)
async def cap_component_version(component: str = "", host_id: str = "",
                                trace_id=None) -> Dict:
    if component not in _COMPONENTS:
        return {"ok": False, "error": f"unknown component {component!r}",
                "components": list(_COMPONENTS)}
    host = host_component_version(component)
    out: Dict[str, Any] = {"ok": True, "component": component, "host": host}
    if not host_id:
        return out
    inst = _COMPONENTS[component].get("install_dir")
    res = await _ssh(host_id, version_lookup_cmd(component, candidates=[inst] if inst else None),
                     timeout=20)
    if not res.get("ok"):
        return {**out, "ok": False, "error": res.get("stderr") or res.get("error")
                or "ssh failed"}
    node = parse_version(res.get("stdout") or "")
    out["node"] = node or None
    out.update(compare_versions(host, node or {}))
    return out


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
            pidfile_lookup_cmd(component) +
            '[ -n "$PID" ] && kill "$PID" 2>/dev/null; rm -f "$PIDF"; echo VERA_STOPPED',
            timeout=20)
    ok = "VERA_STOPPED" in (res.get("stdout", "") or "")
    await emit_event({"type": "provision.component.stop", "component": component, "ok": ok})
    return {"ok": ok, "detail": (res.get("stdout", "") or res.get("stderr", "")).strip()}


# ═════════════════════════════════════════════════════════════════════════════
#  VERA WORKER  — docker container (reuse docker.worker.spawn) OR native process
# ═════════════════════════════════════════════════════════════════════════════
#: The commit this process is RUNNING, read once at import. HEAD moves when main
#: is promoted, before the restart that activates it - a node synced to HEAD in
#: that window would run code the host does not.
_RUNNING_COMMIT = _node_sync.read_git_head(str(_REPO))


async def _host_bundle() -> Dict[str, Any]:
    """The commit this host RUNS, as base64 tar.gz (vera/, edge/, requirements.txt).

    Shipping the host's own commit — rather than cloning from a remote — means a
    node worker runs exactly the code the host runs, and the node needs no git
    credentials (the nodes have none). Spawned through spawn_core: an asyncio
    subprocess under uvloop forks the whole server."""
    try:
        from Vera.vera.execution import spawn_core as _spawn
    except Exception:                                  # pragma: no cover
        from vera.execution import spawn_core as _spawn
    repo = shlex.quote(str(_REPO))
    git = f"git -c safe.directory='*' -C {repo}"
    commit = _RUNNING_COMMIT
    if not commit:
        head = await _spawn.run_argv(["sh", "-c", f"{git} rev-parse HEAD"], timeout=30)
        if not head.get("ok"):
            return {"ok": False, "error": "cannot read this host's commit: "
                    + (head.get("stderr") or head.get("error") or "")[:300]}
        commit = (head.get("stdout") or "").strip()
    arc = await _spawn.run_argv(
        # edge/ too: node_agent_capabilities imports node_runner_core from it
        ["sh", "-c", f"{git} archive --format=tar.gz {shlex.quote(commit)} "
                     f"vera edge requirements.txt | base64 -w0"],
        timeout=180, max_output=256_000_000)
    if not arc.get("ok") or not (arc.get("stdout") or "").strip():
        return {"ok": False, "error": "git archive failed: "
                + (arc.get("stderr") or arc.get("error") or "")[:300]}
    return {"ok": True, "commit": commit, "b64": arc["stdout"].strip()}


async def _registry_get(host_id: str) -> Dict[str, Any]:
    r = _orch.REDIS
    if r is None:
        return {}
    try:
        raw = await r.hget(_node_sync.REGISTRY_KEY, host_id)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


async def _registry_put(host_id: str, entry: Dict[str, Any]) -> None:
    r = _orch.REDIS
    if r is None:
        return
    try:
        await r.hset(_node_sync.REGISTRY_KEY, host_id, json.dumps({**entry, "host_id": host_id}))
    except Exception as e:
        log.warning("node worker registry write %s: %s", host_id, e)


async def _record_native_result(host_id: str, rec: Dict, ok: bool, commit: str,
                                error: str) -> None:
    """Every native provision is recorded, so the sync job knows the node exists,
    what it shipped, and how often it has failed."""
    nodename = ""
    if ok:
        hn = await _ssh(host_id, "hostname", timeout=20)
        nodename = (hn.get("stdout") or "").strip().splitlines()[0] if hn.get("ok") and (hn.get("stdout") or "").strip() else ""
    entry = _node_sync.record_after(await _registry_get(host_id), ok=ok, commit=commit,
                                    error=error, nodename=nodename)
    entry["host"] = rec.get("host", "")
    await _registry_put(host_id, entry)


def _ssh_stored_with_input():
    """exec's stdin-capable runner, from the module that actually registered
    exec.ssh.run (importing it by path again could load a second copy)."""
    import sys as _sys
    fn = _cap("exec.ssh.run")
    mod = _sys.modules.get(getattr(fn, "__module__", "") or "")
    return getattr(mod, "ssh_run_stored", None)


@capability(
    "provision.worker",
    http_method="POST", http_path="/provision/worker", http_tags=["provision"],
    memory="off",
    description="Provision a Vera worker that joins the cluster (consumes the task "
                "stream via the shared REDIS_URL). Inputs: host_id (str!), mode "
                "('docker'|'native'), name (str), image (str — docker), gpus (str "
                "— 'all'), source (str — native: 'host' ships THIS host's checked-out "
                "commit over SSH (default), 'git' clones repo_url / VERA_REPO_URL), "
                "repo_url (str), port (int=8990 — native orchestrator port), "
                "redis_url (str — default this orchestrator's), threads (int=2 — "
                "native: the worker's BLAS/OpenMP pool size), timeout (int=1200). "
                "docker → registers the host as an SSH Docker host then "
                "docker.worker.spawn. native → installs under the first writable of "
                "/opt/vera/worker, /var/lib/vera/worker, $HOME/.vera/worker, as a "
                "systemd unit in the NODE-WORKER role (VERA_IS_WORKER=1: node-safe "
                "caps only, no ambient scheduler), at lower CPU priority than the "
                "node's own services. Re-running refreshes the code and restarts it. "
                "Output: {ok, mode, ...}.",
)
async def cap_worker(host_id: str = "", mode: str = "docker", name: str = "",
                     image: str = "", gpus: str = "", repo_url: str = "",
                     port: int = 8990, redis_url: str = "", timeout: int = 1200,
                     backend_host: str = "", source: str = "", threads: int = 0,
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
        src_kind = (source or ("git" if repo_url else "host")).strip().lower()
        repo = repo_url or os.getenv("VERA_REPO_URL", "")
        if src_kind not in ("host", "git"):
            return {"ok": False, "mode": "native",
                    "error": f"unknown source: {source} (use 'host' or 'git')"}
        if src_kind == "git" and not repo:
            return {"ok": False, "mode": "native",
                    "error": "source=git needs a repo_url (or VERA_REPO_URL on the Vera host); "
                             "source=host ships this host's own commit instead."}
        # A remote worker can't reach the orchestrator's own localhost stores — re-point
        # every backend URL at a LAN-reachable address (this box's IP, or backend_host).
        bh = (backend_host or os.getenv("VERA_ADVERTISE_HOST", "") or _primary_lan_ip())
        redis_url = rewrite_host(redis_url, bh)
        # Never hand on the HOST's Redis credential: a node worker gets its own
        # ACL user (vera-node), read from OpenBao, when one exists. It lands in
        # the node's 0600 worker.env, not the unit (native_worker_cmd).
        import sys as _sys
        _ra = _sys.modules.get("redis_auth_capabilities")
        redis_url = (await _ra.node_redis_url(redis_url)) if _ra is not None \
            else _redis_auth_core.without_credentials(redis_url)
        # Environment first, else the host's effective config (prod runs on
        # config.py's localhost defaults and exports none of these).
        _cfg = getattr(_orch, "cfg", None)
        backend_kv = worker_backend_env(
            dict(os.environ),
            {k: getattr(_cfg, k, None) for k in WORKER_BACKEND_KEYS}, bh)

        # Where to install. Never assume $HOME: /root on these unprivileged LXC
        # nodes is nobody:root 0700 (see components_core.WORKER_DIR_CANDIDATES).
        probe = await _ssh(host_id, edge_dir_probe_cmd(_WORKER_DIR_CANDIDATES), timeout=40)
        root = parse_edge_dir(probe.get("stdout") or "")
        if not root:
            return {"ok": False, "mode": "native",
                    "error": ("could not probe the target over SSH: "
                              f"{probe.get('error') or probe.get('stderr') or 'no response'}")
                    if not probe.get("ok") else
                    ("no writable install directory on the target (tried "
                     f"{', '.join(_WORKER_DIR_CANDIDATES)})")}

        bundle: Dict[str, Any] = {}
        # The install command carries the credentials file (base64 is not
        # encryption), so it never goes through exec.ssh.run - a capability
        # whose arguments are recorded - but the internal runner.
        runner = _ssh_stored_with_input()
        if runner is None:
            return {"ok": False, "mode": "native",
                    "error": "exec module has no ssh_run_stored (stdin transport)"}
        if src_kind == "host":
            bundle = await _host_bundle()
            if not bundle.get("ok"):
                return {"ok": False, "mode": "native", "error": bundle.get("error")}

        extra_env = {
            # the node-worker role: node-safe caps only, no ambient scheduler
            "VERA_IS_WORKER": "1",
            # A worker talks to the cluster through Redis only. Its HTTP app
            # would otherwise serve every capability on the LAN from each node;
            # loopback keeps it for local diagnosis. (Later Environment= lines
            # win in systemd, so this overrides native_worker_cmd's 0.0.0.0.)
            "ORCHESTRATOR_HOST": "127.0.0.1",
            # /root is unreachable on these nodes; anything defaulting a cache
            # under $HOME would die with a permission error
            "HOME": "/",
            **_placement.worker_thread_env(int(threads or _placement.DEFAULT_WORKER_THREADS)),
        }
        if bundle.get("commit"):
            extra_env["VERA_WORKER_COMMIT"] = bundle["commit"]
        # Its identity for the roles registry, and the classes it starts with
        # until the Workers UI sets some (a GPU node's worker takes none).
        _has_gpu = _node_has_gpu(rec.get("host", ""))
        extra_env["VERA_WORKER_HOST_ID"] = host_id
        # "none", never "": the unit drops empty values, and a worker with no
        # VERA_WORKER_CLASSES at all falls back to the CPU-node default - which
        # is how the GPU node's worker came up taking General + NLP.
        extra_env["VERA_WORKER_CLASSES"] = (",".join(_placement.default_classes(_has_gpu))
                                            or _placement.NO_CLASSES)
        # native_worker_cmd handles the repo's vera/ package layout, a neutral cwd (so
        # vera/operator can't shadow stdlib operator), and a durable systemd unit.
        cmd = native_worker_cmd(root=root, repo=repo, redis_url=redis_url,
                                backend_kv=backend_kv, port=int(port),
                                bundle=(src_kind == "host"), extra_env=extra_env,
                                nice=_placement.WORKER_NICE,
                                cpu_weight=_placement.WORKER_CPU_WEIGHT)
        res = await runner(host_id, cmd, timeout=int(timeout or 1200),
                           input=bundle["b64"] if src_kind == "host" else None)
        ok = bool(res.get("ok")) and "VERA_LAUNCHED" in (res.get("stdout", "") or "")
        await emit_event({"type": "provision.worker", "mode": "native", "host": rec.get("host", ""),
                          "ok": ok, "source": src_kind, "commit": bundle.get("commit", "")})
        if src_kind == "host":
            await _record_native_result(
                host_id, rec, ok, bundle.get("commit", ""),
                "" if ok else (res.get("stderr") or res.get("error") or "launch failed"))
        return {"ok": ok, "mode": "native", "source": src_kind, "root": root,
                "recorded": src_kind == "host",
                "commit": bundle.get("commit", ""), "port": int(port), "backend_host": bh,
                "log": ((res.get("stdout", "") or "") + "\n" + (res.get("stderr", "") or ""))[-3000:],
                "error": "" if ok else (res.get("stderr") or res.get("error") or "native worker launch failed")}

    return {"ok": False, "error": f"unknown mode: {mode} (use 'docker' or 'native')"}


# ═════════════════════════════════════════════════════════════════════════════
#  NODE WORKER SYNC — every node worker follows the commit the host runs
# ═════════════════════════════════════════════════════════════════════════════
#: Held in Redis, not in-process: it must hold across every process that could
#: run the tick, and across module copies (a module body can run more than once).
_SYNC_LOCK_KEY = "vera:node_workers:lock"
_SYNC_LOCK_TTL = 1800          # a provision's own ceiling is 1200 s


async def _sync_enabled() -> bool:
    if os.getenv("VERA_NODE_SYNC", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    r = _orch.REDIS
    try:
        raw = await r.get(_node_sync.CONFIG_KEY) if r is not None else None
        return bool(json.loads(raw).get("enabled", True)) if raw else True
    except Exception:
        return True


async def _live_node_workers() -> List[Dict[str, Any]]:
    r = _orch.REDIS
    out: List[Dict[str, Any]] = []
    if r is None:
        return out
    async for k in r.scan_iter("vera:workers:*"):
        try:
            h = {(a.decode() if isinstance(a, bytes) else a): (b.decode() if isinstance(b, bytes) else b)
                 for a, b in (await r.hgetall(k)).items()}
        except Exception:
            continue
        if h.get("role") == "node-worker":
            out.append({"host": h.get("host", ""), "status": h.get("status", ""),
                        "commit": h.get("commit", ""), "role": "node-worker",
                        "id": h.get("id", "")})
    return out


async def _census_busy() -> bool:
    try:
        return bool((await _orch._health_census()).get("busy"))
    except Exception:
        return True          # cannot tell -> assume busy; the next tick asks again


@capability(
    "nodes.workers.sync",
    http_method="POST", http_path="/nodes/workers/sync", http_tags=["nodes", "provision"],
    memory="off",
    description="Bring node workers onto the commit this host is RUNNING (not git HEAD: "
                "a promotion moves HEAD before the restart that activates it). Refreshes "
                "at most `limit` stale or missing node per call through provision.worker, "
                "never while a census goal is in flight or on a node whose worker is "
                "mid-task, with backoff after failures. Runs on its own every 10 min on "
                "the host (off with VERA_NODE_SYNC=off or enabled=false). Inputs: "
                "dry_run (bool=false), limit (int=1), host_ids (list — adopt these "
                "already-provisioned nodes into the registry first), enabled (bool — "
                "persist the on/off switch). Output: {ok, host_commit, plan, results}.",
)
async def cap_nodes_workers_sync(dry_run: bool = False, limit: int = 1,
                                 host_ids: Optional[List[str]] = None,
                                 enabled: Optional[bool] = None, trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "redis not connected"}
    if enabled is not None:
        await r.set(_node_sync.CONFIG_KEY, json.dumps({"enabled": bool(enabled)}))
    for hid in host_ids or []:
        if not await _registry_get(hid):
            rec = await _host_rec(hid)
            if not rec:
                return {"ok": False, "error": f"host_id not found: {hid}"}
            await _registry_put(hid, {"host": rec.get("host", ""), "commit": "",
                                      "failures": 0, "last_attempt": 0, "adopted": True})
    raw = await r.hgetall(_node_sync.REGISTRY_KEY)
    entries = []
    for v in (raw or {}).values():
        try:
            entries.append(json.loads(v))
        except Exception:
            continue
    p = _node_sync.plan(_RUNNING_COMMIT, entries, await _live_node_workers(),
                        census_busy=await _census_busy(), limit=limit)
    out: Dict[str, Any] = {"ok": True, "host_commit": _RUNNING_COMMIT,
                           "enabled": await _sync_enabled(), "plan": p, "results": {}}
    if dry_run or not p["run"]:
        return out
    if _in_sandbox():
        out.update(ok=False, error=_SANDBOX_REFUSAL)
        return out
    token = _orch.new_id()
    if not await r.set(_SYNC_LOCK_KEY, token, nx=True, ex=_SYNC_LOCK_TTL):
        out["results"] = {"_": "a sync is already running"}
        return out
    try:
        for hid in p["run"]:
            await emit_event({"type": "nodes.workers.sync", "host_id": hid, "stage": "start",
                              "commit": _RUNNING_COMMIT})
            try:
                res = await cap_worker(host_id=hid, mode="native", source="host")
            except Exception as e:
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if not res.get("recorded"):
                rec = await _host_rec(hid) or {}
                entry = _node_sync.record_after(await _registry_get(hid), ok=False,
                                                error=str(res.get("error") or "provision failed"))
                entry["host"] = rec.get("host", "")
                await _registry_put(hid, entry)
            out["results"][hid] = {"ok": bool(res.get("ok")), "commit": res.get("commit", ""),
                                   "error": str(res.get("error") or "")[:300]}
            await emit_event({"type": "nodes.workers.sync", "host_id": hid, "stage": "done",
                              "ok": bool(res.get("ok")), "error": str(res.get("error") or "")[:200]})
    finally:
        try:
            held = await r.get(_SYNC_LOCK_KEY)
            if (held.decode() if isinstance(held, bytes) else held) == token:
                await r.delete(_SYNC_LOCK_KEY)
        except Exception:
            pass
    return out


_SANDBOX_REFUSAL = ("this is a dev sandbox: provisioning from here would join PROD's nodes to "
                    "the sandbox's private Redis - use the host's Workers page")

_COMPONENT_SYNC_LOCK = "vera:component_sync:lock:{component}"


async def _tagged_hosts(tag: str) -> List[Dict[str, str]]:
    """Stored SSH hosts carrying `tag`: [{host_id, host}]."""
    try:
        hosts = ((await _cap("exec.ssh.hosts.list")()) or {}).get("hosts", [])
    except Exception:
        hosts = []
    return [{"host_id": h["id"], "host": h.get("host", "")} for h in hosts
            if h.get("id") and tag in (h.get("tags") or [])]


async def component_hosts(component: str) -> List[Dict[str, str]]:
    """Where a component runs: its tagged hosts, else the Ollama nodes."""
    tag = (_COMPONENTS.get(component) or {}).get("hosts_tag")
    return await _tagged_hosts(tag) if tag else await _ollama_node_hosts()


async def _ollama_node_hosts() -> List[Dict[str, str]]:
    """The Ollama nodes that have a stored SSH credential: [{host_id, host}]."""
    from urllib.parse import urlparse
    try:
        hosts = ((await _cap("exec.ssh.hosts.list")()) or {}).get("hosts", [])
    except Exception:
        hosts = []
    by_addr = {h.get("host"): h.get("id") for h in hosts if h.get("host") and h.get("id")}
    out, seen = [], set()
    for inst in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).values():
        addr = urlparse(str(inst.get("url") or "")).hostname or ""
        if addr in by_addr and addr not in seen:
            seen.add(addr)
            out.append({"host_id": by_addr[addr], "host": addr})
    return out


@capability(
    "provision.component.sync",
    http_method="POST", http_path="/provision/component/sync", http_tags=["provision", "nodes"],
    memory="off",
    description="Bring every node that runs a component onto the version this host would "
                "deploy (see provision.component.version) - 'update all nodes to the same "
                "version'. Only components every node runs the same way are syncable "
                "(nlp_server, model_builder). Per node: already current -> left alone; never deployed -> "
                "left alone (a sync updates, it does not spread); otherwise redeployed, "
                "reinstalling dependencies only when they changed. One node at a time, "
                "never while a census goal is in flight, never from a dev sandbox. Inputs: "
                "component (str='nlp_server'), host_ids (list - default: the hosts tagged "
                "for the component, else every Ollama node with a stored SSH credential), dry_run (bool=true), force (bool - redeploy "
                "even a current node, e.g. one edited by hand). Output: {ok, component, "
                "host_version, plan[], results{}}.",
)
async def cap_component_sync(component: str = "nlp_server",
                             host_ids: Optional[List[str]] = None,
                             dry_run: bool = True, force: bool = False,
                             trace_id=None) -> Dict:
    comp = _COMPONENTS.get(component)
    if not comp or not comp.get("sync"):
        return {"ok": False, "error": f"{component!r} is not syncable",
                "syncable": [k for k, c in _COMPONENTS.items() if c.get("sync")]}
    nodes = ([{"host_id": h, "host": ((await _host_rec(h)) or {}).get("host", "")}
              for h in host_ids] if host_ids else await component_hosts(component))
    rows = []
    for n in nodes:
        v = await cap_component_version(component=component, host_id=n["host_id"])
        if not v.get("ok"):
            rows.append({**n, "state": "unreachable", "error": v.get("error", "")})
            continue
        st = await cap_component_status(host_id=n["host_id"], component=component,
                                        systemd=bool(comp.get("systemd")))
        rows.append({**n, "state": v.get("state"), "changed": v.get("changed") or [],
                     "running": bool(st.get("running"))})
    reachable = [r for r in rows if r["state"] != "unreachable"]
    plan = component_sync_plan(reachable)
    if force:
        for p in plan:
            if p["action"] == "skip" and p["state"] == "current":
                p.update(action="deploy", install_deps=False, why="forced")
    plan += [{**{k: r[k] for k in ("host_id", "host", "state")}, "action": "skip",
              "why": "unreachable: " + str(r.get("error") or "")[:200]}
             for r in rows if r["state"] == "unreachable"]
    out: Dict[str, Any] = {"ok": True, "component": component,
                           "host_version": host_component_version(component).get("version", ""),
                           "plan": plan, "results": {}}
    todo = [p for p in plan if p["action"] == "deploy"]
    if dry_run or not todo:
        return out
    if _in_sandbox():
        return {**out, "ok": False, "error": _SANDBOX_REFUSAL}
    if await _census_busy():
        return {**out, "ok": False,
                "error": "a census goal is in flight - redeploying would drop its NLP calls; "
                         "try again when it yields"}
    r = _orch.REDIS
    lock = _COMPONENT_SYNC_LOCK.format(component=component)
    token = _orch.new_id()
    if r is not None and not await r.set(lock, token, nx=True, ex=_SYNC_LOCK_TTL):
        return {**out, "ok": False, "error": f"a {component} sync is already running"}
    try:
        for p in todo:
            await emit_event({"type": "provision.component.sync", "component": component,
                              "host_id": p["host_id"], "stage": "start"})
            try:
                res = await cap_deploy(host_id=p["host_id"], component=component,
                                       install_deps=bool(p.get("install_deps")),
                                       launch=True, systemd=bool(comp.get("systemd")))
            except Exception as e:
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            out["results"][p["host_id"]] = {"ok": bool(res.get("ok")),
                                            "version": res.get("version", ""),
                                            "error": str(res.get("error") or "")[:300]}
            await emit_event({"type": "provision.component.sync", "component": component,
                              "host_id": p["host_id"], "stage": "done",
                              "ok": bool(res.get("ok"))})
    finally:
        try:
            held = await r.get(lock) if r is not None else None
            if (held.decode() if isinstance(held, bytes) else held) == token:
                await r.delete(lock)
        except Exception:
            pass
    out["ok"] = all(v["ok"] for v in out["results"].values())
    return out


def _in_sandbox() -> bool:
    try:
        return bool(_orch.is_dev_sandbox())
    except Exception:
        return False


def _node_has_gpu(addr: str) -> bool:
    """Whether the node at `addr` is a GPU node, from the Ollama registry (the
    one place that already says so)."""
    from urllib.parse import urlparse
    for inst in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).values():
        try:
            if urlparse(str(inst.get("url") or "")).hostname == addr:
                return bool(inst.get("has_gpu"))
        except Exception:
            continue
    return False


async def _roles_get(host_id: str, has_gpu: bool):
    """(classes, is_default)."""
    r = _orch.REDIS
    try:
        raw = await r.hget(_placement.ROLES_KEY, host_id) if r is not None else None
        if raw:
            return list(_placement.clean_classes(json.loads(raw))), False
    except Exception:
        pass
    return list(_placement.default_classes(has_gpu)), True


@capability(
    "nodes.workers.list",
    http_method="GET", http_path="/nodes/workers", http_tags=["nodes", "provision"],
    memory="off", silent=True,
    description="Everything the Workers UI shows about node workers: the task classes "
                "(label, what they cover, how many capabilities are vetted into each), "
                "each registered node (address, GPU or CPU, its classes and whether they "
                "are the default, its live worker - status, commit, caps - whether it is "
                "on the commit this host runs, failures), Ollama nodes with an SSH "
                "credential but no worker yet (candidates to provision), and the sync "
                "plan. Output: {host_commit, sync_enabled, classes[], nodes[], "
                "candidates[], plan}.",
)
async def cap_nodes_workers_list(trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "redis not connected"}
    reg = {}
    for v in (await r.hgetall(_node_sync.REGISTRY_KEY) or {}).values():
        try:
            e = json.loads(v)
            reg[e.get("host_id")] = e
        except Exception:
            continue
    live = await _live_node_workers()
    by_node = {w.get("host"): w for w in live}
    counts: Dict[str, int] = {}
    for c in _orch.CAPABILITY_REGISTRY:
        k = _placement.class_of(c)
        if k:
            counts[k] = counts.get(k, 0) + 1
    classes = [{"key": k, "label": v["label"], "desc": v["desc"], "caps": counts.get(k, 0)}
               for k, v in _placement.CLASSES.items()]
    nodes = []
    for hid, e in sorted(reg.items(), key=lambda kv: str(kv[1].get("host") or "")):
        gpu = _node_has_gpu(e.get("host", ""))
        cls, is_default = await _roles_get(hid, gpu)
        w = by_node.get(e.get("nodename") or "") or {}
        nodes.append({
            "host_id": hid, "host": e.get("host", ""), "nodename": e.get("nodename", ""),
            "has_gpu": gpu, "classes": cls, "classes_default": is_default,
            "worker": {"online": bool(w), "status": w.get("status", ""),
                       "commit": w.get("commit", "") or e.get("commit", ""),
                       "id": w.get("id", "")},
            "in_sync": bool(w) and (w.get("commit") or "") == _RUNNING_COMMIT,
            "failures": int(e.get("failures") or 0), "last_error": e.get("last_error", ""),
            "provisioned_at": e.get("provisioned_at"),
        })
    # Ollama nodes with an SSH credential and no worker yet
    known = {e.get("host") for e in reg.values()}
    candidates = []
    try:
        hosts = ((await _cap("exec.ssh.hosts.list")()) or {}).get("hosts", [])
    except Exception:
        hosts = []
    from urllib.parse import urlparse
    for iid, inst in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).items():
        addr = urlparse(str(inst.get("url") or "")).hostname or ""
        if not addr or addr in known:
            continue
        h = next((h for h in hosts if h.get("host") == addr), None)
        candidates.append({"instance": iid, "host": addr, "label": inst.get("label", iid),
                           "has_gpu": bool(inst.get("has_gpu")),
                           "host_id": (h or {}).get("id", ""),
                           "ssh": bool(h)})
    plan = _node_sync.plan(_RUNNING_COMMIT, list(reg.values()), live,
                           census_busy=await _census_busy())
    return {"ok": True, "host_commit": _RUNNING_COMMIT, "sync_enabled": await _sync_enabled(),
            "classes": classes, "nodes": nodes, "candidates": candidates, "plan": plan,
            "sandbox": _in_sandbox(), "sandbox_note": _SANDBOX_REFUSAL if _in_sandbox() else ""}


@capability(
    "nodes.workers.roles.set",
    http_method="POST", http_path="/nodes/workers/roles", http_tags=["nodes", "provision"],
    memory="off",
    description="Set the task classes a node's worker takes (general, nlp, cpu_compute, "
                "media). Applies live: the worker re-reads its roles within 30 s and "
                "from then reads only those classes' task streams - no restart. "
                "reset=true returns the node to its default (CPU node: general + nlp; "
                "GPU node: none). Inputs: host_id (str!), classes (list), reset (bool). "
                "Output: {ok, host_id, classes}.",
)
async def cap_nodes_workers_roles_set(host_id: str = "", classes: Optional[List[str]] = None,
                                      reset: bool = False, trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None or not host_id:
        return {"ok": False, "error": "host_id required" if host_id else "redis not connected"}
    unknown = [c for c in (classes or []) if c not in _placement.CLASSES]
    if unknown:
        return {"ok": False, "error": "unknown classes %s (known: %s)"
                % (unknown, list(_placement.CLASSES))}
    if reset:
        await r.hdel(_placement.ROLES_KEY, host_id)
        rec = await _host_rec(host_id) or {}
        cls, _ = await _roles_get(host_id, _node_has_gpu(rec.get("host", "")))
    else:
        cls = list(_placement.clean_classes(classes or []))
        await r.hset(_placement.ROLES_KEY, host_id, json.dumps(cls))
    await emit_event({"type": "nodes.workers.roles", "host_id": host_id, "classes": cls,
                      "reset": bool(reset)})
    return {"ok": True, "host_id": host_id, "classes": cls}


@capability(
    "nodes.workers.provision",
    http_method="POST", http_path="/nodes/workers/provision", http_tags=["nodes", "provision"],
    memory="off",
    description="One click: install (or refresh to this host's running commit) the Vera "
                "worker on a node - native, over its stored SSH credential - and record it "
                "for the sync. The same path the automatic sync uses. Input: host_id (str!). "
                "Output: {ok, commit, error}.",
)
async def cap_nodes_workers_provision(host_id: str = "", trace_id=None) -> Dict:
    if not host_id:
        return {"ok": False, "error": "host_id required"}
    if _in_sandbox():
        return {"ok": False, "error": _SANDBOX_REFUSAL}
    res = await cap_worker(host_id=host_id, mode="native", source="host")
    if not res.get("recorded"):
        rec = await _host_rec(host_id) or {}
        entry = _node_sync.record_after(await _registry_get(host_id), ok=False,
                                        error=str(res.get("error") or "provision failed"))
        entry["host"] = rec.get("host", "")
        await _registry_put(host_id, entry)
    return {"ok": bool(res.get("ok")), "commit": res.get("commit", ""),
            "error": str(res.get("error") or "")[:500]}


_NODE_WORKERS_EL = Path(__file__).resolve().parents[1] / "workers" / "node_workers_element.js"


@APP.get("/ui/elements/node_workers.js", include_in_schema=False)
async def _node_workers_element_js():
    from fastapi.responses import Response
    try:
        body = _NODE_WORKERS_EL.read_text(encoding="utf-8")
    except OSError:
        body = "console.error('node_workers_element.js not found')"
    return Response(body, media_type="application/javascript")


@APP.get("/nodes/workers/panel", include_in_schema=False)
async def _node_workers_panel():
    """The element as a page of its own (the registered panel embeds this)."""
    return HTMLResponse("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<script>(function(){try{var d=document.documentElement,S=window.localStorage;
var t=S.getItem('vera:ui:theme');if(t)d.setAttribute('data-theme',t);
var vf=S.getItem('vera:ui:themeVarsFor');if(t&&vf!==t)return;var v=JSON.parse(S.getItem('vera:ui:themeVars')||'null');
if(v)for(var k in v)d.style.setProperty(k,v[k]);}catch(e){}})();</script>
<title>Vera - Node workers</title>
<style>:root{--bg:#0d0f12;--bg1:#14181d;--bg2:#1a1f26;--border:#232a33;--border2:#2e3742;--fg:#d8dde3;
--dim:#5f6975;--acc:#4a9eff;--acc2:#28c28a;--warn:#f5b341;--err:#ef5b5b}
html,body{margin:0;background:var(--bg0,var(--bg));color:var(--fg);height:100%}</style></head>
<body><vera-node-workers></vera-node-workers>
<script src="/ui/vera-ui.js"></script><script src="/ui/elements/node_workers.js"></script></body></html>""")


register_ui(
    "node-workers", "Node workers", "⚙",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/nodes/workers/panel" style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"></iframe>
</div>""",
    "",
    ui_caps=["nodes.workers.list", "nodes.workers.roles.set", "nodes.workers.provision",
             "nodes.workers.sync"],
    # an element of the Workers pane (and any dashboard), not a tab of its own
    mode="element",
    tab_order=74,
)


async def _node_sync_tick():
    """The scheduled half. Host-only (a node worker runs no periodic job), never
    in a sandbox (it would provision PROD's nodes), leader-only (singleton)."""
    if not await _sync_enabled():
        return
    try:
        res = await cap_nodes_workers_sync()
        if res.get("results"):
            log.info("node worker sync: %s", res["results"])
    except Exception as e:
        log.warning("node worker sync tick: %s", e)


async def _node_sync_first():
    # one pass shortly after a boot - a restart is how new code arrives
    await asyncio.sleep(_node_sync.FIRST_TICK_DELAY_S)
    await _node_sync_tick()


schedule(_node_sync_tick, interval=_node_sync.TICK_S, name="node_worker_sync",
         skip_in_sandbox=True, singleton=True)
schedule(_node_sync_first, interval=_placement.STARTUP_INTERVAL, name="node_worker_sync_boot",
         skip_in_sandbox=True, singleton=True)


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL  (served for completeness; the UI lives in the Provision tab)
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/provision/components/panel", include_in_schema=False)
async def _components_panel():
    p = _HERE / "provision_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>provision_panel.html not found</p>")


log.info("components_capabilities ready — %d deployable components", len(_COMPONENTS))
