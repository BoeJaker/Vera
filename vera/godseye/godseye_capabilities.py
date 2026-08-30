"""
godseye_capabilities.py — Godseye globe, integrated into Vera (group `godseye.*`)
================================================================================

Godseye (https://github.com/VrushankPatel/godseye) is a frontend-only React +
Vite + CesiumJS geospatial-intelligence dashboard: a 3D globe with live
aircraft, satellites, seismic/volcano, weather, maritime, air-quality, aurora
and CCTV layers, plus tactical view modes (NVG / FLIR / CRT / God Mode).

It is integrated **without being absorbed**. Godseye stays its own upstream git
repository, cloned at runtime into a git-ignored ``vendor/godseye`` directory;
Vera never tracks a line of its source. This module owns the lifecycle around
that clone:

  • ``godseye.status``        — is it cloned / built / configured, and at what commit
  • ``godseye.repo.sync``     — clone it, or fast-forward an existing clone
                                (git runs in a container too, so the host needs
                                no git and imposes no git config on the fetch)
  • ``godseye.build``         — build it in a throwaway Node container (the host
                                needs no Node toolchain at all) into ``dist/``
  • ``godseye.build.status``  — real build progress, read from the build log
  • ``godseye.config.get/set``— Godseye's optional BYOK API keys, sealed at rest

…and serves the built bundle itself, so the globe is just another Vera tab on
the same origin — no second server, no extra port, nothing to babysit.

HTTP
────
  /godseye/panel            the Vera panel (status + controls + the globe)
  /godseye/app/{path}       the built SPA (Vite ``base`` MUST match this prefix)

Redis
─────
  vera:godseye:config   hash  VITE_* -> sealed value (BYOK keys)

Scope note: this is the UI + lifecycle integration. Godseye's individual data
feeds (OpenSky, CelesTrak, USGS, …) are browser-side JS; exposing them as Vera
capabilities in their own right is a separate follow-up, not part of this.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
)

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
from Vera.vera.godseye import godseye_core as _core
from Vera.vera.godseye import godseye_cctv_core as _cctv
from Vera.vera.godseye import godseye_imagery_core as _img
from Vera.vera.godseye import godseye_buildings_core as _bld

try:
    from Vera.vera.security import secrets as vsecrets
except Exception:                                   # pragma: no cover
    vsecrets = None                                 # type: ignore

log = logging.getLogger("vera.godseye")

_HERE = Path(__file__).parent
_ROOT = _core.repo_root(__file__)
_BUILD_SCRIPT = _HERE / "godseye_build.sh"
_SYNC_SCRIPT = _HERE / "godseye_sync.sh"
_BUILD_CONTAINER = "godseye-build"
KEY_CONFIG = "vera:godseye:config"


# ── small helpers ────────────────────────────────────────────────────────────
def _host_uid() -> int:
    return getattr(os, "getuid", lambda: 0)()


def _host_gid() -> int:
    return getattr(os, "getgid", lambda: 0)()



def _layout() -> Dict[str, Path]:
    """Resolved every call so a VERA_GODSEYE_DIR change takes effect without a
    restart (and so tests can drive it by environment)."""
    return _core.resolve_layout(_ROOT, dict(os.environ))


def _redis():
    return getattr(_orch, "REDIS", None)


def _seal(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.seal(v)
        except Exception:
            return v
    return v


def _open(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.open(v)
        except Exception:
            return v
    return v


async def _run(argv: List[str], cwd: Optional[Path] = None,
               timeout: int = 300,
               env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run a command with no shell — every argument stays a separate argv entry,
    so a hostile branch name or URL cannot become a second command."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd) if cwd else None, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return {"ok": False, "rc": 127, "out": "", "err": f"{argv[0]}: not found on this host"}
    except Exception as e:                          # pragma: no cover — defensive
        return {"ok": False, "rc": -1, "out": "", "err": str(e)}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "rc": -1, "out": "", "err": f"timed out after {timeout}s"}
    return {"ok": proc.returncode == 0, "rc": proc.returncode,
            "out": (out or b"").decode("utf-8", "replace").strip(),
            "err": (err or b"").decode("utf-8", "replace").strip()}


#: The answer only changes when .gitignore does, and godseye.status is polled by
#: an open panel — no reason to fork git every time.
_IGNORE_CACHE: Dict[str, Any] = {"key": None, "value": None, "at": 0.0}
_IGNORE_TTL_S = 120.0


async def _ignored_by_vera_git(clone: Path) -> Tuple[bool, str]:
    """Is the clone directory excluded from Vera's OWN git?

    This is the guard that keeps the promise in the module docstring. A clone
    Vera's git can see makes every checkout permanently dirty, which blocks the
    Loop Lab promote path for every agent — so cloning refuses unless the path
    is ignored. A clone parked outside the repo entirely is fine by definition.

    Asks git first. Vera also runs from images with no ``.git`` at all, where
    `check-ignore` cannot answer; there it falls back to reading ``.gitignore``
    literally, and an inconclusive answer refuses rather than assumes.
    """
    try:
        rel = clone.resolve().relative_to(Path(_ROOT).resolve())
    except ValueError:
        return True, "outside the Vera repo"

    key = str(clone)
    if (_IGNORE_CACHE["key"] == key and _IGNORE_CACHE["value"] is not None
            and (time.monotonic() - _IGNORE_CACHE["at"]) < _IGNORE_TTL_S):
        return _IGNORE_CACHE["value"]

    verdict = await _ignore_verdict(clone, rel)
    _IGNORE_CACHE.update(key=key, value=verdict, at=time.monotonic())
    return verdict


async def _ignore_verdict(clone: Path, rel: Path) -> Tuple[bool, str]:
    r = await _run(["git", "-C", str(_ROOT), "check-ignore", "-q", "--", str(clone)],
                   timeout=30)
    if r["rc"] == 0:
        return True, "git-ignored"
    if r["rc"] == 1:
        return False, "NOT ignored by Vera's .gitignore"

    # git could not answer (no .git in this image, or no git binary).
    try:
        text = (Path(_ROOT) / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if _core.gitignore_covers(str(rel), text):
        return True, "covered by .gitignore (git unavailable here)"
    return False, (r["err"] or "could not run `git check-ignore`")


def _clone_commit(clone: Path) -> Dict[str, str]:
    """What the clone is sitting on — read from the metadata the sync container
    wrote, or from .git/HEAD. Deliberately does NOT shell out to git: the host
    is not required to have it (that is the whole point of syncing in a
    container), and status is polled by an open panel."""
    if not (clone / ".git").exists():
        return {}
    return _core.read_repo_meta(_layout()["state_dir"], clone)


#: A built Cesium bundle is ~2,000 files. The panel polls status every 3s during
#: a build, so walking it each time would put thousands of stat() calls a minute
#: on the event loop. Cache the walk and redo it only when index.html moves —
#: which is exactly when a build has produced something new.
_DIST_CACHE: Dict[str, Any] = {"key": None, "value": None}
_NO_DIST = {"built": False, "files": 0, "bytes": 0, "mtime": None}


def _scan_dist(dist: Path) -> Dict[str, Any]:
    return _core.summarize_dist([p for p in dist.rglob("*") if p.is_file()], dist)


async def _dist_summary() -> Dict[str, Any]:
    dist = _layout()["dist_dir"]
    index = dist / "index.html"
    try:
        st = index.stat()
    except OSError:
        return dict(_NO_DIST)
    key = f"{dist}|{st.st_mtime_ns}|{st.st_size}"
    if _DIST_CACHE["key"] == key and _DIST_CACHE["value"] is not None:
        return dict(_DIST_CACHE["value"])
    value = await asyncio.to_thread(_scan_dist, dist)
    _DIST_CACHE.update(key=key, value=value)
    return dict(value)


def _read_log(tail: int = 40) -> Dict[str, Any]:
    p = _layout()["log_path"]
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"started": False, "finished": False, "running": False, "ok": False,
                "exit_code": None, "stage": "", "lines": 0, "tail": []}
    return _core.parse_build_log(text, tail=tail)


async def _load_config() -> Dict[str, str]:
    r = _redis()
    if not r:
        return {}
    try:
        raw = await r.hgetall(KEY_CONFIG)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for k, v in (raw or {}).items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        val = v.decode() if isinstance(v, bytes) else str(v)
        out[key] = _open(val)
    return _core.known_config(out)


# ═════════════════════════════════════════════════════════════════════════════
#  Capabilities
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "godseye.status", memory="off", silent=True,
    http_method="GET", http_path="/godseye/status", http_tags=["godseye"],
    description="State of the vendored Godseye globe app: whether its separate "
                "upstream repo is cloned, at what commit, whether a built bundle "
                "exists, the last build's outcome, and which BYOK keys are set. "
                "Output: {cloned, commit, dist, build, config_set[], paths, app_url}.",
)
async def godseye_status(trace_id=None) -> Dict[str, Any]:
    lay = _layout()
    cloned = (lay["clone_dir"] / ".git").exists()
    cfg = await _load_config()
    ignored, ignore_reason = await _ignored_by_vera_git(lay["clone_dir"])
    return {
        "upstream": _core.UPSTREAM_URL,
        "cloned": cloned,
        "commit": _clone_commit(lay["clone_dir"]) if cloned else {},
        "dist": await _dist_summary(),
        "build": _read_log(tail=12),
        "config_set": sorted(k for k, v in cfg.items() if v),
        "app_url": _core.APP_BASE,
        "panel_url": "/godseye/panel",
        "vendor_ignored": ignored,
        "vendor_ignored_reason": ignore_reason,
        "paths": {k: str(v) for k, v in lay.items()},
    }


@capability(
    "godseye.repo.sync", memory="on",
    http_method="POST", http_path="/godseye/repo/sync", http_tags=["godseye"],
    description="Clone OUR FORK of Godseye into Vera's git-ignored vendor "
                "directory, or bring upstream's changes into it. The clone is a "
                "real fork: remote `upstream` is only ever fetched, our commits "
                "live on branch `vera`, and updating MERGES upstream into that "
                "branch — never a hard reset, so local work survives. A merge "
                "conflict stops with the tree untouched and reports it. The "
                "branch is pushed to a durable fork remote outside the "
                "disposable vendor dir (VERA_GODSEYE_FORK). Godseye stays a "
                "SEPARATE repo — this never adds its source to Vera's git. "
                "Refuses to clone into a path Vera's git can see (that would "
                "leave every checkout dirty). git runs INSIDE a throwaway "
                "container, so the host needs no git and cannot impose its own "
                "git config on the fetch — needs a Docker socket, same as "
                "godseye.build. Inputs: ref (branch/tag/sha), url (str, defaults "
                "to upstream), depth (int=1, 0=full). "
                "Output: {ok, action, commit, path}.",
)
async def godseye_repo_sync(ref: str = "", url: str = "", depth: int = 0,
                            trace_id=None) -> Dict[str, Any]:
    url = (url or os.environ.get("VERA_GODSEYE_REPO_URL") or _core.UPSTREAM_URL).strip()
    ref = (ref or "").strip()
    allowed, why = _core.allowed_repo_url(url, dict(os.environ))
    if not allowed:
        return {"ok": False, "error": why}
    if ref and not _core.is_safe_ref(ref):
        return {"ok": False, "error": f"unsafe ref: {ref!r}"}

    lay = _layout()
    clone = lay["clone_dir"]
    ignored, reason = await _ignored_by_vera_git(clone)
    if not ignored:
        return {"ok": False, "error": (
            f"refusing to clone into {clone} — {reason}. Godseye must stay out "
            "of Vera's tracked tree; add the directory to .gitignore (or set "
            "VERA_GODSEYE_DIR to a path outside the repo) first.")}

    if not _SYNC_SCRIPT.is_file():
        return {"ok": False, "error": f"sync script missing: {_SYNC_SCRIPT}"}
    clone.parent.mkdir(parents=True, exist_ok=True)
    lay["state_dir"].mkdir(parents=True, exist_ok=True)
    fork = lay["fork_dir"]
    try:
        fork.mkdir(parents=True, exist_ok=True)     # the container inits it bare
    except OSError as e:
        log.warning("godseye: fork dir %s unusable (%s) — syncing without it", fork, e)
        fork = None

    # git runs in the container, never on the host — see godseye_sync.sh.
    argv = _core.docker_sync_argv(
        vendor_dir=lay["vendor_dir"], script_path=_SYNC_SCRIPT,
        clone_name=clone.name, url=url, ref=ref, depth=depth,
        image=os.environ.get("VERA_GODSEYE_IMAGE") or _core.DEFAULT_NODE_IMAGE,
        uid=_host_uid(), gid=_host_gid(), fork_dir=fork)
    r = await _run(argv, timeout=1800)
    # rc 5 is the deliberate "upstream merge conflicts with our fork" signal:
    # the tree was left untouched and a human has to reconcile it.
    if r.get("rc") == 5:
        meta = _core.read_repo_meta(lay["state_dir"], clone)
        return {"ok": False, "action": "conflict", "commit": meta,
                "error": ("upstream does not merge cleanly into our fork branch "
                          f"'{_core.WORK_BRANCH}'. Nothing was changed. Resolve it in "
                          f"{clone} (git merge upstream/HEAD) and push to the fork.")}
    if not r["ok"]:
        return {"ok": False, "action": "sync",
                "error": r["err"] or r["out"] or "git container failed"}

    meta = _core.read_repo_meta(lay["state_dir"], clone)
    action = meta.get("action") or "synced"
    commit = {k: meta.get(k, "") for k in ("sha", "short", "committed_at", "subject")}
    await emit_event({"type": "godseye.repo.synced", "action": action,
                      "commit": commit.get("short", ""), "path": str(clone)})
    return {"ok": True, "action": action, "commit": commit, "path": str(clone),
            "branch": meta.get("branch", _core.WORK_BRANCH),
            "fork": str(fork) if fork else "",
            "ahead": meta.get("ahead", 0), "behind": meta.get("behind", 0),
            "merge": meta.get("merge", ""), "pushed": meta.get("pushed", ""),
            "next": "godseye.build"}


@capability(
    "godseye.build", memory="on",
    http_method="POST", http_path="/godseye/build", http_tags=["godseye"],
    description="Build the vendored Godseye app into static assets Vera serves. "
                "Runs npm ci + vite build inside a THROWAWAY Node container "
                "(the host needs no Node toolchain) against the git-ignored "
                "clone, as the host uid so nothing comes back root-owned. The "
                "cloned repo's npm lifecycle scripts run during this, which is "
                "why godseye.repo.sync pins which repo may be vendored. "
                "Returns as soon as the build STARTS — poll godseye.build.status "
                "for real progress; never infer progress from elapsed time. "
                "Needs a Docker socket, so it works where Vera runs natively, "
                "not from inside a sandbox container. "
                "Inputs: refresh_manifests (bool=false — slow, re-verifies "
                "thousands of upstream feeds), clean_install (bool=false — force "
                "a full `npm ci` even when node_modules is current), image (str), "
                "force (bool — restart a build already running). "
                "Output: {ok, started, container}.",
)
async def godseye_build(refresh_manifests: bool = False, image: str = "",
                        force: bool = False, clean_install: bool = False,
                        trace_id=None) -> Dict[str, Any]:
    lay = _layout()
    if not (lay["clone_dir"] / "package.json").is_file():
        return {"ok": False, "error": "Godseye is not cloned yet — run godseye.repo.sync first"}
    if not _BUILD_SCRIPT.is_file():
        return {"ok": False, "error": f"build script missing: {_BUILD_SCRIPT}"}

    state = _read_log()
    if state["running"] and not force:
        return {"ok": False, "error": "a build is already running — poll "
                                      "godseye.build.status, or pass force=true",
                "build": state}

    # A previous container with this name (finished or wedged) would make
    # `docker run --name` fail outright; clearing it is the whole point of force.
    await _run(["docker", "rm", "-f", _BUILD_CONTAINER], timeout=60)

    lay["state_dir"].mkdir(parents=True, exist_ok=True)

    # Regenerate .env.local from the sealed BYOK config so a key set through
    # godseye.config.set actually reaches the bundle Vite is about to inline.
    cfg = await _load_config()
    try:
        lay["env_file"].write_text(_core.env_file_text(cfg), encoding="utf-8")
    except OSError as e:
        log.warning("godseye: could not write %s: %s", lay["env_file"], e)

    argv = _core.docker_build_argv(
        vendor_dir=lay["vendor_dir"], script_path=_BUILD_SCRIPT,
        clone_name=lay["clone_dir"].name,
        image=(image or os.environ.get("VERA_GODSEYE_IMAGE")
               or _core.DEFAULT_NODE_IMAGE),
        container=_BUILD_CONTAINER, uid=_host_uid(), gid=_host_gid(),
        base=_core.APP_BASE, refresh_manifests=bool(refresh_manifests),
        clean_install=bool(clean_install))
    # Claim the log BEFORE the container starts. Until the container truncates
    # it, godseye.build.status would otherwise still be reporting the PREVIOUS
    # build's `GODSEYE_EXIT=0`, so a watching panel sees "finished, ok" for a
    # build that has not begun and stops polling.
    try:
        lay["log_path"].write_text(
            f"[godseye] build queued {now_iso()} (waiting for the container)\n",
            encoding="utf-8")
    except OSError as e:
        log.warning("godseye: could not stamp %s: %s", lay["log_path"], e)

    r = await _run(argv, timeout=300)
    if not r["ok"]:
        try:
            lay["log_path"].write_text(
                f"[godseye] could not start the build container\n{r['err'] or r['out']}\n"
                "GODSEYE_EXIT=125\n", encoding="utf-8")
        except OSError:
            pass
        return {"ok": False, "error": r["err"] or r["out"] or "docker run failed",
                "argv": argv}

    await emit_event({"type": "godseye.build.started",
                      "refresh_manifests": bool(refresh_manifests),
                      "image": image or _core.DEFAULT_NODE_IMAGE})
    return {"ok": True, "started": True, "container": _BUILD_CONTAINER,
            "started_at": now_iso(), "poll": "godseye.build.status"}


@capability(
    "godseye.build.status", memory="off", silent=True,
    http_method="GET", http_path="/godseye/build/status", http_tags=["godseye"],
    description="Progress of the Godseye build, read from the build log the "
                "container writes into the bind mount (the only truthful "
                "progress signal — the container is detached). "
                "Query: tail (int=40). Output: {running, finished, ok, exit_code, "
                "stage, tail[], dist}.",
)
async def godseye_build_status(tail: int = 40, trace_id=None) -> Dict[str, Any]:
    state = _read_log(tail=max(0, min(int(tail or 40), 500)))
    state["dist"] = await _dist_summary()
    state["log_path"] = str(_layout()["log_path"])
    return state


@capability(
    "godseye.config.get", memory="off", silent=True,
    http_method="GET", http_path="/godseye/config", http_tags=["godseye"],
    description="Godseye's optional BYOK API keys (Google Maps, YouTube, "
                "Guardian, AISstream, …), REDACTED. Each key is optional: an "
                "unset key simply disables that layer. Output: {keys{}, set[]}.",
)
async def godseye_config_get(trace_id=None) -> Dict[str, Any]:
    cfg = await _load_config()
    return {"keys": _core.redact_config(cfg),
            "set": sorted(k for k, v in cfg.items() if v),
            "known": list(_core.BYOK_KEYS),
            "note": "Vite inlines these into the built bundle at build time, so "
                    "anyone who can load the page can read them. Rebuild "
                    "(godseye.build) after changing a key."}


@capability(
    "godseye.config.set", memory="on",
    http_method="POST", http_path="/godseye/config/set", http_tags=["godseye"],
    description="Set or clear Godseye BYOK API keys (sealed at rest). Only "
                "recognised VITE_* keys are accepted; an empty value clears one. "
                "Takes effect on the NEXT godseye.build. Inputs: keys (dict). "
                "Output: {ok, set[], cleared[]}.",
)
async def godseye_config_set(keys: Optional[Dict[str, str]] = None,
                             trace_id=None) -> Dict[str, Any]:
    incoming = _core.known_config(keys or {})
    if not incoming:
        return {"ok": False, "error": "no recognised keys — known: "
                                      + ", ".join(_core.BYOK_KEYS)}
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    was_set, cleared = [], []
    for k, v in incoming.items():
        try:
            if v:
                await r.hset(KEY_CONFIG, k, _seal(v))
                was_set.append(k)
            else:
                await r.hdel(KEY_CONFIG, k)
                cleared.append(k)
        except Exception as e:
            return {"ok": False, "error": f"redis write failed: {e}"}
    await emit_event({"type": "godseye.config.set", "keys": sorted(was_set + cleared)})
    return {"ok": True, "set": sorted(was_set), "cleared": sorted(cleared),
            "rebuild_required": True}


# ═════════════════════════════════════════════════════════════════════════════
#  HTTP — the panel and the built SPA
# ═════════════════════════════════════════════════════════════════════════════
def _placeholder(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;height:100%;background:#0d0f12;color:#d6dde6;"
        "font:14px/1.6 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;"
        "display:flex;align-items:center;justify-content:center}"
        "div{max-width:46ch;padding:24px;text-align:center}"
        "h2{margin:0 0 10px;font-size:16px;color:#5aa9ff}"
        "code{background:#1b1f26;padding:1px 5px;border-radius:4px}</style>"
        f"<div><h2>{title}</h2><p>{body}</p></div>",
        status_code=200)


@APP.get("/godseye/panel", include_in_schema=False)
async def _godseye_panel():
    p = _HERE / "godseye_panel.html"
    if not p.is_file():                             # pragma: no cover — defensive
        return _placeholder("Godseye", "godseye_panel.html not found")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@APP.get(_core.TILE_MOUNT + "/{z}/{y}/{x}", include_in_schema=False)
async def _godseye_tile_legacy(z: int, y: int, x: int):
    """The original single-layer path, kept so a bundle built before the
    per-layer route was added keeps rendering instead of losing its map."""
    return await _serve_tile("imagery", z, y, x)


@APP.get(_core.TILE_MOUNT + "/{layer}/{z}/{y}/{x}", include_in_schema=False)
async def _godseye_tile(layer: str, z: int, y: int, x: int):
    return await _serve_tile(layer, z, y, x)


async def _serve_tile(layer: str, z: int, y: int, x: int):
    """Cache-in-front-of-Esri for the globe's base imagery.

    Godseye asks the BROWSER for every tile straight from arcgisonline, so a
    pan or zoom is a burst of cross-internet round trips and nothing is ever
    reused between sessions or between people. Proxying them means each tile
    crosses the internet once and is LAN-speed forever after.

    Deliberately narrow: coordinates are validated as integers in range and the
    upstream URL is built from those ints, so this cannot be steered at another
    host or walked out of the cache directory.
    """
    if not _core.tile_is_valid(z, y, x, layer):
        return JSONResponse({"error": "bad tile coordinate or layer",
                             "layers": sorted(_core.TILE_LAYERS)}, status_code=400)

    cached = _core.tile_cache_path(_layout()["tile_dir"], z, y, x, layer)
    headers = {
        # Imagery for a fixed z/y/x does not change in any way we care about.
        "Cache-Control": "public, max-age=604800",
        "X-Content-Type-Options": "nosniff",
    }
    if cached.is_file():
        return FileResponse(str(cached), media_type=_core.tile_media_type(layer),
                            headers={**headers, "X-Godseye-Tile": "hit"})

    url = _core.tile_upstream_url(
        z, y, x, os.environ.get("VERA_GODSEYE_TILE_URL", ""), layer=layer)
    try:
        # Shared pooled client: a per-request client meant a TLS handshake per
        # tile, which made proxying slower than fetching upstream directly.
        r = await (await _tile_client()).get(url)
    except Exception as e:
        return JSONResponse({"error": f"tile fetch failed: {e}"}, status_code=502)
    if r.status_code != 200 or not r.content:
        return JSONResponse({"error": "upstream tile unavailable",
                             "status": r.status_code}, status_code=502)

    # Write via a temp file in the same directory: a half-written tile that a
    # concurrent request could serve as a truncated image is worse than a miss.
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(cached)
    except OSError as e:                            # a full disk must not 500
        log.warning("godseye: could not cache tile %s/%s/%s: %s", z, y, x, e)

    return Response(content=r.content, media_type=_core.tile_media_type(layer),
                    headers={**headers, "X-Godseye-Tile": "miss"})


@capability(
    "cctv.sources", memory="off", silent=True,
    http_method="GET", http_path="/godseye/cctv/sources", http_tags=["godseye"],
    description="Open CCTV providers Vera aggregates for the globe's camera "
                "layer, plus the ones probed and found CLOSED (several '511' "
                "sites now require an API key, including one Godseye still "
                "calls). Output: {sources[], closed{}}.",
)
async def cctv_sources(trace_id=None) -> Dict[str, Any]:
    return {"sources": _cctv.SOURCES, "count": len(_cctv.SOURCES),
            "closed": _cctv.CLOSED_SOURCES,
            "manifest_url": "/godseye/cctv/manifest.json"}


@capability(
    "cctv.refresh", memory="on",
    http_method="POST", http_path="/godseye/cctv/refresh", http_tags=["godseye"],
    description="Fetch every registered CCTV provider and rebuild the camera "
                "manifest Godseye consumes. Sources are fetched concurrently "
                "and a provider that fails is recorded in the manifest's errors "
                "rather than shrinking the map silently. Inputs: per_source "
                "(int cap, 0=all), timeout (int). Output: {ok, feedCount, "
                "byProvider, errors}.",
)
async def cctv_refresh(per_source: int = 0, timeout: int = 45,
                       trace_id=None) -> Dict[str, Any]:
    groups: List[Any] = []
    errors: Dict[str, str] = {}
    limit = per_source if per_source and per_source > 0 else 5000

    async def _one(src: Dict[str, Any], client) -> None:
        try:
            r = await client.get(src["url"], headers={"user-agent": "Vera/Godseye"})
            if r.status_code != 200:
                errors[src["id"]] = f"HTTP {r.status_code}"
                return
            body = r.text if src["kind"] == "caltrans" else r.json()
            feeds = _cctv.parse_source(src, body, limit)
            if not feeds:
                # An empty parse is nearly always a changed payload, not an
                # empty province — surface it instead of quietly losing cameras.
                errors[src["id"]] = "parsed 0 cameras"
            groups.append(feeds)
        except Exception as e:
            errors[src["id"]] = f"{type(e).__name__}: {e}"[:200]

    async with httpx.AsyncClient(timeout=float(timeout), follow_redirects=True) as client:
        await asyncio.gather(*(_one(s, client) for s in _cctv.SOURCES))

    # Fold in any stream urls already resolved, so a refresh does not downgrade
    # cameras back to stills and throw away work cctv.streams.resolve has done.
    known_streams = _load_streams()
    groups = [_cctv.apply_streams(g, known_streams) for g in groups]
    manifest = _cctv.build_manifest(groups, generated_at=now_iso(), errors=errors)
    lay = _layout()
    try:
        lay["state_dir"].mkdir(parents=True, exist_ok=True)
        path = lay["state_dir"] / "cctv-manifest.json"
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps(manifest), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        return {"ok": False, "error": f"could not write manifest: {e}"}

    await emit_event({"type": "godseye.cctv.refreshed",
                      "feeds": manifest["feedCount"], "errors": len(errors)})
    return {"ok": True, "feedCount": manifest["feedCount"],
            "byProvider": manifest["byProvider"], "errors": errors}


@capability(
    "imagery.sources", memory="off", silent=True,
    http_method="GET", http_path="/godseye/imagery/sources", http_tags=["godseye"],
    description="Geospatial image providers: which need no key, and which are "
                "key-gated and therefore currently skipped. This is search BY "
                "LOCATION, not reverse image lookup. Output: {open[], keyed{}, "
                "available[]}.",
)
async def imagery_sources(trace_id=None) -> Dict[str, Any]:
    keyed = {name: {"env": env, "configured": bool(os.environ.get(env))}
             for name, env in _img.KEYED_PROVIDERS.items()}
    return {"open": list(_img.OPEN_PROVIDERS), "keyed": keyed,
            "available": list(_img.OPEN_PROVIDERS)
                         + [n for n, v in keyed.items() if v["configured"]],
            "note": "Mapillary is the only provider carrying a camera bearing; "
                    "set VERA_MAPILLARY_TOKEN to enable it."}


@capability(
    "imagery.search", memory="on",
    http_method="POST", http_path="/godseye/imagery/search", http_tags=["godseye"],
    description="Find images taken within a bounding box, across every "
                "configured provider concurrently. Search BY LOCATION — not "
                "reverse image search. A provider that fails is reported in "
                "`errors`; one lacking a key is reported in `skipped`, kept "
                "separate so a broken source cannot hide behind 'no key'. "
                "Inputs: bbox ([south,west,north,east]!), limit (int=50), "
                "sources (list[str]). Output: {count, byProvider, images[], "
                "errors, skipped}.",
)
async def imagery_search(bbox: Optional[List[float]] = None, limit: int = 50,
                         sources: Optional[List[str]] = None,
                         trace_id=None) -> Dict[str, Any]:
    if not bbox or len(bbox) != 4:
        return {"ok": False, "error": "bbox required as [south, west, north, east]"}
    try:
        box = tuple(float(v) for v in bbox)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bbox values must be numbers"}
    if box[0] > box[2] or box[1] > box[3]:
        return {"ok": False, "error": "bbox must be [south, west, north, east]"}

    lat, lon, radius = _img.bbox_centre(box)
    limit = max(1, min(int(limit or 50), 200))
    wanted = set(sources or (list(_img.OPEN_PROVIDERS) + list(_img.KEYED_PROVIDERS)))
    groups: List[List[Dict[str, Any]]] = []
    errors: Dict[str, str] = {}
    skipped: Dict[str, str] = {}

    async def _get(client, url, params):
        r = await client.get(url, params=params,
                             headers={"user-agent": "Vera/Godseye imagery"})
        r.raise_for_status()
        return r.json()

    async def _run_provider(name: str, client) -> None:
        try:
            if name == "commons":
                geo = await _get(client, _img.COMMONS_API,
                                 _img.commons_geosearch_params(lat, lon, radius, limit))
                ids = [row.get("pageid") for row
                       in (((geo or {}).get("query") or {}).get("geosearch") or [])
                       if row.get("pageid")]
                if not ids:
                    groups.append([])
                    return
                # Coordinates and urls come from different endpoints; batch the
                # second call so this stays two requests, not one per image.
                info = await _get(client, _img.COMMONS_API,
                                  _img.commons_imageinfo_params(ids))
                groups.append(_img.parse_commons(geo, info))
            elif name == "wikipedia":
                groups.append(_img.parse_wikipedia(await _get(
                    client, _img.WIKIPEDIA_API,
                    _img.wikipedia_geosearch_params(lat, lon, radius, limit))))
            elif name == "inaturalist":
                groups.append(_img.parse_inaturalist(await _get(
                    client, _img.INAT_API, _img.inat_params(box, limit))))
            elif name in _img.KEYED_PROVIDERS:
                token = os.environ.get(_img.KEYED_PROVIDERS[name], "")
                if not token:
                    skipped[name] = f"no {_img.KEYED_PROVIDERS[name]}"
                    return
                if name == "mapillary":
                    groups.append(_img.parse_mapillary(await _get(
                        client, _img.MAPILLARY_API,
                        _img.mapillary_params(box, token, limit))))
                else:
                    groups.append(_img.parse_flickr(await _get(
                        client, _img.FLICKR_API,
                        _img.flickr_params(box, token, limit))))
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"[:200]

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        await asyncio.gather(*(_run_provider(n, client) for n in sorted(wanted)))

    result = _img.build_result(groups, bbox=box, generated_at=now_iso(),
                               errors=errors, skipped=skipped)
    await emit_event({"type": "godseye.imagery.searched",
                      "count": result["count"], "errors": len(errors)})
    result["ok"] = True
    return result


#: ONE shared client for upstream tile fetches, for the whole process.
#:
#: Measured 2026-08-30: label-overlay tiles were taking 2.3-6.6 SECONDS each on
#: a cold cache. The cause was this module creating a fresh httpx.AsyncClient
#: per request — a new TCP connection and TLS handshake to Esri for every one
#: of ~192 tiles in a page load. Proxying was therefore SLOWER than letting the
#: browser fetch direct, which is the opposite of the point.
#:
#: A shared pooled client reuses connections across tiles. Kept module-level and
#: lazily built because there is no event loop at import time.
_TILE_CLIENT: Optional["httpx.AsyncClient"] = None
_TILE_CLIENT_LOCK = asyncio.Lock()


async def _tile_client() -> "httpx.AsyncClient":
    global _TILE_CLIENT
    if _TILE_CLIENT is not None and not _TILE_CLIENT.is_closed:
        return _TILE_CLIENT
    async with _TILE_CLIENT_LOCK:
        if _TILE_CLIENT is None or _TILE_CLIENT.is_closed:
            _TILE_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=8.0),
                follow_redirects=True,
                # Enough keepalive slots that a whole screen of tiles reuses
                # connections instead of renegotiating TLS each time.
                limits=httpx.Limits(max_connections=32,
                                    max_keepalive_connections=16,
                                    keepalive_expiry=120.0),
                headers={"user-agent": "Vera/Godseye tiles"},
            )
    return _TILE_CLIENT


def _streams_path() -> Path:
    return _layout()["state_dir"] / "cctv-streams.json"


def _load_streams() -> Dict[str, str]:
    try:
        data = json.loads(_streams_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@capability(
    "cctv.streams.resolve", memory="on",
    http_method="POST", http_path="/godseye/cctv/streams/resolve",
    http_tags=["godseye"],
    description="Turn stream-CAPABLE cameras into actual playable video. "
                "Caltrans flags roughly two thirds of its cameras as having "
                "live HLS, but the playlist url is only on each camera's "
                "detail page and is NOT derivable from it — so this reads those "
                "pages and caches the result. Deliberately incremental and "
                "rate-limited: there are thousands, and resolving them in one "
                "burst is how an IP gets blocked. Call repeatedly; it resumes "
                "where it left off. Inputs: batch (int=200), concurrency "
                "(int=6). Output: {resolved, failed, cached_total, remaining}.",
)
async def cctv_streams_resolve(batch: int = 200, concurrency: int = 6,
                               trace_id=None) -> Dict[str, Any]:
    manifest_path = _layout()["state_dir"] / "cctv-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "error": "no camera manifest yet — run cctv.refresh first"}

    feeds = manifest.get("feeds") or []
    streams = _load_streams()
    targets = _cctv.pending_stream_targets(feeds, streams,
                                           limit=max(1, min(int(batch), 1000)))
    if not targets:
        return {"ok": True, "resolved": 0, "failed": 0,
                "cached_total": len(streams), "remaining": 0,
                "note": "every stream-capable camera is resolved"}

    sem = asyncio.Semaphore(max(1, min(int(concurrency), 12)))
    resolved, failed = {}, 0

    async def _one(target: Dict[str, str], client) -> None:
        nonlocal failed
        async with sem:                     # a hard ceiling on concurrent hits
            try:
                r = await client.get(target["detailsUrl"],
                                     headers={"user-agent": "Vera/Godseye"})
                url = _cctv.extract_stream_url(r.text) if r.status_code == 200 else ""
                if url:
                    resolved[target["id"]] = url
                else:
                    failed += 1
            except Exception:
                failed += 1

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        await asyncio.gather(*(_one(t, client) for t in targets))

    streams.update(resolved)
    try:
        p = _streams_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_text(json.dumps(streams), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        return {"ok": False, "error": f"could not persist streams: {e}"}

    remaining = len(_cctv.pending_stream_targets(feeds, streams, limit=100000))
    await emit_event({"type": "godseye.cctv.streams.resolved",
                      "resolved": len(resolved), "remaining": remaining})
    return {"ok": True, "resolved": len(resolved), "failed": failed,
            "cached_total": len(streams), "remaining": remaining,
            "next": "call again to continue" if remaining else "complete"}


@capability(
    "buildings.fetch", memory="on",
    http_method="POST", http_path="/godseye/buildings", http_tags=["godseye"],
    description="OpenStreetMap building footprints with heights, for 3D "
                "extrusion on the globe — the other half of free 3D maps, "
                "alongside open elevation tiles. Cached on a snapped bbox grid "
                "because Overpass is shared, slow and rate-limited; an oversized "
                "bbox is REFUSED rather than sent. Inputs: bbox ([south,west,"
                "north,east]!), limit (int). Output: {count, buildings[], "
                "cached, truncated, attribution}.",
)
async def buildings_fetch(bbox: Optional[List[float]] = None,
                          limit: int = _bld.DEFAULT_LIMIT,
                          trace_id=None) -> Dict[str, Any]:
    if not bbox or len(bbox) != 4:
        return {"ok": False, "error": "bbox required as [south, west, north, east]"}
    sane, why = _bld.bbox_is_sane(bbox)
    if not sane:
        return {"ok": False, "error": why}

    box = tuple(float(v) for v in bbox)
    limit = max(1, min(int(limit or _bld.DEFAULT_LIMIT), 5000))
    cache_dir = _layout()["state_dir"] / "buildings"
    cached_path = cache_dir / f"{_bld.bbox_cache_key(box)}.json"

    if cached_path.is_file():
        try:
            doc = json.loads(cached_path.read_text(encoding="utf-8"))
            doc["cached"] = True
            doc["ok"] = True
            return doc
        except (OSError, ValueError):
            pass                                    # fall through and refetch

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            r = await client.post(
                _bld.OVERPASS_URL,
                data={"data": _bld.overpass_query(box)},
                headers={"user-agent": "Vera/Godseye buildings"})
        if r.status_code != 200:
            # 429 here means we are being told to back off, not that the area
            # has no buildings — do not cache it as an empty answer.
            return {"ok": False, "error": f"overpass HTTP {r.status_code}",
                    "retryable": r.status_code in (429, 504)}
        payload = r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}

    raw = payload.get("elements") if isinstance(payload, dict) else None
    buildings = _bld.parse_overpass(payload, limit)
    result = _bld.build_result(
        buildings, bbox=box, generated_at=now_iso(),
        truncated=bool(isinstance(raw, list) and len(raw) > len(buildings)))

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cached_path.with_suffix(".part")
        tmp.write_text(json.dumps(result), encoding="utf-8")
        tmp.replace(cached_path)
    except OSError as e:
        log.warning("godseye: could not cache buildings: %s", e)

    await emit_event({"type": "godseye.buildings.fetched",
                      "count": result["count"], "truncated": result["truncated"]})
    result["ok"] = True
    return result


@APP.get("/godseye/cctv/manifest.json", include_in_schema=False)
async def _godseye_cctv_manifest(video: int = 0, bbox: str = "", limit: int = 0):
    """The camera list, in the shape Godseye's CameraLayer already parses.

    Pointing the fork's VERIFIED_CCTV_MANIFEST constant here is what makes Vera
    the source of truth: adding a provider then needs no fork change and no
    rebuild. Serves the last good manifest and never blocks on the network — a
    slow provider must not stall the globe's camera layer.

    `?video=1` returns only cameras you can actually watch motion on. Most
    agency cameras are stills by design, so this is a large reduction, not a
    cosmetic filter.
    """
    path = _layout()["state_dir"] / "cctv-manifest.json"
    if path.is_file():
        if not video and not bbox and not limit:
            return FileResponse(str(path), media_type="application/json",
                                headers={"Cache-Control": "public, max-age=300"})
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return JSONResponse({"feeds": [], "feedCount": 0,
                                 "errors": {"manifest": str(e)}}, status_code=200)

        feeds = doc.get("feeds") or []
        applied = []
        if video:
            feeds = _cctv.video_only(feeds)
            applied.append("video")
        if bbox:
            # Measured: shipping every camera on earth so the client can draw a
            # city's worth cost seconds of main-thread JSON.parse. Cut it here.
            try:
                box = [float(v) for v in bbox.split(",")]
            except ValueError:
                return JSONResponse({"feeds": [], "feedCount": 0, "errors": {
                    "bbox": "expected south,west,north,east"}}, status_code=400)
            feeds = _cctv.in_bbox(feeds, box)
            applied.append("bbox")
        if limit and limit > 0:
            feeds = feeds[:int(limit)]
            applied.append("limit")

        doc["feeds"] = feeds
        doc["feedCount"] = len(feeds)
        doc["filtered"] = ",".join(applied)
        return JSONResponse(doc, headers={
            # Varies per view, so a shared long cache would serve one user's
            # bbox to another.
            "Cache-Control": "private, max-age=60"})
    return JSONResponse({"generatedAt": "", "feedCount": 0, "feeds": [],
                         "errors": {"manifest": "not built yet — run cctv.refresh"}})


@APP.get(_core.APP_MOUNT, include_in_schema=False)
async def _godseye_app_root():
    # Without the trailing slash a relative asset would resolve against
    # /godseye/, not /godseye/app/.
    return RedirectResponse(_core.APP_BASE, status_code=307)


@APP.get(_core.APP_MOUNT + "/{path:path}", include_in_schema=False)
async def _godseye_app(request: Request, path: str = ""):
    lay = _layout()
    dist = lay["dist_dir"]
    if not (dist / "index.html").is_file():
        return _placeholder(
            "Godseye is not built yet",
            "Its upstream repo is vendored separately from Vera's source. Run "
            "<code>godseye.repo.sync</code> then <code>godseye.build</code> — or "
            "use the buttons in the Godseye tab.")

    target = _core.resolve_asset(dist, path)
    if target is None:
        # Single-page app: unknown non-file routes are the app's own, so hand
        # back index.html. Requests that look like a missing ASSET stay a 404 —
        # answering those with HTML turns a broken build into a blank page.
        if "." in Path(path or "").name:
            return JSONResponse({"error": "not found", "path": path}, status_code=404)
        target = dist / "index.html"

    # Serve the build-time .gz sidecar to clients that asked for gzip. The load
    # path is 17.7 MB raw / 3.5 MB gzipped, and none of it was being compressed.
    send, encoding = _core.negotiate_encoding(target, request.headers.get("accept-encoding", ""))

    headers = {
        "Cache-Control": _core.cache_control_for(target.name),
        # Any cache in front of this must key on the encoding, or a gzip body
        # gets replayed to a client that cannot decode it.
        "Vary": "Accept-Encoding",
        # The bundle is same-origin content Vera serves; keep it from being
        # sniffed into something else.
        "X-Content-Type-Options": "nosniff",
    }
    if encoding:
        headers["Content-Encoding"] = encoding

    # media_type comes from the ORIGINAL name: left to itself FileResponse would
    # read ".gz" and label a JavaScript bundle application/gzip, which the
    # browser downloads instead of executing.
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(str(send), media_type=media_type, headers=headers)


# ═════════════════════════════════════════════════════════════════════════════
#  UI registration
# ═════════════════════════════════════════════════════════════════════════════
register_ui(
    "godseye",
    "Godseye",
    "\U0001F30D",
    html="""<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/godseye/panel" style="flex:1;border:none;width:100%;height:100%;
          background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write; fullscreen"></iframe>
</div>""",
    ui_caps=["godseye.status", "godseye.repo.sync", "godseye.build",
             "godseye.build.status", "godseye.config.get", "godseye.config.set"],
    mode="tab",
    tab_order=59,
)

log.info("godseye_capabilities loaded — godseye.* (vendored globe at %s)",
         _layout()["clone_dir"])
