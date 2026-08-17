"""Pure, app-free helpers for provision.components — unit-testable without booting Vera.

Consumers import uppercase (Vera.vera.provisioning.components_core); tests import
lowercase (vera.provisioning.components_core) so pytest binds to the worktree copy.

These encode the hard-won fixes for provisioning a NATIVE Vera worker on a fresh host
(proven live: the naive layout failed three different ways before a worker registered)."""
from __future__ import annotations

import base64
import shlex
from typing import Dict

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


def native_worker_cmd(root: str, repo: str, redis_url: str, backend_kv: Dict[str, str],
                      port: int = 8990, use_systemd: bool = True) -> str:
    """Build the shell command that clones Vera, builds a venv, and launches a native
    worker joined to the shared stack. Fixes what the naive launch got wrong:

      1. LAYOUT — the repo's package lives under a ``vera/`` subdir, so expose it as
         ``Vera/vera`` (symlink) or ``python -m Vera.vera.capability_orchestration`` fails.
      2. CWD — run from a NEUTRAL cwd (/tmp), never the package dir, or the repo's
         ``vera/operator`` package shadows Python's stdlib ``operator`` and the
         interpreter can't even finish importing ``collections`` at startup.
      3. DURABILITY — install a systemd unit (survives reboots + restarts on failure),
         falling back to nohup on non-systemd hosts. Runs unbuffered (``-u``).

    ``backend_kv`` must already be host-rewritten (see :func:`rewrite_host`). Pure →
    unit-testable."""
    src = f"{root}/src"
    app = f"{root}/app"
    venv = f"{src}/venv"
    envs = [("PYTHONPATH", app), ("REDIS_URL", redis_url),
            ("ORCHESTRATOR_HOST", "0.0.0.0"), ("ORCHESTRATOR_PORT", str(int(port or 8990))),
            ("EMBED_CAPS_ON_START", "0"), ("SYSLOG_MONITOR", "0")]
    envs += [(k, v) for k, v in (backend_kv or {}).items() if v]

    unit = ("[Unit]\nDescription=Vera native worker\n"
            "After=network-online.target\nWants=network-online.target\n"
            "[Service]\nType=simple\nWorkingDirectory=/tmp\n"
            + "".join(f'Environment="{k}={v}"\n' for k, v in envs)
            + f"ExecStart={venv}/bin/python -u -m Vera.vera.capability_orchestration\n"
            + "Restart=on-failure\nRestartSec=5\n"
            "[Install]\nWantedBy=multi-user.target\n")
    unit_b64 = base64.b64encode(unit.encode()).decode()
    envstr = " ".join(f"{k}={shlex.quote(v)}" for k, v in envs)

    setup = (
        f"set -e; mkdir -p {src}; "
        f"( [ -d {src}/.git ] && git -C {src} pull --ff-only || "
        f"git clone --depth 1 {shlex.quote(repo)} {src} ); "
        # expose the repo's vera/ package as Vera/vera for a clean `-m` import
        f"mkdir -p {app}/Vera; ln -sfn {src}/vera {app}/Vera/vera; : > {app}/Vera/__init__.py; "
        f"[ -f {src}/vera/__init__.py ] || : > {src}/vera/__init__.py; "
        f"python3 -m venv {venv} --system-site-packages; "
        f'"{venv}/bin/pip" install -q -U pip wheel; '
        f'"{venv}/bin/pip" install -q -r {src}/requirements.txt; '
    )
    launch_systemd = (
        f"printf %s {shlex.quote(unit_b64)} | base64 -d > /etc/systemd/system/vera-worker.service; "
        f"systemctl daemon-reload; systemctl enable --now vera-worker; sleep 3; "
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
