"""secstore.kv.get is reachable over the open API, and the vault now holds every
module credential and the Redis ACL passwords. It must return only whether a
secret exists and its field names; Vera's own modules read values in process
through vera/security/secret_reads.py.

The real kv_read / cap_kv_get bodies are run against a fake OpenBao (the
function definitions are taken from the module source and executed with fakes
for its I/O helpers, the same way test_secrets_capabilities.py does it).
"""
import ast
import asyncio
import json
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "vera", "provisioning", "provisioning_capabilities.py")

from vera.security import secret_reads  # noqa: E402

SECRET = {"password": "hunter2-very-secret", "user": "vera-node"}


def _load(kv):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = []
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name in ("kv_read", "cap_kv_get"):
            n.decorator_list = []
            keep.append(n)
    assert {n.name for n in keep} == {"kv_read", "cap_kv_get"}

    async def _state_opened():
        return {"openbao_mount": "secret"}

    async def _bao(st, method, path):
        key = path.split("/data/", 1)[1]
        if key not in kv:
            return {"_status": 404}, ""
        return {"data": {"data": kv[key]}}, ""

    ns = {"Dict": dict, "_state_opened": _state_opened, "_bao": _bao}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns


def run(c):
    return asyncio.run(c)


def test_capability_returns_field_names_never_values():
    ns = _load({"vera/redis/users/vera-node": SECRET})
    r = run(ns["cap_kv_get"](path="vera/redis/users/vera-node"))
    assert r == {"ok": True, "present": True, "keys": ["password", "user"]}
    assert "hunter2" not in json.dumps(r)


def test_missing_and_bad_input():
    ns = _load({})
    assert run(ns["cap_kv_get"](path="vera/nothing")) == {"ok": False}
    assert "error" in run(ns["cap_kv_get"](path=""))


def test_in_process_reader_still_gets_the_value():
    ns = _load({"vera/pki/host.example": {"fqdn": "host.example", "issued_at": "2026-10-17"}})
    r = run(ns["kv_read"]("vera/pki/host.example"))
    assert r["ok"] and r["data"]["fqdn"] == "host.example"


def test_secret_reads_uses_the_registered_reader_and_fails_closed():
    secret_reads._READER = None
    assert "error" in run(secret_reads.read("vera/x"))
    ns = _load({"vera/redis/users/vera-node": SECRET})
    secret_reads.set_reader(ns["kv_read"])
    try:
        r = run(secret_reads.read("vera/redis/users/vera-node"))
        assert r["ok"] and r["data"]["password"] == SECRET["password"]
    finally:
        secret_reads._READER = None
