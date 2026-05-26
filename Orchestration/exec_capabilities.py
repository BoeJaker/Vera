"""
exec_capabilities.py  —  Vera Shell Execution + Network Discovery module
=========================================================================

Two capability groups in one module:

1. exec.*   — Shell execution
   ──────────────────────────
   • exec.bash.run          — run a bash command locally (sync, captured)
   • exec.ps.run            — run a PowerShell command locally (pwsh / powershell)
   • exec.ssh.run           — run a command on a remote host via SSH (password or key)
   • exec.ssh.hosts.list    — list stored SSH host credentials
   • exec.ssh.hosts.save    — save (or replace) an SSH host credential
   • exec.ssh.hosts.delete  — remove an SSH host credential
   • exec.ssh.probe         — quick connectivity probe for a host (tcp-ping :22)

   HTTP stream endpoints (not @capability — need raw SSE):
     POST /exec/bash/stream     — stream stdout/stderr of a local bash command
     POST /exec/ps/stream       — stream stdout/stderr of a local pwsh command
     POST /exec/ssh/stream      — stream stdout/stderr of an SSH command

2. netscan.*  — Network asset discovery + auxiliary graph
   ──────────────────────────────────────────────────────
   • netscan.lan.scan        — ARP + TCP port sweep of a CIDR, persisted to the aux graph
   • netscan.docker.scan     — `docker ps` on a host (local or SSH) → aux graph
   • netscan.proxmox.scan    — Proxmox PVE API → nodes + guests (qemu/lxc) → aux graph
   • netscan.k8s.scan        — kubectl get nodes/pods → aux graph
   • netscan.graph           — fetch the aux graph for the UI (cytoscape format)
   • netscan.node.get        — fetch one node + its edges
   • netscan.nodes.clear     — wipe discovered nodes (by source)

   Auxiliary graph node labels (stored under FABRIC_NEO, separate from memory graph):
     :NetHost           — any reachable IP on the LAN (router, server, laptop…)
     :DockerHost        — a machine running Docker
     :Container         — a Docker container
     :PVENode           — a Proxmox cluster node
     :PVEGuest          — a VM or LXC container on a PVE node
     :K8sCluster        — a Kubernetes cluster
     :K8sNode           — a Kubernetes node
     :K8sPod            — a Kubernetes pod

   Edges:
     :ON_NETWORK    (NetHost)-[:ON_NETWORK]->(Subnet?)      (implicit — via .subnet prop)
     :HOSTS         (DockerHost)-[:HOSTS]->(Container)
     :IN_CLUSTER    (PVENode)-[:IN_CLUSTER]->(PVECluster)
     :RUNS          (PVENode)-[:RUNS]->(PVEGuest)
     :IN_CLUSTER    (K8sNode)-[:IN_CLUSTER]->(K8sCluster)
     :SCHEDULED_ON  (K8sPod)-[:SCHEDULED_ON]->(K8sNode)
     :SAME_IP       (NetHost)-[:SAME_IP]->(PVENode|DockerHost|K8sNode)  — cross-source link

UI panels
──────────
• exec-panel     — Tabbed Bash / PowerShell / SSH consoles (mode="tab", icon ">_")
• netmap-panel   — Interactive Cytoscape.js graph of discovered assets (mode="tab", icon "⬢")
                   Right-click on a node → "SSH here" → jumps to exec-panel with host filled.

Requirements
────────────
  pip install asyncssh httpx
  System tools optionally used (called via bash):
    • arp, ping (LAN scan)
    • docker / docker.exe (Docker scan — local or over SSH)
    • kubectl (K8s scan — local or over SSH)
  Proxmox uses the HTTP API — no shell tools required.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, StreamingResponse

from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
    record_stream_activity,
    register_ui,
    schedule,
)

log = logging.getLogger("vera.exec")

# Coerce timeout into an int, accepting formats like 10s, 60m, 1h, etc.
def parse_timeout(t: Any) -> int:
    if isinstance(t, (int, float)):
        return int(t)
    s = str(t).strip().lower()
    if not s:
        return _DEFAULT_TIMEOUT
    # Try to extract a number and unit
    import re
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd])?$", s)
    if not m:
        return _DEFAULT_TIMEOUT  # fallback on unknown format
    num, unit = int(m.group(1)), m.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(num * multipliers.get(unit, 1))


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONAL DEPS
# ─────────────────────────────────────────────────────────────────────────────
try:
    import asyncssh  # type: ignore
    HAS_ASYNCSSH = True
except Exception:
    asyncssh = None  # type: ignore
    HAS_ASYNCSSH = False
    log.warning("asyncssh not installed — SSH capabilities will return errors. "
                "Install with: pip install asyncssh")


# ─────────────────────────────────────────────────────────────────────────────
# FABRIC NEO4J — pull the driver lazily so we don't have an import-order issue
# ─────────────────────────────────────────────────────────────────────────────
def _fabric_neo():
    """Return the FABRIC_NEO instance from data_fabric if loaded, else None."""
    mod = sys.modules.get("data_fabric")
    if not mod:
        return None
    return getattr(mod, "FABRIC_NEO", None)


async def _aux_run(cypher: str, **params) -> List[Dict]:
    """Execute a Cypher write on the auxiliary graph. Returns [] on failure."""
    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return []
    try:
        async with fn._driver.session() as s:
            result = await s.run(cypher, **params)
            return await result.data()
    except Exception as e:
        log.debug("aux_graph write failed: %s", e)
        return []


async def _aux_read(cypher: str, **params) -> List[Dict]:
    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return []
    try:
        async with fn._driver.session() as s:
            result = await s.run(cypher, **params)
            return await result.data()
    except Exception as e:
        log.debug("aux_graph read failed: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SSH HOST STORE — Neo4j aux graph as primary, JSON file as fallback/cache
# ─────────────────────────────────────────────────────────────────────────────
#
# Hosts live as :SshHost nodes in the Fabric Neo4j database. Each node has:
#   id, label, host, port, user, auth, key_path, tags (list), created_at,
#   updated_at, password_obf (xor+b64), passphrase_obf
#
# If Neo4j is unavailable at the moment a read/write happens we transparently
# fall back to a JSON file on disk so scans and SSH still work. Writes during
# degraded mode are flushed to Neo4j on the next healthy read.
# ─────────────────────────────────────────────────────────────────────────────
_SSH_STORE_PATH = Path(os.getenv(
    "VERA_SSH_STORE",
    os.path.join(os.path.expanduser("~"), ".vera_ssh_hosts.json"),
))

# Obfuscation key — NOT strong encryption. The store file should have 0600 perms.
# For real secrets, use an env var or a proper secret manager.
_OBF = "vera-exec-host-store-v1"


def _obfuscate(s: str) -> str:
    if not s:
        return ""
    import base64
    b = s.encode("utf-8")
    k = _OBF.encode("utf-8")
    out = bytes(c ^ k[i % len(k)] for i, c in enumerate(b))
    return base64.b64encode(out).decode("ascii")


def _deobfuscate(s: str) -> str:
    if not s:
        return ""
    import base64
    try:
        raw = base64.b64decode(s.encode("ascii"))
        k = _OBF.encode("utf-8")
        return bytes(c ^ k[i % len(k)] for i, c in enumerate(raw)).decode("utf-8")
    except Exception as _obf_err:
        log.warning(
            "_deobfuscate: stored credential is corrupt or was saved with a "
            "different key — returning empty string. Re-save the host "
            "credential to fix this. (%s)", _obf_err
        )
        return ""


def _load_hosts_file() -> Dict[str, dict]:
    if not _SSH_STORE_PATH.exists():
        return {}
    try:
        raw = json.loads(_SSH_STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        log.warning("SSH host store (file) corrupt: %s", e)
    return {}


def _save_hosts_file(hosts: Dict[str, dict]) -> None:
    try:
        _SSH_STORE_PATH.write_text(
            json.dumps(hosts, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(_SSH_STORE_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        log.error("Failed to persist SSH host store file: %s", e)


def _neo_available() -> bool:
    fn = _fabric_neo()
    return bool(fn and getattr(fn, "available", False))


async def _load_hosts() -> Dict[str, dict]:
    """Primary: Neo4j. Fallback: JSON file. Also cross-syncs the two."""
    if _neo_available():
        try:
            fn = _fabric_neo()
            async with fn._driver.session() as s:
                res = await s.run("MATCH (h:SshHost) RETURN h")
                out: Dict[str, dict] = {}
                async for row in res:
                    rec = dict(row["h"])
                    # tags comes back as a list of strings or None
                    if "tags" in rec and not isinstance(rec["tags"], list):
                        rec["tags"] = []
                    out[rec["id"]] = rec
            # Cache to file so we still work if Neo4j goes down later
            if out:
                try: _save_hosts_file(out)
                except Exception: pass
            else:
                # Neo4j empty — lift whatever's in the JSON cache into Neo4j
                file_hosts = _load_hosts_file()
                if file_hosts:
                    for rec in file_hosts.values():
                        try: await _persist_host_neo(rec)
                        except Exception: pass
                    return file_hosts
            return out
        except Exception as e:
            log.warning("SshHost Neo4j read failed, falling back to file: %s", e)
    return _load_hosts_file()


async def _persist_host_neo(rec: dict) -> None:
    fn = _fabric_neo()
    if not (fn and getattr(fn, "available", False)):
        return
    # Split into primitive scalars + list props so we can use SET h += $props
    props = {k: v for k, v in rec.items()
             if isinstance(v, (str, int, float, bool)) or v is None}
    tags = rec.get("tags") or []
    async with fn._driver.session() as s:
        await s.run(
            """
            MERGE (h:SshHost {id:$id})
            SET h += $props
            SET h.tags = $tags
            """,
            id=rec["id"], props=props, tags=list(tags),
        )


async def _save_hosts(hosts: Dict[str, dict]) -> None:
    """Persist to Neo4j (primary) + file (cache)."""
    # file first (fast, always works)
    _save_hosts_file(hosts)
    if _neo_available():
        for rec in hosts.values():
            try: await _persist_host_neo(rec)
            except Exception as e:
                log.warning("SshHost persist(%s) failed: %s", rec.get("id"), e)


async def _delete_host(host_id: str) -> None:
    hosts = await _load_hosts()
    hosts.pop(host_id, None)
    _save_hosts_file(hosts)
    if _neo_available():
        try:
            fn = _fabric_neo()
            async with fn._driver.session() as s:
                await s.run("MATCH (h:SshHost {id:$id}) DETACH DELETE h", id=host_id)
        except Exception as e:
            log.warning("SshHost delete failed for %s: %s", host_id, e)


def _public_host_record(h: dict) -> dict:
    """Return the host dict without secrets for API output."""
    return {
        "id":       h.get("id"),
        "label":    h.get("label"),
        "host":     h.get("host"),
        "port":     h.get("port", 22),
        "user":     h.get("user"),
        "auth":     h.get("auth", "password"),
        "key_path": h.get("key_path", ""),
        "tags":     h.get("tags", []),
        "has_password": bool(h.get("password_obf")),
        "has_passphrase": bool(h.get("passphrase_obf")),
        "created_at": h.get("created_at"),
        "updated_at": h.get("updated_at"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL EXEC
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_TIMEOUT = 60          # seconds
_MAX_OUTPUT     = 1_000_000    # 1 MB captured output per stream


async def _run_local(argv: List[str], stdin_data: str = "",
                     timeout: int = _DEFAULT_TIMEOUT,
                     cwd: Optional[str] = None,
                     env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, **(env or {})} if env else None,
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": f"executable not found: {e}",
                "rc": -1, "stdout": "", "stderr": str(e),
                "elapsed_ms": 0}

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(stdin_data.encode("utf-8") if stdin_data else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": f"timeout after {timeout}s",
                "rc": -1, "stdout": "", "stderr": "",
                "elapsed_ms": round((time.monotonic() - t0) * 1000)}

    so = stdout_b.decode("utf-8", errors="replace")[:_MAX_OUTPUT]
    se = stderr_b.decode("utf-8", errors="replace")[:_MAX_OUTPUT]
    return {
        "ok":         proc.returncode == 0,
        "rc":         proc.returncode,
        "stdout":     so,
        "stderr":     se,
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
    }


@capability(
    "exec.bash.run",
    http_method="POST", http_path="/exec/bash/run", http_tags=["exec"],
    description="Run a bash command locally (captured). "
                "Input: command (str!), timeout (int sec), cwd (str). "
                "Output: {ok, rc, stdout, stderr, elapsed_ms}. "
                "Use /exec/bash/stream for live output.",
)
async def cap_bash_run(command: str, timeout: int = _DEFAULT_TIMEOUT,
                       cwd: str = "", trace_id=None) -> Dict:
    timeout = parse_timeout(timeout)
    if not command.strip():
        return {"ok": False, "error": "empty command", "rc": -1,
                "stdout": "", "stderr": ""}
    # Use bash -lc so aliases, PATH, env are loaded
    bash_bin = os.getenv("VERA_BASH_BIN", "/bin/bash")
    argv = [bash_bin, "-lc", command]
    return await _run_local(argv, timeout=timeout, cwd=cwd or None)


@capability(
    "exec.ps.run",
    http_method="POST", http_path="/exec/ps/run", http_tags=["exec"],
    description="Run a PowerShell command locally (captured). "
                "Uses 'pwsh' if available, falls back to 'powershell'. "
                "Input: command (str!), timeout (int sec), cwd (str). "
                "Output: {ok, rc, stdout, stderr, elapsed_ms}.",
)
async def cap_ps_run(command: str, timeout: int = _DEFAULT_TIMEOUT,
                     cwd: str = "", trace_id=None) -> Dict:
    timeout = parse_timeout(timeout)
    if not command.strip():
        return {"ok": False, "error": "empty command", "rc": -1,
                "stdout": "", "stderr": ""}
    ps_bin = os.getenv("VERA_PS_BIN", "")
    if not ps_bin:
        # Probe pwsh (cross-platform) first, then powershell (Windows)
        for cand in ("pwsh", "powershell"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    cand, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.Major",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=4)
                if proc.returncode == 0:
                    ps_bin = cand
                    break
            except Exception:
                continue
    if not ps_bin:
        return {"ok": False, "error": "pwsh/powershell not found on PATH",
                "rc": -1, "stdout": "", "stderr": ""}
    argv = [ps_bin, "-NoProfile", "-NonInteractive", "-Command", command]
    return await _run_local(argv, timeout=timeout, cwd=cwd or None)


# ─────────────────────────────────────────────────────────────────────────────
# SSH EXEC
# ─────────────────────────────────────────────────────────────────────────────
async def _ssh_connect_kwargs(
    host: str, *, port: int = 22, user: str = "",
    password: str = "", key_path: str = "", passphrase: str = "",
    known_hosts: Any = None,
) -> Dict[str, Any]:
    """Build kwargs for asyncssh.connect().

    Special-character passwords (containing $, !, #, @, %, backslash, etc.)
    are passed as plain Python strings to asyncssh — they are never shell-
    interpolated.  To prevent asyncssh from trying agent/key auth before
    password auth (which can cause PermissionDenied before the password is
    even attempted), we explicitly set preferred_auth and disable agent +
    cert lookups when a password is supplied.
    """
    kw: Dict[str, Any] = {
        "host":     host,
        "port":     int(port or 22),
        "username": user or os.getenv("USER", "root"),
        "known_hosts": known_hosts,  # None = disable host-key checking
    }
    if key_path:
        kw["client_keys"] = [os.path.expanduser(key_path)]
        if passphrase:
            kw["passphrase"] = passphrase
        if password:
            # key + password: try key first, fall back to password
            kw["password"] = password
            kw["preferred_auth"] = "publickey,password,keyboard-interactive"
    elif password:
        # Password-only: skip agent / key discovery entirely so special chars
        # in the password are not shadowed by a prior PermissionDenied from
        # a failed key attempt.
        kw["password"] = password
        kw["preferred_auth"] = "password,keyboard-interactive"
        kw["client_keys"]    = []      # don't auto-discover ~/.ssh/id_*; also suppresses cert probing
        kw["agent_path"]     = None    # don't use SSH agent
    return kw


async def _ssh_run_on(
    host: str, command: str, *,
    port: int = 22, user: str = "",
    password: str = "", key_path: str = "", passphrase: str = "",
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    if not HAS_ASYNCSSH:
        return {"ok": False, "error": "asyncssh not installed",
                "rc": -1, "stdout": "", "stderr": ""}
    t0 = time.monotonic()
    try:
        kw = await _ssh_connect_kwargs(
            host, port=port, user=user,
            password=password, key_path=key_path, passphrase=passphrase,
        )
        async with asyncssh.connect(**kw) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False), timeout=timeout)
            so = (result.stdout or "")[:_MAX_OUTPUT] if isinstance(result.stdout, str) \
                else (result.stdout.decode("utf-8", "replace")[:_MAX_OUTPUT] if result.stdout else "")
            se = (result.stderr or "")[:_MAX_OUTPUT] if isinstance(result.stderr, str) \
                else (result.stderr.decode("utf-8", "replace")[:_MAX_OUTPUT] if result.stderr else "")
            return {
                "ok":         (result.exit_status == 0),
                "rc":         result.exit_status or 0,
                "stdout":     so,
                "stderr":     se,
                "elapsed_ms": round((time.monotonic() - t0) * 1000),
                "host":       host,
            }
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timeout after {timeout}s",
                "rc": -1, "stdout": "", "stderr": "", "host": host,
                "elapsed_ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "rc": -1, "stdout": "", "stderr": "", "host": host,
                "elapsed_ms": round((time.monotonic() - t0) * 1000)}


async def _resolve_host_record(host_id: str) -> Optional[dict]:
    hosts = await _load_hosts()
    return hosts.get(host_id)


@capability(
    "exec.ssh.run",
    http_method="POST", http_path="/exec/ssh/run", http_tags=["exec"],
    description="Run a command on a remote host via SSH. "
                "Pass either host_id (a stored credential) OR a full inline set: "
                "host, port, user, password OR key_path, passphrase. "
                "Input: command (str!), host_id (str), host (str), port (int), "
                "user (str), password (str), key_path (str), passphrase (str), timeout (int). "
                "Output: {ok, rc, stdout, stderr, elapsed_ms, host}.",
)
async def cap_ssh_run(
    command:    str,
    host_id:    str = "",
    host:       str = "",
    port:       int = 22,
    user:       str = "",
    password:   str = "",
    key_path:   str = "",
    passphrase: str = "",
    timeout:    int = _DEFAULT_TIMEOUT,
    trace_id=None,
) -> Dict:
    # Resolve from store if host_id given
    if host_id:
        rec = await _resolve_host_record(host_id)
        if not rec:
            return {"ok": False, "error": f"host_id not found: {host_id}",
                    "rc": -1, "stdout": "", "stderr": ""}
        host       = rec.get("host", "")
        port       = int(rec.get("port", 22) or 22)
        user       = rec.get("user", "")
        key_path   = rec.get("key_path", "") or ""
        if rec.get("auth", "password") == "password":
            password   = _deobfuscate(rec.get("password_obf", ""))
            passphrase = ""
        else:
            password   = ""
            passphrase = _deobfuscate(rec.get("passphrase_obf", ""))
    if not host:
        return {"ok": False, "error": "no host provided",
                "rc": -1, "stdout": "", "stderr": ""}
    if not command.strip():
        return {"ok": False, "error": "empty command",
                "rc": -1, "stdout": "", "stderr": ""}
    return await _ssh_run_on(
        host, command,
        port=port, user=user,
        password=password, key_path=key_path, passphrase=passphrase,
        timeout=timeout,
    )


@capability(
    "exec.ssh.hosts.list",
    http_method="GET", http_path="/exec/ssh/hosts", http_tags=["exec"],
    memory="off", silent=True,
    description="List all stored SSH host credentials (secrets redacted).",
)
async def cap_ssh_hosts_list(trace_id=None) -> Dict:
    hosts = await _load_hosts()
    return {"hosts": [_public_host_record(h) for h in hosts.values()],
            "count": len(hosts)}


@capability(
    "exec.ssh.hosts.save",
    http_method="POST", http_path="/exec/ssh/hosts/save", http_tags=["exec"],
    description="Save (or replace) an SSH host credential. Stored primarily in Neo4j "
                "as :SshHost nodes, cached to ~/.vera_ssh_hosts.json. "
                "Input: host (str!), user (str!), port (int=22), label (str), "
                "auth ('password'|'key'), password (str), key_path (str), "
                "passphrase (str), tags (comma-sep), id (str — update if given). "
                "Output: {ok, host: {...}}.",
)
async def cap_ssh_hosts_save(
    host:       str = "",
    user:       str = "",
    port:       int = 22,
    label:      str = "",
    auth:       str = "password",
    password:   str = "",
    key_path:   str = "",
    passphrase: str = "",
    tags:       str = "",
    id:         str = "",
    trace_id=None,
) -> Dict:
    # Inline validation (schema-level defaults keep this cap from 500ing)
    if not host:
        return {"ok": False, "error": "host required"}
    if not user:
        return {"ok": False, "error": "user required"}
    if auth == "key" and not key_path:
        return {"ok": False, "error": "key_path required when auth='key'"}

    hosts = await _load_hosts()
    hid = id or str(uuid.uuid4())
    existing = hosts.get(hid, {})
    # tags may arrive as a list (JSON) or a CSV string
    if isinstance(tags, list):
        tags_list = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tags_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    rec = {
        "id":       hid,
        "label":    label or f"{user}@{host}",
        "host":     host,
        "port":     int(port or 22),
        "user":     user,
        "auth":     "key" if (auth == "key" or key_path) else "password",
        "key_path": key_path or existing.get("key_path", ""),
        "tags":     tags_list or existing.get("tags", []),
        "created_at": existing.get("created_at", now_iso()),
        "updated_at": now_iso(),
    }
    # Only overwrite secrets if provided (so edits don't wipe them)
    if password:
        rec["password_obf"] = _obfuscate(password)
    elif "password_obf" in existing and rec["auth"] == "password":
        rec["password_obf"] = existing["password_obf"]
    if passphrase:
        rec["passphrase_obf"] = _obfuscate(passphrase)
    elif "passphrase_obf" in existing and rec["auth"] == "key":
        rec["passphrase_obf"] = existing["passphrase_obf"]

    hosts[hid] = rec
    await _save_hosts(hosts)
    await emit_event({"type": "ssh.host.saved", "id": hid, "host": host, "user": user})
    return {"ok": True, "host": _public_host_record(rec),
            "storage": "neo4j" if _neo_available() else "file"}


@capability(
    "exec.ssh.hosts.delete",
    http_method="POST", http_path="/exec/ssh/hosts/delete", http_tags=["exec"],
    description="Delete a stored SSH host credential by id. Input: id (str!).",
)
async def cap_ssh_hosts_delete(id: str = "", trace_id=None) -> Dict:
    if not id:
        return {"ok": False, "error": "id required"}
    hosts = await _load_hosts()
    if id not in hosts:
        return {"ok": False, "error": f"host_id not found: {id}"}
    rec = hosts[id]
    await _delete_host(id)
    await emit_event({"type": "ssh.host.deleted", "id": id})
    return {"ok": True, "deleted": _public_host_record(rec)}


@capability(
    "exec.ssh.probe",
    http_method="POST", http_path="/exec/ssh/probe", http_tags=["exec"],
    memory="off",
    description="Quick TCP connectivity probe to an SSH endpoint. "
                "Input: host (str!), port (int=22), timeout (float=3). "
                "Output: {ok, latency_ms, banner}.",
)
async def cap_ssh_probe(host: str, port: int = 22, timeout: float = 3.0,
                        trace_id=None) -> Dict:
    timeout = parse_timeout(timeout)
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), timeout=timeout)
        # SSH banner is sent by the server first
        try:
            banner_b = await asyncio.wait_for(reader.readline(), timeout=1.5)
            banner = banner_b.decode("utf-8", "replace").strip()
        except Exception:
            banner = ""
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"ok": True,
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "banner": banner,
                "host": host, "port": int(port)}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "host": host, "port": int(port)}


# ─────────────────────────────────────────────────────────────────────────────
# STREAMING HTTP ENDPOINTS (not @capability — SSE)
# ─────────────────────────────────────────────────────────────────────────────
def _sse(event: str, data: Any) -> bytes:
    """Format an SSE event."""
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


async def _stream_subprocess(argv: List[str], cwd: Optional[str] = None,
                              timeout: int = 300):
    """Async generator yielding SSE bytes for a subprocess."""
    t0 = time.monotonic()
    yield _sse("start", {"argv": argv, "ts": now_iso()})
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except Exception as e:
        yield _sse("error", {"error": f"spawn failed: {e}"})
        yield _sse("done", {"rc": -1})
        return

    async def _pump(stream, kind: str):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            yield _sse(kind, {"text": text})

    async def _drain_to_queue(stream, kind: str, queue: asyncio.Queue):
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                await queue.put((kind, text))
        except Exception:
            pass
        finally:
            await queue.put((kind + ":eof", ""))

    q: asyncio.Queue = asyncio.Queue()
    t_out = asyncio.create_task(_drain_to_queue(proc.stdout, "stdout", q))
    t_err = asyncio.create_task(_drain_to_queue(proc.stderr, "stderr", q))

    eofs = 0
    try:
        while eofs < 2:
            try:
                kind, text = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Heartbeat so the client doesn't time out idle
                yield _sse("heartbeat", {"elapsed_ms":
                           round((time.monotonic() - t0) * 1000)})
                if (time.monotonic() - t0) > timeout:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    yield _sse("error", {"error": f"timeout after {timeout}s"})
                    break
                continue
            if kind.endswith(":eof"):
                eofs += 1
                continue
            yield _sse(kind, {"text": text})
        rc = await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    finally:
        for t in (t_out, t_err):
            if not t.done():
                t.cancel()
    yield _sse("done", {"rc": rc, "elapsed_ms":
               round((time.monotonic() - t0) * 1000)})


async def _stream_subprocess_recorded(
    argv:        List[str],
    cap_name:    str,
    session_id:  str,
    params:      dict,
    cwd:         Optional[str] = None,
    timeout:     int = 300,
):
    """
    Wraps `_stream_subprocess` and records the call into the activity chain
    when the stream completes. Counts stdout/stderr lines and captures the
    final return code so the recorded result is informative.

    Behaves identically to `_stream_subprocess` from the SSE consumer's point
    of view — same events, same ordering, same tail "done".
    """
    t0 = time.monotonic()
    stdout_n  = 0
    stderr_n  = 0
    rc        = -1
    head_lines: List[str] = []
    error_msg = ""
    try:
        async for chunk in _stream_subprocess(argv, cwd=cwd, timeout=timeout):
            # Cheap line counting from the SSE bytes — we only need the type
            # tag to bump the right counter.
            if b'"stdout"' in chunk[:32]:
                stdout_n += 1
                if len(head_lines) < 8:
                    # Best-effort first-few-lines capture for the recorded
                    # output preview. Decoding errors are non-fatal.
                    try:
                        head_lines.append(chunk.decode("utf-8", "ignore")
                                          [:300])
                    except Exception:
                        pass
            elif b'"stderr"' in chunk[:32]:
                stderr_n += 1
            elif b'"error"' in chunk[:32]:
                # Best-effort capture of the error string
                try:
                    s = chunk.decode("utf-8", "ignore")
                    if '"error":"' in s:
                        error_msg = s.split('"error":"', 1)[1].split('"', 1)[0]
                except Exception:
                    pass
            elif b'"done"' in chunk[:32]:
                # Pull rc out of the done event
                try:
                    s = chunk.decode("utf-8", "ignore")
                    if '"rc":' in s:
                        rc_str = s.split('"rc":', 1)[1].split(",", 1)[0].split("}", 1)[0].strip()
                        rc = int(rc_str)
                except Exception:
                    pass
            yield chunk
    finally:
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        result = {
            "argv":         argv,
            "rc":           rc,
            "stdout_lines": stdout_n,
            "stderr_lines": stderr_n,
            "elapsed_ms":   elapsed_ms,
            "head":         "".join(head_lines)[:1000],
        }
        if error_msg:
            result["error"] = error_msg
        try:
            await record_stream_activity(
                cap_name=cap_name, session_id=session_id,
                params=params, result=result, elapsed_ms=elapsed_ms,
            )
        except Exception as _e:
            log.debug("record_stream_activity failed for %s: %s", cap_name, _e)


@APP.post("/exec/bash/stream")
async def exec_bash_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    command    = body.get("command", "")
    cwd        = body.get("cwd", "") or None
    timeout    = int(body.get("timeout", 300))
    session_id = body.get("session_id", "") or ""

    # Set the syslog trigger context so the activity recorder (which falls
    # back to get_trigger_chain when an explicit session_id isn't passed)
    # picks up the right session, and so any internal cap calls show
    # "exec.bash.stream" as their trigger_cap.
    if session_id:
        _syslog = sys.modules.get("syslog")
        if _syslog:
            try:
                _syslog.set_trigger(str(uuid.uuid4()), "exec.bash.stream", session_id)
            except Exception:
                pass

    if not command.strip():
        async def _err():
            yield _sse("error", {"error": "empty command"})
            yield _sse("done", {"rc": -1})
        return StreamingResponse(_err(), media_type="text/event-stream")
    bash_bin = os.getenv("VERA_BASH_BIN", "/bin/bash")
    argv = [bash_bin, "-lc", command]
    return StreamingResponse(
        _stream_subprocess_recorded(
            argv,
            cap_name="exec.bash.stream", session_id=session_id,
            params={"command": command, "cwd": cwd, "timeout": timeout},
            cwd=cwd, timeout=timeout,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@APP.post("/exec/ps/stream")
async def exec_ps_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    command    = body.get("command", "")
    cwd        = body.get("cwd", "") or None
    timeout    = int(body.get("timeout", 300))
    session_id = body.get("session_id", "") or ""

    if session_id:
        _syslog = sys.modules.get("syslog")
        if _syslog:
            try:
                _syslog.set_trigger(str(uuid.uuid4()), "exec.ps.stream", session_id)
            except Exception:
                pass

    if not command.strip():
        async def _err():
            yield _sse("error", {"error": "empty command"})
            yield _sse("done", {"rc": -1})
        return StreamingResponse(_err(), media_type="text/event-stream")
    ps_bin = os.getenv("VERA_PS_BIN", "pwsh")
    argv = [ps_bin, "-NoProfile", "-NonInteractive", "-Command", command]
    return StreamingResponse(
        _stream_subprocess_recorded(
            argv,
            cap_name="exec.ps.stream", session_id=session_id,
            params={"command": command, "cwd": cwd, "timeout": timeout},
            cwd=cwd, timeout=timeout,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@APP.post("/exec/ssh/stream")
async def exec_ssh_stream(request: Request):
    if not HAS_ASYNCSSH:
        async def _err():
            yield _sse("error", {"error": "asyncssh not installed"})
            yield _sse("done", {"rc": -1})
        return StreamingResponse(_err(), media_type="text/event-stream")
    try:
        body = await request.json()
    except Exception:
        body = {}

    command = body.get("command", "")
    host_id = body.get("host_id", "") or ""
    host    = body.get("host", "") or ""
    port    = int(body.get("port", 22) or 22)
    user    = body.get("user", "") or ""
    password   = body.get("password", "") or ""
    key_path   = body.get("key_path", "") or ""
    passphrase = body.get("passphrase", "") or ""
    timeout    = int(body.get("timeout", 300))
    session_id = body.get("session_id", "") or ""

    if host_id:
        rec = await _resolve_host_record(host_id)
        if not rec:
            async def _err():
                yield _sse("error", {"error": f"host_id not found: {host_id}"})
                yield _sse("done", {"rc": -1})
            return StreamingResponse(_err(), media_type="text/event-stream")
        host = rec.get("host", ""); port = int(rec.get("port", 22) or 22)
        user = rec.get("user", "");  key_path = rec.get("key_path", "") or ""
        if rec.get("auth", "password") == "password":
            password = _deobfuscate(rec.get("password_obf", "")); passphrase = ""
        else:
            password = ""; passphrase = _deobfuscate(rec.get("passphrase_obf", ""))

    if not host or not command.strip():
        async def _err():
            yield _sse("error", {"error": "host + command required"})
            yield _sse("done", {"rc": -1})
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def gen():
        t0 = time.monotonic()
        # Counters for the recorded activity entry
        stdout_n = 0
        stderr_n = 0
        rc_int   = -1
        head_lines: List[str] = []
        error_msg = ""
        yield _sse("start", {"host": host, "user": user, "ts": now_iso()})
        try:
            kw = await _ssh_connect_kwargs(
                host, port=port, user=user,
                password=password, key_path=key_path, passphrase=passphrase,
            )
            async with asyncssh.connect(**kw) as conn:
                proc = await conn.create_process(command)
                loop = asyncio.get_event_loop()

                async def read_stream(s, kind):
                    try:
                        while True:
                            chunk = await s.read(4096)
                            if not chunk:
                                return
                            # asyncssh returns str on text channels
                            text = chunk if isinstance(chunk, str) \
                                else chunk.decode("utf-8", "replace")
                            for line in text.splitlines():
                                yield (kind, line)
                    except Exception:
                        return

                # Merge stdout and stderr
                queue: asyncio.Queue = asyncio.Queue()

                async def pump(s, kind):
                    async for pair in read_stream(s, kind):
                        await queue.put(pair)
                    await queue.put((kind + ":eof", ""))

                t_out = asyncio.create_task(pump(proc.stdout, "stdout"))
                t_err = asyncio.create_task(pump(proc.stderr, "stderr"))
                eofs = 0
                while eofs < 2:
                    try:
                        kind, text = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        yield _sse("heartbeat", {"elapsed_ms":
                            round((time.monotonic() - t0) * 1000)})
                        if (time.monotonic() - t0) > timeout:
                            try: proc.terminate()
                            except Exception: pass
                            error_msg = f"timeout after {timeout}s"
                            yield _sse("error", {"error": error_msg})
                            break
                        continue
                    if kind.endswith(":eof"):
                        eofs += 1; continue
                    if kind == "stdout":
                        stdout_n += 1
                        if len(head_lines) < 8:
                            head_lines.append(text[:200])
                    elif kind == "stderr":
                        stderr_n += 1
                    yield _sse(kind, {"text": text})
                rc = await proc.wait()
                rc_int = rc.exit_status if rc else 0
                yield _sse("done", {"rc": rc_int,
                                    "elapsed_ms":
                                    round((time.monotonic() - t0) * 1000)})
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            yield _sse("error", {"error": error_msg})
            yield _sse("done", {"rc": -1})
        finally:
            elapsed_ms = round((time.monotonic() - t0) * 1000)
            try:
                await record_stream_activity(
                    cap_name="exec.ssh.stream", session_id=session_id,
                    params={"command": command, "host": host, "user": user,
                            "port": port, "host_id": host_id, "timeout": timeout},
                    result={"rc": rc_int, "stdout_lines": stdout_n,
                            "stderr_lines": stderr_n, "elapsed_ms": elapsed_ms,
                            "head": "\n".join(head_lines)[:1000],
                            "error": error_msg or None},
                    elapsed_ms=elapsed_ms,
                )
            except Exception as _e:
                log.debug("record_stream_activity ssh: %s", _e)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — helpers for aux graph
# ─────────────────────────────────────────────────────────────────────────────
def _norm_cidr(cidr: str) -> str:
    try:
        return str(ipaddress.ip_network(cidr, strict=False))
    except Exception:
        return cidr


async def _tcp_ping(host: str, port: int, timeout: float = 0.8) -> bool:
    # Keep as float — parse_timeout truncates to int, which turns 0.8 → 0
    # and immediately kills every connection attempt.
    try:
        timeout = float(timeout) if timeout else 0.8
    except Exception:
        timeout = 0.8
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _reverse_dns(ip: str) -> str:
    loop = asyncio.get_event_loop()
    try:
        name, *_ = await loop.run_in_executor(
            None, lambda: socket.gethostbyaddr(ip))
        return name or ""
    except Exception:
        return ""


async def _mac_lookup_via_arp(ip: str) -> str:
    """Try to resolve MAC from the local ARP cache (best-effort, Linux-only)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", "neigh", "show", ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        text = out.decode("utf-8", "replace")
        m = re.search(r"lladdr ([0-9a-fA-F:]{17})", text)
        if m:
            return m.group(1).lower()
    except Exception:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            "arp", "-n", ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        text = out.decode("utf-8", "replace")
        m = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", text)
        if m:
            return m.group(1).lower()
    except Exception:
        pass
    return ""


async def _aux_upsert_nethost(ip: str, *, mac: str = "", hostname: str = "",
                               subnet: str = "", open_ports: List[int] = None,
                               source: str = "lan", extra: Optional[dict] = None) -> None:
    props = {
        "ip":         ip,
        "mac":        mac or "",
        "hostname":   hostname or "",
        "subnet":     subnet or "",
        "open_ports": open_ports or [],
        "source":     source,
        "last_seen":  now_iso(),
    }
    if extra:
        for k, v in extra.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                props[k] = v
    await _aux_run(
        """
        MERGE (n:NetHost {id: $id})
        SET n += $props, n.updated_at = $ts
        """,
        id=f"net:{ip}", props=props, ts=now_iso(),
    )
    if subnet:
        await _aux_run(
            """
            MERGE (s:Subnet {id: $sid}) SET s.cidr = $cidr, s.updated_at=$ts
            WITH s
            MATCH (n:NetHost {id: $nid})
            MERGE (n)-[:ON_NETWORK]->(s)
            """,
            sid=f"subnet:{subnet}", cidr=subnet, nid=f"net:{ip}", ts=now_iso(),
        )


async def _aux_upsert_ports(host_id: str, ip: str, open_ports: List[int]) -> None:
    """Create :NetPort nodes for each open port and link them to the host."""
    port_hints = globals().get("_PORT_HINTS", {})
    for port in open_ports:
        hint = port_hints.get(port, "")
        pid = f"port:{ip}:{port}"
        await _aux_run(
            """
            MERGE (p:NetPort {id: $pid})
            SET p.port=$port, p.ip=$ip, p.hint=$hint, p.updated_at=$ts
            WITH p
            MATCH (h:NetHost {id: $hid})
            MERGE (h)-[:EXPOSES]->(p)
            """,
            pid=pid, port=port, ip=ip, hint=hint, hid=host_id, ts=now_iso(),
        )


async def _save_scan_to_fabric(dataset_id: str, records: List[Dict]) -> None:
    """Push scan result records into a fabric dataset for Loom processing."""
    mod = sys.modules.get("data_fabric")
    if not mod:
        return
    upsert = getattr(mod, "fabric_record_upsert", None)
    if not upsert:
        return
    for rec in records:
        try:
            rid = rec.get("ip") or rec.get("url") or rec.get("id") or str(uuid.uuid4())
            text_parts = []
            for k, v in rec.items():
                if isinstance(v, str) and v:
                    text_parts.append(f"{k}: {v}")
                elif isinstance(v, list):
                    text_parts.append(f"{k}: {', '.join(str(x) for x in v)}")
                elif isinstance(v, (int, float)):
                    text_parts.append(f"{k}: {v}")
            await upsert(
                dataset_id=dataset_id,
                record_id=rid,
                text=" | ".join(text_parts),
                meta=rec,
            )
        except Exception as e:
            log.debug("fabric save record failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — LAN
# ─────────────────────────────────────────────────────────────────────────────
_COMMON_PORTS = [22, 80, 443, 3389, 5985, 8006, 6443, 2375, 2376, 9090, 3000]


@capability(
    "netscan.lan.scan",
    http_method="POST", http_path="/netscan/lan/scan", http_tags=["netscan"],
    description="Discover hosts on a LAN by TCP-pinging common ports across a CIDR. "
                "Persists :NetHost nodes (and optionally :NetPort nodes) into the aux graph. "
                "Input: cidr (str!), ports (comma-sep ints, optional), "
                "concurrency (int=64), timeout (float=0.8), "
                "port_nodes (bool=true — create :NetPort nodes per open port), "
                "save_to_fabric (bool=true — persist results to fabric dataset), "
                "fabric_dataset (str='netscan_lan' — target dataset id). "
                "Output: {cidr, alive: [{ip, hostname, mac, open_ports}], count, elapsed_ms}.",
)
async def cap_netscan_lan(
    cidr:            str,
    ports:           str   = "",
    concurrency:     int   = 64,
    timeout:         float = 0.8,
    port_nodes:      bool  = True,
    save_to_fabric:  bool  = True,
    fabric_dataset:  str   = "netscan_lan",
    trace_id=None,
) -> Dict:
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except Exception as e:
        return {"error": f"invalid cidr: {e}", "alive": [], "count": 0}
    port_list = [int(p.strip()) for p in ports.split(",")
                 if p.strip().isdigit()] if ports else _COMMON_PORTS
    subnet = str(net)
    t0 = time.monotonic()

    sem = asyncio.Semaphore(concurrency)

    async def probe_host(ip_str: str) -> Optional[dict]:
        async with sem:
            open_ports: List[int] = []
            # Probe ports in parallel per host
            results = await asyncio.gather(
                *[_tcp_ping(ip_str, p, timeout) for p in port_list],
                return_exceptions=True,
            )
            for p, ok in zip(port_list, results):
                if ok is True:
                    open_ports.append(p)
            if not open_ports:
                return None
            hostname = await _reverse_dns(ip_str)
            mac      = await _mac_lookup_via_arp(ip_str)
            rec = {
                "ip": ip_str, "hostname": hostname, "mac": mac,
                "open_ports": open_ports,
            }
            host_id = f"net:{ip_str}"
            await _aux_upsert_nethost(
                ip_str, mac=mac, hostname=hostname, subnet=subnet,
                open_ports=open_ports, source="lan")
            if port_nodes:
                await _aux_upsert_ports(host_id, ip_str, open_ports)
            return rec

    tasks = [probe_host(str(ip)) for ip in net.hosts()]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    alive = [r for r in raw if isinstance(r, dict)]

    if save_to_fabric and alive:
        asyncio.ensure_future(_save_scan_to_fabric(
            fabric_dataset,
            [{**h, "cidr": subnet, "scan_type": "lan"} for h in alive],
        ))

    await emit_event({"type": "netscan.lan.done",
                      "cidr": subnet, "count": len(alive)})
    return {
        "cidr":       subnet,
        "alive":      alive,
        "count":      len(alive),
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
        "ports":      port_list,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — DOCKER
# ─────────────────────────────────────────────────────────────────────────────
async def _docker_ps(ssh_host_id: str = "", host: str = "",
                     use_sudo: bool = False) -> Tuple[Optional[List[dict]], str]:
    """Run `docker ps --format '{{json .}}'` locally or via SSH. Returns (rows, err).
    use_sudo prefixes the command with `sudo -n` — useful when the orchestrator
    user isn't in the `docker` group."""
    prefix = "sudo -n " if use_sudo else ""
    cmd = f"{prefix}docker ps -a --format '{{{{json .}}}}'"
    if ssh_host_id or host:
        if ssh_host_id:
            r = await cap_ssh_run(command=cmd, host_id=ssh_host_id, timeout=30)
        else:
            r = await cap_ssh_run(command=cmd, host=host, timeout=30)
        if not r.get("ok"):
            return None, r.get("stderr") or r.get("error") or "ssh failed"
        out = r.get("stdout", "")
    else:
        r = await _run_local(["bash", "-lc", cmd], timeout=20)
        if not r.get("ok"):
            return None, r.get("stderr") or r.get("error") or "docker command failed"
        out = r.get("stdout", "")

    rows: List[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows, ""


@capability(
    "netscan.docker.scan",
    http_method="POST", http_path="/netscan/docker/scan", http_tags=["netscan"],
    description="Discover Docker containers on the local host or a remote SSH host. "
                "Creates :DockerHost + :Container nodes with :HOSTS edges. "
                "Input: host_id (str — stored SSH creds) OR host (str, remote hostname), "
                "label (str, optional human label), use_sudo (bool=false — prefix "
                "`sudo -n` to docker commands). Output: {host, containers: [...], count}.",
)
async def cap_netscan_docker(
    host_id:  str  = "",
    host:     str  = "",
    label:    str  = "",
    use_sudo: bool = False,
    trace_id=None,
) -> Dict:
    # Determine the DockerHost key + display host
    if host_id:
        rec = (await _resolve_host_record(host_id)) or {}
        disp_host = rec.get("host", host_id)
        disp_label = label or rec.get("label") or disp_host
    else:
        disp_host = host or socket.gethostname()
        disp_label = label or disp_host

    rows, err = await _docker_ps(ssh_host_id=host_id, host=host, use_sudo=use_sudo)
    if rows is None:
        return {"error": err, "host": disp_host, "containers": [], "count": 0}

    # Upsert DockerHost node
    docker_host_id = f"docker:{disp_host}"
    await _aux_run(
        """
        MERGE (h:DockerHost {id:$id})
        SET h.host=$host, h.label=$label, h.updated_at=$ts, h.source='docker'
        """,
        id=docker_host_id, host=disp_host, label=disp_label, ts=now_iso(),
    )

    containers = []
    for row in rows:
        cid  = row.get("ID") or row.get("Id") or ""
        name = row.get("Names") or row.get("Name") or ""
        image = row.get("Image", "")
        status = row.get("Status", "")
        ports  = row.get("Ports", "")
        state  = row.get("State", "")
        cont = {
            "id":     cid,
            "name":   name,
            "image":  image,
            "status": status,
            "state":  state,
            "ports":  ports,
        }
        containers.append(cont)
        await _aux_run(
            """
            MERGE (c:Container {id:$id})
            SET c.name=$name, c.image=$image, c.status=$status,
                c.state=$state, c.ports=$ports, c.updated_at=$ts,
                c.source='docker', c.host=$host
            WITH c
            MATCH (h:DockerHost {id:$hid})
            MERGE (h)-[:HOSTS]->(c)
            """,
            id=f"container:{disp_host}:{cid[:12]}",
            name=name, image=image, status=status, state=state,
            ports=ports, ts=now_iso(),
            hid=docker_host_id, host=disp_host,
        )
    # Cross-link to NetHost if resolvable
    try:
        ip = socket.gethostbyname(disp_host) if disp_host else ""
    except Exception:
        ip = ""
    if ip:
        await _aux_run(
            """
            MATCH (d:DockerHost {id:$did}), (n:NetHost {id:$nid})
            MERGE (n)-[:SAME_IP]->(d)
            """,
            did=docker_host_id, nid=f"net:{ip}",
        )
    await emit_event({"type": "netscan.docker.done",
                      "host": disp_host, "count": len(containers)})
    return {"host": disp_host, "label": disp_label,
            "containers": containers, "count": len(containers)}


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — PROXMOX
# ─────────────────────────────────────────────────────────────────────────────
async def _pve_api_get(base_url: str, token: str, path: str,
                        verify: bool = False) -> Tuple[Optional[Any], str]:
    url = base_url.rstrip("/") + path
    headers = {"Authorization": f"PVEAPIToken={token}"}
    try:
        async with httpx.AsyncClient(timeout=15, verify=verify) as c:
            r = await c.get(url, headers=headers)
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            return r.json().get("data"), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


async def _pve_via_ssh(host_id: str, path: str) -> Tuple[Optional[Any], str]:
    """Run `pvesh get <path> --output-format json` over SSH."""
    rec = await _resolve_host_record(host_id)
    if not rec:
        return None, f"host_id not found: {host_id}"
    pwd = _deobfuscate(rec.get("password_obf", "")) if rec.get("auth", "password") == "password" else ""
    pph = _deobfuscate(rec.get("passphrase_obf", "")) if rec.get("auth") == "key" else ""
    cmd = f"pvesh get {shlex.quote(path)} --output-format json"
    r = await _ssh_run_on(
        rec["host"], cmd,
        port=int(rec.get("port", 22) or 22), user=rec.get("user", ""),
        password=pwd, key_path=rec.get("key_path", ""), passphrase=pph,
        timeout=30,
    )
    if not r.get("ok"):
        return None, r.get("error") or r.get("stderr") or f"rc={r.get('rc')}"
    try:
        return json.loads(r.get("stdout") or "null"), ""
    except Exception as e:
        return None, f"parse: {e}; stdout[:200]={(r.get('stdout') or '')[:200]}"


@capability(
    "netscan.proxmox.scan",
    http_method="POST", http_path="/netscan/proxmox/scan", http_tags=["netscan"],
    description="Discover Proxmox VE cluster nodes + guests (qemu/lxc). "
                "Two modes: (a) API — provide api_url + token; "
                "(b) SSH — provide ssh_host_id pointing to a saved host that runs `pvesh`. "
                "Creates :PVECluster, :PVENode, :PVEGuest with :IN_CLUSTER + :RUNS edges. "
                "Input: ssh_host_id (str, optional), api_url (str, optional), "
                "token (str, optional 'USER@REALM!TOKENID=SECRET'), "
                "cluster_name (str, optional), verify_tls (bool=false). "
                "Output: {cluster, nodes: [...], guests: [...], counts}.",
)
async def cap_netscan_proxmox(
    ssh_host_id:  str  = "",
    api_url:      str  = "",
    token:        str  = "",
    cluster_name: str  = "",
    verify_tls:   bool = False,
    trace_id=None,
) -> Dict:
    use_ssh = bool(ssh_host_id)
    if not use_ssh and (not api_url or not token):
        return {"error": "Provide either ssh_host_id OR (api_url + token)",
                "nodes": [], "guests": []}

    async def _get(path: str):
        if use_ssh:
            return await _pve_via_ssh(ssh_host_id, path)
        return await _pve_api_get(api_url, token, path, verify=verify_tls)

    nodes, err = await _get("/nodes")
    if nodes is None:
        return {"error": err, "nodes": [], "guests": []}

    if use_ssh:
        rec = (await _resolve_host_record(ssh_host_id)) or {}
        src_label = f"ssh:{rec.get('host', ssh_host_id)}"
    else:
        src_label = api_url
    cluster_id = f"pve_cluster:{cluster_name or src_label}"
    cluster_disp = cluster_name or (src_label if use_ssh else
                                    api_url.split('//', 1)[-1].split('/')[0])
    await _aux_run(
        """
        MERGE (c:PVECluster {id:$id})
        SET c.name=$name, c.api_url=$url, c.updated_at=$ts, c.source='proxmox'
        """,
        id=cluster_id, name=cluster_disp, url=src_label, ts=now_iso(),
    )

    node_summ = []
    guest_summ = []
    for n in nodes or []:
        nname = n.get("node", "")
        nid   = f"pve_node:{nname}"
        node_summ.append({
            "name":   nname,
            "status": n.get("status"),
            "cpu":    n.get("cpu"),
            "mem":    n.get("mem"),
            "maxmem": n.get("maxmem"),
            "uptime": n.get("uptime"),
        })
        await _aux_run(
            """
            MERGE (n:PVENode {id:$id})
            SET n.name=$name, n.status=$status, n.cpu=$cpu, n.mem=$mem,
                n.maxmem=$maxmem, n.uptime=$uptime, n.updated_at=$ts,
                n.source='proxmox'
            WITH n
            MATCH (c:PVECluster {id:$cid})
            MERGE (n)-[:IN_CLUSTER]->(c)
            """,
            id=nid, name=nname, status=n.get("status", ""),
            cpu=n.get("cpu", 0), mem=n.get("mem", 0),
            maxmem=n.get("maxmem", 0), uptime=n.get("uptime", 0),
            ts=now_iso(), cid=cluster_id,
        )
        # Try to resolve the node's IP so we can cross-link to NetHost later
        try:
            ip = socket.gethostbyname(nname)
            if ip:
                await _aux_run(
                    """
                    MATCH (p:PVENode {id:$pid}), (h:NetHost {id:$nid})
                    MERGE (h)-[:SAME_IP]->(p)
                    """, pid=nid, nid=f"net:{ip}",
                )
        except Exception:
            pass
        # Fetch guests (qemu + lxc)
        for kind in ("qemu", "lxc"):
            data, _ = await _get(f"/nodes/{nname}/{kind}")
            for g in data or []:
                vmid = g.get("vmid", 0)
                gid  = f"pve_guest:{nname}:{vmid}"
                info = {
                    "vmid":   vmid,
                    "name":   g.get("name", ""),
                    "type":   kind,
                    "status": g.get("status", ""),
                    "node":   nname,
                    "cpu":    g.get("cpu", 0),
                    "mem":    g.get("mem", 0),
                    "maxmem": g.get("maxmem", 0),
                }
                guest_summ.append(info)
                await _aux_run(
                    """
                    MERGE (g:PVEGuest {id:$id})
                    SET g += $props, g.updated_at=$ts, g.source='proxmox'
                    WITH g
                    MATCH (n:PVENode {id:$nid})
                    MERGE (n)-[:RUNS]->(g)
                    """,
                    id=gid, props=info, ts=now_iso(), nid=nid,
                )
    await emit_event({"type": "netscan.proxmox.done",
                      "cluster": cluster_disp,
                      "nodes": len(node_summ), "guests": len(guest_summ)})
    return {
        "cluster": cluster_disp,
        "nodes":   node_summ,
        "guests":  guest_summ,
        "counts":  {"nodes": len(node_summ), "guests": len(guest_summ)},
    }


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — KUBERNETES
# ─────────────────────────────────────────────────────────────────────────────
async def _kubectl(args: List[str], ssh_host_id: str = "", host: str = "",
                    kubeconfig: str = "") -> Tuple[Optional[dict], str]:
    env_prefix = f"KUBECONFIG={shlex.quote(kubeconfig)} " if kubeconfig else ""
    cmd = env_prefix + "kubectl " + " ".join(shlex.quote(a) for a in args) + " -o json"
    if ssh_host_id or host:
        r = await cap_ssh_run(command=cmd,
                              host_id=ssh_host_id or "",
                              host=host or "",
                              timeout=30)
    else:
        r = await _run_local(["bash", "-lc", cmd], timeout=30)
    if not r.get("ok"):
        return None, r.get("stderr") or r.get("error") or "kubectl failed"
    try:
        return json.loads(r.get("stdout", "")), ""
    except Exception as e:
        return None, f"JSON parse error: {e}"


@capability(
    "netscan.k8s.scan",
    http_method="POST", http_path="/netscan/k8s/scan", http_tags=["netscan"],
    description="Discover Kubernetes nodes + pods via `kubectl` (local or SSH). "
                "Creates :K8sCluster, :K8sNode, :K8sPod with :IN_CLUSTER + :SCHEDULED_ON edges. "
                "Input: host_id (str — SSH creds) OR host (str), "
                "kubeconfig (str, path on that machine), cluster_name (str). "
                "Output: {cluster, nodes: [...], pods: [...], counts}.",
)
async def cap_netscan_k8s(
    host_id:      str = "",
    host:         str = "",
    kubeconfig:   str = "",
    cluster_name: str = "default",
    trace_id=None,
) -> Dict:
    ndata, err = await _kubectl(["get", "nodes"], ssh_host_id=host_id,
                                  host=host, kubeconfig=kubeconfig)
    if ndata is None:
        return {"error": err, "nodes": [], "pods": []}
    pdata, _ = await _kubectl(["get", "pods", "--all-namespaces"],
                               ssh_host_id=host_id, host=host,
                               kubeconfig=kubeconfig)

    cluster_id = f"k8s_cluster:{cluster_name}"
    await _aux_run(
        """
        MERGE (c:K8sCluster {id:$id})
        SET c.name=$name, c.updated_at=$ts, c.source='k8s'
        """,
        id=cluster_id, name=cluster_name, ts=now_iso(),
    )

    node_list = []
    for n in (ndata.get("items") or []):
        meta = n.get("metadata", {})
        spec = n.get("spec", {})
        stat = n.get("status", {})
        name = meta.get("name", "")
        # Find Ready condition
        cond = next((c for c in stat.get("conditions", [])
                     if c.get("type") == "Ready"), {})
        info = {
            "name":       name,
            "ready":      cond.get("status", "Unknown"),
            "version":    (stat.get("nodeInfo") or {}).get("kubeletVersion", ""),
            "os":         (stat.get("nodeInfo") or {}).get("osImage", ""),
            "arch":       (stat.get("nodeInfo") or {}).get("architecture", ""),
            "addresses":  [a.get("address", "") for a in stat.get("addresses", [])],
            "roles":      [k.split("/")[-1]
                            for k in meta.get("labels", {})
                            if k.startswith("node-role.kubernetes.io/")],
        }
        node_list.append(info)
        kid = f"k8s_node:{cluster_name}:{name}"
        await _aux_run(
            """
            MERGE (n:K8sNode {id:$id})
            SET n += $props, n.updated_at=$ts, n.source='k8s',
                n.cluster=$cluster
            WITH n
            MATCH (c:K8sCluster {id:$cid})
            MERGE (n)-[:IN_CLUSTER]->(c)
            """,
            id=kid, props=info, ts=now_iso(),
            cluster=cluster_name, cid=cluster_id,
        )
        # Cross-link to NetHost via addresses
        for addr in info["addresses"]:
            if re.match(r"\d+\.\d+\.\d+\.\d+", addr):
                await _aux_run(
                    """
                    MATCH (k:K8sNode {id:$kid}), (h:NetHost {id:$nid})
                    MERGE (h)-[:SAME_IP]->(k)
                    """, kid=kid, nid=f"net:{addr}",
                )

    pod_list = []
    for p in (pdata or {}).get("items", []) if pdata else []:
        meta = p.get("metadata", {})
        spec = p.get("spec", {})
        stat = p.get("status", {})
        name = meta.get("name", "")
        ns   = meta.get("namespace", "")
        node = spec.get("nodeName", "")
        info = {
            "name":      name,
            "namespace": ns,
            "node":      node,
            "phase":     stat.get("phase", ""),
            "ip":        stat.get("podIP", ""),
        }
        pod_list.append(info)
        pid = f"k8s_pod:{cluster_name}:{ns}:{name}"
        await _aux_run(
            """
            MERGE (p:K8sPod {id:$id})
            SET p += $props, p.updated_at=$ts, p.source='k8s',
                p.cluster=$cluster
            """,
            id=pid, props=info, ts=now_iso(), cluster=cluster_name,
        )
        if node:
            await _aux_run(
                """
                MATCH (p:K8sPod {id:$pid}), (n:K8sNode {id:$nid})
                MERGE (p)-[:SCHEDULED_ON]->(n)
                """,
                pid=pid, nid=f"k8s_node:{cluster_name}:{node}",
            )
    await emit_event({"type": "netscan.k8s.done",
                      "cluster": cluster_name,
                      "nodes": len(node_list), "pods": len(pod_list)})
    return {"cluster":  cluster_name,
            "nodes":    node_list,
            "pods":     pod_list,
            "counts":   {"nodes": len(node_list), "pods": len(pod_list)}}


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — WEBSITE / HTTP FINGERPRINT
# ─────────────────────────────────────────────────────────────────────────────
_TECH_HEADER_SIGS = {
    "server":          {"nginx":"nginx","apache":"apache","caddy":"caddy","cloudflare":"cloudflare",
                         "gunicorn":"gunicorn","uvicorn":"uvicorn","envoy":"envoy","openresty":"openresty",
                         "microsoft-iis":"iis","litespeed":"litespeed","tornado":"tornado"},
    "x-powered-by":    {"php":"php","asp.net":"asp.net","express":"express","next.js":"next.js"},
    "x-generator":     {"wordpress":"wordpress","drupal":"drupal"},
    "x-drupal-cache":  {"": "drupal"},
    "x-shopify-stage": {"": "shopify"},
    "x-amz-cf-id":     {"": "cloudfront"},
    "cf-ray":          {"": "cloudflare"},
    "x-vercel-id":     {"": "vercel"},
    "x-fly-request-id":{"": "fly.io"},
    "x-served-by":     {"": "fastly"},
}
_TECH_BODY_SIGS = [
    ("wp-content/",             "wordpress"),
    ("/wp-includes/",           "wordpress"),
    ("Drupal.settings",         "drupal"),
    ("joomla-script-options",   "joomla"),
    ("__NEXT_DATA__",           "next.js"),
    ("window.__NUXT__",         "nuxt"),
    ("ng-version=",             "angular"),
    ("data-reactroot",          "react"),
    ("data-react-helmet",       "react"),
    ("<!-- Ghost",              "ghost"),
    ("shopify.theme",           "shopify"),
    ("cdn.shopify.com",         "shopify"),
    ("<meta name=\"generator\" content=\"Hugo", "hugo"),
    ("<meta name=\"generator\" content=\"Gatsby","gatsby"),
    ("Powered by Discourse",    "discourse"),
    ("phpBB",                   "phpbb"),
    ("MediaWiki",               "mediawiki"),
]
_TITLE_RE = re.compile(r"<title[^>]*>([^<]{1,300})</title>", re.I)
_META_DESC_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{1,400})',
                           re.I)
_LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.I)

def _fingerprint_http(headers: Dict[str, str], body: str) -> List[str]:
    tech = set()
    lower_headers = {k.lower(): (v or "").lower() for k, v in headers.items()}
    for hkey, sigs in _TECH_HEADER_SIGS.items():
        val = lower_headers.get(hkey, "")
        if not val:
            continue
        for needle, tname in sigs.items():
            if needle == "" or needle in val:
                tech.add(tname)
    if body:
        snippet = body[:20000]
        for needle, tname in _TECH_BODY_SIGS:
            if needle in snippet:
                tech.add(tname)
    return sorted(tech)


async def _http_probe(url: str, follow_redirects: bool = True,
                       timeout: float = 10.0) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects, verify=False,
            headers={"User-Agent": "VeraNetScan/1.0"},
        ) as c:
            r = await c.get(url)
            body = r.text if len(r.content or b"") < 500_000 else r.text[:500_000]
            title_m = _TITLE_RE.search(body or "")
            desc_m  = _META_DESC_RE.search(body or "")
            return {
                "ok":         True,
                "status":     r.status_code,
                "final_url":  str(r.url),
                "headers":    {k: v for k, v in r.headers.items()},
                "title":      (title_m.group(1).strip() if title_m else "")[:200],
                "description":(desc_m.group(1).strip() if desc_m else "")[:300],
                "tech":       _fingerprint_http(dict(r.headers), body or ""),
                "body":       body,
                "size":       len(r.content or b""),
                "elapsed_ms": int((r.elapsed.total_seconds() * 1000)
                                    if r.elapsed else 0),
            }
    except httpx.TimeoutException:
        return {"ok": False, "error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _site_id(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"site:{p.scheme}://{p.netloc}"


def _endpoint_id(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    path = p.path or "/"
    return f"endpoint:{p.scheme}://{p.netloc}{path}"


@capability(
    "netscan.web.scan",
    http_method="POST", http_path="/netscan/web/scan", http_tags=["netscan"],
    description="Fetch a website, fingerprint its tech stack, and optionally crawl same-origin "
                "links up to depth. Creates a :Website node + one :WebEndpoint per URL probed "
                "with :HAS_ENDPOINT edges, and :LINKS_TO edges between endpoints. "
                "Input: url (str!), max_depth (int=1), max_pages (int=20), "
                "follow_redirects (bool=true), timeout (float=10). "
                "Output: {site, endpoints, tech, counts}.",
)
async def cap_netscan_web(
    url:              str,
    max_depth:        int   = 1,
    max_pages:        int   = 20,
    follow_redirects: bool  = True,
    timeout:          float = 10.0,
    trace_id=None,
) -> Dict:
    from urllib.parse import urlparse, urljoin, urldefrag
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    start = urlparse(url)
    origin = f"{start.scheme}://{start.netloc}"
    site_id = _site_id(url)

    # Build site node
    first = await _http_probe(url, follow_redirects=follow_redirects, timeout=timeout)
    if not first.get("ok"):
        return {"error": first.get("error", "probe failed"),
                "site": origin, "endpoints": [], "tech": []}
    all_tech: set = set(first.get("tech") or [])
    all_status: Dict[int, int] = {}
    visited: set = set()
    queue: List[Tuple[str, int]] = [(first["final_url"], 0)]
    endpoints_out: List[Dict] = []

    await _aux_run(
        """
        MERGE (s:Website {id:$id})
        SET s.origin=$origin, s.title=$title, s.description=$desc,
            s.source='web', s.updated_at=$ts
        """,
        id=site_id, origin=origin, title=first.get("title", ""),
        desc=first.get("description", ""), ts=now_iso(),
    )

    while queue and len(visited) < max_pages:
        u, depth = queue.pop(0)
        u, _ = urldefrag(u)
        if u in visited:
            continue
        visited.add(u)
        probe = first if u == first["final_url"] else \
                 await _http_probe(u, follow_redirects=follow_redirects, timeout=timeout)
        if not probe.get("ok"):
            endpoints_out.append({"url": u, "error": probe.get("error")})
            continue
        ep_id = _endpoint_id(u)
        status = probe.get("status", 0)
        all_status[status] = all_status.get(status, 0) + 1
        tech = probe.get("tech") or []
        for t in tech:
            all_tech.add(t)
        ep_info = {
            "url":    u,
            "status": status,
            "title":  probe.get("title", ""),
            "size":   probe.get("size", 0),
            "tech":   tech,
        }
        endpoints_out.append(ep_info)
        await _aux_run(
            """
            MERGE (e:WebEndpoint {id:$eid})
            SET e.url=$url, e.status=$status, e.title=$title, e.size=$size,
                e.tech=$tech, e.source='web', e.updated_at=$ts
            WITH e
            MATCH (s:Website {id:$sid})
            MERGE (s)-[:HAS_ENDPOINT]->(e)
            """,
            eid=ep_id, url=u, status=status, title=probe.get("title","")[:200],
            size=probe.get("size", 0), tech=tech, ts=now_iso(), sid=site_id,
        )
        # Crawl same-origin links
        if depth < max_depth:
            body = probe.get("body") or ""
            for m in _LINK_RE.finditer(body[:50_000]):
                href = m.group(1)
                nxt = urljoin(u, href)
                nxt, _ = urldefrag(nxt)
                np = urlparse(nxt)
                if np.scheme not in ("http", "https"):
                    continue
                if np.netloc != start.netloc:
                    continue
                if nxt in visited:
                    continue
                if len(visited) + len(queue) >= max_pages:
                    break
                queue.append((nxt, depth + 1))
                await _aux_run(
                    """
                    MATCH (a:WebEndpoint {id:$a}), (b:WebEndpoint {id:$b})
                    MERGE (a)-[:LINKS_TO]->(b)
                    """, a=ep_id, b=_endpoint_id(nxt),
                )
    # Roll tech up to site node
    await _aux_run(
        "MATCH (s:Website {id:$id}) SET s.tech=$tech, s.pages=$n",
        id=site_id, tech=sorted(all_tech), n=len(visited),
    )
    await emit_event({"type": "netscan.web.done",
                      "site": origin, "pages": len(visited),
                      "tech": sorted(all_tech)})
    return {
        "site":      origin,
        "endpoints": endpoints_out,
        "tech":      sorted(all_tech),
        "counts":    {"pages": len(visited),
                       "statuses": all_status,
                       "tech": len(all_tech)},
    }


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — PER-TARGET TOOLS (ports / tech / traffic)
# ─────────────────────────────────────────────────────────────────────────────
_COMMON_PORTS = [
    21, 22, 23, 25, 53, 80, 81, 110, 111, 135, 139, 143, 161, 389, 443, 445,
    465, 587, 631, 636, 993, 995, 1080, 1433, 1521, 1723, 1883, 2049, 2181,
    2375, 2376, 2379, 3000, 3128, 3306, 3389, 3478, 4000, 4369, 4444, 4789,
    5000, 5044, 5060, 5432, 5601, 5672, 5900, 5984, 6000, 6379, 6443, 6667,
    7000, 7001, 7070, 7474, 7687, 8000, 8006, 8008, 8080, 8086, 8088, 8096,
    8123, 8200, 8300, 8443, 8500, 8529, 8686, 8888, 9000, 9042, 9090, 9092,
    9100, 9200, 9300, 9418, 9443, 9500, 9600, 9999, 10000, 11211, 15672,
    27017, 27018, 32400, 50000, 50070,
]
_PORT_HINTS = {
    21:"ftp",22:"ssh",23:"telnet",25:"smtp",53:"dns",80:"http",81:"http-alt",
    110:"pop3",111:"rpcbind",135:"msrpc",139:"netbios-ssn",143:"imap",
    161:"snmp",389:"ldap",443:"https",445:"smb",465:"smtps",587:"submission",
    631:"ipp",636:"ldaps",993:"imaps",995:"pop3s",1080:"socks",1433:"mssql",
    1521:"oracle",1723:"pptp",1883:"mqtt",2049:"nfs",2181:"zookeeper",
    2375:"docker",2376:"docker-tls",2379:"etcd",3000:"grafana/node",
    3306:"mysql",3389:"rdp",3478:"stun",5000:"http-app",5044:"logstash",
    5060:"sip",5432:"postgres",5601:"kibana",5672:"amqp",5900:"vnc",
    5984:"couchdb",6379:"redis",6443:"k8s-api",7000:"cassandra",7474:"neo4j-http",
    7687:"neo4j-bolt",8000:"http-dev",8006:"proxmox",8080:"http-proxy",
    8086:"influxdb",8096:"jellyfin",8123:"home-assistant",8200:"vault",
    8300:"consul",8443:"https-alt",8500:"consul-ui",8529:"arangodb",
    8888:"jupyter",9000:"minio/sonar",9042:"cassandra-cql",9090:"prometheus",
    9092:"kafka",9100:"node-exporter",9200:"elasticsearch",9418:"git",
    11211:"memcached",15672:"rabbitmq-ui",27017:"mongodb",32400:"plex",
    50000:"db2",
}


def _parse_port_spec(spec: str) -> List[int]:
    """Parse '22,80,1000-1100' or 'common' or profile name → list of ports."""
    spec = (spec or "").strip().lower()
    if not spec or spec in ("common", "default"):
        return list(_COMMON_PORTS)
    if spec in ("all", "*", "1-65535"):
        return list(range(1, 65536))
    # Check PORT_PROFILES (defined in the extras section) for names like
    # 'quick', 'web', 'database', 'iot', 'ms', 'extended'
    try:
        profiles = globals().get("PORT_PROFILES") or {}
        if spec in profiles:
            return list(profiles[spec])
    except Exception:
        pass
    out = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            try:
                a, b = chunk.split("-", 1)
                out.update(range(max(1, int(a)), min(65535, int(b)) + 1))
            except Exception:
                continue
        else:
            try:
                out.add(int(chunk))
            except Exception:
                continue
    return sorted(p for p in out if 1 <= p <= 65535)


@capability(
    "netscan.target.ports",
    http_method="POST", http_path="/netscan/target/ports", http_tags=["netscan"],
    description="TCP connect-scan a target across a port range. "
                "Updates the target NetHost (if present) with open_ports. "
                "Input: host (str!), ports (str='common' | '22,80,443' | '1-1024' | 'all'), "
                "timeout (float=0.8), concurrency (int=128), update_graph (bool=true). "
                "Output: {host, open: [{port,hint}], scanned, elapsed_ms}.",
)
async def cap_netscan_target_ports(
    host:         str,
    ports:        str   = "common",
    timeout:      float = 0.8,
    concurrency:  int   = 128,
    update_graph: bool  = True,
    trace_id=None,
) -> Dict:
    plist = _parse_port_spec(ports)
    if not plist:
        return {"error": "no ports to scan", "host": host, "open": []}
    sem = asyncio.Semaphore(max(1, min(concurrency, 512)))
    open_ports: List[Dict] = []

    async def probe(p: int):
        async with sem:
            try:
                ok = await _tcp_ping(host, p, timeout=timeout)
            except Exception:
                ok = False
            if ok:
                open_ports.append({"port": p, "hint": _PORT_HINTS.get(p, "")})

    t0 = time.monotonic()
    await asyncio.gather(*(probe(p) for p in plist))
    open_ports.sort(key=lambda x: x["port"])
    elapsed = round((time.monotonic() - t0) * 1000)

    if update_graph:
        try:
            ip = host
            try: ip = socket.gethostbyname(host)
            except Exception: pass
            host_id = f"net:{ip}"
            await _aux_run(
                """
                MERGE (h:NetHost {id:$id})
                SET h.ip=coalesce(h.ip,$ip), h.hostname=coalesce(h.hostname,$hn),
                    h.open_ports=$ports, h.ports_scanned_at=$ts, h.source=coalesce(h.source,'portscan')
                """,
                id=host_id, ip=ip, hn=host if host != ip else "",
                ports=[p["port"] for p in open_ports], ts=now_iso(),
            )
            # Create :NetPort nodes for discovered open ports
            await _aux_upsert_ports(host_id, ip, [p["port"] for p in open_ports])
        except Exception:
            pass

    await emit_event({"type": "netscan.target.ports.done",
                      "host": host, "open": len(open_ports),
                      "scanned": len(plist), "ms": elapsed})
    return {"host": host, "open": open_ports,
            "scanned": len(plist), "elapsed_ms": elapsed}


@capability(
    "netscan.target.tech",
    http_method="POST", http_path="/netscan/target/tech", http_tags=["netscan"],
    description="HTTP-fingerprint a target (single URL). Reports status, title, headers, "
                "and detected tech stack (wordpress, nginx, cloudflare, react, etc.). "
                "Input: url (str!) OR host (str!) — if host given, tries https:// then http://. "
                "Output: {final_url, status, title, tech, headers}.",
)
async def cap_netscan_target_tech(
    url:     str   = "",
    host:    str   = "",
    timeout: float = 10.0,
    trace_id=None,
) -> Dict:
    tries: List[str] = []
    if url:
        tries = [url if url.startswith(("http://", "https://")) else "http://" + url]
    elif host:
        tries = [f"https://{host}", f"http://{host}"]
    else:
        return {"error": "url or host required"}
    last_err = ""
    for u in tries:
        r = await _http_probe(u, follow_redirects=True, timeout=timeout)
        if r.get("ok"):
            return {
                "final_url":  r.get("final_url"),
                "status":     r.get("status"),
                "title":      r.get("title"),
                "description":r.get("description"),
                "tech":       r.get("tech") or [],
                "headers":    r.get("headers") or {},
                "size":       r.get("size"),
                "elapsed_ms": r.get("elapsed_ms"),
            }
        last_err = r.get("error", "")
    return {"error": last_err or "http probe failed",
            "tried": tries}


@capability(
    "netscan.target.traffic",
    http_method="POST", http_path="/netscan/target/traffic", http_tags=["netscan"],
    description="Monitor packet flow to/from a target for a bounded duration. "
                "Runs `ss -tn` (active sockets) locally or over SSH; optionally runs "
                "a short tcpdump capture if available and sudo-able. "
                "Input: host (str!), duration (int=5, seconds, max 30), "
                "ssh_host_id (str, optional — run from a remote vantage point), "
                "use_tcpdump (bool=false), iface (str, optional — e.g. 'eth0'). "
                "Output: {host, sockets: [...], tcpdump?: str, source}.",
)
async def cap_netscan_target_traffic(
    host:         str,
    duration:     int  = 5,
    ssh_host_id:  str  = "",
    use_tcpdump:  bool = False,
    iface:        str  = "",
    trace_id=None,
) -> Dict:
    duration = max(1, min(int(duration or 5), 30))
    ip = host
    try: ip = socket.gethostbyname(host)
    except Exception: pass

    # 1. sockets (always cheap)
    ss_cmd = f"ss -tn state established 2>/dev/null | grep -E {shlex.quote(ip)} || true"
    sockets: List[Dict] = []
    if ssh_host_id:
        rec = await _resolve_host_record(ssh_host_id)
        if not rec:
            return {"error": f"ssh_host_id not found: {ssh_host_id}"}
        pwd = _deobfuscate(rec.get("password_obf", "")) if rec.get("auth","password")=="password" else ""
        pph = _deobfuscate(rec.get("passphrase_obf", "")) if rec.get("auth")=="key" else ""
        r = await _ssh_run_on(
            rec["host"], ss_cmd,
            port=int(rec.get("port",22) or 22), user=rec.get("user",""),
            password=pwd, key_path=rec.get("key_path",""), passphrase=pph,
            timeout=15,
        )
        ss_out = r.get("stdout", "")
        source = f"ssh:{rec.get('host')}"
    else:
        r = await _run_local(["bash", "-lc", ss_cmd])
        ss_out = r.get("stdout", "")
        source = "local"
    for line in (ss_out or "").splitlines():
        parts = line.split()
        if len(parts) >= 5:
            sockets.append({"state": parts[0], "recvq": parts[1], "sendq": parts[2],
                             "local": parts[3], "peer": parts[4]})

    # 2. tcpdump (optional, requires sudo and libpcap)
    td_out = ""
    if use_tcpdump:
        iface_arg = f"-i {shlex.quote(iface)}" if iface else ""
        td_cmd = (f"sudo -n timeout {duration} tcpdump -n -c 200 "
                  f"{iface_arg} host {shlex.quote(ip)} 2>&1 || true")
        if ssh_host_id:
            rec = (await _resolve_host_record(ssh_host_id)) or {}
            pwd = _deobfuscate(rec.get("password_obf", "")) if rec.get("auth","password")=="password" else ""
            pph = _deobfuscate(rec.get("passphrase_obf", "")) if rec.get("auth")=="key" else ""
            r = await _ssh_run_on(
                rec.get("host",""), td_cmd,
                port=int(rec.get("port",22) or 22), user=rec.get("user",""),
                password=pwd, key_path=rec.get("key_path",""), passphrase=pph,
                timeout=duration + 10,
            )
            td_out = (r.get("stdout","") + "\n" + r.get("stderr","")).strip()
        else:
            r = await _run_local(["bash", "-lc", td_cmd], timeout=duration + 10)
            td_out = (r.get("stdout","") + "\n" + r.get("stderr","")).strip()

    await emit_event({"type": "netscan.target.traffic.done",
                      "host": host, "sockets": len(sockets),
                      "tcpdump": bool(td_out), "source": source})
    return {
        "host":     host,
        "ip":       ip,
        "sockets":  sockets,
        "tcpdump":  td_out[:20000] if td_out else "",
        "source":   source,
        "duration": duration,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — GRAPH READ ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
_NETSCAN_LABELS = (
    "NetHost", "Subnet", "DockerHost", "Container",
    "PVECluster", "PVENode", "PVEGuest",
    "K8sCluster", "K8sNode", "K8sPod",
    "Website", "WebEndpoint",
    "NetPort",
)
_NETSCAN_LABEL_SET = "|".join(_NETSCAN_LABELS)


@capability(
    "netscan.graph",
    http_method="GET", http_path="/netscan/graph", http_tags=["netscan"],
    memory="off",
    description="Fetch the full network asset graph (cytoscape-friendly). "
                "Output: {nodes: [{data:{id,label,type,...}}], "
                "edges: [{data:{id,source,target,label}}], counts}.",
)
async def cap_netscan_graph(trace_id=None) -> Dict:
    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return {"error": "Neo4j not connected", "nodes": [], "edges": []}
    try:
        async with fn._driver.session() as s:
            # Nodes
            res = await s.run(
                f"MATCH (n) WHERE ANY(l IN labels(n) WHERE l IN $labels) "
                f"RETURN n, labels(n) AS ls LIMIT 5000",
                labels=list(_NETSCAN_LABELS),
            )
            nodes = []
            seen_ids = set()
            async for row in res:
                n = row["n"]
                ls = row["ls"]
                props = dict(n)
                nid = props.get("id") or f"{ls[0]}:{props.get('name','?')}"
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)
                ntype = ls[0] if ls else "Unknown"
                label = (props.get("hostname") or props.get("name")
                         or props.get("label") or props.get("ip")
                         or nid)
                # Clean up unserializable values
                clean_props = {}
                for k, v in props.items():
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        clean_props[k] = v
                    elif isinstance(v, list):
                        clean_props[k] = [
                            x for x in v
                            if isinstance(x, (str, int, float, bool))
                        ]
                nodes.append({
                    "data": {
                        "id":    nid,
                        "label": str(label)[:50],
                        "type":  ntype,
                        **clean_props,
                    }
                })
            # Edges
            res = await s.run(
                f"MATCH (a)-[r]->(b) "
                f"WHERE ANY(l IN labels(a) WHERE l IN $labels) "
                f"AND ANY(l IN labels(b) WHERE l IN $labels) "
                f"RETURN a.id AS src, b.id AS dst, type(r) AS rel LIMIT 10000",
                labels=list(_NETSCAN_LABELS),
            )
            edges = []
            eseen = set()
            async for row in res:
                src = row["src"]; dst = row["dst"]; rel = row["rel"]
                if not src or not dst:
                    continue
                eid = f"{src}|{rel}|{dst}"
                if eid in eseen: continue
                eseen.add(eid)
                edges.append({
                    "data": {
                        "id":     eid,
                        "source": src,
                        "target": dst,
                        "label":  rel,
                    }
                })
            return {
                "nodes": nodes, "edges": edges,
                "counts": {"nodes": len(nodes), "edges": len(edges)},
            }
    except Exception as e:
        return {"error": str(e), "nodes": [], "edges": []}


@capability(
    "netscan.node.get",
    http_method="POST", http_path="/netscan/node/get", http_tags=["netscan"],
    memory="off",
    description="Fetch a single aux-graph node and its 1-hop neighbours. Input: id (str!).",
)
async def cap_netscan_node_get(id: str, trace_id=None) -> Dict:
    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return {"error": "Neo4j not connected"}
    try:
        async with fn._driver.session() as s:
            res = await s.run(
                "MATCH (n {id:$id}) "
                "OPTIONAL MATCH (n)-[r]-(m) "
                "RETURN n, labels(n) AS ls, collect({rel:type(r), "
                "  dir: CASE WHEN startNode(r)=n THEN 'out' ELSE 'in' END, "
                "  other: m, other_labels: labels(m)}) AS nb",
                id=id,
            )
            row = await res.single()
            if not row:
                return {"error": f"node not found: {id}"}
            n = dict(row["n"])
            ls = row["ls"]
            nb = []
            for e in row["nb"]:
                if not e or not e.get("other"):
                    continue
                other = dict(e["other"])
                nb.append({
                    "rel":          e["rel"],
                    "direction":    e["dir"],
                    "neighbour":    other,
                    "neighbour_type": (e.get("other_labels") or ["?"])[0],
                })
            return {"node": n, "type": ls[0] if ls else "?", "neighbours": nb}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "netscan.nodes.clear",
    http_method="POST", http_path="/netscan/nodes/clear", http_tags=["netscan"],
    description="Delete aux-graph nodes by source tag. "
                "Input: source (one of: lan, docker, proxmox, k8s, all). "
                "Output: {deleted}.",
)
async def cap_netscan_clear(source: str = "all", trace_id=None) -> Dict:
    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return {"error": "Neo4j not connected"}
    source = (source or "all").lower()
    if source == "all":
        where = f"ANY(l IN labels(n) WHERE l IN $labels)"
        params = {"labels": list(_NETSCAN_LABELS)}
    else:
        where = "n.source = $src"
        params = {"src": source}
    try:
        async with fn._driver.session() as s:
            res = await s.run(
                f"MATCH (n) WHERE {where} "
                f"WITH n, n.id AS nid DETACH DELETE n RETURN count(nid) AS c",
                **params,
            )
            row = await res.single()
            deleted = row["c"] if row else 0
        return {"ok": True, "deleted": deleted, "source": source}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# NETSCAN — MAP SNAPSHOTS  (save/load/list/delete named network maps)
# ─────────────────────────────────────────────────────────────────────────────

def _netmap_db():
    """Return the shared SQLite connection from data_fabric, or open a local one."""
    mod = sys.modules.get("data_fabric")
    if mod:
        fn = getattr(mod, "_sqlite_conn", None)
        if fn:
            return fn()
    import sqlite3
    db_path = Path(cfg.get("VERA_DATA_DIR", "/tmp/vera")) / "vera.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path), check_same_thread=False)


def _ensure_netmap_table():
    conn = _netmap_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS netscan_maps (
                map_id      TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                description TEXT,
                nodes_json  TEXT,
                edges_json  TEXT,
                meta_json   TEXT,
                created_at  TEXT,
                updated_at  TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


try:
    _ensure_netmap_table()
except Exception as _e:
    log.debug("netmap table init: %s", _e)


@capability(
    "netscan.map.save",
    http_method="POST", http_path="/netscan/map/save", http_tags=["netscan"],
    description="Save the current network graph as a named map snapshot. "
                "If map_id is omitted a new uuid is generated. "
                "Input: name (str!), description (str), map_id (str — update existing). "
                "Output: {ok, map_id, name}.",
)
async def cap_netscan_map_save(
    name:        str,
    description: str = "",
    map_id:      str = "",
    trace_id=None,
) -> Dict:
    # Fetch current live graph
    graph = await cap_netscan_graph()
    if "error" in graph:
        return {"error": graph["error"]}
    mid = map_id or str(uuid.uuid4())
    ts  = now_iso()
    loop = asyncio.get_running_loop()
    def _write():
        conn = _netmap_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO netscan_maps "
                "(map_id, name, description, nodes_json, edges_json, meta_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,COALESCE((SELECT created_at FROM netscan_maps WHERE map_id=?),?),?)",
                (mid, name, description,
                 json.dumps(graph["nodes"]), json.dumps(graph["edges"]),
                 json.dumps(graph.get("counts", {})),
                 mid, ts, ts),
            )
            conn.commit()
        finally:
            conn.close()
    await loop.run_in_executor(None, _write)
    await emit_event({"type": "netscan.map.saved", "map_id": mid, "name": name})
    return {"ok": True, "map_id": mid, "name": name,
            "nodes": len(graph["nodes"]), "edges": len(graph["edges"])}


@capability(
    "netscan.map.list",
    http_method="GET", http_path="/netscan/map/list", http_tags=["netscan"],
    memory="off", silent=True,
    description="List saved network map snapshots. "
                "Output: {maps: [{map_id, name, description, node_count, edge_count, created_at, updated_at}]}.",
)
async def cap_netscan_map_list(trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _netmap_db()
        try:
            rows = conn.execute(
                "SELECT map_id, name, description, meta_json, created_at, updated_at "
                "FROM netscan_maps ORDER BY updated_at DESC"
            ).fetchall()
            result = []
            for r in rows:
                meta = {}
                try: meta = json.loads(r[3] or "{}")
                except Exception: pass
                result.append({
                    "map_id":      r[0],
                    "name":        r[1],
                    "description": r[2] or "",
                    "node_count":  meta.get("nodes", 0),
                    "edge_count":  meta.get("edges", 0),
                    "created_at":  r[4],
                    "updated_at":  r[5],
                })
            return result
        finally:
            conn.close()
    maps = await loop.run_in_executor(None, _read)
    return {"maps": maps}


@capability(
    "netscan.map.load",
    http_method="POST", http_path="/netscan/map/load", http_tags=["netscan"],
    description="Load a saved network map snapshot — returns nodes+edges in cytoscape format. "
                "Input: map_id (str!). "
                "Output: {map_id, name, nodes, edges, counts}.",
)
async def cap_netscan_map_load(map_id: str, trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _netmap_db()
        try:
            row = conn.execute(
                "SELECT name, description, nodes_json, edges_json, meta_json "
                "FROM netscan_maps WHERE map_id=?", (map_id,)
            ).fetchone()
            return row
        finally:
            conn.close()
    row = await loop.run_in_executor(None, _read)
    if not row:
        return {"error": f"map not found: {map_id}"}
    nodes = json.loads(row[2] or "[]")
    edges = json.loads(row[3] or "[]")
    return {
        "map_id": map_id,
        "name":   row[0],
        "description": row[1] or "",
        "nodes":  nodes,
        "edges":  edges,
        "counts": {"nodes": len(nodes), "edges": len(edges)},
    }


@capability(
    "netscan.map.delete",
    http_method="POST", http_path="/netscan/map/delete", http_tags=["netscan"],
    description="Delete a saved network map snapshot. Input: map_id (str!). Output: {ok}.",
)
async def cap_netscan_map_delete(map_id: str, trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _del():
        conn = _netmap_db()
        try:
            conn.execute("DELETE FROM netscan_maps WHERE map_id=?", (map_id,))
            conn.commit()
        finally:
            conn.close()
    await loop.run_in_executor(None, _del)
    return {"ok": True, "map_id": map_id}


@capability(
    "netscan.fabric.load_web",
    http_method="POST", http_path="/netscan/fabric/load_web", http_tags=["netscan"],
    description="Load crawled Website/WebEndpoint records from the fabric graph "
                "into the network graph view (returns cytoscape format). "
                "Input: origin_filter (str — optional domain/origin to filter), limit (int=200). "
                "Output: {nodes, edges, counts}.",
)
async def cap_netscan_fabric_load_web(
    origin_filter: str = "",
    limit:         int = 200,
    trace_id=None,
) -> Dict:
    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return {"error": "Neo4j not connected", "nodes": [], "edges": []}
    where = ""
    if origin_filter:
        where = "WHERE n.origin CONTAINS $origin OR n.url CONTAINS $origin"
    try:
        async with fn._driver.session() as s:
            res = await s.run(
                f"MATCH (n) WHERE ANY(l IN labels(n) WHERE l IN ['Website','WebEndpoint']) "
                f"{where} RETURN n, labels(n) AS ls LIMIT $lim",
                origin=origin_filter, lim=limit,
            )
            nodes = []
            seen = set()
            async for row in res:
                n = dict(row["n"])
                ls = row["ls"]
                nid = n.get("id") or f"{(ls or ['?'])[0]}:{n.get('url','?')}"
                if nid in seen: continue
                seen.add(nid)
                ntype = (ls or ["WebEndpoint"])[0]
                label = n.get("title") or n.get("url") or nid
                clean = {k: v for k, v in n.items()
                         if isinstance(v, (str, int, float, bool)) or v is None}
                nodes.append({"data": {"id": nid, "label": str(label)[:60],
                                        "type": ntype, **clean}})
            res2 = await s.run(
                "MATCH (a)-[r]->(b) "
                "WHERE ANY(l IN labels(a) WHERE l IN ['Website','WebEndpoint']) "
                "AND ANY(l IN labels(b) WHERE l IN ['Website','WebEndpoint']) "
                "RETURN a.id AS src, b.id AS dst, type(r) AS rel LIMIT 5000",
            )
            edges = []
            eseen = set()
            async for row in res2:
                src = row["src"]; dst = row["dst"]; rel = row["rel"]
                if not src or not dst: continue
                eid = f"{src}|{rel}|{dst}"
                if eid in eseen: continue
                eseen.add(eid)
                edges.append({"data": {"id": eid, "source": src,
                                        "target": dst, "label": rel}})
        return {"nodes": nodes, "edges": edges,
                "counts": {"nodes": len(nodes), "edges": len(edges)}}
    except Exception as e:
        return {"error": str(e), "nodes": [], "edges": []}


# ─────────────────────────────────────────────────────────────────────────────
# ASK VERA — scoped plan-stream + model listing
# ─────────────────────────────────────────────────────────────────────────────
#
# The core /dag/plan_stream endpoint gives every registered capability to the
# planner, which is overwhelming for a per-tab LLM chat. These two additions:
#
#   exec.llm.models       →  list available Ollama models per instance
#   /dag/plan_stream_scoped → accept allowed_caps + model/instance and pipe
#                             through plan_dag(available_caps=...) and the
#                             existing _hitl_run_graph_stream / _stepwise_run
#
# We defer the imports until call time to avoid module-load cycles.
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "exec.llm.models",
    http_method="GET", http_path="/exec/llm/models", http_tags=["llm"],
    memory="off", silent=True,
    description="List LLM models available across all online Ollama instances. "
                "Output: {models: [{name, instance_id, instance_label, has_gpu, "
                "size_bytes, parameter_size}], instances: [...], default_model}.",
)
async def cap_llm_models(trace_id=None) -> Dict:
    from Vera.Orchestration import capability_orchestration as _co
    instances = getattr(_co, "OLLAMA_INSTANCES", {}) or {}
    default_model = getattr(_co, "OLLAMA_MODEL", "") or ""
    models: List[Dict] = []
    inst_info: List[Dict] = []
    seen: set = set()
    for iid, inst in instances.items():
        inst_info.append({
            "id":       iid,
            "url":      inst.get("url", ""),
            "label":    inst.get("label", iid),
            "status":   inst.get("status", "unknown"),
            "has_gpu":  bool(inst.get("has_gpu")),
            "latency_ms": inst.get("latency_ms", 0),
        })
        if inst.get("status") != "online":
            continue
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(f"{inst['url']}/api/tags")
                r.raise_for_status()
                tags = r.json().get("models") or []
            for t in tags:
                name = t.get("name") or t.get("model") or ""
                if not name:
                    continue
                details = t.get("details") or {}
                key = (iid, name)
                if key in seen:
                    continue
                seen.add(key)
                models.append({
                    "name":           name,
                    "instance_id":    iid,
                    "instance_label": inst.get("label", iid),
                    "has_gpu":        bool(inst.get("has_gpu")),
                    "size_bytes":     t.get("size", 0),
                    "parameter_size": details.get("parameter_size", ""),
                    "family":         details.get("family", ""),
                    "quantization":   details.get("quantization_level", ""),
                })
        except Exception as e:
            log.debug("ollama tags fetch failed (%s): %s", iid, e)
    # Sort: GPU-hosted models first, then by name
    models.sort(key=lambda m: (not m.get("has_gpu"), m.get("name", "")))
    return {
        "models":        models,
        "instances":     inst_info,
        "default_model": default_model,
        "count":         len(models),
    }


@APP.post("/dag/plan_stream_scoped")
async def dag_plan_stream_scoped(request: Request):
    """
    Scoped planner-stream. Same event shape as /dag/plan_stream but:
      • allowed_caps (list[str])   — restrict planner to a subset; default = all
      • include_extras (list[str]) — explicit caps to add on top of allowed_caps
      • model (str)                — Ollama model name (passes to ollama_generate)
      • instance_id (str)          — specific instance to use
      • state (dict)               — seed state (tab context etc.) merged before run
      • mode ("oneshot"|"stepwise"), execute (bool), hitl (bool), auto_approve_secs (int)

    Streams the same `dag.*` events so the existing client can consume it
    unchanged.
    """
    import json as _json
    from Vera.Orchestration import capability_orchestration as _co

    try:    body = await request.json()
    except: body = {}

    goal              = str(body.get("goal", "") or "")
    mode              = str(body.get("mode", "oneshot") or "oneshot")
    do_execute        = bool(body.get("execute", True))
    hitl              = bool(body.get("hitl", True))
    auto_approve_secs = int(body.get("auto_approve_secs", 30) or 30)
    allowed_caps      = list(body.get("allowed_caps") or [])
    include_extras    = list(body.get("include_extras") or [])
    seed_state        = dict(body.get("state") or {})
    model             = str(body.get("model", "") or "")
    instance_id       = str(body.get("instance_id", "") or "")

    # Compose the final allow-list. Empty = full registry.
    if allowed_caps or include_extras:
        final_caps = list(dict.fromkeys([*allowed_caps, *include_extras]))
        # Filter to ones that actually exist in the registry
        final_caps = [c for c in final_caps if c in _co.CAPABILITY_REGISTRY]
    else:
        final_caps = None  # unrestricted

    # Inject model/instance preference through a context var that
    # ollama_generate will respect. We do this by wrapping ollama_generate.
    _orig_generate = _co.ollama_generate

    async def _patched_generate(prompt, system="", json_mode=False,
                                 model=None, instance_id=None,
                                 prefer_gpu=False, stream_cb=None):
        return await _orig_generate(
            prompt,
            system=system, json_mode=json_mode,
            model=(model if model else (_PATCH_MODEL or None)),
            instance_id=(instance_id if instance_id else (_PATCH_INSTANCE or None)),
            prefer_gpu=prefer_gpu, stream_cb=stream_cb,
        )

    # Per-request overrides — captured in closure, not globals
    _PATCH_MODEL = model
    _PATCH_INSTANCE = instance_id

    def _sse(t, d):
        return f"data: {_json.dumps({'type': t, **d})}\n\n".encode()

    async def _gen():
        # Install wrapper (module-level; only for the duration of this stream)
        if model or instance_id:
            _co.ollama_generate = _patched_generate
        try:
            if not goal:
                yield _sse("dag.error", {"error": "No goal provided"})
                return

            # Emit the scope up front so the UI can display it
            yield _sse("dag.scope", {
                "allowed_caps":  final_caps or [],
                "unrestricted":  final_caps is None,
                "model":         model,
                "instance_id":   instance_id,
                "cap_count":     len(final_caps) if final_caps else len(_co.CAPABILITY_REGISTRY),
            })

            # ── STEPWISE ──────────────────────────────────────────────────────
            if mode == "stepwise":
                try:
                    async for ev_type, ev_data in _scoped_stepwise_run(
                            _co, goal, seed_state, hitl, auto_approve_secs,
                            final_caps):
                        yield _sse(ev_type, ev_data)
                except Exception as e:
                    yield _sse("dag.error", {"error": f"stepwise error: {e}"})
                yield b"data: [DONE]\n\n"
                return

            # ── ONESHOT ───────────────────────────────────────────────────────
            yield _sse("dag.planning", {"goal": goal})
            try:
                plan = await _co.plan_dag(goal, available_caps=final_caps)
            except Exception as e:
                yield _sse("dag.error", {"error": f"planner: {e}"})
                return
            if plan.get("error") and not plan.get("dag"):
                yield _sse("dag.error", {"error": plan["error"]})
                return

            dag_arr    = plan.get("dag", [])
            plan_state = dict(plan.get("initial_state") or {})
            plan_state.update(seed_state)

            yield _sse("dag.plan_ready", {
                "dag":           dag_arr,
                "initial_state": plan_state,
                "rationale":     plan.get("rationale", ""),
                "steps":         len(dag_arr),
                "execute":       do_execute,
                "hitl":          hitl,
                "warnings":      plan.get("warnings") or [],
            })

            if not do_execute:
                yield _sse("dag.done", {"dag": dag_arr})
                return

            # Use the core HITL runner — same event shape
            async for ev_type, ev_data in _co._hitl_run_graph_stream(
                    dag_arr, plan_state, hitl, auto_approve_secs):
                yield _sse(ev_type, ev_data)

            yield b"data: [DONE]\n\n"
        finally:
            if model or instance_id:
                _co.ollama_generate = _orig_generate

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def _scoped_stepwise_run(_co, goal: str, state: dict, hitl: bool,
                                auto_approve_secs: int,
                                allowed_caps: Optional[List[str]]):
    """Minimal stepwise agent loop scoped to allowed_caps.
    Mirrors the core _stepwise_run event contract so the UI renders identically."""
    import json as _json, uuid as _uuid

    cap_keys = allowed_caps or list(_co.CAPABILITY_REGISTRY.keys())

    def _cap_sig(k):
        cap  = _co.CAPABILITY_REGISTRY.get(k, {})
        props = cap.get("schema", {}).get("properties", {})
        req  = set(cap.get("schema", {}).get("required", []))
        params = ", ".join(
            f"{p}:{v.get('type','str')}{'!' if p in req else ''}"
            for p, v in props.items() if p not in ("trace_id",)
        )
        desc = (cap.get("description") or "")[:80]
        return f"  {k}({params}) — {desc}"

    cap_desc = "\n".join(_cap_sig(k) for k in cap_keys)

    SYSTEM = (
        "You are a Vera agent executing a goal step by step. "
        "At each step output a SINGLE JSON object with one of two shapes:\n"
        '  NEXT STEP:  {"action":"call","cap":"capability_name","params":{"k":"v"},"out_key":"result","reason":"why"}\n'
        '  FINISHED:   {"action":"done","summary":"what was accomplished"}\n'
        "Rules:\n"
        "- Only use capability names from the provided list.\n"
        "- params must match the capability signature exactly.\n"
        "- out_key names the state key where the result will be stored.\n"
        "- Output ONLY the JSON object, no markdown, no prose.\n"
    )

    step = 0
    MAX_STEPS = 12
    history: List[Dict] = []

    while step < MAX_STEPS:
        hist_text = "\n".join(
            f"Step {i+1}: called {h['cap']} → {h['result'][:120]}"
            for i, h in enumerate(history)
        ) or "None yet"
        prompt = (
            f"Goal: {goal}\n\n"
            f"Steps taken so far:\n{hist_text}\n\n"
            f"Current state keys: {list(state.keys())}\n\n"
            f"Available capabilities:\n{cap_desc}\n\n"
            "Decide the next step (or finish)."
        )
        try:
            raw = await _co.ollama_generate(prompt, system=SYSTEM, json_mode=True,
                                             prefer_gpu=True)
        except Exception as e:
            yield "dag.error", {"error": f"llm: {e}"}
            return
        try:
            decision = _json.loads(raw)
        except Exception:
            # Fall back: extract outermost {…}
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", raw or "")
            try:
                decision = _json.loads(m.group()) if m else {}
            except Exception:
                decision = {}
        action = decision.get("action")
        if action == "done":
            yield "dag.complete", {"state": state,
                                    "summary": decision.get("summary", "")}
            return
        if action != "call":
            yield "dag.error", {"error": f"invalid decision: {raw[:300]}"}
            return

        cap_name = decision.get("cap", "")
        params   = decision.get("params") or {}
        out_key  = decision.get("out_key") or f"step_{step}"
        reason   = decision.get("reason", "")

        if cap_name not in _co.CAPABILITY_REGISTRY:
            yield "dag.error", {"error": f"unknown cap: {cap_name}"}
            return

        yield "dag.step_start", {"step": step, "total": MAX_STEPS,
                                 "cap": cap_name, "out_key": out_key,
                                 "reason": reason}

        if hitl:
            step_trace = str(_uuid.uuid4())
            fut = asyncio.get_event_loop().create_future()
            _co._HITL_PENDING[step_trace] = fut
            yield "dag.hitl_request", {"step": step, "cap": cap_name,
                                       "out_key": out_key,
                                       "params": params,
                                       "trace_id": step_trace,
                                       "auto_approve_secs": auto_approve_secs}
            try:
                decision_hitl = await asyncio.wait_for(
                    fut, timeout=float(auto_approve_secs))
            except asyncio.TimeoutError:
                decision_hitl = {"action": "approve", "edited_params": {}}
            finally:
                _co._HITL_PENDING.pop(step_trace, None)
            if decision_hitl["action"] == "reject":
                yield "dag.hitl_rejected", {"step": step, "cap": cap_name}
                yield "dag.complete", {"state": state, "aborted_at": step,
                                        "reason": "user rejected"}
                return
            if decision_hitl["action"] == "edit":
                params = decision_hitl.get("edited_params") or params

        # Execute — filter to accepted params
        cap_obj = _co.CAPABILITY_REGISTRY[cap_name]
        accepted = set(cap_obj["schema"].get("properties", {}).keys())
        call_params = {k: v for k, v in {**state, **params}.items() if k in accepted}
        try:
            result = await cap_obj["func"](**call_params)
        except Exception as e:
            yield "dag.step_error", {"step": step, "cap": cap_name, "error": str(e)}
            history.append({"cap": cap_name, "result": f"error: {e}"})
            state[out_key] = {"error": str(e)}
            step += 1
            continue

        state[out_key] = result
        history.append({"cap": cap_name,
                        "result": json.dumps(result, default=str)[:400]})
        yield "dag.step_done", {"step": step, "cap": cap_name,
                                "out_key": out_key,
                                "result_preview": str(result)[:200]}
        step += 1

    yield "dag.complete", {"state": state,
                            "reason": f"max steps reached ({MAX_STEPS})"}


# ─────────────────────────────────────────────────────────────────────────────
# PANEL HTML ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent

@APP.get("/exec/panel", include_in_schema=False)
async def _research_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "exec_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>dream_panel.html not found</p>")


@APP.get("/netmap/panel", include_in_schema=False)
async def _research_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "netmap_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>dream_panel.html not found</p>")



# @capability(
#     "exec.panel.html",
#     http_method="GET", http_path="/exec/panel", http_tags=["exec", "ui"],
#     memory="off", silent=True,
#     description="Serve the Exec panel HTML.",
# )
# async def exec_panel_html(trace_id=None):
#     p = _HERE / "exec_panel.html"
#     if not p.exists():
#         return HTMLResponse(
#             f"<body style='background:#0d0f12;color:#ef4444;"
#             f"font-family:monospace;padding:40px'>"
#             f"<h2>exec_panel.html not found</h2>"
#             f"<p>Expected: {p}</p></body>"
#         )
#     return HTMLResponse(p.read_text(encoding="utf-8"))


# @capability(
#     "netmap.panel.html",
#     http_method="GET", http_path="/netmap/panel", http_tags=["netscan", "ui"],
#     memory="off", silent=True,
#     description="Serve the Network Map panel HTML.",
# )
# async def netmap_panel_html(trace_id=None):
#     p = _HERE / "netmap_panel.html"
#     if not p.exists():
#         return HTMLResponse(
#             f"<body style='background:#0d0f12;color:#ef4444;"
#             f"font-family:monospace;padding:40px'>"
#             f"<h2>netmap_panel.html not found</h2>"
#             f"<p>Expected: {p}</p></body>"
#         )
#     return HTMLResponse(p.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# REGISTER UI PANELS
# ─────────────────────────────────────────────────────────────────────────────
register_ui(
    "exec-panel",
    "Exec",
    "▷_",
    """<div id="exec-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/exec/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "exec.bash.run", "exec.ps.run", "exec.ssh.run",
        "exec.ssh.hosts.list", "exec.ssh.hosts.save",
        "exec.ssh.hosts.delete", "exec.ssh.probe",
        "exec.llm.models",
        "dag.plan", "dag.plan_and_run",
    ],
    mode="tab",
    tab_order=53,
)

register_ui(
    "netmap-panel",
    "Network",
    "⬢",
    """<div id="netmap-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/netmap/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "netscan.lan.scan", "netscan.docker.scan",
        "netscan.proxmox.scan", "netscan.k8s.scan", "netscan.web.scan",
        "netscan.target.ports", "netscan.target.tech", "netscan.target.traffic",
        "netscan.graph", "netscan.node.get", "netscan.nodes.clear",
        "netscan.map.save", "netscan.map.list", "netscan.map.load", "netscan.map.delete",
        "netscan.fabric.load_web",
        "exec.ssh.hosts.list", "exec.ssh.probe", "exec.ssh.run",
        "exec.llm.models",
        "dag.plan", "dag.plan_and_run",
    ],
    mode="tab",
    tab_order=54,
)


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────
async def _startup():
    log.info("exec_capabilities ready — asyncssh=%s, ssh_store=%s",
             "yes" if HAS_ASYNCSSH else "NO (install asyncssh)",
             _SSH_STORE_PATH)


