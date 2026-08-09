# ============================================================================
# build_capabilities.py — Vera-side interface to the vera-builder container
# ============================================================================
#
# The heavy toolchains (arduino-cli + ESP32 core, PlatformIO, gcc/cmake, esptool,
# mpy-cross) live in the `vera-builder` service (vera/build/Dockerfile). Vera
# talks to it over the docker network and never needs a compiler in its own image.
#
#   • build.status     — which toolchains the builder has (and is it reachable)
#   • build.arduino    — compile an Arduino sketch → a flashable, merged .bin that
#                        lands in the mesh firmware catalog (shows in the flasher)
#   • build.platformio — `pio run` for any PlatformIO board/framework
#   • build.run        — run an arbitrary build command (make/cmake/cargo/go/…) in
#                        a sandbox and collect artifacts
#
# Reaches the builder at VERA_BUILDER_URL (default http://vera-builder:8080 for an
# in-stack orchestrator; a native orchestrator sets http://localhost:<BUILDER_PORT>).
# ============================================================================

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from Vera.vera.capability_orchestration import APP, capability
from Vera.vera import state_paths

log = logging.getLogger("vera.build")

# Where a built firmware image is dropped so the mesh panel's flasher can see it.
# This one stays in-tree by design: the flasher UI reads firmware from this exact
# path, and the .bin is gitignored (embedded key) while its .bin.json is tracked —
# a deliberate mixed dir, not a stray machine-output dir.
_MESH_BIN_DIR = Path(__file__).resolve().parent.parent / "mesh" / "firmware" / "bin"
# Generic (non-firmware) build outputs go to the out-of-tree state root, NOT into
# the repo (an in-tree output dir dirties prod's checkout and blocks every
# promote — dev-lifecycle §8.2 #7). The guard asserts that invariant loudly and
# also catches a future edit that points this back inside the tree.
_OUT_DIR = state_paths.build_output_dir()
state_paths.guard_out_of_tree(_OUT_DIR)


# The builder is reachable by two different names depending on how Vera runs:
# in-stack it's the compose service (vera-builder:8080); natively (./build.sh run)
# that DNS name doesn't resolve and the only way in is the published port. An
# explicit VERA_BUILDER_URL always wins; otherwise we probe and remember whichever
# candidate answers /health, so a native orchestrator finds the container too.
_BUILDER_PORT = os.environ.get("BUILDER_PORT", "8785")
_RESOLVED: Dict[str, Any] = {"url": "", "at": 0.0}
_RESOLVE_TTL = 60.0                     # re-probe a failed resolve at most this often


def builder_candidates() -> list:
    explicit = (os.environ.get("VERA_BUILDER_URL") or "").strip().rstrip("/")
    if explicit:
        return [explicit]
    return [f"http://localhost:{_BUILDER_PORT}", f"http://127.0.0.1:{_BUILDER_PORT}",
            "http://vera-builder:8080"]


def builder_url() -> str:
    """The last known-good builder URL (or the first candidate before any probe)."""
    return _RESOLVED["url"] or builder_candidates()[0]


async def resolve_builder_url(force: bool = False) -> str:
    """Probe the candidates and memoise the one that answers. Returns "" when none
    do — callers turn that into the 'start the build service' hint."""
    import time
    import httpx
    if _RESOLVED["url"] and not force:
        return _RESOLVED["url"]
    if not force and (time.time() - _RESOLVED["at"]) < _RESOLVE_TTL:
        return _RESOLVED["url"]
    _RESOLVED["at"] = time.time()
    for cand in builder_candidates():
        try:
            async with httpx.AsyncClient(timeout=2.5) as c:
                r = await c.get(cand + "/health")
            if r.status_code == 200:
                _RESOLVED["url"] = cand
                return cand
        except Exception:
            continue
    _RESOLVED["url"] = ""
    return ""


