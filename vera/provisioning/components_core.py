"""Pure, app-free helpers for provision.components — unit-testable without booting Vera.

Consumers import uppercase (Vera.vera.provisioning.components_core); tests import
lowercase (vera.provisioning.components_core) so pytest binds to the worktree copy.

These encode the hard-won fixes for provisioning a NATIVE Vera worker on a fresh host
(proven live: the naive layout failed three different ways before a worker registered)."""
from __future__ import annotations

import base64
import shlex
from typing import Dict, Optional

# Hosts that mean "this same box" — a REMOTE worker cannot reach the orchestrator's own
# localhost, so any backend URL/host pointing here must be re-pointed at a LAN address.
_LOCAL_HOSTS = ("host.docker.internal", "localhost", "127.0.0.1", "::1")


def rewrite_host(value: str, backend_host: str) -> str:
    """Re-point localhost-ish backend hosts at a LAN-reachable address. Prod runs the
    stores on its own box (REDIS_URL=redis://localhost:6379 etc.), so those URLs are
    useless to a worker on another node until localhost is swapped for the real IP."""
    if not value or not backend_host:
        return value
    out = value
    for h in _LOCAL_HOSTS:
        out = out.replace(h, backend_host)
    return out


#: Backend settings a remote worker must share with the host.
WORKER_BACKEND_KEYS = ("POSTGRES_URL", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASS",
                       "CHROMA_HOST", "CHROMA_PORT", "OLLAMA_BASE_URL", "OLLAMA_GPU_URL",
                       "OLLAMA_CPU_A_URL", "OLLAMA_CPU_B_URL", "OLLAMA_EMBED_URL",
                       "OLLAMA_MODEL", "VERA_COORD_REDIS_DB", "VERA_CPU_NODE_THREADS")


def worker_backend_env(env: Dict[str, str], cfg: Dict[str, object], backend_host: str,
                       keys=WORKER_BACKEND_KEYS) -> Dict[str, str]:
    """The backend settings to hand a remote worker: the host's environment
    first, else the host's EFFECTIVE config value, re-pointed at `backend_host`.

    Environment alone is not enough. Prod sets none of POSTGRES_URL, NEO4J_URI
    or CHROMA_HOST - it runs on config.py's localhost defaults - so a worker
    given only the environment got nothing, fell back to the same defaults on
    ITS machine, and every store refused it (cpu-246, 2026-09-27: Postgres,
    Chroma and Neo4j all "connection refused" on the node's own localhost)."""
    out: Dict[str, str] = {}
    for k in keys:
        v = (env or {}).get(k)
        if v in (None, ""):
            v = (cfg or {}).get(k)
        if v in (None, ""):
            continue
        out[k] = rewrite_host(str(v), backend_host)
    return out


def native_worker_cmd(root: str, repo: str, redis_url: str, backend_kv: Dict[str, str],
                      port: int = 8990, use_systemd: bool = True, *,
                      bundle: bool = False, extra_env: Optional[Dict[str, str]] = None,
                      nice: int = 0, cpu_weight: int = 0) -> str:
    """Build the shell command that clones Vera, builds a venv, and launches a native
    worker joined to the shared stack. Fixes what the naive launch got wrong:

      1. LAYOUT — the repo's package lives under a ``vera/`` subdir, so expose it as
         ``Vera/vera`` (symlink) or ``python -m Vera.vera.capability_orchestration`` fails.
      2. CWD — run from a NEUTRAL cwd (/tmp), never the package dir, or the repo's
         ``vera/operator`` package shadows Python's stdlib ``operator`` and the
         interpreter can't even finish importing ``collections`` at startup.
      3. DURABILITY — install a systemd unit (survives reboots + restarts on failure),
         falling back to nohup on non-systemd hosts. Runs unbuffered (``-u``).

    ``bundle=True`` takes the code from STDIN instead of git: a base64 tar.gz of
    the host's own checked-out commit (see components_capabilities), so the
    worker runs exactly the code the host runs and the node needs no git
    credentials. It unpacks beside the live tree and swaps it in, so a file the
    new commit deleted does not linger; the venv lives outside the tree and
    survives the swap. A re-run restarts the unit, which is how a node picks up
    a new commit.

    ``extra_env`` lands in the unit verbatim (the worker role, thread caps,
    HOME). ``nice``/``cpu_weight`` (when non-zero) lower the worker's claim on
    the CPU below the services it shares the node with.

    ``backend_kv`` must already be host-rewritten (see :func:`rewrite_host`). Pure →
    unit-testable."""
    src = f"{root}/src"
    app = f"{root}/app"
    venv = f"{root}/venv" if bundle else f"{src}/venv"
    envs = [("PYTHONPATH", app), ("REDIS_URL", redis_url),
            ("ORCHESTRATOR_HOST", "0.0.0.0"), ("ORCHESTRATOR_PORT", str(int(port or 8990))),
            ("EMBED_CAPS_ON_START", "0"), ("SYSLOG_MONITOR", "0")]
    envs += [(k, v) for k, v in (backend_kv or {}).items() if v]
    envs += [(k, str(v)) for k, v in (extra_env or {}).items() if str(v)]

    unit = ("[Unit]\nDescription=Vera native worker\n"
            "After=network-online.target\nWants=network-online.target\n"
            "[Service]\nType=simple\nWorkingDirectory=/tmp\n"
            + "".join(f'Environment="{k}={v}"\n' for k, v in envs)
            + (f"Nice={int(nice)}\n" if nice else "")
            + (f"CPUWeight={int(cpu_weight)}\n" if cpu_weight else "")
            + f"ExecStart={venv}/bin/python -u -m Vera.vera.capability_orchestration\n"
            + "Restart=on-failure\nRestartSec=5\n"
            "[Install]\nWantedBy=multi-user.target\n")
    unit_b64 = base64.b64encode(unit.encode()).decode()
    envstr = " ".join(f"{k}={shlex.quote(v)}" for k, v in envs)

    if bundle:
        fetch = (
            f"rm -rf {src}.new; mkdir -p {src}.new; "
            f"base64 -d | tar -xzf - -C {src}.new; "
            f"rm -rf {src}; mv {src}.new {src}; "
        )
    else:
        fetch = (
            f"( [ -d {src}/.git ] && git -C {src} pull --ff-only || "
            f"git clone --depth 1 {shlex.quote(repo)} {src} ); "
        )
    setup = (
        f"set -e; mkdir -p {root}; "
        + fetch +
        # expose the repo's vera/ package as Vera/vera for a clean `-m` import
        f"mkdir -p {app}/Vera; ln -sfn {src}/vera {app}/Vera/vera; : > {app}/Vera/__init__.py; "
        f"[ -f {src}/vera/__init__.py ] || : > {src}/vera/__init__.py; "
        f"[ -x {venv}/bin/python ] || python3 -m venv {venv} --system-site-packages; "
        f'"{venv}/bin/pip" install -q -U pip wheel; '
        f'"{venv}/bin/pip" install -q -r {src}/requirements.txt; '
    )
    launch_systemd = (
        f"printf %s {shlex.quote(unit_b64)} | base64 -d > /etc/systemd/system/vera-worker.service; "
        # restart, not `enable --now`: on a re-provision the unit is already
        # running the old commit, and --now would leave it there
        f"systemctl daemon-reload; systemctl enable vera-worker >/dev/null 2>&1; "
        f"systemctl restart vera-worker; sleep 3; "
        f"systemctl is-active vera-worker >/dev/null 2>&1 && echo VERA_LAUNCHED"
    )
    launch_nohup = (
        # neutral cwd so vera/operator never shadows stdlib operator
        f"cd /tmp; {envstr} nohup {venv}/bin/python -u -m Vera.vera.capability_orchestration "
        f"> {root}/worker.log 2>&1 & echo $! > {root}/worker.pid; sleep 3; "
        f'kill -0 "$(cat {root}/worker.pid)" 2>/dev/null && echo VERA_LAUNCHED'
    )
    if use_systemd:
        launch = (f"if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then "
                  f"{launch_systemd}; else {launch_nohup}; fi")
    else:
        launch = launch_nohup
    return setup + launch


