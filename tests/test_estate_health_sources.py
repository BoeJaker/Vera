"""estate.health reaches the Docker and Proxmox helpers through the capability
registry. Capability modules load under their bare file name, so looking them
up by import path found nothing in a running Vera and both checks reported
"not loaded" (seen on the bleeding-edge mirror, 12 Sep 2026)."""
import ast
import asyncio
import functools
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


def sources(registry):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in {"_module_of", "_containers", "_guests"}]
    ns = {"asyncio": asyncio, "inspect": inspect, "core": core, "Any": Any, "Dict": Dict,
          "List": List, "Optional": Optional, "_INSPECT_CONCURRENCY": 2, "_CONFIG_CONCURRENCY": 2,
          "_orch": types.SimpleNamespace(CAPABILITY_REGISTRY=registry)}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns


def module(code, **values):
    """A stand-in capability module: its functions' globals are this namespace."""
    ns = dict(values)
    exec(code, ns)
    return ns


DOCKER = (
    "async def _engine_request(rec, method, path, timeout=15.0):\n"
    "    if path.startswith('/containers/json'):\n"
    "        return 200, ROWS, 'application/json'\n"
    "    return 200, INSPECTED[path.split('/')[2]], 'application/json'\n"
    "async def _parse_engine_json(body, default):\n"
    "    return body\n"
    "def _get_host(host_id):\n"
    "    return {'id': host_id, 'kind': 'local'}\n"
    "async def cap_docker_ps(host_id='', all=True, trace_id=None):\n"
    "    return {}\n"
)

PROXMOX = (
    "async def _all_raw():\n"
    "    return RECORDS\n"
    "def _open(rec):\n"
    "    return dict(rec)\n"
    "async def _pve(rec, method, path, data=None):\n"
    "    if not rec.get('token'):\n"
    "        return None, 'no API token configured'\n"
    "    return RESPONSES.get(path), ''\n"
    "async def cap_status(cluster_id='', trace_id=None):\n"
    "    return {}\n"
)


def test_containers_are_read_through_the_registered_docker_module():
    rows = [
        {"Id": "a1", "Names": ["/vikunja-web"], "State": "exited",
         "Labels": {"com.docker.compose.project": "src"}},
        {"Id": "b2", "Names": ["/vera-sbx-chat-1"], "State": "exited", "Labels": {"vera.sandbox": "chat-1"}},
        {"Id": "c3", "Names": ["/redis"], "State": "running", "Labels": {"com.docker.compose.project": "src"}},
    ]
    inspected = {"a1": {"State": {"Status": "exited", "ExitCode": 1, "FinishedAt": "2026-09-02T19:28:30Z",
                                  "Error": "Bind for 0.0.0.0:8081 failed: port is already allocated"},
                        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}}
    dk = module(DOCKER, ROWS=rows, INSPECTED=inspected)
    out = asyncio.run(sources({"docker.ps": {"func": dk["cap_docker_ps"]}})["_containers"]())
    [f] = out["findings"]
    assert (f["severity"], f["subject"]) == (core.ERROR, "vikunja-web")
    assert out["facts"] == {"listed": 3, "sandboxes_ignored": 1, "not_running": 1}


def test_a_decorated_capability_still_leads_to_its_module():
    dk = module(DOCKER, ROWS=[], INSPECTED={})

    @functools.wraps(dk["cap_docker_ps"])
    async def wrapper(*args, **kwargs):
        return await dk["cap_docker_ps"](*args, **kwargs)

    ns = sources({"docker.ps": {"func": wrapper}})
    assert ns["_module_of"]("docker.ps")["_get_host"] is dk["_get_host"]


def test_guests_are_read_through_the_registered_proxmox_module():
    records = [{"id": "old", "label": "Home", "api_url": "https://192.168.0.200:8006"},
               {"id": "new", "label": "corp (PVE01)", "api_url": "https://192.168.0.200:8006", "token": "t"}]
    responses = {
        "/cluster/resources?type=vm": [
            {"vmid": 160, "name": "VFS-02", "type": "lxc", "node": "corp", "status": "running", "template": 0},
            {"vmid": 147, "name": "kali-2020-4", "type": "qemu", "node": "corp", "status": "running", "template": 0},
            {"vmid": 141, "name": "debian-12-tmpl", "type": "qemu", "node": "corp", "status": "stopped",
             "template": 1},
        ],
        "/nodes/corp/lxc/160/config": {"onboot": 1},
        "/nodes/corp/qemu/147/config": {},
    }
    px = module(PROXMOX, RECORDS=records, RESPONSES=responses)
    out = asyncio.run(sources({"proxmox.status": {"func": px["cap_status"]}})["_guests"]())
    messages = [f["message"] for f in out["findings"]]
    assert len(messages) == 2
    assert any("kali-2020-4 (VM 147)" in m for m in messages)
    assert any("2 Proxmox cluster records" in m for m in messages)
    assert out["facts"]["running_checked"] == 2


def test_missing_capabilities_are_reported_as_not_loaded():
    ns = sources({})
    assert "not loaded" in asyncio.run(ns["_containers"]())["error"]
    assert "not loaded" in asyncio.run(ns["_guests"]())["error"]


def test_the_startup_check_is_a_one_shot_scheduled_job_that_only_logs():
    """It registers through schedule() with a never-repeating interval, waits for
    the Redis connection, and reaches the log - never the Redis config."""
    import ast
    src = os.path.join(ROOT, "vera", "estate", "estate_health_capabilities.py")
    text = open(src, encoding="utf-8").read()
    tree = ast.parse(text)
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_startup_state_store_check")
    calls = {getattr(c.func, "attr", getattr(c.func, "id", "")) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert {"warning", "info", "_state_store", "startup_lines"} <= calls
    assert not any(a in calls for a in ("hset", "set", "delete", "config_set"))
    sched = next(c for c in ast.walk(tree) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "schedule")
    kw = {k.arg: k.value for k in sched.keywords}
    assert kw["name"].value == "estate_state_store_startup_check"
    assert kw["skip_in_sandbox"].value is True
    assert eval(compile(ast.Expression(kw["interval"]), "<interval>", "eval")) >= 10 ** 6
