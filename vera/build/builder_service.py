#!/usr/bin/env python3
# ============================================================================
# builder_service.py — Vera Builder: a general compilation/build microservice
# ============================================================================
#
# Runs INSIDE the vera-builder container (see Dockerfile) and exposes a small
# HTTP API that Vera calls over the docker network (vera-net). It turns source
# files into artifacts using whatever toolchain fits:
#
#   GET  /health                — what toolchains are available
#   POST /build/arduino         — arduino-cli compile → merged, flash-at-0x0 .bin
#   POST /build/platformio      — `pio run` for any PlatformIO board/framework
#   POST /build/exec            — run an arbitrary build command in a sandbox
#                                 (make/cmake/gcc/cargo/go/…) and return artifacts
#
# Request/response are JSON. Source is passed inline as {files:{path:content}}
# (text) so no shared volume is needed; artifacts come back base64-encoded.
#
# SECURITY: /build/exec runs arbitrary commands. This service is meant to sit on
# the internal vera-net (and optionally a localhost-published port for a native
# orchestrator) — do NOT expose it to untrusted networks. It's a build runner,
# equivalent to a self-hosted CI worker.
# ============================================================================

from __future__ import annotations

import base64
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Vera Builder", version="1.0")

DEFAULT_ARDUINO_FQBN = os.environ.get("BUILDER_DEFAULT_FQBN", "esp32:esp32:esp32")
MAX_LOG = 6000


# ── helpers ──────────────────────────────────────────────────────────────────

def _which(name: str) -> bool:
    return bool(shutil.which(name))


def _tool_version(cmd: List[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else ""
    except Exception:
        return ""


def _safe_join(root: str, rel: str) -> str:
    """Join rel under root, refusing path traversal (rel may not escape root)."""
    rel = (rel or "").lstrip("/\\")
    dest = os.path.normpath(os.path.join(root, rel))
    if not (dest == root or dest.startswith(root + os.sep)):
        raise ValueError(f"unsafe path: {rel}")
    return dest


def _write_files(root: str, files: Dict[str, Any]) -> None:
    for name, content in (files or {}).items():
        dest = _safe_join(root, name)
        os.makedirs(os.path.dirname(dest) or root, exist_ok=True)
        if isinstance(content, dict) and content.get("b64"):
            with open(dest, "wb") as f:
                f.write(base64.b64decode(content["b64"]))
        else:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else str(content))


def _run(cmd, cwd: str, timeout: int = 900, shell: bool = False, env: dict = None):
    e = None
    if env:
        e = dict(os.environ)
        e.update({k: str(v) for k, v in env.items()})
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, shell=shell, env=e)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _b64_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _chip_from_fqbn(fqbn: str) -> str:
    # esp32:esp32:esp32s3:opts → board segment "esp32s3" is also the esptool chip
    try:
        board = fqbn.split(":")[2].split(":")[0]
    except Exception:
        return "esp32"
    board = board.lower()
    for chip in ("esp32s3", "esp32s2", "esp32c3", "esp32c6", "esp32h2", "esp32"):
        if board.startswith(chip):
            return chip
    return "esp32"


def _find_bin(outdir: str, suffix: str, exclude: Optional[List[str]] = None) -> Optional[str]:
    exclude = exclude or []
    for f in sorted(glob.glob(os.path.join(outdir, "**", "*" + suffix), recursive=True)):
        base = os.path.basename(f)
        if any(base.endswith(x) for x in exclude):
            continue
        return f
    return None


def _merge_esp(outdir: str, fqbn: str, flash_size: str = "4MB") -> Optional[str]:
    """Combine the arduino-cli bootloader + partitions + app into ONE image that
    the panel can flash in a single write at 0x0 (works on a blank chip)."""
    boot = _find_bin(outdir, ".bootloader.bin")
    part = _find_bin(outdir, ".partitions.bin")
    app = _find_bin(outdir, ".bin", exclude=[".bootloader.bin", ".partitions.bin",
                                             ".merged.bin", "merged.bin"])
    if not (boot and part and app):
        return None
    chip = _chip_from_fqbn(fqbn)
    boot_off = "0x1000" if chip == "esp32" else "0x0"
    merged = os.path.join(outdir, "merged.bin")
    esptool = "esptool.py" if _which("esptool.py") else "esptool"
    # Omit --flash_mode/--flash_freq so merge_bin keeps the bootloader's own header
    # (those flags aren't accepted across all esptool versions for merge_bin).
    rc, so, se = _run([esptool, "--chip", chip, "merge_bin", "-o", merged,
                       "--flash_size", flash_size,
                       boot_off, boot, "0x8000", part, "0x10000", app], outdir, timeout=180)
    return merged if rc == 0 and os.path.exists(merged) else None


