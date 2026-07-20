"""
session_sandbox_capabilities.py — per-session Docker sandboxes (Phase 6, opt-in)
================================================================================

Gives a chat / IDE / agentic-loop **session** its own dedicated Docker container
that holds only what it needs to run, can be committed to an image and restored
instantly when the session re-opens, and — when enabled — becomes the ONLY place
that session's shell commands and code execution touch an OS. The orchestrator
host is never used for that session's work.

Opt-in model (the user's choice): a session has a sandbox only after
`sandbox.session.start`; until then everything behaves exactly as before. When a
sandbox is active, exec.bash.run / exec.code.run (and the per-language runners)
transparently route into the container via the `route_shell` / `route_code`
hooks this module exposes — but ONLY for calls that carry that session_id.

Lifecycle (group `sandbox.session.*`)
─────────────────────────────────────
  start    — create/restore the session container (minimal base, optional pkgs)
  status   — is there a sandbox for this session; is it running
  exec     — run a command INSIDE the session container
  run_code — write a snippet into the container and run it
  fs.read / fs.write / fs.list — files inside the container
  commit   — docker commit → vera-session:<sid> (fast restore later)
  stop     — (optionally commit then) stop + remove; the volume + image are kept
  list     — all known session sandboxes

Storage: Redis hash `vera:remote:sandboxes` (session_id → record). The per-session
`/workspace` lives in a named volume so it survives stop/restart even without a
commit; a commit additionally snapshots installed packages.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import os
import shlex
import shutil
import sys
import tarfile
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, enum_schema, schedule,
)

log = logging.getLogger("vera.remote.sandbox")
KEY_SBX = "vera:remote:sandboxes"
KEY_CFG = "vera:remote:sandbox:cfg"   # global sandbox defaults (docker host, base image)
KEY_ALIAS = "vera:remote:sandbox:alias"  # session_id → target session_id (shared containers)

# ─────────────────────────────────────────────────────────────────────────────
#  RUN OWNERSHIP — one container per project / goal / dream pipeline / program.
#
#  A governed run (a dream cycle, a V8 program loop, a project/goal loop) sets
#  the RUN OWNER contextvar to its owning container key ("goal-<slug>",
#  "proj-<slug>", "dream-<pipeline>", "v8-<pid>") for the duration of the run.
#  While it is set:
#    • every sandbox auto-create / explicit sandbox.session.start for ANY other
#      session id is REDIRECTED into the owner's container (the session id is
#      aliased to the owner) — so a run can never fan out into per-stage /
#      per-step junk containers, and NOTHING inside a governed run can nest a
#      second sandbox, whether it calls the cap agentically or from code;
#    • sandbox.session.link is forced to the owner as its target.
#  contextvars propagate through create_task, so one set() at the driver's
#  entry covers the whole run tree. The var holds a JSON blob {owner,kind,label}.
# ─────────────────────────────────────────────────────────────────────────────
RUN_OWNER: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "vera_sandbox_run_owner", default="")


def set_run_owner(owner: str, *, kind: str = "session", label: str = ""):
    """Enter an owner scope. Returns the reset token. Never raises."""
    try:
        return RUN_OWNER.set(json.dumps(
            {"owner": str(owner or ""), "kind": kind or "session",
             "label": label or ""}))
    except Exception:
        return None


def reset_run_owner(token) -> None:
    try:
        if token is not None:
            RUN_OWNER.reset(token)
    except Exception:
        pass


def current_run_owner() -> Dict[str, str]:
    """{owner, kind, label} of the governing run, or {} outside any."""
    raw = RUN_OWNER.get("")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) and d.get("owner") else {}
    except Exception:
        return {}


def slug_key(prefix: str, name: str) -> str:
    """Canonical owner container key, e.g. slug_key('dream', 'My Pipeline')
    → 'dream-my-pipeline'."""
    safe = "".join(c if (c.isalnum() or c in "_.-") else "-"
                   for c in str(name or "").strip().lower()).strip("-") or "x"
    return f"{prefix}-{safe}"[:60]


async def _get_cfg() -> Dict:
    r = _redis()
    if not r:
        return {}
    try:
        raw = await r.get(KEY_CFG)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


async def _save_cfg(cfg: Dict) -> None:
    r = _redis()
    if r:
        await r.set(KEY_CFG, json.dumps(cfg))

_DEFAULT_BASE = os.getenv("VERA_SESSION_SANDBOX_IMAGE", "python:3.12-slim")
_WORKDIR = "/workspace"
# Write-confinement: keep agent-generated files IN the workspace instead of
# scattered across the container FS (/tmp, $HOME/.cache, …). We do it by
# REDIRECTION not lock-down — HOME + all the temp-dir vars point INTO the
# workspace volume — so relative writes, `>/tmp/…` redirects, tool caches and
# downloads all land in the one persistent, browsable, synced place, WITHOUT
# breaking package installs the way a read-only rootfs would. Reads anywhere in
# the container still work. Toggle: sandbox.config confine_writes (default on).
_WORKTMP = _WORKDIR + "/.tmp"
_CONFINE_ENV = {
    "HOME": _WORKDIR, "TMPDIR": _WORKTMP, "TMP": _WORKTMP, "TEMP": _WORKTMP,
    "XDG_CACHE_HOME": _WORKDIR + "/.cache",
    "PIP_CACHE_DIR": _WORKDIR + "/.cache/pip",
}


def _confine_env_args(cfg: Dict) -> List[str]:
    """`-e VAR=val` docker flags that redirect HOME/temp/cache into /workspace,
    or [] when confine_writes is off. Applied on both `docker run` (new
    containers) and every `docker exec` (so a container created before this
    feature — or a pwsh/other shell — still gets the redirect)."""
    if not cfg.get("confine_writes", True):
        return []
    out: List[str] = []
    for k, v in _CONFINE_ENV.items():
        out += ["-e", f"{k}={v}"]
    return out
# When a session is sandboxed, the "artifact area" IS the container's /workspace
# (the per-session named volume, which is also the exec WORKDIR) — so files the
# agent generates, their code.save mirror, and their by-path runs all land in the
# one place execution happens. Keeps the host artifact dir out of the loop.
_SBX_ARTIFACT_DIR = _WORKDIR

# language → (container interpreter argv-prefix, file extension)
_LANG_RUN = {
    "python": (["python3"], "py"), "py": (["python3"], "py"),
    "node": (["node"], "js"), "js": (["node"], "js"),
    "ruby": (["ruby"], "rb"), "php": (["php"], "php"),
    "perl": (["perl"], "pl"), "lua": (["lua"], "lua"),
    "bash": (["bash", "-lc"], "sh"), "sh": (["sh", "-lc"], "sh"),
    "powershell": (["pwsh", "-File"], "ps1"), "pwsh": (["pwsh", "-File"], "ps1"),
    "ps1": (["pwsh", "-File"], "ps1"),
}


def _redis():
    return getattr(_orch, "REDIS", None)


def _dk():
    m = sys.modules.get("docker_capabilities")
    if m is not None and hasattr(m, "_get_host"):
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("docker_capabilities") and hasattr(mod, "_get_host"):
            return mod
    return None


async def _get_rec(session_id: str) -> Optional[Dict]:
    r = _redis()
    if not r or not session_id:
        return None
    raw = await r.hget(KEY_SBX, session_id)
    return json.loads(raw) if raw else None


async def _save_rec(rec: Dict) -> None:
    r = _redis()
    if r:
        await r.hset(KEY_SBX, rec["session_id"], json.dumps(rec))


def _cname(session_id: str) -> str:
    # container names allow [a-zA-Z0-9_.-]; sanitise the session id.
    safe = "".join(c if (c.isalnum() or c in "_.-") else "-" for c in session_id)[:48]
    return f"vera-sbx-{safe}"


def _volname(session_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "_.-") else "-" for c in session_id)[:48]
    return f"vera-sbx-{safe}-ws"


async def _container_running(dk, rec_host: Dict, cname: str) -> Optional[str]:
    """Return the container State ('running'/'exited'/…) or None if absent."""
    import urllib.parse
    filt = urllib.parse.quote(json.dumps({"name": [cname]}))
    try:
        status, body, _ = await dk._engine_request(
            rec_host, "GET", f"/containers/json?all=true&filters={filt}")
        rows = json.loads(body or b"[]") if status == 200 else []
        for row in rows:
            if any(n.lstrip("/") == cname for n in (row.get("Names") or [])):
                return row.get("State", "")
    except Exception:
        pass
    return None


async def _docker_host(dk, host_id: str):
    rec = dk._get_host(host_id or "local")
    return rec


# ─────────────────────────────────────────────────────────────────────────────
#  ALIASES  — several run/session ids can SHARE one container (projects, goals,
#  IDE workspaces). The alias map redirects a session_id to the container-owning
#  id (e.g. dream run "dream-…" → "goal-<slug>").
# ─────────────────────────────────────────────────────────────────────────────
async def _resolve_sid(session_id: str) -> str:
    r = _redis()
    if not r or not session_id:
        return session_id
    try:
        t = await r.hget(KEY_ALIAS, session_id)
        t = (t.decode() if isinstance(t, bytes) else t) or ""
        return str(t) or session_id
    except Exception:
        return session_id


def _session_source(sid: str) -> str:
    """Best-effort classification of a session id for the containers UI."""
    s = str(sid or "")
    if s.startswith("dream:") or s.startswith("dream-"):
        return "dream"
    if s.startswith("v8:") or s.startswith("v8-"):
        return "v8"
    if s.startswith("goal-"):
        return "goal"
    if s.startswith("proj"):
        return "project"
    if s.startswith("ws-") or s.startswith("sbxw-"):
        return "ide"
    return "chat"


async def _note_session(target: str, session_id: str, source: str = "") -> None:
    """Tie `session_id` to the container-owning record `target` so the
    containers UI can show every session/run that used this container (and the
    context package can bundle them). Keeps the most recent 60 ties."""
    if not target or not session_id or target == session_id:
        return
    try:
        rec = await _get_rec(target) or {"session_id": target, "created": now_iso()}
        sess = [s for s in (rec.get("sessions") or [])
                if isinstance(s, dict) and s.get("id") != session_id]
        sess.append({"id": session_id, "ts": now_iso(),
                     "source": source or _session_source(session_id)})
        rec["sessions"] = sess[-60:]
        await _save_rec(rec)
    except Exception as e:
        log.debug("note_session %s→%s: %s", session_id, target, e)


async def _touch(rec: Dict) -> None:
    """Stamp last-use time (throttled) so idle auto-sleep knows what's in use."""
    try:
        now = time.time()
        if now - float(rec.get("last_used") or 0) > 60:
            rec["last_used"] = now
            await _save_rec(rec)
    except Exception:
        pass


async def _wake(dk, host, rec: Dict, state: Optional[str]) -> bool:
    """Bring a stopped/paused container back online IN PLACE (docker start /
    unpause) — preserves installed packages without needing a commit."""
    try:
        if state == "running":
            return True
        if state == "paused":
            r = await dk._run_local(await dk._docker_argv(
                host, ["unpause", rec["container"]]), timeout=30)
        elif state in ("exited", "created"):
            r = await dk._run_local(await dk._docker_argv(
                host, ["start", rec["container"]]), timeout=90)
        else:
            return False   # absent — needs a full start()
        if r.get("ok"):
            await emit_event({"type": "remote.sandbox.woken",
                              "session_id": rec.get("session_id", "")})
        return bool(r.get("ok"))
    except Exception:
        return False


