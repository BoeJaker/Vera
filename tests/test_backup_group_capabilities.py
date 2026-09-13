"""backup.status, backup.guest and backup.run against a stand-in Proxmox API,
pxstore and nodes.backup: a shared PBS storage is read once, guests and the
covering job come together, the answer is cached, one guest's backups list
newest first, and a one-off backup is a dry run until confirmed."""
import ast
import asyncio
import os
import sys
import time
import types
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import backup_group_core as core  # noqa: E402
from vera.estate import estate_health_core as health  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "estate", "backup_capabilities.py")
WANTED = {"_flag", "_timed", "_clusters", "_content", "_host", "_gather", "_public", "_ensure",
          "cap_backup_status", "cap_backup_guest", "cap_backup_run"}
HOME = "2d83c1b5-b25a-4b47-98b8-50660e24b682"
NOW = time.time()
JOB = {"id": "vera-estate", "enabled": True, "all": True, "exclude": "160", "vmid": "",
       "storage": "pbs-estate", "mode": "snapshot", "schedule": "02:30", "next_run": 0}
PBS = {"name": "pbs-estate", "type": "pbs", "active": True, "total": 8, "used": 1, "avail": 7}


class World:
    def __init__(self, nodes=("corp", "pve02"), host_error="systemctl missing"):
        self.api: List = []
        self.status_calls: List[str] = []
        self.events: List = []
        self.nodes = list(nodes)

        async def all_raw():
            return [{"id": HOME, "label": "Home", "api_url": "https://192.168.0.200:8006"}]

        async def pve(record, method, path, data=None):
            self.api.append((method, path, data))
            if path == "/cluster/resources?type=node":
                return [{"node": n, "status": "online"} for n in self.nodes] + \
                       [{"node": "down", "status": "offline"}], None
            if path == "/cluster/resources?type=vm":
                return [{"vmid": 136, "name": "Void", "type": "qemu", "node": "corp", "status": "stopped"},
                        {"vmid": 160, "name": "VFS-02", "type": "lxc", "node": "corp", "status": "running"}], None
            if path.endswith("/storage/pbs-estate/content?content=backup"):
                return [{"volid": "pbs-estate:backup/vm/136/a", "vmid": 136, "ctime": int(NOW - 3600),
                         "size": 5, "content": "backup", "verification": {"state": "ok"}},
                        {"volid": "pbs-estate:backup/vm/136/b", "vmid": 136, "ctime": int(NOW - 90000),
                         "size": 4, "content": "backup"}], None
            if method == "POST" and path.endswith("/vzdump"):
                return "UPID:corp:vzdump:136", None
            return None, f"unexpected {path}"

        async def get_cluster(cid, opened=False):
            return {"id": cid}

        async def pxstore_status(cluster_id="", node=""):
            self.status_calls.append(node)
            return {"jobs": [JOB], "storages": [PBS], "timers": {}, "snapshots": {}, "replication": [],
                    "warnings": [], "runs": []}

        async def backup_get():
            return {"config": {"enabled": False, "interval_hours": 24, "proxmox": {}, "docker": {}}}

        px = {"_all_raw": all_raw, "_open": lambda r: dict(r), "_pve": pve, "_get_cluster": get_cluster}
        registry = {"pxstore.backup.status": pxstore_status, "nodes.backup.get": backup_get}

        async def call(name, **kw):
            fn = registry.get(name)
            return await fn(**kw) if fn else {"error": f"{name} is not loaded"}

        async def emit(ev):
            self.events.append(ev)

        async def host():
            return {"error": host_error}

        tree = ast.parse(open(SRC, encoding="utf-8").read())
        keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
        for n in keep:
            n.decorator_list = []
        self.ns = {"asyncio": asyncio, "time": time, "shutil": types.SimpleNamespace(which=lambda n: None),
                   "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "core": core, "health": health,
                   "CACHE_TTL_S": 120.0, "TIMEOUTS_S": {"proxmox": 5, "node": 5, "content": 5, "vera": 5, "host": 5},
                   "_CACHE": {"at": 0.0, "value": None}, "_module_of": lambda name: px, "_call": call,
                   "emit_event": emit, "log": types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None)}
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), self.ns)
        self.ns["_host"] = host


def run(coro):
    return asyncio.run(coro)


