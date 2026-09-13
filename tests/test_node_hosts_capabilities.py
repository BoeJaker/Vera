"""proxmox.node.exec, proxmox.node_hosts.merge, proxmox.cluster.save and the
storage fabric's node lookup against stand-in Redis, exec logins and modules:
the cluster record's node map is read first everywhere, the old hostname match
still works when nothing is mapped, and merging only adds map entries."""
import ast
import asyncio
import json
import os
import sys
import types
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.proxmox import node_hosts_core as node_hosts  # noqa: E402

pytestmark = pytest.mark.critical

PMX_SRC = os.path.join(ROOT, "vera", "proxmox", "proxmox_capabilities.py")
PX_SRC = os.path.join(ROOT, "vera", "proxmox", "pxstore_capabilities.py")
PMX_WANTED = {"_redact", "_all_raw", "_get_cluster", "_open", "_web_root", "_host_port", "set_node_hosts",
              "_exec_logins", "_pxstore_cfg_raw", "_resolve_node_login", "cap_node_exec",
              "cap_node_hosts_merge", "cap_cluster_save"}
PVE01 = "27f0ce6b-1be7-4e3c-8240-af94780bb4ae"
EXEC = [{"id": PVE01, "label": "PVE01", "host": "192.168.0.200"},
        {"id": "pve02-login", "label": "pve02", "host": "192.168.0.201"},
        {"id": "llm", "label": "LLM", "host": "192.168.0.138"}]


class FakeRedis:
    def __init__(self, hashes):
        self.hashes = {k: dict(v) for k, v in hashes.items()}

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


def functions(src, wanted, extra_assigns=()):
    tree = ast.parse(open(src, encoding="utf-8").read())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            node.decorator_list = []
            keep.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(getattr(t, "id", "") in extra_assigns for t in targets):
                keep.append(node)
    return ast.Module(body=keep, type_ignores=[])


def proxmox(records, pxstore=None, exec_hosts=EXEC):
    redis = FakeRedis({"vera:proxmox:clusters": {r["id"]: json.dumps(r) for r in records},
                       "vera:pxstore:cfg": {cid: json.dumps(cfg) for cid, cfg in (pxstore or {}).items()}})
    runs, events = [], []

    async def hosts_list(trace_id=None):
        return {"hosts": list(exec_hosts)}

    async def ssh_run(host_id="", command="", timeout=0, **kw):
        runs.append(host_id)
        return {"ok": True, "rc": 0, "stdout": "ok", "stderr": ""}

    async def emit(ev):
        events.append(ev)

    registry = {"exec.ssh.hosts.list": hosts_list, "exec.ssh.run": ssh_run}
    ns = {"json": json, "uuid": uuid, "urlparse": urlparse, "Any": Any, "Dict": Dict, "List": List,
          "Optional": Optional, "Tuple": Tuple, "_node_hosts": node_hosts,
          "KEY_CLUSTERS": "vera:proxmox:clusters", "_SECRET_FIELDS": ("token", "console_password"),
          "_redis": lambda: redis, "_cap": registry.get, "now_iso": lambda: "2026-09-13T10:00:00Z",
          "emit_event": emit,
          "vsecrets": types.SimpleNamespace(seal=lambda s: "sealed:" + s, open_secret=lambda s: s)}
    exec(compile(functions(PMX_SRC, PMX_WANTED), PMX_SRC, "exec"), ns)
    return ns, redis, runs, events


def cluster(cid, label, **kw):
    rec = {"id": cid, "label": label, "api_url": "https://192.168.0.200:8006", "verify_tls": True,
           "token": "sealed:root@pam!Vera=secret", "console_user": ""}
    rec.update(kw)
    return rec


def run(coro):
    return asyncio.run(coro)


def test_node_exec_uses_the_cluster_record_map_first():
    ns, _, runs, _ = proxmox([cluster("corp", "corp", node_hosts={"corp": "llm"})],
                             pxstore={"corp": {"node_hosts": {"corp": PVE01}}})
    assert run(ns["cap_node_exec"](cluster_id="corp", command="hostname", node="corp"))["ok"]
    assert runs == ["llm"]