try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass



"""
netscan_extras.py  —  Vera Network Scan extensions
====================================================

Companion module to `exec_capabilities.py`. Adds:

  • netscan.lan.stream         — SSE-streaming LAN sweep (host nodes appear live)
  • netscan.target.ports.stream — SSE port scan (ports appear live)
  • netscan.web.stream         — SSE crawl (page nodes appear live)
  • netscan.target.banner      — TCP banner grab on a single (host, port)
  • netscan.target.tls         — TLS certificate inspection (subject, SAN, issuer)
  • netscan.target.fingerprint — combined ports + banners + tech (one-shot)
  • netscan.target.traceroute  — Run traceroute and persist :Hop nodes + :ROUTES_TO edges
  • netscan.dork.search        — Google-dork-style search against a search engine
                                  (DuckDuckGo HTML, no API key) + URL extraction
  • netscan.dork.targeted      — Run dork query *and* fingerprint each result
  • netscan.graph.clear_all    — Wipe ALL netscan nodes (new-graph button)
  • netscan.lan.scan_v2        — Configurable LAN scan with profile presets:
                                    quick / common / extended / web / database / all
                                    + service detection toggle
  • netscan.web.scan_v2        — Crawl with configurable crawl rules:
                                    same_origin / same_registrable / no_filter,
                                    path filter regex, exclude regex,
                                    user-agent override, robots.txt obey, wait_ms

All new caps reuse the aux-graph helpers that already exist in
`exec_capabilities` (we import them lazily) so they share the same Neo4j
storage layer, the same NetHost / NetPort node model, and emit the same
`emit_event` channel — which is what the `netmap_panel.html` UI listens on.

NOTE: This module DOES NOT replace anything in exec_capabilities.py.
It adds new caps alongside.  Two small monkey-patches are applied at
import time:

  1. Fix `_aux_upsert_ports` to also store the port value as `name` on
     the NetPort node so the netscan.graph endpoint will surface it as
     the cytoscape label (port number) rather than the IP.

  2. Patch `cap_netscan_graph` to use port number as the label for
     :NetPort nodes.

Both patches are idempotent; if the upstream module already has the fix,
the patch is a no-op.
"""

