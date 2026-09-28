"""
ollama_node_core.py — pure rules for provisioning an Ollama node (no app imports)

Three defects, each confirmed against the live estate on 2026-09-11:

  * **The Proxmox/LXC recipe left Ollama on loopback.** It installed Ollama and
    started the STOCK unit, which carries no OLLAMA_HOST. Measured on CT130 the
    unit is `ExecStart=/usr/local/bin/ollama serve` with only PATH in its
    Environment, and the daemon listens on `127.0.0.1:11434`. Vera then
    registered `http://<addr>:11434` — an address nothing outside the container
    can reach. Every fleet node still carries that stray loopback daemon beside
    the hand-made unit doing the real work on :11435, which is what the recipe
    leaves behind. The recipe also ignored the requested port entirely.

  * **The SSH path dropped the port.** It called provision.install without
    `port`, so Ollama installed on the default 11434 while the node was
    registered on whatever port was asked for.

  * **Registration never looked at the URL.** add_ollama_instance stores whatever
    id it is given, so re-provisioning an already-registered server adds a SECOND
    id for the same Ollama. The GPU gate counts capacity per instance id, so one
    card would be booked twice at once.

Pure, so the rules are unit-tested without an estate to provision against. The
shell text uses __PORT__ substitution rather than str.format because the script
contains shell braces.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

try:
    from Vera.vera.node_threads_core import DEFAULT_CPU_THREADS
except Exception:                                    # worktree / app-free import
    from vera.node_threads_core import DEFAULT_CPU_THREADS

DEFAULT_OLLAMA_PORT = 11434
DONE_MARKER = "VERA_PROVISION_DONE"
# Ollama's own installer writes /etc/systemd/system/ollama.service; a drop-in is
# used so an upgrade that rewrites the unit does not silently take the bind
# address with it.
DROPIN_PATH = "/etc/systemd/system/ollama.service.d/10-vera-host.conf"

_DROPIN = ('[Service]\n'
           '# Written by Vera provisioning. Without this the stock unit binds\n'
           '# 127.0.0.1 and the node is unreachable from Vera.\n'
           'Environment="OLLAMA_HOST=0.0.0.0:__PORT__"\n')

# Idempotent: safe to re-run on a node that is already provisioned. Ends by
# PROVING the node answers on the port, so a recipe that "succeeded" while
# leaving Ollama unreachable cannot report success. One recipe serves both
# provisioning paths — `pct exec` runs as root (sudo=""), an SSH login may not.
_CT_INSTALL = (
    'command -v ollama >/dev/null 2>&1 || '
    '(command -v curl >/dev/null 2>&1 || '
    '(__SUDO__apt-get -qq update && __SUDO__apt-get -qq -y install curl); '
    'curl -fsSL https://ollama.com/install.sh | __SUDO__sh); '
    '__SUDO__mkdir -p /etc/systemd/system/ollama.service.d && '
    "printf '%s' '__DROPIN__' | __SUDO__tee __DROPIN_PATH__ >/dev/null && "
    '__SUDO__systemctl daemon-reload && __SUDO__systemctl enable ollama >/dev/null 2>&1; '
    '__SUDO__systemctl restart ollama && sleep 2 && '
    '__SUDO__systemctl is-active ollama >/dev/null && '
    'curl -fsS -m 5 http://127.0.0.1:__PORT__/api/tags >/dev/null && '
    'echo __DONE__'
)


def ollama_host_dropin(port: int = DEFAULT_OLLAMA_PORT) -> str:
    """The systemd drop-in that makes Ollama listen on every interface."""
    return _DROPIN.replace("__PORT__", str(int(port)))


def ct_install_script(port: int = DEFAULT_OLLAMA_PORT, sudo: str = "") -> str:
    """Install (or confirm) Ollama and bind it to `port` on all interfaces, then
    verify it answers there. `sudo` is '' when the login is already root (as it
    is under `pct exec`), otherwise 'sudo '."""
    p = str(int(port))
    dropin = ollama_host_dropin(p).replace("'", "'\\''")
    return (_CT_INSTALL
            .replace("__DROPIN_PATH__", DROPIN_PATH)
            .replace("__DROPIN__", dropin)
            .replace("__PORT__", p)
            .replace("__SUDO__", sudo or "")
            .replace("__DONE__", DONE_MARKER))


def normalise_url(url: Any) -> str:
    """Compare Ollama URLs the way a node would answer them: lowercase scheme and
    host, explicit port, no trailing slash or path."""
    s = str(url or "").strip().rstrip("/")
    if not s:
        return ""
    scheme, _, rest = s.partition("://")
    if not rest:
        scheme, rest = "http", s
    scheme = scheme.lower()
    rest = rest.split("/", 1)[0]
    host, _, port = rest.partition(":")
    if not port:
        port = "443" if scheme == "https" else str(DEFAULT_OLLAMA_PORT)
    return "%s://%s:%s" % (scheme, host.lower(), port)


def find_instance_by_url(instances: Dict[str, dict], url: Any) -> Optional[str]:
    """The id already registered for this Ollama server, or None."""
    want = normalise_url(url)
    if not want:
        return None
    for iid, inst in (instances or {}).items():
        if normalise_url((inst or {}).get("url")) == want:
            return iid
    return None


def registration_plan(instances: Dict[str, dict], addr: str, port: int,
                      has_gpu: bool = False,
                      preferred_id: str = "") -> Dict[str, Any]:
    """Whether to reuse the id already registered for this server, or create one.

    Reuse matters: the GPU gate's capacity is per instance id, so a second id for
    the same Ollama lets two callers each believe they hold the node's only slot."""
    url = normalise_url("http://%s:%d" % (addr, int(port)))
    existing = find_instance_by_url(instances, url)
    if existing:
        return {"action": "reuse", "instance_id": existing, "url": url,
                "reason": "this Ollama is already registered as %r; a second id "
                          "would let the gate book the node twice" % existing}
    iid = preferred_id or ("node-%s-%d" % (str(addr).replace(".", "-"), int(port)))
    return {"action": "create", "instance_id": iid, "url": url,
            "has_gpu": bool(has_gpu), "reason": "no registered node answers on this URL"}