def test_status_joins_nodes_guests_and_a_shared_storage_read_once():
    w = World()
    out = run(w.ns["cap_backup_status"]())
    assert w.status_calls == ["corp", "pve02"]
    content_reads = [p for m, p, _ in w.api if "/content" in p]
    assert content_reads == ["/nodes/corp/storage/pbs-estate/content?content=backup"]
    rows = {g["vmid"]: g for g in out["guests"]}
    assert rows[136]["state"] == "ok" and rows[136]["backups"] == 2 and rows[136]["verified"] == "ok"
    assert rows[160]["state"] == "excluded" and rows[160]["cluster_id"] == HOME
    assert [s["id"] for s in out["schedules"]] == ["vera-estate", "nodes.backup"]
    assert "The Vera host's own file backup can only be read on the Vera host." in [f["message"] for f in out["findings"]]
    assert not any(k.startswith("_") for k in out) and out["cached"] is False


def test_status_is_cached_until_refresh():
    w = World()
    run(w.ns["cap_backup_status"]())
    second = run(w.ns["cap_backup_status"]())
    assert second["cached"] is True and w.status_calls == ["corp", "pve02"]
    third = run(w.ns["cap_backup_status"](refresh="true"))
    assert third["cached"] is False and len(w.status_calls) == 4


def test_one_guests_backups_list_newest_first():
    w = World()
    out = run(w.ns["cap_backup_guest"](vmid="136"))
    assert [b["volid"] for b in out["backups"]] == ["pbs-estate:backup/vm/136/a", "pbs-estate:backup/vm/136/b"]
    assert out["backups"][0]["verified"] == "ok" and out["backups"][1]["verified"] is None
    assert out["guest"]["name"] == "Void"
    assert "no guest 999" in run(w.ns["cap_backup_guest"](vmid=999))["error"]
    assert run(w.ns["cap_backup_guest"]())["error"] == "vmid required"


def test_a_one_off_backup_is_a_dry_run_until_confirmed():
    w = World(nodes=("corp",))
    dry = run(w.ns["cap_backup_run"](vmid=136, confirm="false"))
    assert dry["dry_run"] and dry["storage"] == "pbs-estate" and dry["mode"] == "snapshot" and dry["job"] == "vera-estate"
    assert not any(m == "POST" for m, _, _ in w.api)
    done = run(w.ns["cap_backup_run"](vmid=136, confirm=True, mode="stop"))
    posts = [(p, d) for m, p, d in w.api if m == "POST"]
    assert posts == [("/nodes/corp/vzdump", {"vmid": "136", "storage": "pbs-estate", "mode": "stop",
                                             "notes-template": "{{guestname}}"})]
    assert done["ok"] and done["upid"] == "UPID:corp:vzdump:136" and w.ns["_CACHE"]["at"] == 0.0
    assert w.events[-1]["type"] == "backup.run"


def test_an_excluded_guest_still_gets_a_one_off_backup_on_pbs_and_bad_input_is_refused():
    w = World(nodes=("corp",))
    out = run(w.ns["cap_backup_run"](vmid=160))
    assert out["dry_run"] and out["storage"] == "pbs-estate" and out["job"] == ""
    assert "mode must be" in run(w.ns["cap_backup_run"](vmid=160, mode="fast"))["error"]
    assert "not an active backup storage" in run(w.ns["cap_backup_run"](vmid=160, storage="local"))["error"]
    assert "no guest 7" in run(w.ns["cap_backup_run"](vmid=7))["error"]


def test_the_host_backup_is_read_through_systemctl_show():
    w = World()
    outputs = {
        core.HOST_TIMER: "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                         "LastTriggerUSec=@1789260048\nNextElapseUSecRealtime=@1789345891\n",
        core.HOST_SERVICE: "ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n"
                           "ExecMainStartTimestamp=@1789260048\nExecMainExitTimestamp=@1789266086\n"}
    seen = []

    async def fake_run(args, timeout=10.0):
        seen.append(list(args))
        if "--timestamp=unix" in args:
            return {"rc": 1, "stdout": "", "stderr": "unknown option"}
        return {"rc": 0, "stdout": outputs[args[2]], "stderr": ""}

    tree = ast.parse(open(SRC, encoding="utf-8").read())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_host")
    ns = {"core": core, "shutil": types.SimpleNamespace(which=lambda n: "/usr/bin/systemctl"), "_run": fake_run,
          "Dict": Dict, "Any": Any}
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    host = run(ns["_host"]())
    assert host["enabled"] and host["last_end"] == 1789266086 and host["result"] == "success"
    assert len(seen) == 4 and "--timestamp=unix" not in seen[1]