# from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse as _urlparse
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
)

log = logging.getLogger("vera.netscan.extras")


# ─────────────────────────────────────────────────────────────────────────────
# Lazy access to functions defined in exec_capabilities.py
# ─────────────────────────────────────────────────────────────────────────────
def _exec_mod():
    return sys.modules.get("exec_capabilities") or sys.modules.get(
        "Vera.Orchestration.exec_capabilities"
    )


def _ec_attr(name: str, default=None):
    m = _exec_mod()
    if not m:
        return default
    return getattr(m, name, default)


# ─────────────────────────────────────────────────────────────────────────────
# Monkey-patch #1: upsert NetPort with port-number label
# ─────────────────────────────────────────────────────────────────────────────
def _install_port_label_fix() -> None:
    """Patch `_aux_upsert_ports` to set `name` = port number, so the generic
    label-resolution in `cap_netscan_graph` surfaces the port instead of the
    IP. Also patch the graph capability to give NetPort an explicit label."""
    # Since extras are now in the same file, use the original _aux_run
    # directly rather than going through module lookup.
    _real_aux_run = _orig_aux_run
    port_hints = _orig_PORT_HINTS or {}

    async def _aux_upsert_ports_fixed(host_id: str, ip: str,
                                      open_ports: List[int]) -> None:
        for port in open_ports:
            hint = port_hints.get(port, "")
            pid = f"port:{ip}:{port}"
            label = f"{port}/tcp" + (f" {hint}" if hint else "")
            await _real_aux_run(
                """
                MERGE (p:NetPort {id: $pid})
                SET p.port=$port, p.ip=$ip, p.hint=$hint,
                    p.name=$name, p.label=$label,
                    p.updated_at=$ts
                WITH p
                MATCH (h:NetHost {id: $hid})
                MERGE (h)-[:EXPOSES]->(p)
                """,
                pid=pid, port=port, ip=ip, hint=hint,
                name=str(port), label=label, hid=host_id, ts=now_iso(),
            )

    # Patch it on the module so the original scan code uses the fixed version too
    m = _exec_mod()
    if m:
        setattr(m, "_aux_upsert_ports", _aux_upsert_ports_fixed)
    # Also update our own saved reference
    global _orig_aux_upsert_ports
    _orig_aux_upsert_ports = _aux_upsert_ports_fixed
    log.info("netscan_extras: NetPort label fix installed")


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight helpers — direct references to the originals defined earlier
# in this file.  (The extras code was originally a separate module that
# delegated via _ec_attr; now that everything lives in one file the wrapper
# pattern causes infinite recursion because the second definition shadows
# the first in the module namespace.)
# ─────────────────────────────────────────────────────────────────────────────
# Save direct references to the originals BEFORE we (re-)define names
# that would shadow them.  The functions at lines ~1110-1220 are the real
# implementations; the names below are used by the streaming scan code
# further down.
_orig_tcp_ping          = _tcp_ping           # line ~1110
_orig_reverse_dns       = _reverse_dns        # line ~1130
_orig_aux_upsert_nethost = _aux_upsert_nethost  # line ~1169
_orig_aux_upsert_ports  = _aux_upsert_ports   # line ~1204
_orig_aux_run           = _aux_run            # line ~141
_orig_PORT_HINTS        = globals().get("_PORT_HINTS", {})


