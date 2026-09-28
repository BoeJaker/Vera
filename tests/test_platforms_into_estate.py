"""Platforms folded into Estate: its credentials live in the secrets service
(falling back to an inline seal where that service is missing, and still reading
older inline records), deleting one removes the named secret too, the Platforms
pane sits under Estate > Integrations with the old tab retired into it, and
Trust has a Secrets view served by the secrets service."""
import ast
import asyncio
import json
import os
import re
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_nav_core as nav  # noqa: E402
from vera.platforms import platform_core as pc  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "platforms", "platform_capabilities.py")
PANEL = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")
SECRETS_SRC = os.path.join(ROOT, "vera", "security", "secrets_capabilities.py")
SECRETS_PANEL = os.path.join(ROOT, "vera", "security", "secrets_panel.html")
WANTED = {"_hgetall", "_hset", "_no_store", "_secret_path", "_secrets_plain",
          "cap_secrets_list", "cap_secrets_set", "cap_secrets_delete"}


class FakeRedis:
    def __init__(self):
        self.h: Dict[str, Dict[str, str]] = {}

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)


class World:
    def __init__(self, service=True, openbao_active=True):
        self.redis = FakeRedis()
        self.named: Dict[str, Dict[str, Any]] = {}
        world = self

        async def put_named(path, fields):
            if not openbao_active:
                return {"error": "OpenBao is not active for Vera"}
            world.named[path] = dict(fields)
            return {"ok": True, "path": path, "version": 1}

        async def get_named(path):
            return world.named.get(path)

        async def delete_named(path):
            world.named.pop(path, None)
            return {"ok": True, "path": path}

        svc = {"put_named": put_named, "get_named": get_named, "delete_named": delete_named} if service else None
        tree = ast.parse(open(SRC, encoding="utf-8").read())
        keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
        for n in keep:
            n.decorator_list = []
        self.ns = {"json": json, "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "pc": pc,
                   "KEY_VALUES": "vera:platform:values", "KEY_SECRETS": "vera:platform:secrets",
                   "KEY_TARGETS": "vera:platform:targets", "_redis": lambda: world.redis,
                   "_secrets_service": lambda: svc, "now_iso": lambda: "2026-09-13T16:00:00Z",
                   "log": types.SimpleNamespace(warning=lambda *a, **k: None),
                   "vsecrets": types.SimpleNamespace(seal=lambda v: "fernet:" + v,
                                                     open_secret=lambda v: v[len("fernet:"):])}
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), self.ns)

    def record(self, key):
        return json.loads(self.redis.h["vera:platform:secrets"][key])


def run(coro):
    return asyncio.run(coro)


def test_a_credential_goes_into_the_secrets_service_and_redis_keeps_no_value():
    w = World()
    out = run(w.ns["cap_secrets_set"](key="ha_token", value="long-lived-token", label="Home Assistant"))
    assert out["ok"] and out["store"] == "secrets service" and "long-lived-token" not in json.dumps(out)
    rec = w.record("ha_token")
    assert rec["path"] == "platform/ha_token" and "value" not in rec
    assert w.named["platform/ha_token"]["value"] == "long-lived-token"
    listed = run(w.ns["cap_secrets_list"]())["secrets"][0]
    assert (listed["set"], listed["store"], listed["path"]) == (True, "secrets service", "platform/ha_token")
    assert run(w.ns["_secrets_plain"]()) == {"ha_token": "long-lived-token"}


def test_without_openbao_or_the_service_it_seals_inline_and_says_so():
    for w in (World(openbao_active=False), World(service=False)):
        out = run(w.ns["cap_secrets_set"](key="n8n_key", value="k1"))
        assert out["ok"] and out["store"] == "sealed inline"
        assert w.record("n8n_key")["value"] == "fernet:k1" and "path" not in w.record("n8n_key")
        assert run(w.ns["_secrets_plain"]()) == {"n8n_key": "k1"}


def test_an_older_inline_record_still_resolves_beside_new_ones():
    w = World()
    w.redis.h["vera:platform:secrets"] = {"old": json.dumps({"key": "old", "value": "fernet:legacy"})}
    run(w.ns["cap_secrets_set"](key="new", value="fresh"))
    assert run(w.ns["_secrets_plain"]()) == {"old": "legacy", "new": "fresh"}
    stores = {s["key"]: s["store"] for s in run(w.ns["cap_secrets_list"]())["secrets"]}
    assert stores == {"old": "sealed inline", "new": "secrets service"}


def test_deleting_removes_the_named_secret_and_respects_references():
    w = World()
    run(w.ns["cap_secrets_set"](key="ha_token", value="t"))
    w.redis.h["vera:platform:targets"] = {"ha": json.dumps({"id": "ha", "kind": "homeassistant",
                                                             "fields": {"token": pc.SECRET_REF + "ha_token"}})}
    refused = run(w.ns["cap_secrets_delete"](key="ha_token"))
    assert "still referenced" in refused["error"] and "platform/ha_token" in w.named
    out = run(w.ns["cap_secrets_delete"](key="ha_token", force=True))
    assert out["ok"] and out["removed_from_secrets_service"] is True
    assert w.named == {} and "ha_token" not in w.redis.h["vera:platform:secrets"]


def test_platforms_opens_inside_estate_integrations():
    html = open(PANEL, encoding="utf-8").read()
    assert nav.RETIRED_TABS["platform-config"] == {"panel": "workers-ollama", "pane": "platforms", "sub": "",
                                                   "section": "Integrations"}
    assert 'id="pane-platforms"' in html and 'data-pane="platforms" data-view="estate"' in html
    assert "_mountFrame('platforms-frame', BASE+'/platform/panel')" in html
    start = html.index('<aside id="sidebar" data-vera-lhm>')
    nav_block = html[start:html.index('data-pane="ollama"', start)]
    assert nav_block.index('>Integrations</div>') < nav_block.index('data-pane="integrations"') < nav_block.index('data-pane="platforms"')


def test_trust_has_a_secrets_view_that_never_asks_for_values():
    html = open(PANEL, encoding="utf-8").read()
    subs = re.search(r"const _prvSubSrc=\{([^}]*)\}", html).group(1)
    assert "secrets:'/secrets/panel'" in subs and 'id="prv-sub-secrets"' in html and 'id="prvs-secrets"' in html
    assert '@APP.get("/secrets/panel", include_in_schema=False)' in open(SECRETS_SRC, encoding="utf-8").read()
    panel = open(SECRETS_PANEL, encoding="utf-8").read()
    called = set(re.findall(r"cap\('([a-z_.]+)'", panel))
    assert called == {"secrets.status", "secrets.migrate", "secrets.list", "secrets.handoff", "secrets.delete",
                      "secrets.put", "exec.ssh.hosts.cleanup"}
    # Values are only ever typed in (args.value, the form fields); no answer's value is shown.
    assert not re.search(r"(r|s|st|sec|secret|i|a|c)\.value", panel)
