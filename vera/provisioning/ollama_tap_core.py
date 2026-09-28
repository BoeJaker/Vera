"""ollama_tap_core.py - putting the activity tap in front of a node's Ollama.

The tap (edge/ollama_tap.py) takes the node's PUBLIC Ollama port; Ollama moves
to loopback on port + UPSTREAM_OFFSET. Callers see no change of address. Pure:
unit text, env text and the cutover script (with its own rollback) are built
here and unit-tested; nodes.ollama.tap runs them over SSH.
"""
from __future__ import annotations

import shlex
from typing import Dict

TAP_DIR = "/opt/vera/tap"
TAP_ENV_DIR = "/etc/vera-tap"
TAP_SECRET_ENV = "/etc/vera-tap/redis.env"
TAP_LOG_DIR = "/var/log/vera-tap"
TAP_UNIT = "vera-ollama-tap@.service"
TAP_DROPIN = "30-vera-tap.conf"
UPSTREAM_OFFSET = 10
DONE = "VERA_TAP_READY"
ROLLED_BACK = "VERA_TAP_ROLLED_BACK"


def upstream_port(public_port: int) -> int:
    return int(public_port) + UPSTREAM_OFFSET


def tap_unit() -> str:
    """One tap per public port: vera-ollama-tap@11435, @11436 ..."""
    return ("[Unit]\n"
            "Description=Vera activity tap in front of Ollama on :%i\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\n"
            f"EnvironmentFile=-{TAP_SECRET_ENV}\n"
            f"EnvironmentFile={TAP_ENV_DIR}/%i.env\n"
            f"ExecStart={TAP_DIR}/venv/bin/python {TAP_DIR}/ollama_tap.py\n"
            "Restart=always\nRestartSec=2\n"
            # never starve the inference it fronts
            "Nice=5\n\n[Install]\nWantedBy=multi-user.target\n")


def instance_env(node: str, public_port: int) -> str:
    p = int(public_port)
    return (f"VERA_TAP_NODE={node}\n"
            f"VERA_TAP_LISTEN=0.0.0.0:{p}\n"
            f"VERA_TAP_UPSTREAM=http://127.0.0.1:{upstream_port(p)}\n"
            f"VERA_TAP_LOG_DIR={TAP_LOG_DIR}\n")


def ollama_dropin(public_port: int) -> str:
    return ("[Service]\n# Written by Vera (nodes.ollama.tap): Ollama on loopback, the\n"
            f"# activity tap on the public :{int(public_port)}.\n"
            f'Environment="OLLAMA_HOST=127.0.0.1:{upstream_port(public_port)}"\n')


def install_cmd() -> str:
    """Install/refresh the tap and its venv (idempotent). The tap's source
    arrives on STDIN (it is too big for a command line). Carries no secret."""
    return " && ".join([
        f"mkdir -p {TAP_DIR} {TAP_ENV_DIR} {TAP_LOG_DIR}",
        f"base64 -d > {TAP_DIR}/ollama_tap.py",
        f"( [ -x {TAP_DIR}/venv/bin/python ] || python3 -m venv {TAP_DIR}/venv )",
        f"{TAP_DIR}/venv/bin/pip install -q 'aiohttp>=3.9' 'redis>=5' >/dev/null",
        f"printf '%s' {shlex.quote(tap_unit())} > /etc/systemd/system/{TAP_UNIT}",
        "systemctl daemon-reload",
        "echo VERA_TAP_INSTALLED",
    ])


def secret_cmd() -> str:
    """Write the Redis env (arriving on STDIN) to a 0600 file. The credential
    never appears in a command line, so it never reaches a recorded argument."""
    return (f"mkdir -p {TAP_ENV_DIR} && umask 077 && cat > {TAP_SECRET_ENV} && "
            f"chmod 600 {TAP_SECRET_ENV} && echo VERA_TAP_SECRET")


def cutover_cmd(unit: str, node: str, public_port: int) -> str:
    """Move `unit`'s Ollama to loopback and start the tap on the public port.
    Checks both answer; on any failure puts Ollama back on its old address and
    stops the tap, so a failed cutover never leaves the node unreachable."""
    p, up = int(public_port), upstream_port(public_port)
    d = f"/etc/systemd/system/{unit}.service.d"
    inst = f"vera-ollama-tap@{p}"
    # Only the final check prints DONE; cutover_script rolls back without it.
    return (f"printf '%s' {shlex.quote(instance_env(node, p))} > {TAP_ENV_DIR}/{p}.env && "
            f"mkdir -p {d} && printf '%s' {shlex.quote(ollama_dropin(p))} > {d}/{TAP_DROPIN} && "
            f"systemctl daemon-reload && systemctl restart {unit} && "
            f"for i in $(seq 1 30); do curl -fsS -m 3 http://127.0.0.1:{up}/api/tags >/dev/null "
            f"&& break; sleep 1; done && "
            f"systemctl enable --now {inst} && systemctl restart {inst} && "
            f"for i in $(seq 1 30); do curl -fsS -m 3 http://127.0.0.1:{p}/api/tags >/dev/null "
            f"&& curl -fsS -m 3 http://127.0.0.1:{p}/vera-tap/health >/dev/null && "
            f"echo {DONE} && break; sleep 1; done")


def cutover_script(unit: str, node: str, public_port: int) -> str:
    """cutover_cmd, then roll back unless it printed DONE."""
    inner = cutover_cmd(unit, node, public_port)
    p = int(public_port)
    d = f"/etc/systemd/system/{unit}.service.d"
    inst = f"vera-ollama-tap@{p}"
    return (f"OUT=$( {inner} 2>&1 ); echo \"$OUT\"; "
            f"case \"$OUT\" in *{DONE}*) ;; *) "
            f"rm -f {d}/{TAP_DROPIN}; systemctl stop {inst}; systemctl disable {inst} 2>/dev/null; "
            f"systemctl daemon-reload; systemctl restart {unit}; echo {ROLLED_BACK};; esac")


def status_cmd(public_port: int) -> str:
    p = int(public_port)
    return (f"systemctl is-active --quiet vera-ollama-tap@{p} && echo TAP=active || echo TAP=absent; "
            f"curl -fsS -m 3 http://127.0.0.1:{p}/vera-tap/health 2>/dev/null | head -c 400; echo")


def parse_status(stdout: str) -> Dict[str, object]:
    active = "TAP=active" in (stdout or "")
    import json
    health = {}
    for line in (stdout or "").splitlines():
        if line.startswith("{"):
            try:
                health = json.loads(line)
            except ValueError:
                pass
    return {"active": active, "health": health}
