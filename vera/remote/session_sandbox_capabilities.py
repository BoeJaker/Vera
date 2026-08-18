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
import re
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
# Directory dropped on the container's PYTHONPATH holding a sitecustomize.py that
# auto-loads for every `python` invocation — see _SITECUSTOMIZE_PY / _ensure_sandbox_pyenv.
_SITE_DIR = _WORKDIR + "/.python"

_CONFINE_ENV = {
    "HOME": _WORKDIR, "TMPDIR": _WORKTMP, "TMP": _WORKTMP, "TEMP": _WORKTMP,
    "XDG_CACHE_HOME": _WORKDIR + "/.cache",
    "PIP_CACHE_DIR": _WORKDIR + "/.cache/pip",
    # Auto-load our sitecustomize so sandbox scripts' outbound HTTP looks human.
    "PYTHONPATH": _SITE_DIR,
}

# A realistic desktop-Chrome UA + companion headers, overridable per-deployment via
# VERA_SANDBOX_HTTP_UA. Kept in sync in spirit with the web_client.py fingerprint
# the HTTP CAPS already use — the point is that scripts RUN IN THE SANDBOX (urllib/
# requests/httpx) look the same, so public sites don't bot-block them.
_SANDBOX_HTTP_UA = os.getenv(
    "VERA_SANDBOX_HTTP_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36")

# sitecustomize.py (Python auto-imports this when it's on sys.path): patch the
# default outbound HTTP headers of urllib, requests and httpx so a bare
# `urllib.request.urlopen(url)` / `requests.get(url)` from an agent-written script
# presents as a real browser instead of "Python-urllib/3.x" (which PokeAPI, GitHub
# raw, Cloudflare-fronted sites, etc. throttle or 403 as an obvious bot).
# NOTE: built with a token replace, NOT str.format — the body contains literal
# `{}` (dict/set) which str.format would misread as replacement fields (that bug
# once raised IndexError at import and took the whole sandbox module down).
_SITECUSTOMIZE_TEMPLATE = '''\
# Vera sandbox — make outbound HTTP look like a real browser (auto-loaded).
import os
_UA = os.environ.get("VERA_HTTP_UA") or __VERA_UA__
# Advertise ONLY encodings the HTTP STACK can actually DECODE, and take that from
# urllib3 itself rather than guessing.
#
# These headers are injected by hand and OVERRIDE urllib3's own negotiation, so
# any mismatch silently corrupts every response: the server compresses with
# something we asked for, nothing decompresses it, and the script sees 200 OK with
# binary garbage under `Content-Type: application/json`. .json() then dies with
# "Expecting value: line 1 column 1 (char 0)", which reads as a broken API rather
# than a bad header — an agent will rewrite its parser forever and never fix it.
#
# Two real cases hit this: `br` advertised with no brotli package (the base image
# ships none), and then `zstd` advertised merely because `zstandard` imports —
# urllib3 2.7 does NOT decode zstd even so. Module importability is the wrong
# test; urllib3's ACCEPT_ENCODING is the authoritative one, derived from its
# actual decoder table.
_ENC = "gzip, deflate"
try:
    from urllib3.util.request import ACCEPT_ENCODING as _AE
    if _AE:
        _ENC = _AE
except Exception:
    try:
        from urllib3.response import HTTPResponse as _HR
        _cd = [c for c in (getattr(_HR, "CONTENT_DECODERS", None) or []) if c != "x-gzip"]
        if _cd:
            _ENC = ", ".join(_cd)
    except Exception:
        pass
_HDRS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": _ENC,
    "Sec-CH-UA": '"Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-CH-UA-Platform": '"Linux"',
    "Upgrade-Insecure-Requests": "1",
}
try:
    import urllib.request as _u
    _op = _u.build_opener()
    # urllib.request does NOT transparently decompress — urlopen() hands back the
    # raw body. So it must ask for `identity`: inheriting the requests/urllib3
    # Accept-Encoding would guarantee an unreadable gzip/br response for every
    # bare urlopen() an agent script makes.
    _uh = dict(_HDRS)
    _uh["Accept-Encoding"] = "identity"
    _op.addheaders = list(_uh.items())
    _u.install_opener(_op)
except Exception:
    pass
try:
    import requests as _r
    _orig = _r.sessions.Session.__init__
    def _patched(self, *a, **k):
        _orig(self, *a, **k)
        try:
            self.headers.update(_HDRS)
        except Exception:
            pass
    _r.sessions.Session.__init__ = _patched
except Exception:
    pass
try:
    import httpx as _h
    _oc = _h.Client.__init__
    def _pc(self, *a, **k):
        h = dict(k.get("headers") or {})
        for _kk, _vv in _HDRS.items():
            h.setdefault(_kk, _vv)
        k["headers"] = h
        return _oc(self, *a, **k)
    _h.Client.__init__ = _pc
except Exception:
    pass
'''
_SITECUSTOMIZE_PY = _SITECUSTOMIZE_TEMPLATE.replace("__VERA_UA__", repr(_SANDBOX_HTTP_UA))


async def _ensure_sandbox_pyenv(dk, host, cname: str) -> None:
    """Drop sitecustomize.py into the container's PYTHONPATH so EVERY python script's
    outbound HTTP (urllib/requests/httpx) carries realistic browser headers — public
    sites stop bot-blocking sandbox scripts — and ensure `requests` is importable so
    scripts don't fail on ImportError and fall back to a bot-blocked urllib. Idempotent
    + best-effort; never blocks/breaks container start on failure.

    Also installs the brotli/zstd DECODERS. The browser fingerprint above asks for
    `br`, and Cloudflare-fronted APIs honour it — without a decoder the body comes
    back as raw compressed bytes and every .json() in every agent-written script
    fails with a misleading parse error. The base image (python:3.12-slim) ships
    neither, so this is what makes the fingerprint safe to send."""
    try:
        b64 = base64.b64encode(_SITECUSTOMIZE_PY.encode("utf-8")).decode()
        script = (
            f"mkdir -p {shlex.quote(_SITE_DIR)} && "
            f"echo {b64} | base64 -d > {shlex.quote(_SITE_DIR)}/sitecustomize.py; "
            "python3 -c 'import requests' 2>/dev/null || "
            "pip install --quiet --disable-pip-version-check requests 2>/dev/null || true; "
            # Decoders for the Accept-Encoding the fingerprint advertises.
            "python3 -c 'import brotli' 2>/dev/null || "
            "pip install --quiet --disable-pip-version-check brotli 2>/dev/null || true; "
            "python3 -c 'import zstandard' 2>/dev/null || "
            "pip install --quiet --disable-pip-version-check zstandard 2>/dev/null || true; "
            # bs4/lxml: the Python equivalent of the curl case below. Any step that
            # scrapes reaches for BeautifulSoup by reflex, and a model has no way to
            # know it is absent until its script dies on ImportError — after which
            # the step burns cycles rewriting around a gap that one pip install
            # closes. lxml comes along because bs4 without a parser backend falls
            # back to the slow built-in and warns on every construction.
            "python3 -c 'import bs4, lxml' 2>/dev/null || "
            "pip install --quiet --disable-pip-version-check beautifulsoup4 lxml "
            "2>/dev/null || true; "
            # curl: the single most commonly reached-for CLI tool the base image
            # lacks (it has python3/pip and little else) — install it up front so
            # the FIRST command a step runs doesn't have to hit the "not found" →
            # auto-install-and-retry path at all. Everything else missing is
            # still covered reactively by _auto_install_missing_bin_and_retry.
            "command -v curl >/dev/null 2>&1 || "
            "(apt-get update -qq && apt-get install -y -qq --no-install-recommends curl "
            "2>/dev/null) || true")
        await dk._run_local(
            await dk._docker_argv(host, ["exec", cname, "sh", "-lc", script]), timeout=300)
    except Exception as e:
        log.debug("sandbox pyenv setup failed for %s: %s", cname, e)


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


# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL (IN-CONTAINER) BACKEND
#
#  A session sandbox normally IS a dedicated docker container. But this same
#  code runs INSIDE a Loop-Lab dev sandbox — itself a container with NO docker
#  socket and no docker CLI — where `docker run` is impossible, so the loop's
#  file-authoring / exec steps had nowhere to go and the run couldn't produce
#  anything. Rather than nest containers (docker-in-docker), the dev container
#  USES ITSELF as the sandbox: exec runs as a local subprocess and files live on
#  its own filesystem under a workspace dir. Everything already funnels through
#  `_exec_in`, so a single local branch there (plus recognising a local record
#  in the routing gates) makes the whole surface work.
#
#  SAFETY — this path is INERT wherever real docker exists. The trigger is a
#  STRUCTURAL absence of docker (no socket, no DOCKER_HOST, no `docker` on PATH),
#  a deterministic fact that a transient daemon hiccup can NOT flip — so prod
#  (which has the socket) never takes the local path and its behaviour is
#  completely unchanged.
# ─────────────────────────────────────────────────────────────────────────────
_LOCAL_BACKEND = "local"
# One shared local workspace for the whole process (this container IS the one
# "container"), resolved once to the first writable of: an explicit override,
# the canonical /workspace (so absolute /workspace/... references still resolve),
# then a temp dir. Cached so every session in this process agrees on it.
_LOCAL_WS_CACHE: Optional[str] = None
_DOCKER_ABSENT_CACHE: Optional[bool] = None


def _docker_structurally_absent() -> bool:
    """True only when docker is STRUCTURALLY unavailable to THIS process: no
    DOCKER_HOST, no /var/run/docker.sock, and no `docker` binary on PATH. This is
    a fact about the environment, not a live daemon probe, so it can never be
    flipped by a transient error — prod (socket present) is always False here."""
    global _DOCKER_ABSENT_CACHE
    if _DOCKER_ABSENT_CACHE is not None:
        return _DOCKER_ABSENT_CACHE
    absent = True
    try:
        if os.environ.get("DOCKER_HOST"):
            absent = False
        elif os.path.exists("/var/run/docker.sock"):
            absent = False
        elif shutil.which("docker"):
            absent = False
    except Exception:
        absent = False   # unsure → assume docker present (never divert prod)
    _DOCKER_ABSENT_CACHE = absent
    return absent


def _local_backend_ok(docker_host_id: str = "") -> bool:
    """Should this session use the local in-container backend? Only when docker is
    structurally absent AND the target host is the (missing) LOCAL one — a session
    explicitly pinned to a reachable REMOTE docker host must still use docker."""
    hid = (docker_host_id or "local").strip().lower()
    if hid not in ("", "local"):
        return False
    return _docker_structurally_absent()


def _local_ws_dir() -> str:
    """The one local workspace dir for this process, created on first use."""
    global _LOCAL_WS_CACHE
    if _LOCAL_WS_CACHE:
        return _LOCAL_WS_CACHE
    candidates = [
        os.environ.get("VERA_LOCAL_SANDBOX_ROOT", "").strip(),
        _WORKDIR,   # "/workspace" — matches the absolute paths caps emit
        os.path.join(tempfile.gettempdir(), "vera-local-workspace"),
    ]
    for cand in candidates:
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".vera_wtest")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            _LOCAL_WS_CACHE = cand
            return cand
        except Exception:
            continue
    # Last resort — a unique temp dir that must be creatable.
    _LOCAL_WS_CACHE = tempfile.mkdtemp(prefix="vera-local-ws-")
    return _LOCAL_WS_CACHE