async def builder_post(path: str, payload: dict, timeout: float = 1200.0) -> dict:
    import httpx
    url = (await resolve_builder_url() or builder_url()) + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=payload)
        if r.status_code != 200:
            return {"ok": False, "error": f"builder {r.status_code}", "body": r.text[:500]}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"builder unreachable at {url}: {e}",
                "tried": builder_candidates(),
                "hint": "run build.builder.up (or docker compose up -d vera-builder) "
                        "or set VERA_BUILDER_URL"}


async def builder_get(path: str, timeout: float = 15.0) -> dict:
    import httpx
    url = (await resolve_builder_url() or builder_url()) + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"builder unreachable at {url}: {e}",
                "tried": builder_candidates()}


def _safe_name(name: str) -> str:
    return os.path.basename(name or "").replace("\\", "").strip() or "artifact.bin"


# ─────────────────────────────────────────────────────────────────────────────
# Job progress registry
# ─────────────────────────────────────────────────────────────────────────────
# Compiles take a minute and a first-time image build takes ten. Run them in the
# background against a progress record the UI can poll, so a long build shows a
# live phase, a percentage and the tail of the log instead of a frozen button.

_JOBS: Dict[str, dict] = {}
_JOB_LOG_MAX = 400                      # keep the tail bounded; builds are chatty


def job_start(kind: str, label: str = "") -> str:
    import time
    import uuid
    jid = f"{kind}-{uuid.uuid4().hex[:8]}"
    _JOBS[jid] = {"id": jid, "kind": kind, "label": label, "phase": "starting",
                  "pct": 0, "log": [], "started": time.time(), "done": False,
                  "ok": None, "error": "", "result": None}
    # Drop old finished jobs so a long-lived process doesn't accumulate them.
    if len(_JOBS) > 40:
        for old in sorted([j for j in _JOBS.values() if j["done"]],
                          key=lambda j: j["started"])[:20]:
            _JOBS.pop(old["id"], None)
    return jid


def job_phase(jid: str, phase: str, pct: Optional[int] = None) -> None:
    j = _JOBS.get(jid)
    if not j:
        return
    j["phase"] = phase
    if pct is not None:
        j["pct"] = max(0, min(100, int(pct)))


