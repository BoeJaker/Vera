"""godseye_core.py — pure logic for the vendored Godseye integration.

App-free on purpose (no orchestrator, no Redis, no FastAPI) so the pieces that
are easy to get *quietly* wrong — the static-asset path guard, the build-log
state machine, the git argv builders, BYOK key handling — are unit-testable
without booting Vera. ``godseye_capabilities`` wires this to caps/routes/UI.

Godseye (https://github.com/VrushankPatel/godseye) is a **separate upstream
repo**, deliberately NOT vendored into Vera's source tree: it is cloned at
runtime into a git-ignored directory and built into static assets that Vera
serves. Nothing under ``vendor/`` is ever tracked by Vera's git.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── Constants ────────────────────────────────────────────────────────────────
UPSTREAM_URL = "https://github.com/VrushankPatel/godseye"

#: URL prefix the built SPA is served under. The Vite build MUST be given the
#: matching ``--base`` or every hashed asset 404s (they are absolute URLs).
APP_MOUNT = "/godseye/app"
APP_BASE = APP_MOUNT + "/"

#: The branch our fork's work lives on. Kept distinct from any upstream branch
#: name so `godseye.repo.sync` can always tell "ours" from "theirs".
WORK_BRANCH = "vera"

#: Node image used for the containerised build — the host needs no toolchain.
#: Vite 7 requires Node >= 20.19, so pin a major that satisfies it.
DEFAULT_NODE_IMAGE = "node:22-alpine"

#: Godseye's "bring your own key" integrations. Every one is optional: absent
#: keys just disable that layer in the UI. Values are inlined into the bundle at
#: BUILD time (that is how Vite `import.meta.env` works), so anyone who can load
#: the built page can read them — they are treated as low-sensitivity API keys,
#: sealed at rest but never claimed to be secret from the browser.
BYOK_KEYS: tuple = (
    "VITE_GOOGLE_MAPS_3D_KEY",
    "VITE_GOOGLE_MAPS_API_KEY",
    "VITE_MAPBOX_ACCESS_TOKEN",
    "VITE_YOUTUBE_API_KEY",
    "VITE_GUARDIAN_API_KEY",
    "VITE_AISSTREAM_API_KEY",
    "VITE_FIREBASE_RTDB_URL",
    "VITE_GODSEYE_CACHE_SECRET",
)

#: Marker the build script appends as its last line. Its presence is the ONLY
#: reliable "the build finished" signal — the container is detached, so an
#: absent marker means "still running OR died hard", never "succeeded".
EXIT_MARKER = "GODSEYE_EXIT="

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_STAGE_LINE = re.compile(r"^\[godseye\] --- (.+?) ---\s*$")


# ── Layout ───────────────────────────────────────────────────────────────────
def repo_root(module_file: str) -> Path:
    """Vera's repo root, derived from a file inside ``vera/godseye/``."""
    return Path(module_file).resolve().parent.parent.parent


def resolve_layout(root: Path, env: Optional[Dict[str, str]] = None) -> Dict[str, Path]:
    """Where the vendored clone, its build state and its built assets live.

    ``VERA_GODSEYE_DIR`` overrides the clone directory outright (so an operator
    can park it on a bigger disk); everything else hangs off it. The default
    keeps the clone inside the repo working directory but under ``vendor/``,
    which Vera's .gitignore excludes — see ``clone_dir_is_ignored``.
    """
    env = env or {}
    override = (env.get("VERA_GODSEYE_DIR") or "").strip()
    clone = Path(override).expanduser() if override else Path(root) / "vendor" / "godseye"
    clone = clone if clone.is_absolute() else (Path(root) / clone)
    state = clone.parent / ".godseye"

    # The fork lives OUTSIDE the vendor directory on purpose. Everything under
    # vendor/ is disposable — git-ignored, rebuildable, and deleted whenever the
    # clone is reset — so a fork kept in there would take our own commits with
    # it. ``VERA_GODSEYE_FORK`` can point this at any git remote (a Gitea repo,
    # say) without a code change.
    fork_override = (env.get("VERA_GODSEYE_FORK") or "").strip()
    fork = Path(fork_override).expanduser() if fork_override else \
        Path(env.get("HOME") or Path.home()) / "godseye-fork.git"

    return {
        "vendor_dir": clone.parent,
        "clone_dir": clone,
        "state_dir": state,
        "log_path": state / "build.log",
        "dist_dir": clone / "dist",
        "env_file": clone / ".env.local",
        "fork_dir": fork,
        "tile_dir": state / "tiles",
    }


