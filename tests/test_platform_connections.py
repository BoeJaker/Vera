"""API connections through the Platforms store (data-connections plan, owner 2026-10-11).

Every credential lives in the store: a literal secret typed into a platform is moved there and replaced by an
@secret: reference; a connection made from a template points at its named key; a request through a connection carries
the credential only to its own host, and a redirect elsewhere is refused rather than followed with it.
"""
import ast
import asyncio
import json
import os
import sys
import time
import types
from typing import Any, Dict, List, Optional

import httpx

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.platforms import connections_core as cc  # noqa: E402
from vera.platforms import platform_core as pc      # noqa: E402

SRC = os.path.join(ROOT, "vera", "platforms", "platform_capabilities.py")
WANTED = {"_hgetall", "_hset", "_no_store", "_secret_path", "_secrets_plain", "_values_plain", "_store_secret",
          "_vault_literals", "_resolved", "cap_upsert", "cap_secrets_list", "cap_secrets_set",
          "cap_secrets_vault_literals", "_connections", "_rate_wait", "connection_request", "connection_get",
          "_shape", "cap_connection_templates", "cap_connection_add", "cap_connection_test"}


class FakeRedis:
    def __init__(self):
        self.h: Dict[str, Dict[str, str]] = {}

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value


def world(handler=None):
    redis, named = FakeRedis(), {}

    async def put_named(path, fields):
        named[path] = dict(fields)
        return {"ok": True, "path": path}

    async def get_named(path):
        return named.get(path)

    svc = {"put_named": put_named, "get_named": get_named}
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(200, json={})))
    fake_httpx = types.SimpleNamespace(
        AsyncClient=lambda **kw: httpx.AsyncClient(transport=transport, **kw),
        URL=httpx.URL, HTTPError=httpx.HTTPError)
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
    assert {n.name for n in keep} == WANTED
    for n in keep:
        n.decorator_list = []
    ns = {"json": json, "asyncio": asyncio, "time": time, "httpx": fake_httpx, "Any": Any, "Dict": Dict,
          "List": List, "Optional": Optional, "pc": pc, "cc": cc, "_RATE": {}, "_CONSUMERS": {}, "_PLAIN": {},
          "KEY_VALUES": "vera:platform:values", "KEY_SECRETS": "vera:platform:secrets",
          "KEY_TARGETS": "vera:platform:targets", "_redis": lambda: redis, "_secrets_service": lambda: svc,
          "now_iso": lambda: "2026-10-11T12:00:00Z", "log": types.SimpleNamespace(warning=lambda *a, **k: None),
          "vsecrets": types.SimpleNamespace(seal=lambda v: "fernet:" + v, open_secret=lambda v: v[7:])}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    ns["_named"], ns["_redis_obj"] = named, redis
    return ns


def run(c):
    """On a private loop: asyncio.run() would leave the main thread without one for later tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def test_a_literal_secret_typed_into_a_platform_goes_to_the_store():
    ns = world()
    out = run(ns["cap_upsert"](kind="n8n", id="n8n", fields={"base_url": "http://n8n:5678", "api_key": "LITERAL"}))
    assert out["vaulted"] == ["n8n_api_key"]
    stored = json.loads(ns["_redis_obj"].h["vera:platform:targets"]["n8n"])
    assert stored["fields"]["api_key"] == "@secret:n8n_api_key"
    assert "LITERAL" not in json.dumps(ns["_redis_obj"].h)                # not in redis at all
    assert ns["_named"]["platform/n8n_api_key"]["value"] == "LITERAL"
    # a reference stays a reference
    out = run(ns["cap_upsert"](id="n8n", fields={"api_key": "@secret:shared_n8n"}))
    assert out["vaulted"] == []


def test_vault_literals_sweep_is_a_dry_run_by_default():
    ns = world()
    rec = pc.new_platform("n8n", "n8n")
    rec["fields"]["api_key"] = "OLD"
    run(ns["_hset"]("vera:platform:targets", "n8n", rec))
    dry = run(ns["cap_secrets_vault_literals"]())
    assert dry["moved"] == [{"platform": "n8n", "keys": ["n8n_api_key"]}]
    assert "OLD" in ns["_redis_obj"].h["vera:platform:targets"]["n8n"]   # untouched
    run(ns["cap_secrets_vault_literals"](dry_run=False))
    assert "OLD" not in ns["_redis_obj"].h["vera:platform:targets"]["n8n"]


def test_connection_from_a_template_references_its_key_and_lists_it_as_wanted():
    ns = world()
    out = run(ns["cap_connection_add"](template="guardian"))
    assert out["ok"] and out["platform"]["secret_refs"] == {"credential": "@secret:guardian_api_key"}
    tpl = {t["id"]: t for t in run(ns["cap_connection_templates"]())["templates"]}
    assert tpl["guardian"]["added"] == "guardian" and not tpl["guardian"]["key_set"]
    wanted = [s for s in run(ns["cap_secrets_list"]())["secrets"] if s.get("wanted")]
    assert [s["key"] for s in wanted] == ["guardian_api_key"]
    # until the key is set the connection says so, and sends nothing
    req = run(ns["connection_request"]("guardian", "/search?q=x"))
    assert "guardian_api_key" in req["error"]
    run(ns["cap_secrets_set"](key="guardian_api_key", value="GKEY"))
    assert run(ns["cap_connection_templates"]())["templates"][2]["key_set"]


def test_a_hand_made_connection_vaults_the_credential_given_to_it():
    ns = world()
    out = run(ns["cap_connection_add"](base_url="https://api.example.com/v2", auth="header:X-Key", credential="K1"))
    assert out["platform"]["id"] == "example" and out["vaulted"] == ["example_credential"]
    assert "K1" not in json.dumps(ns["_redis_obj"].h)
    assert "error" in run(ns["cap_connection_add"](base_url="https://api.example.com/v2"))      # id taken
    assert "error" in run(ns["cap_connection_add"](base_url="https://x.org", id="x2", headers="[1]"))


def test_requests_carry_the_credential_and_tests_never_show_it():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"response": {"results": [{"id": 1}, {"id": 2}], "echo": "QKEY"}})

    ns = world(handler)
    run(ns["cap_connection_add"](template="guardian"))
    run(ns["cap_secrets_set"](key="guardian_api_key", value="QKEY"))
    out = run(ns["cap_connection_test"](id="guardian"))
    assert out["ok"] and seen[0].url.params["api-key"] == "QKEY"
    assert out["shape"]["item_path"] == "response.results" and out["shape"]["items"] == 2
    assert "QKEY" not in json.dumps(out)


def test_a_redirect_to_another_host_is_not_followed_with_the_credential():
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://evil.example.net/steal"})
        return httpx.Response(200)

    ns = world(handler)
    run(ns["cap_connection_add"](template="github", credential="GHTOKEN"))
    out = run(ns["connection_get"]("github", "/rate_limit"))
    assert "another host" in out["error"] and hosts == ["api.github.com"]


def test_disabled_and_non_connection_platforms_refuse():
    ns = world()
    run(ns["cap_upsert"](kind="n8n", id="n8n", fields={"base_url": "http://n8n"}))
    assert "not an API connection" in run(ns["connection_request"]("n8n"))["error"]
    run(ns["cap_connection_add"](template="hackernews"))
    run(ns["cap_upsert"](id="hackernews", enabled=False))
    assert "disabled" in run(ns["connection_request"]("hackernews"))["error"]
