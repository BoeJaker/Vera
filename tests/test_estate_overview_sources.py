"""The backups, storage and services checks gather their facts through the
capability registry: per-node pxstore capabilities on every online Proxmox
node (once per API host, whatever the number of cluster records), plus
docker.disk.status, vfs.health and identity.status."""
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

from vera.estate import estate_health_core as core  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "estate", "estate_health_capabilities.py")
WANTED = {"_module_of", "_call", "_proxmox_nodes", "_per_node", "_backups", "_storage", "_services"}


def sources(registry):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
    ns = {"asyncio": asyncio, "inspect": inspect, "core": core, "Any": Any, "Dict": Dict,
          "List": List, "Optional": Optional,
          "_orch": types.SimpleNamespace(CAPABILITY_REGISTRY=registry)}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns


PROXMOX = (
    "async def _all_raw():\n"
    "    return RECORDS\n"
    "def _open(rec):\n"
    "    return dict(rec)\n"
    "async def _pve(rec, method, path, data=None):\n"
    "    if not rec.get('token'):\n"
    "        return None, 'no API token configured'\n"
    "    return NODES, ''\n"
    "async def cap_status(cluster_id='', trace_id=None):\n"
    "    return {}\n"
)


def registry(**caps):
    ns = {"RECORDS": [{"id": "old", "label": "Home", "api_url": "https://192.168.0.200:8006"},
                      {"id": "new", "label": "corp (PVE01)", "api_url": "https://192.168.0.200:8006", "token": "t"}],
          "NODES": [{"node": "corp", "status": "online"}, {"node": "spare", "status": "offline"}]}
    exec(PROXMOX, ns)
    reg = {"proxmox.status": {"func": ns["cap_status"]}}
    reg.update({name: {"func": fn} for name, fn in caps.items()})
    return reg


def test_backups_run_once_per_online_node_with_the_working_record():
    calls = []

    async def backup_status(cluster_id="", node="", trace_id=None):
        calls.append((cluster_id, node))
        return {"jobs": [], "runs": [], "warnings": ["no backup job is enabled, so nothing backs the estate up"]}

    ns = sources(registry(**{"pxstore.backup.status": backup_status}))
    out = asyncio.run(ns["_backups"]())
    assert calls == [("new", "corp")]
    assert any(f["message"].startswith("No backup job is enabled") for f in out["findings"])


def test_storage_combines_node_disks_with_the_docker_disk():
    async def disks(cluster_id="", node="", trace_id=None):
        return {"disks": [{"name": "sdj", "size": 8e9, "model": "SD", "usb": True, "state": "damaged",
                           "pool": "mypool", "detail": "mypool"}], "free": []}

    async def docker_disk(trace_id=None):
        return {"level": "warn", "pct_used": 88.0, "mount": "/mnt/dockerdata", "note": ""}

    ns = sources(registry(**{"pxstore.disks": disks, "docker.disk.status": docker_disk}))
    out = asyncio.run(ns["_storage"]())
    assert sorted(f["severity"] for f in out["findings"]) == [core.WARN, core.WARN]
    assert out["facts"]["docker_disk_used_pct"] == 88.0


def test_services_read_vfs_and_the_directory():
    async def vfs(trace_id=None):
        return {"ok": False, "services": {"smbd": False}, "estate_mounts": 86}

    async def identity(trace_id=None):
        return {"configured": True, "reachable": True, "version": "4.13.1"}

    ns = sources(registry(**{"vfs.health": vfs, "identity.status": identity}))
    [f] = asyncio.run(ns["_services"]())["findings"]
    assert (f["severity"], f["subject"]) == (core.ERROR, "VFS-02/smbd")


def test_a_capability_that_raises_becomes_an_error_result():
    async def broken(cluster_id="", node="", trace_id=None):
        raise RuntimeError("ssh exploded")

    ns = sources(registry(**{"pxstore.backup.status": broken}))
    [f] = asyncio.run(ns["_backups"]())["findings"]
    assert f["severity"] == core.WARN and "ssh exploded" in f["detail"]


def test_nothing_loaded_is_reported_not_raised():
    ns = sources({})
    assert "not loaded" in asyncio.run(ns["_backups"]())["error"]
    storage = asyncio.run(ns["_storage"]())
    assert len(storage["findings"]) == 2 and all(f["severity"] == core.WARN for f in storage["findings"])
    services = asyncio.run(ns["_services"]())
    assert len(services["findings"]) == 2 and all(f["severity"] == core.WARN for f in services["findings"])