# ── Arduino dependency auto-management ───────────────────────────────────────
# Headers that ship with a core (or are C/C++ std) — never try to install a lib.
_ARDUINO_BUNDLED = {
    "arduino.h", "wifi.h", "wificlient.h", "wificlientsecure.h", "wifiudp.h", "wifiserver.h",
    "esp8266wifi.h", "esp8266httpclient.h", "httpclient.h", "httpupdate.h", "wifimulti.h",
    "espmdns.h", "update.h", "webserver.h", "dnsserver.h", "wire.h", "spi.h", "sd.h",
    "sd_mmc.h", "fs.h", "spiffs.h", "littlefs.h", "preferences.h", "eeprom.h", "ticker.h",
    "esp_wifi.h", "esp_sleep.h", "esp_now.h", "esp_system.h", "esp_task_wdt.h", "esp_camera.h",
    "bledevice.h", "blescan.h", "bleutils.h", "bleserver.h", "bleadvertiseddevice.h",
    "math.h", "stdint.h", "string.h", "stdlib.h", "stdio.h", "stdbool.h", "time.h", "ctype.h",
}
# Header → Library-Manager name. ArduinoJson is pinned to v6 (the sketch uses the
# v6 API that v7 deprecates/removes); others take the latest.
_HEADER_LIB = {
    "arduinojson.h": "ArduinoJson@6.21.5",
    "pubsubclient.h": "PubSubClient",
    "arduinomqttclient.h": "ArduinoMqttClient",
    "adafruit_gfx.h": "Adafruit GFX Library",
    "adafruit_ssd1306.h": "Adafruit SSD1306",
    "adafruit_neopixel.h": "Adafruit NeoPixel",
    "adafruit_sensor.h": "Adafruit Unified Sensor",
    "fastled.h": "FastLED",
    "tft_espi.h": "TFT_eSPI",
    "u8g2lib.h": "U8g2",
    "dht.h": "DHT sensor library",
    "onewire.h": "OneWire",
    "dallastemperature.h": "DallasTemperature",
    "websockets.h": "WebSockets",
    "asynctcp.h": "AsyncTCP",
    "espasyncwebserver.h": "ESPAsyncWebServer",
    "mfrc522.h": "MFRC522",
    "servo.h": "Servo",
    "irremote.h": "IRremote",
    "ntpclient.h": "NTPClient",
}


def _scan_includes(source: str) -> List[str]:
    return re.findall(r'(?m)^\s*#\s*include\s*[<"]([^>"]+)[>"]', source or "")


def _arduino_core_installed(platform: str) -> bool:
    rc, so, _ = _run(["arduino-cli", "core", "list"], "/", timeout=60)
    if rc != 0:
        return False
    return any(line.split()[:1] == [platform] for line in so.splitlines()[1:])


def _ensure_arduino_core(fqbn: str, board_urls=None, log: list = None) -> None:
    """Install the board platform for this FQBN if it isn't present (e.g. esp32:esp32,
    arduino:avr, rp2040:rp2040, STMicroelectronics:stm32). board_urls adds package
    index URLs for third-party cores before install."""
    platform = ":".join(fqbn.split(":")[:2])
    if not platform or _arduino_core_installed(platform):
        return
    for u in (board_urls or []):
        _run(["arduino-cli", "config", "add", "board_manager.additional_urls", u], "/", timeout=60)
    _run(["arduino-cli", "core", "update-index"], "/", timeout=600)
    rc, so, se = _run(["arduino-cli", "core", "install", platform], "/", timeout=2400)
    if log is not None:
        log.append(f"[core {platform}] rc={rc} " + (so + se).strip()[-400:])


def _lib_for_header(header: str) -> Optional[str]:
    key = header.lower()
    if key in _HEADER_LIB:
        return _HEADER_LIB[key]
    # Ask the index which library provides this header.
    q = header[:-2] if header.endswith(".h") else header
    rc, so, _ = _run(["arduino-cli", "lib", "search", q, "--format", "json"], "/", timeout=120)
    if rc != 0:
        return None
    try:
        data = json.loads(so or "{}")
        libs = data.get("libraries") or data.get("Libraries") or []
        for lib in libs:
            provides = (lib.get("provides_includes")
                        or (lib.get("latest") or {}).get("provides_includes") or [])
            if header in provides:
                return lib.get("name") or (lib.get("latest") or {}).get("name")
    except Exception:
        pass
    return None


