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

from typing import Any, Dict

#: what the probe prints for "not in a container"
_NOT_CONTAINER = {"", "none", "unknown"}


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
    out["temps"] = {}
    out["drives"] = {}
    out["max_c"] = None
    h = entry.get("health") or {}
    out["host_health"] = h
    out["health"] = {"fan": {}, "voltage": {}, "power": {}}
    # an empty `temps` is not a fault here: there are none of its own to read
    if not out["host_temps"] or str(out.get("error") or "").startswith(("no sensors", "no temperature")):
        out["error"] = ""
    return out
