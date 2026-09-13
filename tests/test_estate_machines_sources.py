"""estate.machines gathers nodes.list and the Proxmox guests (with their
static IPs) through the capability registry, reads guests once per Proxmox API
host with the record whose token works, and still answers when a source fails."""
import ast
import asyncio
import inspect
import os
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_health_core as health_core  # noqa: E402
from vera.estate import estate_machines_core as core  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "estate", "estate_machines_capabilities.py")
WANTED = {"_module_of", "_call", "_guests", "_within", "cap_estate_machines"}

PROXMOX = (
    "async def _all_raw():\n"
    "    return RECORDS\n"
    "def _open(rec):\n"
    "    return dict(rec)\n"
    "async def _pve(rec, method, path, data=None):\n"
    "    CALLS.append((rec.get('id'), path))\n"
    "    if not rec.get('token'):\n"
    "        return None, 'no API token configured'\n"
    "    if path.endswith('/config'):\n"
    "        return CONFIGS.get(path, {}), ''\n"
    "    return GUESTS, ''\n"
    "async def cap_status(cluster_id='', trace_id=None):\n"
    "    return {}\n"
)


def capability_ns(registry):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED:
            node.decorator_list = []
            keep.append(node)
    ns = {"asyncio": asyncio, "inspect": inspect, "core": core, "health_core": health_core,
          "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "SOURCE_TIMEOUT_S": 5.0,
          "_CONFIG_CONCURRENCY": 2, "now_iso": lambda: "2026-09-12T21:00:00Z",
          "_orch": types.SimpleNamespace(CAPABILITY_REGISTRY=registry)}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns


def proxmox_module(calls):
    ns = {"CALLS": calls,
          "RECORDS": [{"id": "home", "label": "Home", "api_url": "https://192.168.0.200:8006"},
                      {"id": "corp-id", "label": "corp (PVE01)", "api_url": "https://192.168.0.200:8006",
                       "token": "t"}],
          "GUESTS": [{"vmid": 126, "name": "ollama-gpu", "type": "lxc", "node": "corp", "status": "running"},
                     {"vmid": 160, "name": "VFS-02", "type": "lxc", "node": "corp", "status": "running"},
                     {"vmid": 141, "name": "debian-12-tmpl", "type": "qemu", "node": "corp",
                      "status": "stopped", "template": 1}],
          "CONFIGS": {"/nodes/corp/lxc/160/config": {"net0": "name=eth0,ip=192.168.0.160/24"}}}
    exec(PROXMOX, ns)
    return ns


async def nodes_list(trace_id=None):
    return {"nodes": [
        {"id": "g126", "label": "pve:126@corp", "ssh_host_id": "g126",
         "proxmox": {"kind": "guest", "cluster_id": "home", "vmid": 126, "node": "corp"}, "backends": ["ssh"]},
        {"id": "vfs", "label": "VFS-02", "addr": "192.168.0.160", "ssh_host_id": "vfs", "proxmox": None,
         "backends": ["ssh"]},
    ]}


def ids_of(row):
    return [a["id"] for a in row["actions"]]


def test_guests_are_read_once_per_api_host_and_folded_with_their_ssh_hosts():
    calls = []
    px = proxmox_module(calls)
    ns = capability_ns({"proxmox.status": {"func": px["cap_status"]}, "nodes.list": {"func": nodes_list}})
    out = asyncio.run(ns["cap_estate_machines"]())
    resources = [c for c in calls if c[1] == "/cluster/resources?type=vm"]
    configs = sorted(c[1] for c in calls if c[1].endswith("/config"))
    assert resources == [("home", "/cluster/resources?type=vm"), ("corp-id", "/cluster/resources?type=vm")]
    assert configs == ["/nodes/corp/lxc/126/config", "/nodes/corp/lxc/160/config"]
    rows = {r["label"]: r for r in out["machines"]}
    assert sorted(rows) == ["VFS-02", "debian-12-tmpl", "ollama-gpu"]
    assert rows["VFS-02"]["ssh_host_id"] == "vfs" and rows["VFS-02"]["ips"] == ["192.168.0.160"]
    assert rows["ollama-gpu"]["cluster_id"] == "corp-id"
    assert out["errors"] == []


def test_a_missing_nodes_list_still_lists_the_guests():
    px = proxmox_module([])
    ns = capability_ns({"proxmox.status": {"func": px["cap_status"]}})
    out = asyncio.run(ns["cap_estate_machines"]())
    assert any("nodes.list" in e for e in out["errors"])
    assert sorted(r["label"] for r in out["machines"]) == ["VFS-02", "debian-12-tmpl", "ollama-gpu"]
    assert ids_of({r["label"]: r for r in out["machines"]}["VFS-02"]) == ["console", "shutdown", "reboot", "ssh"]


def test_nothing_loaded_is_an_empty_list_with_reasons():
    out = asyncio.run(capability_ns({})["cap_estate_machines"]())
    assert out["machines"] == []
    assert len(out["errors"]) == 2
