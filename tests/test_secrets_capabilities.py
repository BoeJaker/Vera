"""secrets.* against a stand-in OpenBao, keydrop, Redis and SSH login store:
setup hands the root token to keydrop before anything changes and stops if
keydrop refuses, named secrets never come back out, migration re-seals file-key
values (leaving OpenBao's own secrets and changed values alone), and the watcher
unseals a restarted OpenBao and renews Vera's token."""
import ast
import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.security import secret_service_core as core  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "security", "secrets_capabilities.py")
WANTED = {"_text", "_flag", "_thread", "_cfg", "_bao", "_prov_state", "_openbao", "_token", "_renew",
          "put_named", "get_named", "_redis_entries", "_exec_store", "_ssh_records", "_counts",
          "cap_secrets_status", "cap_secrets_setup", "cap_secrets_put", "cap_secrets_list",
          "cap_secrets_delete", "cap_secrets_handoff", "cap_secrets_migrate", "cap_secrets_renew",
          "_watch_tick"}
ADDR = "http://127.0.0.1:8200"


class FakeRedis:
    def __init__(self):
        self.strings: Dict[str, str] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttl: Dict[str, int] = {}
        self.gets: Dict[str, int] = {}
        self.change_on_reread = ""

    async def scan_iter(self, match=None, count=None):
        for k in list(self.strings) + list(self.hashes):
            yield k

    async def type(self, k):
        return "string" if k in self.strings else "hash" if k in self.hashes else "none"

    async def get(self, k):
        self.gets[k] = self.gets.get(k, 0) + 1
        if k == self.change_on_reread and self.gets[k] == 2:
            self.strings[k] = "fernet:changed-meanwhile"
        return self.strings.get(k)

    async def set(self, k, v, keepttl=False):
        self.strings[k] = v

    async def hgetall(self, k):
        return dict(self.hashes.get(k, {}))

    async def hget(self, k, f):
        return self.hashes.get(k, {}).get(f)

    async def hset(self, k, f, v):
        self.hashes.setdefault(k, {})[f] = v

    async def expire(self, k, s):
        self.ttl[k] = s