async def _ensure_routable(session_id: str, *, create: bool = True) -> Optional[Dict]:
    """The routing gatekeeper: resolve aliases, WAKE a sleeping container, and —
    when the system-wide `auto_create` default is on (sandbox.config, default
    true) and `create` is allowed — CREATE a container for a session that has
    none yet. Returns the routable record, or None → the caller runs on the
    host. A record that exists but is INACTIVE is an explicit opt-out and is
    never auto-created over."""
    if not session_id:
        return None
    sid = await _resolve_sid(session_id)
    rec = await _get_rec(sid)
    if rec and rec.get("container") and rec.get("active"):
        dk = _dk()
        if dk is not None:
            host = await _docker_host(dk, rec.get("docker_host_id", "local"))
            if host:
                state = await _container_running(dk, host, rec["container"])
                if state != "running" and not await _wake(dk, host, rec, state):
                    # gone entirely (pruned image/container) → full restore
                    try:
                        res = await cap_sbx_start(session_id=sid, enable=True)
                        if not res.get("ok"):
                            return None
                        rec = await _get_rec(await _resolve_sid(sid)) or rec
                    except Exception as e:
                        log.debug("sandbox restore-on-use failed for %s: %s", sid, e)
                        return None
        await _touch(rec)
        return rec
    if rec and rec.get("container"):
        return None   # container exists but active=false — explicit opt-out
    # No container yet (no record, or a metadata-only stub from link_session)
    # → fall through to auto-create.
    if not create:
        return None
    cfg = await _get_cfg()
    if not cfg.get("auto_create", True):
        return None
    try:
        res = await cap_sbx_start(session_id=sid, enable=True)
        if not res.get("ok"):
            return None
    except Exception as e:
        log.debug("sandbox auto-create failed for %s: %s", sid, e)
        return None
    # start() may have REDIRECTED the session into its run-owner's container
    # (aliasing sid) — resolve again so routing lands in the shared container.
    rec = await _get_rec(await _resolve_sid(sid))
    if rec:
        await _touch(rec)
    return rec


# ═════════════════════════════════════════════════════════════════════════════
#  LIFECYCLE
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "sandbox.session.start",
    http_method="POST", http_path="/remote/sandbox/start", http_tags=["remote", "sandbox"],
    description="Create (or restore) a dedicated Docker sandbox for a session. If "
                "the session was committed before, its image is restored; else the "
                "base image is used. The /workspace dir lives in a named volume so "
                "it survives restarts. Inputs: session_id (str!), base_image (str — "
                "default python:3.12-slim), docker_host_id (str='local'), packages "
                "(str — apt/pip space-sep, best-effort install), enable (bool=true — "
                "route this session's exec/code into the container), rehydrate "
                "(bool=true — when /workspace comes up EMPTY, restore it from the "
                "durable session store so long-term work resumes even on a new host/"
                "volume), kind (str — session|project|goal|workspace|custom; how "
                "this container is used), label (str — human-readable name). A "
                "STOPPED container is woken in place (docker start — installed "
                "packages survive) unless a different base_image is requested. "
                "Output: {ok, container_id, image, restored, rehydrated, woken}.",
)
async def cap_sbx_start(session_id: str = "", base_image: str = "",
                        docker_host_id: str = "", packages: str = "",
                        enable: bool = True, rehydrate: bool = True,
                        kind: str = "", label: str = "",
                        trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    # ── RUN-OWNER guard (no nested / per-stage containers) ──────────────────
    # Inside a governed run (dream cycle, V8 program loop, project/goal loop)
    # every sandbox start for a DIFFERENT session id is redirected into the
    # run's owner container: the session is aliased to the owner and the
    # owner's container is started/woken instead. This holds for agentic tool
    # calls AND deterministic in-code calls alike.
    ro = current_run_owner()
    if ro and session_id != ro["owner"]:
        target = ro["owner"]
        try:
            await cap_sbx_link(session_id=session_id, target=target)
            await _note_session(target, session_id)
        except Exception as e:
            log.debug("owner-redirect link %s→%s: %s", session_id, target, e)
        res = await cap_sbx_start(
            session_id=target, base_image=base_image,
            docker_host_id=docker_host_id, packages=packages, enable=enable,
            rehydrate=rehydrate, kind=ro.get("kind") or kind,
            label=ro.get("label") or label, trace_id=trace_id)
        if isinstance(res, dict):
            res["redirected_to"] = target
            res["note"] = ("nested sandbox prevented — session shares its "
                           "run owner's container")
        return res
    # A session already LINKED to a shared container starts/wakes the target
    # container rather than minting a duplicate named after the alias.
    resolved = await _resolve_sid(session_id)
    if resolved != session_id:
        await _note_session(resolved, session_id)
        session_id = resolved
    dk = _dk()
    if dk is None:
        return {"ok": False, "error": "docker module not loaded"}
    # Resolve the docker host: explicit arg wins, else the configured global
    # sandbox default (sandbox.config.set), else "local".
    scfg = await _get_cfg()
    host_id = docker_host_id or scfg.get("docker_host_id") or "local"
    host = await _docker_host(dk, host_id)
    if not host:
        return {"ok": False, "error": f"unknown docker host: {host_id}"}

    rec = await _get_rec(session_id) or {"session_id": session_id, "created": now_iso()}
    cname = _cname(session_id)
    vol = _volname(session_id)
    committed = rec.get("committed_image", "")
    image = base_image or committed or rec.get("base_image") or scfg.get("base_image") or _DEFAULT_BASE
    restored = bool(committed and not base_image)

    if kind:
        rec["kind"] = str(kind).strip().lower()
    if label:
        rec["label"] = str(label).strip()

    state = await _container_running(dk, host, cname)
    if state == "running":
        rec.update({"container": cname, "image": image, "active": bool(enable),
                    "docker_host_id": host["id"], "base_image": image, "updated": now_iso()})
        await _save_rec(rec)
        return {"ok": True, "container_id": cname, "image": image, "already": True,
                "restored": restored}
    if state in ("exited", "created", "paused"):
        # Same image → wake the existing container in place: installed packages
        # and non-volume files survive without needing a commit.
        if not base_image or base_image == rec.get("image"):
            if await _wake(dk, host, {**rec, "container": cname}, state):
                rec.update({"container": cname, "image": rec.get("image") or image,
                            "active": bool(enable), "docker_host_id": host["id"],
                            "updated": now_iso(), "last_used": time.time()})
                await _save_rec(rec)
                await emit_event({"type": "remote.sandbox.started",
                                  "session_id": session_id, "image": rec["image"],
                                  "restored": False, "woken": True})
                return {"ok": True, "container_id": cname, "image": rec["image"],
                        "woken": True, "restored": False, "active": bool(enable)}
        # Different image requested (or wake failed) → recreate fresh below.
        await dk._run_local(await dk._docker_argv(host, ["rm", "-f", cname]), timeout=40)

    # Bake the write-confinement env into the container config so `docker exec`
    # (and an interactive terminal) inherit HOME/temp/cache → /workspace.
    run_env = dict(_CONFINE_ENV) if scfg.get("confine_writes", True) else None
    run = await dk.cap_docker_run(
        host_id=host["id"], image=image, name=cname, env=run_env,
        volumes=f"{vol}:{_WORKDIR}", restart="unless-stopped",
        extra_args=f"-w {_WORKDIR} --label vera.sandbox={session_id}",
        command="tail -f /dev/null", pull=not restored)
    if not run.get("ok"):
        return {"ok": False, "error": run.get("error", "docker run failed"),
                "image": image}
    # Pre-create the redirected dirs so tools honouring TMPDIR/XDG don't fall
    # back to /tmp when the target is missing.
    await dk._run_local(await dk._docker_argv(host, [
        "exec", cname, "sh", "-lc",
        f"mkdir -p {_WORKTMP} {_WORKDIR}/.cache/pip"]), timeout=30)

    if packages.strip():
        # best-effort: apt for debian-ish bases, then pip.
        pkgs = packages.strip()
        install = (f"(apt-get update && apt-get install -y {pkgs}) 2>/dev/null || "
                   f"(pip install {pkgs}) 2>/dev/null || apk add {pkgs} 2>/dev/null || true")
        await dk._run_local(
            await dk._docker_argv(host, ["exec", cname, "sh", "-lc", install]), timeout=600)

    rec.update({"container": cname, "image": image, "base_image": base_image or rec.get("base_image") or _DEFAULT_BASE,
                "committed_image": committed, "volume": vol,
                "docker_host_id": host["id"], "active": bool(enable),
                "updated": now_iso()})
    await _save_rec(rec)

    # Rehydrate /workspace from the durable session store when it came up empty
    # (fresh volume / different host) — resumes long-term work. Never clobbers an
    # already-populated volume.
    rehydrated = False
    if rehydrate:
        try:
            if await _workspace_is_empty(session_id):
                r = await _restore_session(session_id)
                rehydrated = bool(r.get("ok"))
        except Exception as e:
            log.debug("sandbox rehydrate skipped for %s: %s", session_id, e)

    await emit_event({"type": "remote.sandbox.started", "session_id": session_id,
                      "image": image, "restored": restored, "rehydrated": rehydrated})
    return {"ok": True, "container_id": run.get("container_id", cname), "image": image,
            "restored": restored, "active": bool(enable), "rehydrated": rehydrated}


@capability(
    "sandbox.session.status",
    http_method="POST", http_path="/remote/sandbox/status", http_tags=["remote", "sandbox"],
    memory="off",
    description="Sandbox status for a session (alias-aware: a linked session "
                "reports its shared container). Input: session_id (str!). Output: "
                "{exists, active, running, container, image, committed, linked_to, "
                "kind, label}.",
)
async def cap_sbx_status(session_id: str = "", trace_id=None) -> Dict:
    resolved = await _resolve_sid(session_id)
    rec = await _get_rec(resolved)
    if not rec:
        return {"exists": False,
                "linked_to": resolved if resolved != session_id else ""}
    dk = _dk()
    running = False
    if dk:
        host = await _docker_host(dk, rec.get("docker_host_id", "local"))
        if host:
            running = (await _container_running(dk, host, rec["container"])) == "running"
    return {"exists": True, "active": bool(rec.get("active")), "running": running,
            "container": rec.get("container"), "image": rec.get("image"),
            "committed": bool(rec.get("committed_image")),
            "linked_to": resolved if resolved != session_id else "",
            "kind": rec.get("kind", "session"), "label": rec.get("label", ""),
            "sessions": len(rec.get("sessions") or [])}


@capability(
    "sandbox.session.exec",
    http_method="POST", http_path="/remote/sandbox/exec", http_tags=["remote", "sandbox"],
    description="Run a command INSIDE a session's sandbox container. Inputs: "
                "session_id (str!), command (str!), workdir (str), timeout (int=120). "
                "Output: {ok, rc, stdout, stderr}.",
)
async def cap_sbx_exec(session_id: str = "", command: str = "", workdir: str = "",
                       timeout: int = 120, trace_id=None) -> Dict:
    res = await _exec_in(session_id, command, workdir=workdir, timeout=int(timeout))
    if res is None:
        return {"ok": False, "error": "no sandbox for this session (call sandbox.session.start)"}
    return res


def _shell_argv(shell: str, command: str) -> List[str]:
    """In-container interpreter argv for a command string. `sh` (default) or
    `pwsh` (PowerShell — needs a pwsh-capable base image)."""
    if shell == "pwsh":
        return ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["sh", "-lc", command]


async def _exec_in(session_id: str, command: str, *, workdir: str = "",
                   timeout: int = 120, shell: str = "sh") -> Optional[Dict]:
    """Run `command` in the session container. None if there's no sandbox.
    Alias-aware: a linked session id executes in its target's container."""
    session_id = await _resolve_sid(session_id)
    rec = await _get_rec(session_id)
    if not rec or not rec.get("container"):
        return None
    dk = _dk()
    if dk is None:
        return None
    host = await _docker_host(dk, rec.get("docker_host_id", "local"))
    if not host:
        return None
    cfg = await _get_cfg()
    args = ["exec"]
    # Default the working dir to /workspace so relative writes land there, and
    # redirect HOME/temp/cache into the workspace (write-confinement).
    args += ["-w", workdir or _WORKDIR]
    args += _confine_env_args(cfg)
    args += [rec["container"], *_shell_argv(shell, command)]
    res = await dk._run_local(await dk._docker_argv(host, args), timeout=timeout)
    return {"ok": res.get("ok", False), "rc": res.get("rc"),
            "stdout": res.get("stdout", ""), "stderr": res.get("stderr", ""),
            "sandboxed": True}


@capability(
    "sandbox.session.run_code",
    http_method="POST", http_path="/remote/sandbox/run_code", http_tags=["remote", "sandbox"],
    description="Write a code snippet into a session sandbox and run it there. "
                "Inputs: session_id (str!), language (str! — python|node|ruby|php|"
                "perl|lua|bash), code (str!), args (list), timeout (int=120). "
                "Output: {ok, rc, stdout, stderr, language}.",
    schema=enum_schema(language=sorted(set(_LANG_RUN.keys()))),
)
async def cap_sbx_run_code(session_id: str = "", language: str = "", code: str = "",
                           args: Optional[List[str]] = None, timeout: int = 120,
                           trace_id=None) -> Dict:
    res = await _run_code_in(session_id, language, code, args=args, timeout=int(timeout))
    if res is None:
        return {"ok": False, "error": "no sandbox for this session, or unsupported language"}
    return res


async def _run_code_in(session_id: str, language: str, code: str, *,
                       args: Optional[List[str]] = None, timeout: int = 120
                       ) -> Optional[Dict]:
    lang = (language or "").lower()
    spec = _LANG_RUN.get(lang)
    if not spec or not code:
        return None
    rec = await _get_rec(session_id)
    if not rec or not rec.get("container"):
        return None
    prefix, ext = spec
    if lang in ("bash", "sh"):
        # shell: run inline, no temp file needed
        out = await _exec_in(session_id, code, timeout=timeout)
        if out is not None:
            out["language"] = lang
        return out
    b64 = base64.b64encode(code.encode()).decode()
    fname = f"/tmp/vera_{uuid.uuid4().hex[:8]}.{ext}"
    argline = " ".join(shlex.quote(a) for a in (args or []))
    script = (f"echo {b64} | base64 -d > {fname}; "
              f"{' '.join(prefix)} {fname} {argline}; rc=$?; rm -f {fname}; exit $rc")
    out = await _exec_in(session_id, script, timeout=timeout)
    if out is not None:
        out["language"] = lang
    return out


_EXT_LANG = {".py": "python", ".js": "node", ".rb": "ruby", ".php": "php",
             ".pl": "perl", ".lua": "lua", ".sh": "bash", ".bash": "bash",
             ".ps1": "powershell"}


def _lang_from_ext(path: str) -> str:
    return _EXT_LANG.get(os.path.splitext(path)[1].lower(), "")


async def _run_pathfile_in(session_id: str, language: str, path: str, *,
                           args: Optional[List[str]] = None, timeout: int = 120
                           ) -> Optional[Dict]:
    """Run an EXISTING file (already written into the container) by path — the
    sandbox twin of exec's path-based run. Interpreter from `language` or the
    file extension. An unsupported language errors (never falls through to host,
    which can't see the container's file)."""
    lang = (language or "").lower() or _lang_from_ext(path)
    rec = await _get_rec(session_id)
    if not rec or not rec.get("container"):
        return None
    spec = _LANG_RUN.get(lang)
    if not spec:
        return {"ok": False, "rc": -1, "stdout": "", "language": lang,
                "stderr": f"unsupported language '{lang or '?'}' for {path} in sandbox",
                "sandboxed": True}
    prefix, _ext = spec
    argline = " ".join(shlex.quote(a) for a in (args or []))
    cmd = f"{' '.join(prefix)} {shlex.quote(path)} {argline}".strip()
    out = await _exec_in(session_id, cmd, timeout=timeout)
    if out is not None:
        out["language"] = lang
    return out


# ---- files inside the container --------------------------------------------
@capability(
    "sandbox.session.fs.write",
    http_method="POST", http_path="/remote/sandbox/fs/write", http_tags=["remote", "sandbox"],
    description="Write a file inside a session sandbox. Inputs: session_id (str!), "
                "path (str!), content (str). Output: {ok, path, bytes}.",
)
async def cap_sbx_fs_write(session_id: str = "", path: str = "", content: str = "",
                           trace_id=None) -> Dict:
    if not path:
        return {"ok": False, "error": "path required"}
    b64 = base64.b64encode((content or "").encode()).decode()
    res = await _exec_in(session_id,
                         f"mkdir -p $(dirname {shlex.quote(path)}); "
                         f"echo {b64} | base64 -d > {shlex.quote(path)}", timeout=45)
    if res is None:
        return {"ok": False, "error": "no sandbox for this session"}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or "write failed"}
    return {"ok": True, "path": path, "bytes": len((content or '').encode())}


