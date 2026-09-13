"""exec.ssh.hosts.cleanup against stand-in stores: a dry run changes nothing,
references in Redis keep a login, applying backs every login up into the secrets
service first and refuses when that backup fails, and without a machine list no
login is judged stale."""
import ast
import asyncio
import json
import os
import sys
import time
import types
from typing import Any, Dict, List, Optional, Set

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.execution import ssh_cleanup_core as core  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "execution", "ssh_cleanup_capabilities.py")
WANTED = {"_text", "_flag", "_logins", "_referenced", "_reachability", "_guests", "cap_ssh_cleanup"}
KEY = "/home/boejaker/.vera/ssh/id_vera"


def rec(host, label, user="root", auth="key", tags=(), updated="2026-08-01T00:00:00Z"):
    return {"host": host, "port": 22, "user": user, "auth": auth, "label": label,
            "key_path": KEY if auth == "key" else "", "tags": list(tags), "updated_at": updated}


class FakeRedis:
    def __init__(self, strings=None, hashes=None):
        self.strings = dict(strings or {})
        self.hashes = dict(hashes or {})

    async def scan_iter(self, match=None, count=None):
        for k in list(self.strings) + list(self.hashes):
            yield k

    async def type(self, k):
        return "string" if k in self.strings else "hash"

    async def get(self, k):
        return self.strings[k]

    async def hgetall(self, k):
        return dict(self.hashes[k])


class World:
    def __init__(self, machines_error=False, backup_error=False, redis=None):
        self.logins = {
            "d1": rec("192.168.0.248", "Ollama-D", tags=["enrol:e1"], updated="2026-08-07T00:00:00Z"),
            "d2": rec("192.168.0.248", "Ollama-D", tags=["enrol:e2"], updated="2026-08-09T00:00:00Z"),
            "ob": rec("192.168.0.248", "Ollama-b", auth="password"),
            "gone": rec("192.168.0.152", "foundry-mesh-val.vera.lab"),
            "mesh": rec("192.168.0.90", "vera-test-apps"),
        }
        self.calls: List = []
        self.backups: List = []
        self.redis = redis or FakeRedis(hashes={"vera:netsec:mesh": {"main": json.dumps({"members": {"mesh": {}}})}},
                                        strings={"vera:cap:result:exec.ssh.hosts.list": "gone d1 d2 ob"})
        world = self

        async def load_hosts():
            return {k: dict(v) for k, v in world.logins.items()}

        async def call(name, **kw):
            world.calls.append((name, kw))
            if name == "estate.machines":
                if machines_error:
                    return {"error": "down"}
                return {"machines": [{"kind": "guest", "label": "Ollama-D", "ips": ["192.168.0.248"]}]}
            return {"ok": True}

        async def put_named(path, fields):
            world.backups.append((path, fields))
            return {"error": "OpenBao is sealed"} if backup_error else {"ok": True, "path": path}

        async def probe(host, port):
            return host == "192.168.0.248"

        async def emit(ev):
            world.calls.append(("event", ev))

        modules = {"exec.ssh.hosts.list": {"_load_hosts": load_hosts}, "secrets.status": {"put_named": put_named}}
        tree = ast.parse(open(SRC, encoding="utf-8").read())
        keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
        for n in keep:
            n.decorator_list = []
        self.ns = {"asyncio": asyncio, "json": json, "time": time, "os": os, "Any": Any, "Dict": Dict, "List": List,
                   "Optional": Optional, "Set": Set, "core": core, "_module_of": lambda n: modules.get(n),
                   "_call": call, "_redis": lambda: world.redis, "_probe": probe, "emit_event": emit,
                   "glob": types.SimpleNamespace(glob=lambda pattern: []), "PROBE_CONCURRENCY": 4,
                   "SKIP_PREFIXES": ("vera:cap:result:", "vera:activity", "vera:loop:", "vera:dream:", "vera:events")}
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), self.ns)

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]


def run(coro):
    return asyncio.run(coro)


def by_id(out):
    return {s["id"]: s for s in out["steps"]}


def test_a_dry_run_plans_but_changes_nothing():
    w = World()
    out = run(w.ns["cap_ssh_cleanup"]())
    s = by_id(out)
    assert out["dry_run"] and out["referenced"] == ["mesh"]
    assert (s["d1"]["action"], s["ob"]["action"], s["gone"]["action"], s["mesh"]["action"]) == \
        ("merge", "superseded", "stale", "keep")
    assert w.named("exec.ssh.hosts.delete") == [] and w.backups == []


def test_applying_backs_everything_up_first_then_tags_and_removes():
    w = World()
    out = run(w.ns["cap_ssh_cleanup"](apply="true"))
    assert out["applied"]["removed"] == 3 and out["applied"]["tagged"] == 1 and out["applied"]["errors"] == []
    path, fields = w.backups[0]
    assert path.startswith("backup/ssh-logins/") and set(json.loads(fields["value"])) == set(w.logins)
    names = [n for n, _ in w.calls if n.startswith("exec.ssh.hosts.")]
    assert names == ["exec.ssh.hosts.save", "exec.ssh.hosts.delete", "exec.ssh.hosts.delete", "exec.ssh.hosts.delete"]
    saved = w.named("exec.ssh.hosts.save")[0]
    assert saved["id"] == "d2" and saved["tags"] == "enrol:e2,enrol:e1" and saved["key_path"] == KEY
    assert sorted(kw["id"] for kw in w.named("exec.ssh.hosts.delete")) == ["d1", "gone", "ob"]


def test_nothing_changes_when_the_backup_fails():
    w = World(backup_error=True)
    out = run(w.ns["cap_ssh_cleanup"](apply=True))
    assert "backup failed, so nothing changed" in out["error"]
    assert w.named("exec.ssh.hosts.delete") == [] and w.named("exec.ssh.hosts.save") == []


def test_without_a_machine_list_no_login_is_judged_stale():
    w = World(machines_error=True)
    out = run(w.ns["cap_ssh_cleanup"]())
    assert by_id(out)["gone"]["action"] == "keep" and out["notes"]


def test_caches_do_not_count_as_references():
    w = World(redis=FakeRedis(strings={"vera:cap:result:x": "gone", "vera:loop:history": "ob"}))
    out = run(w.ns["cap_ssh_cleanup"]())
    assert out["referenced"] == [] and by_id(out)["gone"]["action"] == "stale"