# ── Static asset serving ─────────────────────────────────────────────────────
def gitignore_covers(rel_dir: str, gitignore_text: str) -> bool:
    """Does this .gitignore exclude ``rel_dir`` (a path relative to the repo)?

    A deliberately conservative fallback for when `git check-ignore` cannot be
    consulted — Vera also runs from containers with no ``.git`` at all, and
    "couldn't ask git" must not silently become "go ahead and clone into the
    tracked tree". Only recognises a literal rule for the directory or one of
    its ancestors; anything cleverer (globs, negations) reads as NOT covered,
    which fails closed.
    """
    parts = [p for p in str(rel_dir or "").replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    prefixes = ["/".join(parts[:i + 1]) for i in range(len(parts))]
    wanted = set()
    for p in prefixes:
        wanted |= {p, f"/{p}", f"{p}/", f"/{p}/"}
    hit = False
    for raw in (gitignore_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            # A re-include anywhere in the file means this fallback cannot be
            # sure, and "not sure" must mean "no".
            if line[1:].strip() in wanted:
                return False
            continue
        if line in wanted:
            hit = True
    return hit


def resolve_asset(dist_dir: Path, rel: str) -> Optional[Path]:
    """Map a request path under ``/godseye/app/`` to a file inside ``dist``.

    Returns None when the path escapes ``dist`` or is not an existing file.
    Traversal is rejected on the RESOLVED path rather than by pattern-matching
    the raw string, so encoded/duplicated separators and symlinks inside dist
    cannot walk out of the served root.
    """
    rel = (rel or "").lstrip("/")
    if not rel:
        rel = "index.html"
    if "\x00" in rel:
        return None
    try:
        base = Path(dist_dir).resolve()
        target = (base / rel).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if target != base and base not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


# ── map tile cache ───────────────────────────────────────────────────────────
#: Godseye asks the browser to pull Esri World Imagery tiles straight from
#: arcgisonline on every pan and zoom, uncached. Vera proxies them instead so a
#: tile is fetched from the internet once and served from local disk after that.
TILE_UPSTREAM = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                 "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TILE_MOUNT = "/godseye/tiles"

#: The globe stacks THREE imagery layers, not one — satellite base plus two
#: semi-transparent Esri label overlays. Proxying only the base left two thirds
#: of the tile traffic still going straight to the internet on every pan, which
#: is why caching the base alone barely moved the needle.
#:
#: An allowlist, not a pass-through: the layer name selects one of these fixed
#: templates, so no caller can point this at an arbitrary host.
#: Free 3D terrain. Terrarium PNGs encode elevation as RGB
#: (height = R*256 + G + B/256 - 32768), which Cesium cannot consume directly —
#: it wants quantized-mesh. The fork decodes these into a HeightmapTerrainData,
#: which Cesium DOES accept natively, so no server-side mesh conversion is
#: needed. Open data, no key, which is why it is the default rather than
#: Cesium Ion or Google.
TERRAIN_UPSTREAM = ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/"
                    "{z}/{x}/{y}.png")

TILE_LAYERS = {
    "imagery": TILE_UPSTREAM,
    # Note the {x}/{y} order differs from the imagery layers' {y}/{x}; the
    # template is per-layer precisely so providers can disagree about this.
    "terrain": TERRAIN_UPSTREAM,
    "reference": ("https://services.arcgisonline.com/ArcGIS/rest/services/"
                  "Reference/World_Reference_Overlay/MapServer/tile/{z}/{y}/{x}"),
    "places": ("https://services.arcgisonline.com/ArcGIS/rest/services/"
               "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"),
}

#: Per-layer zoom ceilings, mirroring the providers' own maximumLevel. Asking
#: beyond them only produces upstream 404s.
TILE_LAYER_MAX_ZOOM = {"imagery": 19, "reference": 8, "places": 15, "terrain": 15}

#: Only the satellite base is JPEG. The label overlays need alpha and the
#: elevation tiles carry data in their exact RGB values, so both are PNG —
#: and because these responses are served with X-Content-Type-Options: nosniff,
#: mislabelling them is not something the browser will quietly correct.
TILE_LAYER_MEDIA = {"imagery": "image/jpeg", "reference": "image/png",
                    "places": "image/png", "terrain": "image/png"}


def tile_layer_is_valid(layer: str) -> bool:
    return layer in TILE_LAYERS


def tile_media_type(layer: str) -> str:
    return TILE_LAYER_MEDIA.get(layer, "image/jpeg")


def tile_extension(layer: str) -> str:
    return ".png" if tile_media_type(layer) == "image/png" else ".jpg"

#: Esri's World Imagery tops out at 19. Anything beyond is a client bug or an
#: attempt to make us issue unbounded upstream requests.
TILE_MAX_ZOOM = 19


def tile_is_valid(z: int, y: int, x: int, layer: str = "imagery") -> bool:
    """Is this a real tile coordinate for this layer?

    The z/y/x go into an upstream URL and into a cache path, so they are
    validated as integers in range rather than interpolated as text — that is
    what keeps this from being both a path-traversal sink and an open proxy.
    """
    if not tile_layer_is_valid(layer):
        return False
    try:
        z, y, x = int(z), int(y), int(x)
    except (TypeError, ValueError):
        return False
    if z < 0 or z > TILE_LAYER_MAX_ZOOM.get(layer, TILE_MAX_ZOOM):
        return False
    limit = 1 << z                       # a zoom level is a 2^z square of tiles
    return 0 <= y < limit and 0 <= x < limit


def tile_cache_path(cache_root: Path, z: int, y: int, x: int,
                    layer: str = "imagery") -> Path:
    """Where a tile is cached. Only ever called with a validated layer name and
    coordinates, and built from ints so no caller-supplied string reaches the
    path."""
    if not tile_layer_is_valid(layer):
        raise ValueError(f"unknown tile layer: {layer!r}")
    return (Path(cache_root) / layer / str(int(z)) / str(int(y))
            / f"{int(x)}{tile_extension(layer)}")


def tile_upstream_url(z: int, y: int, x: int, template: str = "",
                      layer: str = "imagery") -> str:
    if not template and not tile_layer_is_valid(layer):
        raise ValueError(f"unknown tile layer: {layer!r}")
    tpl = template or TILE_LAYERS[layer]
    return tpl.format(z=int(z), y=int(y), x=int(x))


def negotiate_encoding(target: Path, accept_encoding: str) -> tuple:
    """Pick the precompressed sidecar when — and only when — the client asked.

    ``godseye_build.sh`` gzips the bundle at build time, so serving is a plain
    file read with a header rather than per-request compression. Returns
    ``(path_to_send, content_encoding)``; ``content_encoding`` is "" when the
    original file is being sent, which is what a client that did not offer gzip
    must always get.
    """
    accept = (accept_encoding or "").lower()
    if not any(tok.strip().split(";")[0] == "gzip" for tok in accept.split(",")):
        return (target, "")
    gz = Path(str(target) + ".gz")
    try:
        if gz.is_file():
            return (gz, "gzip")
    except OSError:
        pass
    return (target, "")


def cache_control_for(path: str) -> str:
    """Vite emits content-hashed files under ``assets/`` — only THOSE are safe to
    pin forever, because a rebuild changes their names.

    Everything else keeps its name across rebuilds: ``cesium/Cesium.js`` is
    still ``cesium/Cesium.js`` after a Godseye upgrade, so marking it immutable
    would serve a year-stale runtime to anyone who had loaded the old one. Those
    get a short TTL instead — long enough to skip a revalidation storm across
    the hundreds of Cesium worker/asset files, short enough that an upgrade
    lands the same day.

    ``index.html`` is never cached: it is the pointer to the hashed assets, so a
    stale copy is how you get a page wired to files that no longer exist.
    """
    p = str(path).replace("\\", "/")
    if p.endswith(".gz"):                       # judge by what was REQUESTED
        p = p[:-3]
    name = p.rsplit("/", 1)[-1]
    if name in ("index.html", "") or name.endswith(".html"):
        return "no-store"
    if "/assets/" in p or p.startswith("assets/"):
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600, must-revalidate"


# ── Build log ────────────────────────────────────────────────────────────────
def parse_build_log(text: str, tail: int = 40) -> Dict[str, Any]:
    """Turn the raw build log into state. Never guesses from elapsed time."""
    lines = [ln for ln in (text or "").splitlines()]
    exit_code: Optional[int] = None
    stage = ""
    for ln in lines:
        m = _STAGE_LINE.match(ln)
        if m:
            stage = m.group(1)
        if ln.startswith(EXIT_MARKER):
            rest = ln[len(EXIT_MARKER):].strip().split()
            try:
                exit_code = int(rest[0]) if rest else None
            except ValueError:
                exit_code = None
    finished = exit_code is not None
    return {
        "started": bool(lines),
        "finished": finished,
        "running": bool(lines) and not finished,
        "ok": finished and exit_code == 0,
        "exit_code": exit_code,
        "stage": stage,
        "lines": len(lines),
        "tail": lines[-max(0, int(tail)):] if tail else [],
    }


# ── git argv builders ────────────────────────────────────────────────────────
def is_safe_repo_url(url: str) -> bool:
    """Only plain http(s) URLs. Rejects anything that git would read as an
    OPTION (a leading '-'), plus ssh/file/ext transports — `git clone` accepts
    `ext::sh -c ...` as a remote, which is remote code execution by URL."""
    u = (url or "").strip()
    if not u or u.startswith("-") or any(c.isspace() for c in u):
        return False
    return u.startswith("http://") or u.startswith("https://")


def allowed_repo_url(url: str, env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Is this repository allowed to be cloned, built and SERVED by Vera?

    ``godseye.repo.sync`` is reachable by any caller a capability is reachable
    by, including agent loops. Whatever it clones gets built and served as
    JavaScript from Vera's own origin, and its npm ``postinstall`` scripts run
    during the build — so an arbitrary URL here is arbitrary code execution plus
    same-origin script injection. The URL is therefore PINNED to the vendored
    upstream unless an operator deliberately points ``VERA_GODSEYE_REPO_URL``
    somewhere else (e.g. an internal mirror). Env is operator-controlled;
    capability arguments are not.
    """
    u = (url or "").strip()
    if not is_safe_repo_url(u):
        return False, f"unsafe repo url: {u!r} (http(s) only)"
    allowed = {UPSTREAM_URL, UPSTREAM_URL + ".git"}
    override = ((env or {}).get("VERA_GODSEYE_REPO_URL") or "").strip()
    if override:
        allowed |= {override, override.rstrip("/")}
    if u.rstrip("/") in {a.rstrip("/") for a in allowed}:
        return True, ""
    return False, (
        f"{u} is not the vendored Godseye upstream. Vera builds and serves this "
        "repo's JavaScript on its own origin, so the source is pinned to "
        f"{UPSTREAM_URL}; set VERA_GODSEYE_REPO_URL to allow a mirror.")


def is_safe_ref(ref: str) -> bool:
    """Conservative branch/tag/sha filter — no option injection, no `..`."""
    r = (ref or "").strip()
    if not r or ".." in r or r.endswith(".lock") or r.endswith("/"):
        return False
    return bool(_SAFE_REF.match(r))


def read_repo_meta(state_dir: Path, clone_dir: Path) -> Dict[str, Any]:
    """Describe the vendored clone WITHOUT running git on the host.

    ``godseye_sync.sh`` captures the commit metadata inside the container that
    actually has git and writes it here, so nothing on the host needs a git
    binary — or the right git configuration — to answer godseye.status.

    Falls back to reading ``.git/HEAD`` directly (plus loose/packed refs) so a
    clone made by hand, or one predating the metadata file, still reports a sha.
    """
    meta_path = Path(state_dir) / "repo.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("sha"):
            return data
    except (OSError, ValueError):
        pass

    sha = _head_sha(Path(clone_dir))
    return {"sha": sha, "short": sha[:7], "committed_at": "", "subject": "",
            "source": "git-head"} if sha else {}


def _head_sha(clone: Path) -> str:
    """Resolve .git/HEAD to a sha — detached (a raw sha) or symbolic."""
    git_dir = clone / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head if _SHA_RE.match(head) else ""
    ref = head[4:].strip()
    try:
        return (git_dir / ref).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:                                        # packed-refs, for a fresh clone
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        pass
    return ""


# ── BYOK config ──────────────────────────────────────────────────────────────
def clean_env_value(value: str) -> str:
    """Strip anything that could end an assignment early.

    Quoting alone is NOT enough here: dotenv accepts multi-line quoted values,
    so a key containing a newline still lands as a second `KEY=…` line in the
    generated file for anyone reading it (and a lone stray quote can swallow the
    following lines). No real API key contains a control character, so the
    honest fix is to drop them rather than trust the quoting.
    """
    return "".join(ch for ch in str(value or "") if ch.isprintable()).strip()


def env_file_text(config: Dict[str, str]) -> str:
    """Render ``.env.local`` for the Vite build from a config mapping.

    Only recognised VITE_ keys are emitted, each stripped of control characters
    and then shell-quoted, so no value can inject a second assignment.
    """
    out = ["# Generated by Vera (godseye.config.set) — do not edit by hand.",
           "# Values are inlined into the built bundle by Vite at build time."]
    for key in BYOK_KEYS:
        val = clean_env_value((config or {}).get(key, ""))
        if not val:
            continue
        out.append(f"{key}={shlex.quote(val)}")
    return "\n".join(out) + "\n"


def redact_config(config: Dict[str, str]) -> Dict[str, str]:
    """Never hand raw keys back over the API — show only enough to recognise."""
    out: Dict[str, str] = {}
    for key in BYOK_KEYS:
        val = str((config or {}).get(key, "") or "")
        if not val:
            out[key] = ""
        elif len(val) <= 8:
            out[key] = "*" * len(val)
        else:
            out[key] = f"{val[:3]}{'*' * 6}{val[-2:]}"
    return out


def known_config(config: Dict[str, str]) -> Dict[str, str]:
    """Drop anything that is not a recognised Godseye BYOK key."""
    return {k: str(v or "") for k, v in (config or {}).items() if k in BYOK_KEYS}


# ── docker argv ──────────────────────────────────────────────────────────────
def docker_sync_argv(*, vendor_dir: Path, script_path: Path, clone_name: str,
                     url: str, ref: str = "", depth: int = 0,
                     image: str = DEFAULT_NODE_IMAGE, uid: int = 0, gid: int = 0,
                     fork_dir: Optional[Path] = None,
                     work_branch: str = WORK_BRANCH) -> List[str]:
    """`docker run` argv for the clone/update, which happens in a container too.

    Not because the host cannot run git, but because it should not: the host's
    git config rewrites GitHub HTTPS urls to SSH (see godseye_sync.sh), and a
    container simply has no operator config to inherit.

    Attached and ``--rm``, unlike the build: a shallow clone finishes in seconds,
    so the caller can just wait for it and report the result, and there is no
    container left behind to name-clash with the next run.

    Runs as ROOT — the node image has no git and installing it needs root — and
    the script chowns the result to uid/gid so the unprivileged build can write
    node_modules/dist into it afterwards.
    """
    env = {
        "GODSEYE_SRC": f"/work/{clone_name}",
        "GODSEYE_URL": url,
        "GODSEYE_REF": ref or "",
        "GODSEYE_DEPTH": str(max(0, int(depth))),
        "GODSEYE_META": "/work/.godseye/repo.json",
        "GODSEYE_UID": str(int(uid)),
        "GODSEYE_GID": str(int(gid)),
        "GODSEYE_BRANCH": work_branch or WORK_BRANCH,
        "GODSEYE_FORK": "/fork" if fork_dir else "",
        "HOME": "/tmp",
    }
    argv = ["docker", "run", "--rm",
            "-v", f"{vendor_dir}:/work",
            "-v", f"{script_path}:/opt/godseye_sync.sh:ro"]
    if fork_dir:
        # Mounted at a fixed path so the container never has to know where on
        # the host the fork lives.
        argv += ["-v", f"{fork_dir}:/fork"]
    for k, v in env.items():
        argv += ["-e", f"{k}={v}"]
    argv += [image, "sh", "/opt/godseye_sync.sh"]
    return argv


def docker_build_argv(*, vendor_dir: Path, script_path: Path, clone_name: str,
                      image: str = DEFAULT_NODE_IMAGE, container: str = "godseye-build",
                      uid: int = 0, gid: int = 0, base: str = APP_BASE,
                      refresh_manifests: bool = False,
                      clean_install: bool = False) -> List[str]:
    """`docker run` argv for a one-shot containerised production build.

    Detached on purpose: the build outlives any single HTTP request, and its
    only progress channel is the log file inside the bind mount, which the
    caller can read while it is still running.

    Runs as the HOST's uid/gid so ``node_modules``/``dist`` do not come back
    root-owned — the 2026-08-28 first attempt did exactly that and then could
    not write its own log on the next run.
    """
    env = {
        "GODSEYE_SRC": f"/work/{clone_name}",
        "GODSEYE_LOG": "/work/.godseye/build.log",
        "GODSEYE_BASE": base or APP_BASE,
        "GODSEYE_REFRESH": "1" if refresh_manifests else "0",
        "GODSEYE_CLEAN_INSTALL": "1" if clean_install else "0",
        "HOME": "/tmp",
        "npm_config_cache": "/tmp/.npm",
    }
    argv = ["docker", "run", "-d", "--name", container,
            "--user", f"{int(uid)}:{int(gid)}",
            "-v", f"{vendor_dir}:/work",
            "-v", f"{script_path}:/opt/godseye_build.sh:ro"]
    for k, v in env.items():
        argv += ["-e", f"{k}={v}"]
    argv += [image, "sh", "/opt/godseye_build.sh"]
    return argv


def summarize_dist(files: Sequence[Path], dist_dir: Path) -> Dict[str, Any]:
    """Compact description of a built bundle for `godseye.status`."""
    total = 0
    newest = 0.0
    count = 0
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        count += 1
        total += st.st_size
        newest = max(newest, st.st_mtime)
    return {
        "built": count > 0 and (Path(dist_dir) / "index.html").is_file(),
        "files": count,
        "bytes": total,
        "mtime": newest or None,
    }