@capability(
    "sandbox.session.fs.read",
    http_method="POST", http_path="/remote/sandbox/fs/read", http_tags=["remote", "sandbox"],
    description="Read a file inside a session sandbox. Inputs: session_id (str!), "
                "path (str!). Output: {ok, path, text}.",
)
async def cap_sbx_fs_read(session_id: str = "", path: str = "", trace_id=None) -> Dict:
    if not path:
        return {"ok": False, "error": "path required"}
    res = await _exec_in(session_id, f"cat {shlex.quote(path)}", timeout=30)
    if res is None:
        return {"ok": False, "error": "no sandbox for this session"}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or "read failed"}
    return {"ok": True, "path": path, "text": res.get("stdout", "")}


@capability(
    "sandbox.session.commit",
    http_method="POST", http_path="/remote/sandbox/commit", http_tags=["remote", "sandbox"],
    description="Commit a session's sandbox container to an image (vera-session:"
                "<sid>) so it can be restored instantly when the session re-opens. "
                "Input: session_id (str!). Output: {ok, image}.",
)
async def cap_sbx_commit(session_id: str = "", trace_id=None) -> Dict:
    rec = await _get_rec(session_id)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session"}
    dk = _dk()
    host = await _docker_host(dk, rec.get("docker_host_id", "local")) if dk else None
    if not host:
        return {"ok": False, "error": "docker host unavailable"}
    safe = "".join(c if c.isalnum() else "-" for c in session_id)[:40].strip("-").lower() or "s"
    image = f"vera-session:{safe}"
    res = await dk._run_local(
        await dk._docker_argv(host, ["commit", rec["container"], image]), timeout=180)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or "commit failed"}
    rec["committed_image"] = image
    rec["committed_at"] = now_iso()
    await _save_rec(rec)
    await emit_event({"type": "remote.sandbox.committed", "session_id": session_id, "image": image})
    return {"ok": True, "image": image}


@capability(
    "sandbox.session.stop",
    http_method="POST", http_path="/remote/sandbox/stop", http_tags=["remote", "sandbox"],
    description="Stop a session sandbox. When archiving is on (sandbox.config "
                "archive_on_stop, default true) it syncs /workspace to the durable "
                "session store (Garage blob store + Gitea) AND commits the container "
                "image first, so the session can be fully rehydrated on reopen; the "
                "/workspace volume is always kept. Inputs: session_id (str!), sync "
                "(bool — snapshot to the session store; default = archive_on_stop), "
                "commit (bool — default = archive_on_stop), remove (bool=true — remove "
                "the container after stopping). Output: {ok, committed_image, synced}.",
)
async def cap_sbx_stop(session_id: str = "", sync: Optional[bool] = None,
                       commit: Optional[bool] = None,
                       remove: bool = True, trace_id=None) -> Dict:
    rec = await _get_rec(session_id)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session"}
    # Archiving default: the global archive_on_stop config governs both the
    # blob-store snapshot and the image commit unless the caller overrides.
    if sync is None or commit is None:
        scfg = await _get_cfg()
        archive = bool(scfg.get("archive_on_stop", True))
        if sync is None:
            sync = archive
        if commit is None:
            commit = archive
    dk = _dk()
    host = await _docker_host(dk, rec.get("docker_host_id", "local")) if dk else None
    if not host:
        return {"ok": False, "error": "docker host unavailable"}
    # Snapshot to the durable store BEFORE the container is stopped/removed.
    # The context package (sessions/runs tied to this container) is written
    # into /workspace/.vera first so it travels with the snapshot + commit.
    synced = None
    if sync:
        try:
            await cap_sbx_context(session_id=session_id, package=True)
        except Exception:
            pass
        try:
            synced = await _sync_session(session_id, message="session stop")
        except Exception as e:
            log.warning("sandbox sync on stop failed for %s: %s", session_id, e)
            synced = {"ok": False, "error": str(e)}
    committed = ""
    if commit:
        c = await cap_sbx_commit(session_id=session_id)
        committed = c.get("image", "")
    if remove:
        await dk._run_local(await dk._docker_argv(host, ["rm", "-f", rec["container"]]), timeout=40)
    else:
        await dk._run_local(await dk._docker_argv(host, ["stop", rec["container"]]), timeout=40)
    rec["active"] = False
    rec["updated"] = now_iso()
    await _save_rec(rec)
    await emit_event({"type": "remote.sandbox.stopped", "session_id": session_id})
    return {"ok": True, "committed_image": committed, "synced": synced}


@capability(
    "sandbox.session.list",
    http_method="GET", http_path="/remote/sandbox/list", http_tags=["remote", "sandbox"],
    memory="off", silent=True,
    description="List all known session sandboxes. Output: {sandboxes:[{session_id,"
                "container,image,docker_host_id,active,committed,kind,label,state,"
                "last_used}], count}. `state` (running|exited|…|absent) is a "
                "best-effort docker query per host.",
)
async def cap_sbx_list(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"sandboxes": [], "count": 0}
    try:
        items = await r.hgetall(KEY_SBX)
    except Exception:
        items = {}
    dk = _dk()
    out = []
    for v in items.values():
        try:
            rec = json.loads(v)
        except Exception:
            continue
        state = ""
        if dk and rec.get("container"):
            try:
                host = await _docker_host(dk, rec.get("docker_host_id", "local"))
                if host:
                    state = (await _container_running(dk, host, rec["container"])) or "absent"
            except Exception:
                state = ""
        out.append({"session_id": rec.get("session_id"), "container": rec.get("container"),
                    "image": rec.get("image"), "active": bool(rec.get("active")),
                    "docker_host_id": rec.get("docker_host_id", "local"),
                    "committed": bool(rec.get("committed_image")),
                    "kind": rec.get("kind", "session"), "label": rec.get("label", ""),
                    "state": state, "last_used": float(rec.get("last_used") or 0),
                    "source": _session_source(rec.get("session_id") or ""),
                    "sessions": (rec.get("sessions") or [])[-8:],
                    "session_count": len(rec.get("sessions") or [])})
    return {"sandboxes": out, "count": len(out)}