class World:
    def __init__(self, token="root-tok", active=True, sealed=False, keydrop_ok=True):
        self.kv: Dict[str, Dict[str, Any]] = {}
        self.policies: Dict[str, str] = {}
        self.audit: Dict[str, Any] = {}
        self.calls: List[Tuple] = []
        self.drops: List[Tuple] = []
        self.events: List[Dict] = []
        self.opened = {"openbao_addr": ADDR, "openbao_mount": "secret", "openbao_token": token,
                       "openbao_unseal": json.dumps(["unseal-1"])}
        self.env_token = token if active else ""
        self.active, self.sealed, self.keydrop_ok = active, sealed, keydrop_ok
        self.redis = FakeRedis()
        self.hosts: Dict[str, Dict[str, Any]] = {}
        self.saved_hosts: Optional[Dict] = None
        world = self

        async def http(method, url, *, token="", body=None, namespace="", verify=False, timeout=8.0):
            path = url.split("/v1/", 1)[1]
            world.calls.append((method, path))
            if path == "sys/seal-status":
                return 200, {"initialized": True, "sealed": world.sealed, "storage_type": "file"}, ""
            if path == "auth/token/lookup-self":
                if token == "root-tok":
                    return 200, {"data": {"policies": ["root"], "ttl": 0, "renewable": False}}, ""
                return 200, {"data": {"policies": ["default", "vera"], "ttl": 2_700_000,
                                      "renewable": True, "period": 2_764_800}}, ""
            if path.startswith("sys/policies/acl/"):
                world.policies[path.rsplit("/", 1)[1]] = body["policy"]
                return 204, None, ""
            if path == "sys/audit":
                return 200, {"data": dict(world.audit)}, ""
            if path.startswith("sys/audit/"):
                world.audit["file/"] = body
                return 204, None, ""
            if path == "auth/token/create-orphan":
                return 200, {"auth": {"client_token": "scoped-tok"}}, ""
            if path == "auth/token/renew-self":
                return 200, {"auth": {"lease_duration": 2_764_800}}, ""
            data_prefix, meta_prefix = "secret/data/vera/named/", "secret/metadata/vera/named/"
            if path.startswith(data_prefix):
                p = path[len(data_prefix):]
                if method == "POST":
                    world.kv[p] = dict(body["data"])
                    return 200, {"data": {"version": 1}}, ""
                return (200, {"data": {"data": dict(world.kv[p])}}, "") if p in world.kv else (404, None, "")
            if path.startswith(meta_prefix):
                p = path[len(meta_prefix):]
                if method == "LIST":
                    keys = set()
                    for k in world.kv:
                        if k.startswith(p):
                            rest = k[len(p):]
                            keys.add(rest.split("/")[0] + ("/" if "/" in rest else ""))
                    return (200, {"data": {"keys": sorted(keys)}}, "") if keys else (404, None, "")
                if method == "DELETE":
                    world.kv.pop(p, None)
                    return 204, None, ""
                return 200, {"data": {"updated_time": "2026-09-13T13:00:00Z", "current_version": 1}}, ""
            return 404, None, "unexpected " + path

        def bao_cfg():
            return ({"addr": ADDR, "token": world.env_token, "mount": "secret", "namespace": "", "verify": False}
                    if world.env_token else {})

        def seal(v, force_fernet=False):
            if v.startswith(("fernet:", "bao:v1:")):
                return v
            if force_fernet or not world.active:
                return "fernet:" + v
            return "bao:v1:secret:vera-secrets/" + v

        def open_secret(v):
            if v.startswith("bao:v1:"):
                return v.split("vera-secrets/", 1)[1]
            return v[len("fernet:"):] if v.startswith("fernet:") else v

        vsecrets = types.SimpleNamespace(
            _bao_cfg=bao_cfg, _bao_active=lambda: bool(world.env_token) and world.active and not world.sealed,
            backend_status=lambda: {"backend": "openbao" if world.active else "fernet",
                                    "openbao_configured": bool(world.env_token), "openbao_active": world.active},
            seal=seal, open_secret=open_secret)

        async def state_raw():
            return {k: ("fernet:x" if k in ("openbao_token", "openbao_unseal") else v) for k, v in world.opened.items()}

        async def state_opened():
            return dict(world.opened)

        def export(st):
            world.env_token = st.get("openbao_token", "")

        async def load_hosts():
            return {k: dict(v) for k, v in world.hosts.items()}

        async def save_hosts(h):
            world.saved_hosts = h

        modules = {"prov.config.get": {"_state_raw": state_raw, "_state_opened": state_opened,
                                       "_export_bao_env": export},
                   "exec.ssh.hosts.list": {"_load_hosts": load_hosts, "_save_hosts": save_hosts,
                                           "_deobfuscate": lambda v: v[len("fernet:"):] if v.startswith("fernet:")
                                           else "legacy-" + v}}

        async def call(name, **kw):
            world.calls.append(("call", name))
            if name == "prov.config.save":
                world.opened["openbao_token"] = kw["openbao_token"]
                return {"ok": True}
            if name == "secstore.unseal":
                world.sealed = False
                return {"sealed": False}
            return {"error": name + " is not loaded"}

        def kd_info():
            return {"available": True, "entries": len(world.drops), "chain_ok": True, "message": "intact"}

        def kd_put(label, payload):
            world.calls.append(("keydrop", label))
            if not world.keydrop_ok:
                return {"error": "no recipient key"}
            world.drops.append((label, payload))
            return {"ok": True, "entry": len(world.drops), "label": label}

        async def emit(ev):
            world.events.append(ev)

        tree = ast.parse(open(SRC, encoding="utf-8").read())
        keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
        for n in keep:
            n.decorator_list = []
        self.ns = {"asyncio": asyncio, "json": json, "time": time, "os": os, "Path": Path, "Any": Any, "Dict": Dict,
                   "List": List, "Optional": Optional, "Tuple": Tuple, "core": core, "vsecrets": vsecrets,
                   "_http": http, "_redis": lambda: world.redis, "_module_of": lambda n: modules.get(n),
                   "_call": call, "_keydrop_info_sync": kd_info, "_keydrop_put_sync": kd_put, "emit_event": emit,
                   "now_iso": lambda: "2026-09-13T13:00:00Z",
                   "log": types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None),
                   "KEY_SETUP": "vera:secrets:setup", "BACKUP_PREFIX": "vera:secrets:migrate:backup:",
                   "BACKUP_TTL_S": 30 * 24 * 3600, "AUDIT_PATH": "/openbao/logs/audit.log", "COUNT_TTL_S": 300.0,
                   "RENEW_EVERY_S": 6 * 3600, "_COUNTS": {"at": 0.0, "value": None},
                   "_WATCH": {"task": None, "renewed_at": 0.0}}
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), self.ns)

    def order(self, *kinds):
        return [c for c in self.calls if c[0] in kinds or c[1] in kinds]


