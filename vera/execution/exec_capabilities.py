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

from Vera.vera.config import cfg
from Vera.vera.capability_orchestration import (
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

# SSH host credentials are sealed with Fernet (the shared vault used by Proxmox /
# accounts / email — keyed by VERA_SECRET_KEY, never stored next to ciphertext).
# Legacy records used a weak XOR "obfuscation"; the `_deobfuscate` reader still
# decodes those transparently, and they upgrade to Fernet on the next save. The
# field names (`password_obf` / `passphrase_obf`) are unchanged so every existing
# call site keeps working — only the on-disk encoding changed.
from Vera.vera.security import secrets as vsecrets

# Legacy XOR key — retained ONLY to decode records written before the Fernet
# migration. Never used for new writes.
_OBF = "vera-exec-host-store-v1"


def _xor_obfuscate(s: str) -> str:
    import base64
    b = s.encode("utf-8")
    k = _OBF.encode("utf-8")
    out = bytes(c ^ k[i % len(k)] for i, c in enumerate(b))
    return base64.b64encode(out).decode("ascii")


def _xor_deobfuscate(s: str) -> str:
    import base64
    try:
        raw = base64.b64decode(s.encode("ascii"))
        k = _OBF.encode("utf-8")
        return bytes(c ^ k[i % len(k)] for i, c in enumerate(raw)).decode("utf-8")
    except Exception as _obf_err:
        log.warning(
            "_xor_deobfuscate: stored credential is corrupt or was saved with a "
            "different key — returning empty string. Re-save the host "
            "credential to fix this. (%s)", _obf_err
        )
        return ""


def _obfuscate(s: str) -> str:
    """Seal a secret for storage (Fernet). Falls back to legacy XOR only if the
    Fernet vault is unavailable, so saving a host never hard-fails."""
    if not s:
        return ""
    try:
        return vsecrets.seal(s)            # -> "fernet:…"
    except Exception as e:
        log.warning("SSH cred: Fernet seal unavailable (%s) — using legacy XOR. "
                    "Install 'cryptography' / set VERA_SECRET_KEY for real "
                    "encryption.", e)
        return _xor_obfuscate(s)


def _deobfuscate(s: str) -> str:
    """Open a stored secret. Fernet tokens are decrypted; legacy XOR values are
    decoded for backward compatibility."""
    if not s:
        return ""
    if vsecrets.is_sealed(s):
        return vsecrets.open_secret(s)
    return _xor_deobfuscate(s)


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


_HOSTS_FILE_LAST: List[str] = [""]  # serialized blob last written — skip redundant rewrites


def _save_hosts_file(hosts: Dict[str, dict]) -> None:
    try:
        blob = json.dumps(hosts, indent=2, sort_keys=True)
        # _load_hosts() re-saves this cache on EVERY host resolve (every ssh/docker
        # call, and the monitor's per-host docker.ps tick), but the host list almost
        # never changes — a caught stall was a >1.2s write_text on the loop against
        # the busy home volume. Skip the write (and chmod) when nothing changed.
        if blob == _HOSTS_FILE_LAST[0] and _SSH_STORE_PATH.exists():
            return
        _SSH_STORE_PATH.write_text(blob, encoding="utf-8")
        _HOSTS_FILE_LAST[0] = blob
        try:
            os.chmod(_SSH_STORE_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        log.error("Failed to persist SSH host store file: %s", e)


# Resolving a host record happens on every ssh/docker call (the monitor polls
# docker.ps per host), so _load_hosts() is cached. Vera can run as a CLUSTER of
# instances sharing one Neo4j+Redis backend, so a host added/edited/deleted on
# ANOTHER node must be picked up here too: every mutation bumps a shared Redis
# counter (_HOSTS_VER_KEY) and we reload when it changes → near-instant cross-node
# propagation. A time-TTL (VERA_SSH_HOSTS_TTL) backstops the version check (bounds
# staleness if Redis is down or a bump is ever missed) and is the sole freshness
# signal when Redis is unreachable. Local mutations also invalidate directly.
_HOSTS_CACHE: Dict[str, Any] = {}   # {"data": {id: rec}, "ver": <val|None>}
_HOSTS_CACHE_TS = [0.0]
_HOSTS_CACHE_TTL = float(os.getenv("VERA_SSH_HOSTS_TTL", "10") or 10)
_HOSTS_VER_KEY = "vera:sshhosts:ver"


def _orch_redis():
    """Live handle to the cluster-shared Redis (set at runtime in lifespan)."""
    m = sys.modules.get("Vera.vera.capability_orchestration")
    return getattr(m, "REDIS", None) if m else None


def _invalidate_hosts_cache() -> None:
    _HOSTS_CACHE_TS[0] = 0.0


async def _bump_hosts_ver() -> None:
    """Signal every cluster node (incl. self) to reload the host store."""
    r = _orch_redis()
    if r is not None:
        try: await r.incr(_HOSTS_VER_KEY)
        except Exception: pass


def _neo_available() -> bool:
    fn = _fabric_neo()
    return bool(fn and getattr(fn, "available", False))


async def _load_hosts() -> Dict[str, dict]:
    """Version-aware, TTL-backstopped cache over _load_hosts_uncached (see note
    above). Returns a shallow copy so callers that mutate the dict in place
    (save/delete) can't corrupt the cached snapshot; per-host records are tiny so
    the copy is free."""
    r = _orch_redis()
    ver = None
    redis_ok = False
    if r is not None:
        try:
            ver = await r.get(_HOSTS_VER_KEY)   # sub-ms, non-blocking; None until first mutation
            redis_ok = True
        except Exception:
            redis_ok = False
    data = _HOSTS_CACHE.get("data")
    within_ttl = _HOSTS_CACHE_TS[0] and (time.monotonic() - _HOSTS_CACHE_TS[0]) < _HOSTS_CACHE_TTL
    # Serve the cache when the TTL hasn't lapsed AND (Redis is down → best-effort,
    # or the shared version is unchanged → cluster-confirmed no edits anywhere).
    if data is not None and within_ttl and (not redis_ok or ver == _HOSTS_CACHE.get("ver")):
        return dict(data)
    loaded = await _load_hosts_uncached()
    _HOSTS_CACHE["data"] = loaded
    _HOSTS_CACHE["ver"] = ver
    _HOSTS_CACHE_TS[0] = time.monotonic()
    return dict(loaded)


async def _load_hosts_uncached() -> Dict[str, dict]:
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
            # Cache to file so we still work if Neo4j goes down later. Blocking
            # file I/O is pushed off the loop — the unchanged-skip in
            # _save_hosts_file makes the common case a no-op, but the first/changed
            # write and the fallback reads must not stall the event loop.
            if out:
                try: await asyncio.to_thread(_save_hosts_file, out)
                except Exception: pass
            else:
                # Neo4j empty — lift whatever's in the JSON cache into Neo4j
                file_hosts = await asyncio.to_thread(_load_hosts_file)
                if file_hosts:
                    for rec in file_hosts.values():
                        try: await _persist_host_neo(rec)
                        except Exception: pass
                    return file_hosts
            return out
        except Exception as e:
            log.warning("SshHost Neo4j read failed, falling back to file: %s", e)
    return await asyncio.to_thread(_load_hosts_file)


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
    _invalidate_hosts_cache()
    await _bump_hosts_ver()


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
    _invalidate_hosts_cache()
    await _bump_hosts_ver()


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
_DEFAULT_TIMEOUT = 60          # seconds — short probes (ssh reach, port/banner, ping)
# Interactive shell/code execution (bash/ps/python/…) routinely runs things that
# legitimately take minutes: a network scan, a package install, a build, a long
# query. 60 s was far too tight and produced spurious "timeout after 60s" (and,
# once clamped, 120 s) failures on perfectly healthy commands. Give these caps a
# generous default and let the agent raise it further per-call. Env-tunable.
_EXEC_DEFAULT_TIMEOUT = int(os.getenv("VERA_EXEC_TIMEOUT", "600") or 600)   # 10 min
_MAX_OUTPUT     = 1_000_000    # 1 MB captured output per stream


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in per-session sandbox routing (Phase 6). When a session has an ACTIVE
# sandbox (session_sandbox_capabilities), its shell/code runs INSIDE the
# container instead of on this host. These are no-ops (return None) whenever the
# module isn't loaded, no session_id is supplied, or the session has no sandbox.
# ─────────────────────────────────────────────────────────────────────────────
def _sandbox_mod():
    m = sys.modules.get("session_sandbox_capabilities")
    if m is not None and hasattr(m, "route_shell"):
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("session_sandbox_capabilities") \
                and hasattr(mod, "route_shell"):
            return mod
    return None


def _trigger_session_id() -> str:
    """Session id from the syslog trigger chain (set by chat.stream / the agentic
    loop). This is why a cap invoked from chat WITHOUT an explicit session_id still
    routes into that session's sandbox — the same mechanism ide.fs.* uses. Callers
    that HAVE a session_id should still pass it; this is the safety net."""
    try:
        sl = sys.modules.get("syslog")
        if sl is not None:
            return (sl.get_trigger_chain() or {}).get("session_id", "") or ""
    except Exception:
        pass
    return ""


async def _route_session_shell(session_id: str, command: str, timeout: int):
    session_id = session_id or _trigger_session_id()
    if not session_id:
        return None
    sb = _sandbox_mod()
    if sb is None:
        return None
    try:
        return await sb.route_shell(session_id, command, timeout)
    except Exception as e:
        log.debug("session sandbox route_shell failed (running on host): %s", e)
        return None


async def _route_session_code(session_id: str, language: str, code: str,
                              path: str, stdin: str, timeout: int, args):
    # NOTE: path-based runs ARE routed now — a file written into the container
    # (via routed ide.fs.write / write_artifact / code.save) must be run there
    # too, not on a host that can't see it. The sandbox module resolves path.
    session_id = session_id or _trigger_session_id()
    if not session_id:
        return None
    sb = _sandbox_mod()
    if sb is None:
        return None
    try:
        return await sb.route_code(session_id, language, code, path=path,
                                   stdin=stdin, timeout=timeout, args=args)
    except Exception as e:
        log.debug("session sandbox route_code failed (running on host): %s", e)
        return None


async def _route_session_shell_argv(session_id: str, command: str, shell: str = "sh"):
    """Streaming twin of _route_session_shell: returns the host-side argv that
    runs `command` INSIDE the session sandbox (docker exec …), or None → host.
    `shell` picks the in-container interpreter ("sh" or "pwsh" for PowerShell)."""
    session_id = session_id or _trigger_session_id()
    if not session_id:
        return None
    sb = _sandbox_mod()
    if sb is None or not hasattr(sb, "route_shell_argv"):
        return None
    try:
        return await sb.route_shell_argv(session_id, command, shell=shell)
    except Exception as e:
        log.debug("session sandbox route_shell_argv failed (host): %s", e)
        return None


async def _route_session_shell_ps(session_id: str, command: str, timeout: int):
    """PowerShell twin of _route_session_shell — runs `command` via pwsh INSIDE the
    session sandbox (needs a pwsh-capable base image), or None → host."""
    session_id = session_id or _trigger_session_id()
    if not session_id:
        return None
    sb = _sandbox_mod()
    if sb is None or not hasattr(sb, "route_shell"):
        return None
    try:
        return await sb.route_shell(session_id, command, timeout, shell="pwsh")
    except Exception as e:
        log.debug("session sandbox route_shell(pwsh) failed (host): %s", e)
        return None


async def _route_session_code_argv(session_id: str, language: str, code: str,
                                   args=None):
    """Streaming twin of _route_session_code: returns the host-side argv that
    runs `code` INSIDE the session sandbox, or None → host."""
    session_id = session_id or _trigger_session_id()
    if not session_id:
        return None
    sb = _sandbox_mod()
    if sb is None or not hasattr(sb, "route_code_argv"):
        return None
    try:
        return await sb.route_code_argv(session_id, language, code, args=args)
    except Exception as e:
        log.debug("session sandbox route_code_argv failed (host): %s", e)
        return None


async def _run_local(argv: List[str], stdin_data: str = "",
                     timeout: int = _DEFAULT_TIMEOUT,
                     cwd: Optional[str] = None,
                     env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    t0 = time.monotonic()
    # Spawning from this large-RSS server via the default fork() path copies the
    # parent's page tables while holding the GIL — so the loop freezes for the
    # whole copy and NO thread offload can help (fork holds the GIL). A caught
    # stall was 1.16s just to spawn `docker exec` for a workspace dirty-check.
    # close_fds=False lets CPython take the posix_spawn (vfork) path instead,
    # which shares the address space and never copies page tables, so spawn cost
    # stops scaling with process size. Safe: since PEP 446 (3.4+) every fd Python
    # opens is non-inheritable (O_CLOEXEC) by default, so no server socket / DB
    # handle leaks into the child. (posix_spawn is skipped when cwd is set on
    # Python <3.13; those callers keep the old path.) Opt out with VERA_FAST_SPAWN=0.
    _spawn_kw: Dict[str, Any] = {}
    if os.getenv("VERA_FAST_SPAWN", "1") != "0":
        _spawn_kw["close_fds"] = False
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, **(env or {})} if env else None,
            **_spawn_kw,
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
    description="Run a bash command on the local machine and capture output. "
                "WHEN TO USE: shell commands, file operations, system queries, running scripts, "
                "installing packages, checking disk/process/network state on the local host. "
                "Returns immediately when complete (not LONG-RUNNING). "
                "Check ok and rc in response — if ok=False, stderr contains the error. "
                "Input: command (str!), timeout (int sec, default 600 = 10 min), cwd (str — working "
                "directory). Pass a GENEROUS timeout for commands that legitimately take a while "
                "(network scans like nmap, package installs, builds, big greps) — don't let a slow "
                "-but-healthy command trip the timeout. "
                "Output: {ok, rc, stdout, stderr, elapsed_ms}. "
                "Use exec.bash.stream for live streaming output of long-running commands.",
)
async def cap_bash_run(command: str, timeout: int = _EXEC_DEFAULT_TIMEOUT,
                       cwd: str = "", session_id: str = "", trace_id=None) -> Dict:
    timeout = parse_timeout(timeout)
    if not command.strip():
        return {"ok": False, "error": "empty command", "rc": -1,
                "stdout": "", "stderr": ""}
    # Opt-in per-session sandbox: if this session has an ACTIVE sandbox, the
    # command runs INSIDE its container instead of on the host (no-op otherwise).
    routed = await _route_session_shell(session_id, command, timeout)
    if routed is not None:
        return routed
    ok, reason = _sandbox_check(command, cwd=cwd)
    if not ok:
        await emit_event({"type": "exec.sandbox.blocked", "shell": "bash",
                          "reason": reason})
        return {"ok": False, "error": f"sandbox: {reason}", "blocked": True,
                "rc": -1, "stdout": "", "stderr": ""}
    timeout = _sandbox_clamp_timeout(timeout)
    cwd = _sandbox_effective_cwd(cwd)
    # Use bash -lc so aliases, PATH, env are loaded
    bash_bin = os.getenv("VERA_BASH_BIN", "/bin/bash")
    argv = [bash_bin, "-lc", command]
    return await _run_local(argv, timeout=timeout, cwd=cwd or None)


@capability(
    "exec.ps.run",
    http_method="POST", http_path="/exec/ps/run", http_tags=["exec"],
    description="Run a PowerShell command (captured). Uses 'pwsh' if available, "
                "falls back to 'powershell'. If the session has an ACTIVE sandbox it "
                "runs via pwsh INSIDE the container (needs a pwsh-capable base image). "
                "Input: command (str!), timeout (int sec, default 600 = 10 min), cwd (str), "
                "session_id (str). Pass a larger timeout for long commands (installs, builds). "
                "Output: {ok, rc, stdout, stderr, elapsed_ms}.",
)
async def cap_ps_run(command: str, timeout: int = _EXEC_DEFAULT_TIMEOUT,
                     cwd: str = "", session_id: str = "", trace_id=None) -> Dict:
    timeout = parse_timeout(timeout)
    if not command.strip():
        return {"ok": False, "error": "empty command", "rc": -1,
                "stdout": "", "stderr": ""}
    # Opt-in per-session sandbox: run via pwsh inside the container when active.
    routed = await _route_session_shell_ps(session_id, command, timeout)
    if routed is not None:
        return routed
    ok, reason = _sandbox_check(command, cwd=cwd)
    if not ok:
        await emit_event({"type": "exec.sandbox.blocked", "shell": "powershell",
                          "reason": reason})
        return {"ok": False, "error": f"sandbox: {reason}", "blocked": True,
                "rc": -1, "stdout": "", "stderr": ""}
    timeout = _sandbox_clamp_timeout(timeout)
    cwd = _sandbox_effective_cwd(cwd)
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
# EXEC SANDBOX  —  configurable allow/deny policy for local execution
# ─────────────────────────────────────────────────────────────────────────────
#
# A small, JSON-persisted policy that gates every *local* execution path
# (bash, powershell, and the code runners below). It is intentionally
# best-effort: it cannot fully jail an interpreter, but it blocks obviously
# destructive commands, restricts which filesystem roots a run may touch
# (by cwd) and which languages may run, and caps the timeout.
#
# Stored at ~/.vera_exec_sandbox.json (override via VERA_EXEC_SANDBOX). Read
# fresh on every call so edits via exec.sandbox.set take effect immediately.
# ─────────────────────────────────────────────────────────────────────────────
_SANDBOX_PATH = Path(os.getenv(
    "VERA_EXEC_SANDBOX",
    os.path.join(os.path.expanduser("~"), ".vera_exec_sandbox.json"),
))

# Shipped out-of-the-box: enabled with a starter blocklist of obviously
# destructive patterns. No path/language restriction by default so normal
# usage is unaffected — only the egregious stuff is denied until the operator
# tightens the policy via exec.sandbox.set.
_SAFE_BLOCKLIST = [
    r"rm\s+-rf?\s+(/|~|\$HOME)(\s|$)",          # rm -rf / | ~ | $HOME
    r"rm\s+-rf?\s+--no-preserve-root",
    r"\bmkfs\.[a-z0-9]+\b",                       # format a filesystem
    r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|vd|hd|mmcblk)", # overwrite a block device
    r">\s*/dev/(sd|nvme|vd|hd)[a-z]",            # redirect onto a raw disk
    r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b",
    r":\s*\(\s*\)\s*\{[^}]*\}\s*;\s*:",          # classic fork bomb :(){ :|:& };:
    r"\bchmod\s+-R?\s*0?777\s+/",                # chmod -R 777 /
    r"\bchown\s+-R\b[^\n]*\s+/(\s|$)",           # chown -R ... /
    r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh)\b",  # curl … | sh
    r"\bformat\s+[c-zC-Z]:",                      # windows format C:
    r"Remove-Item\b[^\n]*-Recurse[^\n]*-Force[^\n]*[A-Za-z]:\\(\s|$|\")",  # rm -rf on a drive root
    r"\bdel\b\s+/[sSqQfF]\b[^\n]*[A-Za-z]:\\",   # del /s /q C:\
]

# Where agent-generated files (code/documents/artifacts) land by default. The
# resolved per-run directory is ALWAYS treated as allowed by the sandbox (it's a
# controlled location), so the agent can write artifacts even under a strict
# allow_paths jail. deny_paths still override it.
_DEFAULT_ARTIFACT_ROOT = os.path.join(os.path.expanduser("~"), ".vera_artifacts")
_ARTIFACT_SCOPES = ("artifact", "session", "project", "workspace")