def test_node_exec_falls_back_to_the_fabric_map_then_the_api_host():
    ns, _, runs, _ = proxmox([cluster("corp", "corp")], pxstore={"corp": {"node_hosts": {"corp": PVE01}}})
    run(ns["cap_node_exec"](cluster_id="corp", command="hostname", node="corp"))
    ns2, _, runs2, _ = proxmox([cluster("corp", "corp")])
    run(ns2["cap_node_exec"](cluster_id="corp", command="hostname"))
    assert runs == [PVE01] and runs2 == [PVE01]


def test_node_exec_finds_a_second_node_by_its_login_and_honours_an_override():
    ns, _, runs, _ = proxmox([cluster("corp", "corp")])
    run(ns["cap_node_exec"](cluster_id="corp", command="hostname", node="pve02"))
    run(ns["cap_node_exec"](cluster_id="corp", command="hostname", node="pve02", pve_ssh_host_id="llm"))
    assert runs == ["pve02-login", "llm"]


def test_node_exec_without_any_login_says_how_to_fix_it():
    ns, _, runs, _ = proxmox([cluster("x", "x", api_url="https://10.9.9.9:8006")], exec_hosts=[])
    out = run(ns["cap_node_exec"](cluster_id="x", command="hostname", node="ghost"))
    assert "no SSH login is mapped" in out["error"] and runs == []


def test_merge_dry_run_writes_nothing_and_apply_only_adds_map_entries():
    records = [cluster("corp", "corp (PVE01)"), cluster("home", "Home", verify_tls=False)]
    cfgs = {"corp": {"node_hosts": {"corp": PVE01}}, "home": {"node_hosts": {"corp": PVE01}}}
    ns, redis, _, events = proxmox(records, pxstore=cfgs)
    plan = run(ns["cap_node_hosts_merge"]())
    assert plan["dry_run"] and plan["clusters_changed"] == 2 and events == []
    assert all("node_hosts" not in json.loads(v) for v in redis.hashes["vera:proxmox:clusters"].values())
    assert run(ns["cap_node_hosts_merge"](apply="false"))["dry_run"] is True
    out = run(ns["cap_node_hosts_merge"](apply=True))
    assert [a["ok"] for a in out["applied"]] == [True, True]
    corp = json.loads(redis.hashes["vera:proxmox:clusters"]["corp"])
    home = json.loads(redis.hashes["vera:proxmox:clusters"]["home"])
    assert corp["node_hosts"] == {"corp": PVE01} and home["node_hosts"] == {"corp": PVE01}
    assert corp["token"] == "sealed:root@pam!Vera=secret" and corp["verify_tls"] is True
    assert home["verify_tls"] is False
    assert events == [{"type": "proxmox.node_hosts.merged", "clusters": 2}]
    assert run(ns["cap_node_hosts_merge"]())["counts"] == {"current": 2}


def test_cluster_save_sets_the_node_map():
    ns, redis, _, _ = proxmox([cluster("corp", "corp")])
    out = run(ns["cap_cluster_save"](id="corp", node_hosts='{"corp": "%s", "blank": ""}' % PVE01))
    assert out["ok"] and out["cluster"]["node_hosts"] == {"corp": PVE01}
    assert "error" in run(ns["cap_cluster_save"](id="corp", node_hosts="{not json"))


def test_the_storage_fabric_reads_the_cluster_record_map_first():
    async def get_cluster(cid, opened=False):
        return {"corp": {"id": "corp", "node_hosts": {"corp": "llm"}}}.get(cid)

    pm = types.SimpleNamespace(_get_cluster=get_cluster)
    redis = FakeRedis({"vera:pxstore:cfg": {"corp": json.dumps({"node_hosts": {"corp": PVE01, "other": "pve02-login"}})}})
    ns = {"json": json, "Dict": Dict, "Any": Any, "_pmx": lambda: pm, "_redis": lambda: redis,
          "KEY_CFG": "vera:pxstore:cfg", "log": types.SimpleNamespace(debug=lambda *a, **k: None)}
    exec(compile(functions(PX_SRC, {"_node_host_id", "_cfg_get"}, extra_assigns=("_DEFAULT_CFG",)),
                 PX_SRC, "exec"), ns)
    assert run(ns["_node_host_id"]("corp", "corp")) == "llm"
    assert run(ns["_node_host_id"]("corp", "other")) == "pve02-login"
    assert run(ns["_node_host_id"]("corp", "ghost")) == ""
