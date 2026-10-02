"""node_temps_core.py - whose sensors a node's temperatures are.

Pure functions for obs.node_temps (nodes_capabilities.py); pinned by
tests/test_node_temps_core.py.

Why (owner, 2026-09-28: the cpu-cores matrix showed 82-85 °C for every host).
The probe runs `sensors`, `ipmitool` and `smartctl` over SSH on each registered
host. A container shares its host's kernel, BMC and disks, so in an LXC guest
all three read the HOST: ollama126 (CT 126), Ollama-C (CT 130) and VFS-02 each
reported PVE01's "Core 0..13" temperatures and its sda..sdj drives as their own
(measured on prod 2026-10-01). A container's readings are kept, but as the
host's (`host_temps`), never as the container's own `temps`/`max_c`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

#: what the probe prints for "not in a container"
_NOT_CONTAINER = {"", "none", "unknown"}

# Per-core temperatures, per package (2026-10-02). `sensors` prints one coretemp
# chip per CPU package (coretemp-isa-0000, -0001 ...), each labelling its cores
# "Core N" by the hardware core_id - so on PVE01's two packages "Core 0" appears
# twice, and a flat {label: celsius} kept only the last package's. Every logical
# cpu maps to exactly one (package, core_id) in /sys, which is what lets a guest
# pinned to host cpus 12-23 be given the temperatures of the cores it runs on.
_CHIP = re.compile(r"^coretemp-isa-0*(\d+)$")
_PKG = re.compile(r"^Package id (\d+)$")
_CORE = re.compile(r"^Core (\d+)$")


def parse_core_temps(sensors_u: str) -> Dict[str, float]:
    """`sensors -A -u` -> {"<package>:<core_id>": celsius} for every coretemp
    core. The package is the chip's own "Package id N" when it prints one, else
    the chip's isa index."""
    out: Dict[str, float] = {}
    pkg, label, chip_core = None, "", False
    for raw in (sensors_u or "").splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            s = raw.strip()
            m = _CHIP.match(s)
            if m:
                pkg, label, chip_core = int(m.group(1)), "", True
                continue
            if s.endswith(":"):
                label = s[:-1].strip()
                mp = _PKG.match(label)
                if mp and chip_core:
                    pkg = int(mp.group(1))
                continue
            chip_core, label = False, ""          # another chip (tg3, acpi ...)
            continue
        if not chip_core or pkg is None:
            continue
        mc = _CORE.match(label)
        key, sep, val = raw.strip().partition(":")
        if mc and sep and key.strip().endswith("_input"):
            try:
                out[f"{pkg}:{int(mc.group(1))}"] = round(float(val.strip()), 1)
            except ValueError:
                continue
    return out


def parse_cpu_topo(out: str) -> Dict[str, Tuple[int, int]]:
    """The probe's `CPUTOPO|cpuN|package|core_id` lines -> {"cpuN": (package, core_id)}."""
    topo: Dict[str, Tuple[int, int]] = {}
    for ln in (out or "").splitlines():
        if not ln.startswith("CPUTOPO|"):
            continue
        parts = ln.split("|")
        if len(parts) != 4:
            continue
        try:
            topo[parts[1].strip()] = (int(parts[2]), int(parts[3]))
        except ValueError:
            continue
    return topo


def cpu_temps(core_temps: Dict[str, float], topo: Dict[str, Tuple[int, int]]) -> Dict[str, float]:
    """{"cpuN": celsius}: each logical cpu with its physical core's temperature
    (hyperthread siblings share their core's)."""
    out: Dict[str, float] = {}
    for cpu, (pkg, core) in topo.items():
        t = core_temps.get(f"{pkg}:{core}")
        if t is not None:
            out[cpu] = t
    return out


def parse_virt(out: str) -> str:
    """The probe's `VIRT|<kind>` line -> kind ('lxc', 'docker', ...) or '' when
    the host is not a container (or the probe predates the line)."""
    for ln in (out or "").splitlines():
        if ln.startswith("VIRT|"):
            v = ln.split("|", 1)[1].strip().lower()
            return "" if v in _NOT_CONTAINER else v
    return ""


def attribute(entry: Dict[str, Any], virt: str) -> Dict[str, Any]:
    """Return the cache entry with a container's sensor readings moved to the
    host's. Load (percpu) and disk usage are the container's own and stay."""
    out = dict(entry)
    out["virt"] = virt or ""
    if not virt:
        out["temps_from"] = "own"
        return out
    out["temps_from"] = "host"
    out["host_temps"] = dict(entry.get("temps") or {})
    out["host_drives"] = dict(entry.get("drives") or {})
    out["host_core_temps"] = dict(entry.get("core_temps") or {})
    out["host_cpu_temps"] = dict(entry.get("cpu_temps") or {})
    out["temps"] = {}
    out["drives"] = {}
    out["core_temps"] = {}
    out["cpu_temps"] = {}
    out["max_c"] = None
    h = entry.get("health") or {}
    out["host_health"] = h
    out["health"] = {"fan": {}, "voltage": {}, "power": {}}
    # an empty `temps` is not a fault here: there are none of its own to read
    if not out["host_temps"] or str(out.get("error") or "").startswith(("no sensors", "no temperature")):
        out["error"] = ""
    return out
