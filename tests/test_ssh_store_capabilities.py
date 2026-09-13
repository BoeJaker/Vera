"""ssh.host.* and ssh.stores.merge against stand-in Redis, secrets and exec
stores: the list reads the exec store through, a dry run writes nothing,
applying links twins once and keeps their logins, saving never duplicates an
exec login, and an exec-only login cannot be deleted from the enrolment side."""
import ast
import asyncio
import json
import os
import sys
import types
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.provisioning import ssh_store_merge_core as merge  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "provisioning", "enroll_capabilities.py")
WANTED = {"_hosts_raw", "_redact", "_open", "_get_host", "_exec_hosts", "_ensure_exec_twin",
          "cap_host_save", "cap_host_list", "cap_host_delete", "cap_stores_merge"}
KEY = "/home/boejaker/.vera/ssh/id_vera"
CLUSTER = "2d83c1b5-b25a-4b47-98b8-50660e24b682"


class FakeRedis:
    def __init__(self, hosts):
        self.data = {h["id"]: json.dumps(h) for h in hosts}

    async def hgetall(self, key):
        return dict(self.data)

    async def hget(self, key, field):
        return self.data.get(field)

    async def hset(self, key, field, value):
        self.data[field] = value

    async def hexists(self, key, field):
        return field in self.data

    async def hdel(self, key, field):
        self.data.pop(field, None)


class FakeExec:
    """Enough of exec.ssh.hosts.list/save: save with an id replaces that
    record (keeping its secrets), without one creates a record."""

    def __init__(self, records):
        self.records = {r["id"]: dict(r) for r in records}
        self.saves: List[Dict[str, Any]] = []

    async def list(self, trace_id=None):
        return {"hosts": [dict(r) for r in self.records.values()]}

    async def save(self, tags="", id="", **kw):
        self.saves.append(dict(kw, id=id, tags=tags))
        rid = id or f"x-new-{len(self.records)}"
        prev = self.records.get(rid, {})
        rec = dict(prev, **{k: v for k, v in kw.items() if k != "password"}, id=rid,
                   tags=[t for t in tags.split(",") if t])
        rec["has_password"] = bool(kw.get("password")) or bool(prev.get("has_password"))
        self.records[rid] = rec
        return {"ok": True, "host": rec}


def caps(enrol_hosts, exec_records):
    redis = FakeRedis(enrol_hosts)
    ex = FakeExec(exec_records)
    events = []
    registry = {"exec.ssh.hosts.list": ex.list, "exec.ssh.hosts.save": ex.save}
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED:
            node.decorator_list = []
            keep.append(node)

    async def emit(ev):
        events.append(ev)

    ns = {"json": json, "uuid": uuid, "asyncio": asyncio, "Any": Any, "Dict": Dict, "List": List,
          "Optional": Optional, "Tuple": Tuple, "_merge": merge, "KEY_HOSTS": "vera:provisioning:ssh_hosts",
          "_SECRET_FIELDS": ("password", "private_key"),
          "_redis": lambda: redis, "_cap": registry.get, "_vera_key_path": lambda: KEY,
          "now_iso": lambda: "2026-09-13T09:00:00Z", "emit_event": emit,
          "log": types.SimpleNamespace(debug=lambda *a, **k: None),
          "vsecrets": types.SimpleNamespace(seal=lambda s: "sealed:" + s,
                                            open_secret=lambda s: s[len("sealed:"):] if s.startswith("sealed:") else s)}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns, redis, ex, events


def exec_rec(xid, label, host, user="root", auth="key", tags=("enrolled", "lxc")):
    return {"id": xid, "label": label, "host": host, "port": 22, "user": user, "auth": auth,
            "key_path": KEY if auth == "key" else "", "tags": list(tags),
            "has_password": auth == "password", "updated_at": "2026-08-06T23:47:01Z"}


