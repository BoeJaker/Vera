"""
node_backend_core.py - moving a compute node between its Ollama and its vLLM
service in place (nodes.backend.switch). Pure: shell text and decisions only.

A node keeps its identity. Its Ollama instance id (e.g. "cpu-246") stays in
routing; while it is switched:
  * the Ollama instance is disabled, and a vLLM instance "vllm-<id>" with
    gate_as=<id> is registered, so its calls take the node's own semaphore and
    GPU gate;
  * a routing pin naming the node resolves to that vLLM instance;
  * the tune / tap / warm steps of a release sync leave the node alone - they
    would otherwise restart or load the Ollama that was deliberately stopped.

Both services are disabled/enabled, not only stopped/started, so a CT reboot
comes back in the backend it was switched to.
"""
from __future__ import annotations

import shlex
from typing import Any, Dict, Iterable, List, Optional

BACKENDS = ("vllm", "ollama")

#: the fleet's Ollama unit; a fresh stock install has `ollama`
OLLAMA_UNIT_EXPR = ("$(systemctl cat ollama-vera >/dev/null 2>&1 && echo ollama-vera "
                    "|| echo ollama)")
DONE = "VERA_SWITCHED"

#: where a switch to vLLM records which Ollama unit it stopped, per public port
UNIT_STATE = "/var/lib/vera/backend-ollama-unit.{port}"


def ollama_unit_expr(public_port: int) -> str:
    """Shell: the systemd unit of the Ollama behind THIS instance's public port.

    A container can hold more than one Ollama (CT126: gpu-250 on 11435 and its
    CPU sibling gpu-250-cpu on 11436). The switch used to stop `ollama-vera`
    whichever instance it was asked to move - switching the CPU sibling would
    have stopped the GPU's Ollama (5 Oct 2026). The unit is the one that owns
    the process listening on the instance's upstream port: the tap's upstream
    (/etc/vera-tap/<port>.env) when it is tapped, else the port itself. A unit
    recorded by an earlier switch wins (nothing listens once it is stopped);
    the fleet default is the last resort."""
    p = int(public_port)
    state = UNIT_STATE.format(port=p)
    return (
        "$( if [ -s " + state + " ]; then cat " + state + "; else "
        "UP=$(sed -n 's#^VERA_TAP_UPSTREAM=.*:\\([0-9][0-9]*\\).*#\\1#p' /etc/vera-tap/" + str(p) + ".env 2>/dev/null | head -1); "
        "UP=${UP:-" + str(p) + "}; "
        "PID=$(ss -ltnpH \"sport = :$UP\" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2); "
        "UN=$( [ -n \"$PID\" ] && ps -o unit= -p \"$PID\" 2>/dev/null | tr -d ' ' ); "
        "case \"$UN\" in *.service) echo \"$UN\";; "
        "*) systemctl cat ollama-vera >/dev/null 2>&1 && echo ollama-vera || echo ollama;; esac; fi )"
    )


#: A container runs one vLLM per slot. The second slot is for a CPU Ollama that
#: shares its container with a GPU one (CT126: gpu-250-cpu beside gpu-250) - its
#: own CPU build, service and port, so the GPU's vLLM stays free for the GPU.
SLOT_DEFAULT, SLOT_CPU = "default", "cpu"
SLOT_COMPONENT = {SLOT_DEFAULT: "vllm_server", SLOT_CPU: "vllm_server_cpu"}


def _host(inst: Dict[str, Any]) -> str:
    from urllib.parse import urlparse
    return urlparse(str((inst or {}).get("url") or "")).hostname or ""


def slot_for(ollama_id: str, ollama: Dict[str, Dict[str, Any]]) -> str:
    """The vLLM slot an Ollama instance moves into: the CPU slot for a CPU
    instance on a container that also hosts a GPU instance, else the default."""
    me = (ollama or {}).get(ollama_id) or {}
    if me.get("has_gpu"):
        return SLOT_DEFAULT
    host = _host(me)
    if host and any(i != ollama_id and o.get("has_gpu") and _host(o) == host
                    for i, o in (ollama or {}).items()):
        return SLOT_CPU
    return SLOT_DEFAULT


def vllm_instance_id(ollama_id: str) -> str:
    """The id of the node's vLLM instance: the registry entry for the vLLM service
    on the SAME container as its Ollama (port 8000), used while it is switched."""
    return f"vllm-{ollama_id}"