# ── Where a component may live on the target ─────────────────────────────────
# ⚠ `$HOME` is NOT reliably writable. On an unprivileged LXC container, `/root`
# can be owned by a uid outside the container's mapped range: it shows as
# `nobody:root` mode 0700 and even root INSIDE the container cannot traverse it.
# Measured 2026-09-20 across the three ollama nodes — gpu-250 `nobody:root`
# (mkdir fails), cpu-246 `root:100000` (works), cpu-247 `nobody:root` (fails) —
# so two of the three could not receive ANY python component. It is the same
# reason `ollama-vera.service` on those nodes sets `HOME=/`.
EDGE_DIR_CANDIDATES = ("$HOME/.vera/edge", "/opt/vera/edge", "/var/lib/vera/edge")

#: Where a native worker lives. A system service, so /opt first; `$HOME` last,
#: for the same unwritable-/root reason as above (the old fixed
#: `$HOME/.vera/worker` could not install on gpu-250 or cpu-247 at all).
WORKER_DIR_CANDIDATES = ("/opt/vera/worker", "/var/lib/vera/worker", "$HOME/.vera/worker")

#: Marker the probe prints so the caller can parse a definite answer rather than
#: guessing from an exit code.
EDGE_DIR_MARKER = "VERA_EDGE_DIR="


def edge_dir_probe_cmd(candidates=None) -> str:
    """Shell that prints `VERA_EDGE_DIR=<first writable candidate>`.

    Probing beats assuming: without it the failure surfaces as a venv creation
    error several steps later that names a path but never the reason.
    """
    cands = tuple(candidates or EDGE_DIR_CANDIDATES)
    parts = ['D=""']
    parts += [f'[ -z "$D" ] && mkdir -p {c} 2>/dev/null && [ -w {c} ] && D={c}'
              for c in cands]
    parts.append(f'[ -n "$D" ] && echo "{EDGE_DIR_MARKER}$D" || echo VERA_EDGE_DIR_NONE')
    return "; ".join(parts)


def parse_edge_dir(stdout: str) -> str:
    """The directory the probe chose, or "" when none was writable."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith(EDGE_DIR_MARKER):
            return line[len(EDGE_DIR_MARKER):].strip()
    return ""


def pidfile_lookup_cmd(component: str, candidates=None) -> str:
    """Shell that locates `<component>.pid` in whichever edge dir was used.

    status/stop must search the SAME candidates as the deploy probe. If they
    only looked in `$HOME`, a component deployed to the fallback directory would
    report "stopped" while still running — the worst possible answer to give a
    stop command.
    """
    dirs = " ".join(tuple(candidates or EDGE_DIR_CANDIDATES))
    return (f'PIDF=""; for d in {dirs}; do '
            f'[ -f "$d/{component}.pid" ] && PIDF="$d/{component}.pid" && break; done; '
            f'PID=$(cat "$PIDF" 2>/dev/null); ')