def run(coro):
    return asyncio.run(coro)


def test_setup_is_a_dry_run_until_applied():
    w = World()
    out = run(w.ns["cap_secrets_setup"]())
    assert out["dry_run"] and len(out["steps"]) == 5
    assert w.drops == [] and w.policies == {} and w.opened["openbao_token"] == "root-tok"


def test_setup_hands_the_root_token_to_keydrop_before_changing_anything():
    w = World()
    out = run(w.ns["cap_secrets_setup"](apply="true"))
    assert out["ok"] and out["policy"] == "vera" and out["audit"] == "on" and out["keydrop_entry"] == 1
    kinds = [c[1] if c[0] != "keydrop" else "keydrop" for c in w.calls]
    assert kinds.index("keydrop") < kinds.index("sys/policies/acl/vera") < kinds.index("auth/token/create-orphan")
    label, payload = w.drops[0]
    assert payload["password"] == "root-tok" and payload["extra"] == {"unseal_key_1": "unseal-1"}
    assert "secret/data/vera/*" in w.policies["vera"]
    assert w.opened["openbao_token"] == "scoped-tok" and w.env_token == "scoped-tok"
    assert json.loads(w.redis.strings["vera:secrets:setup"])["keydrop_entry"] == 1
    assert "root-tok" not in json.dumps(out)


def test_setup_stops_before_any_change_when_keydrop_refuses():
    w = World(keydrop_ok=False)
    out = run(w.ns["cap_secrets_setup"](apply=True))
    assert "stopped before changing anything" in out["error"]
    assert w.policies == {} and w.opened["openbao_token"] == "root-tok"
    assert not any(c[1] == "auth/token/create-orphan" for c in w.calls)


def test_setup_leaves_an_already_limited_token_alone():
    w = World(token="scoped-tok")
    assert run(w.ns["cap_secrets_setup"](apply=True))["already_scoped"] is True
    assert w.drops == []


def test_named_secrets_go_in_and_to_keydrop_but_never_back_out():
    w = World(token="scoped-tok")
    out = run(w.ns["cap_secrets_put"](path="netctl/door-files", value="door-token-value", url="http://nwm-02",
                                      handoff=True, title="netctl door (files)"))
    assert out["ok"] and out["path"] == "netctl/door-files" and out["keydrop"]["ok"]
    assert "door-token-value" not in json.dumps(out)
    assert w.kv["netctl/door-files"]["value"] == "door-token-value"
    assert w.drops[0][1]["password"] == "door-token-value" and w.drops[0][1]["title"] == "netctl door (files)"
    assert run(w.ns["get_named"]("netctl/door-files"))["value"] == "door-token-value"
    listed = run(w.ns["cap_secrets_list"]())
    assert [s["path"] for s in listed["secrets"]] == ["netctl/door-files"]
    handed = run(w.ns["cap_secrets_handoff"](path="netctl/door-files"))
    assert handed["ok"] and "door-token-value" not in json.dumps(handed)


def test_named_secrets_need_openbao_and_a_valid_path():
    w = World(active=False)
    assert "OpenBao is not active" in run(w.ns["cap_secrets_put"](path="a/b", value="v"))["error"]
    w2 = World(token="scoped-tok")
    assert "path:" in run(w2.ns["cap_secrets_put"](path="../x", value="v"))["error"]
    assert run(w2.ns["cap_secrets_put"](path="a/b"))["error"] == "value required"


def test_delete_is_a_dry_run_until_confirmed():
    w = World(token="scoped-tok")
    run(w.ns["put_named"]("backup/ssh-logins/1", {"value": "x"}))
    assert run(w.ns["cap_secrets_delete"](path="backup/ssh-logins/1"))["dry_run"]
    assert "backup/ssh-logins/1" in w.kv
    assert run(w.ns["cap_secrets_delete"](path="backup/ssh-logins/1", confirm=True))["ok"]
    assert w.kv == {}


def seed_migration(w):
    w.redis.strings["vera:tg:config"] = json.dumps({"bot_token": "fernet:tg-secret", "chat": 1})
    w.redis.strings["vera:plain"] = "fernet:plain-secret"
    w.redis.strings["vera:cap:result:accounts.list"] = "fernet:cached"
    w.redis.hashes["vera:provisioning:state"] = {"main": json.dumps({"openbao_token": "fernet:root"})}
    w.redis.hashes["vera:accounts"] = {"a1": json.dumps({"password": "fernet:pw1"}), "a2": json.dumps({"n": 1})}
    w.hosts = {"pve01": {"label": "PVE01", "password_obf": "fernet:root-pw"},
               "llm": {"label": "LLM", "password_obf": "xorvalue"},
               "vfs": {"label": "VFS-02", "auth": "key"}}