def _local_rec(session_id: str, *, active: bool = True, kind: str = "session",
               label: str = "") -> Dict:
    """A sandbox record for the local backend — shaped like a docker one so the
    rest of the module (list/status/routing) reads it uniformly."""
    return {"session_id": session_id, "container": f"local:{_cname(session_id)}",
            "backend": _LOCAL_BACKEND, "workdir": _local_ws_dir(),
            "image": _LOCAL_BACKEND, "active": bool(active),
            "kind": kind or "session", "label": label or "",
            "created_at": now_iso(), "sessions": [session_id]}


def _is_local_rec(rec: Optional[Dict]) -> bool:
    return bool(rec) and rec.get("backend") == _LOCAL_BACKEND


async def _exec_local(command: str, *, workdir: str = "", timeout: int = 120,
                      shell: str = "sh") -> Dict:
    """Run `command` as a LOCAL subprocess in this container (the local backend's
    equivalent of `docker exec`). Returns the same exec-shaped dict `_exec_in`
    does for the docker path."""
    wd = workdir or _local_ws_dir()
    try:
        os.makedirs(wd, exist_ok=True)
    except Exception:
        pass
    argv = _shell_argv(shell, command)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=wd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL)
    except FileNotFoundError as e:
        return {"ok": False, "rc": 127, "stdout": "", "stderr": str(e),
                "sandboxed": True, "backend": _LOCAL_BACKEND}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=max(1, int(timeout)))
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "rc": -1, "stdout": "",
                "stderr": f"timed out after {int(timeout)}s", "sandboxed": True,
                "backend": _LOCAL_BACKEND}
    return {"ok": proc.returncode == 0, "rc": proc.returncode,
            "stdout": (out or b"").decode("utf-8", "replace"),
            "stderr": (err or b"").decode("utf-8", "replace"),
            "sandboxed": True, "backend": _LOCAL_BACKEND}


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


# ── Bulk container-state map (event-loop protection) ─────────────────────────
# `sandbox.session.list` is silently polled by the Sandboxes/Workers UI. It used
# to fire ONE `/containers/json` docker round-trip PER sandbox record; with 100+
# historical sandboxes that is a serial storm of docker inspects on the event
# loop every poll, and concurrent pollers kept the loop saturated — enough to
# starve TLS handshakes (server appears down while the port stays open). We now
# do ONE bulk query per docker host (the `vera-sbx-` name prefix is a docker
# substring filter that returns them all) and cache it for a few seconds so a
# burst of pollers collapses to a single call.
_CSTATE_CACHE: Dict[str, Any] = {}   # host_key -> (expiry_monotonic, {cname: state})
_CSTATE_TTL = float(os.getenv("VERA_SBX_STATE_TTL", "3.0"))


async def _containers_state_map(dk, rec_host: Dict, host_key: str,
                                prefix: str = "vera-sbx-") -> Dict[str, str]:
    """ONE docker call per host → {container_name: State}, TTL-cached."""
    now = time.monotonic()
    hit = _CSTATE_CACHE.get(host_key)
    if hit and hit[0] > now:
        return hit[1]
    import urllib.parse
    filt = urllib.parse.quote(json.dumps({"name": [prefix]}))
    m: Dict[str, str] = {}
    try:
        status, body, _ = await dk._engine_request(
            rec_host, "GET", f"/containers/json?all=true&filters={filt}")
        rows = json.loads(body or b"[]") if status == 200 else []
        for row in rows:
            st = row.get("State", "")
            for n in (row.get("Names") or []):
                m[n.lstrip("/")] = st
    except Exception:
        pass
    _CSTATE_CACHE[host_key] = (now + _CSTATE_TTL, m)
    return m


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
    # Local backend: no docker container to wake/health-check — the workspace is
    # this process's own filesystem, so an active local record is always routable.
    if _is_local_rec(rec) and rec.get("active"):
        await _touch(rec)
        return rec
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
    # ── LOCAL BACKEND ───────────────────────────────────────────────────────
    # No docker anywhere reachable (a Loop-Lab dev sandbox with no socket): use
    # THIS container as the sandbox instead of failing. Preserve an existing
    # record's kind/label; the workspace is a real dir on this filesystem.
    if _local_backend_ok(docker_host_id):
        prev = await _get_rec(session_id) or {}
        rec = _local_rec(session_id, active=bool(enable),
                         kind=kind or prev.get("kind", "session"),
                         label=label or prev.get("label", ""))
        if prev.get("sessions"):
            rec["sessions"] = sorted(set(prev["sessions"]) | {session_id})
        await _save_rec(rec)
        ws = rec["workdir"]
        try:
            os.makedirs(ws, exist_ok=True)
        except Exception:
            pass
        return {"ok": True, "container_id": rec["container"], "image": _LOCAL_BACKEND,
                "restored": False, "backend": _LOCAL_BACKEND, "workdir": ws,
                "active": bool(enable)}
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
                # Refresh the python HTTP-shaping env — cheap + idempotent, and it
                # back-fills containers created before this feature existed.
                await _ensure_sandbox_pyenv(dk, host, cname)
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
    # Human-shaped outbound HTTP + a usable `requests` for agent scripts.
    await _ensure_sandbox_pyenv(dk, host, cname)

    # Standing preload (sandbox.config package_preload): the set the user has
    # decided EVERY sandbox should have, so the common libraries are never an
    # approval prompt on a fresh container. Applied before the per-call
    # `packages` so an explicit request can still override a version.
    preload = _pkg_listcfg(scfg, "package_preload")
    if preload:
        quoted = " ".join(shlex.quote(n) for n in preload)
        await dk._run_local(
            await dk._docker_argv(host, ["exec", cname, "sh", "-lc",
                                         f"pip install --quiet --disable-pip-version-check "
                                         f"{quoted} 2>/dev/null || true"]), timeout=900)

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

    # Populate a fresh /workspace: prefer a configured host SEED (e.g. an IDE
    # workspace's project files) so a loop can operate on them; else rehydrate
    # from the durable session store to resume long-term work. Never clobbers an
    # already-populated volume.
    rehydrated = False
    seeded = False
    if rehydrate:
        try:
            if await _workspace_is_empty(session_id):
                sp = str(rec.get("seed_path") or "").strip()
                if sp and os.path.isdir(sp):
                    seeded = await _seed_from_host(dk, host, cname, sp)
                    if seeded:
                        rec["seeded_at"] = time.time()
                        await _save_rec(rec)
                if not seeded:
                    r = await _restore_session(session_id)
                    rehydrated = bool(r.get("ok"))
        except Exception as e:
            log.debug("sandbox seed/rehydrate skipped for %s: %s", session_id, e)

    await emit_event({"type": "remote.sandbox.started", "session_id": session_id,
                      "image": image, "restored": restored, "rehydrated": rehydrated,
                      "seeded": seeded})
    return {"ok": True, "container_id": run.get("container_id", cname), "image": image,
            "restored": restored, "active": bool(enable), "rehydrated": rehydrated,
            "seeded": seeded}


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
    # Local backend: the "container" is this process — always running, no docker.
    if _is_local_rec(rec):
        return {"exists": True, "active": bool(rec.get("active")), "running": True,
                "container": rec.get("container"), "image": _LOCAL_BACKEND,
                "committed": False, "backend": _LOCAL_BACKEND,
                "linked_to": resolved if resolved != session_id else "",
                "kind": rec.get("kind", "session"), "label": rec.get("label", ""),
                "sessions": len(rec.get("sessions") or [])}
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
    # Local backend: run in THIS container as a subprocess, no docker.
    if _is_local_rec(rec):
        return await _exec_local(command, workdir=workdir or rec.get("workdir", ""),
                                 timeout=timeout, shell=shell)
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
    # Local backend: no container to stop/commit — just mark it inactive. The
    # workspace is a plain dir on this filesystem; leave its files in place.
    if _is_local_rec(rec):
        rec["active"] = False
        rec["updated"] = now_iso()
        await _save_rec(rec)
        await emit_event({"type": "remote.sandbox.stopped", "session_id": session_id,
                          "backend": _LOCAL_BACKEND})
        return {"ok": True, "backend": _LOCAL_BACKEND, "committed_image": "", "synced": None}
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
        # cap_sbx_commit correctly persists committed_image on ITS OWN copy of
        # the record — but `rec` here was fetched before that call, so the
        # plain _save_rec(rec) below would silently overwrite that update back
        # to empty. Merge it into the local copy so the real archive actually
        # survives the stop — without this, cap_sbx_start never finds a
        # committed_image to restore from and just builds fresh every time.
        if committed:
            rec["committed_image"] = committed
            rec["committed_at"] = now_iso()
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
    recs = []
    for v in items.values():
        try:
            recs.append(json.loads(v))
        except Exception:
            continue
    # ONE bulk docker query per host (cached) instead of one per container —
    # collapses the per-poll serial inspect storm that used to starve the loop.
    state_maps: Dict[str, Dict[str, str]] = {}
    if dk:
        for hid in {rec.get("docker_host_id", "local")
                    for rec in recs if rec.get("container") and not _is_local_rec(rec)}:
            try:
                host = await _docker_host(dk, hid)
                state_maps[hid] = await _containers_state_map(dk, host, hid) if host else {}
            except Exception:
                state_maps[hid] = {}
    out = []
    for rec in recs:
        state = ""
        if _is_local_rec(rec):
            state = "running" if rec.get("active") else "stopped"
        elif dk and rec.get("container"):
            hid = rec.get("docker_host_id", "local")
            state = state_maps.get(hid, {}).get(rec["container"], "absent")
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