_DEFAULT_SANDBOX: Dict[str, Any] = {
    "enabled":           True,
    "languages":         [],            # [] = all allowed; else whitelist of lang ids
    "allow_paths":       [],            # [] = no restriction; else cwd must live under one
    "deny_paths":        [],            # cwd / command may never reference these roots
    "command_blocklist": list(_SAFE_BLOCKLIST),  # regex; any match → deny
    "command_allowlist": [],            # regex; if non-empty, command MUST match one
    "max_timeout":       0,             # 0 = no cap (seconds)
    "network":           True,          # informational hint (not enforced)
    "artifact_root":     "",            # "" = _DEFAULT_ARTIFACT_ROOT
    "artifact_scope":    "session",     # artifact | session | project | workspace
}


def _load_sandbox() -> Dict[str, Any]:
    """Return the active policy, merging the on-disk file over the defaults."""
    pol = dict(_DEFAULT_SANDBOX)
    try:
        if _SANDBOX_PATH.exists():
            raw = json.loads(_SANDBOX_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                pol.update({k: v for k, v in raw.items() if k in _DEFAULT_SANDBOX})
    except Exception as e:
        log.warning("exec sandbox config corrupt (%s) — using defaults", e)
    return pol


def _save_sandbox(pol: Dict[str, Any]) -> None:
    try:
        _SANDBOX_PATH.write_text(
            json.dumps(pol, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(_SANDBOX_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        log.error("Failed to persist exec sandbox config: %s", e)


def _norm_path(p: str) -> str:
    if not p:
        return ""
    try:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))
    except Exception:
        return p


def _path_within(child: str, parent: str) -> bool:
    if not child or not parent:
        return False
    c, p = _norm_path(child), _norm_path(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _sandbox_check(text: str, *, cwd: str = "", language: str = "") -> Tuple[bool, str]:
    """Gate a local execution. Returns (allowed, reason). reason is '' when ok.

    `text` is the command or source about to run. `cwd` is the requested working
    directory (may be empty). `language` is the resolved language id (or '' for
    raw shell)."""
    pol = _load_sandbox()
    if not pol.get("enabled"):
        return True, ""

    # 1. Language whitelist
    langs = pol.get("languages") or []
    if language and langs and language not in langs:
        return False, f"language '{language}' is not in the sandbox whitelist"

    # 2. Denied paths — match either the cwd or a literal mention in the command
    eff_cwd = _norm_path(cwd) if cwd else ""
    for deny in (pol.get("deny_paths") or []):
        dn = _norm_path(deny)
        if eff_cwd and _path_within(eff_cwd, dn):
            return False, f"cwd is inside denied path: {deny}"
        if dn and dn in (text or ""):
            return False, f"command references denied path: {deny}"

    # 3. Allowed paths — if set, cwd must live under one of them. The artifact
    #    root is always implicitly allowed (a controlled location for agent
    #    output); deny_paths above still override it.
    allow = list(pol.get("allow_paths") or [])
    if allow:
        allow.append(_artifact_root(pol))
        if eff_cwd and not any(_path_within(eff_cwd, a) for a in allow):
            return False, f"cwd '{cwd}' is outside the sandbox allow_paths"

    # 4. Blocklist — any regex match denies
    for pat in (pol.get("command_blocklist") or []):
        try:
            if re.search(pat, text or "", re.IGNORECASE):
                return False, f"command matches blocked pattern: {pat}"
        except re.error:
            continue

    # 5. Allowlist — if present, command must match at least one
    allowlist = pol.get("command_allowlist") or []
    if allowlist:
        if not any(_safe_search(pat, text) for pat in allowlist):
            return False, "command does not match any sandbox allowlist pattern"

    return True, ""


def _safe_search(pat: str, text: str) -> bool:
    try:
        return bool(re.search(pat, text or "", re.IGNORECASE))
    except re.error:
        return False


def _sandbox_clamp_timeout(timeout: int) -> int:
    pol = _load_sandbox()
    cap = int(pol.get("max_timeout") or 0)
    if cap > 0:
        return min(int(timeout), cap)
    return int(timeout)


def _sandbox_effective_cwd(cwd: str) -> str:
    """If allow_paths is set and no cwd was given, default cwd to the first
    allowed root so unspecified runs land inside the jail rather than the
    process cwd."""
    pol = _load_sandbox()
    if cwd:
        return cwd
    if pol.get("enabled") and (pol.get("allow_paths") or []):
        return pol["allow_paths"][0]
    return cwd


def _safe_seg(s: str) -> str:
    """Filesystem-safe single path segment (no separators / traversal)."""
    s = (s or "").strip().replace("\\", "_").replace("/", "_")
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s)
    s = s.strip("._") or "default"
    return s[:80]


def _artifact_root(pol: Optional[Dict[str, Any]] = None) -> str:
    pol = pol or _load_sandbox()
    return _norm_path(pol.get("artifact_root") or _DEFAULT_ARTIFACT_ROOT)


def artifact_dir(*, session_id: str = "", project: str = "", workspace: str = "",
                 artifact: str = "", create: bool = True) -> str:
    """Resolve the artifact directory for a run, per the sandbox's artifact_scope:
      • artifact  → <root>/<artifact or session>   (a per-output folder)
      • session   → <root>/session/<session_id>
      • project   → <root>/project/<project>
      • workspace → <root>/workspace/<workspace>
    The directory is created (when `create`) and is always sandbox-allowed."""
    pol = _load_sandbox()
    root = _artifact_root(pol)
    scope = (pol.get("artifact_scope") or "session").lower()
    if scope not in _ARTIFACT_SCOPES:
        scope = "session"
    if scope == "artifact":
        path = os.path.join(root, _safe_seg(artifact or session_id))
    elif scope == "project":
        path = os.path.join(root, "project", _safe_seg(project))
    elif scope == "workspace":
        path = os.path.join(root, "workspace", _safe_seg(workspace))
    else:  # session
        path = os.path.join(root, "session", _safe_seg(session_id))
    path = _norm_path(path)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            log.warning("could not create artifact dir %s: %s", path, e)
    return path


async def artifact_dir_async(*, session_id: str = "", project: str = "",
                             workspace: str = "", artifact: str = "",
                             create: bool = True) -> str:
    """Sandbox-aware artifact dir. When the session has an ACTIVE sandbox the
    artifact area IS the container's /workspace — where routed exec/code runs land
    and the per-session volume persists — so the path the agent is told to use, its
    forced run cwd, its code.save mirror, and its by-path runs all cohere in one
    place. Otherwise falls back to the host `artifact_dir()`. Prefer this over the
    sync `artifact_dir()` anywhere a session_id is in play."""
    session_id = session_id or _trigger_session_id()
    sb = _sandbox_mod()
    if session_id and sb is not None and hasattr(sb, "route_artifact_dir"):
        try:
            d = await sb.route_artifact_dir(session_id, create=create)
            if d:
                return d
        except Exception as e:
            log.debug("sandbox artifact_dir route failed (host): %s", e)
    return artifact_dir(session_id=session_id, project=project,
                        workspace=workspace, artifact=artifact, create=create)


async def write_artifact_file(*, relpath: str, content: str, session_id: str = "",
                              project: str = "", workspace: str = "") -> str:
    """Write `relpath` under the (sandbox-aware) artifact dir — INTO the container
    when the session is sandboxed (so the file is where its runs execute), else on
    the host. Returns the absolute path (container or host). Segments are
    sanitised; traversal outside the artifact dir is rejected (host path only)."""
    session_id = session_id or _trigger_session_id()
    rel = str(relpath or "").strip().strip("/\\")
    parts = [_safe_seg(p) for p in re.split(r"[\\/]+", rel)
             if p and p not in (".", "..")]
    if not parts:
        raise ValueError("invalid artifact filename")

    sb = _sandbox_mod()
    if session_id and sb is not None and hasattr(sb, "route_artifact_dir") \
            and hasattr(sb, "route_fs_write"):
        try:
            base = await sb.route_artifact_dir(session_id, create=True)
            if base:
                full = base.rstrip("/") + "/" + "/".join(parts)
                res = await sb.route_fs_write(session_id, full, content)
                if res is not None and not res.get("error"):
                    return full
        except Exception as e:
            log.debug("sandbox artifact write failed (host): %s", e)

    base = artifact_dir(session_id=session_id, project=project, workspace=workspace)
    target = _norm_path(os.path.join(base, *parts))
    if not _path_within(target, base):
        raise ValueError("path escapes the artifact directory")
    os.makedirs(os.path.dirname(target) or base, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return target


async def artifact_file_exists(session_id: str = "", relpath: str = "") -> Optional[bool]:
    """Does `relpath` actually exist in the session's EFFECTIVE artifact location —
    INSIDE the container when sandboxed (where its runs execute), else the host
    artifact dir? Mirrors write_artifact_file's routing so the answer means "is the
    file where a later exec/read would find it?". Accepts absolute container paths
    (…/workspace/x) or a path relative to the artifact dir.

    Returns True (exists), False (definitely missing), or None (could NOT be
    determined — no session, probe error). Callers MUST treat None as unknown and
    never as 'missing', so a broken probe can't fail an otherwise-good step."""
    session_id = session_id or _trigger_session_id()
    p = str(relpath or "").strip()
    if not p or "://" in p:
        return None
    pnorm = p.replace("\\", "/")
    is_bare = "/" not in pnorm                       # a plain filename, no directory
    base_name = os.path.basename(pnorm)
    sb = _sandbox_mod()
    # Sandboxed session → stat inside the container (its /workspace, not the host).
    if session_id and sb is not None:
        cpath = pnorm if pnorm.startswith("/") else (_WORKDIR.rstrip("/") + "/" + pnorm.lstrip("/"))
        try:
            hit: Optional[bool] = None
            if hasattr(sb, "route_fs_exists"):
                r = await sb.route_fs_exists(session_id, cpath)
                if r is not None:
                    hit = None if r.get("error") else bool(r.get("exists"))
            elif hasattr(sb, "route_fs_read"):       # older sandbox module: infer from read
                r = await sb.route_fs_read(session_id, cpath, max_bytes=1)
                if r is not None:
                    hit = None if (r.get("error") and "not found" not in str(r.get("error")).lower()) \
                        else (False if r.get("error") else True)
            if hit is True:
                return True
            # A BARE filename may legitimately live in a workspace SUBDIR the plan
            # chose — search the whole workspace before ruling it missing, so a real
            # file is never reported absent just because it isn't at the root.
            if hit is False and is_bare and base_name and hasattr(sb, "route_shell"):
                safe = re.sub(r"[^\w.\-]", "", base_name)
                if safe:
                    rr = await sb.route_shell(
                        session_id,
                        f"find /workspace -maxdepth 6 -name '{safe}' -print -quit 2>/dev/null", 20)
                    if rr is not None and (rr.get("stdout") or "").strip():
                        return True
            if hit is not None:
                return hit
            # sb present but no active sandbox for this session → fall through to host.
        except Exception as e:
            log.debug("artifact_file_exists sandbox probe failed for %s: %s", cpath, e)
            return None
    # Host artifact dir.
    try:
        base = artifact_dir(session_id=session_id)
        rel = re.sub(r"^/?workspace/", "", pnorm).lstrip("/")
        parts = [s for s in rel.split("/") if s and s not in (".", "..")]
        if not parts:
            return None
        target = _norm_path(os.path.join(base, *parts))
        if _path_within(target, base) and os.path.isfile(target):
            return True
        if is_bare and base_name:                    # search subdirs (bounded)
            seen = 0
            for root, _dirs, files in os.walk(base):
                if base_name in files:
                    return True
                seen += 1
                if seen > 2000:
                    break
        return False
    except Exception:
        return None


async def read_artifact_file(session_id: str = "", relpath: str = "",
                             max_bytes: int = 60000) -> Optional[str]:
    """Read `relpath` from the session's EFFECTIVE artifact location — inside the
    container when sandboxed, else the host artifact dir. The read sibling of
    write_artifact_file / artifact_file_exists, routed identically so it returns
    the same bytes a later exec/read in the run would see.

    Returns the text (truncated to `max_bytes`), or None when it could not be read
    (no such file, probe unavailable, binary). Callers must treat None as "unknown",
    never as "empty"."""
    session_id = session_id or _trigger_session_id()
    p = str(relpath or "").strip()
    if not p or "://" in p:
        return None
    pnorm = p.replace("\\", "/")
    sb = _sandbox_mod()
    if session_id and sb is not None and hasattr(sb, "route_fs_read"):
        cpath = pnorm if pnorm.startswith("/") else (_WORKDIR.rstrip("/") + "/" + pnorm.lstrip("/"))
        try:
            r = await sb.route_fs_read(session_id, cpath, max_bytes=max_bytes)
            if r is not None and not r.get("error"):
                return str(r.get("content") or "")[:max_bytes]
        except Exception as e:
            log.debug("read_artifact_file sandbox read failed for %s: %s", cpath, e)
    try:
        base = artifact_dir(session_id=session_id)
        rel = re.sub(r"^/?workspace/", "", pnorm).lstrip("/")
        parts = [s for s in rel.split("/") if s and s not in (".", "..")]
        if not parts:
            return None
        target = _norm_path(os.path.join(base, *parts))
        if not (_path_within(target, base) and os.path.isfile(target)):
            return None
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_bytes)
    except Exception as e:
        log.debug("read_artifact_file host read failed for %s: %s", p, e)
        return None


async def artifact_list_files(session_id: str = "", limit: int = 40) -> Optional[List[str]]:
    """The REAL filenames in the session's effective working directory — inside the
    container when sandboxed (where its runs execute), else the host artifact dir.
    Relative names only, so they resolve in the exec cwd either way.

    This is what an agent should be told instead of being left to guess: the
    invented-path failure mode (`cat /mnt/data/summary.txt`) is a model filling a
    gap in its knowledge of what actually exists. Returns None when the listing
    could NOT be determined (never [] — an empty list means the directory really
    is empty, and callers state that to the model as fact)."""
    session_id = session_id or _trigger_session_id()
    names: List[str] = []
    sb = _sandbox_mod()
    if session_id and sb is not None and hasattr(sb, "route_fs_list"):
        try:
            r = await sb.route_fs_list(session_id, _sandbox_workdir())
            if r is not None and not r.get("error"):
                for e in (r.get("entries") or []):
                    n = str(e.get("name") or "").strip()
                    if n and not n.startswith("."):
                        names.append(n + ("/" if e.get("kind") == "dir" else ""))
                return sorted(names)[:max(1, limit)]
        except Exception as e:
            log.debug("artifact_list_files sandbox list failed: %s", e)
    try:
        base = artifact_dir(session_id=session_id)
        for n in sorted(os.listdir(base)):
            if n.startswith("."):
                continue
            names.append(n + ("/" if os.path.isdir(os.path.join(base, n)) else ""))
    except Exception as e:
        log.debug("artifact_list_files host list failed: %s", e)
        return None
    return names[:max(1, limit)]


def _sandbox_workdir() -> str:
    """The container path a sandboxed session runs in ('/workspace')."""
    sb = _sandbox_mod()
    return str(getattr(sb, "_WORKDIR", "/workspace") or "/workspace") if sb else "/workspace"


async def copy_file_into_artifacts(session_id: str = "", host_path: str = "",
                                   dest_name: str = "") -> str:
    """Place an existing HOST file (e.g. generated podcast audio) into the session's
    artifact area — the container /workspace when sandboxed (binary-safe docker cp),
    else the host artifact dir. Lets a cap that produced a file DELIVER it where the
    loop/agent will find it, so no separate 'save to disk' step is needed. Returns
    the destination path, or '' on failure."""
    session_id = session_id or _trigger_session_id()
    if not host_path or not os.path.isfile(host_path):
        return ""
    sb = _sandbox_mod()
    if session_id and sb is not None and hasattr(sb, "route_copy_in"):
        try:
            r = await sb.route_copy_in(session_id, host_path, dest_name)
            if r and not r.get("error") and r.get("path"):
                return r["path"]
        except Exception as e:
            log.debug("copy_file_into_artifacts sandbox route failed: %s", e)
    # Host fallback.
    try:
        import shutil
        base = artifact_dir(session_id=session_id)
        name = re.sub(r"[^\w.\-]", "_", dest_name or os.path.basename(host_path)) or "artifact.bin"
        target = _norm_path(os.path.join(base, name))
        if _path_within(target, base):
            os.makedirs(base, exist_ok=True)
            shutil.copyfile(host_path, target)
            return target
    except Exception as e:
        log.debug("copy_file_into_artifacts host copy failed: %s", e)
    return ""


@APP.get("/exec/artifacts/list", include_in_schema=False)
async def artifact_list(session_id: str = ""):
    """The files in a session's working directory, for the loop UI's file browser.

    The agentic-loop cards could name files a run produced but gave no way to open
    them: the run's sandbox is not reachable from the chat UI, so a generated
    report/page/script was effectively write-only until someone shelled in. Pairs
    with /exec/artifacts/download (same routing) so the UI can list, preview and
    fetch the source of anything a loop wrote.

    Output: {ok, session_id, dir, files:[{name, size, is_dir, ext}]}. Hidden
    entries and the loop's own bookkeeping are filtered out."""
    from fastapi.responses import JSONResponse
    sid = str(session_id or "").strip()
    if not sid:
        return JSONResponse({"error": "session_id required"}, status_code=400)
    try:
        base = await artifact_dir_async(session_id=sid, create=False)
    except Exception:
        base = ""
    def _row(name: str, size, mtime, is_dir: bool):
        return {"name": name, "is_dir": is_dir, "size": size, "mtime": mtime,
                "ext": (name.rsplit(".", 1)[-1].lower() if "." in name else "")}

    def _keep(name: str) -> bool:
        return bool(name) and not name.startswith(".") and name != "__pycache__"

    out = []
    # Sandboxed → route_fs_list already carries size/mtime per entry, so take them
    # from there rather than probing each file (one call, not N round-trips).
    sb = _sandbox_mod()
    if sb is not None and hasattr(sb, "route_fs_list"):
        try:
            r = await sb.route_fs_list(sid, _sandbox_workdir())
        except Exception:
            r = None
        if r is not None and not r.get("error"):
            for e in (r.get("entries") or []):
                nm = str(e.get("name") or "")
                if _keep(nm):
                    out.append(_row(nm, e.get("size"), e.get("mtime"),
                                    e.get("kind") == "dir"))
            out.sort(key=lambda x: (x["is_dir"], x["name"].lower()))
            return {"ok": True, "session_id": sid, "dir": base, "files": out}
    # Host artifact dir.
    try:
        hbase = artifact_dir(session_id=sid)
        with os.scandir(hbase) as it:
            for e in it:
                if not _keep(e.name):
                    continue
                try:
                    st = e.stat()
                    out.append(_row(e.name, st.st_size, int(st.st_mtime), e.is_dir()))
                except Exception:
                    out.append(_row(e.name, None, None, e.is_dir()))
    except Exception as e:
        return {"ok": False, "session_id": sid, "dir": base, "files": [],
                "error": f"working directory could not be listed: {e}"}
    out.sort(key=lambda x: (x["is_dir"], x["name"].lower()))
    return {"ok": True, "session_id": sid, "dir": base, "files": out}


@APP.get("/exec/artifacts/download", include_in_schema=False)
async def artifact_download(session_id: str = "", rel: str = ""):
    """Download one file from a session's artifact directory as an attachment.
    `rel` is the path relative to the artifact dir; traversal outside it is
    rejected. Lets the chat UI (and loop final cards) deliver generated files
    (reports, documents, code, archives) as real downloads."""
    from fastapi.responses import FileResponse, JSONResponse, Response
    rel = str(rel or "").strip().strip("/\\")
    if not rel:
        return JSONResponse({"error": "rel required"}, status_code=400)
    safe_parts = [p for p in re.split(r"[\\/]+", rel) if p and p not in (".", "..")]
    base = await artifact_dir_async(session_id=session_id, create=False)

    # Sandboxed session → stream the file OUT of the container (its /workspace is
    # not on this host, so os.path can't see it).
    sb = _sandbox_mod()
    if base and base.startswith("/workspace") and sb is not None \
            and hasattr(sb, "route_fs_read"):
        cpath = base.rstrip("/") + "/" + "/".join(safe_parts)
        try:
            res = await sb.route_fs_read(session_id, cpath)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        if res is None or res.get("error"):
            return JSONResponse({"error": (res or {}).get("error") or f"not found: {rel}"},
                                status_code=404)
        data = (res.get("content") or "").encode("utf-8", "replace")
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{os.path.basename(rel)}"'})

    if not base or not os.path.isdir(base):
        return JSONResponse({"error": "no artifact directory for this session"},
                            status_code=404)
    target = _norm_path(os.path.join(base, *safe_parts))
    if not _path_within(target, base):
        return JSONResponse({"error": "path escapes the artifact directory"},
                            status_code=400)
    if not os.path.isfile(target):
        return JSONResponse({"error": f"not found: {rel}"}, status_code=404)
    return FileResponse(target, filename=os.path.basename(target),
                        media_type="application/octet-stream")


@capability(
    "exec.sandbox.get",
    http_method="GET", http_path="/exec/sandbox", http_tags=["exec"],
    memory="off", silent=True,
    description="Return the active exec sandbox policy (allow/deny paths, "
                "language whitelist, command block/allow lists, timeout cap). "
                "Output: {policy: {...}, path, defaults}.",
)
async def cap_sandbox_get(trace_id=None) -> Dict:
    return {
        "policy":   _load_sandbox(),
        "path":     str(_SANDBOX_PATH),
        "defaults": _DEFAULT_SANDBOX,
    }


@capability(
    "exec.sandbox.set",
    http_method="POST", http_path="/exec/sandbox/set", http_tags=["exec"],
    description="Update the exec sandbox policy. Any omitted field is left "
                "unchanged. Input: enabled (bool), languages (list[str] — [] = all), "
                "allow_paths (list[str]), deny_paths (list[str]), "
                "command_blocklist (list[str] regex), command_allowlist (list[str] regex), "
                "max_timeout (int sec, 0=uncapped), network (bool), "
                "artifact_root (str — '' = default ~/.vera_artifacts), "
                "artifact_scope (artifact|session|project|workspace), "
                "reset (bool — restore shipped defaults first). "
                "Output: {ok, policy}.",
)
async def cap_sandbox_set(
    enabled:           Optional[bool] = None,
    languages:         Optional[List[str]] = None,
    allow_paths:       Optional[List[str]] = None,
    deny_paths:        Optional[List[str]] = None,
    command_blocklist: Optional[List[str]] = None,
    command_allowlist: Optional[List[str]] = None,
    max_timeout:       Optional[int] = None,
    network:           Optional[bool] = None,
    artifact_root:     Optional[str] = None,
    artifact_scope:    Optional[str] = None,
    reset:             bool = False,
    trace_id=None,
) -> Dict:
    pol = dict(_DEFAULT_SANDBOX) if reset else _load_sandbox()

    if artifact_root is not None:
        pol["artifact_root"] = str(artifact_root).strip()
    if artifact_scope is not None:
        sc = str(artifact_scope).strip().lower()
        if sc and sc not in _ARTIFACT_SCOPES:
            return {"ok": False, "error": f"artifact_scope must be one of {_ARTIFACT_SCOPES}"}
        if sc:
            pol["artifact_scope"] = sc

    def _as_list(v):
        if v is None:
            return None
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        # accept newline- or comma-separated strings from form inputs
        return [s.strip() for s in re.split(r"[\n,]", str(v)) if s.strip()]

    if enabled is not None:           pol["enabled"] = bool(enabled)
    if network is not None:           pol["network"] = bool(network)
    if max_timeout is not None:       pol["max_timeout"] = int(max_timeout)
    for key, val in (
        ("languages", languages), ("allow_paths", allow_paths),
        ("deny_paths", deny_paths), ("command_blocklist", command_blocklist),
        ("command_allowlist", command_allowlist),
    ):
        lst = _as_list(val)
        if lst is not None:
            pol[key] = lst

    # Validate regex lists so a bad pattern can't silently disable a list
    bad = []
    for key in ("command_blocklist", "command_allowlist"):
        for pat in pol.get(key, []):
            try:
                re.compile(pat)
            except re.error as e:
                bad.append(f"{key}: {pat!r} ({e})")
    if bad:
        return {"ok": False, "error": "invalid regex pattern(s)", "details": bad}

    _save_sandbox(pol)
    await emit_event({"type": "exec.sandbox.updated", "enabled": pol["enabled"]})
    return {"ok": True, "policy": pol, "path": str(_SANDBOX_PATH)}


@capability(
    "exec.sandbox.artifact_dir",
    http_method="GET", http_path="/exec/sandbox/artifact_dir", http_tags=["exec"],
    memory="off", silent=True,
    description="Resolve (and create) the artifact directory for agent-generated "
                "files, per the sandbox's artifact_scope. Inputs: session_id (str), "
                "project (str), workspace (str), artifact (str), create (bool, default true). "
                "Output: {dir, root, scope}.",
)
async def cap_sandbox_artifact_dir(
    session_id: str = "", project: str = "", workspace: str = "",
    artifact: str = "", create: bool = True, trace_id=None,
) -> Dict:
    pol = _load_sandbox()
    # Sandbox-aware: when the session has an active sandbox the artifact dir is the
    # container's /workspace (where its files actually live), so the chat artifacts
    # panel resolves — and then lists/reads — the right place.
    d = await artifact_dir_async(session_id=session_id, project=project,
                                 workspace=workspace, artifact=artifact,
                                 create=bool(create))
    sandboxed = d.startswith("/workspace")
    return {"dir": d, "root": ("/workspace" if sandboxed else _artifact_root(pol)),
            "scope": pol.get("artifact_scope", "session"), "sandboxed": sandboxed}


def _unescape_collapsed(content: str) -> str:
    """Recover a file an LLM double-escaped — it emitted literal "\\n"/"\\t"
    instead of real newlines/tabs, collapsing the whole file onto one line
    (a frequent small-model failure that makes saved scripts unrunnable).

    Conservative: only fires when there is NO real newline at all and at least
    two literal "\\n" sequences are present, so normal multi-line content (and a
    one-off "\\n" inside a string literal) is left untouched."""
    if not content or "\n" in content:
        return content
    if content.count("\\n") < 2:
        return content
    return (content.replace("\\r\\n", "\n").replace("\\n", "\n")
                   .replace("\\t", "\t"))


@capability(
    "exec.sandbox.write_artifact",
    http_method="POST", http_path="/exec/sandbox/write_artifact", http_tags=["exec"],
    description="Write content to a file inside the run's artifact directory "
                "(resolved per artifact_scope; path is confined to that dir). "
                "Inputs: filename (str! — may include subdirs), content (str), "
                "session_id, project, workspace. Output: {ok, path, rel}.",
)
async def cap_sandbox_write_artifact(
    filename:   str,
    content:    str = "",
    session_id: str = "", project: str = "", workspace: str = "",
    trace_id=None,
) -> Dict:
    if not filename or not str(filename).strip():
        return {"ok": False, "error": "filename required"}
    # Sandbox-aware: when the session has an active sandbox this lands in the
    # container's /workspace (where its runs execute), else in the host dir.
    try:
        target = await write_artifact_file(
            relpath=filename, content=_unescape_collapsed(content or ""),
            session_id=session_id, project=project, workspace=workspace)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    sandboxed = target.startswith("/workspace")
    await emit_event({"type": "exec.sandbox.artifact_written",
                      "path": target, "session_id": session_id,
                      "sandboxed": sandboxed})
    rel = target.split("/workspace/", 1)[-1] if sandboxed else \
        os.path.relpath(target, artifact_dir(session_id=session_id, project=project,
                                              workspace=workspace, create=False))
    return {"ok": True, "path": target, "rel": rel, "sandboxed": sandboxed}


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-LANGUAGE CODE EXECUTION
# ─────────────────────────────────────────────────────────────────────────────
#
# exec.code.run runs a snippet in a chosen language by writing it to a temp
# file and invoking the interpreter. Per-language convenience caps
# (exec.python.run, exec.node.run, …) delegate here. bash / powershell route
# back to the dedicated runners above so behaviour stays identical.
#
# Each entry: bins = interpreter candidates (first found on PATH wins, unless
# VERA_<LANG>_BIN overrides), ext = temp-file suffix, argv = how to invoke.
# ─────────────────────────────────────────────────────────────────────────────
_LANG_SPECS: Dict[str, Dict[str, Any]] = {
    "python": {"bins": ["python3", "python"], "ext": ".py",  "argv": lambda b, f: [b, f]},
    "node":   {"bins": ["node"],              "ext": ".js",  "argv": lambda b, f: [b, f]},
    "ruby":   {"bins": ["ruby"],              "ext": ".rb",  "argv": lambda b, f: [b, f]},
    "php":    {"bins": ["php"],               "ext": ".php", "argv": lambda b, f: [b, f]},
    "perl":   {"bins": ["perl"],              "ext": ".pl",  "argv": lambda b, f: [b, f]},
    "go":     {"bins": ["go"],                "ext": ".go",  "argv": lambda b, f: [b, "run", f]},
    "lua":    {"bins": ["lua"],               "ext": ".lua", "argv": lambda b, f: [b, f]},
    "deno":   {"bins": ["deno"],              "ext": ".ts",  "argv": lambda b, f: [b, "run", "-A", f]},
}

# Friendly aliases → canonical lang id (also used by the chat UI's Run button)
_LANG_ALIASES: Dict[str, str] = {
    "py": "python", "python3": "python",
    "js": "node", "javascript": "node", "nodejs": "node", "mjs": "node",
    "rb": "ruby",
    "pl": "perl",
    "golang": "go",
    "ts": "deno", "typescript": "deno",
    # shell langs route to the dedicated runners
    "sh": "bash", "shell": "bash", "zsh": "bash",
    "ps": "powershell", "ps1": "powershell", "pwsh": "powershell", "posh": "powershell",
}


def _canon_lang(language: str) -> str:
    l = (language or "").strip().lower()
    return _LANG_ALIASES.get(l, l)


def _resolve_lang_bin(lang: str) -> str:
    """Find the interpreter for a canonical lang id. Honours VERA_<LANG>_BIN."""
    import shutil
    env_bin = os.getenv(f"VERA_{lang.upper()}_BIN", "")
    if env_bin:
        return env_bin
    spec = _LANG_SPECS.get(lang)
    if not spec:
        return ""
    for cand in spec["bins"]:
        found = shutil.which(cand)
        if found:
            return found
    return ""


# Map a file extension → canonical run language (used when running a saved file
# by path without an explicit language).
_EXT_LANG: Dict[str, str] = {
    ".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node",
    ".rb": "ruby", ".php": "php", ".pl": "perl", ".go": "go", ".lua": "lua",
    ".ts": "deno", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".ps1": "powershell",
}


def _lang_from_ext(p: str) -> str:
    return _EXT_LANG.get(os.path.splitext(str(p))[1].lower(), "")


# Interpreter-prefixed one-liner, e.g. "python /art/app.py" — a very common way
# an agent tries (wrongly) to "run a file" by passing the shell invocation as code.
_INVOCATION_RE = re.compile(
    r'^\s*(?:python3?|node|nodejs|ruby|php|perl|lua|deno|bash|sh)\s+'
    r'(["\']?)([^"\']+\.[A-Za-z0-9]+)\1\s*$')


def _invocation_path(code: str) -> str:
    """If `code` is really just *running a file* (an interpreter + path, or a
    bare path to an existing file) rather than a snippet, return that path so we
    can run the file directly. Empty string otherwise."""
    s = (code or "").strip()
    if not s or "\n" in s:
        return ""
    m = _INVOCATION_RE.match(s)
    if m:
        return m.group(2)
    if re.match(r'^["\']?[~/][^\n]*\.[A-Za-z0-9]+["\']?$', s):
        cand = s.strip('"\'')
        if os.path.isfile(cand):
            return cand
    return ""


async def _run_code(language: str, code: str, *, stdin: str = "",
                    timeout: int = _EXEC_DEFAULT_TIMEOUT, cwd: str = "",
                    args: Optional[List[str]] = None, path: str = "") -> Dict[str, Any]:
    import tempfile
    lang = _canon_lang(language)

    # ── Run an existing FILE by path ────────────────────────────────────────
    # Agents routinely save a script then want to run it. Accept an explicit
    # `path`, or recover from the common mistake of passing the invocation
    # (e.g. "python /art/app.py") or a bare file path as `code`.
    run_path = str(path or "").strip().strip('"\'')
    if not run_path and code:
        run_path = _invocation_path(code)
    if run_path:
        try:
            with open(run_path, "r", encoding="utf-8", errors="replace") as fh:
                code = fh.read()
        except Exception as e:
            return {"ok": False, "rc": -1, "stdout": "", "stderr": str(e),
                    "language": lang, "error": f"cannot read file '{run_path}': {e}"}
        if not lang:
            lang = _canon_lang(_lang_from_ext(run_path))
        if not cwd:
            cwd = os.path.dirname(run_path)

    if not code or not code.strip():
        return {"ok": False, "error": "empty code", "rc": -1,
                "stdout": "", "stderr": "", "language": lang}

    timeout = _sandbox_clamp_timeout(parse_timeout(timeout))
    cwd = _sandbox_effective_cwd(cwd)

    # Shared sandbox gate (skipped for bash/ps — their own caps re-check)
    if lang not in ("bash", "powershell"):
        ok, reason = _sandbox_check(code, cwd=cwd, language=lang)
        if not ok:
            await emit_event({"type": "exec.sandbox.blocked",
                              "language": lang, "reason": reason})
            return {"ok": False, "error": f"sandbox: {reason}", "blocked": True,
                    "rc": -1, "stdout": "", "stderr": "", "language": lang}

    # Shell languages reuse the dedicated runners (identical behaviour)
    if lang == "bash":
        r = await cap_bash_run(command=code, timeout=timeout, cwd=cwd)
        r["language"] = "bash"; return r
    if lang == "powershell":
        r = await cap_ps_run(command=code, timeout=timeout, cwd=cwd)
        r["language"] = "powershell"; return r

    spec = _LANG_SPECS.get(lang)
    if not spec:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "language": lang,
                "error": f"unsupported language '{language}'. "
                         f"Known: {', '.join(sorted(_LANG_SPECS) + ['bash', 'powershell'])}"}

    bin_path = _resolve_lang_bin(lang)
    if not bin_path:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "language": lang,
                "error": f"interpreter for '{lang}' not found on PATH "
                         f"(tried {', '.join(spec['bins'])}). Install it or set "
                         f"VERA_{lang.upper()}_BIN."}

    # Write the snippet to a temp file, run it, then clean up.
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=spec["ext"], prefix="vera_exec_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        argv = spec["argv"](bin_path, tmp_path) + list(args or [])
        result = await _run_local(argv, stdin_data=stdin, timeout=timeout,
                                  cwd=cwd or None)
        result["language"] = lang
        result["bin"] = bin_path
        return result
    except Exception as e:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": str(e),
                "language": lang, "error": f"{type(e).__name__}: {e}"}
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass


@capability(
    "exec.code.run",
    http_method="POST", http_path="/exec/code/run", http_tags=["exec"],
    description="Run a code snippet locally in a chosen language and capture output. "
                "WHEN TO USE: test-run a script Vera (or the user) wrote — a Python "
                "LAN scanner, a Node helper, a Ruby one-liner, etc. Subject to the exec "
                "sandbox policy (exec.sandbox.get/set). "
                "Input: language (str — python|node|ruby|php|perl|go|lua|deno|bash|powershell, "
                "aliases py/js/ts accepted; optional when `path` is given — inferred from the "
                "file extension), code (str — inline snippet), path (str — run an EXISTING saved "
                "file instead of a snippet; use this to run a script you wrote to the artifact "
                "dir), stdin (str), timeout (int sec, default 600 = 10 min; raise it for long "
                "runs), cwd (str — working dir, "
                "defaults to the file's dir when running by path), args (list[str] — passed to "
                "the script). Provide EITHER code OR path. "
                "Output: {ok, rc, stdout, stderr, elapsed_ms, language, bin}. "
                "Use the /exec/code/stream endpoint for live output of long runs.",
    schema={"properties": {
        "language": {"enum": sorted(set(list(_LANG_SPECS) + ["bash", "powershell"]
                                        + list(_LANG_ALIASES)))},
    }},
)
async def cap_code_run(language: str = "", code: str = "", stdin: str = "",
                       timeout: int = _EXEC_DEFAULT_TIMEOUT, cwd: str = "",
                       args: Optional[List[str]] = None, path: str = "",
                       session_id: str = "", trace_id=None) -> Dict:
    if not language and not str(path or "").strip():
        return {"ok": False, "error": "language required (or pass a file `path`)",
                "rc": -1, "stdout": "", "stderr": ""}
    routed = await _route_session_code(session_id, language, code, path, stdin,
                                       timeout, args)
    if routed is not None:
        return routed
    return await _run_code(language, code, stdin=stdin, timeout=timeout,
                           cwd=cwd, args=args, path=path)


def _make_lang_cap(lang_id: str, label: str):
    """Build a thin per-language @capability that delegates to _run_code."""
    async def _runner(code: str = "", stdin: str = "",
                      timeout: int = _EXEC_DEFAULT_TIMEOUT, cwd: str = "",
                      args: Optional[List[str]] = None, path: str = "",
                      session_id: str = "", trace_id=None) -> Dict:
        routed = await _route_session_code(session_id, lang_id, code, path, stdin,
                                           timeout, args)
        if routed is not None:
            return routed
        return await _run_code(lang_id, code, stdin=stdin, timeout=timeout,
                               cwd=cwd, args=args, path=path)
    _runner.__name__ = f"cap_{lang_id}_run"
    return capability(
        f"exec.{lang_id}.run",
        http_method="POST", http_path=f"/exec/{lang_id}/run", http_tags=["exec"],
        description=f"Run {label} locally and capture output. Subject to the exec "
                    f"sandbox policy. Provide EITHER an inline snippet via code, OR a saved "
                    f"file to run via path (e.g. a script you wrote to the artifact dir — "
                    f"pass the absolute path, NOT 'python <path>' as code). "
                    f"Input: code (str), path (str — existing file to run), stdin (str), "
                    f"timeout (int sec), cwd (str), args (list[str]). "
                    f"Output: {{ok, rc, stdout, stderr, elapsed_ms, language, bin}}.",
    )(_runner)