def job_log(jid: str, line: str) -> None:
    j = _JOBS.get(jid)
    if not j or not line:
        return
    j["log"].append(line.rstrip()[:500])
    if len(j["log"]) > _JOB_LOG_MAX:
        del j["log"][:len(j["log"]) - _JOB_LOG_MAX]
    # `docker build` prints "Step 7/12" — turn that into a real percentage.
    m = re.match(r"\s*Step (\d+)/(\d+)", line)
    if m:
        cur, tot = int(m.group(1)), int(m.group(2))
        if tot:
            j["pct"] = max(j["pct"], min(99, cur * 100 // tot))


def job_done(jid: str, ok: bool, result=None, error: str = "") -> None:
    import time
    j = _JOBS.get(jid)
    if not j:
        return
    j.update(done=True, ok=bool(ok), result=result, error=error or "",
             phase="done" if ok else "failed", pct=100 if ok else j["pct"],
             finished=time.time())


def job_view(jid: str, tail: int = 40) -> dict:
    import time
    j = _JOBS.get(jid)
    if not j:
        return {"error": "unknown job", "job_id": jid}
    return {"job_id": j["id"], "kind": j["kind"], "label": j["label"],
            "phase": j["phase"], "pct": j["pct"], "done": j["done"], "ok": j["ok"],
            "error": j["error"], "result": j["result"],
            "elapsed_s": round((j.get("finished") or time.time()) - j["started"], 1),
            "log": j["log"][-max(1, int(tail)):]}


async def run_streaming(jid: str, argv: list, timeout: int = 2400) -> int:
    """Run a command, feeding every output line into the job's log as it appears.
    Returns the exit code (or -1 on timeout/failure to start)."""
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except Exception as e:
        job_log(jid, f"could not start {argv[0]}: {e}")
        return -1

    async def _pump():
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            job_log(jid, raw.decode("utf-8", "replace"))

    try:
        await asyncio.wait_for(asyncio.gather(_pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        job_log(jid, f"timed out after {timeout}s — killing")
        try:
            proc.kill()
        except Exception:
            pass
        return -1
    return proc.returncode if proc.returncode is not None else -1


if True:  # capability registration (mirrors the guard style of the mesh modules)

    @capability(
        "build.status", http_method="GET", http_path="/build/status",
        http_tags=["build"], memory="off", silent=True,
        description="Check the vera-builder service and list its toolchains (arduino-cli, platformio, "
                    "gcc/cmake, esptool, mpy-cross, cargo, go). Output: {ok, url, tools, ...}.",
    )
    async def cap_build_status(trace_id=None) -> dict:
        url = await resolve_builder_url(force=True)
        if not url:
            return {"ok": False, "url": "", "tried": builder_candidates(),
                    "error": "no build service reachable",
                    "hint": "run build.builder.up to build+start the vera-builder container"}
        h = await builder_get("/health")
        h["url"] = url
        return h

    @capability(
        "build.builder.up", http_method="POST", http_path="/build/builder/up",
        http_tags=["build"], memory="on",
        description="Bring the vera-builder compile service up on the local Docker host — builds the "
                    "image from vera/build/Dockerfile if it is missing (SLOW the first time: the ESP32 "
                    "Arduino core pulls ~2 GB of toolchains) and starts the container with its port "
                    "published, then waits for /health. Idempotent — a reachable builder returns "
                    "immediately. Runs in the BACKGROUND by default and returns a job_id — poll "
                    "build.progress for the live phase, percentage and log tail (an image build "
                    "streams `Step n/m`). Input: port (int=8785 — host port), rebuild (bool=False — "
                    "force a fresh image build), network (str — extra docker network to join), "
                    "background (bool=True), timeout (int=2400). Output: {ok, job_id} when "
                    "backgrounded, else {ok, url, tools, image, container}.",
    )
    async def cap_build_builder_up(port: int = 0, rebuild: bool = False, network: str = "",
                                   background: bool = True, timeout: int = 2400,
                                   trace_id=None) -> dict:
        import asyncio as _aio

        port = int(port or _BUILDER_PORT)
        if not rebuild:
            url = await resolve_builder_url(force=True)
            if url:
                h = await builder_get("/health")
                return {"ok": True, "already": True, "url": url, "tools": h.get("tools")}

        jid = job_start("builder-up", "start build service")
        if not background:
            return await _builder_up_job(jid, port, rebuild, network, timeout)
        _aio.create_task(_builder_up_job(jid, port, rebuild, network, timeout))
        return {"ok": True, "job_id": jid, "background": True,
                "note": "poll build.progress for phase/pct/log"}

    async def _builder_up_job(jid: str, port: int, rebuild: bool, network: str,
                              timeout: int) -> dict:
        """The actual bring-up, reporting into the job record as it goes."""
        from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
        import asyncio as _aio

        # Positional-only: the capability's own kwargs include `name` (docker.run's
        # container name), which would otherwise collide with this parameter.
        async def _cap(_name: str, /, **kw):
            entry = CAPABILITY_REGISTRY.get(_name)
            if not entry:
                return {"ok": False, "error": f"capability unavailable: {_name}"}
            return await entry["func"](**kw)

        image, container = "vera-builder:latest", "vera-builder"
        ctx = Path(__file__).resolve().parent
        try:
            # 1) Image. Streamed rather than delegated to docker.image.ensure so the
            # ~10-minute first build reports `Step n/m` instead of sitting silent.
            job_phase(jid, "checking image", 2)
            chk = await _aio.create_subprocess_exec(
                "docker", "images", "-q", image,
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.DEVNULL)
            out, _ = await chk.communicate()
            present = bool((out or b"").strip())
            if present and not rebuild:
                job_phase(jid, "image present — skipping build", 55)
                job_log(jid, f"{image} already built; use rebuild=true to refresh it")
            else:
                job_phase(jid, "building image (first run pulls ~2 GB of toolchains)", 3)
                job_log(jid, f"docker build -t {image} {ctx}")
                rc = await run_streaming(jid, ["docker", "build", "-t", image,
                                               "-f", str(ctx / "Dockerfile"), str(ctx)],
                                         timeout=int(timeout))
                if rc != 0:
                    job_done(jid, False, error=f"docker build failed (rc={rc})")
                    return {"ok": False, "stage": "image", "image": image,
                            "error": f"docker build failed (rc={rc})"}
                job_phase(jid, "image built", 60)

            # 2) Container.
            job_phase(jid, "starting container", 70)
            await _cap("docker.rm", host_id="local", container=container, force=True)

            async def _run(cname: str):
                return await _cap("docker.run", host_id="local", image=image, name=cname,
                                  ports=f"{port}:8080", network=network,
                                  volumes="vera-builder-cache:/opt/arduino,"
                                          "vera-builder-pio:/opt/platformio",
                                  env={"BUILDER_DEFAULT_FQBN": os.environ.get(
                                      "BUILDER_DEFAULT_FQBN", "esp32:esp32:esp32")})

            run = await _run(container)
            # An interrupted `docker create` can leave the daemon holding the NAME
            # while the container is un-inspectable and un-removable (only a daemon
            # restart clears it). Don't make the user restart Docker — take a new name.
            if not (isinstance(run, dict) and run.get("ok")) and \
                    "already in use" in str((run or {}).get("error", "")):
                import uuid as _uuid
                container = f"vera-builder-{_uuid.uuid4().hex[:6]}"
                job_log(jid, f"name in use by a stale record — starting as {container}")
                run = await _run(container)
            if not (isinstance(run, dict) and run.get("ok")):
                err = (run or {}).get("error", "docker run failed")
                job_log(jid, err)
                job_done(jid, False, error=err)
                return {"ok": False, "stage": "run", "image": image, "error": err}

            # 3) Health — uvicorn needs a moment after the container starts.
            job_phase(jid, "waiting for the build service to answer", 85)
            for i in range(30):
                await _aio.sleep(2)
                url = await resolve_builder_url(force=True)
                if url:
                    h = await builder_get("/health")
                    res = {"ok": True, "url": url, "image": image, "container": container,
                           "tools": h.get("tools")}
                    job_log(jid, f"build service healthy at {url}")
                    job_done(jid, True, result=res)
                    return res
                if i % 3 == 2:
                    job_log(jid, f"still waiting for :{port}/health…")
            err = f"container started but /health never answered on port {port}"
            job_done(jid, False, error=err)
            return {"ok": False, "stage": "health", "container": container, "error": err,
                    "hint": "check `docker logs vera-builder`"}
        except Exception as e:
            log.warning("builder up job %s: %s", jid, e)
            job_done(jid, False, error=str(e))
            return {"ok": False, "error": str(e)}

    @capability(
        "build.progress", http_method="GET", http_path="/build/progress",
        http_tags=["build"], memory="off", silent=True,
        description="Poll a background build job (from build.builder.up or mesh.firmware.build). "
                    "Input: job_id (str!), tail (int=40 — log lines). Output: {phase, pct, done, ok, "
                    "elapsed_s, log:[...], result, error}.",
    )
    async def cap_build_progress(job_id: str = "", tail: int = 40, trace_id=None) -> dict:
        if not job_id:
            return {"error": "job_id required", "jobs": [j["id"] for j in _JOBS.values()]}
        return job_view(job_id, tail)

    @capability(
        "build.arduino", http_method="POST", http_path="/build/arduino",
        http_tags=["build"], memory="on",
        description="Compile an Arduino sketch in the builder and drop a flashable (merged, 0x0) .bin "
                    "into the mesh firmware catalog so the panel flasher can pick it up. Input: "
                    "source (str — the .ino) OR files (dict {name:content}), main (str='sketch.ino'), "
                    "fqbn (str — e.g. esp32:esp32:esp32s3:CDCOnBoot=default), libraries (list — extra), "
                    "auto_libs (bool=True — auto-install libs from the sketch's #includes), board_urls "
                    "(list — extra board-manager index URLs for third-party cores), build_properties "
                    "(list), name (str — output .bin name). Output: {ok, name, url, size, deps, log}.",
    )
    async def cap_build_arduino(source: str = "", files=None, main: str = "sketch.ino",
                                fqbn: str = "", libraries=None, auto_libs: bool = True,
                                board_urls=None, build_properties=None,
                                name: str = "", trace_id=None) -> dict:
        payload: Dict[str, Any] = {"main": main, "auto_libs": bool(auto_libs)}
        if source:
            payload["source"] = source
        if files:
            payload["files"] = files
        if fqbn:
            payload["fqbn"] = fqbn
        if libraries:
            payload["libraries"] = libraries
        if board_urls:
            payload["board_urls"] = board_urls
        if build_properties:
            payload["build_properties"] = build_properties
        res = await builder_post("/build/arduino", payload)
        if not res.get("ok"):
            return res
        out_name = _safe_name(name or res.get("name") or "arduino.bin")
        try:
            _MESH_BIN_DIR.mkdir(parents=True, exist_ok=True)
            (_MESH_BIN_DIR / out_name).write_bytes(base64.b64decode(res["bin_b64"]))
        except Exception as e:
            return {"ok": False, "error": f"could not save bin: {e}", "log": res.get("log", "")}
        return {"ok": True, "name": out_name, "url": f"/mesh/firmware/bin/{out_name}",
                "chip": res.get("chip"), "merged": res.get("merged"), "size": res.get("size"),
                "deps": res.get("deps"), "log": (res.get("log") or "")[-1500:]}

    @capability(
        "build.platformio", http_method="POST", http_path="/build/platformio",
        http_tags=["build"], memory="on",
        description="Build a PlatformIO project in the builder (any supported board/framework). Input: "
                    "platformio_ini (str) OR files (dict incl. platformio.ini + src/*), environment "
                    "(str — a [env:] name), name (str — output .bin name). Output: {ok, name, url, size, log}.",
    )
    async def cap_build_platformio(platformio_ini: str = "", files=None, environment: str = "",
                                   name: str = "", trace_id=None) -> dict:
        payload: Dict[str, Any] = {}
        if platformio_ini:
            payload["platformio_ini"] = platformio_ini
        if files:
            payload["files"] = files
        if environment:
            payload["environment"] = environment
        res = await builder_post("/build/platformio", payload)
        if not res.get("ok"):
            return res
        out_name = _safe_name(name or res.get("name") or "firmware.bin")
        try:
            _OUT_DIR.mkdir(parents=True, exist_ok=True)
            (_OUT_DIR / out_name).write_bytes(base64.b64decode(res["bin_b64"]))
        except Exception as e:
            return {"ok": False, "error": f"could not save bin: {e}", "log": res.get("log", "")}
        return {"ok": True, "name": out_name, "url": f"/build/output/{out_name}",
                "size": res.get("size"), "log": (res.get("log") or "")[-1500:]}

    @capability(
        "build.run", http_method="POST", http_path="/build/run",
        http_tags=["build"], memory="on",
        description="Run an arbitrary build/compile command in the builder sandbox (make, cmake, gcc, "
                    "cargo, go, tsc, …) and collect artifacts, auto-managing dependencies. Input: "
                    "command (str!), files (dict {path:content}), artifacts (list of globs), apt (list "
                    "— system packages to install), pip (list — Python packages, installed into an "
                    "isolated venv), venv (bool — force a venv even with no pip), env (dict — extra env "
                    "vars), timeout (int). Output: {ok, returncode, stdout, stderr, setup_log, artifacts}.",
    )
    async def cap_build_run(command: str = "", files=None, artifacts=None, apt=None, pip=None,
                            venv: bool = False, env=None, timeout: int = 900, trace_id=None) -> dict:
        if not command:
            return {"error": "command required"}
        res = await builder_post("/build/exec", {
            "command": command, "files": files or {}, "artifacts": artifacts or [],
            "apt": apt or [], "pip": pip or [], "venv": bool(venv), "env": env or {},
            "timeout": int(timeout)}, timeout=float(timeout) + 120)
        saved: Dict[str, str] = {}
        for rel, b64 in (res.get("artifacts") or {}).items():
            try:
                out_name = _safe_name(rel.replace("/", "_"))
                _OUT_DIR.mkdir(parents=True, exist_ok=True)
                (_OUT_DIR / out_name).write_bytes(base64.b64decode(b64))
                saved[rel] = f"/build/output/{out_name}"
            except Exception:
                pass
        return {"ok": res.get("ok", False), "returncode": res.get("returncode"),
                "stdout": (res.get("stdout") or "")[-3000:],
                "stderr": (res.get("stderr") or "")[-3000:],
                "setup_log": (res.get("setup_log") or "")[-2000:],
                "artifacts": saved, "error": res.get("error")}

    @capability(
        "build.python", http_method="POST", http_path="/build/python",
        http_tags=["build"], memory="on",
        description="Run Python in a fresh, isolated virtualenv the builder creates per call — installs "
                    "your deps, runs, then discards the env. Input: files (dict {path:content}), "
                    "requirements (list|str — pip deps; a requirements.txt in files also works), command "
                    "(str='python3 main.py'), artifacts (list of globs), env (dict), timeout (int). "
                    "Output: {ok, returncode, stdout, stderr, setup_log, artifacts:{name:url}}.",
    )
    async def cap_build_python(files=None, requirements=None, command: str = "",
                               artifacts=None, env=None, timeout: int = 900, trace_id=None) -> dict:
        res = await builder_post("/build/python", {
            "files": files or {}, "requirements": requirements or [],
            "command": command or "python3 main.py", "artifacts": artifacts or [],
            "env": env or {}, "timeout": int(timeout)}, timeout=float(timeout) + 120)
        saved: Dict[str, str] = {}
        for rel, b64 in (res.get("artifacts") or {}).items():
            try:
                out_name = _safe_name(rel.replace("/", "_"))
                _OUT_DIR.mkdir(parents=True, exist_ok=True)
                (_OUT_DIR / out_name).write_bytes(base64.b64decode(b64))
                saved[rel] = f"/build/output/{out_name}"
            except Exception:
                pass
        return {"ok": res.get("ok", False), "returncode": res.get("returncode"),
                "stdout": (res.get("stdout") or "")[-3000:],
                "stderr": (res.get("stderr") or "")[-3000:],
                "setup_log": (res.get("setup_log") or "")[-2000:],
                "artifacts": saved, "error": res.get("error")}

    # Serve generic build artifacts (firmware .bins live under the mesh route).
    try:
        from fastapi import Request                       # noqa
        from fastapi.responses import FileResponse, JSONResponse

        @APP.get("/build/output/{name}", include_in_schema=False)
        async def _build_output(name: str):
            safe = _safe_name(name)
            p = _OUT_DIR / safe
            if not p.exists():
                return JSONResponse({"error": "not found"}, status_code=404)
            return FileResponse(str(p), filename=safe)
    except Exception as _e:
        log.debug("build output route not registered: %s", _e)


log.info("build_capabilities ready — builder at %s", builder_url())