@capability(
    "sandbox.session.context",
    http_method="GET", http_path="/remote/sandbox/context", http_tags=["remote", "sandbox"],
    memory="off", silent=True,
    description="The full CONTEXT PACKAGE for a sandbox container: its record "
                "(kind, label, owner key, image, docker host), every session/run "
                "tied to it, and the run-state of each tied agentic-loop session "
                "(from the loop session store) — everything needed to understand "
                "what this container was for. Inputs: session_id (str! — the "
                "container-owning key, alias-aware), package (bool=false — ALSO "
                "write the package into the container at /workspace/.vera/"
                "context.json so it travels with commits/snapshots). "
                "Output: {ok, record, sessions, runs, packaged}.",
)
async def cap_sbx_context(session_id: str = "", package: bool = False,
                          trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    sid = await _resolve_sid(session_id)
    rec = await _get_rec(sid)
    if not rec:
        return {"ok": False, "error": f"no sandbox record for {session_id}"}
    pub = {k: rec.get(k) for k in
           ("session_id", "container", "image", "base_image", "committed_image",
            "docker_host_id", "kind", "label", "active", "created", "updated",
            "last_used", "store_version", "last_sync", "gitea_repo")}
    sessions = list(rec.get("sessions") or [])
    # Include the owner key itself as a session so its own runs are found too.
    tied_ids = [s.get("id") for s in sessions if s.get("id")] + [sid]
    runs: List[Dict[str, Any]] = []
    r = _redis()
    if r:
        for tid in tied_ids[-40:]:
            try:
                raw = await r.hgetall(f"vera:loop:run:{tid}")
                if raw:
                    run = {(k.decode() if isinstance(k, bytes) else k):
                           (v.decode() if isinstance(v, bytes) else v)
                           for k, v in raw.items()}
                    run["session_id"] = tid
                    runs.append(run)
            except Exception:
                continue
    out = {"ok": True, "record": pub, "sessions": sessions, "runs": runs,
           "packaged": False}
    if package:
        try:
            blob = json.dumps({"record": pub, "sessions": sessions,
                               "runs": runs, "packaged_at": now_iso()},
                              indent=2, default=str)
            w = await cap_sbx_fs_write(session_id=sid,
                                       path=f"{_WORKDIR}/.vera/context.json",
                                       content=blob)
            out["packaged"] = bool(w.get("ok"))
        except Exception as e:
            out["package_error"] = str(e)
    return out


@capability(
    "sandbox.session.set_active",
    http_method="POST", http_path="/remote/sandbox/set_active", http_tags=["remote", "sandbox"],
    description="Flip a session sandbox's ACTIVE flag without touching Docker. "
                "active=true routes the session's exec/code/file-IO into its container "
                "(the container must already exist — use sandbox.session.start to create "
                "one); active=false stops routing (the container keeps running and can be "
                "re-activated instantly). Inputs: session_id (str!), active (bool=true). "
                "Output: {ok, session_id, active}.",
)
async def cap_sbx_set_active(session_id: str = "", active: bool = True,
                             trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    rec = await _get_rec(session_id)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session (call sandbox.session.start)"}
    rec["active"] = bool(active)
    rec["updated"] = now_iso()
    await _save_rec(rec)
    await emit_event({"type": "remote.sandbox.active", "session_id": session_id,
                      "active": bool(active)})
    return {"ok": True, "session_id": session_id, "active": bool(active)}


@capability(
    "sandbox.session.sleep",
    http_method="POST", http_path="/remote/sandbox/sleep", http_tags=["remote", "sandbox"],
    description="Put a session sandbox to SLEEP: docker-stop the container while "
                "keeping the container, its /workspace volume and the active flag — "
                "the next exec/file-IO for the session wakes it automatically "
                "(sandbox.session.start also wakes it explicitly). When archiving "
                "is on (or sync=true) /workspace is snapshotted to the blob store "
                "first. Inputs: session_id (str!), sync (bool — default = "
                "archive_on_stop config). Output: {ok, slept, synced}.",
)
async def cap_sbx_sleep(session_id: str = "", sync: Optional[bool] = None,
                        trace_id=None) -> Dict:
    sid = await _resolve_sid(session_id)
    rec = await _get_rec(sid)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session"}
    dk = _dk()
    host = await _docker_host(dk, rec.get("docker_host_id", "local")) if dk else None
    if not host:
        return {"ok": False, "error": "docker host unavailable"}
    if sync is None:
        sync = bool((await _get_cfg()).get("archive_on_stop", True))
    synced = None
    if sync:
        try:
            await cap_sbx_context(session_id=sid, package=True)
        except Exception:
            pass
        try:
            synced = await _sync_session(sid, message="sleep")
        except Exception as e:
            synced = {"ok": False, "error": str(e)}
    res = await dk._run_local(await dk._docker_argv(
        host, ["stop", rec["container"]]), timeout=90)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or "docker stop failed",
                "synced": synced}
    rec["updated"] = now_iso()
    await _save_rec(rec)
    await emit_event({"type": "remote.sandbox.slept", "session_id": sid})
    return {"ok": True, "slept": True, "synced": synced}


@capability(
    "sandbox.session.link",
    http_method="POST", http_path="/remote/sandbox/link", http_tags=["remote", "sandbox"],
    description="Link a session id to ANOTHER session's sandbox so both share one "
                "container — e.g. every dream run of a goal-project links to "
                "'goal-<slug>', all work on a project to 'proj-<slug>', an IDE "
                "workspace to 'ws-<name>'. All exec/code/file-IO routing for the "
                "linked id transparently lands in the target's container. Inputs: "
                "session_id (str!), target (str! — the container-owning session "
                "key), unlink (bool=false — remove the link instead). "
                "Output: {ok, session_id, target}.",
)
async def cap_sbx_link(session_id: str = "", target: str = "",
                       unlink: bool = False, trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    if unlink:
        try:
            await r.hdel(KEY_ALIAS, session_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "session_id": session_id, "target": ""}
    if not target or target == session_id:
        return {"ok": False, "error": "target required (and must differ from session_id)"}
    # RUN-OWNER guard: inside a governed run all links go to the owner —
    # a loop cannot re-point its session at some other container.
    ro = current_run_owner()
    if ro and target != ro["owner"] and session_id != ro["owner"]:
        log.info("sandbox link %s→%s overridden to run owner %s",
                 session_id, target, ro["owner"])
        target = ro["owner"]
    # Refuse alias chains: the target must not itself be an alias.
    if await _resolve_sid(target) != target:
        return {"ok": False, "error": f"target '{target}' is itself linked — link "
                "directly to the container-owning id"}
    await r.hset(KEY_ALIAS, session_id, target)
    await _note_session(target, session_id)
    await emit_event({"type": "remote.sandbox.linked", "session_id": session_id,
                      "target": target})
    return {"ok": True, "session_id": session_id, "target": target}


@capability(
    "sandbox.session.terminal",
    http_method="POST", http_path="/remote/sandbox/terminal_open", http_tags=["remote", "sandbox"],
    description="Open an interactive TTY into a session's sandbox container: wakes/"
                "creates the container, then returns a terminal descriptor the "
                "<vera-terminal> element connects to (and a ready-to-open full-page "
                "URL). Inputs: session_id (str!), shell (str='bash'). Output: {ok, "
                "ws_path, page_url, container, docker_host_id, shell}.",
)
async def cap_sbx_terminal(session_id: str = "", shell: str = "bash",
                           trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    rec = await _ensure_routable(session_id, create=True)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session (and auto-create "
                "is off) — start one with sandbox.session.start"}
    from urllib.parse import quote
    hid = rec.get("docker_host_id", "local")
    cont = rec["container"]
    sh = (shell or "bash").strip() or "bash"
    ws_path = (f"/remote/docker/term/ws/{quote(hid, safe='')}/"
               f"{quote(cont, safe='')}?shell={quote(sh)}")
    page_url = (f"/remote/sandbox/terminal?session_id={quote(session_id, safe='')}"
                f"&shell={quote(sh)}")
    return {"ok": True, "ws_path": ws_path, "page_url": page_url,
            "container": cont, "docker_host_id": hid, "shell": sh,
            "protocol": "vera-term"}


_TERM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Sandbox terminal · {label}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>html,body{{margin:0;height:100%;background:#000;font-family:ui-monospace,Menlo,monospace}}
#bar{{position:fixed;top:0;left:0;right:0;height:26px;background:#14171d;color:#8a93a3;
 display:flex;align-items:center;gap:8px;padding:0 10px;font-size:12px;z-index:5;border-bottom:1px solid #222}}
#bar b{{color:#6ea8d8}} #t{{position:fixed;top:26px;left:0;right:0;bottom:0}}</style>
<script src="/ui/vera-terminal.js"></script></head>
<body><div id="bar"><b>{label}</b><span>{cont}</span></div>
<vera-terminal id="t" ws="{ws}"></vera-terminal></body></html>"""


@APP.get("/remote/sandbox/terminal", include_in_schema=False)
async def _sandbox_terminal_page(session_id: str = "", shell: str = "bash"):
    """Full-page interactive terminal for a session's sandbox container — served
    same-origin with Vera so the <vera-terminal> WebSocket resolves correctly.
    Opened from the chat UI's Session-container 'Terminal' button."""
    from fastapi.responses import HTMLResponse
    info = await cap_sbx_terminal(session_id=session_id, shell=shell)
    if not info.get("ok"):
        return HTMLResponse(f"<pre style='color:#e06060;font:13px monospace;padding:20px'>"
                            f"Cannot open terminal: {info.get('error','')}</pre>",
                            status_code=400)
    rec = await _get_rec(await _resolve_sid(session_id)) or {}
    label = rec.get("label") or session_id
    html = _TERM_PAGE.format(label=_html_escape(label), cont=_html_escape(info["container"]),
                             ws=_html_escape(info["ws_path"]))
    return HTMLResponse(html)


def _html_escape(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


async def link_session(session_id: str, target: str, *, kind: str = "",
                       label: str = "") -> bool:
    """Programmatic helper for loops: link `session_id` to a shared container key
    (creating/naming the target record lazily via kind/label metadata on first
    start). Never raises."""
    try:
        res = await cap_sbx_link(session_id=session_id, target=target)
        if not res.get("ok"):
            return False
        if kind or label:
            rec = await _get_rec(target)
            if rec:
                if kind:
                    rec.setdefault("kind", kind)
                if label:
                    rec.setdefault("label", label)
                await _save_rec(rec)
            else:
                # Stash the metadata so auto-create's start() picks it up.
                await _save_rec({"session_id": target, "created": now_iso(),
                                 "kind": kind or "session", "label": label})
        return True
    except Exception as e:
        log.debug("sandbox link %s→%s failed: %s", session_id, target, e)
        return False


@capability(
    "sandbox.config.get",
    http_method="GET", http_path="/remote/sandbox/config", http_tags=["remote", "sandbox"],
    memory="off", silent=True,
    description="Get the global session-sandbox defaults (which docker host runs "
                "sandboxes, the default base image, the periodic auto-sync "
                "interval in seconds, and whether stopped sandboxes are archived "
                "— workspace snapshot to the blob store + image commit). Output: "
                "{docker_host_id, base_image, default_base, auto_sync_interval, "
                "archive_on_stop}.",
)
async def cap_sbx_cfg_get(trace_id=None) -> Dict:
    cfg = await _get_cfg()
    return {"docker_host_id": cfg.get("docker_host_id", "local"),
            "base_image": cfg.get("base_image", "") or _DEFAULT_BASE,
            "default_base": _DEFAULT_BASE,
            "auto_sync_interval": int(cfg.get("auto_sync_interval", _AUTO_SYNC_DEFAULT) or 0),
            "archive_on_stop": bool(cfg.get("archive_on_stop", True)),
            "auto_create": bool(cfg.get("auto_create", True)),
            "idle_sleep_minutes": int(cfg.get("idle_sleep_minutes", _IDLE_SLEEP_DEFAULT) or 0),
            "confine_writes": bool(cfg.get("confine_writes", True))}


@capability(
    "sandbox.config.set",
    http_method="POST", http_path="/remote/sandbox/config/set", http_tags=["remote", "sandbox"],
    description="Set the global session-sandbox defaults so ALL new sandboxes run on "
                "a chosen docker host (e.g. a dedicated Proxmox Docker stack) unless a "
                "call overrides docker_host_id. Inputs: docker_host_id (str — must be a "
                "registered docker host), base_image (str), auto_sync_interval (int sec "
                "— periodic auto-sync of active sandboxes to the session store; 0 "
                "disables), archive_on_stop (bool — when true, sandbox.session.stop "
                "defaults to snapshotting /workspace into the blob store AND committing "
                "the container image so the session can be fully restored later; when "
                "false, stop just removes the container — the /workspace volume is "
                "still kept), auto_create (bool — SYSTEM-WIDE default: when true, "
                "any session that executes shell/code or writes artifacts gets its "
                "own container automatically — chat, dream, goals, loops; when "
                "false, containers exist only where explicitly started), "
                "idle_sleep_minutes (int — auto-STOP containers idle for this long; "
                "they wake automatically on next use; 0 disables), confine_writes "
                "(bool — keep agent-generated files in /workspace by redirecting "
                "HOME/temp/cache into the workspace volume and defaulting the exec "
                "cwd there; reads elsewhere still work; default true). "
                "Output: {ok, config}.",
)
async def cap_sbx_cfg_set(docker_host_id: Optional[str] = None,
                          base_image: Optional[str] = None,
                          auto_sync_interval: Optional[int] = None,
                          archive_on_stop: Optional[bool] = None,
                          auto_create: Optional[bool] = None,
                          idle_sleep_minutes: Optional[int] = None,
                          confine_writes: Optional[bool] = None,
                          trace_id=None) -> Dict:
    cfg = await _get_cfg()
    if docker_host_id is not None:
        hid = str(docker_host_id).strip()
        if hid:
            dk = _dk()
            if dk is not None and dk._get_host(hid) is None:
                return {"ok": False, "error": f"unknown docker host: {hid} "
                        "(register it first in the docker host registry)"}
        cfg["docker_host_id"] = hid
    if base_image is not None:
        cfg["base_image"] = str(base_image).strip()
    if auto_sync_interval is not None:
        try:
            cfg["auto_sync_interval"] = max(0, int(auto_sync_interval))
        except Exception:
            return {"ok": False, "error": "auto_sync_interval must be an integer"}
    if archive_on_stop is not None:
        cfg["archive_on_stop"] = bool(archive_on_stop)
    if auto_create is not None:
        cfg["auto_create"] = bool(auto_create)
    if idle_sleep_minutes is not None:
        try:
            cfg["idle_sleep_minutes"] = max(0, int(idle_sleep_minutes))
        except Exception:
            return {"ok": False, "error": "idle_sleep_minutes must be an integer"}
    if confine_writes is not None:
        cfg["confine_writes"] = bool(confine_writes)
    await _save_cfg(cfg)
    await emit_event({"type": "remote.sandbox.config",
                      "docker_host_id": cfg.get("docker_host_id", ""),
                      "base_image": cfg.get("base_image", "")})
    return {"ok": True, "config": cfg}


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTING HOOKS  (called by exec_capabilities when a call carries a session_id)
# ═════════════════════════════════════════════════════════════════════════════
async def route_shell(session_id: str, command: str, timeout: int = 60,
                      shell: str = "sh") -> Optional[Dict]:
    """If `session_id` has (or, with auto_create on, GETS) an ACTIVE sandbox, run
    the shell command inside it and return an exec-shaped dict; else None so the
    caller runs on the host. Wakes sleeping containers; `shell="pwsh"` runs
    PowerShell (needs a pwsh-capable base image)."""
    if not session_id:
        return None
    rec = await _ensure_routable(session_id, create=True)
    if not rec:
        return None
    res = await _exec_in(rec["session_id"], command, timeout=int(timeout or 60), shell=shell)
    if res is None:
        return None
    res.setdefault("elapsed_ms", 0)
    return res


async def route_code(session_id: str, language: str, code: str, path: str = "",
                     stdin: str = "", timeout: int = 60,
                     args: Optional[List[str]] = None) -> Optional[Dict]:
    """If `session_id` has an ACTIVE sandbox, run the snippet — OR an existing file
    by `path` — inside it. Returns None (→ host) only when there's NO active
    sandbox; a by-path run of a file that lives in the container therefore stays
    confined instead of leaking to a host that can't see it."""
    if not session_id:
        return None
    rec = await _ensure_routable(session_id, create=True)
    if not rec:
        return None
    p = (path or "").strip()
    if p:
        res = await _run_pathfile_in(rec["session_id"], language, p, args=args,
                                     timeout=int(timeout or 60))
    elif code:
        res = await _run_code_in(rec["session_id"], language, code, args=args,
                                 timeout=int(timeout or 60))
    else:
        return None
    if res is None:
        return None
    res.setdefault("elapsed_ms", 0)
    res.setdefault("language", language)
    return res


# ═════════════════════════════════════════════════════════════════════════════
#  STREAMING ROUTING  (called by exec_capabilities' SSE stream endpoints)
# ═════════════════════════════════════════════════════════════════════════════
async def _sbx_host(session_id: str, *, create: bool = False):
    """Return (dk, host, rec) when the session has an ACTIVE sandbox, else
    (None, None, None). The caller runs on the host only when dk is None.
    Alias-aware; wakes sleeping containers; `create=True` additionally
    auto-creates one when the global auto_create default is on."""
    if not session_id:
        return None, None, None
    rec = await _ensure_routable(session_id, create=create)
    if not rec or not rec.get("container"):
        return None, None, None
    dk = _dk()
    if dk is None:
        return None, None, None
    host = await _docker_host(dk, rec.get("docker_host_id", "local"))
    if not host:
        return None, None, None
    return dk, host, rec


async def route_shell_argv(session_id: str, command: str, *, workdir: str = "",
                           shell: str = "sh") -> Optional[List[str]]:
    """Host-side argv (`docker exec … <shell> <command>`) that runs `command`
    INSIDE the session's sandbox, or None when the session has no active sandbox.
    Lets a caller STREAM the subprocess itself while execution stays confined to
    the container (the streaming twin of `route_shell`). `shell="pwsh"` streams
    PowerShell (needs a pwsh-capable base image)."""
    if not command:
        return None
    dk, host, rec = await _sbx_host(session_id, create=True)
    if dk is None:
        return None
    cfg = await _get_cfg()
    args = ["exec", "-w", workdir or _WORKDIR]
    args += _confine_env_args(cfg)
    args += [rec["container"], *_shell_argv(shell, command)]
    return await dk._docker_argv(host, args)


async def route_code_argv(session_id: str, language: str, code: str, *,
                          args: Optional[List[str]] = None) -> Optional[List[str]]:
    """Host-side argv that runs a code snippet INSIDE the session sandbox (writes
    a temp file in the container, runs the interpreter, cleans up), or None for
    an inactive sandbox / unsupported language (→ caller runs on the host). The
    streaming twin of `route_code`."""
    lang = (language or "").lower()
    spec = _LANG_RUN.get(lang)
    if not spec or not code:
        return None
    prefix, ext = spec
    if lang in ("bash", "sh"):
        return await route_shell_argv(session_id, code)
    # Only build the docker-exec argv once we know a sandbox is active.
    if (await _sbx_host(session_id, create=True))[0] is None:
        return None
    b64 = base64.b64encode(code.encode()).decode()
    fname = f"/tmp/vera_{uuid.uuid4().hex[:8]}.{ext}"
    argline = " ".join(shlex.quote(a) for a in (args or []))
    script = (f"echo {b64} | base64 -d > {fname}; "
              f"{' '.join(prefix)} {fname} {argline}; rc=$?; rm -f {fname}; exit $rc")
    return await route_shell_argv(session_id, script)


# ═════════════════════════════════════════════════════════════════════════════
#  FILESYSTEM ROUTING  (called by ide_capabilities' ide.fs.* caps)
#
#  Each returns an ide.fs.*-shaped dict when the session has an ACTIVE sandbox
#  (so the operation touches ONLY the container), or None when there's no active
#  sandbox (→ the ide.fs cap runs on the host as before). Once a sandbox is known
#  active the helpers never return None — an infra failure yields an error dict,
#  never a silent fall-through to the host filesystem.
# ═════════════════════════════════════════════════════════════════════════════
def _ls_script(path: str) -> str:
    """POSIX-sh (works on debian coreutils + busybox) directory lister emitting
    a `__VERA_DIR__ <abspath>` line then tab-separated `name\\tkind\\tsize\\tmtime`
    rows. Prints `__VERA_ENOENT__` and exits 3 when the path is missing."""
    q = shlex.quote(path) if path else '"$PWD"'
    return (
        f'd={q}; '
        'if [ ! -e "$d" ]; then echo __VERA_ENOENT__; exit 3; fi; '
        'if [ ! -d "$d" ]; then d=$(dirname "$d"); fi; '
        'cd "$d" || { echo __VERA_ENOENT__; exit 3; }; '
        'echo "__VERA_DIR__ $PWD"; '
        'ls -1A 2>/dev/null | while IFS= read -r n; do '
        'if [ -d "$n" ]; then k=directory; else k=file; fi; '
        's=$(stat -c %s "$n" 2>/dev/null || echo 0); '
        'm=$(stat -c %Y "$n" 2>/dev/null || echo 0); '
        'printf "%s\\t%s\\t%s\\t%s\\n" "$n" "$k" "$s" "$m"; '
        'done'
    )


async def _ls_in(session_id: str, path: str):
    """(target_abspath, [entry dicts], error_str). error 'enoent' = not found."""
    res = await _exec_in(session_id, _ls_script(path or ""), timeout=45)
    if res is None:
        return "", [], "sandbox exec failed"
    blob = (res.get("stdout") or "") + (res.get("stderr") or "")
    if "__VERA_ENOENT__" in blob:
        return "", [], "enoent"
    target = ""
    entries = []
    for ln in (res.get("stdout") or "").splitlines():
        if ln.startswith("__VERA_DIR__ "):
            target = ln[len("__VERA_DIR__ "):]
            continue
        parts = ln.split("\t")
        if len(parts) != 4:
            continue
        name, kind, size, mtime = parts
        try:
            size_i = int(size or 0)
        except Exception:
            size_i = 0
        try:
            mtime_f = float(mtime or 0)
        except Exception:
            mtime_f = 0.0
        entries.append({"name": name, "kind": kind, "size": size_i,
                        "mtime": mtime_f})
    entries.sort(key=lambda e: (e["kind"] != "directory", e["name"].lower()))
    return target, entries, ""


def _join(target: str, name: str) -> str:
    return (target.rstrip("/") + "/" + name) if target else name


async def route_fs_read(session_id: str, path: str, *,
                        max_bytes: int = 1_048_576) -> Optional[Dict]:
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    if not path:
        return {"error": "path required"}
    q = shlex.quote(path)
    meta = await _exec_in(
        session_id,
        f"if [ ! -e {q} ]; then echo __VERA_ENOENT__; exit 3; fi; "
        f"stat -c %s {q} 2>/dev/null || echo 0", timeout=30)
    if meta is None:
        return {"error": "sandbox exec failed"}
    if "__VERA_ENOENT__" in (meta.get("stdout", "") + meta.get("stderr", "")):
        return {"error": f"File not found: {path}"}
    try:
        size = int((meta.get("stdout", "").strip().splitlines() or ["0"])[-1] or 0)
    except Exception:
        size = 0
    body = await _exec_in(
        session_id, f"head -c {int(max_bytes)} {q} | base64 | tr -d '\\n'",
        timeout=60)
    if body is None:
        return {"error": "sandbox exec failed"}
    b64 = "".join((body.get("stdout", "") or "").split())
    b64 = b64[:len(b64) // 4 * 4]   # exec output cap may cut the tail mid-quad
    try:
        content = base64.b64decode(b64).decode("utf-8", "replace")
    except Exception:
        content = body.get("stdout", "")
    # The exec output cap (1 MB) can bound the base64 below the requested bytes.
    truncated = size > max_bytes or size > len(content.encode("utf-8", "replace"))
    return {"path": path, "content": content, "size": size,
            "truncated": truncated, "sandboxed": True}


async def route_fs_write(session_id: str, path: str, content: str) -> Optional[Dict]:
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    if not path:
        return {"error": "path required"}
    q = shlex.quote(path)
    b64 = base64.b64encode((content or "").encode()).decode()
    script = (f"if [ -e {q} ]; then __c=0; else __c=1; fi; "
              f'mkdir -p "$(dirname {q})" 2>/dev/null; '
              f'echo {b64} | base64 -d > {q} && echo "__VERA_W_OK_$__c"')
    res = await _exec_in(session_id, script, timeout=60)
    if res is None:
        return {"error": "sandbox exec failed"}
    out = res.get("stdout", "")
    if "__VERA_W_OK_" not in out or not res.get("ok"):
        return {"error": res.get("stderr") or "write failed"}
    created = out.split("__VERA_W_OK_", 1)[1].strip().startswith("1")
    return {"path": path, "bytes": len((content or "").encode()),
            "created": created, "sandboxed": True}


async def route_fs_delete(session_id: str, path: str) -> Optional[Dict]:
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    if not path:
        return {"error": "path required", "deleted": False}
    q = shlex.quote(path)
    chk = await _exec_in(session_id, f"[ -e {q} ] && echo 1 || echo 0", timeout=20)
    if chk is None:
        return {"error": "sandbox exec failed", "deleted": False}
    if chk.get("stdout", "").strip() != "1":
        return {"error": f"File not found: {path}", "deleted": False}
    res = await _exec_in(session_id, f"rm -f {q}", timeout=30)
    if res is None or not res.get("ok"):
        return {"error": (res or {}).get("stderr") or "delete failed",
                "deleted": False}
    return {"path": path, "deleted": True, "sandboxed": True}


async def route_fs_list(session_id: str, path: str = "") -> Optional[Dict]:
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    target, entries, err = await _ls_in(session_id, path)
    if err == "enoent":
        return {"error": f"Path not found: {path}", "entries": []}
    if err:
        return {"error": err, "entries": []}
    out = [{"name": e["name"], "path": _join(target, e["name"]), "kind": e["kind"],
            "size": e["size"], "mtime": e["mtime"], "readable": True}
           for e in entries]
    return {"path": target, "entries": out, "sandboxed": True}


async def route_fs_browse(session_id: str, path: str = "") -> Optional[Dict]:
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    target, entries, err = await _ls_in(session_id, path)
    if err == "enoent":
        return {"error": f"Path not found: {path}", "path": path,
                "parent": None, "crumbs": [], "entries": []}
    if err:
        return {"error": err, "path": path, "parent": None,
                "crumbs": [], "entries": []}
    tp = target or "/"
    segs = [s for s in tp.split("/") if s]
    crumbs = [{"name": "/", "path": "/"}]
    acc = ""
    for s in segs:
        acc += "/" + s
        crumbs.append({"name": s, "path": acc})
    parent = "/" + "/".join(segs[:-1]) if len(segs) > 1 else ("/" if segs else None)
    if parent == tp:
        parent = None
    out = [{"name": e["name"], "path": _join(tp, e["name"]), "kind": e["kind"],
            "size": e["size"], "mtime": e["mtime"], "readable": True}
           for e in entries]
    return {"path": tp, "parent": parent, "crumbs": crumbs, "entries": out,
            "sandboxed": True}


async def route_fs_exists(session_id: str, path: str) -> Optional[Dict]:
    """Existence check INSIDE the session sandbox, shaped like ide.fs.exists
    ({path, exists, kind, size, mtime}), or None when there's no active sandbox."""
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    if not path:
        return {"path": "", "exists": False, "kind": "missing", "size": 0,
                "sandboxed": True}
    q = shlex.quote(path)
    script = (f'if [ -d {q} ]; then k=directory; elif [ -e {q} ]; then k=file; '
              f'else k=missing; fi; '
              f's=$(stat -c %s {q} 2>/dev/null || echo 0); '
              f'm=$(stat -c %Y {q} 2>/dev/null || echo 0); '
              f'printf "%s\\t%s\\t%s\\n" "$k" "$s" "$m"')
    res = await _exec_in(session_id, script, timeout=20)
    if res is None:
        return {"path": path, "exists": False, "kind": "error",
                "error": "sandbox exec failed", "sandboxed": True}
    parts = (res.get("stdout") or "").strip().split("\t")
    kind = parts[0] if parts else "missing"
    try:
        size = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        size = 0
    try:
        mtime = float(parts[2]) if len(parts) > 2 else 0.0
    except Exception:
        mtime = 0.0
    if kind == "missing":
        return {"path": path, "exists": False, "kind": "missing", "size": 0,
                "sandboxed": True}
    return {"path": path, "exists": True, "kind": kind, "size": size,
            "mtime": mtime, "sandboxed": True}


async def route_fs_grep(session_id: str, pattern: str, root: str = "", *,
                        is_regex: bool = False, case_sensitive: bool = True,
                        max_results: int = 200) -> Optional[Dict]:
    """Text search INSIDE the session sandbox, shaped like ide.code.grep
    ({root, pattern, total_matches, matches:[{path, rel, line, text}]}), or None
    when there's no active sandbox. Uses the container's grep (debian coreutils
    or busybox); context lines are not extracted."""
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    if not pattern:
        return {"error": "pattern is required", "matches": [], "sandboxed": True}
    base = (root or _WORKDIR).rstrip("/") or _WORKDIR
    qb = shlex.quote(base)
    qp = shlex.quote(pattern)
    mode = "-E" if is_regex else "-F"
    ci = "" if case_sensitive else " -i"
    n = max(1, min(int(max_results or 200), 2000))
    # grep rc=1 just means "no matches" — mask it so _exec_in's ok stays true.
    script = (f'cd {qb} 2>/dev/null || {{ echo __VERA_ENOENT__; exit 3; }}; '
              f'grep -rn {mode}{ci} -- {qp} . 2>/dev/null '
              f'| grep -v -e "^\\./\\.git/" -e "^\\./node_modules/" '
              f'| head -n {n}; true')
    res = await _exec_in(session_id, script, timeout=60)
    if res is None:
        return {"error": "sandbox exec failed", "matches": [], "sandboxed": True}
    blob = (res.get("stdout") or "") + (res.get("stderr") or "")
    if "__VERA_ENOENT__" in blob:
        return {"error": f"Not a directory: {base}", "matches": [], "sandboxed": True}
    matches = []
    for ln in (res.get("stdout") or "").splitlines():
        parts = ln.split(":", 2)
        if len(parts) != 3:
            continue
        p, lineno, text = parts
        try:
            lineno_i = int(lineno)
        except Exception:
            continue
        rel = p[2:] if p.startswith("./") else p
        matches.append({"path": base + "/" + rel, "rel": rel, "line": lineno_i,
                        "col": 1, "text": text[:500], "match": "",
                        "context_before": [], "context_after": []})
    return {"root": base, "pattern": pattern, "is_regex": is_regex,
            "total_matches": len(matches), "truncated": len(matches) >= n,
            "matches": matches, "sandboxed": True}


async def export_workspace(session_id: str) -> Optional[str]:
    """Public wrapper over _collect_workspace: `docker cp` the session's
    /workspace to a fresh host temp dir (works on stopped containers too).
    Caller MUST rmtree the returned dir. None when there's no container."""
    return await _collect_workspace(session_id)


async def route_code_list_files(session_id: str, root: str = "", *,
                                max_files: int = 2000) -> Optional[Dict]:
    """Recursive file list under `root` INSIDE the session sandbox, shaped like
    ide.code.list_files ({root, count, files:[{rel, path, size}]}), or None when
    there's no active sandbox. Powers the chat artifacts/code panel."""
    dk, _host, _rec = await _sbx_host(session_id)
    if dk is None:
        return None
    base = (root or _WORKDIR).rstrip("/") or _WORKDIR
    q = shlex.quote(base)
    script = (
        f'cd {q} 2>/dev/null || {{ echo __VERA_ENOENT__; exit 3; }}; '
        f'find . -type f -not -path "*/.git/*" -not -path "*/node_modules/*" '
        f'! -name {shlex.quote(_SYNC_MARKER_NAME)} 2>/dev/null '
        f'| head -n {int(max_files)} | while IFS= read -r p; do '
        f's=$(stat -c %s "$p" 2>/dev/null || echo 0); '
        f'printf "%s\\t%s\\n" "$s" "$p"; done'
    )
    res = await _exec_in(session_id, script, timeout=45)
    if res is None:
        return {"root": base, "count": 0, "files": []}
    if "__VERA_ENOENT__" in (res.get("stdout", "") + res.get("stderr", "")):
        return {"root": base, "count": 0, "files": []}
    files = []
    for ln in (res.get("stdout") or "").splitlines():
        parts = ln.split("\t", 1)
        if len(parts) != 2:
            continue
        sz, p = parts
        rel = p[2:] if p.startswith("./") else p
        try:
            size = int(sz or 0)
        except Exception:
            size = 0
        files.append({"rel": rel, "path": base + "/" + rel, "size": size})
    files.sort(key=lambda f: f["rel"].lower())
    return {"root": base, "count": len(files), "files": files, "sandboxed": True}


async def route_artifact_dir(session_id: str, *, create: bool = True
                             ) -> Optional[str]:
    """The container artifact directory for a session with an ACTIVE sandbox — its
    /workspace (persistent per-session volume + exec WORKDIR) — or None (→ the
    caller uses the host artifact dir). Ensures the dir exists in-container so the
    agent is handed a path that its routed exec/code runs actually resolve.
    Auto-creates the container (auto_create default) — a session about to write
    artifacts is a session doing real work."""
    dk, _host, _rec = await _sbx_host(session_id, create=True)
    if dk is None:
        return None
    if create:
        # Also ensure the write-confinement dirs so a container created before
        # this feature (or a woken one) has real TMPDIR/cache targets.
        await _exec_in(session_id,
                       f"mkdir -p {shlex.quote(_SBX_ARTIFACT_DIR)} "
                       f"{shlex.quote(_WORKTMP)} {shlex.quote(_WORKDIR + '/.cache/pip')}",
                       timeout=20)
    return _SBX_ARTIFACT_DIR


# ═════════════════════════════════════════════════════════════════════════════
#  DURABLE SESSION STORE  (Phase 2)
#  Persist a session's /workspace to a durable "repo of sessions" so it can be
#  re-hydrated when the session is picked back up (dreaming / long-term work),
#  even on a different docker host. GARAGE object store is the PRIMARY, always-
#  available layer (tar.gz per version + a `workspace-latest.tar.gz` pointer, keys
#  under sessions/<sid>/); GITEA is a best-effort git mirror for history/browsing.
#  If Gitea is down at sync time the session is flagged `gitea_pending` and
#  replicated on the next sync once it's back online. Rehydration always reads
#  from Garage (the reliable source). `docker cp` is the binary-safe transport
#  (no in-container tar needed, not subject to the 1 MB exec-output cap, works
#  against local + remote daemons).
# ═════════════════════════════════════════════════════════════════════════════
STORE_PREFIX = "sessions"
_SYNC_MARKER_NAME = ".vera_synced"
_SYNC_MARKER = _WORKDIR + "/" + _SYNC_MARKER_NAME
# Periodic auto-sync cadence for long-running (e.g. dream) sessions. The scheduler
# ticks often but each session only re-syncs when its /workspace is DIRTY and its
# per-session interval has elapsed. 0 disables. Also settable via sandbox.config.
_AUTO_SYNC_DEFAULT = int(os.getenv("VERA_SANDBOX_AUTOSYNC_SEC", "900"))
# Idle auto-sleep: containers unused for this many minutes are STOPPED (docker
# stop — volume + layers kept) and wake automatically on next use. 0 disables.
_IDLE_SLEEP_DEFAULT = int(os.getenv("VERA_SANDBOX_IDLE_SLEEP_MIN", "30"))


def _object_store():
    m = sys.modules.get("data_fabric")
    if m is not None and hasattr(m, "OBJECT_STORE"):
        return m.OBJECT_STORE
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("data_fabric") and hasattr(mod, "OBJECT_STORE"):
            return mod.OBJECT_STORE
    return None


def _cfg():
    try:
        from Vera.vera.config import cfg
        return cfg
    except Exception:
        for name, mod in list(sys.modules.items()):
            if name.endswith("config") and hasattr(mod, "cfg"):
                return mod.cfg
    return None


def _store_seg(session_id: str) -> str:
    return "".join(c if (c.isalnum() or c in "_.-") else "-" for c in session_id)[:64] or "s"


def _store_key(session_id: str, name: str) -> str:
    return f"{STORE_PREFIX}/{_store_seg(session_id)}/{name}"


async def _sbx_host_any(session_id: str):
    """Like _sbx_host but does NOT require the sandbox to be `active` — only that a
    container exists (docker cp works on stopped containers too)."""
    rec = await _get_rec(await _resolve_sid(session_id))
    if not rec or not rec.get("container"):
        return None, None, None
    dk = _dk()
    if dk is None:
        return None, None, None
    host = await _docker_host(dk, rec.get("docker_host_id", "local"))
    if not host:
        return None, None, None
    return dk, host, rec


async def _collect_workspace(session_id: str) -> Optional[str]:
    """`docker cp` the container's /workspace into a fresh host temp dir. Returns
    the temp dir (caller MUST rmtree) or None."""
    dk, host, rec = await _sbx_host_any(session_id)
    if dk is None:
        return None
    tmp = tempfile.mkdtemp(prefix="vera-sbx-snap-")
    src = f"{rec['container']}:{_WORKDIR}/."
    cp = await dk._run_local(await dk._docker_argv(host, ["cp", src, tmp]), timeout=600)
    if not cp.get("ok"):
        shutil.rmtree(tmp, ignore_errors=True)
        log.warning("sandbox snapshot cp failed for %s: %s", session_id, cp.get("stderr"))
        return None
    return tmp


def _tar_dir(src_dir: str) -> str:
    fd, tarpath = tempfile.mkstemp(suffix=".tar.gz", prefix="vera-sbx-")
    os.close(fd)
    with tarfile.open(tarpath, "w:gz") as tf:
        for entry in sorted(os.listdir(src_dir)):
            if entry in (".git", _SYNC_MARKER_NAME):
                continue
            tf.add(os.path.join(src_dir, entry), arcname=entry)
    return tarpath


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _extract_tar(tarpath: str, dest: str) -> None:
    with tarfile.open(tarpath, "r:gz") as tf:
        try:
            tf.extractall(dest, filter="data")   # py3.12: block path traversal
        except TypeError:
            tf.extractall(dest)


def _iter_files(src_dir: str, *, max_files: int = 500, max_bytes: int = 2_000_000):
    """Yield (posix_relpath, bytes) for files under src_dir, bounded, skipping
    .git and oversized blobs — for the Gitea mirror."""
    n = 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            if fn == _SYNC_MARKER_NAME:
                continue
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > max_bytes:
                    continue
                data = _read_bytes(fp)
            except Exception:
                continue
            rel = os.path.relpath(fp, src_dir).replace("\\", "/")
            yield rel, data
            n += 1
            if n >= max_files:
                return


async def _gitea_reachable(base: str, headers: dict) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/api/v1/version", headers=headers)
            return r.status_code < 400
    except Exception:
        return False


async def _gitea_sync_tree(session_id: str, src_dir: str, *, message: str = "",
                           version: int = 0) -> Dict:
    """Best-effort mirror of the workspace tree to a per-session Gitea repo
    (vera-session-<sid>) via the contents API. Returns {ok, ...}."""
    cfg = _cfg()
    base = (getattr(cfg, "GITEA_BASE_URL", "") or "").rstrip("/") if cfg else ""
    token = getattr(cfg, "GITEA_TOKEN", "") if cfg else ""
    owner = getattr(cfg, "GITEA_OWNER", "") if cfg else ""
    if not (base and token and owner):
        return {"ok": False, "error": "gitea_not_configured"}
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
    if not await _gitea_reachable(base, headers):
        return {"ok": False, "error": "gitea_unreachable"}
    repo = "vera-session-" + _store_seg(session_id)
    import httpx
    pushed = 0
    failed = 0
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            # Ensure the repo exists (idempotent — try org then user namespace).
            await c.post(f"{base}/api/v1/orgs/{owner}/repos", headers=headers,
                         json={"name": repo, "auto_init": True, "private": True})
            await c.post(f"{base}/api/v1/user/repos", headers=headers,
                         json={"name": repo, "auto_init": True, "private": True})
            for rel, data in _iter_files(src_dir):
                url = f"{base}/api/v1/repos/{owner}/{repo}/contents/{rel}"
                payload = {"content": base64.b64encode(data).decode(),
                           "message": message or f"sync v{version}: {rel}"}
                try:
                    existing = await c.get(url, headers=headers)
                    if existing.status_code == 200:
                        payload["sha"] = existing.json().get("sha", "")
                        r = await c.put(url, headers=headers, json=payload)
                    else:
                        r = await c.post(url, headers=headers, json=payload)
                    if r.status_code in (200, 201):
                        pushed += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
        return {"ok": True, "repo": f"{base}/{owner}/{repo}", "pushed": pushed,
                "failed": failed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _sync_session(session_id: str, *, message: str = "") -> Dict:
    """Snapshot /workspace → Garage (primary) + Gitea (best-effort mirror)."""
    rec = await _get_rec(session_id)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session"}
    tmp = await _collect_workspace(session_id)
    if tmp is None:
        return {"ok": False, "error": "snapshot failed"}
    try:
        version = int(rec.get("store_version") or 0) + 1
        garage_ok = False
        store = _object_store()
        if store is not None and getattr(store, "mode", "none") != "none":
            # Stream the tarball from disk (upload_file) — never load it all into
            # memory, so very large workspaces don't blow the heap.
            tarpath = await asyncio.to_thread(_tar_dir, tmp)
            try:
                key = _store_key(session_id, f"workspace-v{version}.tar.gz")
                latest = _store_key(session_id, "workspace-latest.tar.gz")
                garage_ok = await asyncio.to_thread(
                    store.upload_file, key, tarpath, "application/gzip")
                if garage_ok:
                    await asyncio.to_thread(
                        store.upload_file, latest, tarpath, "application/gzip")
            finally:
                try: os.unlink(tarpath)
                except Exception: pass
        gitea = await _gitea_sync_tree(session_id, tmp, message=message, version=version)
        if garage_ok:
            rec["store_version"] = version
            rec["last_sync"] = now_iso()
        rec["gitea_pending"] = not bool(gitea.get("ok"))
        if gitea.get("ok"):
            rec["gitea_repo"] = gitea.get("repo", "")
        await _save_rec(rec)
        # Mark the workspace clean so the periodic auto-sync only fires on change.
        if garage_ok or gitea.get("ok"):
            try:
                await _exec_in(session_id, f"touch {shlex.quote(_SYNC_MARKER)}", timeout=15)
            except Exception:
                pass
        await emit_event({"type": "remote.sandbox.synced", "session_id": session_id,
                          "version": rec.get("store_version", 0), "garage": garage_ok,
                          "gitea": bool(gitea.get("ok"))})
        return {"ok": garage_ok or bool(gitea.get("ok")),
                "version": rec.get("store_version", 0), "garage": garage_ok,
                "gitea": gitea, "gitea_pending": rec["gitea_pending"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _restore_session(session_id: str) -> Dict:
    """Rehydrate /workspace from the Garage `latest` snapshot (the reliable
    source). No-op-safe: returns ok=False when there's nothing to restore."""
    dk, host, rec = await _sbx_host_any(session_id)
    if dk is None:
        return {"ok": False, "error": "no sandbox container"}
    store = _object_store()
    if store is None or getattr(store, "mode", "none") == "none":
        return {"ok": False, "error": "object store disabled"}
    latest = _store_key(session_id, "workspace-latest.tar.gz")
    if await asyncio.to_thread(store.stat, latest) is None:
        return {"ok": False, "error": "no snapshot in store"}
    tmp = tempfile.mkdtemp(prefix="vera-sbx-rst-")
    try:
        tarpath = os.path.join(tmp, "ws.tar.gz")
        # Stream the snapshot to disk (download_file) rather than into memory.
        if not await asyncio.to_thread(store.download_file, latest, tarpath):
            return {"ok": False, "error": "download failed"}
        extract = os.path.join(tmp, "x")
        os.makedirs(extract, exist_ok=True)
        await asyncio.to_thread(_extract_tar, tarpath, extract)
        cp = await dk._run_local(await dk._docker_argv(
            host, ["cp", extract + "/.", f"{rec['container']}:{_WORKDIR}"]), timeout=600)
        if not cp.get("ok"):
            return {"ok": False, "error": cp.get("stderr") or "cp into container failed"}
        size = os.path.getsize(tarpath) if os.path.exists(tarpath) else 0
        await emit_event({"type": "remote.sandbox.restored", "session_id": session_id,
                          "bytes": size})
        return {"ok": True, "restored_bytes": size}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _workspace_is_empty(session_id: str) -> bool:
    res = await _exec_in(session_id, f"ls -A {shlex.quote(_WORKDIR)} 2>/dev/null | head -1",
                         timeout=20)
    if res is None:
        return False
    return not (res.get("stdout") or "").strip()


@capability(
    "sandbox.session.sync",
    http_method="POST", http_path="/remote/sandbox/sync", http_tags=["remote", "sandbox"],
    description="Snapshot a session's /workspace to the durable session store: "
                "Garage object store (primary, versioned) + Gitea mirror (best-effort; "
                "replicated on the next sync if Gitea was offline). Inputs: session_id "
                "(str!), message (str). Output: {ok, version, garage, gitea, gitea_pending}.",
)
async def cap_sbx_sync(session_id: str = "", message: str = "", trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    return await _sync_session(session_id, message=message)


@capability(
    "sandbox.session.restore",
    http_method="POST", http_path="/remote/sandbox/restore", http_tags=["remote", "sandbox"],
    description="Rehydrate a session's /workspace from the Garage `latest` snapshot "
                "into its (running) sandbox container. Input: session_id (str!). "
                "Output: {ok, restored_bytes}.",
)
async def cap_sbx_restore(session_id: str = "", trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    return await _restore_session(session_id)


@capability(
    "sandbox.session.snapshots",
    http_method="POST", http_path="/remote/sandbox/snapshots", http_tags=["remote", "sandbox"],
    memory="off", silent=True,
    description="List the durable snapshots stored for a session in Garage. "
                "Input: session_id (str!). Output: {snapshots: [{key, size, last_modified}], "
                "gitea_repo, gitea_pending}.",
)
async def cap_sbx_snapshots(session_id: str = "", trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    store = _object_store()
    objs = []
    if store is not None and getattr(store, "mode", "none") != "none":
        objs = await asyncio.to_thread(store.list_objects,
                                       _store_key(session_id, ""), "", 1000)
    rec = await _get_rec(session_id) or {}
    return {"snapshots": objs, "gitea_repo": rec.get("gitea_repo", ""),
            "gitea_pending": bool(rec.get("gitea_pending"))}


async def _workspace_dirty(session_id: str) -> bool:
    """Has /workspace changed since the last sync? Cheap in-container check against
    the `.vera_synced` marker (portable: busybox + coreutils find both support
    `-newer`). Missing marker ⇒ dirty (never synced / restored)."""
    script = (f'if [ ! -e {shlex.quote(_SYNC_MARKER)} ]; then echo DIRTY; exit 0; fi; '
              f'find {shlex.quote(_WORKDIR)} ! -name {shlex.quote(_SYNC_MARKER_NAME)} '
              f'-newer {shlex.quote(_SYNC_MARKER)} 2>/dev/null | head -1')
    res = await _exec_in(session_id, script, timeout=20)
    if res is None:
        return False
    return bool((res.get("stdout") or "").strip())


async def _auto_sync_tick() -> None:
    """Scheduler tick: sync each ACTIVE sandbox whose /workspace is dirty and whose
    per-session interval has elapsed — so long-running (dream) sessions persist
    without waiting for stop. Interval 0 disables."""
    try:
        cfg = await _get_cfg()
        interval = int(cfg.get("auto_sync_interval", _AUTO_SYNC_DEFAULT) or 0)
        if interval <= 0:
            return
        r = _redis()
        if not r:
            return
        # Skip the whole tick (avoid wasteful docker cp/tar) when there's nowhere
        # to sync TO — neither the object store nor Gitea is configured.
        store = _object_store()
        store_on = store is not None and getattr(store, "mode", "none") != "none"
        vcfg = _cfg()
        gitea_on = bool(vcfg and getattr(vcfg, "GITEA_BASE_URL", "")
                        and getattr(vcfg, "GITEA_TOKEN", "")
                        and getattr(vcfg, "GITEA_OWNER", ""))
        if not (store_on or gitea_on):
            return
        items = await r.hgetall(KEY_SBX)
        now = time.time()
        for v in (items or {}).values():
            try:
                rec = json.loads(v)
            except Exception:
                continue
            if not rec.get("active") or not rec.get("container"):
                continue
            sid = rec.get("session_id")
            if not sid:
                continue
            if now - float(rec.get("last_autosync") or 0) < interval:
                continue
            try:
                if not await _workspace_dirty(sid):
                    # Still stamp the check time so we don't hammer find every tick.
                    rec["last_autosync"] = now
                    await _save_rec(rec)
                    continue
                await _sync_session(sid, message="auto-sync")
                rec2 = await _get_rec(sid) or rec
                rec2["last_autosync"] = now
                await _save_rec(rec2)
            except Exception as e:
                log.debug("auto-sync for %s failed: %s", sid, e)
    except Exception as e:
        log.debug("sandbox auto-sync tick failed: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
#  SANDBOX HOST PROVISIONING — create a dedicated, desktopless Docker host
#  (Proxmox LXC with nesting) purely for running session sandbox containers,
#  register it in the docker host registry, and (optionally) make it the
#  default sandbox host.
# ═════════════════════════════════════════════════════════════════════════════
_DOCKER_INSTALL_SH = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "command -v docker >/dev/null 2>&1 || "
    "(apt-get update && apt-get install -y curl ca-certificates && "
    "curl -fsSL https://get.docker.com | sh); "
    "systemctl enable --now docker 2>/dev/null || service docker start || true; "
    "docker info --format '{{.ServerVersion}}'"
)


def _cap_fn(name: str):
    reg = getattr(_orch, "CAPABILITY_REGISTRY", {}) or {}
    cap = reg.get(name)
    return cap.get("func") if cap else None


async def _lxc_ip(cluster_id: str, node: str, vmid: int) -> str:
    """Poll the CT's DHCP address via the PVE interfaces endpoint."""
    pm = None
    for n, m in list(sys.modules.items()):
        if m is not None and n.endswith("proxmox_capabilities") and hasattr(m, "_pve"):
            pm = m
            break
    if pm is None:
        return ""
    try:
        rec = await pm._get_cluster(cluster_id, opened=True)
        if not rec:
            return ""
        data, err = await pm._pve(rec, "GET", f"/nodes/{node}/lxc/{int(vmid)}/interfaces")
        if err or not isinstance(data, list):
            return ""
        for itf in data:
            if (itf.get("name") or "") in ("lo",):
                continue
            for k in ("inet", "inet4"):
                ip = str(itf.get(k) or "").split("/")[0]
                if ip and not ip.startswith("127."):
                    return ip
    except Exception:
        pass
    return ""


@capability(
    "sandbox.host.provision",
    http_method="POST", http_path="/remote/sandbox/host/provision", http_tags=["remote", "sandbox"],
    description="Provision a dedicated Docker host for session-sandbox containers: "
                "creates a light desktopless Proxmox LXC (nesting enabled), installs "
                "Docker in it over SSH, registers it in the Docker host registry and "
                "(by default) makes it the sandbox default host. Inputs: cluster_id "
                "(str!), node (str! — PVE node), ostemplate (str! — vztmpl volid, "
                "e.g. 'local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst'), "
                "password (str! — CT root password, also stored as the SSH cred), "
                "hostname (str='vera-sbx-host'), cores (int=4), memory (int=4096 MB), "
                "disk (int=32 GB), storage (str='local-lvm'), net0 (str — default "
                "DHCP on vmbr0), make_default (bool=true — point sandbox.config at "
                "it), wait_secs (int=120 — max wait for the CT to get an IP). "
                "Output: {ok, vmid, ip, docker_host_id, steps:[{step, ok, detail}]}.",
)
async def cap_sbx_host_provision(cluster_id: str = "", node: str = "",
                                 ostemplate: str = "", password: str = "",
                                 hostname: str = "vera-sbx-host",
                                 cores: int = 4, memory: int = 4096,
                                 disk: int = 32, storage: str = "local-lvm",
                                 net0: str = "", make_default: bool = True,
                                 wait_secs: int = 120, trace_id=None) -> Dict:
    steps: List[Dict[str, Any]] = []

    def _step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": ok, "detail": str(detail)[:400]})

    if not (cluster_id and node and ostemplate and password):
        return {"ok": False, "error": "cluster_id, node, ostemplate and password required",
                "steps": steps}

    # 1) Create the LXC (nesting+keyctl → docker works inside).
    lxc_create = _cap_fn("proxmox.lxc.create")
    if lxc_create is None:
        return {"ok": False, "error": "proxmox module not loaded", "steps": steps}
    ct = await lxc_create(cluster_id=cluster_id, node=node, ostemplate=ostemplate,
                          hostname=hostname, storage=storage, cores=int(cores),
                          memory=int(memory), disk=int(disk), password=password,
                          net0=net0, unprivileged=True,
                          features="nesting=1,keyctl=1", start=True)
    if not ct.get("ok"):
        _step("lxc.create", False, ct.get("error", ""))
        return {"ok": False, "error": ct.get("error", "lxc create failed"), "steps": steps}
    vmid = int(ct.get("vmid") or 0)
    _step("lxc.create", True, f"vmid={vmid}")

    # 2) Wait for the CT to come up and get an IP.
    ip = ""
    deadline = time.time() + max(30, int(wait_secs))
    while time.time() < deadline:
        ip = await _lxc_ip(cluster_id, node, vmid)
        if ip:
            break
        await asyncio.sleep(5)
    if not ip:
        _step("await-ip", False, "no IP within wait window")
        return {"ok": False, "error": "CT did not get an IP in time (check net0/DHCP)",
                "vmid": vmid, "steps": steps}
    _step("await-ip", True, ip)

    # 3) Store the SSH credential.
    ssh_save = _cap_fn("exec.ssh.hosts.save")
    if ssh_save is None:
        return {"ok": False, "error": "exec module not loaded", "vmid": vmid,
                "ip": ip, "steps": steps}
    sh = await ssh_save(host=ip, user="root", auth="password", password=password,
                        label=f"{hostname} (sandbox docker host)", tags="sandbox,docker")
    if not sh.get("ok"):
        _step("ssh.save", False, sh.get("error", ""))
        return {"ok": False, "error": sh.get("error", "ssh save failed"),
                "vmid": vmid, "ip": ip, "steps": steps}
    ssh_host_id = (sh.get("host") or {}).get("id", "")
    _step("ssh.save", True, ssh_host_id)

    # 4) Install Docker over SSH (idempotent; sshd may take a moment to accept).
    ssh_run = _cap_fn("exec.ssh.run")
    if ssh_run is None:
        return {"ok": False, "error": "exec.ssh.run unavailable", "vmid": vmid,
                "ip": ip, "steps": steps}
    res: Dict[str, Any] = {}
    ver = ""
    for attempt in range(4):
        res = await ssh_run(host_id=ssh_host_id, command=_DOCKER_INSTALL_SH,
                            timeout=900)
        if res.get("ok") and (res.get("stdout") or "").strip():
            ver = (res.get("stdout") or "").strip().splitlines()[-1]
            break
        await asyncio.sleep(8)
    if not ver:
        _step("docker.install", False, (res or {}).get("stderr", "")[:300])
        return {"ok": False, "error": "docker install/verify failed",
                "vmid": vmid, "ip": ip, "steps": steps}
    _step("docker.install", True, f"docker {ver}")

    # 5) Register as a Docker host (ssh kind).
    dk_save = _cap_fn("docker.hosts.save")
    if dk_save is None:
        return {"ok": False, "error": "docker module not loaded", "vmid": vmid,
                "ip": ip, "steps": steps}
    dh = await dk_save(kind="ssh", label=hostname, ssh_host_id=ssh_host_id)
    if not dh.get("ok"):
        _step("docker.hosts.save", False, dh.get("error", ""))
        return {"ok": False, "error": dh.get("error", "docker host register failed"),
                "vmid": vmid, "ip": ip, "steps": steps}
    docker_host_id = (dh.get("host") or {}).get("id", "")
    _step("docker.hosts.save", True, docker_host_id)

    # 6) Point the sandbox system at it.
    if make_default and docker_host_id:
        c = await cap_sbx_cfg_set(docker_host_id=docker_host_id)
        _step("sandbox.config", bool(c.get("ok")), c.get("error", "") or docker_host_id)

    await emit_event({"type": "remote.sandbox.host_provisioned", "vmid": vmid,
                      "ip": ip, "docker_host_id": docker_host_id})
    return {"ok": True, "vmid": vmid, "ip": ip, "docker_host_id": docker_host_id,
            "ssh_host_id": ssh_host_id, "steps": steps}


async def _idle_sleep_tick() -> None:
    """Scheduler tick: docker-stop ACTIVE containers that have been idle for
    idle_sleep_minutes (0 = disabled). They wake automatically on next use via
    _ensure_routable. Containers with no last_used stamp get one now (grace
    period) instead of being stopped immediately."""
    try:
        cfg = await _get_cfg()
        idle_min = int(cfg.get("idle_sleep_minutes", _IDLE_SLEEP_DEFAULT) or 0)
        if idle_min <= 0:
            return
        r = _redis()
        dk = _dk()
        if not r or dk is None:
            return
        items = await r.hgetall(KEY_SBX)
        now = time.time()
        for v in (items or {}).values():
            try:
                rec = json.loads(v)
            except Exception:
                continue
            if not rec.get("active") or not rec.get("container"):
                continue
            last = float(rec.get("last_used") or 0)
            if not last:
                rec["last_used"] = now
                await _save_rec(rec)
                continue
            if now - last < idle_min * 60:
                continue
            try:
                host = await _docker_host(dk, rec.get("docker_host_id", "local"))
                if not host:
                    continue
                if (await _container_running(dk, host, rec["container"])) != "running":
                    continue
                await cap_sbx_sleep(session_id=rec["session_id"])
                log.info("sandbox %s idle %dmin — slept", rec["session_id"], idle_min)
            except Exception as e:
                log.debug("idle sleep for %s failed: %s", rec.get("session_id"), e)
    except Exception as e:
        log.debug("sandbox idle-sleep tick failed: %s", e)


# Register the periodic auto-sync + idle-sleep (the scheduler_loop invokes them;
# each tick throttles per-session and no-ops when its config interval is 0).
try:
    schedule(_auto_sync_tick, 60, name="sandbox_auto_sync")
except Exception as _e:
    log.debug("could not register sandbox auto-sync: %s", _e)
try:
    schedule(_idle_sleep_tick, 120, name="sandbox_idle_sleep")
except Exception as _e:
    log.debug("could not register sandbox idle-sleep: %s", _e)


# Keep sandbox LIFECYCLE caps out of agent-loop toolkits: a loop already runs
# INSIDE its owner's container (routed transparently by exec/code/fs hooks), so
# letting it call start/link/stop/commit agentically is exactly the nested-
# sandbox path we forbid. exec/run_code/fs.* stay available — they operate in
# the loop's own container. (The RUN_OWNER guard above additionally covers
# deterministic in-code calls.)
def _extend_loop_blacklist() -> None:
    dw = (sys.modules.get("dag_workshop_capabilities")
          or sys.modules.get("Vera.vera.dag.dag_workshop_capabilities"))
    try:
        bl = getattr(dw, "_DEFAULT_CAP_BLACKLIST", None)
        if isinstance(bl, set):
            bl.update({
                "sandbox.session.start", "sandbox.session.stop",
                "sandbox.session.link", "sandbox.session.commit",
                "sandbox.session.set_active", "sandbox.session.sleep",
                "sandbox.config.set", "sandbox.host.provision",
            })
    except Exception as e:
        log.debug("sandbox: could not extend loop blacklist: %s", e)


_extend_loop_blacklist()

log.info("session_sandbox_capabilities loaded — opt-in per-session containers, "
         "run-owner scoped (one container per project/goal/dream/program)")