# Register the per-language convenience capabilities.
cap_python_run = _make_lang_cap("python", "Python")
cap_node_run   = _make_lang_cap("node",   "Node.js / JavaScript")
cap_ruby_run   = _make_lang_cap("ruby",   "Ruby")
cap_php_run    = _make_lang_cap("php",    "PHP")
cap_perl_run   = _make_lang_cap("perl",   "Perl")
cap_go_run     = _make_lang_cap("go",     "Go")
cap_lua_run    = _make_lang_cap("lua",    "Lua")


@capability(
    "exec.code.langs",
    http_method="GET", http_path="/exec/code/langs", http_tags=["exec"],
    memory="off", silent=True,
    description="List supported code-execution languages and whether each "
                "interpreter is installed on the host. "
                "Output: {languages: [{id, bins, ext, available, bin}], sandbox_enabled}.",
)
async def cap_code_langs(trace_id=None) -> Dict:
    pol = _load_sandbox()
    langs = []
    for lang, spec in sorted(_LANG_SPECS.items()):
        bin_path = _resolve_lang_bin(lang)
        langs.append({
            "id":        lang,
            "bins":      spec["bins"],
            "ext":       spec["ext"],
            "available": bool(bin_path),
            "bin":       bin_path,
            "allowed":   (not pol.get("languages")) or lang in pol["languages"],
        })
    # bash / powershell are always available via the dedicated runners
    for lang in ("bash", "powershell"):
        langs.append({"id": lang, "bins": [lang], "ext": "", "available": True,
                      "bin": "", "allowed": (not pol.get("languages"))
                      or lang in pol["languages"]})
    return {"languages": langs, "sandbox_enabled": bool(pol.get("enabled")),
            "aliases": _LANG_ALIASES}