def registration_threads(has_gpu: bool, requested: Any = None) -> int:
    """`num_thread` to record for a node at registration, or 0 for none.

    Ollama 0.32 has no server-side thread setting (`ollama serve --help` lists
    none), so the count can only travel on each request - and every request
    path reads it from the node's registry entry (node_threads_core). A node
    registered without it falls back to one process-wide env default, which is
    how three nodes with different core layouts ended up sharing one number.

    A GPU node gets none (its runner barely uses CPU threads). A CPU node gets
    what was asked for, else the measured default: six, because cpu-246 and
    cpu-247 are the two hyperthreads of the SAME twelve physical cores
    (CPUs 0-11 and 24-35 on socket 0), so six each is six real cores each."""
    if has_gpu:
        return 0
    try:
        n = int(requested or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else DEFAULT_CPU_THREADS


# ── concurrency on CPU Ollama servers ─────────────────────────────────────────
# Measured 2026-09-28 (cpu-246 / gpu-250's CPU, 6 threads): with the default one
# slot, a second request to a CPU server WAITS for the first (2x0.5b: 4.6 s then
# 8.9 s wall). Two slots run them together: 0.5b 44.8 -> 2x30.3 tok/s (+35%
# total), 7b 6.16 -> 2x3.7 (+20%). Each request is slower, the node does more.
# The embedding model is a different runner, so it runs beside a generation as
# long as both stay loaded - MAX_LOADED_MODELS 3 keeps a big LLM, the embedder
# and one more resident (the CPU nodes have 50 GB).
CPU_NUM_PARALLEL = 2
CPU_MAX_LOADED_MODELS = 3
CONCURRENCY_DROPIN_NAME = "20-vera-concurrency.conf"

#: The CPU-only sibling Ollama on a GPU node (user, 2026-09-28): the GPU node's
#: cores serve embeddings and small models beside the GPU runner. Measured: CPU
#: embeds (6.1-6.6 docs/s) and a 0.5b (42-47 tok/s) on gpu-250's CPU did not
#: move the GPU's 9b throughput beyond run-to-run noise.
CPU_SIBLING_PORT = 11436
CPU_SIBLING_UNIT = "ollama-vera-cpu.service"


def concurrency_dropin(num_parallel: int = CPU_NUM_PARALLEL,
                       max_loaded: int = CPU_MAX_LOADED_MODELS) -> str:
    return ("[Service]\n"
            "# Written by Vera (nodes.ollama.tune): two requests at once on a CPU\n"
            "# server, and room for the embedder beside a large model.\n"
            f'Environment="OLLAMA_NUM_PARALLEL={int(num_parallel)}"\n'
            f'Environment="OLLAMA_MAX_LOADED_MODELS={int(max_loaded)}"\n')


def cpu_sibling_id(gpu_instance_id: str) -> str:
    return f"{gpu_instance_id}-cpu"


def cpu_sibling_unit(models_dir: str, port: int = CPU_SIBLING_PORT) -> str:
    """A CPU-only Ollama beside a GPU node's own. The GPU is hidden from it
    (and the CPU library forced), it reads the SAME model store read-only
    (NOPRUNE), and it has the CPU concurrency settings."""
    return ("[Unit]\n"
            f"Description=Ollama CPU-only sibling (Vera, port {int(port)}) - embeddings and small models\n"
            "After=network-online.target ollama-vera.service\nWants=network-online.target\n\n"
            "[Service]\nExecStart=/usr/local/bin/ollama serve\nUser=root\nGroup=root\n"
            'Environment="HOME=/"\n'
            f'Environment="OLLAMA_MODELS={models_dir}"\n'
            f'Environment="OLLAMA_HOST=0.0.0.0:{int(port)}"\n'
            'Environment="OLLAMA_NOPRUNE=1"\n'
            'Environment="CUDA_VISIBLE_DEVICES="\n'
            'Environment="HIP_VISIBLE_DEVICES="\n'
            'Environment="OLLAMA_LLM_LIBRARY=cpu"\n'
            f'Environment="OLLAMA_NUM_PARALLEL={CPU_NUM_PARALLEL}"\n'
            f'Environment="OLLAMA_MAX_LOADED_MODELS={CPU_MAX_LOADED_MODELS}"\n'
            'Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
            "Restart=always\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n")


def tune_probe_cmd() -> str:
    """Shell that reports a node's Ollama unit, its model dir, its current
    concurrency drop-in and whether a CPU sibling runs. Read-only."""
    return ('U=ollama; systemctl is-active --quiet ollama-vera && U=ollama-vera; echo "UNIT=$U"; '
            'echo "MODELS=$(systemctl show $U -p Environment --value | tr " " "\\n" '
            '| sed -n "s/^OLLAMA_MODELS=//p")"; '
            'echo "USER=$(systemctl show $U -p User --value)"; '
            f'D=/etc/systemd/system/$U.service.d/{CONCURRENCY_DROPIN_NAME}; '
            '[ -f $D ] && echo "DROPIN_B64=$(base64 -w0 $D)" || echo "DROPIN_B64="; '
            f'systemctl is-active --quiet {CPU_SIBLING_UNIT} && echo SIBLING=active || echo SIBLING=absent')


def parse_tune_probe(stdout: str) -> Dict[str, str]:
    import base64 as _b64
    out: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        k, sep, v = line.partition("=")
        if sep and k in ("UNIT", "MODELS", "DROPIN_B64", "SIBLING", "USER"):
            out[k] = v.strip()
    try:
        out["dropin"] = _b64.b64decode(out.get("DROPIN_B64") or "").decode("utf-8", "replace")
    except Exception:
        out["dropin"] = ""
    return out


def default_models_dir(user: str) -> str:
    """Where a unit with no OLLAMA_MODELS keeps its models: Ollama's installer
    runs the stock unit as `ollama` (home /usr/share/ollama); root's is /root."""
    u = str(user or "").strip()
    return "/usr/share/ollama/.ollama/models" if u == "ollama" else (
        "/root/.ollama/models" if u in ("", "root") else f"/home/{u}/.ollama/models")


def tune_plan(has_gpu: bool, probe: Dict[str, str]) -> Dict[str, Any]:
    """What tuning a node takes. A CPU node needs the concurrency drop-in on
    its Ollama unit (a restart of that unit - loaded models reload). A GPU node
    keeps ONE slot on its GPU server (a V100 cannot hold two big models) and
    gets the CPU-only sibling instead."""
    unit = probe.get("UNIT") or "ollama"
    if has_gpu:
        if probe.get("SIBLING") == "active":
            return {"action": "none", "why": "CPU sibling already running", "unit": unit}
        models = probe.get("MODELS") or ""
        if not models and "USER" in probe:
            # a fresh install: the stock unit keeps the installer's default
            models = default_models_dir(probe.get("USER") or "")
        if not models:
            return {"action": "skip", "why": f"could not read OLLAMA_MODELS from {unit}",
                    "unit": unit}
        return {"action": "add_sibling", "unit": unit, "models": models,
                "why": f"add the CPU-only sibling on :{CPU_SIBLING_PORT}"}
    want = concurrency_dropin()
    if (probe.get("dropin") or "") == want:
        return {"action": "none", "why": "concurrency already set", "unit": unit}
    return {"action": "set_concurrency", "unit": unit,
            "why": f"{unit}: OLLAMA_NUM_PARALLEL={CPU_NUM_PARALLEL}, "
                   f"MAX_LOADED_MODELS={CPU_MAX_LOADED_MODELS} (restarts {unit})"}


def install_succeeded(stdout: Any) -> bool:
    """The recipe only succeeds when it reached its end marker — which it only
    prints after the node answered on the port."""
    return DONE_MARKER in str(stdout or "")
