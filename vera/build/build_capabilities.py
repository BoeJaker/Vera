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
from pathlib import Path
from typing import Any, Dict, Optional

from Vera.vera.capability_orchestration import APP, capability

log = logging.getLogger("vera.build")

# Where a built firmware image is dropped so the mesh panel's flasher can see it.
_MESH_BIN_DIR = Path(__file__).resolve().parent.parent / "mesh" / "firmware" / "bin"
# Generic (non-firmware) build outputs.
_OUT_DIR = Path(__file__).resolve().parent / "output"


def builder_url() -> str:
    return os.environ.get("VERA_BUILDER_URL", "http://vera-builder:8080").rstrip("/")


async def builder_post(path: str, payload: dict, timeout: float = 1200.0) -> dict:
    import httpx
    url = builder_url() + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=payload)
        if r.status_code != 200:
            return {"ok": False, "error": f"builder {r.status_code}", "body": r.text[:500]}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"builder unreachable at {url}: {e}",
                "hint": "start the vera-builder service (docker compose up -d vera-builder) "
                        "or set VERA_BUILDER_URL"}


async def builder_get(path: str, timeout: float = 15.0) -> dict:
    import httpx
    url = builder_url() + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"builder unreachable at {url}: {e}"}


def _safe_name(name: str) -> str:
    return os.path.basename(name or "").replace("\\", "").strip() or "artifact.bin"


if True:  # capability registration (mirrors the guard style of the mesh modules)

    @capability(
        "build.status", http_method="GET", http_path="/build/status",
        http_tags=["build"], memory="off", silent=True,
        description="Check the vera-builder service and list its toolchains (arduino-cli, platformio, "
                    "gcc/cmake, esptool, mpy-cross, cargo, go). Output: {ok, url, tools, ...}.",
    )
    async def cap_build_status(trace_id=None) -> dict:
        h = await builder_get("/health")
        h["url"] = builder_url()
        return h

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