def _ensure_arduino_libs(source: str, log: list = None) -> List[dict]:
    """Scan #includes and install any non-bundled libraries the sketch needs."""
    installed: List[dict] = []
    for header in dict.fromkeys(_scan_includes(source)):
        base = os.path.basename(header)
        if "/" in header or not base.endswith(".h") or base.lower() in _ARDUINO_BUNDLED:
            continue
        libname = _lib_for_header(base)
        if not libname:
            continue
        rc, so, se = _run(["arduino-cli", "lib", "install", libname], "/", timeout=600)
        installed.append({"header": base, "library": libname, "ok": rc == 0})
        if log is not None and rc != 0:
            log.append(f"[lib {libname}] rc={rc} " + (so + se).strip()[-300:])
    return installed


# ── endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "vera-builder",
        "tools": {
            "arduino_cli": _which("arduino-cli"),
            "platformio": _which("pio") or _which("platformio"),
            "esptool": _which("esptool.py") or _which("esptool"),
            "gcc": _which("gcc"),
            "g++": _which("g++"),
            "make": _which("make"),
            "cmake": _which("cmake"),
            "ninja": _which("ninja"),
            "mpy_cross": _which("mpy-cross"),
            "cargo": _which("cargo"),
            "go": _which("go"),
            "git": _which("git"),
        },
        "arduino_cores": _tool_version(["arduino-cli", "core", "list"]) if _which("arduino-cli") else "",
        "arduino_libs": (_run(["arduino-cli", "lib", "list"], "/", timeout=30)[1].strip()
                         if _which("arduino-cli") else ""),
        "default_fqbn": DEFAULT_ARDUINO_FQBN,
        "auto_deps": True,
    }


@app.post("/build/arduino")
def build_arduino(req: Dict[str, Any]):
    if not _which("arduino-cli"):
        return JSONResponse({"ok": False, "error": "arduino-cli not installed in builder"}, status_code=200)
    files = dict(req.get("files") or {})
    main = req.get("main") or ""
    if req.get("source"):
        main = main or "sketch.ino"
        files[main] = req["source"]
    if not main:
        main = next((n for n in files if n.endswith(".ino")), "")
    if not main.endswith(".ino"):
        return {"ok": False, "error": "no .ino main file (pass main=<name>.ino or source=...)"}
    fqbn = req.get("fqbn") or DEFAULT_ARDUINO_FQBN
    sketch_name = os.path.splitext(os.path.basename(main))[0]

    with tempfile.TemporaryDirectory() as td:
        sdir = os.path.join(td, sketch_name)
        os.makedirs(sdir, exist_ok=True)
        # arduino-cli needs the primary .ino to match the folder name.
        for name, content in files.items():
            target = f"{sketch_name}.ino" if name == main else os.path.basename(name)
            _write_files(sdir, {target: content})
        # optional extra libraries (names installable from the index, or dirs)
        # Auto-manage dependencies: install the core for this FQBN if missing, and
        # resolve libraries from the sketch's #includes (+ any explicit `libraries`).
        dep_log: List[str] = []
        _ensure_arduino_core(fqbn, req.get("board_urls"), dep_log)
        all_src = "\n".join(c for c in files.values() if isinstance(c, str))
        deps = _ensure_arduino_libs(all_src, dep_log) if req.get("auto_libs", True) else []
        for lib in req.get("libraries") or []:
            rc0, so0, se0 = _run(["arduino-cli", "lib", "install", lib], td, timeout=600)
            deps.append({"library": lib, "ok": rc0 == 0})
        outdir = os.path.join(td, "out")
        cmd = ["arduino-cli", "compile", "--fqbn", fqbn, "--output-dir", outdir]
        for prop in req.get("build_properties") or []:
            cmd += ["--build-property", prop]
        cmd += [sdir]
        rc, so, se = _run(cmd, td, timeout=req.get("timeout", 900))
        log = (so + se)[-MAX_LOG:]
        if rc != 0:
            return {"ok": False, "error": "compile failed", "log": log,
                    "deps": deps, "dep_log": "\n".join(dep_log)[-2000:]}
        merged = _merge_esp(outdir, fqbn, req.get("flash_size", "4MB"))
        app_bin = merged or _find_bin(outdir, ".bin",
                                      exclude=[".bootloader.bin", ".partitions.bin"])
        if not app_bin:
            return {"ok": False, "error": "no .bin produced", "log": log, "deps": deps}
        return {
            "ok": True, "chip": _chip_from_fqbn(fqbn), "fqbn": fqbn,
            "offset": "0x0" if merged else "0x10000", "merged": bool(merged),
            "name": f"{sketch_name}.bin", "bin_b64": _b64_file(app_bin),
            "size": os.path.getsize(app_bin), "deps": deps, "log": log,
        }