def test_migrate_dry_run_counts_what_would_move():
    w = World(token="scoped-tok")
    seed_migration(w)
    out = run(w.ns["cap_secrets_migrate"]())
    assert out["dry_run"] and out["stored"] == {"fernet": 3, "bao": 0, "pinned": 1, "keys": 3}
    assert out["ssh_store"]["counts"] == {"fernet": 1, "bao": 0, "legacy": 1}
    assert w.redis.strings["vera:plain"] == "fernet:plain-secret" and w.saved_hosts is None


def test_migrate_moves_values_into_openbao_and_keeps_the_originals():
    w = World(token="scoped-tok")
    seed_migration(w)
    out = run(w.ns["cap_secrets_migrate"](apply=True))
    assert out["moved"] == 3 and out["ssh_moved"] == 2 and out["errors"] == []
    assert json.loads(w.redis.strings["vera:tg:config"]) == {"bot_token": "bao:v1:secret:vera-secrets/tg-secret", "chat": 1}
    assert w.redis.strings["vera:plain"] == "bao:v1:secret:vera-secrets/plain-secret"
    assert json.loads(w.redis.hashes["vera:accounts"]["a1"]) == {"password": "bao:v1:secret:vera-secrets/pw1"}
    assert w.redis.hashes["vera:provisioning:state"]["main"] == json.dumps({"openbao_token": "fernet:root"})
    assert w.redis.strings["vera:cap:result:accounts.list"] == "fernet:cached"
    backup = w.redis.hashes[out["backup_key"]]
    assert backup["vera:plain|"] == "fernet:plain-secret" and backup["ssh|llm|password_obf"] == "fernet:legacy-xorvalue"
    assert w.redis.ttl[out["backup_key"]] == 30 * 24 * 3600
    assert w.saved_hosts["pve01"]["password_obf"] == "bao:v1:secret:vera-secrets/root-pw"
    assert w.saved_hosts["llm"]["password_obf"] == "bao:v1:secret:vera-secrets/legacy-xorvalue"
    again = run(w.ns["cap_secrets_migrate"]())
    assert again["stored"]["fernet"] == 0 and again["stored"]["bao"] == 3


def test_a_value_that_changes_while_moving_is_left_alone():
    w = World(token="scoped-tok")
    seed_migration(w)
    w.redis.change_on_reread = "vera:plain"
    out = run(w.ns["cap_secrets_migrate"](apply=True))
    assert out["moved"] == 2 and any("vera:plain: changed while moving" in e for e in out["errors"])
    assert w.redis.strings["vera:plain"] == "fernet:changed-meanwhile"


def test_migrate_refuses_without_openbao():
    w = World(active=False)
    seed_migration(w)
    out = run(w.ns["cap_secrets_migrate"](apply=True))
    assert "nothing was moved" in out["error"] and w.redis.strings["vera:plain"] == "fernet:plain-secret"


def test_status_reports_the_limited_token_and_what_is_left():
    w = World(token="scoped-tok")
    seed_migration(w)
    out = run(w.ns["cap_secrets_status"](refresh=True))
    assert out["backend"] == "openbao" and out["openbao"]["storage"] == "file"
    assert out["token"]["root"] is False and out["token"]["renewable"] is True
    assert out["stored"]["fernet"] == 3 and out["ssh_store"]["legacy"] == 1
    assert [f["message"] for f in out["findings"]] == ["5 stored secret(s) are still sealed with the file key."]


def test_the_watcher_unseals_a_restarted_openbao_and_renews_the_token():
    w = World(token="scoped-tok", sealed=True)
    did = run(w.ns["_watch_tick"]())
    assert did["unsealed"] is True and ("call", "secstore.unseal") in w.calls
    assert did["renewed"] is True and ("POST", "auth/token/renew-self") in w.calls
    w2 = World(token="scoped-tok")
    w2.env_token = ""
    assert run(w2.ns["_watch_tick"]())["exported"] is True and w2.env_token == "scoped-tok"
    w3 = World(token="root-tok")
    assert "renewed" not in run(w3.ns["_watch_tick"]())
    assert "root token" in run(w3.ns["cap_secrets_renew"]())["error"]