@APP.get("/remote/sandbox/preview/{session_id}/{path:path}", include_in_schema=False)
async def _sandbox_preview_file(session_id: str, path: str):
    """Serve ONE file straight out of a session sandbox's working directory, live
    (re-read on every request, never cached) — so a browser (operator.run's
    Playwright session in particular) can actually load and render/click a file a
    loop step just authored inside the sandbox.

    Exists because there was no reachable URL for anything a step wrote — a
    session sandbox is a container, not a network-routable host, and the only
    prior way to look at its files was `ide.fs.read`-style text extraction. This
    reuses that exact same read path (`read_artifact_file`, the same one
    `_v5_workdir_files`/`code.author`'s context-grounding already trust) rather
    than opening any new container-network reachability — no docker networking,
    no port publishing, nothing that widens what a session sandbox can be
    reached FROM. It only widens what can be done with content a caller could
    already pull out via that same read path.

    Deliberately narrow: text files only (`read_artifact_file` decodes as
    UTF-8), single relative path per request — an HTML/CSS/JS deliverable with
    inline styling/scripts (the actual motivating case, §3.13) needs nothing
    more. A page with EXTERNAL asset files works too, since the browser will
    request each asset's own relative path against this same route — only a
    page needing a real backend (server-rendered routes, an API) is out of
    scope, which was never true of anything the loop authors as a static
    deliverable."""
    from fastapi.responses import Response, PlainTextResponse
    import importlib, mimetypes
    rel = str(path or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        return PlainTextResponse("invalid path", status_code=400)
    try:
        ex = importlib.import_module("Vera.vera.execution.exec_capabilities")
        read_fn = getattr(ex, "read_artifact_file", None)
    except Exception as e:
        return PlainTextResponse(f"preview unavailable: {e}", status_code=500)
    if not read_fn:
        return PlainTextResponse("preview unavailable: read_artifact_file missing", status_code=500)
    content = await read_fn(session_id=session_id, relpath=rel, max_bytes=2_000_000)
    if content is None:
        return PlainTextResponse(f"not found in sandbox {session_id}: {rel}", status_code=404)
    ctype = mimetypes.guess_type(rel)[0] or "text/plain"
    return Response(content=content, media_type=ctype)


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
            "idle_archive_days": int(cfg.get("idle_archive_days", _IDLE_ARCHIVE_DEFAULT_DAYS) or 0),
            "confine_writes": bool(cfg.get("confine_writes", True)),
            "package_policy": _pkg_policy(cfg),
            "package_allowlist": _pkg_listcfg(cfg, "package_allowlist"),
            "package_blocklist": _pkg_listcfg(cfg, "package_blocklist"),
            "package_preload": _pkg_listcfg(cfg, "package_preload"),
            "package_ask_timeout_secs": int(cfg.get("package_ask_timeout_secs")
                                            or _PKG_ASK_TIMEOUT_DEFAULT)}


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
                "they wake automatically on next use; 0 disables), idle_archive_days "
                "(int — go further than sleep: sessions unused for this many DAYS get "
                "fully archived (commit + sync, same as sandbox.session.stop with "
                "archive_on_stop) and their container removed — the record's real "
                "committed_image is kept, so sandbox.session.start on that session id "
                "restores from it rather than building fresh; 0 disables), confine_writes "
                "(bool — keep agent-generated files in /workspace by redirecting "
                "HOME/temp/cache into the workspace volume and defaulting the exec "
                "cwd there; reads elsewhere still work; default true), package_policy "
                "(str — what happens when code needs a package the sandbox lacks: "
                "'ask' (default) pauses the run and prompts the user to approve the "
                "install, 'auto' installs it silently, 'deny' never installs and "
                "fails the run with the list), package_allowlist (str/list — names "
                "ALWAYS auto-installed even in ask mode, i.e. already-approved "
                "packages), package_blocklist (str/list — names never installed, "
                "whatever the policy), package_preload (str/list — pip packages "
                "installed into EVERY new sandbox at creation), "
                "package_ask_timeout_secs (int — how long a paused run waits for an "
                "answer before giving up; default 300). "
                "Output: {ok, config}.",
)
async def cap_sbx_cfg_set(docker_host_id: Optional[str] = None,
                          base_image: Optional[str] = None,
                          auto_sync_interval: Optional[int] = None,
                          archive_on_stop: Optional[bool] = None,
                          auto_create: Optional[bool] = None,
                          idle_sleep_minutes: Optional[int] = None,
                          idle_archive_days: Optional[int] = None,
                          confine_writes: Optional[bool] = None,
                          package_policy: Optional[str] = None,
                          package_allowlist: Any = None,
                          package_blocklist: Any = None,
                          package_preload: Any = None,
                          package_ask_timeout_secs: Optional[int] = None,
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
    if idle_archive_days is not None:
        try:
            cfg["idle_archive_days"] = max(0, int(idle_archive_days))
        except Exception:
            return {"ok": False, "error": "idle_archive_days must be an integer"}
    if confine_writes is not None:
        cfg["confine_writes"] = bool(confine_writes)
    if package_policy is not None:
        p = str(package_policy).strip().lower()
        if p not in (_PKG_POLICY_ASK, _PKG_POLICY_AUTO, _PKG_POLICY_DENY):
            return {"ok": False, "error": "package_policy must be ask, auto or deny"}
        cfg["package_policy"] = p
    for _field, _val in (("package_allowlist", package_allowlist),
                         ("package_blocklist", package_blocklist),
                         ("package_preload", package_preload)):
        if _val is not None:
            cfg[_field] = [_pkg_norm(n) for n in _pkg_names(_val)]
    if package_ask_timeout_secs is not None:
        try:
            cfg["package_ask_timeout_secs"] = max(15, int(package_ask_timeout_secs))
        except Exception:
            return {"ok": False, "error": "package_ask_timeout_secs must be an integer"}
    await _save_cfg(cfg)
    await emit_event({"type": "remote.sandbox.config",
                      "docker_host_id": cfg.get("docker_host_id", ""),
                      "base_image": cfg.get("base_image", "")})
    return {"ok": True, "config": cfg}


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-INSTALL missing binaries and retry, once
# ─────────────────────────────────────────────────────────────────────────────
# The base image (python:3.12-slim) has python3/pip and NOT MUCH else — no curl,
# wget, git, jq, ... A specialist reaching for one of these is usually right
# that the tool is a completely normal thing to expect on Linux, just not
# present in this minimal image. Failing the call and making the specialist
# reason its way to "install it first" (if it even thinks of that) burns a
# whole cycle on a problem the sandbox can fix itself in ~1s. Deliberately a
# SMALL, known-safe allowlist — this installs a specific missing binary the
# command just asked for, not arbitrary packages an LLM might name.
_APT_PKG_FOR_MISSING_BIN = {
    "curl": "curl", "wget": "wget", "git": "git", "jq": "jq",
    "unzip": "unzip", "zip": "zip", "vim": "vim", "nano": "nano",
    "less": "less", "tree": "tree", "ping": "iputils-ping",
    "dig": "dnsutils", "nslookup": "dnsutils", "host": "bind9-host",
    "ssh": "openssh-client", "scp": "openssh-client", "rsync": "rsync",
    "make": "make", "gcc": "build-essential", "g++": "build-essential",
    "node": "nodejs", "npm": "nodejs", "convert": "imagemagick",
}
# Matches both dash/BusyBox-style ("sh: 1: curl: not found") and bash-style
# ("bash: curl: command not found") shell messages, with or without a leading
# path on the shell name.
_NOT_FOUND_RE = re.compile(
    r"(?im)^\s*(?:/[\w./]*)?(?:ba)?sh:\s*(?:\d+:\s*)?([A-Za-z0-9_.+-]+):\s*"
    r"(?:not found|command not found)\b")


async def _auto_install_missing_bin_and_retry(sid: str, command: str, res: Optional[Dict], *,
                                              workdir: str, timeout: int, shell: str) -> Optional[Dict]:
    """If `res` is a failed exec whose output is a shell '<bin>: not found', and
    `<bin>` is a known-safe common CLI tool, apt-get install it inside the SAME
    container and re-run the ORIGINAL command once. Returns `res` unchanged
    (silently) whenever there's nothing to do or the install/retry doesn't pan
    out — never raises, never loops more than once."""
    if not res or res.get("ok") or not (res.get("stderr") or res.get("stdout")):
        return res
    m = _NOT_FOUND_RE.search(f"{res.get('stderr','')}\n{res.get('stdout','')}")
    if not m:
        return res
    binname = m.group(1)
    pkg = _APT_PKG_FOR_MISSING_BIN.get(binname)
    if not pkg:
        return res
    try:
        install = await _exec_in(
            sid, f"apt-get update -qq && apt-get install -y -qq --no-install-recommends "
                 f"{shlex.quote(pkg)}",
            timeout=120, shell="sh")
    except Exception as e:
        log.debug("sandbox auto-install of %s failed: %s", pkg, e)
        return res
    if not install or not install.get("ok"):
        return res
    try:
        retry = await _exec_in(sid, command, workdir=workdir, timeout=timeout, shell=shell)
    except Exception as e:
        log.debug("sandbox retry after auto-install of %s failed: %s", pkg, e)
        return res
    if retry is None:
        return res
    retry["auto_installed"] = [pkg]
    retry["stdout"] = (f"[sandbox: '{binname}' was missing — auto-installed '{pkg}' and re-ran "
                       f"the command]\n" + str(retry.get("stdout") or ""))
    return retry


# ═════════════════════════════════════════════════════════════════════════════
#  PACKAGES — availability, approval, and management
# ═════════════════════════════════════════════════════════════════════════════
# _auto_install_missing_bin_and_retry (above) is REACTIVE: the command fails, we
# recognise the shell's "not found", install, retry. That works for a fixed list
# of CLI binaries but not for library imports, where the failure costs a whole
# agent cycle: the script dies on ImportError, the specialist re-reasons, and it
# usually rewrites AROUND the missing library rather than asking for it (observed
# live — a step spent four cycles hand-rolling regex HTML parsing after `bs4`
# came back missing).
#
# So this section is PROACTIVE and asks the human:
#   1. Before code runs, statically scan it for the modules/binaries it needs.
#   2. Probe the container for which of those are actually absent.
#   3. Apply policy — auto-install, ASK the user, or deny — and when asking,
#      block the run until they answer (or the ask times out).
#   4. Give the coder a list of what IS available so it stops guessing, plus the
#      knowledge that it can ask for more.
# ─────────────────────────────────────────────────────────────────────────────

KEY_PKG_PENDING = "vera:remote:sandbox:pkg:pending"    # hash: req_id → request
_PKG_DECISION_TTL = 1800


def _pkg_decision_key(req_id: str) -> str:
    return f"vera:remote:sandbox:pkg:decision:{req_id}"


# Local futures for the same-instance fast path; the Redis key above is the
# cross-instance bridge (the run and the user's click can land on different
# instances — see [[vera-cluster-shared-backend]]).
_PKG_PENDING_LOCAL: Dict[str, asyncio.Future] = {}

# Policy values for cfg["package_policy"].
_PKG_POLICY_ASK = "ask"       # default — pause and ask the user (approve/deny)
_PKG_POLICY_AUTO = "auto"     # install whatever a script needs, no prompt
_PKG_POLICY_DENY = "deny"     # never install; the run fails with a clear message
_PKG_ASK_TIMEOUT_DEFAULT = 300

# Import name → distribution name, for the cases where they differ. Only the
# genuinely non-obvious ones: everything else installs under its import name and
# is handled by the identity fallback in _pkg_for_import.
_PY_IMPORT_TO_PKG = {
    "bs4": "beautifulsoup4", "PIL": "pillow", "sklearn": "scikit-learn",
    "yaml": "pyyaml", "cv2": "opencv-python-headless", "docx": "python-docx",
    "pptx": "python-pptx", "fitz": "pymupdf", "dateutil": "python-dateutil",
    "bson": "pymongo", "serial": "pyserial", "OpenSSL": "pyopenssl",
    "Crypto": "pycryptodome", "git": "GitPython", "attr": "attrs",
    "jwt": "pyjwt", "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient",
    "dotenv": "python-dotenv", "magic": "python-magic", "lxml": "lxml",
    "skimage": "scikit-image", "Levenshtein": "python-Levenshtein",
    "pkg_resources": "setuptools", "zoneinfo": "", "usb": "pyusb",
    "slugify": "python-slugify", "markdown": "Markdown", "toml": "toml",
    "ruamel": "ruamel.yaml", "google": "google-api-python-client",
    "PyPDF2": "PyPDF2", "pypdf": "pypdf", "openpyxl": "openpyxl",
    "xlrd": "xlrd", "nacl": "pynacl", "jose": "python-jose",
    "redis": "redis", "requests_html": "requests-html",
}

# The curated menu the UI offers and the coder is told about. `imports` lists the
# module names that prove it's present. Kept deliberately to things an agent
# doing research / data / reporting work actually reaches for — a full PyPI
# browser belongs in the model-catalog-style UI, not here.
_PKG_CATALOG: List[Dict[str, Any]] = [
    # ── HTTP + scraping ──────────────────────────────────────────────────────
    {"name": "requests", "kind": "pip", "imports": ["requests"], "group": "web",
     "summary": "The standard HTTP client."},
    {"name": "beautifulsoup4", "kind": "pip", "imports": ["bs4"], "group": "web",
     "summary": "HTML/XML parsing (BeautifulSoup)."},
    {"name": "lxml", "kind": "pip", "imports": ["lxml"], "group": "web",
     "summary": "Fast XML/HTML parser; BeautifulSoup's best backend."},
    {"name": "httpx", "kind": "pip", "imports": ["httpx"], "group": "web",
     "summary": "HTTP client with HTTP/2 and async support."},
    {"name": "feedparser", "kind": "pip", "imports": ["feedparser"], "group": "web",
     "summary": "RSS/Atom feed parsing."},
    {"name": "html5lib", "kind": "pip", "imports": ["html5lib"], "group": "web",
     "summary": "Spec-compliant HTML parser for malformed markup."},
    # ── data ─────────────────────────────────────────────────────────────────
    {"name": "pandas", "kind": "pip", "imports": ["pandas"], "group": "data",
     "summary": "DataFrames: tabular analysis, CSV/Excel/JSON IO."},
    {"name": "numpy", "kind": "pip", "imports": ["numpy"], "group": "data",
     "summary": "Numeric arrays; dependency of most data libraries."},
    {"name": "duckdb", "kind": "pip", "imports": ["duckdb"], "group": "data",
     "summary": "In-process analytical SQL over files."},
    {"name": "openpyxl", "kind": "pip", "imports": ["openpyxl"], "group": "data",
     "summary": "Read/write .xlsx workbooks."},
    {"name": "pyyaml", "kind": "pip", "imports": ["yaml"], "group": "data",
     "summary": "YAML parsing/serialisation."},
    {"name": "tabulate", "kind": "pip", "imports": ["tabulate"], "group": "data",
     "summary": "Render tables as text/markdown."},
    # ── documents + reporting ────────────────────────────────────────────────
    {"name": "markdown", "kind": "pip", "imports": ["markdown"], "group": "docs",
     "summary": "Markdown → HTML."},
    {"name": "jinja2", "kind": "pip", "imports": ["jinja2"], "group": "docs",
     "summary": "Templating for generated HTML/report output."},
    {"name": "pypdf", "kind": "pip", "imports": ["pypdf"], "group": "docs",
     "summary": "Read/split/merge PDFs."},
    {"name": "python-docx", "kind": "pip", "imports": ["docx"], "group": "docs",
     "summary": "Read/write .docx documents."},
    {"name": "pillow", "kind": "pip", "imports": ["PIL"], "group": "docs",
     "summary": "Image loading, resizing, conversion."},
    {"name": "matplotlib", "kind": "pip", "imports": ["matplotlib"], "group": "docs",
     "summary": "Charts and plots (writes image files)."},
    # ── science / ML ─────────────────────────────────────────────────────────
    {"name": "scipy", "kind": "pip", "imports": ["scipy"], "group": "ml",
     "summary": "Scientific computing on top of numpy."},
    {"name": "scikit-learn", "kind": "pip", "imports": ["sklearn"], "group": "ml",
     "summary": "Classical ML: clustering, regression, metrics."},
    # ── binaries (apt) ───────────────────────────────────────────────────────
    {"name": "curl", "kind": "apt", "bins": ["curl"], "group": "cli",
     "summary": "HTTP from the shell."},
    {"name": "wget", "kind": "apt", "bins": ["wget"], "group": "cli",
     "summary": "File downloader."},
    {"name": "git", "kind": "apt", "bins": ["git"], "group": "cli",
     "summary": "Version control."},
    {"name": "jq", "kind": "apt", "bins": ["jq"], "group": "cli",
     "summary": "Command-line JSON processing."},
    {"name": "ripgrep", "kind": "apt", "bins": ["rg"], "group": "cli",
     "summary": "Fast recursive search."},
    {"name": "poppler-utils", "kind": "apt", "bins": ["pdftotext"], "group": "cli",
     "summary": "pdftotext / pdfimages PDF extraction."},
    {"name": "imagemagick", "kind": "apt", "bins": ["convert"], "group": "cli",
     "summary": "Image conversion from the shell."},
    {"name": "sqlite3", "kind": "apt", "bins": ["sqlite3"], "group": "cli",
     "summary": "SQLite CLI."},
]

_PKG_KINDS = ("pip", "apt", "npm", "pwsh")


def _pkg_norm(name: str) -> str:
    """PEP 503 normalisation, so `Beautiful_Soup4` and `beautifulsoup4` are the
    same entry in an allow/block list."""
    return re.sub(r"[-_.]+", "-", str(name or "").strip().lower())


def _pkg_catalog_entry(name: str) -> Dict[str, Any]:
    n = _pkg_norm(name)
    for e in _PKG_CATALOG:
        if _pkg_norm(e["name"]) == n:
            return e
    return {}


def _py_stdlib() -> set:
    """Module names that never need installing. `sys.stdlib_module_names` is the
    authoritative list (3.10+); the container runs 3.12, and so does Vera."""
    names = set(getattr(sys, "stdlib_module_names", ()) or ())
    if not names:                       # ancient interpreter — degrade safely
        names = set(sys.builtin_module_names)
    return names


def _pkg_for_import(module: str) -> str:
    """Distribution that provides `module`. Falls back to the module name, which
    is right for the large majority (requests, pandas, httpx …)."""
    m = str(module or "").split(".")[0]
    if m in _PY_IMPORT_TO_PKG:
        return _PY_IMPORT_TO_PKG[m]
    for e in _PKG_CATALOG:
        if m in (e.get("imports") or []):
            return e["name"]
    return m


_PY_IMPORT_RE = re.compile(
    r"(?m)^\s*(?:import\s+([A-Za-z_][\w.]*)|from\s+([A-Za-z_][\w.]*)\s+import)")


def scan_python_imports(code: str) -> List[str]:
    """Third-party top-level module names a snippet imports.

    AST first (exact, ignores imports inside strings/comments); regex only if the
    code doesn't parse — a snippet with a syntax error still tells us what it
    MEANT to import, and reporting a missing package beats reporting a
    SyntaxError the author can already see."""
    mods: List[str] = []
    try:
        import ast as _ast
        tree = _ast.parse(code or "")
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                mods += [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, _ast.ImportFrom):
                # `from . import x` is local — node.module is None or level > 0
                if node.module and not node.level:
                    mods.append(node.module.split(".")[0])
    except Exception:
        for a, b in _PY_IMPORT_RE.findall(code or ""):
            mods.append((a or b).split(".")[0])
    std = _py_stdlib()
    out: List[str] = []
    for m in mods:
        if not m or m in std or m.startswith("_") or m in out:
            continue
        out.append(m)
    return out


# Shell tokens worth checking for existence before a bash command runs. Only
# names in the catalog or the reactive apt map are considered, so a scan of
# arbitrary agent shell never turns into "install anything it typed".
_SH_TOKEN_RE = re.compile(r"(?m)(?:^|[|;&]\s*|\$\(\s*|`\s*)\s*([A-Za-z][\w.+-]*)")


def scan_shell_bins(command: str) -> List[str]:
    """Binaries a shell command invokes that we know how to install."""
    known = dict(_APT_PKG_FOR_MISSING_BIN)
    for e in _PKG_CATALOG:
        for b in (e.get("bins") or []):
            known.setdefault(b, e["name"])
    out: List[str] = []
    for tok in _SH_TOKEN_RE.findall(command or ""):
        if tok in known and tok not in out:
            out.append(tok)
    return out


_PWSH_IMPORT_RE = re.compile(r"(?im)^\s*(?:Import-Module|using\s+module)\s+"
                             r"['\"]?([A-Za-z][\w.]*)")


def scan_pwsh_modules(code: str) -> List[str]:
    """PowerShell modules a script explicitly imports. Deliberately narrow:
    inferring modules from bare cmdlet names guesses wrong constantly, and a
    wrong guess here would prompt the user to install something irrelevant."""
    builtin = {"Microsoft.PowerShell.Management", "Microsoft.PowerShell.Utility",
               "Microsoft.PowerShell.Security", "Microsoft.PowerShell.Core",
               "PSReadLine", "ThreadJob"}
    out: List[str] = []
    for m in _PWSH_IMPORT_RE.findall(code or ""):
        if m not in builtin and m not in out:
            out.append(m)
    return out


# ── container probes ─────────────────────────────────────────────────────────
# Probing the REAL interpreter beats mapping installed distribution names back to
# import names: `pip list` says "beautifulsoup4", the script says "import bs4",
# and any table mapping between them is one stale entry away from a false
# "missing" that prompts the user to install something already there.

async def _probe_python_modules(sid: str, modules: List[str]) -> Dict[str, bool]:
    """{module: importable} inside the sandbox, via importlib.find_spec."""
    if not modules:
        return {}
    probe = ("import importlib.util,json,sys;"
             "print(json.dumps({m:(importlib.util.find_spec(m) is not None) "
             "for m in sys.argv[1:]}))")
    cmd = "python3 -c " + shlex.quote(probe) + " " + " ".join(
        shlex.quote(m) for m in modules[:60])
    try:
        res = await _exec_in(sid, cmd, timeout=60, shell="sh")
    except Exception as e:
        log.debug("python module probe failed: %s", e)
        return {}
    if not res or not res.get("ok"):
        return {}
    try:
        return {k: bool(v) for k, v in
                json.loads(str(res.get("stdout") or "").strip().splitlines()[-1]).items()}
    except Exception:
        return {}


async def _probe_bins(sid: str, bins: List[str]) -> Dict[str, bool]:
    """{binary: on PATH} inside the sandbox."""
    if not bins:
        return {}
    checks = "; ".join(
        f"command -v {shlex.quote(b)} >/dev/null 2>&1 && echo {shlex.quote(b)}=1 "
        f"|| echo {shlex.quote(b)}=0" for b in bins[:40])
    try:
        res = await _exec_in(sid, checks, timeout=60, shell="sh")
    except Exception as e:
        log.debug("binary probe failed: %s", e)
        return {}
    if not res:
        return {}
    out: Dict[str, bool] = {}
    for line in str(res.get("stdout") or "").splitlines():
        name, _, val = line.strip().partition("=")
        if name:
            out[name] = val == "1"
    return out


async def _probe_pwsh_modules(sid: str, modules: List[str]) -> Dict[str, bool]:
    if not modules:
        return {}
    names = ",".join("'" + m.replace("'", "''") + "'" for m in modules[:40])
    ps = (f"@({names}) | ForEach-Object {{ "
          "$ok = [bool](Get-Module -ListAvailable -Name $_); "
          "Write-Output \"$_=$([int]$ok)\" }")
    try:
        res = await _exec_in(sid, ps, timeout=90, shell="pwsh")
    except Exception as e:
        log.debug("pwsh module probe failed: %s", e)
        return {}
    if not res:
        return {}
    out: Dict[str, bool] = {}
    for line in str(res.get("stdout") or "").splitlines():
        name, _, val = line.strip().partition("=")
        if name:
            out[name] = val.strip() == "1"
    return out


async def sandbox_installed_packages(session_id: str) -> Dict[str, Any]:
    """Everything installed in a session's sandbox: pip distributions, plus which
    catalog binaries are on PATH. Used by the management UI and the coder hint."""
    sid = await _resolve_sid(session_id)
    rec = await _get_rec(sid)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session", "pip": [], "bins": []}
    pip: List[Dict[str, str]] = []
    try:
        res = await _exec_in(sid, "pip list --format=json --disable-pip-version-check",
                             timeout=90, shell="sh")
        if res and res.get("ok"):
            raw = str(res.get("stdout") or "").strip()
            start = raw.find("[")
            if start >= 0:
                pip = [{"name": str(d.get("name") or ""), "version": str(d.get("version") or "")}
                       for d in json.loads(raw[start:]) if isinstance(d, dict)]
    except Exception as e:
        log.debug("pip list failed for %s: %s", sid, e)
    cat_bins = sorted({b for e in _PKG_CATALOG for b in (e.get("bins") or [])})
    present = await _probe_bins(sid, cat_bins)
    return {"ok": True, "session_id": sid,
            "pip": sorted(pip, key=lambda d: d["name"].lower()),
            "bins": [b for b, ok in present.items() if ok]}


# ── policy ───────────────────────────────────────────────────────────────────

def _pkg_policy(cfg: Dict) -> str:
    p = str(cfg.get("package_policy") or _PKG_POLICY_ASK).strip().lower()
    return p if p in (_PKG_POLICY_ASK, _PKG_POLICY_AUTO, _PKG_POLICY_DENY) else _PKG_POLICY_ASK


def _pkg_listcfg(cfg: Dict, key: str) -> List[str]:
    v = cfg.get(key) or []
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    return [_pkg_norm(x) for x in v if str(x).strip()]


def _classify_requests(cfg: Dict, reqs: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
    """Split what a run needs into auto-installable / must-ask / blocked.

    The allowlist is what makes 'ask' mode liveable: a user who has already said
    yes to beautifulsoup4 once should never be asked again, without having to
    turn prompting off globally."""
    policy = _pkg_policy(cfg)
    allow = set(_pkg_listcfg(cfg, "package_allowlist"))
    block = set(_pkg_listcfg(cfg, "package_blocklist"))
    auto, ask, denied = [], [], []
    for req in reqs:
        n = _pkg_norm(req.get("package"))
        if n in block:
            denied.append({**req, "reason": "on the package blocklist"})
        elif policy == _PKG_POLICY_DENY:
            denied.append({**req, "reason": "package installs are disabled "
                                            "(sandbox package policy = deny)"})
        elif policy == _PKG_POLICY_AUTO or n in allow:
            auto.append(req)
        else:
            ask.append(req)
    return {"auto": auto, "ask": ask, "denied": denied}


# ── install ──────────────────────────────────────────────────────────────────

async def _install_packages(sid: str, reqs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Install a batch inside the sandbox, grouped by ecosystem. Returns per-item
    outcomes; never raises."""
    by_kind: Dict[str, List[str]] = {}
    for req in reqs:
        by_kind.setdefault(str(req.get("kind") or "pip"), []).append(str(req.get("package")))
    installed, failed, log_lines = [], [], []
    for kind, names in by_kind.items():
        names = [n for n in names if n]
        if not names:
            continue
        quoted = " ".join(shlex.quote(n) for n in names)
        if kind == "pip":
            cmd = f"pip install --disable-pip-version-check --no-input {quoted}"
            shell = "sh"
            timeout = 600
        elif kind == "apt":
            cmd = (f"apt-get update -qq && apt-get install -y -qq "
                   f"--no-install-recommends {quoted}")
            shell = "sh"
            timeout = 600
        elif kind == "npm":
            cmd = f"npm install -g {quoted}"
            shell = "sh"
            timeout = 600
        elif kind == "pwsh":
            inner = ",".join("'" + n.replace("'", "''") + "'" for n in names)
            cmd = (f"Install-Module -Name @({inner}) -Scope CurrentUser -Force "
                   f"-AcceptLicense -ErrorAction Stop")
            shell = "pwsh"
            timeout = 900
        else:
            failed += [{"package": n, "kind": kind, "error": f"unknown package kind '{kind}'"}
                       for n in names]
            continue
        try:
            res = await _exec_in(sid, cmd, timeout=timeout, shell=shell)
        except Exception as e:
            res = {"ok": False, "stderr": str(e)}
        ok = bool(res and res.get("ok"))
        tail = str((res or {}).get("stderr") or (res or {}).get("stdout") or "")[-600:]
        log_lines.append(f"[{kind}] {' '.join(names)} → {'ok' if ok else 'FAILED'}")
        if ok:
            installed += [{"package": n, "kind": kind} for n in names]
        else:
            failed += [{"package": n, "kind": kind, "error": tail} for n in names]
    return {"ok": not failed, "installed": installed, "failed": failed,
            "log": "\n".join(log_lines)}


# ── the ask ──────────────────────────────────────────────────────────────────

async def _await_package_decision(req_id: str, timeout: float) -> Dict[str, Any]:
    """Block until the user approves/denies, or the ask times out. Same dual
    local-future + Redis-key resolution as the loop's HITL gate, for the same
    reason: the run and the click can be on different instances."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _PKG_PENDING_LOCAL[req_id] = fut
    r = _redis()
    key = _pkg_decision_key(req_id)
    deadline = time.monotonic() + max(5.0, timeout)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"decision": "timeout"}
            if fut.done():
                return fut.result()
            if r is not None:
                try:
                    raw = await r.get(key)
                except Exception:
                    raw = None
                if raw:
                    try:
                        await r.delete(key)
                    except Exception:
                        pass
                    try:
                        return json.loads(raw if isinstance(raw, str) else raw.decode())
                    except Exception:
                        return {"decision": "deny"}
            try:
                await asyncio.wait_for(asyncio.shield(fut),
                                       timeout=min(1.0, max(0.05, remaining)))
                return fut.result()
            except asyncio.TimeoutError:
                continue
    finally:
        _PKG_PENDING_LOCAL.pop(req_id, None)
        if r is not None:
            try:
                await r.hdel(KEY_PKG_PENDING, req_id)
            except Exception:
                pass


def _pkg_lines(reqs: List[Dict[str, Any]]) -> str:
    out = []
    for q in reqs:
        entry = _pkg_catalog_entry(q.get("package"))
        why = q.get("needed_for") or ""
        summary = entry.get("summary") or ""
        bits = [f"  • {q.get('package')} ({q.get('kind')})"]
        if why:
            bits.append(f"— needed for `{why}`")
        if summary:
            bits.append(f"— {summary}")
        out.append(" ".join(bits))
    return "\n".join(out)


async def _request_package_approval(sid: str, reqs: List[Dict[str, Any]], *,
                                    context: str = "", event_sid: str = "") -> Dict[str, Any]:
    """Register a pending request, tell the UI, and wait for the answer.

    `event_sid` is the session the CALLER thinks it is (a chat/loop session id);
    `sid` is the container's, which differs whenever an alias redirects a
    governed run into an owner container. The event must carry the caller's, or
    the loop SSE — which drops events whose session_id doesn't match the run —
    filters out the very prompt the run is blocked on."""
    cfg = await _get_cfg()
    timeout = float(cfg.get("package_ask_timeout_secs") or _PKG_ASK_TIMEOUT_DEFAULT)
    req_id = uuid.uuid4().hex[:12]
    record = {"id": req_id, "session_id": event_sid or sid, "sandbox_id": sid,
              "packages": reqs,
              "context": str(context or "")[:400], "created": now_iso(),
              "expires_in": timeout}
    r = _redis()
    if r is not None:
        try:
            await r.hset(KEY_PKG_PENDING, req_id, json.dumps(record))
        except Exception:
            pass
    await emit_event({"type": "remote.sandbox.package_request", **record,
                      "summary": f"{len(reqs)} package(s) need approval",
                      "lines": _pkg_lines(reqs)})
    decision = await _await_package_decision(req_id, timeout)
    await emit_event({"type": "remote.sandbox.package_decision", "id": req_id,
                      "session_id": event_sid or sid,
                      "decision": decision.get("decision", "timeout")})
    return {"request": record, **decision}


# ── the gate ─────────────────────────────────────────────────────────────────

def _gate_message(head: str, reqs: List[Dict[str, Any]], tail: str) -> str:
    return f"{head}\n{_pkg_lines(reqs)}\n{tail}"


async def _package_gate(sid: str, *, reqs: List[Dict[str, Any]],
                        context: str = "", event_sid: str = "") -> Optional[Dict[str, Any]]:
    """Ensure everything in `reqs` is present before a run.

    Returns None when the run may proceed (nothing missing, or everything was
    installed), or an exec-shaped FAILURE dict explaining what is needed and how
    to get it. Never raises — a sandbox that can't be probed must not become a
    sandbox that can't run code."""
    if not reqs:
        return None
    cfg = await _get_cfg()
    split = _classify_requests(cfg, reqs)
    if split["denied"]:
        names = ", ".join(q["package"] for q in split["denied"])
        # Both keys: exec results are consumed as `rc` in most places and
        # `exit_code` in others, and a gate refusal that carries only one reads
        # as rc=None — indistinguishable from "never ran" to a caller that
        # branches on it. 126 is the shell's "found but not executable".
        return {"ok": False, "rc": 126, "exit_code": 126, "stdout": "", "packages_denied": split["denied"],
                "stderr": _gate_message(
                    f"BLOCKED: this code needs package(s) that policy will not install: {names}",
                    split["denied"],
                    "Rewrite it using what is already available, or ask the user to change the "
                    "sandbox package policy / blocklist.")}
    to_install = list(split["auto"])
    rejected: List[Dict[str, Any]] = []
    head = ""
    if split["ask"]:
        outcome = await _request_package_approval(sid, split["ask"], context=context,
                                                  event_sid=event_sid)
        decision = str(outcome.get("decision") or "timeout")
        approved = {_pkg_norm(p) for p in (outcome.get("approved") or [])}
        if decision == "approve":
            # An empty `approved` list means "all of them" (the plain Approve
            # button); a populated one is a per-package SUBSET.
            picked = [q for q in split["ask"]
                      if not approved or _pkg_norm(q["package"]) in approved]
            to_install += picked
            rejected = [q for q in split["ask"] if q not in picked]
            head = "The user approved only SOME of them. These were NOT installed:"
        else:
            rejected = list(split["ask"])
            _t = int(float(cfg.get("package_ask_timeout_secs") or _PKG_ASK_TIMEOUT_DEFAULT))
            head = ("The user DENIED installing these packages:" if decision == "deny"
                    else f"No answer within {_t}s, so these packages were NOT installed:")

    # Install what we may BEFORE reporting anything refused: a partial approval
    # should still leave the container better off, and the next attempt then only
    # has to solve the genuinely-refused import. (The install persists in the
    # container even though this run is stopped.)
    installed_failures: List[Dict[str, Any]] = []
    if to_install:
        res = await _install_packages(sid, to_install)
        await _invalidate_pkg_cache(sid)
        await emit_event({"type": "remote.sandbox.packages_installed",
                          "session_id": event_sid or sid, "sandbox_id": sid,
                          "installed": [q["package"] for q in res.get("installed") or []],
                          "failed": [q["package"] for q in res.get("failed") or []]})
        installed_failures = list(res.get("failed") or [])

    # Anything the code imports but still doesn't have STOPS the run — including
    # the partial-approval case, which previously fell through and let the script
    # run straight into the ImportError the gate exists to prevent.
    if rejected:
        # Both keys: exec results are consumed as `rc` in most places and
        # `exit_code` in others, and a gate refusal that carries only one reads
        # as rc=None — indistinguishable from "never ran" to a caller that
        # branches on it. 126 is the shell's "found but not executable".
        return {"ok": False, "rc": 126, "exit_code": 126, "stdout": "",
                "packages_rejected": rejected,
                "stderr": _gate_message(
                    head, rejected,
                    "Do NOT retry the same import. Either solve it with the packages "
                    "already installed, or explain to the user why this one is needed.")}
    if installed_failures:
        # Both keys: exec results are consumed as `rc` in most places and
        # `exit_code` in others, and a gate refusal that carries only one reads
        # as rc=None — indistinguishable from "never ran" to a caller that
        # branches on it. 126 is the shell's "found but not executable".
        return {"ok": False, "rc": 126, "exit_code": 126, "stdout": "",
                "packages_failed": installed_failures,
                "stderr": _gate_message(
                    "These packages could not be installed:",
                    installed_failures,
                    "The install itself failed — the name may be wrong, or the package may "
                    "need a build toolchain. Try a different library.")}
    return None


async def _invalidate_pkg_cache(sid: str) -> None:
    r = _redis()
    if r is not None:
        try:
            await r.delete(f"vera:remote:sandbox:pkgcache:{sid}")
        except Exception:
            pass


async def preflight_python(sid: str, code: str, *,
                           event_sid: str = "") -> Optional[Dict[str, Any]]:
    """Missing-import gate for a python snippet about to run in `sid`."""
    mods = scan_python_imports(code)
    if not mods:
        return None
    present = await _probe_python_modules(sid, mods)
    if not present:
        return None                       # probe failed → never block the run
    reqs = []
    for m in mods:
        if present.get(m) is False:
            pkg = _pkg_for_import(m)
            if pkg:
                reqs.append({"package": pkg, "kind": "pip", "needed_for": f"import {m}"})
    return await _package_gate(sid, reqs=reqs, context="python code", event_sid=event_sid)


async def preflight_shell(sid: str, command: str, *,
                          event_sid: str = "") -> Optional[Dict[str, Any]]:
    """Missing-binary gate for a shell command about to run in `sid`."""
    bins = scan_shell_bins(command)
    if not bins:
        return None
    present = await _probe_bins(sid, bins)
    if not present:
        return None
    known = dict(_APT_PKG_FOR_MISSING_BIN)
    for e in _PKG_CATALOG:
        for b in (e.get("bins") or []):
            known.setdefault(b, e["name"])
    reqs = [{"package": known[b], "kind": "apt", "needed_for": b}
            for b in bins if present.get(b) is False and b in known]
    # de-dup: several binaries often come from one package (dnsutils → dig+nslookup)
    seen, uniq = set(), []
    for q in reqs:
        if _pkg_norm(q["package"]) not in seen:
            seen.add(_pkg_norm(q["package"]))
            uniq.append(q)
    return await _package_gate(sid, reqs=uniq, context="shell command", event_sid=event_sid)


_PREFLIGHT_LANGS = {
    "python": "python", "py": "python", "python3": "python",
    "bash": "shell", "sh": "shell", "shell": "shell",
    "powershell": "pwsh", "pwsh": "pwsh", "ps": "pwsh", "ps1": "pwsh",
}


async def _preflight_for(sid: str, language: str, *, code: str = "",
                         path: str = "", event_sid: str = "") -> Optional[Dict[str, Any]]:
    """Dispatch the right preflight for `language`, reading the source out of the
    container first when the run is by path. Unknown languages are not gated —
    there is nothing useful to scan, and guessing would block valid runs."""
    kind = _PREFLIGHT_LANGS.get(str(language or "").lower())
    if not kind:
        return None
    src = code or ""
    if not src and path:
        try:
            res = await _exec_in(sid, f"cat {shlex.quote(path)}", timeout=30, shell="sh")
            if res and res.get("ok"):
                src = str(res.get("stdout") or "")
        except Exception:
            return None
    if not src.strip():
        return None
    try:
        if kind == "python":
            return await preflight_python(sid, src, event_sid=event_sid)
        if kind == "shell":
            return await preflight_shell(sid, src, event_sid=event_sid)
        return await preflight_pwsh(sid, src, event_sid=event_sid)
    except Exception as e:
        log.debug("preflight (%s) failed for %s: %s", kind, sid, e)
        return None


async def preflight_pwsh(sid: str, code: str, *,
                         event_sid: str = "") -> Optional[Dict[str, Any]]:
    """Missing-module gate for a PowerShell script about to run in `sid`."""
    mods = scan_pwsh_modules(code)
    if not mods:
        return None
    present = await _probe_pwsh_modules(sid, mods)
    if not present:
        return None
    reqs = [{"package": m, "kind": "pwsh", "needed_for": f"Import-Module {m}"}
            for m in mods if present.get(m) is False]
    return await _package_gate(sid, reqs=reqs, context="powershell script",
                               event_sid=event_sid)


# ── what the coder is told ───────────────────────────────────────────────────

async def sandbox_package_hint(session_id: str, language: str = "python") -> str:
    """A short block naming what's installed and how to get more, for injection
    into code-authoring prompts.

    Without this the model guesses, and guesses badly in both directions: it
    assumes pandas is present (it isn't, on a slim base) or hand-rolls a parser
    because it assumes bs4 is absent (it may well be there). Telling it the truth
    once is far cheaper than either failure."""
    try:
        sid = await _resolve_sid(session_id)
        rec = await _get_rec(sid)
        if not rec or not rec.get("container"):
            return ""
        inv = await sandbox_installed_packages(sid)
        if not inv.get("ok"):
            return ""
        cfg = await _get_cfg()
        policy = _pkg_policy(cfg)
        lang = (language or "python").lower()
        lines: List[str] = []
        if lang in ("python", "py"):
            names = [d["name"] for d in inv.get("pip") or []]
            lines.append("PYTHON PACKAGES INSTALLED in this sandbox: "
                         + (", ".join(sorted(names)[:80]) or "(only the standard library)"))
        elif lang in ("bash", "sh", "shell"):
            lines.append("EXTRA CLI TOOLS available in this sandbox: "
                         + (", ".join(inv.get("bins") or []) or "(none beyond the base image)"))
        else:
            names = [d["name"] for d in inv.get("pip") or []]
            lines.append("INSTALLED: python — " + (", ".join(sorted(names)[:60]) or "stdlib only")
                         + "; CLI — " + (", ".join(inv.get("bins") or []) or "base image only"))
        offer = ", ".join(e["name"] for e in _PKG_CATALOG
                          if _pkg_norm(e["name"]) not in
                          {_pkg_norm(d["name"]) for d in inv.get("pip") or []})
        if policy == _PKG_POLICY_AUTO:
            lines.append("Anything else you import is installed AUTOMATICALLY before the run — "
                         "just import what you need. Commonly available: " + offer[:400])
        elif policy == _PKG_POLICY_DENY:
            lines.append("Installing new packages is DISABLED. Use only what is listed above "
                         "and the standard library.")
        else:
            lines.append("If you need something else, IMPORT IT ANYWAY: the run pauses and asks "
                         "the user to approve the install, then continues. Do not hand-roll a "
                         "replacement for a standard library. Approvable examples: " + offer[:400])
        return "\n".join(lines)
    except Exception as e:
        log.debug("package hint failed for %s: %s", session_id, e)
        return ""


# ── management capabilities ──────────────────────────────────────────────────

@capability(
    "sandbox.packages.catalog",
    http_method="GET", http_path="/remote/sandbox/packages/catalog",
    http_tags=["remote", "sandbox"], memory="off", silent=True,
    description="The curated menu of packages that can be installed into session "
                "sandboxes, each marked with whether it is already present in a "
                "given session. Input: session_id (str — optional; omit for the "
                "bare catalog). Output: {ok, policy, catalog:[{name, kind, group, "
                "summary, installed}], installed:{pip:[], bins:[]}}.",
)
async def cap_sbx_pkg_catalog(session_id: str = "", trace_id=None) -> Dict:
    cfg = await _get_cfg()
    inv = {"pip": [], "bins": []}
    if session_id:
        got = await sandbox_installed_packages(session_id)
        if got.get("ok"):
            inv = {"pip": got.get("pip") or [], "bins": got.get("bins") or []}
    have = {_pkg_norm(d["name"]) for d in inv["pip"]} | {b for b in inv["bins"]}
    catalog = []
    for e in _PKG_CATALOG:
        installed = _pkg_norm(e["name"]) in have or any(b in have for b in (e.get("bins") or []))
        catalog.append({"name": e["name"], "kind": e["kind"], "group": e.get("group", ""),
                        "summary": e.get("summary", ""), "installed": installed})
    return {"ok": True, "policy": _pkg_policy(cfg), "catalog": catalog, "installed": inv,
            "allowlist": _pkg_listcfg(cfg, "package_allowlist"),
            "blocklist": _pkg_listcfg(cfg, "package_blocklist")}


@capability(
    "sandbox.packages.list",
    http_method="POST", http_path="/remote/sandbox/packages/list",
    http_tags=["remote", "sandbox"], memory="off", silent=True,
    description="Everything actually installed in one session's sandbox. Input: "
                "session_id (str!). Output: {ok, pip:[{name,version}], bins:[]}.",
)
async def cap_sbx_pkg_list(session_id: str = "", trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    return await sandbox_installed_packages(session_id)


@capability(
    "sandbox.packages.install",
    http_method="POST", http_path="/remote/sandbox/packages/install",
    http_tags=["remote", "sandbox"],
    description="Install packages into a session's sandbox. Inputs: session_id "
                "(str!), packages (str/list! — names, e.g. 'pandas,lxml'), kind "
                "(str — pip|apt|npm|pwsh, default pip), remember (bool — also add "
                "them to the auto-approve allowlist so future runs never ask). "
                "Blocklisted names are refused. Output: {ok, installed, failed}.",
)
async def cap_sbx_pkg_install(session_id: str = "", packages: Any = "", kind: str = "pip",
                              remember: bool = False, trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    names = _pkg_names(packages)
    if not names:
        return {"ok": False, "error": "packages required"}
    k = (kind or "pip").strip().lower()
    if k not in _PKG_KINDS:
        return {"ok": False, "error": f"kind must be one of {', '.join(_PKG_KINDS)}"}
    cfg = await _get_cfg()
    block = set(_pkg_listcfg(cfg, "package_blocklist"))
    refused = [n for n in names if _pkg_norm(n) in block]
    if refused:
        return {"ok": False, "error": f"blocklisted: {', '.join(refused)}"}
    sid = await _resolve_sid(session_id)
    rec = await _get_rec(sid)
    if not rec or not rec.get("container"):
        return {"ok": False, "error": "no sandbox for this session"}
    res = await _install_packages(sid, [{"package": n, "kind": k} for n in names])
    await _invalidate_pkg_cache(sid)
    if remember and res.get("installed"):
        allow = _pkg_listcfg(cfg, "package_allowlist")
        for q in res["installed"]:
            if _pkg_norm(q["package"]) not in allow:
                allow.append(_pkg_norm(q["package"]))
        cfg["package_allowlist"] = allow
        await _save_cfg(cfg)
    await emit_event({"type": "remote.sandbox.packages_installed", "session_id": sid,
                      "installed": [q["package"] for q in res.get("installed") or []],
                      "failed": [q["package"] for q in res.get("failed") or []]})
    return {"session_id": sid, **res}


@capability(
    "sandbox.packages.remove",
    http_method="POST", http_path="/remote/sandbox/packages/remove",
    http_tags=["remote", "sandbox"],
    description="Uninstall packages from a session's sandbox. Inputs: session_id "
                "(str!), packages (str/list!), kind (str — pip|apt, default pip). "
                "Output: {ok, removed, log}.",
)
async def cap_sbx_pkg_remove(session_id: str = "", packages: Any = "", kind: str = "pip",
                             trace_id=None) -> Dict:
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    names = _pkg_names(packages)
    if not names:
        return {"ok": False, "error": "packages required"}
    sid = await _resolve_sid(session_id)
    quoted = " ".join(shlex.quote(n) for n in names)
    k = (kind or "pip").strip().lower()
    if k == "pip":
        cmd = f"pip uninstall -y --disable-pip-version-check {quoted}"
    elif k == "apt":
        cmd = f"apt-get remove -y -qq {quoted}"
    else:
        return {"ok": False, "error": "kind must be pip or apt"}
    res = await _exec_in(sid, cmd, timeout=300, shell="sh")
    await _invalidate_pkg_cache(sid)
    if res is None:
        return {"ok": False, "error": "no sandbox for this session"}
    return {"ok": bool(res.get("ok")), "removed": names if res.get("ok") else [],
            "log": str(res.get("stdout") or res.get("stderr") or "")[-1500:]}


@capability(
    "sandbox.packages.pending",
    http_method="GET", http_path="/remote/sandbox/packages/pending",
    http_tags=["remote", "sandbox"], memory="off", silent=True,
    description="Package installs currently waiting on the user's approval (a run "
                "is paused on each one). Output: {ok, pending:[{id, session_id, "
                "packages, context, created}]}.",
)
async def cap_sbx_pkg_pending(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"ok": True, "pending": []}
    try:
        raw = await r.hgetall(KEY_PKG_PENDING)
    except Exception as e:
        return {"ok": False, "error": str(e), "pending": []}
    out = []
    for v in (raw or {}).values():
        try:
            out.append(json.loads(v if isinstance(v, str) else v.decode()))
        except Exception:
            continue
    out.sort(key=lambda d: str(d.get("created") or ""), reverse=True)
    return {"ok": True, "pending": out, "count": len(out)}


@capability(
    "sandbox.packages.respond",
    http_method="POST", http_path="/remote/sandbox/packages/respond",
    http_tags=["remote", "sandbox"],
    description="Answer a pending package-install request, releasing the paused "
                "run. Inputs: id (str! — from sandbox.packages.pending), decision "
                "(str! — approve|deny), packages (str/list — approve only a SUBSET; "
                "omit to approve all of them), remember (bool — add the approved "
                "names to the auto-approve allowlist so this never asks again). "
                "Output: {ok, id, decision}.",
)
async def cap_sbx_pkg_respond(id: str = "", decision: str = "approve", packages: Any = "",
                              remember: bool = False, trace_id=None) -> Dict:
    req_id = str(id or "").strip()
    if not req_id:
        return {"ok": False, "error": "id required"}
    dec = (decision or "").strip().lower()
    if dec not in ("approve", "deny"):
        return {"ok": False, "error": "decision must be 'approve' or 'deny'"}
    approved = _pkg_names(packages)
    payload = {"decision": dec, "approved": approved}
    if dec == "approve" and remember:
        cfg = await _get_cfg()
        allow = _pkg_listcfg(cfg, "package_allowlist")
        # With no explicit subset the user approved the whole request, so the
        # remembered set is the request's own package list.
        names = approved
        if not names:
            r0 = _redis()
            try:
                raw = await r0.hget(KEY_PKG_PENDING, req_id) if r0 else None
                if raw:
                    rec = json.loads(raw if isinstance(raw, str) else raw.decode())
                    names = [str(q.get("package")) for q in (rec.get("packages") or [])]
            except Exception:
                names = []
        for n in names:
            if _pkg_norm(n) and _pkg_norm(n) not in allow:
                allow.append(_pkg_norm(n))
        cfg["package_allowlist"] = allow
        await _save_cfg(cfg)
    # Local future first (same-instance, instant), Redis key as the cross-instance
    # bridge — the paused run may be on another node entirely.
    fut = _PKG_PENDING_LOCAL.get(req_id)
    if fut is not None and not fut.done():
        try:
            fut.set_result(payload)
        except Exception:
            pass
    r = _redis()
    if r is not None:
        try:
            await r.set(_pkg_decision_key(req_id), json.dumps(payload), ex=_PKG_DECISION_TTL)
        except Exception:
            pass
    return {"ok": True, "id": req_id, "decision": dec, "approved": approved}


def _pkg_names(packages: Any) -> List[str]:
    """Accept a list, or a comma/space-separated string, from either a UI form or
    an agent that ignored the schema."""
    if isinstance(packages, str):
        items = re.split(r"[,\s]+", packages)
    elif isinstance(packages, (list, tuple)):
        items = [str(p) for p in packages]
    else:
        items = []
    out: List[str] = []
    for p in items:
        p = str(p).strip()
        if p and p not in out:
            out.append(p)
    return out


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
    # Missing tools the command NAMES are settled before it runs, so a policy of
    # "ask" gets one prompt up front rather than the command half-executing and
    # dying partway. _auto_install_missing_bin_and_retry below still covers what
    # a static scan can't see (a binary invoked from inside a script it calls).
    if shell != "pwsh":
        gate = await preflight_shell(rec["session_id"], command, event_sid=session_id)
        if gate is not None:
            gate.setdefault("elapsed_ms", 0)
            return gate
    res = await _exec_in(rec["session_id"], command, timeout=int(timeout or 60), shell=shell)
    if res is not None and shell != "pwsh":
        res = await _auto_install_missing_bin_and_retry(
            rec["session_id"], command, res, workdir="", timeout=int(timeout or 60), shell=shell)
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
    # Preflight the SOURCE, whether it arrived inline or already lives in the
    # container — a by-path run is exactly the case that used to fail on an
    # ImportError the author never sees, because the file was written in an
    # earlier step.
    gate = await _preflight_for(rec["session_id"], language, code=code, path=p,
                                event_sid=session_id)
    if gate is not None:
        gate.setdefault("elapsed_ms", 0)
        gate.setdefault("language", language)
        return gate
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
async def _route_gate(session_id: str, *, create: bool = False) -> Optional[Dict]:
    """Return the routable record for a session's filesystem/exec routing, or None
    → the caller runs on the host. A LOCAL-backend record is always routable (its
    "container" is this process). A DOCKER record is routable only when docker is
    actually reachable — mirroring the old `_sbx_host` gate exactly, so a
    docker-backed session still FALLS BACK TO THE HOST (returns None) when the
    docker module/host is unavailable, rather than surfacing an error."""
    rec = await _ensure_routable(session_id, create=create)
    if not rec:
        return None
    if _is_local_rec(rec):
        return rec
    dk = _dk()
    if dk is None:
        return None
    if not await _docker_host(dk, rec.get("docker_host_id", "local")):
        return None
    return rec


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
    # Gate on a ROUTABLE record (docker OR local backend), not on docker
    # specifically — a local-backend session reads via _exec_in just the same.
    # _route_gate preserves the docker path's host-fallback (None) exactly.
    if not await _route_gate(session_id, create=False):
        return None
    if not path:
        return {"error": "path required"}
    resolved = path
    q = shlex.quote(path)
    meta = await _exec_in(
        session_id,
        f"if [ ! -e {q} ]; then echo __VERA_ENOENT__; exit 3; fi; "
        f"stat -c %s {q} 2>/dev/null || echo 0", timeout=30)
    if meta is None:
        return {"error": "sandbox exec failed"}
    if "__VERA_ENOENT__" in (meta.get("stdout", "") + meta.get("stderr", "")):
        # Self-correcting read: the agent frequently guesses a slightly-wrong name
        # (a leading underscore, the wrong sub-dir) and then loops on the phantom
        # path. Locate the file by BASENAME anywhere under /workspace; a unique
        # match is read transparently. Otherwise return the error WITH the actual
        # workspace file list so the next attempt uses a REAL name.
        base = os.path.basename(str(path).replace("\\", "/")) or str(path)
        probe = await _exec_in(
            session_id,
            f"find {shlex.quote(_WORKDIR)} -maxdepth 6 -type f -name {shlex.quote(base)} "
            f"2>/dev/null | head -3 | sed 's/^/__M__/'; echo __VERA_LS__; "
            f"ls -1p {shlex.quote(_WORKDIR)} 2>/dev/null | grep -v '/$' | head -40",
            timeout=30)
        matches: List[str] = []
        listing: List[str] = []
        if probe is not None:
            seen_ls = False
            for line in (probe.get("stdout", "") or "").splitlines():
                if line.startswith("__M__"):
                    m = line[len("__M__"):].strip()
                    if m:
                        matches.append(m)
                elif line.strip() == "__VERA_LS__":
                    seen_ls = True
                elif seen_ls and line.strip():
                    listing.append(line.strip())
        if len(matches) == 1:
            resolved = matches[0]
            q = shlex.quote(resolved)
            meta = await _exec_in(session_id, f"stat -c %s {q} 2>/dev/null || echo 0", timeout=30)
            if meta is None:
                return {"error": "sandbox exec failed"}
        else:
            hint = (" — files in /workspace: " + ", ".join(listing[:30])) if listing else \
                   " (and /workspace is empty or unreadable)"
            return {"error": f"File not found: {path}{hint}"}
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
    out = {"path": resolved, "content": content, "size": size,
           "truncated": truncated, "sandboxed": True}
    if resolved != path:
        out["resolved_from"] = path
    return out


async def route_fs_write(session_id: str, path: str, content: str) -> Optional[Dict]:
    # A write is real work: auto-create the session's sandbox (when auto_create is
    # on) exactly like route_shell/route_code/route_artifact_dir do, so a file the
    # agent writes lands in the SAME container its exec.* runs see — never on the
    # host while exec looks in a freshly-made container. Without this, ide.fs.write
    # fell through to a host write of an absolute '/workspace/x' path and died with
    # EACCES, and the loop regenerated the file forever.
    if not await _route_gate(session_id, create=True):
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
    if not await _route_gate(session_id, create=False):
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


async def route_copy_in(session_id: str, host_path: str, dest_name: str = "",
                        *, subdir: str = "") -> Optional[Dict]:
    """docker-cp a HOST file INTO the session's sandbox /workspace (binary-safe —
    the right transport for generated media like audio, not the 1 MB base64 exec
    path). Auto-creates the sandbox (a session receiving a deliverable is doing
    real work). Returns {path} (absolute container path) or None when there's no
    active sandbox → the caller writes to the host artifact dir instead."""
    rec = await _ensure_routable(session_id, create=True)
    if not rec:
        return None
    if not host_path or not os.path.isfile(host_path):
        return {"error": f"source not found: {host_path}"}
    name = re.sub(r"[^\w.\-]", "_", dest_name or os.path.basename(host_path)) or "artifact.bin"
    rel = (subdir.strip("/") + "/" + name) if subdir else name
    # Local backend: a plain filesystem copy into the workspace (no docker cp).
    if _is_local_rec(rec):
        ws = rec.get("workdir") or _local_ws_dir()
        dest = os.path.join(ws, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(host_path, dest)
            return {"path": dest, "sandboxed": True, "backend": _LOCAL_BACKEND}
        except Exception as e:
            log.debug("route_copy_in (local) failed for %s: %s", session_id, e)
            return {"error": str(e)}
    dk, host, _ = await _sbx_host(session_id, create=True)
    if dk is None:
        return None
    dest = _WORKDIR.rstrip("/") + "/" + rel
    try:
        await _exec_in(session_id, f"mkdir -p {shlex.quote(os.path.dirname(dest))}", timeout=20)
        await dk._run_local(
            await dk._docker_argv(host, ["cp", host_path, f"{rec['container']}:{dest}"]),
            timeout=180)
        chk = await _exec_in(session_id, f"[ -f {shlex.quote(dest)} ] && echo OK || echo NO", timeout=20)
        if chk and "OK" in (chk.get("stdout", "") or ""):
            return {"path": dest, "sandboxed": True}
        return {"error": "docker cp verification failed"}
    except Exception as e:
        log.debug("route_copy_in failed for %s: %s", session_id, e)
        return {"error": str(e)}


async def route_fs_list(session_id: str, path: str = "") -> Optional[Dict]:
    if not await _route_gate(session_id, create=False):
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
    if not await _route_gate(session_id, create=False):
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


async def export_workspace_changes(session_id: str) -> Optional[str]:
    """Like export_workspace, but when the container was SEEDED from a host dir
    (seeded_at set — e.g. an IDE workspace clone), prunes the export down to only
    files CREATED OR MODIFIED since the seed. docker cp preserves mtimes, so the
    seeded project files keep their (older) host mtimes while the loop's own
    output is newer — this is what stops the seeded project from being harvested
    or proposed as if the loop had produced it. Caller MUST rmtree the result."""
    tmp = await _collect_workspace(session_id)
    if tmp is None:
        return None
    try:
        rec = await _get_rec(await _resolve_sid(session_id)) or {}
        seeded_at = float(rec.get("seeded_at") or 0)
    except Exception:
        seeded_at = 0.0
    if seeded_at <= 0:
        return tmp                       # never seeded → all of it is loop output
    cutoff = seeded_at - 2.0             # small epsilon for fs mtime granularity
    try:
        for root, _dirs, files in os.walk(tmp, topdown=False):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                except Exception:
                    pass
            try:
                if root != tmp and not os.listdir(root):
                    os.rmdir(root)
            except Exception:
                pass
    except Exception as e:
        log.debug("export_workspace_changes prune %s: %s", session_id, e)
    return tmp


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
    rec = await _route_gate(session_id, create=True)
    if not rec:
        return None
    # Local backend: the artifact dir is this container's own workspace dir.
    if _is_local_rec(rec):
        ws = rec.get("workdir") or _local_ws_dir()
        try:
            os.makedirs(ws, exist_ok=True)
        except Exception:
            pass
        return ws
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

# Idle auto-ARCHIVE: a second, much longer tier past sleep — sessions unused
# for this many DAYS are fully stopped via cap_sbx_stop (commit + sync +
# remove, same as a manual archive-and-close) rather than just left sleeping
# forever. Sleeping containers still occupy disk/volume/docker-ps-a space
# indefinitely; this actually reclaims it. Restoration on next use is real —
# sandbox.session.start finds the record's committed_image and rebuilds from
# it (see the cap_sbx_stop fix, 2026-08-04: committed_image used to get
# silently wiped by a stale-record overwrite, which made this tier pointless
# — archiving worked but nothing could ever find the archive again). 0 disables.
_IDLE_ARCHIVE_DEFAULT_DAYS = int(os.getenv("VERA_SANDBOX_IDLE_ARCHIVE_DAYS", "7"))


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


async def _seed_from_host(dk, host, container: str, host_path: str) -> bool:
    """`docker cp` a HOST directory's CONTENTS into container:/workspace."""
    src = os.path.join(host_path, ".")
    cp = await dk._run_local(await dk._docker_argv(
        host, ["cp", src, f"{container}:{_WORKDIR}"]), timeout=600)
    if not cp.get("ok"):
        log.warning("sandbox seed cp failed (%s → %s): %s",
                    host_path, container, cp.get("stderr"))
    return bool(cp.get("ok"))


async def import_workspace(session_id: str, host_path: str, *,
                           only_if_empty: bool = True) -> Dict:
    """Seed a session container's /workspace FROM a host directory (the inverse
    of export_workspace) — e.g. clone an IDE workspace's project files into a
    loop's container so the loop can operate on them. By default only when
    /workspace is empty, so it never clobbers work already in the container."""
    if not host_path or not os.path.isdir(host_path):
        return {"ok": False, "error": f"host path not found: {host_path}"}
    if only_if_empty:
        await _ensure_routable(session_id, create=True)   # need a container to test
        if not await _workspace_is_empty(session_id):
            return {"ok": True, "skipped": "workspace not empty"}
    dk, host, rec = await _sbx_host_any(session_id)
    if dk is None or not rec:
        return {"ok": False, "error": "no container for session"}
    await _exec_in(session_id, f"mkdir -p {shlex.quote(_WORKDIR)}", timeout=20)
    ok = await _seed_from_host(dk, host, rec["container"], host_path)
    if ok:
        rec["seeded_at"] = time.time()
        await _save_rec(rec)
        await emit_event({"type": "remote.sandbox.seeded",
                          "session_id": session_id, "host_path": host_path})
    return {"ok": ok, "seeded_from": host_path} if ok \
        else {"ok": False, "error": "docker cp failed"}


async def set_seed_path(session_id: str, host_path: str) -> bool:
    """Record a HOST directory to seed a session's /workspace from whenever its
    container is (re)created empty (e.g. an IDE workspace's project files). The
    seed itself is applied in sandbox.session.start's fresh-volume path."""
    try:
        sid = await _resolve_sid(session_id)
        rec = await _get_rec(sid) or {"session_id": sid, "created": now_iso()}
        rec["seed_path"] = str(host_path or "")
        await _save_rec(rec)
        return True
    except Exception as e:
        log.debug("set_seed_path %s: %s", session_id, e)
        return False


async def seed_path_for_session(session_id: str) -> str:
    """The HOST seed path of the container a session resolves to (set when an IDE
    workspace was opened) — lets a caller auto-associate a program with the
    workspace the session is working in. '' when the session isn't in a workspace."""
    try:
        rec = await _get_rec(await _resolve_sid(session_id)) or {}
        return str(rec.get("seed_path") or "")
    except Exception:
        return ""


@capability(
    "sandbox.session.seed",
    http_method="POST", http_path="/remote/sandbox/seed", http_tags=["remote", "sandbox"],
    description="Seed a session's sandbox /workspace FROM a host directory — clone "
                "project files (e.g. an IDE workspace) into the container so a loop or "
                "session can operate on them. Also records the path as the container's "
                "default seed, re-applied whenever a fresh container comes up empty. "
                "Inputs: session_id (str!), host_path (str! — a directory on the Vera "
                "host), set_default (bool=true — remember it for future recreations), "
                "only_if_empty (bool=true — never clobber existing container files). "
                "Output: {ok, seeded_from, skipped?}.",
)
async def cap_sbx_seed(session_id: str = "", host_path: str = "",
                       set_default: bool = True, only_if_empty: bool = True,
                       trace_id=None) -> Dict:
    if not session_id or not host_path:
        return {"ok": False, "error": "session_id and host_path required"}
    if set_default:
        await set_seed_path(session_id, host_path)
    return await import_workspace(session_id, host_path, only_if_empty=only_if_empty)


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
        due = []
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
            due.append(rec)
        if not due:
            return
        # One bulk docker call (cached, see `_containers_state_map`) per DISTINCT
        # host instead of one `_container_running` round trip per record — with
        # hundreds of accumulated sandbox records all past the idle threshold,
        # the old per-record loop serially fired that many docker/SSH round
        # trips on the event loop every tick, which is exactly the storm
        # `_containers_state_map` was built to fix for sandbox.session.list.
        host_cache: Dict[str, Any] = {}
        state_cache: Dict[str, Dict[str, str]] = {}
        for rec in due:
            host_id = rec.get("docker_host_id", "local")
            if host_id not in host_cache:
                host_cache[host_id] = await _docker_host(dk, host_id)
                if host_cache[host_id]:
                    state_cache[host_id] = await _containers_state_map(
                        dk, host_cache[host_id], host_id)
            if not host_cache[host_id]:
                continue
            try:
                if state_cache.get(host_id, {}).get(rec["container"]) != "running":
                    continue
                await cap_sbx_sleep(session_id=rec["session_id"])
                log.info("sandbox %s idle %dmin — slept", rec["session_id"], idle_min)
            except Exception as e:
                log.debug("idle sleep for %s failed: %s", rec.get("session_id"), e)
    except Exception as e:
        log.debug("sandbox idle-sleep tick failed: %s", e)


async def _idle_archive_tick() -> None:
    """Scheduler tick: fully ARCHIVE (commit + sync + remove, via cap_sbx_stop)
    sessions unused for idle_archive_days (0 = disabled) — the second, much
    longer tier past _idle_sleep_tick's docker-stop. A session sleeping
    forever still occupies real disk (image layers + volume) indefinitely;
    this reclaims it. Checked regardless of the `active` flag (cap_sbx_sleep
    never clears it, so a long-sleeping session stays "active" forever and
    would never be considered otherwise) — the real signal is last_used
    staleness, not that flag."""
    try:
        cfg = await _get_cfg()
        idle_days = int(cfg.get("idle_archive_days", _IDLE_ARCHIVE_DEFAULT_DAYS) or 0)
        if idle_days <= 0:
            return
        r = _redis()
        if not r:
            return
        items = await r.hgetall(KEY_SBX)
        now = time.time()
        threshold_s = idle_days * 86400
        for v in (items or {}).values():
            try:
                rec = json.loads(v)
            except Exception:
                continue
            if not rec.get("container"):
                continue
            last = float(rec.get("last_used") or 0)
            if not last or now - last < threshold_s:
                continue
            try:
                res = await cap_sbx_stop(session_id=rec["session_id"], remove=True)
                if res.get("ok"):
                    log.info("sandbox %s idle %dd — archived (committed_image=%s)",
                             rec["session_id"], idle_days, res.get("committed_image", ""))
            except Exception as e:
                log.debug("idle archive for %s failed: %s", rec.get("session_id"), e)
    except Exception as e:
        log.debug("sandbox idle-archive tick failed: %s", e)


# Register the periodic auto-sync + idle-sleep + idle-archive (the
# scheduler_loop invokes them; each tick throttles per-session and no-ops
# when its config interval/threshold is 0).
try:
    schedule(_auto_sync_tick, 60, name="sandbox_auto_sync")
except Exception as _e:
    log.debug("could not register sandbox auto-sync: %s", _e)
try:
    schedule(_idle_sleep_tick, 120, name="sandbox_idle_sleep")
except Exception as _e:
    log.debug("could not register sandbox idle-sleep: %s", _e)
try:
    schedule(_idle_archive_tick, 3600, name="sandbox_idle_archive")
except Exception as _e:
    log.debug("could not register sandbox idle-archive: %s", _e)


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