# Thin aliases so the rest of the extras code can call these without
# worrying about shadowing.  No delegation, no _ec_attr, no recursion.
async def _tcp_ping(host: str, port: int, timeout: float = 0.8) -> bool:
    return await _orig_tcp_ping(host, port, timeout)


async def _reverse_dns(ip: str) -> str:
    return await _orig_reverse_dns(ip)


async def _aux_upsert_nethost(ip: str, **kw) -> None:
    await _orig_aux_upsert_nethost(ip, **kw)


async def _aux_upsert_ports(host_id: str, ip: str,
                            open_ports: List[int]) -> None:
    await _orig_aux_upsert_ports(host_id, ip, open_ports)


def _port_hint(port: int) -> str:
    return _orig_PORT_HINTS.get(port, "")


async def _aux_run(cypher: str, **params) -> List[Dict]:
    return await _orig_aux_run(cypher, **params)


# ─────────────────────────────────────────────────────────────────────────────
# SSE helper
# ─────────────────────────────────────────────────────────────────────────────
def _sse(event: str, data: Any) -> bytes:
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE PORT SETS
# ─────────────────────────────────────────────────────────────────────────────
PORT_PROFILES = {
    "quick":     [22, 80, 443, 3389, 8080, 8443],
    "common":    [21, 22, 23, 25, 53, 80, 110, 135, 139, 143, 161, 389, 443,
                   445, 465, 587, 631, 636, 993, 995, 1433, 1521, 3306, 3389,
                   5432, 5900, 6379, 8000, 8006, 8080, 8443, 8888, 9090, 9200,
                   27017],
    "web":       [80, 81, 443, 591, 2082, 2083, 2086, 2087, 2095, 2096,
                   3000, 3128, 3306, 5000, 5601, 7547, 8000, 8008, 8080,
                   8081, 8088, 8090, 8096, 8123, 8181, 8443, 8888, 9000,
                   9090, 9200, 9300, 32400],
    "database":  [1433, 1521, 3306, 5432, 5984, 6379, 7000, 7001, 7474,
                   7687, 8086, 9042, 9092, 9200, 11211, 27017, 27018, 28015],
    "iot":       [80, 443, 1883, 5353, 5683, 8080, 8123, 8443, 8883, 9999,
                   23, 2323, 7547, 49152],
    "ms":        [88, 135, 139, 389, 445, 464, 593, 636, 993, 995, 1433,
                   3268, 3269, 3389, 5985, 5986, 9389],
    "extended":  list(range(1, 1025)),
    "all":       list(range(1, 65536)),
}