def _f(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def switched_ids(ollama: Dict[str, Dict[str, Any]], vllm: Dict[str, Any]) -> List[str]:
    """Ollama node ids currently switched to vLLM: the Ollama instance is
    disabled and a vLLM instance gates as it."""
    gated = {str(_f(v, "gate_as", "") or "") for v in (vllm or {}).values()}
    return sorted(i for i, inst in (ollama or {}).items()
                  if i in gated and not (inst or {}).get("enabled", True))


def vllm_of(node_id: str, vllm: Dict[str, Any]) -> str:
    """The vLLM instance that gates as `node_id` ('' when none does)."""
    for vid, v in (vllm or {}).items():
        if node_id and str(_f(v, "gate_as", "") or "") == node_id:
            return vid
    return ""


def standin_view(ollama_inst: Dict[str, Any], vinst: Any) -> Dict[str, Any]:
    """A switched node as the Ollama picker sees it: its own record (id,
    priority, gate) with its vLLM server's state - online, the names it serves,
    its load. The picker scores it like any node; the caller then serves the
    pick on vLLM."""
    v = dict(ollama_inst or {})
    load = max(int(v.get("in_use", 0) or 0), int(_f(vinst, "in_use", 0) or 0))
    v.update(status="online", enabled=True, backend="vllm", running=[],
             models=list(_f(vinst, "models", []) or [])
             + [n for n in (_f(vinst, "stands_in_for", []) or []) if n],
             has_gpu=bool(_f(vinst, "has_gpu", v.get("has_gpu", False))),
             in_use=load + int(_f(vinst, "waiting_requests", 0) or 0))
    return v


def standins(ollama: Dict[str, Dict[str, Any]], vllm: Dict[str, Any],
             model: Optional[str] = None, serves=None) -> Dict[str, Dict[str, Any]]:
    """{node id: standin_view} for every switched node whose vLLM server is up
    and - when a model is asked for - serves it (by name or alias). A switched
    node is never offered for a model its server does not have: an embedding
    call must not land on a chat model's server."""
    out: Dict[str, Dict[str, Any]] = {}
    for oid in switched_ids(ollama, vllm):
        v = (vllm or {}).get(vllm_of(oid, vllm))
        if v is None or _f(v, "status") not in ("online", "degraded"):
            continue
        if model and serves is not None and not serves(v, model):
            continue
        out[oid] = standin_view((ollama or {}).get(oid) or {}, v)
    return out


def liveness(vinst: Any, vid: str) -> Dict[str, Any]:
    """What a switched node's own record should say about it: the state of the
    vLLM server that serves it. The node's Ollama is stopped on purpose, so
    pinging it reported every switched node "offline" in /health, the monitor,
    the cluster view and node activity (5 Oct 2026)."""
    st = str(_f(vinst, "status", "") or "")
    return {"status": "online" if st in ("online", "degraded") else "offline",
            "latency_ms": _f(vinst, "latency_ms", None),
            "models": list(_f(vinst, "models", []) or []),
            "backend": "vllm", "served_by": vid, "errors": 0}


def resolve_pin(pin: str, ollama: Dict[str, Dict[str, Any]], vllm: Dict[str, Any]) -> str:
    """A pin naming a switched Ollama node -> "vllm:<its vLLM instance>"; any
    other pin unchanged. Rules keep saying "cpu-246" while the node serves vLLM."""
    p = str(pin or "")
    if not p or p.startswith(("vllm:", "provider:")) or p not in (ollama or {}):
        return p
    if p not in switched_ids(ollama, vllm):
        return p
    for vid, v in (vllm or {}).items():
        if str(_f(v, "gate_as", "") or "") == p:
            return f"vllm:{vid}"
    return p


def serve_args(model: str, aliases: Iterable[str] = (), extra: str = "") -> str:
    """vllm serve arguments after the model: the served names (the model and
    the Ollama tags it stands in for, so routed calls asking for those resolve)
    and anything extra."""
    names = [model] + [a for a in (aliases or []) if a and a != model]
    out = "--served-model-name " + " ".join(shlex.quote(n) for n in names)
    return out + (f" {extra}" if extra else "")


def reasoning_parser_for(model: str, aliases: Iterable[str] = ()) -> str:
    """The vLLM reasoning parser a model needs so its thinking comes back in its
    own field (as Ollama returns `thinking` apart from the answer): qwen3 for
    the Qwen3 family (Qwen3, Qwen3.5, Qwen3.6), '' when unknown."""
    names = " ".join([str(model or "")] + [str(a) for a in aliases or ()]).lower()
    return "qwen3" if "qwen3" in names else ""


def env_file(model: str, *, aliases: Iterable[str] = (), extra: str = "",
             threads: int = 0, has_gpu: bool = False) -> str:
    """The unit environment for the vLLM service on a switched node. On a CPU
    node OMP_NUM_THREADS is the node's Ollama runner thread count (parity).
    A thinking model gets its reasoning parser unless `extra` names one."""
    rp = reasoning_parser_for(model, aliases)
    if rp and "--reasoning-parser" not in (extra or ""):
        extra = (f"{extra} " if extra else "") + f"--reasoning-parser {rp}"
    lines = [f"VLLM_MODEL={model}",
             f"VLLM_EXTRA_ARGS={serve_args(model, aliases, extra)}"]
    if not has_gpu and int(threads or 0) > 0:
        lines.append(f"OMP_NUM_THREADS={int(threads)}")
    return "\n".join(lines) + "\n"


def switch_script(backend: str, *, vllm_service: str, env_path: str, env_text: str,
                  public_port: int, vllm_port: int, serve_path: str,
                  wait_s: int = 900) -> str:
    """Shell run on the node (as root) that moves it to `backend` and proves the
    new service answers. Prints DONE on success."""
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}")
    tap = f"vera-ollama-tap@{int(public_port)}"
    has_tap = f"systemctl cat {tap} >/dev/null 2>&1"
    tries = max(1, int(wait_s) // 5)
    if backend == "vllm":
        return "\n".join([
            "set -e",
            f"test -x {shlex.quote(serve_path)} || {{ echo 'vLLM is not installed in this slot on the node'; exit 3; }}",
            f"printf '%s' {shlex.quote(env_text)} > {shlex.quote(env_path)}.new",
            f"mv {shlex.quote(env_path)}.new {shlex.quote(env_path)}",
            f"U={ollama_unit_expr(public_port)}",
            "mkdir -p /var/lib/vera",
            f"echo \"$U\" > {UNIT_STATE.format(port=int(public_port))}",
            "echo \"stopping $U\"",
            f"if {has_tap}; then systemctl disable --now {tap} || true; fi",
            "systemctl disable --now $U || true",
            # vLLM not coming up must not leave the node serving nothing (5 Oct
            # 2026: a failed GPU switch left the V100 idle until restored by hand)
            "rollback() {",
            f"  systemctl disable --now {vllm_service} 2>/dev/null || true",
            "  systemctl enable $U; systemctl restart $U",
            f"  if {has_tap}; then systemctl enable {tap}; systemctl restart {tap}; fi",
            f"  rm -f {UNIT_STATE.format(port=int(public_port))}",
            "  echo \"rolled back: $U serves the node again\"",
            "}",
            "systemctl daemon-reload",
            f"systemctl enable {vllm_service}",
            f"systemctl restart {vllm_service}",
            f"for i in $(seq 1 {tries}); do",
            f"  systemctl is-active --quiet {vllm_service} || {{ echo 'vllm exited'; "
            f"journalctl -u {vllm_service} -n 30 --no-pager; rollback; exit 4; }}",
            f"  curl -sf -m 5 http://127.0.0.1:{int(vllm_port)}/v1/models >/dev/null && "
            f"{{ echo \"{DONE} backend=vllm after $((i*5))s\"; exit 0; }}",
            "  sleep 5",
            "done",
            f"echo 'vllm did not answer within {int(wait_s)}s'; rollback; exit 5",
        ])
    return "\n".join([
        "set -e",
        f"systemctl disable --now {vllm_service} 2>/dev/null || true",
        f"U={ollama_unit_expr(public_port)}",
        "echo \"starting $U\"",
        "systemctl enable $U",
        "systemctl restart $U",
        f"rm -f {UNIT_STATE.format(port=int(public_port))}",
        f"if {has_tap}; then systemctl enable {tap}; systemctl restart {tap}; fi",
        f"for i in $(seq 1 {tries}); do",
        f"  curl -sf -m 5 http://127.0.0.1:{int(public_port)}/api/tags >/dev/null && "
        f"{{ echo \"{DONE} backend=ollama after $((i*5))s\"; exit 0; }}",
        "  sleep 5",
        "done",
        f"echo 'ollama did not answer on :{int(public_port)} within {int(wait_s)}s'; exit 5",
    ])


def same_container_switched(ollama_id: str, ollama: Dict[str, Dict[str, Any]],
                            switched: Iterable[str]) -> List[str]:
    """Other instances on the same host as `ollama_id` that already serve vLLM
    from the SAME slot (each slot has one vLLM service)."""
    host = _host((ollama or {}).get(ollama_id) or {})
    slot = slot_for(ollama_id, ollama)
    return sorted(i for i in switched
                  if i != ollama_id and host and _host((ollama or {}).get(i) or {}) == host
                  and slot_for(i, ollama) == slot)


def plan(backend: str, *, ollama_id: str, inst: Dict[str, Any], vllm_present: bool,
         switched: bool, in_flight: int, force: bool,
         sharing: Iterable[str] = (), component: str = "vllm_server") -> Dict[str, Any]:
    """What a switch would do, or why it refuses. `sharing`: other instances on
    the same container already on vLLM (same_container_switched) - a container
    has one vLLM service (port 8000), so a second instance cannot move over
    until the first moves back."""
    if backend not in BACKENDS:
        return {"action": "refuse", "why": f"backend must be one of {BACKENDS}"}
    if backend == "vllm" and not switched and list(sharing):
        return {"action": "refuse",
                "why": f"this container's vLLM service already serves {', '.join(sharing)} - "
                       "one vLLM per container; switch that back first"}
    if backend == "vllm" and switched:
        return {"action": "noop", "why": f"{ollama_id} already serves vLLM"}
    if backend == "ollama" and not switched:
        return {"action": "noop", "why": f"{ollama_id} already serves Ollama"}
    if backend == "vllm" and not vllm_present:
        return {"action": "refuse", "component": component,
                "why": f"{component} is not installed on the node - provision.deploy "
                       f"component={component} install_deps=true first"}
    if in_flight > 0 and not force:
        return {"action": "refuse", "why": f"{in_flight} request(s) in flight on {ollama_id}; "
                                            "wait, or force=true"}
    return {"action": "switch", "to": backend, "vllm_instance": vllm_instance_id(ollama_id),
            "has_gpu": bool((inst or {}).get("has_gpu")), "component": component}