@APP.post("/exec/code/stream")
async def exec_code_stream(request: Request):
    """SSE-stream stdout/stderr of a code snippet (writes a temp file, runs the
    interpreter, deletes the temp file when the stream ends)."""
    import tempfile
    try:
        body = await request.json()
    except Exception:
        body = {}
    language   = body.get("language", "")
    code       = body.get("code", "")
    cwd        = body.get("cwd", "") or ""
    timeout    = int(body.get("timeout", 300))
    session_id = body.get("session_id", "") or ""

    lang = _canon_lang(language)
    timeout = _sandbox_clamp_timeout(timeout)
    cwd = _sandbox_effective_cwd(cwd)

    def _err_stream(msg: str):
        async def _g():
            yield _sse("error", {"error": msg})
            yield _sse("done", {"rc": -1})
        return StreamingResponse(_g(), media_type="text/event-stream")

    if not code.strip():
        return _err_stream("empty code")

    # Opt-in per-session sandbox: if this session has an ACTIVE sandbox, stream
    # the run from INSIDE its container (no host temp file, no host interpreter).
    # Falls through to the host path for inactive sandboxes / unsupported langs.
    sbx_argv = await _route_session_code_argv(session_id, lang, code)
    if sbx_argv is not None:
        if session_id:
            _syslog = sys.modules.get("syslog")
            if _syslog:
                try:
                    _syslog.set_trigger(str(uuid.uuid4()), "exec.code.stream", session_id)
                except Exception:
                    pass

        async def _sbx_gen():
            async for chunk in _stream_subprocess_recorded(
                sbx_argv, cap_name="exec.code.stream", session_id=session_id,
                params={"language": lang, "sandboxed": True, "timeout": timeout},
                cwd=None, timeout=timeout,
            ):
                yield chunk

        return StreamingResponse(
            _sbx_gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Shell langs reuse the existing stream endpoints' machinery via argv
    if lang == "bash":
        bash_bin = os.getenv("VERA_BASH_BIN", "/bin/bash")
        ok, reason = _sandbox_check(code, cwd=cwd, language="")
        if not ok:
            return _err_stream(f"sandbox: {reason}")
        argv = [bash_bin, "-lc", code]
    elif lang == "powershell":
        ps_bin = os.getenv("VERA_PS_BIN", "pwsh")
        ok, reason = _sandbox_check(code, cwd=cwd, language="")
        if not ok:
            return _err_stream(f"sandbox: {reason}")
        argv = [ps_bin, "-NoProfile", "-NonInteractive", "-Command", code]
    else:
        ok, reason = _sandbox_check(code, cwd=cwd, language=lang)
        if not ok:
            return _err_stream(f"sandbox: {reason}")
        spec = _LANG_SPECS.get(lang)
        if not spec:
            return _err_stream(f"unsupported language '{language}'")
        bin_path = _resolve_lang_bin(lang)
        if not bin_path:
            return _err_stream(f"interpreter for '{lang}' not found on PATH")
        fd, tmp_path = tempfile.mkstemp(suffix=spec["ext"], prefix="vera_exec_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        argv = spec["argv"](bin_path, tmp_path)

    if session_id:
        _syslog = sys.modules.get("syslog")
        if _syslog:
            try:
                _syslog.set_trigger(str(uuid.uuid4()), "exec.code.stream", session_id)
            except Exception:
                pass

    async def _gen():
        try:
            async for chunk in _stream_subprocess_recorded(
                argv, cap_name="exec.code.stream", session_id=session_id,
                params={"language": lang, "cwd": cwd, "timeout": timeout},
                cwd=cwd or None, timeout=timeout,
            ):
                yield chunk
        finally:
            if lang not in ("bash", "powershell"):
                try: os.unlink(argv[-1])
                except Exception: pass

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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

    # Opt-in per-session sandbox: stream from INSIDE the session's container when
    # it has an ACTIVE sandbox (mirrors exec.bash.run's route_shell). The host
    # exec-sandbox policy check below is bypassed — isolation is the container.
    sbx_argv = await _route_session_shell_argv(session_id, command)
    if sbx_argv is not None:
        return StreamingResponse(
            _stream_subprocess_recorded(
                sbx_argv, cap_name="exec.bash.stream", session_id=session_id,
                params={"command": command, "sandboxed": True,
                        "timeout": _sandbox_clamp_timeout(timeout)},
                cwd=None, timeout=_sandbox_clamp_timeout(timeout),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    _ok, _reason = _sandbox_check(command, cwd=cwd or "")
    if not _ok:
        async def _blocked():
            yield _sse("error", {"error": f"sandbox: {_reason}"})
            yield _sse("done", {"rc": -1})
        return StreamingResponse(_blocked(), media_type="text/event-stream")
    timeout = _sandbox_clamp_timeout(timeout)
    cwd = _sandbox_effective_cwd(cwd or "") or None
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

    # Opt-in per-session sandbox: stream PowerShell from INSIDE the container via
    # pwsh when this session has an ACTIVE sandbox (needs a pwsh-capable base).
    sbx_argv = await _route_session_shell_argv(session_id, command, shell="pwsh")
    if sbx_argv is not None:
        return StreamingResponse(
            _stream_subprocess_recorded(
                sbx_argv, cap_name="exec.ps.stream", session_id=session_id,
                params={"command": command, "sandboxed": True,
                        "timeout": _sandbox_clamp_timeout(timeout)},
                cwd=None, timeout=_sandbox_clamp_timeout(timeout),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    _ok, _reason = _sandbox_check(command, cwd=cwd or "")
    if not _ok:
        async def _blocked():
            yield _sse("error", {"error": f"sandbox: {_reason}"})
            yield _sse("done", {"rc": -1})
        return StreamingResponse(_blocked(), media_type="text/event-stream")
    timeout = _sandbox_clamp_timeout(timeout)
    cwd = _sandbox_effective_cwd(cwd or "") or None
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


async def _icmp_ping(ip: str, timeout: float = 1.0) -> bool:
    """Best-effort ICMP echo via the system ping binary (no raw-socket / root
    needed). Returns True if the host replies. Used as a LAN-scan fallback so
    hosts with no open TCP ports are still discovered."""
    proc = None
    try:
        if os.name == "nt":
            argv = ["ping", "-n", "1", "-w",
                    str(int(max(timeout, 0.5) * 1000)), ip]
        else:
            argv = ["ping", "-c", "1", "-W",
                    str(max(1, int(round(timeout)))), ip]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=max(timeout + 1.0, 2.0))
        return rc == 0
    except Exception:
        try:
            if proc:
                proc.kill()
        except Exception:
            pass
        return False


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
    description="Discover hosts on a LAN by TCP-pinging common ports across a CIDR, "
                "with an ICMP ping fallback so hosts with no open ports are still found. "
                "Persists :NetHost nodes (and optionally :NetPort nodes) into the aux graph. "
                "Input: cidr (str!), ports (comma-sep ints, optional), "
                "concurrency (int=64), timeout (float=0.8), "
                "ping (bool=true — ICMP-ping hosts that expose no open TCP ports), "
                "port_nodes (bool=true — create :NetPort nodes per open port), "
                "save_to_fabric (bool=true — persist results to fabric dataset), "
                "fabric_dataset (str='netscan_lan' — target dataset id). "
                "Output: {cidr, alive: [{ip, hostname, mac, open_ports, alive_via}], count, elapsed_ms}.",
)
async def cap_netscan_lan(
    cidr:            str,
    ports:           str   = "",
    concurrency:     int   = 64,
    timeout:         float = 0.8,
    ping:            bool  = True,
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
            # Mark alive via open TCP port, else fall back to ICMP ping so a
            # host that exposes none of the scanned ports is still discovered.
            if open_ports:
                alive_via = "tcp"
            elif ping and await _icmp_ping(ip_str, max(timeout, 1.0)):
                alive_via = "icmp"
            else:
                return None
            hostname = await _reverse_dns(ip_str)
            mac      = await _mac_lookup_via_arp(ip_str)
            rec = {
                "ip": ip_str, "hostname": hostname, "mac": mac,
                "open_ports": open_ports, "alive_via": alive_via,
            }
            host_id = f"net:{ip_str}"
            await _aux_upsert_nethost(
                ip_str, mac=mac, hostname=hostname, subnet=subnet,
                open_ports=open_ports, source="lan")
            if port_nodes and open_ports:
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
    "NetPort", "NetHop",
    # OSINT / boundary model
    "Domain", "ASN", "NetBlock", "GeoRegion",
    # WiFi (ingested from ESP32 mesh)
    "WifiAP",
    # Encrypted overlay mesh (netsec WireGuard/Nebula) — see netmap.mesh.ingest
    "MeshNet", "MeshNode",
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
    "netmap.mesh.ingest",
    http_method="POST", http_path="/netmap/mesh/ingest", http_tags=["netscan", "netsec"],
    memory="off",
    description="Map the encrypted overlay mesh (netsec WireGuard/Nebula) into the "
                "network graph: a :MeshNet overlay node, a :MeshNode per member "
                "(overlay IP + enrolment + bring-up state), :IN_MESH membership "
                "edges, :MESH_PEER edges for the peer topology, and :SAME_IP "
                "cross-links to the matching :NetHost by underlay IP. Run it after "
                "a mesh join/apply (or any time) to keep the map current. "
                "Output: {ok, provider, members, peers, error?}.",
)
async def cap_netmap_mesh_ingest(trace_id=None) -> Dict:
    from Vera.vera import capability_orchestration as _co
    mc = _co.CAPABILITY_REGISTRY.get("netsec.mesh.members")
    if not mc or not mc.get("func"):
        return {"ok": False, "error": "netsec.mesh.members capability unavailable"}
    try:
        data = await mc["func"](trace_id=trace_id) or {}
    except Exception as e:
        return {"ok": False, "error": f"could not read mesh members: {e}"}
    members = data.get("members") or []
    provider = data.get("provider") or "wireguard"
    subnet   = data.get("subnet") or ""
    if not members:
        return {"ok": True, "provider": provider, "members": 0, "peers": 0,
                "note": "no mesh members to map"}

    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return {"ok": False, "error": "Neo4j not connected", "members": len(members)}

    ts = now_iso()
    net_id = f"mesh:{provider}:{subnet or 'overlay'}"
    # Overlay node
    await _aux_run(
        """
        MERGE (m:MeshNet {id:$id})
        SET m.provider=$provider, m.subnet=$subnet, m.label=$label,
            m.member_count=$n, m.updated_at=$ts, m.source='netsec'
        """,
        id=net_id, provider=provider, subnet=subnet,
        label=f"{provider} mesh", n=len(members), ts=ts,
    )

    ingested = 0
    for m in members:
        hid = m.get("host_id") or m.get("host") or ""
        if not hid:
            continue
        node_id = f"meshnode:{provider}:{hid}"
        await _aux_run(
            """
            MERGE (n:MeshNode {id:$id})
            SET n.host_id=$hid, n.label=$label, n.host=$host, n.overlay_ip=$ip,
                n.pubkey=$pubkey, n.endpoint=$endpoint, n.state=$state,
                n.enrolled=$enrolled, n.provider=$provider, n.updated_at=$ts,
                n.source='netsec'
            WITH n
            MATCH (mn:MeshNet {id:$nid})
            MERGE (n)-[:IN_MESH]->(mn)
            """,
            id=node_id, hid=hid, label=(m.get("label") or hid),
            host=(m.get("host") or ""), ip=(m.get("ip") or ""),
            pubkey=(m.get("pubkey") or ""), endpoint=(m.get("endpoint") or ""),
            state=(m.get("state") or ""), enrolled=bool(m.get("enrolled")),
            provider=provider, ts=ts, nid=net_id,
        )
        ingested += 1
        # Cross-link to the underlay NetHost by IP (best-effort — the node may
        # not have been discovered by a LAN scan yet).
        under_ip = str(m.get("host") or "").strip()
        if under_ip and under_ip.count(".") == 3:
            await _aux_run(
                """
                MATCH (mn:MeshNode {id:$id}), (h:NetHost {id:$nid})
                MERGE (h)-[:SAME_IP]->(mn)
                """,
                id=node_id, nid=f"net:{under_ip}",
            )

    # Peer topology: WireGuard/Nebula overlays are full-mesh between members
    # that carry a pubkey — draw an (undirected-style) MESH_PEER for each pair.
    peers = 0
    keyed = [m for m in members if (m.get("host_id") or m.get("host")) and m.get("pubkey")]
    for i in range(len(keyed)):
        for j in range(i + 1, len(keyed)):
            a = f"meshnode:{provider}:{keyed[i].get('host_id') or keyed[i].get('host')}"
            b = f"meshnode:{provider}:{keyed[j].get('host_id') or keyed[j].get('host')}"
            await _aux_run(
                """
                MATCH (x:MeshNode {id:$a}), (y:MeshNode {id:$b})
                MERGE (x)-[:MESH_PEER]->(y)
                """,
                a=a, b=b,
            )
            peers += 1

    await emit_event({"type": "netmap.mesh.ingested", "provider": provider,
                      "members": ingested, "peers": peers})
    return {"ok": True, "provider": provider, "subnet": subnet,
            "members": ingested, "peers": peers}


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
    from Vera.vera import capability_orchestration as _co
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
    from Vera.vera import capability_orchestration as _co

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
            _co._format_param_sig(p, v, req)
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
# Exec + Network are one tab with CSS-only sub-tabs. The shell mounts panel
# HTML via innerHTML (inline <script> would not run), so <style> + :checked
# sibling selectors switch the two iframes with no JS. The old standalone
# "Network" tab is no longer registered; /exec/panel and /netmap/panel are both
# still served and iframed by the sub-tabs.
register_ui(
    "exec-panel",
    "Exec",
    "▷_",
    """<div class="vera-subtab-panel" style="height:100%;display:flex;flex-direction:column;background:var(--bg0,#0d0f12)">
  <style>
    .vera-subtab-panel>input[type=radio]{position:absolute;left:-9999px;opacity:0}
    .vera-subtab-panel>.st-bar{display:flex;gap:2px;padding:5px 6px 0;border-bottom:1px solid var(--border,#333);flex-shrink:0}
    .vera-subtab-panel>.st-bar>label{padding:5px 14px;font-size:12px;font-weight:600;cursor:pointer;color:var(--dim2,#8a8a8a);border:1px solid transparent;border-bottom:none;border-radius:5px 5px 0 0;user-select:none;line-height:1.2}
    .vera-subtab-panel>.st-bar>label:hover{color:var(--fg,#eee)}
    .vera-subtab-panel>.st-frames{position:relative;flex:1;min-height:0}
    .vera-subtab-panel>.st-frames>iframe{position:absolute;inset:0;width:100%;height:100%;border:none;visibility:hidden}
    #exmg-exec:checked~.st-frames>.f-exec{visibility:visible}
    #exmg-net:checked~.st-frames>.f-net{visibility:visible}
    #exmg-exec:checked~.st-bar>label[for=exmg-exec],
    #exmg-net:checked~.st-bar>label[for=exmg-net]{color:var(--fg,#fff);background:var(--bg2,#1c1c1c);border-color:var(--border,#333)}
  </style>
  <input type="radio" name="exmg" id="exmg-exec" checked>
  <input type="radio" name="exmg" id="exmg-net">
  <div class="st-bar">
    <label for="exmg-exec">Exec</label>
    <label for="exmg-net">Network</label>
  </div>
  <div class="st-frames">
    <iframe class="f-exec" src="/exec/panel"   allow="clipboard-read; clipboard-write"></iframe>
    <iframe class="f-net"  src="/netmap/panel" allow="clipboard-read; clipboard-write"></iframe>
  </div>
</div>""",
    # Injected as a real <script> in the shell (register_ui `js`). Bridges the
    # exec<->netmap cross-links now that they're sub-tabs of one panel: an
    # Exec-side "recon in Network" / a Network-side "ssh in Exec" click posts to
    # the shell (window.parent); we catch it here, flip the sub-tab radio, and
    # forward the deep-link into the target iframe.
    """(function(){
      function sub(id){ var r=document.getElementById(id); if(r) r.checked=true; }
      function frame(sel){ return document.querySelector(sel); }
      window.addEventListener('message', function(ev){
        var d = ev.data; if(!d || typeof d!=='object') return;
        if(d.action==='vera-netmap-recon' && d.target){
          sub('exmg-net');
          var f=frame('.f-net');
          try{ f && f.contentWindow && f.contentWindow.postMessage({action:'vera-netmap-recon', target:d.target}, '*'); }catch(e){}
          return;
        }
        if(d.action==='vera-open-panel'){
          if(d.panel_id==='netmap-panel'){
            sub('exmg-net');
            if(d.hash){ var fn=frame('.f-net'); try{ if(fn&&fn.contentWindow) fn.contentWindow.location.hash=d.hash; }catch(e){} }
          } else if(d.panel_id==='exec-panel'){
            sub('exmg-exec');
            if(d.hash){ var fe=frame('.f-exec'); try{ if(fe&&fe.contentWindow) fe.contentWindow.location.hash=d.hash; }catch(e){} }
          }
        }
      });
    })();""",
    ui_caps=[
        # ── Exec (shell / code / ssh) ──
        "exec.bash.run", "exec.ps.run", "exec.ssh.run",
        "exec.code.run", "exec.code.langs",
        "exec.python.run", "exec.node.run", "exec.ruby.run",
        "exec.php.run", "exec.perl.run", "exec.go.run", "exec.lua.run",
        "exec.sandbox.get", "exec.sandbox.set",
        "exec.ssh.hosts.list", "exec.ssh.hosts.save",
        "exec.ssh.hosts.delete", "exec.ssh.probe",
        "exec.llm.models",
        "dag.plan", "dag.plan_and_run",
        # ── Network (netscan / netmon — folded in as the Network sub-tab) ──
        "netscan.lan.scan", "netscan.docker.scan",
        "netscan.proxmox.scan", "netscan.k8s.scan", "netscan.web.scan",
        "netscan.target.ports", "netscan.target.tech", "netscan.target.traffic",
        "netscan.target.cert_scrape",
        "netscan.target.fingerprint", "netscan.target.banner",
        "netscan.target.tls", "netscan.target.traceroute",
        "netscan.recon.run",
        "netscan.proxmox.import", "netscan.docker.import",
        "netscan.dork.search", "netscan.dork.targeted",
        "netscan.osint.run",
        "netscan.osint.campaign.list", "netscan.osint.campaign.create",
        "netscan.osint.campaign.get", "netscan.osint.campaign.add",
        "netscan.osint.campaign.delete",
        "netscan.enrich.host", "netscan.enrich.bulk",
        "netscan.graph.relink", "netscan.map.aggregate", "netscan.asn.expand",
        "netmon.config.get", "netmon.config.set",
        "netmon.target.list", "netmon.target.save", "netmon.target.delete",
        "netmon.target.watch", "netmon.device.name",
        "netmon.alerts.list", "netmon.alerts.clear", "netmon.test",
        "netmon.scan_now", "netscan.wifi.ingest",
        "netscan.graph", "netscan.node.get", "netscan.nodes.clear",
        "netscan.graph.clear_all",
        "netscan.map.save", "netscan.map.list", "netscan.map.load", "netscan.map.delete",
        "netscan.fabric.load_web",
    ],
    mode="tab",
    tab_order=53,
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

from Vera.vera.config import cfg
from Vera.vera.capability_orchestration import (
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
        "Vera.vera.exec_capabilities"
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
    ping        = bool(body.get("ping", True))
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
                    alive_via = "tcp"
                elif ping and await _icmp_ping(ip_str, max(timeout, 1.0)):
                    alive_via = "icmp"
                else:
                    alive_via = ""
                if alive_via:
                    live_count += 1
                    hostname = await _reverse_dns(ip_str)
                    rec = {
                        "ip": ip_str, "hostname": hostname,
                        "open_ports": open_ports,
                        "subnet": subnet, "alive_via": alive_via,
                    }
                    host_id = f"net:{ip_str}"
                    await _aux_upsert_nethost(
                        ip_str, hostname=hostname, subnet=subnet,
                        open_ports=open_ports, source="lan",
                    )
                    if port_nodes and open_ports:
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
# 5b.  CERTIFICATE TRANSPARENCY SCRAPE (crt.sh) — discover hosts/pages that are
#      not linked from the site or even reachable, by reading issued certs.
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "netscan.target.cert_scrape",
    http_method="POST", http_path="/netscan/target/cert_scrape",
    http_tags=["netscan"],
    description="Enumerate hostnames for a domain from Certificate Transparency "
                "logs (crt.sh) — surfaces subdomains/hosts that may not be linked "
                "from the site or even reachable. Each discovered host is added to "
                "the netmap aux graph as a :WebEndpoint{source='cert',probed=false} "
                "under a :Website for the domain (no active probing). "
                "Input: domain (str!), include_expired (bool=true), "
                "timeout (float=20), limit (int=1000). "
                "Output: {domain, hosts:[...], count}.",
)
async def cap_netscan_cert_scrape(domain: str,
                                   include_expired: bool = True,
                                   timeout: float = 20.0,
                                   limit: int = 1000,
                                   trace_id=None) -> Dict:
    if not domain:
        return {"error": "domain required"}
    from urllib.parse import urlparse
    d = domain.strip().lower()
    if "://" in d:
        d = urlparse(d).netloc or d
    d = d.split("/")[0].strip(".")
    if not d:
        return {"error": "invalid domain"}

    url = f"https://crt.sh/?q=%25.{d}&output=json"
    if not include_expired:
        url += "&exclude=expired"

    rows: Any = []
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": "VeraNetScan/1.0"}) as c:
            resp = await c.get(url)
            if resp.status_code != 200:
                return {"error": f"crt.sh HTTP {resp.status_code}",
                        "domain": d, "hosts": [], "count": 0}
            try:
                rows = resp.json()
            except Exception:
                # crt.sh sometimes returns concatenated objects rather than an array
                txt = resp.text.strip()
                try:
                    rows = json.loads("[" + txt.replace("}\n{", "},{") + "]")
                except Exception:
                    rows = []
    except Exception as e:
        return {"error": f"crt.sh fetch failed: {e}",
                "domain": d, "hosts": [], "count": 0}

    # Extract + dedupe hostnames from name_value (newline-separated) + common_name
    hosts: set = set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        candidates = str(r.get("name_value") or "").split("\n")
        candidates.append(str(r.get("common_name") or ""))
        for raw in candidates:
            h = raw.strip().lower().lstrip("*.").strip(".")
            if not h or " " in h or "@" in h:
                continue
            if h == d or h.endswith("." + d):
                hosts.add(h)
        if len(hosts) >= limit:
            break
    host_list = sorted(hosts)[:limit]

    # Persist to the aux graph: one Website + a WebEndpoint per discovered host.
    site_url = f"https://{d}"
    sid = _site_id(site_url)
    try:
        await _aux_run(
            """
            MERGE (s:Website {id:$sid})
            SET s.origin=$origin, s.updated_at=$ts,
                s.cert_hosts=$n, s.has_cert_scan=true
            """,
            sid=sid, origin=site_url, ts=now_iso(), n=len(host_list),
        )
        for h in host_list:
            hurl = f"https://{h}"
            await _aux_run(
                """
                MERGE (e:WebEndpoint {id:$eid})
                SET e.url=$url, e.host=$host, e.source='cert',
                    e.discovered_via='ct_log', e.probed=false, e.updated_at=$ts
                WITH e
                MATCH (s:Website {id:$sid})
                MERGE (s)-[:HAS_ENDPOINT]->(e)
                """,
                eid=_endpoint_id(hurl), url=hurl, host=h, ts=now_iso(), sid=sid,
            )
    except Exception as e:
        log.debug("cert_scrape aux persist: %s", e)

    # Mirror into the fabric for Loom processing.
    try:
        await _save_scan_to_fabric(
            "netscan_cert",
            [{"id": f"cert:{h}", "host": h, "domain": d,
              "url": f"https://{h}", "scan_type": "cert",
              "discovered_via": "ct_log"}
             for h in host_list],
        )
    except Exception as e:
        log.debug("cert_scrape fabric save: %s", e)

    await emit_event({"type": "netscan.cert.done",
                      "domain": d, "count": len(host_list)})
    return {"domain": d, "hosts": host_list, "count": len(host_list)}


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
                "Input: target (str!), max_hops (int=20), timeout (int=30), "
                "tag_asn (bool=false — look up the ASN of each responding hop and "
                "link :NetHop-[:IN_ASN]->:ASN so the path's network boundaries "
                "show). Output: {target, hops:[{hop,ip,latency_ms,asn}], elapsed_ms}.",
)
async def cap_netscan_traceroute(target: str, max_hops: int = 20,
                                  timeout: int = 30, tag_asn: bool = False,
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
            # Optionally tag the hop with its ASN so network boundaries show
            if tag_asn and not (ipaddress.ip_address(h["ip"]).is_private
                                if _looks_like_ip(h["ip"]) else True):
                try:
                    _enr = await _enrich_ip(h["ip"])
                    _asn = _enr.get("asn")
                    h["asn"] = _asn
                    h["as_name"] = _enr.get("as_name", "")
                    if _asn:
                        await _aux_run(
                            "MERGE (a:ASN {id:$aid}) SET a.asn=$asn, a.label=$lbl, "
                            "a.source=coalesce(a.source,'traceroute'), a.updated_at=$ts",
                            aid=f"asn:{_asn}", asn=str(_asn), lbl=f"AS{_asn}", ts=now_iso())
                        await _aux_run(
                            "MATCH (n:NetHop {id:$hid}),(a:ASN {id:$aid}) "
                            "MERGE (n)-[:IN_ASN]->(a)",
                            hid=hop_id, aid=f"asn:{_asn}")
                        await _write_enrichment_to_graph(h["ip"], _enr)
                except Exception:
                    pass
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
    # ── Exposed files / directories ──────────────────────────────────────────
    "exposed_files":     'intext:"index of /" "parent directory"',
    "config_files":      'ext:env OR ext:conf OR ext:cnf OR ext:ini',
    "sql_dumps":         'ext:sql intext:"INSERT INTO" -github.com',
    "open_directories":  'intitle:"index of" -intext:"github"',
    "backup_files":      'ext:bak OR ext:old OR ext:backup OR ext:swp OR inurl:backup',
    "log_files":         'ext:log intext:password OR intext:error',
    "phpinfo":           'intitle:"phpinfo()" "PHP Version"',
    # ── Secrets / credentials / VCS ──────────────────────────────────────────
    "exposed_git":       'inurl:/.git/HEAD OR inurl:/.git/config',
    "exposed_svn":       'inurl:/.svn/entries',
    "exposed_env":       'inurl:/.env intext:DB_PASSWORD OR intext:APP_KEY OR intext:SECRET',
    "dotfiles":          'inurl:/.npmrc OR inurl:/.dockercfg OR inurl:/.aws/credentials',
    "private_keys":      'intext:"BEGIN RSA PRIVATE KEY" OR intext:"BEGIN OPENSSH PRIVATE KEY" ext:key OR ext:pem',
    "secrets_files":     'filetype:json intext:api_key OR intext:client_secret',
    # ── Login / admin / panels ───────────────────────────────────────────────
    "login_pages":       'inurl:login OR inurl:signin OR inurl:admin',
    "wordpress_admin":   'inurl:/wp-admin/ OR inurl:/wp-login.php',
    "admin_panels":      'intitle:"admin" inurl:admin intext:login -site:github.com',
    "phpmyadmin":        'intitle:phpMyAdmin "Welcome to phpMyAdmin" inurl:index.php',
    # ── API / dev surfaces ───────────────────────────────────────────────────
    "swagger_docs":      'inurl:swagger OR inurl:api-docs OR inurl:swagger-ui.html',
    "graphql":           'inurl:/graphql intext:"__schema" OR intext:"query"',
    "api_endpoints":     'inurl:/api/ ext:json OR intitle:"API" inurl:v1 OR inurl:v2',
    # ── Dashboards / CI / observability ──────────────────────────────────────
    "jenkins":           'intitle:"Dashboard [Jenkins]"',
    "grafana":           'intitle:"Grafana" inurl:/login',
    "kibana":            'intitle:"Kibana" "kbn-version"',
    "prometheus":        'intitle:"Prometheus Time Series" inurl:/graph',
    "gitlab":            'intitle:"GitLab" inurl:/users/sign_in',
    "sonarqube":         'intitle:"SonarQube" inurl:/sessions/new',
    "argocd":            'intitle:"Argo CD" inurl:/login',
    "portainer":         'intitle:"Portainer" inurl:/#!/auth',
    # ── Cloud storage / buckets ──────────────────────────────────────────────
    "open_s3":           'site:s3.amazonaws.com',
    "azure_blobs":       'site:blob.core.windows.net',
    "gcs_buckets":       'site:storage.googleapis.com',
    "do_spaces":         'site:digitaloceanspaces.com',
    # ── Documents / data leaks ───────────────────────────────────────────────
    "document_leaks":    'ext:pdf OR ext:docx OR ext:xlsx intext:confidential OR intext:"internal use only"',
    "csv_leaks":         'ext:csv intext:email OR intext:password OR intext:phone',
    # ── Subdomain / takeover hints ───────────────────────────────────────────
    "subdomains":        'site:*.{domain} -www',
    "takeover_hints":    'intext:"NoSuchBucket" OR intext:"There isn\'t a GitHub Pages site here" OR intext:"Domain not found"',
    # ── IoT / devices ────────────────────────────────────────────────────────
    "iot_cameras":       'intitle:"Network Camera" inurl:view/index',
    "webcamxp":          'intitle:"webcamXP" inurl:8080',
    "printers":          'intitle:"HP LaserJet" OR intitle:"PRINTER" inurl:hp/device',
    "routers":           'intitle:"router" intext:"login" inurl:cgi-bin',
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
    # Presets may carry a {domain} placeholder (e.g. subdomain enum). Fill it
    # from the site filter when given, else drop it. When the placeholder is
    # used we don't ALSO prepend a site: operator.
    if "{domain}" in q:
        q = q.replace("{domain}", (site or "").strip())
        site = ""
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
                    # Attach the hit to its registrable :Domain so it doesn't float
                    await _link_website_domain(sid, parsed.netloc)
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
# 8b.  OSINT CAMPAIGNS  — additive/iterative result collection across sessions
#
# Dork / Agent-OSINT runs are fire-and-forget today. A "campaign" is a durable,
# named bucket of de-duplicated hits (keyed by URL) that accumulates across many
# searches and sessions. Stored in the same SQLite db as the map snapshots, and
# (optionally) forwarded to the fabric `osint_dork` dataset so cross-tool query
# keeps working. Each hit tracks first_seen / last_seen / seen_count + which
# queries surfaced it, so re-running a search enriches rather than duplicates.
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_osint_table() -> None:
    conn = _netmap_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS osint_campaigns (
                campaign_id  TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                description  TEXT,
                queries_json TEXT,
                hits_json    TEXT,
                meta_json    TEXT,
                created_at   TEXT,
                updated_at   TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


try:
    _ensure_osint_table()
except Exception as _e:
    log.debug("osint_campaigns table init: %s", _e)


def _osint_host_of(url: str) -> str:
    try:
        return _urlparse.urlparse(url).netloc
    except Exception:
        return ""


def _osint_merge_hits(existing: List[Dict], new_results: List[Dict],
                      query_label: str, ts: str) -> Tuple[List[Dict], int, int]:
    """Merge a batch of search results into the campaign's hit list, de-duped by
    URL. Returns (hits, added, updated)."""
    by_url = {h.get("url"): h for h in existing if h.get("url")}
    added = updated = 0
    for r in new_results or []:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        if url in by_url:
            h = by_url[url]
            h["last_seen"] = ts
            h["seen_count"] = int(h.get("seen_count", 1)) + 1
            for k in ("title", "host", "snippet", "status"):
                if not h.get(k) and r.get(k):
                    h[k] = r.get(k)
            if r.get("tech"):
                h["tech"] = sorted(set(h.get("tech") or []) | set(r.get("tech") or []))
            if query_label and query_label not in (h.get("queries") or []):
                h.setdefault("queries", []).append(query_label)
            updated += 1
        else:
            hit = {
                "url":        url,
                "title":      r.get("title", ""),
                "host":       r.get("host", "") or _osint_host_of(url),
                "snippet":    (r.get("snippet", "") or "")[:500],
                "tech":       r.get("tech") or [],
                "status":     r.get("status", ""),
                "query":      query_label,
                "queries":    [query_label] if query_label else [],
                "first_seen": ts, "last_seen": ts, "seen_count": 1,
            }
            by_url[url] = hit
            existing.append(hit)
            added += 1
    return existing, added, updated


async def _osint_forward_fabric(campaign_name: str, results: List[Dict]) -> None:
    """Best-effort: also push results into the fabric `osint_dork` dataset so the
    research / fabric tools can query them. Never raises."""
    mod = (sys.modules.get("data_fabric")
           or sys.modules.get("Vera.vera.data_fabric"))
    fn = getattr(mod, "ingest_dataset", None) if mod else None
    if not fn:
        return
    records = [{
        "url": r.get("url", ""), "title": r.get("title", ""),
        "host": r.get("host", "") or _osint_host_of(r.get("url", "")),
        "snippet": (r.get("snippet", "") or "")[:500],
        "tech": r.get("tech") or [], "status": r.get("status", ""),
        "campaign": campaign_name, "discovered_at": now_iso(),
    } for r in results if r.get("url")]
    if not records:
        return
    try:
        await fn("osint_dork", records, source="osint_campaign",
                 tags=["osint", "dork", (campaign_name or "")[:40]])
    except Exception as e:
        log.debug("osint fabric forward failed: %s", e)


async def _osint_read_campaign(campaign_id: str) -> Optional[Dict]:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _netmap_db()
        try:
            return conn.execute(
                "SELECT campaign_id, name, description, queries_json, hits_json, "
                "meta_json, created_at, updated_at FROM osint_campaigns "
                "WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
        finally:
            conn.close()
    row = await loop.run_in_executor(None, _read)
    if not row:
        return None
    return {
        "campaign_id": row[0], "name": row[1], "description": row[2] or "",
        "queries": json.loads(row[3] or "[]"),
        "hits":    json.loads(row[4] or "[]"),
        "meta":    json.loads(row[5] or "{}"),
        "created_at": row[6], "updated_at": row[7],
    }


async def _osint_write_campaign(rec: Dict) -> None:
    loop = asyncio.get_running_loop()
    ts = now_iso()
    def _write():
        conn = _netmap_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO osint_campaigns "
                "(campaign_id, name, description, queries_json, hits_json, "
                " meta_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,"
                "  COALESCE((SELECT created_at FROM osint_campaigns WHERE campaign_id=?),?),?)",
                (rec["campaign_id"], rec.get("name", ""), rec.get("description", ""),
                 json.dumps(rec.get("queries", [])), json.dumps(rec.get("hits", [])),
                 json.dumps(rec.get("meta", {})),
                 rec["campaign_id"], ts, ts),
            )
            conn.commit()
        finally:
            conn.close()
    await loop.run_in_executor(None, _write)


@capability(
    "netscan.osint.campaign.list",
    http_method="GET", http_path="/netscan/osint/campaign/list",
    http_tags=["netscan", "osint"], memory="off", silent=True,
    description="List saved OSINT campaigns (durable, additive buckets of "
                "de-duplicated dork/OSINT hits). "
                "Output: {campaigns: [{campaign_id, name, description, hit_count, "
                "query_count, created_at, updated_at}]}.",
)
async def cap_osint_campaign_list(trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _read():
        conn = _netmap_db()
        try:
            rows = conn.execute(
                "SELECT campaign_id, name, description, queries_json, "
                "meta_json, created_at, updated_at FROM osint_campaigns "
                "ORDER BY updated_at DESC"
            ).fetchall()
            out = []
            for r in rows:
                meta = {}
                try: meta = json.loads(r[4] or "{}")
                except Exception: pass
                try: qn = len(json.loads(r[3] or "[]"))
                except Exception: qn = 0
                out.append({
                    "campaign_id": r[0], "name": r[1],
                    "description": r[2] or "",
                    "hit_count":   meta.get("hit_count", 0),
                    "query_count": qn,
                    "created_at":  r[5], "updated_at": r[6],
                })
            return out
        finally:
            conn.close()
    return {"campaigns": await loop.run_in_executor(None, _read)}


@capability(
    "netscan.osint.campaign.create",
    http_method="POST", http_path="/netscan/osint/campaign/create",
    http_tags=["netscan", "osint"],
    description="Create a new (empty) OSINT campaign. "
                "Input: name (str!), description (str). "
                "Output: {ok, campaign_id, name}.",
)
async def cap_osint_campaign_create(name: str = "", description: str = "",
                                    trace_id=None) -> Dict:
    if not (name or "").strip():
        return {"error": "name required"}
    cid = str(uuid.uuid4())
    rec = {"campaign_id": cid, "name": name.strip(), "description": description,
           "queries": [], "hits": [], "meta": {"hit_count": 0}}
    await _osint_write_campaign(rec)
    await emit_event({"type": "netscan.osint.campaign.created",
                      "campaign_id": cid, "name": name})
    return {"ok": True, "campaign_id": cid, "name": name.strip()}


@capability(
    "netscan.osint.campaign.get",
    http_method="POST", http_path="/netscan/osint/campaign/get",
    http_tags=["netscan", "osint"], memory="off",
    description="Fetch one OSINT campaign with its accumulated, de-duplicated "
                "hits. Input: campaign_id (str!). "
                "Output: {campaign_id, name, queries, hits:[{url,title,host,"
                "snippet,tech,status,first_seen,last_seen,seen_count}], counts}.",
)
async def cap_osint_campaign_get(campaign_id: str = "", trace_id=None) -> Dict:
    rec = await _osint_read_campaign(campaign_id)
    if not rec:
        return {"error": f"campaign not found: {campaign_id}"}
    rec["counts"] = {"hits": len(rec.get("hits", [])),
                     "queries": len(rec.get("queries", []))}
    return rec


@capability(
    "netscan.osint.campaign.delete",
    http_method="POST", http_path="/netscan/osint/campaign/delete",
    http_tags=["netscan", "osint"],
    description="Delete an OSINT campaign. Input: campaign_id (str!). Output: {ok}.",
)
async def cap_osint_campaign_delete(campaign_id: str = "", trace_id=None) -> Dict:
    loop = asyncio.get_running_loop()
    def _del():
        conn = _netmap_db()
        try:
            conn.execute("DELETE FROM osint_campaigns WHERE campaign_id=?",
                         (campaign_id,))
            conn.commit()
        finally:
            conn.close()
    await loop.run_in_executor(None, _del)
    return {"ok": True, "campaign_id": campaign_id}


@capability(
    "netscan.osint.campaign.add",
    http_method="POST", http_path="/netscan/osint/campaign/add",
    http_tags=["netscan", "osint"],
    description="Merge a batch of OSINT/dork results into a campaign, de-duped "
                "by URL (existing hits bump seen_count + last_seen). Also forwards "
                "to the fabric `osint_dork` dataset. "
                "Input: campaign_id (str!), results (list[{url,title,host,snippet,"
                "tech,status}]), query (str — label for this batch), "
                "fabric (bool=true). "
                "Output: {ok, added, updated, hit_count}.",
)
async def cap_osint_campaign_add(campaign_id: str = "",
                                 results: Optional[List[Dict]] = None,
                                 query: str = "", fabric: bool = True,
                                 trace_id=None) -> Dict:
    rec = await _osint_read_campaign(campaign_id)
    if not rec:
        return {"error": f"campaign not found: {campaign_id}"}
    results = results or []
    ts = now_iso()
    hits, added, updated = _osint_merge_hits(
        rec.get("hits", []), results, (query or "").strip(), ts)
    rec["hits"] = hits
    if query and query.strip() and query.strip() not in rec.get("queries", []):
        rec.setdefault("queries", []).append(query.strip())
    rec["meta"] = {"hit_count": len(hits)}
    await _osint_write_campaign(rec)
    if fabric and results:
        await _osint_forward_fabric(rec.get("name", ""), results)
    await emit_event({"type": "netscan.osint.campaign.updated",
                      "campaign_id": campaign_id,
                      "added": added, "updated": updated,
                      "hit_count": len(hits)})
    return {"ok": True, "added": added, "updated": updated,
            "hit_count": len(hits)}


@capability(
    "netscan.osint.run",
    http_method="POST", http_path="/netscan/osint/run",
    http_tags=["netscan", "osint"],
    description="Run a dork search (optionally HTTP-fingerprinting each hit) and "
                "merge the results straight into a named campaign — the one-call "
                "additive OSINT path. Creates the campaign if campaign_id is "
                "omitted but campaign_name is given. "
                "Input: query (str) | preset (str), site (str), max_results "
                "(int=25), fingerprint (bool=false), campaign_id (str), "
                "campaign_name (str — used when no id), fabric (bool=true). "
                "Output: {ok, campaign_id, query, found, added, updated, hit_count}.",
)
async def cap_osint_run(query: str = "", preset: str = "", site: str = "",
                        max_results: int = 25, fingerprint: bool = False,
                        campaign_id: str = "", campaign_name: str = "",
                        fabric: bool = True, trace_id=None) -> Dict:
    # Resolve / create the campaign
    if not campaign_id:
        if not (campaign_name or "").strip():
            return {"error": "campaign_id or campaign_name required"}
        created = await cap_osint_campaign_create(name=campaign_name)
        if created.get("error"):
            return created
        campaign_id = created["campaign_id"]
    # Run the search
    if fingerprint:
        base = await cap_netscan_dork_targeted(
            query=query, preset=preset, site=site, max_results=max_results)
        results = base.get("sites", [])
        eff_q = base.get("query", query or preset)
    else:
        base = await cap_netscan_dork(
            query=query, preset=preset, site=site, max_results=max_results)
        if base.get("error"):
            return base
        results = base.get("results", [])
        eff_q = base.get("query", query or preset)
    merged = await cap_osint_campaign_add(
        campaign_id=campaign_id, results=results, query=eff_q, fabric=fabric)
    if merged.get("error"):
        return merged
    return {"ok": True, "campaign_id": campaign_id, "query": eff_q,
            "found": len(results), "added": merged.get("added", 0),
            "updated": merged.get("updated", 0),
            "hit_count": merged.get("hit_count", 0)}


# ═════════════════════════════════════════════════════════════════════════════
# 8c.  IMPORT FROM REGISTERED PROXMOX / DOCKER STORES
#
# The workers/ollama panel already manages Proxmox clusters (sealed creds, via
# proxmox.cluster.list + proxmox.status) and Docker hosts (docker.hosts.list +
# docker.ps over the engine API). Rather than re-typing credentials in the
# netmap, these caps pull from those registries and write the same
# :PVE*/:DockerHost/:Container nodes the manual scans produce — so the network
# map shows exactly what the workers panel manages, in one graph.
# ═════════════════════════════════════════════════════════════════════════════

def _sibling_mod(*names: str):
    for n in names:
        m = sys.modules.get(n)
        if m:
            return m
    return None


async def _import_pve_status_to_graph(cluster_key: str, cname: str,
                                      st: Dict) -> Tuple[int, int]:
    """Write a proxmox.status snapshot into the aux graph using the same node id
    scheme as cap_netscan_proxmox (so manual + imported scans de-dup)."""
    cluster_id = f"pve_cluster:{cname or cluster_key}"
    await _aux_run(
        """
        MERGE (c:PVECluster {id:$id})
        SET c.name=$name, c.updated_at=$ts, c.source='proxmox'
        """,
        id=cluster_id, name=cname or cluster_key, ts=now_iso(),
    )
    n_nodes = n_guests = 0
    for n in st.get("nodes", []) or []:
        nname = n.get("node", "") or n.get("name", "")
        if not nname:
            continue
        nid = f"pve_node:{nname}"
        n_nodes += 1
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
        try:
            ip = socket.gethostbyname(nname)
            if ip:
                await _aux_run(
                    "MATCH (p:PVENode {id:$pid}), (h:NetHost {id:$nid}) "
                    "MERGE (h)-[:SAME_IP]->(p)",
                    pid=nid, nid=f"net:{ip}",
                )
        except Exception:
            pass
    for g in st.get("guests", []) or []:
        nname = g.get("node", "")
        vmid = g.get("vmid", 0)
        if not nname:
            continue
        gid = f"pve_guest:{nname}:{vmid}"
        info = {
            "vmid": vmid, "name": g.get("name", ""),
            "type": g.get("type", ""), "status": g.get("status", ""),
            "node": nname, "cpu": g.get("cpu", 0), "mem": g.get("mem", 0),
            "maxmem": g.get("maxmem", 0),
        }
        n_guests += 1
        await _aux_run(
            """
            MERGE (g:PVEGuest {id:$id})
            SET g += $props, g.updated_at=$ts, g.source='proxmox'
            WITH g
            MATCH (n:PVENode {id:$nid})
            MERGE (n)-[:RUNS]->(g)
            """,
            id=gid, props=info, ts=now_iso(), nid=f"pve_node:{nname}",
        )
    return n_nodes, n_guests


@capability(
    "netscan.proxmox.import",
    http_method="POST", http_path="/netscan/proxmox/import",
    http_tags=["netscan"],
    description="Import Proxmox clusters already registered in the workers/Proxmox "
                "panel (sealed creds) straight into the network graph — no manual "
                "credentials needed. Creates :PVECluster/:PVENode/:PVEGuest with "
                "SAME_IP cross-links. Input: cluster_id (str — one registered "
                "cluster, omit for ALL). Output: {ok, imported:[{cluster,nodes,"
                "guests}], count}.",
)
async def cap_netscan_proxmox_import(cluster_id: str = "", trace_id=None) -> Dict:
    pmod = _sibling_mod("proxmox_capabilities", "Vera.vera.proxmox_capabilities")
    if not pmod:
        return {"error": "proxmox module not loaded — register a cluster in the "
                         "Proxmox panel first", "imported": []}
    list_fn = getattr(pmod, "cap_cluster_list", None)
    status_fn = getattr(pmod, "cap_status", None)
    if not (list_fn and status_fn):
        return {"error": "proxmox capabilities unavailable", "imported": []}
    clusters = (await list_fn()).get("clusters", []) or []
    if cluster_id:
        clusters = [c for c in clusters if c.get("id") == cluster_id]
        if not clusters:
            return {"error": f"registered cluster not found: {cluster_id}",
                    "imported": []}
    imported: List[Dict] = []
    for c in clusters:
        cid = c.get("id")
        if not cid:
            continue
        st = await status_fn(cluster_id=cid)
        if st.get("error"):
            imported.append({"cluster": c.get("label") or c.get("name") or cid,
                             "error": st["error"]})
            continue
        cname = ((st.get("cluster") or {}).get("name")
                 or c.get("label") or c.get("name") or cid)
        nn, ng = await _import_pve_status_to_graph(cid, cname, st)
        imported.append({"cluster": cname, "nodes": nn, "guests": ng})
    await emit_event({"type": "netscan.proxmox.imported",
                      "count": len(imported)})
    return {"ok": True, "imported": imported, "count": len(imported)}


def _fmt_engine_ports(ports: Any) -> str:
    out = []
    for p in ports or []:
        if not isinstance(p, dict):
            continue
        pub = p.get("PublicPort")
        priv = p.get("PrivatePort")
        typ = p.get("Type", "tcp")
        ip = p.get("IP", "")
        if pub:
            out.append(f"{(ip + ':') if ip else ''}{pub}->{priv}/{typ}")
        elif priv:
            out.append(f"{priv}/{typ}")
    # de-dup while preserving order
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s); uniq.append(s)
    return ", ".join(uniq)


async def _import_docker_engine_to_graph(disp_host: str,
                                         rows: List[Dict]) -> int:
    """Write Docker engine /containers/json rows into the aux graph using the
    same node id scheme as cap_netscan_docker."""
    docker_host_id = f"docker:{disp_host}"
    await _aux_run(
        """
        MERGE (h:DockerHost {id:$id})
        SET h.host=$host, h.label=$label, h.updated_at=$ts, h.source='docker'
        """,
        id=docker_host_id, host=disp_host, label=disp_host, ts=now_iso(),
    )
    count = 0
    for row in rows or []:
        cid = row.get("Id") or row.get("ID") or ""
        names = row.get("Names") or []
        name = (names[0] if isinstance(names, list) and names
                else row.get("Name") or "").lstrip("/")
        image = row.get("Image", "")
        status = row.get("Status", "")
        state = row.get("State", "")
        ports = _fmt_engine_ports(row.get("Ports"))
        if not cid:
            continue
        count += 1
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
            ports=ports, ts=now_iso(), hid=docker_host_id, host=disp_host,
        )
    return count


@capability(
    "netscan.docker.import",
    http_method="POST", http_path="/netscan/docker/import",
    http_tags=["netscan"],
    description="Import Docker hosts already registered in the workers/Docker "
                "panel (engine API, no SSH re-entry) into the network graph. "
                "Creates :DockerHost/:Container nodes. Input: host_id (str — one "
                "registered host, omit for ALL). Output: {ok, imported:[{host,"
                "containers}], count}.",
)
async def cap_netscan_docker_import(host_id: str = "", trace_id=None) -> Dict:
    dmod = _sibling_mod("docker_capabilities", "Vera.vera.docker_capabilities")
    if not dmod:
        return {"error": "docker module not loaded — register a host in the "
                         "Docker panel first", "imported": []}
    list_fn = getattr(dmod, "cap_docker_hosts_list", None)
    ps_fn = getattr(dmod, "cap_docker_ps", None)
    if not (list_fn and ps_fn):
        return {"error": "docker capabilities unavailable", "imported": []}
    hosts = (await list_fn()).get("hosts", []) or []
    if host_id:
        hosts = [h for h in hosts if h.get("id") == host_id]
        if not hosts:
            return {"error": f"registered docker host not found: {host_id}",
                    "imported": []}
    imported: List[Dict] = []
    for h in hosts:
        hid = h.get("id")
        disp = h.get("label") or h.get("url") or hid
        ps = await ps_fn(host_id=hid, all=True)
        if ps.get("error"):
            imported.append({"host": disp, "error": ps["error"]})
            continue
        n = await _import_docker_engine_to_graph(disp, ps.get("containers", []))
        imported.append({"host": disp, "containers": n})
    await emit_event({"type": "netscan.docker.imported",
                      "count": len(imported)})
    return {"ok": True, "imported": imported, "count": len(imported)}


# ═════════════════════════════════════════════════════════════════════════════
# 8d.  RECON ORCHESTRATOR — deterministic multi-stage pipeline
#
# Chains the existing scan caps into a staged recon run:
#   sweep/ports → fingerprint live hosts → OSINT enrich domains → infra link.
# Exposed two ways:
#   • netscan.recon.run   — @capability (one tool call the planner / V5 agent
#                           loop can invoke; returns a summary)
#   • POST /netscan/recon/stream — SSE for the panel button (live stage events;
#                           nodes are written to the graph by the underlying caps)
# ═════════════════════════════════════════════════════════════════════════════
async def _recon_stages(
    *, target: str, profile: str = "quick",
    fingerprint: bool = True, osint: bool = False, osint_preset: str = "",
    osint_campaign_id: str = "", osint_campaign_name: str = "",
    osint_max: int = 15, max_hosts: int = 20, link_infra: bool = False,
    enrich: bool = False, map_boundaries: bool = False,
) -> "AsyncGenerator[Tuple[str, Dict], None]":
    target = (target or "").strip()
    if not target:
        yield ("error", {"error": "target required (CIDR, host, IP, or domain)"})
        return
    is_cidr = "/" in target
    yield ("start", {"target": target, "profile": profile,
                     "cidr": is_cidr, "ts": now_iso()})

    hosts: List[Dict] = []
    # ── Stage 1 — discovery ──────────────────────────────────────────────────
    if is_cidr:
        yield ("stage", {"stage": "sweep", "status": "running",
                         "msg": f"LAN sweep {target}"})
        r = await cap_netscan_lan(cidr=target, ping=True, port_nodes=True,
                                  save_to_fabric=False)
        if r.get("error"):
            yield ("error", {"error": r["error"]}); return
        hosts = [{"ip": h["ip"], "hostname": h.get("hostname", ""),
                  "open_ports": h.get("open_ports", [])}
                 for h in r.get("alive", [])]
        yield ("stage", {"stage": "sweep", "status": "done",
                         "live": len(hosts), "msg": f"{len(hosts)} live hosts"})
        for h in hosts:
            yield ("host", h)
    else:
        yield ("stage", {"stage": "ports", "status": "running",
                         "msg": f"port scan {target}"})
        pr = await cap_netscan_target_ports(host=target,
                                            ports=profile or "common")
        if pr.get("error"):
            yield ("stage", {"stage": "ports", "status": "error",
                             "msg": pr.get("error")})
        else:
            op = [p["port"] for p in pr.get("open", [])]
            ip = target
            try: ip = socket.gethostbyname(target)
            except Exception: pass
            hosts = [{"ip": ip, "hostname": (target if target != ip else ""),
                      "open_ports": op}]
            yield ("stage", {"stage": "ports", "status": "done",
                             "open": len(op), "msg": f"{len(op)} open ports"})
            for h in hosts:
                yield ("host", h)

    # ── Stage 2 — fingerprint live hosts ─────────────────────────────────────
    if fingerprint:
        fp_targets = [h for h in hosts if h.get("open_ports")][:max_hosts]
        if fp_targets:
            yield ("stage", {"stage": "fingerprint", "status": "running",
                             "msg": f"fingerprint {len(fp_targets)} host(s)"})
            for h in fp_targets:
                tgt = h.get("hostname") or h.get("ip")
                try:
                    f = await cap_netscan_fingerprint(host=tgt,
                                                      profile=profile or "quick")
                    yield ("fingerprint", {"host": tgt,
                                           "open": f.get("open_count", 0),
                                           "ports": f.get("ports", [])[:20]})
                except Exception as e:
                    yield ("fingerprint", {"host": tgt, "error": str(e)})
            yield ("stage", {"stage": "fingerprint", "status": "done"})

    # ── Link discovered hosts to their registrable :Domain (cheap, always) ───
    for h in hosts:
        hn = h.get("hostname", "")
        if hn and h.get("ip"):
            try: await _link_host_domain(h["ip"], hn)
            except Exception: pass

    # ── Stage 2b — deep OSINT enrichment (geo / ASN / CVEs) ──────────────────
    if enrich and hosts:
        yield ("stage", {"stage": "enrich", "status": "running",
                         "msg": f"enriching {len(hosts)} host(s)"})
        try:
            res = await cap_netscan_enrich_bulk(
                hosts=[h["ip"] for h in hosts if h.get("ip")][:max_hosts])
            yield ("stage", {"stage": "enrich", "status": "done",
                             "enriched": res.get("enriched", 0)})
        except Exception as e:
            yield ("stage", {"stage": "enrich", "status": "error", "msg": str(e)})

    # ── Stage 3 — OSINT enrich resolvable domains ────────────────────────────
    cid = osint_campaign_id
    if osint:
        domains: List[str] = []
        seen = set()
        def _add_dom(d: str):
            d = (d or "").strip().lower()
            if d and "." in d and re.search(r"[a-z]", d) and d not in seen:
                seen.add(d); domains.append(d)
        if not is_cidr and ":" not in target:
            _add_dom(target)
        for h in hosts:
            _add_dom(h.get("hostname", ""))
        domains = domains[:max(1, min(max_hosts, 8))]
        if domains:
            yield ("stage", {"stage": "osint", "status": "running",
                             "msg": f"OSINT {len(domains)} domain(s)"})
            for dom in domains:
                try:
                    res = await cap_osint_run(
                        preset=osint_preset or "",
                        query=("" if osint_preset else "login OR admin OR api OR portal"),
                        site=dom, max_results=osint_max,
                        campaign_id=cid,
                        campaign_name=osint_campaign_name or f"recon:{target}",
                        fingerprint=True)
                    cid = res.get("campaign_id", cid)
                    yield ("osint", {"domain": dom, "found": res.get("found", 0),
                                     "added": res.get("added", 0),
                                     "campaign_id": cid})
                except Exception as e:
                    yield ("osint", {"domain": dom, "error": str(e)})
            yield ("stage", {"stage": "osint", "status": "done",
                             "campaign_id": cid})
        else:
            yield ("stage", {"stage": "osint", "status": "skipped",
                             "msg": "no resolvable domains in scope"})

    # ── Stage 4 — infra cross-link (import registered Proxmox/Docker) ─────────
    if link_infra:
        yield ("stage", {"stage": "link", "status": "running",
                         "msg": "importing registered Proxmox/Docker"})
        p = await cap_netscan_proxmox_import()
        d = await cap_netscan_docker_import()
        yield ("stage", {"stage": "link", "status": "done",
                         "proxmox": p.get("count", 0),
                         "docker": d.get("count", 0)})

    # ── Stage 5 — aggregate hosts into network-boundary (:NetBlock) nodes ─────
    if map_boundaries:
        yield ("stage", {"stage": "boundary", "status": "running",
                         "msg": "aggregating network boundaries"})
        try:
            agg = await cap_netscan_map_aggregate()
            yield ("stage", {"stage": "boundary", "status": "done",
                             "blocks": agg.get("blocks", 0)})
        except Exception as e:
            yield ("stage", {"stage": "boundary", "status": "error", "msg": str(e)})

    yield ("done", {"hosts": len(hosts), "campaign_id": cid, "ts": now_iso()})


@capability(
    "netscan.recon.run",
    http_method="POST", http_path="/netscan/recon/run",
    http_tags=["netscan"],
    description="Run a multi-stage network recon pipeline and write everything to "
                "the graph: sweep/port-scan → fingerprint live hosts → (optional) "
                "OSINT enrich resolvable domains into a campaign → (optional) link "
                "registered Proxmox/Docker infra. One tool the planner / agentic "
                "loop can call. Use netscan.recon.stream for live UI progress. "
                "Input: target (str! — CIDR like 10.0.0.0/24, or a host/IP/domain), "
                "profile (str='quick'|common|web|database|iot|ms), fingerprint "
                "(bool=true), osint (bool=false), osint_preset (str), "
                "osint_campaign_name (str), osint_campaign_id (str), max_hosts "
                "(int=20), link_infra (bool=false), enrich (bool=false — deep "
                "geo/ASN/CVE enrichment of discovered hosts), map_boundaries "
                "(bool=false — aggregate hosts into :NetBlock boundary nodes). "
                "Output: {ok, target, stages, hosts, osint, host_count, campaign_id}.",
)
async def cap_netscan_recon_run(
    target: str = "", profile: str = "quick",
    fingerprint: bool = True, osint: bool = False, osint_preset: str = "",
    osint_campaign_id: str = "", osint_campaign_name: str = "",
    max_hosts: int = 20, link_infra: bool = False,
    enrich: bool = False, map_boundaries: bool = False, trace_id=None,
) -> Dict:
    summary: Dict[str, Any] = {"target": target, "stages": [], "hosts": [],
                               "osint": [], "fingerprints": []}
    campaign_id = osint_campaign_id
    async for ev, data in _recon_stages(
        target=target, profile=profile, fingerprint=fingerprint, osint=osint,
        osint_preset=osint_preset, osint_campaign_id=osint_campaign_id,
        osint_campaign_name=osint_campaign_name, max_hosts=max_hosts,
        link_infra=link_infra, enrich=enrich, map_boundaries=map_boundaries,
    ):
        if ev == "error":
            return {"error": data.get("error"), **summary}
        elif ev == "stage":
            summary["stages"].append(data)
        elif ev == "host":
            summary["hosts"].append(data)
        elif ev == "fingerprint":
            summary["fingerprints"].append(data)
        elif ev == "osint":
            summary["osint"].append(data)
        elif ev == "done":
            campaign_id = data.get("campaign_id", campaign_id)
    return {"ok": True, **summary, "host_count": len(summary["hosts"]),
            "campaign_id": campaign_id}


@APP.post("/netscan/recon/stream", tags=["netscan"], include_in_schema=True,
          summary="SSE-stream a multi-stage recon run (sweep → fingerprint → "
                  "OSINT → link); nodes appear in the graph as caps write them.")
async def recon_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    async def _gen() -> AsyncGenerator[bytes, None]:
        try:
            async for ev, data in _recon_stages(
                target=(body.get("target") or "").strip(),
                profile=body.get("profile") or "quick",
                fingerprint=bool(body.get("fingerprint", True)),
                osint=bool(body.get("osint", False)),
                osint_preset=body.get("osint_preset") or "",
                osint_campaign_id=body.get("osint_campaign_id") or "",
                osint_campaign_name=body.get("osint_campaign_name") or "",
                osint_max=int(body.get("osint_max", 15) or 15),
                max_hosts=int(body.get("max_hosts", 20) or 20),
                link_infra=bool(body.get("link_infra", False)),
                enrich=bool(body.get("enrich", False)),
                map_boundaries=bool(body.get("map_boundaries", False)),
            ):
                if await request.is_disconnected():
                    return
                yield _sse(ev, data)
        except Exception as e:
            yield _sse("error", {"error": f"{type(e).__name__}: {e}"})

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-cache"})


# ═════════════════════════════════════════════════════════════════════════════
# 8e.  ADDRESS / HOST OSINT ENRICHMENT  +  DOMAIN MODEL  +  NETWORK BOUNDARIES
#
# Enrich an IP/host with geo, ASN, ISP/org, allocation CIDR and open-ports/CVE
# intel from free, no-key sources (cached locally so repeat lookups are free and
# accumulate across sessions). Roll hosts up into :NetBlock → :ASN → :GeoRegion
# boundary nodes, and attach :Website / :NetHost to a :Domain so OSINT/recon hits
# stop floating. Optional opt-in ASN/prefix/peering expansion via RIPEstat.
#
# Sources: ip-api.com (geo+ASN, 100-IP batch), Shodan InternetDB
# (internetdb.shodan.io — ports/CVEs/tags), RDAP (rdap.org — allocation CIDR),
# RIPEstat (stat.ripe.net — announced prefixes + AS neighbours).
# ═════════════════════════════════════════════════════════════════════════════
ENRICH_TTL = int(os.getenv("VERA_ENRICH_TTL", str(7 * 86400)))   # 7 days


def _looks_like_ip(s: str) -> bool:
    try:
        ipaddress.ip_address((s or "").strip())
        return True
    except Exception:
        return False


def _ensure_ipcache_table() -> None:
    conn = _netmap_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS osint_ip_cache (
                ip         TEXT PRIMARY KEY,
                json       TEXT,
                fetched_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


try:
    _ensure_ipcache_table()
except Exception as _e:
    log.debug("osint_ip_cache table init: %s", _e)


def _ip_cache_get(ip: str, ttl: int = ENRICH_TTL) -> Optional[Dict]:
    try:
        conn = _netmap_db()
        try:
            row = conn.execute(
                "SELECT json, fetched_at FROM osint_ip_cache WHERE ip=?", (ip,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        data = json.loads(row[0] or "{}")
        # TTL check
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat((row[1] or "").replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if ttl and age > ttl:
                return None
        except Exception:
            pass
        return data
    except Exception:
        return None


def _ip_cache_put(ip: str, data: Dict) -> None:
    try:
        conn = _netmap_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO osint_ip_cache (ip, json, fetched_at) "
                "VALUES (?,?,?)",
                (ip, json.dumps(data), now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("ip cache put failed: %s", e)


def _parse_ipapi(j: Dict) -> Dict:
    """Normalise an ip-api.com record (single or batch item)."""
    if not isinstance(j, dict) or j.get("status") != "success":
        return {}
    asn, asname = "", j.get("asname", "") or ""
    m = re.match(r"AS(\d+)\s*(.*)", str(j.get("as", "") or ""))
    if m:
        asn = m.group(1)
        asname = asname or m.group(2)
    return {
        "country":      j.get("country", ""),
        "country_code": j.get("countryCode", ""),
        "city":         j.get("city", ""),
        "region":       j.get("regionName", ""),
        "lat":          j.get("lat"),
        "lon":          j.get("lon"),
        "isp":          j.get("isp", ""),
        "org":          j.get("org", ""),
        "asn":          asn,
        "as_name":      asname,
        "rdns":         j.get("reverse", "") or "",
    }


_IPAPI_FIELDS = ("status,message,country,countryCode,city,regionName,lat,lon,"
                 "isp,org,as,asname,reverse,query")


async def _fetch_ipapi(ip: str) -> Dict:
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"http://ip-api.com/json/{ip}?fields={_IPAPI_FIELDS}")
            return _parse_ipapi(r.json())
    except Exception:
        return {}


async def _fetch_ipapi_batch(ips: List[str]) -> Dict[str, Dict]:
    """ip-api batch endpoint — up to 100 IPs in one request. Returns {ip: enr}."""
    if not ips:
        return {}
    payload = [{"query": ip, "fields": _IPAPI_FIELDS} for ip in ips]
    out: Dict[str, Dict] = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post("http://ip-api.com/batch", json=payload)
            data = r.json()
        if isinstance(data, list):
            for item in data:
                q = item.get("query")
                if q:
                    out[q] = _parse_ipapi(item)
    except Exception:
        return out
    return out


async def _fetch_internetdb(ip: str) -> Dict:
    """Shodan InternetDB — free, no key. 404 = nothing known for this IP."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"https://internetdb.shodan.io/{ip}")
            if r.status_code == 404:
                return {}
            if r.status_code >= 400:
                return {}
            j = r.json()
    except Exception:
        return {}
    if not isinstance(j, dict):
        return {}
    return {
        "shodan_ports":     [p for p in (j.get("ports") or []) if isinstance(p, int)],
        "shodan_hostnames": [h for h in (j.get("hostnames") or []) if isinstance(h, str)],
        "shodan_cpes":      [c for c in (j.get("cpes") or []) if isinstance(c, str)],
        "shodan_tags":      [t for t in (j.get("tags") or []) if isinstance(t, str)],
        "shodan_vulns":     [v for v in (j.get("vulns") or []) if isinstance(v, str)],
    }


def _range_to_cidr(start: str, end: str) -> str:
    """Smallest single CIDR covering an IP range (the allocation boundary)."""
    try:
        a = ipaddress.ip_address(start)
        b = ipaddress.ip_address(end)
        plen = a.max_prefixlen
        while plen >= 0:
            cand = ipaddress.ip_network(f"{a}/{plen}", strict=False)
            if b in cand:
                return str(cand)
            plen -= 1
    except Exception:
        pass
    return ""


def _vcard_fn(vcard: Any) -> str:
    try:
        for item in vcard[1]:
            if item and item[0] == "fn":
                return str(item[3])
    except Exception:
        pass
    return ""


def _rdap_org(j: Dict) -> str:
    for ent in j.get("entities") or []:
        name = _vcard_fn(ent.get("vcardArray"))
        if name:
            return name
    return ""


async def _fetch_rdap(ip: str) -> Dict:
    """RDAP — allocation CIDR boundary + netname + org + country (no key)."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
            r = await c.get(f"https://rdap.org/ip/{ip}",
                            headers={"Accept": "application/rdap+json"})
            if r.status_code >= 400:
                return {}
            j = r.json()
    except Exception:
        return {}
    if not isinstance(j, dict):
        return {}
    cidr = ""
    for blk in j.get("cidr0_cidrs") or []:
        pfx = blk.get("v4prefix") or blk.get("v6prefix")
        ln = blk.get("length")
        if pfx and ln is not None:
            cidr = f"{pfx}/{ln}"
            break
    if not cidr and j.get("startAddress") and j.get("endAddress"):
        cidr = _range_to_cidr(j["startAddress"], j["endAddress"])
    return {
        "netblock_cidr": cidr,
        "netname":       j.get("name", "") or "",
        "rir_country":   j.get("country", "") or "",
        "rir_org":       _rdap_org(j),
    }


async def _enrich_ip(ip: str, force: bool = False) -> Dict:
    """Full per-IP enrichment (cached). Never raises."""
    if not force:
        cached = _ip_cache_get(ip, ENRICH_TTL)
        if cached is not None:
            return cached
    ipapi, idb, rdap = await asyncio.gather(
        _fetch_ipapi(ip), _fetch_internetdb(ip), _fetch_rdap(ip),
        return_exceptions=False,
    )
    enr: Dict[str, Any] = {"ip": ip}
    enr.update(ipapi or {})
    enr.update(idb or {})
    enr.update(rdap or {})
    if not enr.get("rdns"):
        enr["rdns"] = await _reverse_dns(ip)
    if not enr.get("org"):
        enr["org"] = enr.get("rir_org", "") or enr.get("netname", "")
    enr["fetched_at"] = now_iso()
    _ip_cache_put(ip, enr)
    return enr


async def _write_enrichment_to_graph(ip: str, enr: Dict) -> None:
    """Persist enrichment as NetHost props + :ASN/:NetBlock/:GeoRegion boundaries."""
    host_id = f"net:{ip}"
    scalar_keys = ("country", "country_code", "city", "region", "lat", "lon",
                   "isp", "org", "asn", "as_name", "netblock_cidr", "netname",
                   "rdns")
    props = {k: enr.get(k) for k in scalar_keys
             if enr.get(k) not in (None, "")}
    # Frontend grouping reads geo_country / geo_city directly
    if enr.get("country"):
        props["geo_country"] = enr["country"]
    if enr.get("city"):
        props["geo_city"] = enr["city"]
    for k in ("shodan_ports", "shodan_vulns", "shodan_tags", "shodan_hostnames"):
        v = enr.get(k)
        if isinstance(v, list) and v:
            props[k] = [x for x in v if isinstance(x, (str, int, float, bool))]
    await _aux_run(
        "MERGE (h:NetHost {id:$id}) SET h += $props, h.enriched_at=$ts, "
        "h.ip=coalesce(h.ip,$ip)",
        id=host_id, props=props, ts=now_iso(), ip=ip,
    )
    asn = enr.get("asn")
    cidr = enr.get("netblock_cidr")
    country = enr.get("country")
    if asn:
        await _aux_run(
            "MERGE (a:ASN {id:$id}) SET a.asn=$asn, a.name=$name, "
            "a.label=$lbl, a.source=coalesce(a.source,'enrich'), a.updated_at=$ts",
            id=f"asn:{asn}", asn=str(asn),
            name=enr.get("as_name", "") or enr.get("org", ""),
            lbl=f"AS{asn}", ts=now_iso(),
        )
        if cidr:
            await _aux_run(
                """
                MERGE (b:NetBlock {id:$bid})
                SET b.cidr=$cidr, b.label=$cidr, b.name=$name,
                    b.source=coalesce(b.source,'enrich'), b.updated_at=$ts
                WITH b
                MATCH (h:NetHost {id:$hid}) MERGE (h)-[:IN_PREFIX]->(b)
                WITH b
                MATCH (a:ASN {id:$aid}) MERGE (b)-[:ANNOUNCED_BY]->(a)
                """,
                bid=f"block:{cidr}", cidr=cidr, name=enr.get("netname", ""),
                ts=now_iso(), hid=host_id, aid=f"asn:{asn}",
            )
        else:
            await _aux_run(
                "MATCH (h:NetHost {id:$hid}),(a:ASN {id:$aid}) "
                "MERGE (h)-[:IN_ASN]->(a)",
                hid=host_id, aid=f"asn:{asn}",
            )
    elif cidr:
        await _aux_run(
            """
            MERGE (b:NetBlock {id:$bid})
            SET b.cidr=$cidr, b.label=$cidr,
                b.source=coalesce(b.source,'enrich'), b.updated_at=$ts
            WITH b MATCH (h:NetHost {id:$hid}) MERGE (h)-[:IN_PREFIX]->(b)
            """,
            bid=f"block:{cidr}", cidr=cidr, ts=now_iso(), hid=host_id,
        )
    if country:
        await _aux_run(
            """
            MERGE (g:GeoRegion {id:$gid})
            SET g.country=$country, g.code=$cc, g.label=$country,
                g.source=coalesce(g.source,'enrich'), g.updated_at=$ts
            WITH g MATCH (h:NetHost {id:$hid}) MERGE (h)-[:LOCATED_IN]->(g)
            """,
            gid=f"geo:{country}", country=country,
            cc=enr.get("country_code", ""), ts=now_iso(), hid=host_id,
        )


# ── Domain model ─────────────────────────────────────────────────────────────
# A small public-suffix set so we group by the *registrable* domain (so
# a.example.co.uk and b.example.co.uk roll up to example.co.uk, not co.uk).
_MULTI_TLDS = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "net.nz", "org.nz",
    "co.za", "org.za", "com.br", "net.br", "co.jp", "or.jp", "ne.jp", "co.in",
    "co.kr", "com.cn", "net.cn", "org.cn", "com.mx", "com.sg", "com.tr", "com.tw",
    "co.il", "com.hk", "com.ua", "co.id", "com.my", "com.ph", "com.ar",
}


def _registrable_domain(host: str) -> str:
    host = (host or "").strip().strip(".").lower()
    if not host or _looks_like_ip(host):
        return ""
    parts = host.split(".")
    if len(parts) < 2:
        return ""
    last2 = ".".join(parts[-2:])
    if last2 in _MULTI_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


async def _upsert_domain(domain: str) -> None:
    if not domain:
        return
    await _aux_run(
        "MERGE (d:Domain {id:$id}) SET d.domain=$d, d.label=$d, "
        "d.source=coalesce(d.source,'osint'), d.updated_at=$ts",
        id=f"domain:{domain}", d=domain, ts=now_iso(),
    )


async def _link_website_domain(website_id: str, netloc: str) -> None:
    host = (netloc or "").split("@")[-1].split(":")[0]
    dom = _registrable_domain(host)
    if not dom:
        return
    await _upsert_domain(dom)
    await _aux_run(
        "MATCH (d:Domain {id:$did}),(s:Website {id:$sid}) "
        "MERGE (d)-[:HAS_SITE]->(s)",
        did=f"domain:{dom}", sid=website_id,
    )
    # Attach the site to any host already known by this exact hostname
    await _aux_run(
        "MATCH (h:NetHost) WHERE h.hostname=$host "
        "MERGE (h)-[:SERVES]->(s:Website {id:$sid})",
        host=host, sid=website_id,
    )


async def _link_host_domain(ip: str, hostname: str) -> None:
    dom = _registrable_domain(hostname)
    if not dom:
        return
    await _upsert_domain(dom)
    await _aux_run(
        "MATCH (d:Domain {id:$did}),(h:NetHost {id:$hid}) "
        "MERGE (d)-[:HAS_SUBDOMAIN]->(h)",
        did=f"domain:{dom}", hid=f"net:{ip}",
    )


@capability(
    "netscan.enrich.host",
    http_method="POST", http_path="/netscan/enrich/host",
    http_tags=["netscan", "osint"],
    description="Deep-enrich a single host/IP with geo (country/city/lat/lon), "
                "ASN + ISP/org, RDAP allocation CIDR (network boundary), reverse "
                "DNS, and Shodan InternetDB open ports / CVEs / tags. Writes "
                "properties onto the :NetHost and creates :ASN/:NetBlock/:GeoRegion "
                "boundary nodes. Results cached locally. "
                "Input: host (str! — IP or hostname), force (bool=false — bypass "
                "cache), write_graph (bool=true). "
                "Output: {ok, ip, country, city, asn, as_name, org, netblock_cidr, "
                "shodan_ports, shodan_vulns, ...}.",
)
async def cap_netscan_enrich_host(host: str = "", force: bool = False,
                                  write_graph: bool = True, trace_id=None) -> Dict:
    if not (host or "").strip():
        return {"error": "host required"}
    ip = host.strip()
    if not _looks_like_ip(ip):
        try:
            ip = socket.gethostbyname(host.strip())
        except Exception:
            return {"error": f"could not resolve host: {host}"}
    enr = await _enrich_ip(ip, force=force)
    enr = {**enr, "host": host, "ip": ip}
    if write_graph:
        await _aux_upsert_nethost(ip, hostname=(host if host != ip else ""),
                                  source="enrich")
        await _write_enrichment_to_graph(ip, enr)
        if host != ip:
            await _link_host_domain(ip, host)
    await emit_event({"type": "netscan.enrich.done", "ip": ip,
                      "asn": enr.get("asn"), "country": enr.get("country")})
    return {"ok": True, **enr}


@capability(
    "netscan.enrich.bulk",
    http_method="POST", http_path="/netscan/enrich/bulk",
    http_tags=["netscan", "osint"],
    description="Enrich many hosts/IPs at once (geo + ASN via ip-api batch, plus "
                "Shodan InternetDB ports/CVEs), writing props + boundary nodes for "
                "each. Use after a sweep to roll a whole subnet up into ASN/geo "
                "boundaries. Input: hosts (list[str]!), force (bool=false), "
                "max_hosts (int=128). Output: {ok, enriched, hosts:[{ip,asn,"
                "country,ports}]}.",
)
async def cap_netscan_enrich_bulk(hosts: Optional[List[str]] = None,
                                  force: bool = False, max_hosts: int = 128,
                                  trace_id=None) -> Dict:
    hosts = hosts or []
    ipmap: Dict[str, str] = {}
    for h in hosts[:max_hosts]:
        ip = (h or "").strip()
        if not ip:
            continue
        if not _looks_like_ip(ip):
            try:
                ip = socket.gethostbyname(ip)
            except Exception:
                continue
        ipmap.setdefault(ip, h)
    ips = list(ipmap.keys())
    uncached = [ip for ip in ips
                if force or _ip_cache_get(ip, ENRICH_TTL) is None]
    # Batch geo/ASN
    batch: Dict[str, Dict] = {}
    for i in range(0, len(uncached), 100):
        batch.update(await _fetch_ipapi_batch(uncached[i:i + 100]))
    # InternetDB (concurrency-limited)
    idb_map: Dict[str, Dict] = {}
    if uncached:
        sem = asyncio.Semaphore(8)
        async def _one(ip):
            async with sem:
                return ip, await _fetch_internetdb(ip)
        for ip, d in await asyncio.gather(*[_one(ip) for ip in uncached]):
            idb_map[ip] = d
    out: List[Dict] = []
    for ip in ips:
        enr = None if force else _ip_cache_get(ip, ENRICH_TTL)
        if enr is None:
            enr = {"ip": ip}
            enr.update(batch.get(ip, {}))
            enr.update(idb_map.get(ip, {}))
            if not enr.get("rdns"):
                enr["rdns"] = await _reverse_dns(ip)
            enr["fetched_at"] = now_iso()
            _ip_cache_put(ip, enr)
        hostname = ipmap[ip] if ipmap[ip] != ip else ""
        await _aux_upsert_nethost(ip, hostname=hostname, source="enrich")
        await _write_enrichment_to_graph(ip, enr)
        if hostname:
            await _link_host_domain(ip, hostname)
        out.append({"ip": ip, "asn": enr.get("asn"),
                    "country": enr.get("country"),
                    "ports": enr.get("shodan_ports", [])})
    await emit_event({"type": "netscan.enrich.bulk.done", "count": len(out)})
    return {"ok": True, "enriched": len(out), "hosts": out}


@capability(
    "netscan.graph.relink",
    http_method="POST", http_path="/netscan/graph/relink",
    http_tags=["netscan", "osint"],
    description="Repair the graph: attach every existing :Website and hostname-"
                "bearing :NetHost to its registrable :Domain (HAS_SITE / "
                "HAS_SUBDOMAIN), and link sites to the hosts that serve them "
                "(SERVES). Fixes OSINT/recon nodes that were left floating. "
                "Input: none. Output: {ok, domains_linked, sites_linked, served}.",
)
async def cap_netscan_graph_relink(trace_id=None) -> Dict:
    sites = await _aux_read(
        "MATCH (s:Website) RETURN s.id AS id, "
        "coalesce(s.origin, s.url, '') AS origin")
    sites_linked = 0
    for r in sites:
        origin = r.get("origin") or ""
        netloc = ""
        try:
            netloc = _urlparse.urlparse(origin).netloc or origin.split("//")[-1]
        except Exception:
            netloc = origin
        if netloc:
            await _link_website_domain(r["id"], netloc)
            sites_linked += 1
    hosts = await _aux_read(
        "MATCH (h:NetHost) WHERE h.hostname IS NOT NULL AND h.hostname <> '' "
        "RETURN h.id AS id, h.hostname AS hn, h.ip AS ip")
    hosts_linked = 0
    for r in hosts:
        ip = r.get("ip") or str(r.get("id", "")).split("net:")[-1]
        await _link_host_domain(ip, r.get("hn", ""))
        hosts_linked += 1
    await emit_event({"type": "netscan.graph.relinked",
                      "sites": sites_linked, "hosts": hosts_linked})
    return {"ok": True, "sites_linked": sites_linked,
            "hosts_linked": hosts_linked}


# ── Network boundary mapping (passive aggregate + opt-in RIPEstat expansion) ──
@capability(
    "netscan.map.aggregate",
    http_method="POST", http_path="/netscan/map/aggregate",
    http_tags=["netscan"],
    description="Group every discovered :NetHost into its network boundary: use "
                "the RDAP allocation CIDR when enriched, else a /N prefix. Creates "
                ":NetBlock nodes with IN_PREFIX edges (and ANNOUNCED_BY to :ASN "
                "when known). No external calls. Input: prefix_bits (int=24). "
                "Output: {ok, hosts, blocks}.",
)
async def cap_netscan_map_aggregate(prefix_bits: int = 24, trace_id=None) -> Dict:
    rows = await _aux_read(
        "MATCH (h:NetHost) RETURN h.id AS id, h.ip AS ip, "
        "h.netblock_cidr AS cidr, h.asn AS asn")
    blocks = set()
    n = 0
    for r in rows:
        ip = r.get("ip")
        if not ip:
            continue
        cidr = r.get("cidr")
        if not cidr:
            try:
                cidr = str(ipaddress.ip_network(f"{ip}/{int(prefix_bits)}",
                                                strict=False))
            except Exception:
                continue
        blocks.add(cidr)
        await _aux_run(
            """
            MERGE (b:NetBlock {id:$bid})
            SET b.cidr=$cidr, b.label=$cidr,
                b.source=coalesce(b.source,'aggregate'), b.updated_at=$ts
            WITH b MATCH (h:NetHost {id:$hid}) MERGE (h)-[:IN_PREFIX]->(b)
            """,
            bid=f"block:{cidr}", cidr=cidr, ts=now_iso(), hid=r["id"],
        )
        if r.get("asn"):
            await _aux_run(
                "MATCH (b:NetBlock {id:$bid}),(a:ASN {id:$aid}) "
                "MERGE (b)-[:ANNOUNCED_BY]->(a)",
                bid=f"block:{cidr}", aid=f"asn:{r['asn']}",
            )
        n += 1
    await emit_event({"type": "netscan.map.aggregated",
                      "hosts": n, "blocks": len(blocks)})
    return {"ok": True, "hosts": n, "blocks": len(blocks)}


async def _ripe_get(path: str, asn: str) -> Optional[Dict]:
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.get(f"https://stat.ripe.net/data/{path}/data.json",
                            params={"resource": f"AS{asn}"})
            if r.status_code >= 400:
                return None
            return r.json()
    except Exception:
        return None


@capability(
    "netscan.asn.expand",
    http_method="POST", http_path="/netscan/asn/expand",
    http_tags=["netscan"],
    description="Map an ASN's footprint: pull its BGP announced prefixes (and "
                "optionally its peer ASNs) from RIPEstat and graph them as "
                ":NetBlock under the :ASN with PEERS_WITH links. Reveals the "
                "boundaries of a whole network. Input: asn (str — e.g. 15169 or "
                "AS15169) OR host (str — resolve its ASN), peering (bool=true), "
                "max_prefixes (int=200). Output: {ok, asn, prefixes, peers}.",
)
async def cap_netscan_asn_expand(asn: str = "", host: str = "",
                                 peering: bool = True, max_prefixes: int = 200,
                                 trace_id=None) -> Dict:
    if not asn and host:
        ip = host.strip()
        if not _looks_like_ip(ip):
            try:
                ip = socket.gethostbyname(host.strip())
            except Exception:
                ip = ""
        if ip:
            asn = (await _enrich_ip(ip)).get("asn", "")
    asn = str(asn).upper().replace("AS", "").strip()
    if not asn.isdigit():
        return {"error": "valid asn (or resolvable host) required"}
    await _aux_run(
        "MERGE (a:ASN {id:$id}) SET a.asn=$asn, a.label=$lbl, "
        "a.source=coalesce(a.source,'expand'), a.updated_at=$ts",
        id=f"asn:{asn}", asn=asn, lbl=f"AS{asn}", ts=now_iso(),
    )
    prefixes = 0
    ap = await _ripe_get("announced-prefixes", asn)
    for item in (((ap or {}).get("data") or {}).get("prefixes") or []):
        cidr = item.get("prefix")
        if not cidr:
            continue
        await _aux_run(
            """
            MERGE (b:NetBlock {id:$bid})
            SET b.cidr=$cidr, b.label=$cidr, b.source='expand', b.updated_at=$ts
            WITH b MATCH (a:ASN {id:$aid}) MERGE (b)-[:ANNOUNCED_BY]->(a)
            """,
            bid=f"block:{cidr}", cidr=cidr, ts=now_iso(), aid=f"asn:{asn}",
        )
        prefixes += 1
        if prefixes >= int(max_prefixes):
            break
    peers = 0
    if peering:
        nb = await _ripe_get("asn-neighbours", asn)
        for item in (((nb or {}).get("data") or {}).get("neighbours") or []):
            pasn = str(item.get("asn", "")).strip()
            if not pasn.isdigit():
                continue
            await _aux_run(
                """
                MERGE (p:ASN {id:$pid})
                SET p.asn=$pasn, p.label=$lbl,
                    p.source=coalesce(p.source,'peer'), p.updated_at=$ts
                WITH p MATCH (a:ASN {id:$aid}) MERGE (a)-[:PEERS_WITH]->(p)
                """,
                pid=f"asn:{pasn}", pasn=pasn, lbl=f"AS{pasn}",
                ts=now_iso(), aid=f"asn:{asn}",
            )
            peers += 1
    await emit_event({"type": "netscan.asn.expanded", "asn": asn,
                      "prefixes": prefixes, "peers": peers})
    return {"ok": True, "asn": asn, "prefixes": prefixes, "peers": peers}


# ═════════════════════════════════════════════════════════════════════════════
# 9.  CLEAR-ALL / NEW-GRAPH BUTTON
# ═════════════════════════════════════════════════════════════════════════════
_NETSCAN_LABELS_FALLBACK = (
    "NetHost", "Subnet", "DockerHost", "Container",
    "PVECluster", "PVENode", "PVEGuest",
    "K8sCluster", "K8sNode", "K8sPod",
    "Website", "WebEndpoint",
    "NetPort", "NetHop",
    "Domain", "ASN", "NetBlock", "GeoRegion",
    "WifiAP",
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


# ═════════════════════════════════════════════════════════════════════════════
# CODE ITERATE — write → run → read the failure → fix → run again
# ═════════════════════════════════════════════════════════════════════════════
# The self-correcting twin of exec.code.run: Vera (chat, the V5 loop, a dream
# stage) hands over a snippet plus success criteria, and this cap drives the
# test-and-iterate loop server-side — run, and on failure ask the LLM for a
# fixed version (full-file rewrite), re-run, up to max_iterations. Every run
# and fix is reported via exec.iterate events so the chat/loop UI can show a
# live ticker.

_ITERATE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*[^\n]*\n([\s\S]*?)```")


def _iterate_extract_code(text: str, fallback: str) -> str:
    """First fenced block of an LLM fix reply; whole reply if it looks like
    bare code; otherwise the previous version (no change)."""
    if not text:
        return fallback
    m = _ITERATE_FENCE_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip("\n") + "\n"
    # Bare-code heuristic: no prose lead-in before the first line of code
    t = text.strip()
    if t and not t.lower().startswith(("here", "the ", "i ", "sure", "okay", "ok")):
        return t + "\n"
    return fallback


def _iterate_llm():
    """llm.generate callable via the registry (None if not loaded)."""
    import Vera.vera.capability_orchestration as _o
    entry = _o.CAPABILITY_REGISTRY.get("llm.generate")
    return entry.get("func") if isinstance(entry, dict) else None


@capability(
    "exec.code.iterate",
    http_method="POST", http_path="/exec/code/iterate", http_tags=["exec", "code"],
    description="Run a code snippet and AUTOMATICALLY fix-and-retry until it passes "
                "or the iteration budget runs out — the self-correcting version of "
                "exec.code.run. Pass criteria via `expect` (substring that must "
                "appear in stdout) and/or `test_code` (a separate snippet run after "
                "the main code; its rc!=0 fails the round). With neither, rc==0 "
                "passes. On failure the LLM rewrites the code (guided by `goal`) and "
                "it re-runs. Subject to the exec sandbox policy. "
                "Input: language (str!), code (str!), goal (str — what the code "
                "should do; strongly recommended), expect (str), test_code (str), "
                "max_iterations (int 1-6, default 3), timeout (int sec per run), "
                "model (str — fixer model, blank = default), session_id (str — for "
                "live event routing). "
                "Output: {ok, passed, runs, final_code, iterations:[{n, rc, "
                "stdout, stderr, fixed}], last_stdout, last_stderr}.",
)
async def cap_code_iterate(
    language:       str = "",
    code:           str = "",
    goal:           str = "",
    expect:         str = "",
    test_code:      str = "",
    max_iterations: int = 3,
    timeout:        int = _DEFAULT_TIMEOUT,
    model:          str = "",
    session_id:     str = "",
    trace_id=None,
) -> Dict[str, Any]:
    if not (language or "").strip():
        return {"ok": False, "error": "language required"}
    if not (code or "").strip():
        return {"ok": False, "error": "code required"}
    max_iterations = max(1, min(6, int(max_iterations or 3)))
    llm = _iterate_llm()

    # Opt-in per-session sandbox: every run (and test run) executes INSIDE the
    # session's container when it has an ACTIVE sandbox — same routing as
    # exec.code.run. Falls back to the host otherwise.
    async def _iter_run(snippet: str) -> Dict[str, Any]:
        routed = await _route_session_code(session_id, language, snippet,
                                           "", "", timeout, None)
        if routed is not None:
            return routed
        return await _run_code(language, snippet, timeout=timeout)

    cur = code
    iterations: List[Dict[str, Any]] = []
    passed = False
    last: Dict[str, Any] = {}

    for n in range(1, max_iterations + 1):
        await emit_event({"type": "exec.iterate", "stage": "run", "n": n,
                          "session_id": session_id,
                          "message": f"iteration {n}/{max_iterations}: running "
                                     f"{language} ({len(cur)} chars)"})
        last = await _iter_run(cur)
        rc_ok = bool(last.get("ok")) and int(last.get("rc", 1) or 0) == 0
        exp_ok = (expect in (last.get("stdout") or "")) if expect else True

        test_res: Optional[Dict[str, Any]] = None
        test_ok = True
        if rc_ok and test_code.strip():
            test_res = await _iter_run(test_code)
            test_ok = bool(test_res.get("ok")) and int(test_res.get("rc", 1) or 0) == 0

        entry: Dict[str, Any] = {
            "n": n, "rc": last.get("rc"), "fixed": False,
            "stdout": (last.get("stdout") or "")[:4000],
            "stderr": (last.get("stderr") or "")[:4000],
        }
        if test_res is not None:
            entry["test_rc"] = test_res.get("rc")
            entry["test_stderr"] = (test_res.get("stderr") or "")[:2000]
        iterations.append(entry)

        if rc_ok and exp_ok and test_ok:
            passed = True
            await emit_event({"type": "exec.iterate", "stage": "pass", "n": n,
                              "session_id": session_id,
                              "message": f"passed on iteration {n}"})
            break

        if n == max_iterations or llm is None:
            break

        # ── Ask the LLM for a fixed full version ────────────────────────
        failure = []
        if not rc_ok:
            failure.append(f"exit code {last.get('rc')}")
        if expect and not exp_ok:
            failure.append(f"stdout did not contain the expected text {expect!r}")
        if test_res is not None and not test_ok:
            failure.append(f"the test snippet failed (rc {test_res.get('rc')}): "
                           f"{(test_res.get('stderr') or test_res.get('stdout') or '')[:800]}")
        await emit_event({"type": "exec.iterate", "stage": "fix", "n": n,
                          "session_id": session_id,
                          "message": f"iteration {n} failed ({'; '.join(failure)}) "
                                     f"— asking for a fix"})
        fix_prompt = (
            f"This {language} program failed. Fix it and reply with ONLY the "
            f"complete corrected program in a single fenced code block — no "
            f"explanation.\n\n"
            + (f"GOAL: {goal}\n\n" if goal else "")
            + f"FAILURE: {'; '.join(failure)}\n\n"
            + f"CODE:\n```{language}\n{cur}\n```\n\n"
            + f"STDOUT:\n{(last.get('stdout') or '')[:2000]}\n\n"
            + f"STDERR:\n{(last.get('stderr') or '')[:3000]}\n"
        )
        try:
            fix = await llm(prompt=fix_prompt, model=(model or None),
                            system="You are an expert debugger. Output only the "
                                   "corrected code in one fenced block.",
                            caller="exec.code.iterate")
            new_code = _iterate_extract_code((fix or {}).get("text", ""), cur)
        except Exception as e:
            log.warning("exec.code.iterate fix call failed: %s", e)
            break
        if new_code.strip() == cur.strip():
            await emit_event({"type": "exec.iterate", "stage": "stuck", "n": n,
                              "session_id": session_id,
                              "message": "fixer returned an identical program — stopping"})
            break
        cur = new_code
        iterations[-1]["fixed"] = True

    await emit_event({"type": "exec.iterate", "stage": "done",
                      "session_id": session_id, "passed": passed,
                      "message": f"{'passed' if passed else 'did not pass'} "
                                 f"after {len(iterations)} run(s)"})
    return {"ok": True, "passed": passed, "runs": len(iterations),
            "final_code": cur, "iterations": iterations,
            "last_stdout": (last.get("stdout") or "")[:8000],
            "last_stderr": (last.get("stderr") or "")[:8000]}