def enrol_rec(eid, label, host, user="root", auth="cert", vmid=144):
    return {"id": eid, "label": label, "host": host, "port": 22, "user": user, "auth": auth,
            "guest_ref": f"{CLUSTER}:{vmid}", "public_key": "", "created": "2026-08-06T23:32:00Z"}


def run(coro):
    return asyncio.run(coro)


def test_the_list_reads_the_exec_store_through():
    ns, _, _, _ = caps([enrol_rec("e1", "foundry-ct-test.vera.int", "192.168.0.95")],
                       [exec_rec("x1", "foundry-ct-test.vera.int", "192.168.0.95"),
                        exec_rec("x-llm", "LLM", "192.168.0.138", user="boejaker", auth="password", tags=())])
    rows = run(ns["cap_host_list"]())["hosts"]
    assert [(r["id"], r["source"], r["exec_id"]) for r in rows] == [("e1", "enrol", "x1"), ("x-llm", "exec", "x-llm")]


def test_a_dry_run_writes_nothing():
    ns, _, ex, events = caps([enrol_rec("e1", "Ollama-E", "192.168.0.249", vmid=132)],
                             [exec_rec("x1", "Ollama-E", "192.168.0.249")])
    plan = run(ns["cap_stores_merge"]())
    assert plan["dry_run"] is True and plan["counts"] == {"link": 1}
    assert run(ns["cap_stores_merge"](apply="false"))["dry_run"] is True
    assert ex.saves == [] and events == []


def test_applying_links_each_twin_once_and_keeps_its_login():
    ns, _, ex, events = caps([enrol_rec("e1", "foundry-ct-test.vera.int", "192.168.0.95"),
                              enrol_rec("e2", "foundry-ct-test.vera.int", "192.168.0.95")],
                             [exec_rec("x1", "foundry-ct-test.vera.int", "192.168.0.95")])
    out = run(ns["cap_stores_merge"](apply=True))
    assert [a["ok"] for a in out["applied"]] == [True, True]
    assert len(ex.records) == 1
    rec = ex.records["x1"]
    assert rec["tags"] == ["enrolled", "lxc", f"guest:{CLUSTER}:144", "enrol:e1", "enrol:e2"]
    assert all(s["id"] == "x1" and s["auth"] == "key" and s["key_path"] == KEY and "password" not in s
               for s in ex.saves)
    assert events == [{"type": "ssh.stores.merged", "linked": 2, "created": 0, "failed": 0}]
    again = run(ns["cap_stores_merge"]())
    assert again["counts"] == {"linked": 2}


def test_saving_a_login_updates_its_exec_twin_instead_of_adding_one():
    ns, redis, ex, _ = caps([], [exec_rec("x1", "Ollama-D", "192.168.0.248")])
    out = run(ns["cap_host_save"](label="Ollama-D", host="192.168.0.248", user="root", auth="cert",
                                  guest_ref=f"{CLUSTER}:131", tags="enrolled,lxc"))
    assert out["ok"] and out["exec_host_id"] == "x1" and out["exec_error"] == ""
    assert len(ex.records) == 1 and len(redis.data) == 1
    assert f"guest:{CLUSTER}:131" in ex.records["x1"]["tags"]


def test_saving_a_new_cert_login_creates_one_exec_login_with_veras_key():
    ns, _, ex, _ = caps([], [])
    out = run(ns["cap_host_save"](label="new-ct", host="192.168.0.170", user="root", auth="cert",
                                  guest_ref=f"{CLUSTER}:170"))
    assert out["exec_host_id"] and len(ex.records) == 1
    assert ex.saves[0]["auth"] == "key" and ex.saves[0]["key_path"] == KEY


def test_an_exec_only_login_is_not_deleted_from_the_enrolment_side():
    ns, _, ex, _ = caps([], [exec_rec("x-llm", "LLM", "192.168.0.138", user="boejaker", auth="password")])
    out = run(ns["cap_host_delete"](id="x-llm"))
    assert "exec store" in out["error"] and "x-llm" in ex.records
    assert run(ns["cap_host_delete"](id="nope"))["error"] == "host not found"
