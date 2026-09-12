"""pxstore_attach_core.py -- pure logic for attaching the central store to a CT.

Two facts that cost real time on 2026-09-12 and are encoded here so the
capability cannot regress into either:

1. Proxmox refuses bind-mount `mpN` entries from an API token. A host path in
   `mpN` is root@pam-only -- the same restriction Foundry documents for
   privileged CTs and the `features` flag -- so the token-authenticated
   `PUT /nodes/<node>/lxc/<vmid>/config` ALWAYS answers HTTP 403. The attach has
   to fall back to `pct set` on the node shell, which runs as root.

2. The default mount path must not be under /root. /root is 0:100000 mode 700
   on these containers; in an UNPRIVILEGED one host uid 0 is outside the id map,
   shows as `nobody`, and container root cannot traverse it, so ollama exits at
   startup with "mkdir /root/.ollama: permission denied". /.ollama/models works in
   privileged and unprivileged containers alike, and matches the canonical
   edge/ollama-vera.service.

Pure: no I/O, no app imports. Tests import it lowercase as
`vera.proxmox.pxstore_attach_core` -- see worktree-testable-cores-pattern.
"""
from __future__ import annotations

import shlex

#: Where the store appears inside a CT. Matches OLLAMA_MODELS in the unit.
DEFAULT_CT_PATH = "/.ollama/models"


def mp_value(host_path: str, ct_path: str = DEFAULT_CT_PATH, ro: bool = True) -> str:
    """The value half of an `mpN` config entry."""
    host_path = str(host_path).rstrip("/")
    if not host_path.startswith("/"):
        raise ValueError(f"host_path must be absolute, got {host_path!r}")
    if not str(ct_path).startswith("/"):
        raise ValueError(f"ct_path must be absolute, got {ct_path!r}")
    return f"{host_path},mp={ct_path}" + (",ro=1" if ro else "")


def is_token_bindmount_refusal(err) -> bool:
    """True when a PVE API error is the token-can't-bind-mount refusal.

    `_pve` reports failures as "HTTP <code>: <body>". Only a 403 means "the API
    will never do this for a token"; anything else is a real failure that a
    shell fallback would merely hide.
    """
    return str(err or "").strip().startswith("HTTP 403")


def pct_set_command(vmid, mp_key: str, value: str) -> str:
    """`pct set <vmid> -<mpN> <value>`, safe to hand to a root shell.

    `vmid` must be an integer and `mp_key` must look like mpN; `value` is
    single-quoted so a crafted path cannot end the argument and start a command.
    """
    try:
        vid = int(vmid)
    except (TypeError, ValueError):
        raise ValueError(f"vmid must be an integer, got {vmid!r}")
    if vid <= 0:
        raise ValueError(f"vmid must be positive, got {vid}")
    key = str(mp_key)
    if not (key.startswith("mp") and key[2:].isdigit()):
        raise ValueError(f"mp_key must look like mpN, got {mp_key!r}")
    return f"pct set {vid} -{key} {shlex.quote(value)}"