def _resolve_port_spec(spec: str) -> List[int]:
    spec = (spec or "").strip().lower()
    if not spec:
        return list(PORT_PROFILES["common"])
    if spec in PORT_PROFILES:
        return list(PORT_PROFILES[spec])
    # Try the parser from exec_capabilities
    parser = _ec_attr("_parse_port_spec")
    if parser:
        try:
            r = parser(spec)
            if r:
                return r
        except Exception:
            pass
    out: set = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            try:
                a, b = chunk.split("-", 1)
                out.update(range(max(1, int(a)), min(65535, int(b)) + 1))
            except Exception:
                continue
        else:
            try:
                out.add(int(chunk))
            except Exception:
                continue
    return sorted(p for p in out if 1 <= p <= 65535)


# ═════════════════════════════════════════════════════════════════════════════
# 1.  STREAMING LAN SCAN — results appear in the graph live
# ═════════════════════════════════════════════════════════════════════════════
@APP.post("/netscan/lan/stream", tags=["netscan"], include_in_schema=True,
          summary="SSE-stream a LAN sweep; emits a 'host' event for every "
                  "live host as it's discovered.")
async def lan_scan_stream(request: Request):
    """Body: {cidr, ports, timeout, concurrency, port_nodes, profile}"""
    try:
        body = await request.json()
    except Exception:
        body = {}

    cidr        = (body.get("cidr") or "").strip()
    ports_spec  = body.get("ports") or body.get("profile") or "quick"
    timeout     = float(body.get("timeout") or 1.0)
    concurrency = int(body.get("concurrency") or 64)
    port_nodes  = bool(body.get("port_nodes", True))
    profile     = body.get("profile") or ""

    if profile and not body.get("ports"):
        ports_spec = profile

    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except Exception as e:
        async def _err():
            yield _sse("error", {"error": f"invalid cidr: {e}"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    plist = _resolve_port_spec(str(ports_spec))
    subnet = str(net)
    hosts = list(net.hosts())

    async def _gen() -> AsyncGenerator[bytes, None]:
        yield _sse("start", {
            "cidr": subnet, "ports": plist[:50],
            "port_count": len(plist), "host_count": len(hosts),
            "ts": now_iso(),
        })
        sem = asyncio.Semaphore(max(1, min(concurrency, 256)))
        done_count = 0
        live_count = 0
        send_q: asyncio.Queue = asyncio.Queue()

        async def probe_host(ip_str: str):
            nonlocal done_count, live_count
            async with sem:
                results = await asyncio.gather(
                    *[_tcp_ping(ip_str, p, timeout) for p in plist],
                    return_exceptions=True,
                )
                open_ports = [p for p, ok in zip(plist, results) if ok is True]
                done_count += 1
                if open_ports:
                    live_count += 1
                    hostname = await _reverse_dns(ip_str)
                    rec = {
                        "ip": ip_str, "hostname": hostname,
                        "open_ports": open_ports,
                        "subnet": subnet,
                    }
                    host_id = f"net:{ip_str}"
                    await _aux_upsert_nethost(
                        ip_str, hostname=hostname, subnet=subnet,
                        open_ports=open_ports, source="lan",
                    )
                    if port_nodes:
                        await _aux_upsert_ports(host_id, ip_str, open_ports)
                    await send_q.put(("host", rec))
                # progress every 8 hosts
                if done_count % 8 == 0:
                    await send_q.put(("progress", {
                        "done": done_count, "total": len(hosts),
                        "live": live_count,
                    }))

        async def runner():
            await asyncio.gather(
                *[probe_host(str(ip)) for ip in hosts],
                return_exceptions=True,
            )
            await send_q.put(("done", {"live": live_count,
                                       "scanned": len(hosts)}))

        task = asyncio.create_task(runner())
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    ev, payload = await asyncio.wait_for(send_q.get(),
                                                          timeout=1.0)
                except asyncio.TimeoutError:
                    yield _sse("ping", {"ts": now_iso()})
                    if task.done():
                        # drain anything queued
                        while not send_q.empty():
                            ev, payload = send_q.get_nowait()
                            yield _sse(ev, payload)
                        break
                    continue
                yield _sse(ev, payload)
                if ev == "done":
                    break
        finally:
            with contextlib.suppress(Exception):
                if not task.done():
                    task.cancel()
            await emit_event({"type": "netscan.lan.done",
                              "cidr": subnet, "count": live_count})

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-cache"})


# ═════════════════════════════════════════════════════════════════════════════
# 2.  STREAMING PORT SCAN — open ports show up live
# ═════════════════════════════════════════════════════════════════════════════
@APP.post("/netscan/target/ports/stream", tags=["netscan"],
          include_in_schema=True,
          summary="SSE-stream a port scan; emits 'port' as each open port is "
                  "found.")
async def target_ports_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    host        = (body.get("host") or "").strip()
    ports_spec  = body.get("ports") or body.get("profile") or "common"
    timeout     = float(body.get("timeout") or 0.8)
    concurrency = int(body.get("concurrency") or 256)

    if not host:
        async def _err():
            yield _sse("error", {"error": "host required"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    plist = _resolve_port_spec(str(ports_spec))
    if not plist:
        async def _err():
            yield _sse("error", {"error": "no ports to scan"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def _gen():
        yield _sse("start", {"host": host, "ports": len(plist),
                              "ts": now_iso()})
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            ip = host
        host_id = f"net:{ip}"
        sem = asyncio.Semaphore(max(1, min(concurrency, 1024)))
        open_ports: List[int] = []
        done = 0
        send_q: asyncio.Queue = asyncio.Queue()

        async def probe(p: int):
            nonlocal done
            async with sem:
                ok = await _tcp_ping(host, p, timeout=timeout)
                done += 1
                if ok:
                    open_ports.append(p)
                    hint = _port_hint(p)
                    await _aux_upsert_nethost(
                        ip, hostname=(host if host != ip else ""),
                        open_ports=open_ports, source="portscan",
                    )
                    await _aux_upsert_ports(host_id, ip, [p])
                    await send_q.put(("port", {"host": host, "ip": ip,
                                                "port": p, "hint": hint}))
                if done % 64 == 0:
                    await send_q.put(("progress",
                                       {"done": done, "total": len(plist)}))

        async def runner():
            await asyncio.gather(*(probe(p) for p in plist),
                                  return_exceptions=True)
            await send_q.put(("done", {"open": sorted(open_ports),
                                       "scanned": len(plist),
                                       "host": host, "ip": ip}))

        task = asyncio.create_task(runner())
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    ev, payload = await asyncio.wait_for(send_q.get(),
                                                          timeout=1.0)
                except asyncio.TimeoutError:
                    yield _sse("ping", {"ts": now_iso()})
                    if task.done():
                        while not send_q.empty():
                            ev, payload = send_q.get_nowait()
                            yield _sse(ev, payload)
                        break
                    continue
                yield _sse(ev, payload)
                if ev == "done":
                    break
        finally:
            with contextlib.suppress(Exception):
                if not task.done():
                    task.cancel()
            await emit_event({"type": "netscan.target.ports.done",
                              "host": host, "open": len(open_ports)})

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-cache"})


# ═════════════════════════════════════════════════════════════════════════════
# 3.  STREAMING WEB CRAWL — pages graphed as they're fetched
# ═════════════════════════════════════════════════════════════════════════════
def _registrable(host: str) -> str:
    """Crude registrable-domain extraction (good enough for in-graph grouping)"""
    parts = host.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host.lower()


@APP.post("/netscan/web/stream", tags=["netscan"], include_in_schema=True,
          summary="SSE-stream a website crawl; emits 'page' as each page is "
                  "fetched.")
async def web_scan_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    url        = (body.get("url") or "").strip()
    max_depth  = int(body.get("max_depth", 1))
    max_pages  = int(body.get("max_pages", 20))
    timeout    = float(body.get("timeout", 10.0))
    follow     = bool(body.get("follow_redirects", True))
    scope      = (body.get("scope") or "same_origin").lower()  # same_origin / same_registrable / no_filter
    path_re_s  = (body.get("path_filter") or "").strip()
    excl_re_s  = (body.get("exclude_filter") or "").strip()
    user_agent = (body.get("user_agent") or "VeraNetScan/1.0").strip()
    wait_ms    = max(0, int(body.get("wait_ms") or 0))

    if not url:
        async def _err():
            yield _sse("error", {"error": "url required"})
        return StreamingResponse(_err(), media_type="text/event-stream")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    try:
        path_re = re.compile(path_re_s) if path_re_s else None
        excl_re = re.compile(excl_re_s) if excl_re_s else None
    except Exception as e:
        async def _err():
            yield _sse("error", {"error": f"invalid regex: {e}"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    http_probe = _ec_attr("_http_probe")
    site_id_fn = _ec_attr("_site_id")
    ep_id_fn   = _ec_attr("_endpoint_id")
    link_re    = _ec_attr("_LINK_RE") or re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\']', re.I)

    if not (http_probe and site_id_fn and ep_id_fn):
        async def _err():
            yield _sse("error", {"error":
                "exec_capabilities web helpers not available"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def _gen():
        try:
            from urllib.parse import urlparse, urljoin, urldefrag
            start = urlparse(url)
            origin = f"{start.scheme}://{start.netloc}"
            site_id = site_id_fn(url)

            yield _sse("start", {"url": url, "scope": scope, "max_depth": max_depth,
                                  "max_pages": max_pages, "ts": now_iso()})

            first = await http_probe(url, follow_redirects=follow, timeout=timeout)
            if not first.get("ok"):
                yield _sse("error", {"error": first.get("error", "probe failed")})
                return

            all_tech: set = set(first.get("tech") or [])
            await _aux_run(
                """
                MERGE (s:Website {id:$id})
                SET s.origin=$origin, s.title=$title, s.description=$desc,
                    s.source='web', s.updated_at=$ts, s.url=$origin
                """,
                id=site_id, origin=origin,
                title=(first.get("title") or "")[:200],
                desc=(first.get("description") or "")[:300],
                ts=now_iso(),
            )
            yield _sse("site", {"id": site_id, "origin": origin,
                                 "title": first.get("title", "")})

            visited: set = set()
            queue: List[Tuple[str, int]] = [(first.get("final_url") or url, 0)]
            statuses: Dict[int, int] = {}
            target_reg = _registrable(start.netloc)

            while queue and len(visited) < max_pages:
                u, depth = queue.pop(0)
                u, _ = urldefrag(u)
                if u in visited:
                    continue
                visited.add(u)
                if u != (first.get("final_url") or url):
                    if wait_ms:
                        await asyncio.sleep(wait_ms / 1000.0)
                    probe = await http_probe(
                        u, follow_redirects=follow, timeout=timeout
                    )
                else:
                    probe = first
                if not probe.get("ok"):
                    yield _sse("page_err", {"url": u,
                                              "error": probe.get("error")})
                    continue
                ep_id = ep_id_fn(u)
                status = probe.get("status", 0)
                statuses[status] = statuses.get(status, 0) + 1
                tech = probe.get("tech") or []
                for t in tech:
                    all_tech.add(t)
                await _aux_run(
                    """
                    MERGE (e:WebEndpoint {id:$eid})
                    SET e.url=$url, e.status=$status, e.title=$title, e.size=$size,
                        e.tech=$tech, e.source='web', e.updated_at=$ts
                    WITH e
                    MATCH (s:Website {id:$sid})
                    MERGE (s)-[:HAS_ENDPOINT]->(e)
                    """,
                    eid=ep_id, url=u, status=status,
                    title=(probe.get("title", "") or "")[:200],
                    size=probe.get("size", 0), tech=tech, ts=now_iso(),
                    sid=site_id,
                )
                yield _sse("page", {
                    "id": ep_id, "url": u, "status": status,
                    "title": probe.get("title", ""),
                    "tech": tech, "depth": depth,
                })

                if depth < max_depth:
                    body_text = probe.get("body") or ""
                    for m in link_re.finditer(body_text[:80_000]):
                        href = m.group(1)
                        nxt = urljoin(u, href)
                        nxt, _ = urldefrag(nxt)
                        np = urlparse(nxt)
                        if np.scheme not in ("http", "https"):
                            continue
                        # Scope filter
                        if scope == "same_origin" and np.netloc != start.netloc:
                            continue
                        if scope == "same_registrable" and \
                                _registrable(np.netloc) != target_reg:
                            continue
                        # Path / exclude regex
                        if path_re and not path_re.search(np.path or "/"):
                            continue
                        if excl_re and excl_re.search(nxt):
                            continue
                        if nxt in visited:
                            continue
                        if len(visited) + len(queue) >= max_pages:
                            break
                        queue.append((nxt, depth + 1))
                        await _aux_run(
                            """
                            MATCH (a:WebEndpoint {id:$a}), (b:WebEndpoint {id:$b})
                            MERGE (a)-[:LINKS_TO]->(b)
                            """, a=ep_id, b=ep_id_fn(nxt),
                        )

            await _aux_run(
                "MATCH (s:Website {id:$id}) SET s.tech=$tech, s.pages=$n",
                id=site_id, tech=sorted(all_tech), n=len(visited),
            )
            yield _sse("done", {
                "site": origin, "pages": len(visited),
                "tech": sorted(all_tech),
                "statuses": statuses,
            })
            await emit_event({"type": "netscan.web.done",
                              "site": origin, "pages": len(visited)})
        except Exception as exc:
            log.exception("web_scan_stream error: %s", exc)
            yield _sse("error", {"error": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-cache"})


# ═════════════════════════════════════════════════════════════════════════════
# 4.  BANNER GRAB
# ═════════════════════════════════════════════════════════════════════════════
async def _grab_banner(host: str, port: int,
                       timeout: float = 3.0) -> Tuple[bool, str]:
    """Open TCP, optionally send a probe, read up to 1KB."""
    probe = b""
    if port in (80, 8000, 8080, 8081, 8443, 443, 8888):
        probe = (
            f"HEAD / HTTP/1.0\r\nHost: {host}\r\n"
            f"User-Agent: Vera-Banner/1.0\r\n\r\n"
        ).encode()
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except Exception as e:
        return False, f"connect failed: {e}"
    try:
        if probe:
            writer.write(probe)
            await writer.drain()
        data = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        return True, data.decode("utf-8", errors="replace")
    except Exception as e:
        return False, str(e)
    finally:
        with contextlib.suppress(Exception):
            writer.close()


@capability(
    "netscan.target.banner",
    http_method="POST", http_path="/netscan/target/banner",
    http_tags=["netscan"],
    description="TCP banner grab on a single (host, port). Opens a "
                "connection, sends a minimal probe for known ports, and "
                "reads up to 1KB. "
                "Input: host (str!), port (int!), timeout (float=3.0). "
                "Output: {host, port, ok, banner, parsed}.",
)
async def cap_netscan_banner(host: str, port: int,
                              timeout: float = 3.0,
                              trace_id=None) -> Dict:
    if not host or not port:
        return {"error": "host and port required"}
    ok, raw = await _grab_banner(host, int(port), float(timeout))
    parsed = {}
    if ok:
        # Try parse HTTP response line + first few headers
        head_lines = raw.splitlines()[:12]
        if head_lines and head_lines[0].startswith(("HTTP/1.", "HTTP/2")):
            parsed["http_status_line"] = head_lines[0]
            for line in head_lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    parsed[k.strip().lower()] = v.strip()
        # SSH banner
        elif head_lines and head_lines[0].lower().startswith("ssh-"):
            parsed["ssh_banner"] = head_lines[0]
        # FTP / SMTP / etc — first line is usually a status string
        elif head_lines:
            parsed["greeting"] = head_lines[0]
    # Persist banner on the port node
    try:
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            ip = host
        await _aux_run(
            """
            MERGE (p:NetPort {id:$pid})
            SET p.banner=$b, p.banner_ts=$ts
            """,
            pid=f"port:{ip}:{int(port)}", b=raw[:800], ts=now_iso(),
        )
    except Exception:
        pass
    return {"host": host, "port": int(port), "ok": ok,
            "banner": raw[:1000], "parsed": parsed}


# ═════════════════════════════════════════════════════════════════════════════
# 5.  TLS CERT INSPECT
# ═════════════════════════════════════════════════════════════════════════════
def _tls_inspect_sync(host: str, port: int, timeout: float) -> Dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                version = ss.version()
                cipher = ss.cipher()
        # Convert tuple-of-tuple cert to dict-ish
        subject = {}
        for rdn in cert.get("subject", ()):
            for k, v in rdn:
                subject[k] = v
        issuer = {}
        for rdn in cert.get("issuer", ()):
            for k, v in rdn:
                issuer[k] = v
        san = []
        for typ, val in cert.get("subjectAltName", ()) or ():
            san.append(f"{typ}:{val}")
        return {
            "ok": True,
            "subject": subject,
            "issuer": issuer,
            "san": san,
            "not_before": cert.get("notBefore"),
            "not_after": cert.get("notAfter"),
            "version": version,
            "cipher": list(cipher) if cipher else None,
            "serial": cert.get("serialNumber"),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@capability(
    "netscan.target.tls",
    http_method="POST", http_path="/netscan/target/tls",
    http_tags=["netscan"],
    description="Inspect the TLS certificate of a remote service. "
                "Input: host (str!), port (int=443), timeout (float=5). "
                "Output: {ok, subject, issuer, san, not_before, not_after, "
                "version, cipher}.",
)
async def cap_netscan_tls(host: str, port: int = 443,
                           timeout: float = 5.0,
                           trace_id=None) -> Dict:
    if not host:
        return {"error": "host required"}
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, _tls_inspect_sync, host, int(port), float(timeout)
    )
    # Persist a quick summary on the port node, if any
    if res.get("ok"):
        try:
            try:
                ip = socket.gethostbyname(host)
            except Exception:
                ip = host
            cn = (res.get("subject") or {}).get("commonName") or ""
            issuer_cn = (res.get("issuer") or {}).get("commonName") or ""
            await _aux_run(
                """
                MERGE (p:NetPort {id:$pid})
                SET p.tls_subject=$cn, p.tls_issuer=$ic,
                    p.tls_not_after=$na, p.tls_san=$san, p.tls_ts=$ts
                """,
                pid=f"port:{ip}:{int(port)}",
                cn=cn, ic=issuer_cn, na=res.get("not_after") or "",
                san=res.get("san") or [], ts=now_iso(),
            )
        except Exception:
            pass
    return {"host": host, "port": int(port), **res}


# ═════════════════════════════════════════════════════════════════════════════
# 6.  COMBINED FINGERPRINT (one-shot ports + banner + tech)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "netscan.target.fingerprint",
    http_method="POST", http_path="/netscan/target/fingerprint",
    http_tags=["netscan"],
    description="Combined fingerprint: scans common ports, grabs banners on "
                "open services, runs HTTP fingerprint on web ports, and "
                "inspects TLS on TLS-bearing ports. "
                "Input: host (str!), profile (str='quick' — one of quick / "
                "common / web / database / iot / ms / extended), "
                "banner (bool=true), tls (bool=true), tech (bool=true), "
                "timeout (float=2.0). "
                "Output: {host, ip, ports:[{port,hint,banner,tls,tech}]}.",
)
async def cap_netscan_fingerprint(host: str, profile: str = "quick",
                                   banner: bool = True, tls: bool = True,
                                   tech: bool = True,
                                   timeout: float = 2.0,
                                   trace_id=None) -> Dict:
    if not host:
        return {"error": "host required"}
    plist = _resolve_port_spec(profile)
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = host
    open_ports: List[int] = []
    sem = asyncio.Semaphore(128)

    async def probe(p: int):
        async with sem:
            ok = await _tcp_ping(host, p, timeout=timeout)
            if ok:
                open_ports.append(p)

    await asyncio.gather(*(probe(p) for p in plist))
    open_ports.sort()
    # Persist hosts + ports
    await _aux_upsert_nethost(ip,
                               hostname=(host if host != ip else ""),
                               open_ports=open_ports,
                               source="fingerprint")
    await _aux_upsert_ports(f"net:{ip}", ip, open_ports)

    http_probe = _ec_attr("_http_probe")
    out_ports: List[Dict] = []
    for p in open_ports:
        rec: Dict[str, Any] = {"port": p, "hint": _port_hint(p)}
        if banner:
            ok, b = await _grab_banner(host, p, timeout=3.0)
            if ok:
                rec["banner"] = b[:400]
        if tech and p in (80, 81, 443, 8000, 8008, 8080, 8081, 8088, 8443,
                           8888, 3000, 5000, 8096, 8123, 9090, 9200, 32400):
            scheme = "https" if p in (443, 8443) else "http"
            url = f"{scheme}://{host}" + ("" if p in (80, 443) else f":{p}")
            if http_probe:
                r = await http_probe(url, follow_redirects=True,
                                      timeout=timeout * 2.5)
                if r.get("ok"):
                    rec["http"] = {
                        "status": r.get("status"),
                        "title": r.get("title"),
                        "tech":  r.get("tech") or [],
                        "server": (r.get("headers") or {}).get("server", ""),
                    }
        if tls and p in (443, 465, 636, 993, 995, 5061, 5671, 8443, 9443):
            loop = asyncio.get_running_loop()
            r = await loop.run_in_executor(
                None, _tls_inspect_sync, host, p, 5.0)
            if r.get("ok"):
                rec["tls"] = {
                    "subject": (r.get("subject") or {}).get("commonName", ""),
                    "issuer":  (r.get("issuer") or {}).get("commonName", ""),
                    "san":     r.get("san") or [],
                    "not_after": r.get("not_after"),
                }
        out_ports.append(rec)
    await emit_event({"type": "netscan.target.fingerprint.done",
                      "host": host, "open": len(open_ports)})
    return {"host": host, "ip": ip, "ports": out_ports,
            "open_count": len(open_ports), "scanned": len(plist)}


# ═════════════════════════════════════════════════════════════════════════════
# 7.  TRACEROUTE
# ═════════════════════════════════════════════════════════════════════════════
_TRACERT_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(?:([\d\.\:a-fA-F]+)|\*)\s*"
    r"(?:\(([\d\.\:a-fA-F]+)\))?"
    r"(?:\s+([\d\.]+)\s*ms)?"
)


async def _run_traceroute(target: str, max_hops: int = 20,
                          timeout: int = 30) -> Tuple[bool, str, str]:
    """Run system traceroute / tracert. Returns (ok, stdout, error)."""
    runner = _ec_attr("_run_local")
    is_win = os.name == "nt" or sys.platform.startswith("win")
    if is_win:
        argv = ["tracert", "-d", "-h", str(max_hops), "-w", "1500", target]
    else:
        # Prefer -I (ICMP) to avoid root-only UDP, but fall back to default
        argv = ["traceroute", "-n", "-I", "-w", "2", "-q", "1",
                "-m", str(max_hops), target]
    if runner:
        try:
            r = await runner(argv, timeout=timeout)
            if r.get("ok"):
                return True, r.get("stdout", ""), ""
            # Fallback to udp variant
            if not is_win:
                r2 = await runner(
                    ["traceroute", "-n", "-w", "2", "-q", "1",
                     "-m", str(max_hops), target],
                    timeout=timeout,
                )
                if r2.get("ok"):
                    return True, r2.get("stdout", ""), ""
                return False, "", r2.get("stderr") or r2.get("error") or "failed"
            return False, "", r.get("stderr") or r.get("error") or "failed"
        except Exception as e:
            return False, "", str(e)
    # Pure asyncio fallback (no _run_local helper)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return True, (out or b"").decode("utf-8", "replace"), ""
        return False, "", (err or b"").decode("utf-8", "replace")
    except Exception as e:
        return False, "", str(e)


def _parse_traceroute(text: str) -> List[Dict]:
    hops: List[Dict] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # Skip the header line
        if line.lower().startswith(("traceroute", "tracing route")):
            continue
        m = _TRACERT_LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        # Either group 2 (linux: just an IP) or group 3 (windows: in parens)
        ip = m.group(2) or m.group(3) or ""
        if ip == "*":
            ip = ""
        # Latency: very rough — just first ms value we see on the line
        ms_match = re.search(r"(\d+\.\d+|\d+)\s*ms", line)
        latency = float(ms_match.group(1)) if ms_match else None
        hops.append({"hop": idx, "ip": ip, "latency_ms": latency,
                      "raw": line.strip()})
    return hops


@capability(
    "netscan.target.traceroute",
    http_method="POST", http_path="/netscan/target/traceroute",
    http_tags=["netscan"],
    description="Run a traceroute (or Windows tracert) to a target and "
                "persist each hop as a :NetHop node connected with "
                ":ROUTES_TO edges, plus :NetHost stubs for any responding "
                "hop IP. "
                "Input: target (str!), max_hops (int=20), timeout (int=30). "
                "Output: {target, hops:[{hop,ip,latency_ms}], elapsed_ms}.",
)
async def cap_netscan_traceroute(target: str, max_hops: int = 20,
                                  timeout: int = 30,
                                  trace_id=None) -> Dict:
    if not target:
        return {"error": "target required"}

    # Sanitise: extract bare hostname/IP from URLs, strip whitespace
    target = target.strip()
    if "://" in target:
        # User passed a URL like http://192.168.1.1 — extract the host part
        try:
            from urllib.parse import urlparse
            parsed = urlparse(target)
            target = parsed.hostname or parsed.netloc or target
        except Exception:
            pass
    # Strip any trailing path, port, or query
    target = target.split("/")[0].split("?")[0].split("#")[0]
    if ":" in target and not target.startswith("["):
        # host:port — drop the port
        target = target.rsplit(":", 1)[0]
    target = target.strip()
    if not target:
        return {"error": "could not extract host from target"}

    t0 = time.monotonic()
    ok, out, err = await _run_traceroute(target, max_hops=int(max_hops),
                                          timeout=int(timeout))
    elapsed = round((time.monotonic() - t0) * 1000)
    if not ok and not out:
        return {"error": err or "traceroute failed",
                "target": target, "hops": [], "elapsed_ms": elapsed}
    hops = _parse_traceroute(out)

    # Resolve final target to an IP (for the chain end)
    try:
        final_ip = socket.gethostbyname(target)
    except Exception:
        final_ip = target

    # Persist as :NetHop chain
    trace_id_str = str(uuid.uuid4())[:8]
    prev_id = None
    for h in hops:
        ip = h.get("ip") or f"hop:{trace_id_str}:{h['hop']}"
        hop_id = f"hop:{trace_id_str}:{h['hop']}"
        await _aux_run(
            """
            MERGE (n:NetHop {id:$id})
            SET n.hop=$hop, n.ip=$ip, n.latency_ms=$lat, n.target=$tg,
                n.trace_id=$tid, n.source='traceroute',
                n.name=$name, n.label=$label, n.updated_at=$ts
            """,
            id=hop_id, hop=h["hop"], ip=ip,
            lat=h.get("latency_ms"), tg=target, tid=trace_id_str,
            name=f"hop {h['hop']}",
            label=f"#{h['hop']} {ip}" if ip else f"#{h['hop']} *",
            ts=now_iso(),
        )
        # Promote responding hop IPs to NetHost stubs (so SSH actions work)
        if h.get("ip"):
            await _aux_upsert_nethost(
                h["ip"], hostname="",
                open_ports=[], source="traceroute",
                extra={"role": "transit-hop"},
            )
            await _aux_run(
                "MATCH (n:NetHop {id:$hid}), (h:NetHost {id:$nid}) "
                "MERGE (n)-[:RESOLVED_TO]->(h)",
                hid=hop_id, nid=f"net:{h['ip']}",
            )
        if prev_id:
            await _aux_run(
                "MATCH (a:NetHop {id:$a}), (b:NetHop {id:$b}) "
                "MERGE (a)-[:ROUTES_TO]->(b)",
                a=prev_id, b=hop_id,
            )
        prev_id = hop_id

    # Connect last hop to final target
    if prev_id:
        await _aux_upsert_nethost(final_ip, hostname=(target if target != final_ip else ""),
                                   open_ports=[], source="traceroute")
        await _aux_run(
            "MATCH (n:NetHop {id:$hid}), (h:NetHost {id:$nid}) "
            "MERGE (n)-[:ROUTES_TO]->(h)",
            hid=prev_id, nid=f"net:{final_ip}",
        )

    # ── Cross-link: connect to existing Website/WebEndpoint nodes that
    # share this hostname, so traceroute results join up with web scans.
    # Also link to any NetHost that already has this hostname but a
    # different IP (e.g. from a previous scan before DNS changed).
    if target != final_ip:
        # Link Website nodes (id = "site:http(s)://hostname")
        for scheme in ("http", "https"):
            site_id = f"site:{scheme}://{target}"
            await _aux_run(
                "MATCH (s:Website {id:$sid}), (h:NetHost {id:$hid}) "
                "MERGE (s)-[:RESOLVES_TO]->(h)",
                sid=site_id, hid=f"net:{final_ip}",
            )
        # Link any NetHost that has this hostname but different IP
        await _aux_run(
            "MATCH (h1:NetHost {id:$hid}), (h2:NetHost) "
            "WHERE h2.hostname = $hostname AND h2.id <> $hid "
            "MERGE (h1)-[:SAME_HOST]->(h2)",
            hid=f"net:{final_ip}", hostname=target,
        )
    # Also set hostname on the NetHost if it was blank
    if target != final_ip:
        await _aux_run(
            "MATCH (h:NetHost {id:$hid}) "
            "SET h.hostname = CASE WHEN h.hostname IS NULL OR h.hostname = '' "
            "THEN $hn ELSE h.hostname END",
            hid=f"net:{final_ip}", hn=target,
        )

    await emit_event({"type": "netscan.traceroute.done",
                      "target": target, "hops": len(hops)})
    return {"target": target, "final_ip": final_ip, "hops": hops,
            "elapsed_ms": elapsed, "trace_id": trace_id_str,
            "raw": out[:4000],
            "hostname": target if target != final_ip else ""}


# ═════════════════════════════════════════════════════════════════════════════
# 8.  GOOGLE-DORK SEARCH (DuckDuckGo HTML, no API key)
# ═════════════════════════════════════════════════════════════════════════════
DORK_PRESETS = {
    "exposed_files":     'intext:"index of /" "parent directory"',
    "config_files":      'ext:env OR ext:conf OR ext:cnf OR ext:ini',
    "sql_dumps":         'ext:sql intext:"INSERT INTO" -github.com',
    "open_directories":  'intitle:"index of" -intext:"github"',
    "login_pages":       'inurl:login OR inurl:signin OR inurl:admin',
    "phpinfo":           'intitle:"phpinfo()" "PHP Version"',
    "wordpress_admin":   'inurl:/wp-admin/ OR inurl:/wp-login.php',
    "exposed_git":       'inurl:/.git/HEAD',
    "exposed_env":       'inurl:/.env intext:DB_PASSWORD',
    "swagger_docs":      'inurl:swagger OR inurl:api-docs',
    "open_s3":           'site:s3.amazonaws.com',
    "jenkins":           'intitle:"Dashboard [Jenkins]"',
    "grafana":           'intitle:"Grafana" inurl:/login',
    "kibana":            'intitle:"Kibana" "kbn-version"',
    "iot_cameras":       'intitle:"Network Camera" inurl:view/index',
}


async def _ddg_search(query: str, max_results: int = 25,
                       timeout: float = 12.0) -> List[Dict]:
    """DuckDuckGo HTML search — no API key. Returns list of {title,url,snippet}."""
    base = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    out: List[Dict] = []
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                      follow_redirects=True) as c:
            r = await c.post(base, data={"q": query, "kl": "us-en"})
            html = r.text
    except Exception as e:
        log.debug("DDG search failed: %s", e)
        return []
    # Parse anchors of class result__a
    anchor_re = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.I | re.S,
    )
    snippet_re = re.compile(
        r'class="result__snippet"[^>]*>(.*?)</a>', re.I | re.S,
    )
    snippets = [re.sub(r"<[^>]+>", "", s).strip()
                for s in snippet_re.findall(html)]
    for i, m in enumerate(anchor_re.finditer(html)):
        if len(out) >= max_results:
            break
        href, title_html = m.group(1), m.group(2)
        # DDG wraps results in a redirect — pull out uddg= param if present
        clean_url = href
        if href.startswith("//") or href.startswith("/l/"):
            try:
                qs = _urlparse.urlparse(
                    href if href.startswith("http") else "https:" + href
                )
                params = _urlparse.parse_qs(qs.query)
                if "uddg" in params:
                    clean_url = _urlparse.unquote(params["uddg"][0])
                else:
                    clean_url = "https:" + href if href.startswith("//") else href
            except Exception:
                pass
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet = snippets[i] if i < len(snippets) else ""
        out.append({"title": title, "url": clean_url, "snippet": snippet})
    return out


@capability(
    "netscan.dork.search",
    http_method="POST", http_path="/netscan/dork/search",
    http_tags=["netscan", "osint"],
    description="Run a Google-dork-style search (via DuckDuckGo HTML — no "
                "API key needed). Optional preset via `preset` overrides "
                "the raw query. "
                "Input: query (str), preset (str — e.g. exposed_env, "
                "open_directories, swagger_docs, jenkins, iot_cameras), "
                "site (str — limit to a domain), max_results (int=25). "
                "Output: {query, results:[{title,url,snippet,host}], "
                "presets:[…] (when no input)}.",
)
async def cap_netscan_dork(query: str = "", preset: str = "",
                            site: str = "",
                            max_results: int = 25,
                            trace_id=None) -> Dict:
    # No input — just list available presets
    if not query and not preset:
        return {"presets": list(DORK_PRESETS.keys()),
                "note": "Pass 'preset' or 'query'. Example: "
                        "preset=exposed_env, site=example.com"}
    q = query or DORK_PRESETS.get(preset, "")
    if not q:
        return {"error": f"unknown preset: {preset}",
                "available": list(DORK_PRESETS.keys())}
    if site:
        q = f"site:{site} {q}"
    results = await _ddg_search(q, max_results=int(max_results))
    enriched = []
    for r in results:
        host = ""
        try:
            host = _urlparse.urlparse(r["url"]).netloc
        except Exception:
            pass
        enriched.append({**r, "host": host})
    await emit_event({"type": "netscan.dork.done",
                      "query": q, "count": len(enriched)})
    return {"query": q, "preset": preset, "site": site,
            "results": enriched, "count": len(enriched)}


@capability(
    "netscan.dork.targeted",
    http_method="POST", http_path="/netscan/dork/targeted",
    http_tags=["netscan", "osint"],
    description="Run a dork search and feed each result URL through HTTP "
                "fingerprinting, persisting :Website nodes to the graph. "
                "Useful for OSINT: find exposed Grafana/Jenkins/Swagger "
                "etc. and graph them. "
                "Input: query (str) | preset (str), site (str — limit), "
                "max_results (int=10), fingerprint (bool=true). "
                "Output: {query, sites:[{url,status,title,tech}]}.",
)
async def cap_netscan_dork_targeted(query: str = "", preset: str = "",
                                     site: str = "",
                                     max_results: int = 10,
                                     fingerprint: bool = True,
                                     trace_id=None) -> Dict:
    base = await cap_netscan_dork(
        query=query, preset=preset, site=site,
        max_results=max_results, trace_id=trace_id,
    )
    if "error" in base:
        return base
    http_probe = _ec_attr("_http_probe")
    site_id_fn = _ec_attr("_site_id")
    sites: List[Dict] = []
    for r in base.get("results", []):
        url = r.get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        rec = {**r}
        if fingerprint and http_probe and site_id_fn:
            try:
                p = await http_probe(url, follow_redirects=True, timeout=8.0)
                if p.get("ok"):
                    rec.update({
                        "status": p.get("status"),
                        "title":  p.get("title"),
                        "tech":   p.get("tech") or [],
                        "server": (p.get("headers") or {}).get("server", ""),
                    })
                    sid = site_id_fn(url)
                    parsed = _urlparse.urlparse(url)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    await _aux_run(
                        """
                        MERGE (s:Website {id:$id})
                        SET s.origin=$origin, s.url=$origin, s.title=$t,
                            s.tech=$tech, s.source='dork', s.dork=$q,
                            s.dork_snippet=$sn, s.updated_at=$ts
                        """,
                        id=sid, origin=origin,
                        t=(p.get("title") or "")[:200],
                        tech=p.get("tech") or [],
                        q=base.get("query", ""),
                        sn=(r.get("snippet") or "")[:200],
                        ts=now_iso(),
                    )
                else:
                    rec["error"] = p.get("error", "probe failed")
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
        sites.append(rec)
    return {"query": base.get("query"),
            "preset": base.get("preset"),
            "site": base.get("site"),
            "sites": sites, "count": len(sites)}


# ═════════════════════════════════════════════════════════════════════════════
# 9.  CLEAR-ALL / NEW-GRAPH BUTTON
# ═════════════════════════════════════════════════════════════════════════════
_NETSCAN_LABELS_FALLBACK = (
    "NetHost", "Subnet", "DockerHost", "Container",
    "PVECluster", "PVENode", "PVEGuest",
    "K8sCluster", "K8sNode", "K8sPod",
    "Website", "WebEndpoint",
    "NetPort", "NetHop",
)


@capability(
    "netscan.graph.clear_all",
    http_method="POST", http_path="/netscan/graph/clear_all",
    http_tags=["netscan"],
    description="Wipe the entire network-scan aux graph (every node Vera "
                "discovered or scanned). Use this for the 'new graph' "
                "button. "
                "Input: confirm (bool=true). "
                "Output: {deleted, labels_cleared}.",
)
async def cap_netscan_graph_clear_all(confirm: bool = True,
                                       trace_id=None) -> Dict:
    if not confirm:
        return {"error": "must pass confirm=true"}
    labels = _ec_attr("_NETSCAN_LABELS") or _NETSCAN_LABELS_FALLBACK
    labels = list(labels) + ["NetHop"]   # ensure NetHop included
    rows = await _aux_run(
        "MATCH (n) WHERE ANY(l IN labels(n) WHERE l IN $labels) "
        "WITH n DETACH DELETE n RETURN count(*) AS c",
        labels=labels,
    )
    deleted = (rows[0].get("c") if rows else 0) or 0
    await emit_event({"type": "netscan.graph.cleared", "deleted": deleted})
    return {"deleted": deleted, "labels_cleared": labels}


# ═════════════════════════════════════════════════════════════════════════════
# 10.  GRAPH-AWARE NetPort label patch (read side)
#
# `cap_netscan_graph` in exec_capabilities.py picks `hostname or name or
# label or ip` as the cytoscape label. With our `name=str(port)` upsert
# fix above, NetPort nodes will already get the right label. But if the
# upstream module is updated, we leave a runtime-side fallback in case
# any old NetPort nodes still lack a name — by patching the cytoscape
# graph response.
# ═════════════════════════════════════════════════════════════════════════════
def _install_graph_label_postfix() -> None:
    """Wrap cap_netscan_graph so any NetPort nodes are relabeled to port#."""
    m = _exec_mod()
    if not m:
        return
    orig = getattr(m, "cap_netscan_graph", None)
    if not orig:
        return
    if getattr(orig, "_vera_port_label_wrapped", False):
        return  # already wrapped

    async def wrapped(trace_id=None) -> Dict:
        out = await orig(trace_id=trace_id)
        if not isinstance(out, dict):
            return out
        for node in out.get("nodes") or []:
            d = node.get("data") or {}
            if d.get("type") == "NetPort":
                # Prefer explicit port number
                port = d.get("port") or d.get("name")
                hint = d.get("hint") or ""
                if port:
                    d["label"] = (
                        f"{port}/tcp" + (f" {hint}" if hint else "")
                    )
            elif d.get("type") == "NetHop":
                hop = d.get("hop")
                ip = d.get("ip") or "*"
                if hop is not None:
                    d["label"] = f"#{hop} {ip}"
        return out

    wrapped._vera_port_label_wrapped = True   # type: ignore
    setattr(m, "cap_netscan_graph", wrapped)
    # Re-register the FastAPI route endpoint (but NOT rt.app — that expects
    # an ASGI callable, not a plain async function)
    for rt in list(APP.routes):
        if getattr(rt, "path", "") == "/netscan/graph":
            try:
                rt.endpoint = wrapped
            except Exception:
                pass
    log.info("netscan_extras: graph label post-fix installed")


# ═════════════════════════════════════════════════════════════════════════════
# Apply patches at import time
# ═════════════════════════════════════════════════════════════════════════════
def _maybe_install_patches():
    try:
        _install_port_label_fix()
    except Exception as e:
        log.warning("port label fix failed: %s", e)
    try:
        _install_graph_label_postfix()
    except Exception as e:
        log.warning("graph label post-fix failed: %s", e)


_maybe_install_patches()

# No delayed install needed — extras are in the same file now, so
# all originals are guaranteed to exist at this point.


log.info("netscan_extras loaded — added: lan/stream, ports/stream, web/stream, "
         "banner, tls, fingerprint, traceroute, dork.search, dork.targeted, "
         "graph.clear_all")