@app.post("/build/platformio")
def build_platformio(req: Dict[str, Any]):
    pio = "pio" if _which("pio") else ("platformio" if _which("platformio") else "")
    if not pio:
        return {"ok": False, "error": "platformio not installed in builder"}
    files = dict(req.get("files") or {})
    if req.get("platformio_ini"):
        files["platformio.ini"] = req["platformio_ini"]
    if "platformio.ini" not in files:
        return {"ok": False, "error": "platformio.ini required (in files or platformio_ini)"}
    with tempfile.TemporaryDirectory() as td:
        _write_files(td, files)
        cmd = [pio, "run"]
        if req.get("environment"):
            cmd += ["-e", str(req["environment"])]
        rc, so, se = _run(cmd, td, timeout=req.get("timeout", 1200))
        log = (so + se)[-MAX_LOG:]
        if rc != 0:
            return {"ok": False, "error": "pio run failed", "log": log}
        fw = _find_bin(os.path.join(td, ".pio", "build"), "firmware.bin") \
            or _find_bin(os.path.join(td, ".pio", "build"), ".bin")
        if not fw:
            return {"ok": False, "error": "no firmware produced", "log": log}
        return {"ok": True, "name": os.path.basename(fw), "bin_b64": _b64_file(fw),
                "size": os.path.getsize(fw), "log": log}


@app.post("/build/exec")
def build_exec(req: Dict[str, Any]):
    """Run an arbitrary build command in a fresh sandbox dir. Optionally installs
    system packages (`apt`), and/or provisions an isolated Python venv and installs
    `pip` packages (and any requirements.txt in files) into it before running."""
    command = req.get("command")
    if not command:
        return {"ok": False, "error": "command required"}
    timeout = int(req.get("timeout", 900))
    with tempfile.TemporaryDirectory() as td:
        try:
            _write_files(td, req.get("files") or {})
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        setup: List[str] = []
        env: Dict[str, str] = {}

        # System packages (not isolated — persists in the running container).
        apt = req.get("apt") or []
        if apt:
            rc, so, se = _run("apt-get update && apt-get install -y --no-install-recommends "
                              + " ".join(shlex.quote(x) for x in apt),
                              td, timeout=timeout, shell=True)
            setup.append(f"[apt] rc={rc}\n" + (so + se)[-1500:])

        # Isolated Python virtualenv for pip deps.
        pip = req.get("pip") or []
        if req.get("venv") or pip or os.path.exists(os.path.join(td, "requirements.txt")):
            rc, so, se = _run(["python3", "-m", "venv", "venv"], td, timeout=300)
            setup.append(f"[venv] rc={rc} " + (se or so).strip()[-300:])
            vbin = os.path.join(td, "venv", "bin")
            env["VIRTUAL_ENV"] = os.path.join(td, "venv")
            env["PATH"] = vbin + os.pathsep + os.environ.get("PATH", "")
            pip_exe = os.path.join(vbin, "pip")
            if os.path.exists(os.path.join(td, "requirements.txt")):
                rc, so, se = _run([pip_exe, "install", "-r", "requirements.txt"],
                                  td, timeout=timeout, env=env)
                setup.append(f"[pip -r requirements.txt] rc={rc}\n" + (so + se)[-1500:])
            if pip:
                rc, so, se = _run([pip_exe, "install", *pip], td, timeout=timeout, env=env)
                setup.append(f"[pip install] rc={rc}\n" + (so + se)[-1500:])

        for k, v in (req.get("env") or {}).items():
            env[k] = str(v)

        rc, so, se = _run(command, td, timeout=timeout, shell=True, env=env)
        artifacts: Dict[str, str] = {}
        for pat in req.get("artifacts") or []:
            for f in glob.glob(os.path.join(td, pat), recursive=True):
                if os.path.isfile(f):
                    try:
                        artifacts[os.path.relpath(f, td)] = _b64_file(f)
                    except Exception:
                        pass
        return {"ok": rc == 0, "returncode": rc, "stdout": so[-MAX_LOG:],
                "stderr": se[-MAX_LOG:], "setup_log": "\n".join(setup)[-MAX_LOG:],
                "artifacts": artifacts}


@app.post("/build/python")
def build_python(req: Dict[str, Any]):
    """Run Python in a fresh, isolated virtualenv. Input: files (dict), requirements
    (list|str — pip deps, also accepts a requirements.txt in files), command
    (default 'python3 main.py'), artifacts (globs). Everything installs into a
    per-build venv that's discarded afterwards."""
    files = dict(req.get("files") or {})
    reqs = req.get("requirements")
    if isinstance(reqs, list):
        files["requirements.txt"] = "\n".join(reqs)
    elif isinstance(reqs, str) and reqs.strip():
        files["requirements.txt"] = reqs
    return build_exec({
        "command": req.get("command") or "python3 main.py",
        "files": files, "artifacts": req.get("artifacts") or [],
        "pip": req.get("pip") or [], "venv": True,
        "env": req.get("env") or {}, "timeout": req.get("timeout", 900),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BUILDER_PORT", "8080")))
