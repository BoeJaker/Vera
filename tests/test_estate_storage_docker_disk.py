"""The storage check reads Docker's data-disk headroom without waiting on
docker.disk.status's sweep of exited sandboxes (~122 s on prod, which pushed
the whole storage check past its 75 s timeout on 13 Sep 2026)."""
import ast
import asyncio
import inspect
import os
import sys
import time
import types
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "estate", "estate_health_capabilities.py")

EVOLVE = (
    "CALLS = []\n"
    "async def _docker_root():\n"
    "    return '/mnt/dockerdata'\n"
    "async def _disk_reading(path):\n"
    "    return {'mount': path, 'total_gb': 738.0, 'free_gb': 238.0}\n"
    "async def docker_disk_status(trace_id=None):\n"
    "    CALLS.append('status')\n"
    "    await asyncio.sleep(SLOW_S)\n"
    "    return {'level': 'ok', 'pct_used': 1.0, 'mount': '/slow', 'note': 'slow path'}\n"
)


def describe(mount, total_gb, free_gb):
    used = round(100 * (total_gb - free_gb) / total_gb, 1)
    return {"mount": mount, "level": "ok", "total_gb": total_gb, "free_gb": free_gb, "pct_used": used,
            "note": f"{mount}: {free_gb:.0f}G free of {total_gb:.0f}G ({used}% used)"}


def evolve_module(with_helpers=True, slow_s=5.0):
    ns = {"asyncio": asyncio, "SLOW_S": slow_s}
    exec(EVOLVE, ns)
    if with_helpers:
        ns["_disk"] = types.SimpleNamespace(describe=describe)
    else:
        for name in ("_docker_root", "_disk_reading"):
            ns.pop(name)
    return ns


def health_ns(registry, timeout_s=0.3):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in {"_module_of", "_call", "_docker_disk"}]
    ns = {"asyncio": asyncio, "inspect": inspect, "Any": Any, "Dict": Dict, "List": List,
          "Optional": Optional, "DOCKER_DISK_TIMEOUT_S": timeout_s,
          "log": types.SimpleNamespace(debug=lambda *a, **k: None),
          "_orch": types.SimpleNamespace(CAPABILITY_REGISTRY=registry)}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns


def test_headroom_comes_from_the_fast_helpers_without_the_sandbox_sweep():
    ev = evolve_module()
    ns = health_ns({"docker.disk.status": {"func": ev["docker_disk_status"]}})
    started = time.monotonic()
    out = asyncio.run(ns["_docker_disk"]())
    assert time.monotonic() - started < 1.0
    assert (out["mount"], out["level"], out["pct_used"]) == ("/mnt/dockerdata", "ok", 67.8)
    assert ev["CALLS"] == []


def test_without_the_helpers_it_falls_back_to_the_capability():
    ev = evolve_module(with_helpers=False, slow_s=0.0)
    ns = health_ns({"docker.disk.status": {"func": ev["docker_disk_status"]}})
    out = asyncio.run(ns["_docker_disk"]())
    assert out["note"] == "slow path" and ev["CALLS"] == ["status"]


def test_a_slow_fallback_is_reported_instead_of_blocking_the_disk_check():
    ev = evolve_module(with_helpers=False, slow_s=5.0)
    ns = health_ns({"docker.disk.status": {"func": ev["docker_disk_status"]}}, timeout_s=0.2)
    started = time.monotonic()
    out = asyncio.run(ns["_docker_disk"]())
    assert time.monotonic() - started < 2.0
    assert "did not answer" in out["error"]


def test_the_storage_check_uses_it():
    body = open(SRC, encoding="utf-8").read()
    storage = body[body.index("async def _storage()"):body.index("async def _services()")]
    assert "_docker_disk()" in storage and '_call("docker.disk.status")' not in storage
