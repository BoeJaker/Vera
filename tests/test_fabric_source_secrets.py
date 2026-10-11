"""Fabric sources keep credentials in the Platforms store (data-connections plan, owner 2026-10-11).

A literal key in a source's headers or auth is moved into the store and replaced by an @secret: reference; at pull
time the puller gets a resolved copy, and nothing that writes the source back (the recon puller does) can persist the
resolved value. A key written into the URL is reported, not touched. A source can read through an API connection.
"""
import ast
import asyncio
import json
import os
import re
import sys
import types
from typing import Any, Dict, List

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.fabric import source_secrets_core as ssc  # noqa: E402

FABRIC = os.path.join(ROOT, "vera", "fabric", "data_fabric.py")
DISCOVERY = os.path.join(ROOT, "vera", "fabric", "discovery.py")


def run(c):
    """On a private loop: asyncio.run() would leave the main thread without one for later tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


# ── the pure plan ────────────────────────────────────────────────────────────
def test_secret_names():
    for n in ("Authorization", "X-Api-Key", "X-Subscription-Token", "api_key", "apikey", "access_token", "token",
              "password", "client_secret", "Cookie"):
        assert ssc.is_secret_name(n), n
    for n in ("Accept", "User-Agent", "username", "email", "Content-Type", "key_id", "X-Request-Id"):
        assert not ssc.is_secret_name(n), n


def test_plan_moves_literals_and_leaves_references_env_and_plain_headers():
    src = {"id": "abc123", "label": "My Feed", "url": "https://x.org/feed?apiKey=K9&q=1",
           "headers": json.dumps({"Authorization": "Bearer T1", "Accept": "application/json",
                                  "X-Api-Key": "@secret:already", "X-Token": "${MY_TOKEN}"}),
           "auth": json.dumps({"token": "G1", "username": "me"})}
    p = ssc.plan(src)
    assert p["headers"] == {"Authorization": "fabric_my_feed_authorization"}
    assert p["auth"] == {"token": "fabric_my_feed_token"}
    assert p["url_params"] == ["apiKey"]
    vals = {v["key"]: v["value"] for v in ssc.values(src, p)}
    assert vals == {"fabric_my_feed_authorization": "T1", "fabric_my_feed_token": "G1"}   # "Bearer " not stored
    out = ssc.apply(src, p)
    h, a = json.loads(out["headers"]), json.loads(out["auth"])
    assert h["Authorization"] == "Bearer @secret:fabric_my_feed_authorization"
    assert h["X-Api-Key"] == "@secret:already" and h["X-Token"] == "${MY_TOKEN}" and h["Accept"] == "application/json"
    assert a == {"token": "@secret:fabric_my_feed_token", "username": "me"}
    assert "T1" not in json.dumps(out["headers"]) and "G1" not in out["auth"]


def test_plan_of_a_clean_source_is_empty():
    p = ssc.plan({"id": "s", "headers": "{}", "auth": "", "url": "https://x.org/rss"})
    assert not ssc.anything(p) and p["url_params"] == []
    assert ssc.plan({"id": "s", "headers": "not json"})["headers"] == {}


def test_keys_are_valid_store_keys():
    k = ssc.key_for({"label": "A very long label for an API source that goes on and on"}, "h", "X-Subscription-Token")
    assert re.fullmatch(r"[a-z0-9_]{1,64}", k)
    assert ssc.key_for({"id": "9f3e-11", "label": ""}, "a", "token") == "fabric_9f3e_11_token"


# ── the fabric glue, extracted from data_fabric.py ───────────────────────────
WANTED = {"_platform_access", "_stored_form", "_secure_source", "_with_secrets", "_src_auth", "_resolve_env"}


def fabric_ns(store: Dict[str, str]):
    tree = ast.parse(open(FABRIC, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
    assert {n.name for n in keep} == WANTED

    async def secret_value(reg, ref):
        return store.get(ref.replace("@secret:", ""), "")

    async def store_secret(reg, key, value, label=""):
        store[key] = value
        return True

    pa = types.SimpleNamespace(is_ref=lambda v: isinstance(v, str) and v.startswith("@secret:"),
                               secret_value=secret_value, store_secret=store_secret)
    ns = {"json": json, "os": os, "re": re, "Any": Any, "Dict": Dict, "List": List,
          "_ENV_REF_RE": re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"),
          "_orch": types.SimpleNamespace(CAPABILITY_REGISTRY={}),
          "log": types.SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None)}
    exec(compile(ast.Module(body=keep, type_ignores=[]), FABRIC, "exec"), ns)
    ns["_platform_access"] = lambda: pa
    return ns


def test_secure_moves_into_the_store_and_pullers_get_a_resolved_copy_that_cannot_be_persisted():
    store: Dict[str, str] = {}
    ns = fabric_ns(store)
    src = {"id": "s1", "label": "gh", "headers": "{}", "auth": json.dumps({"token": "GHP"})}
    stored, rep = run(ns["_secure_source"](src))
    assert rep["keys"] == ["fabric_gh_token"] and store == {"fabric_gh_token": "GHP"}
    assert json.loads(stored["auth"]) == {"token": "@secret:fabric_gh_token"}
    psrc = run(ns["_with_secrets"](stored))
    assert ns["_src_auth"](psrc) == {"token": "GHP"}                    # the puller sees the key
    back = ns["_stored_form"]({**psrc, "page": "x"})                      # what recon writes back
    assert "GHP" not in json.dumps(back) and back["page"] == "x" and "_auth_stored" not in back
    assert run(ns["_with_secrets"]({"id": "s2", "auth": ""})) == {"id": "s2", "auth": ""}


def test_secure_dry_run_changes_nothing():
    store: Dict[str, str] = {}
    ns = fabric_ns(store)
    src = {"id": "s1", "label": "x", "headers": json.dumps({"X-Api-Key": "K"})}
    out, rep = run(ns["_secure_source"](src, dry=True))
    assert out is src and rep["keys"] == ["fabric_x_x_api_key"] and store == {}


def test_writer_and_pull_dispatch_are_wired():
    s = open(FABRIC, encoding="utf-8").read()
    up = s[s.index("async def _sqlite_upsert_source"):s.index("@dataclass", s.index("async def _sqlite_upsert_source"))]
    assert "_stored_form(src)" in up
    disp = s[s.index("async def _pull_source(src: dict)"):s.index("except httpx.HTTPStatusError", s.index("async def _pull_source(src: dict)"))]
    assert "psrc = await _with_secrets(src)" in disp and "(src)" not in disp.replace("_with_secrets(src)", "")
    api = s[s.index("async def _pull_api"):s.index("async def _pull_telegram")]
    assert 'cfg.get("connection")' in api and "connection_get" in api
    add = s[s.index("async def fabric_sources_add("):s.index('"fabric.sources.add_index"')]
    assert "await _secure_source(src)" in add and "connection_info" in add


def test_api_map_finds_items_one_level_down_and_offers_the_source():
    tree = ast.parse(open(DISCOVERY, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_map_answer", "_json_type", "_short_ex")]
    ns = {"Any": Any, "Dict": Dict, "List": List, "json": json}
    exec(compile(ast.Module(body=keep, type_ignores=[]), DISCOVERY, "exec"), ns)
    out = ns["_map_answer"]({"response": {"status": "ok", "results": [{"id": 1, "webTitle": "a"}]}},
                            "https://content.guardianapis.com/search?api-key=***", 5,
                            {"connection": "guardian", "path": "/search", "source_type": "api"})
    assert out["suggested_jq"] == "response.results" and out["count"] == 1
    assert out["add_source"] == {"connection": "guardian", "path": "/search", "source_type": "api",
                                 "jq_path": "response.results"}


def test_whole_mode_and_connection_paths_are_wired():
    s = open(FABRIC, encoding="utf-8").read()
    api = s[s.index("async def _pull_api"):s.index("async def _pull_telegram")]
    assert 'cfg.get("whole")' in api and "(latest)" in api          # one record, fixed text -> replaced each pull
    pc = open(os.path.join(ROOT, "vera", "platforms", "platform_capabilities.py"), encoding="utf-8").read()
    assert "cc.build_request(fields, cc.expand_path(path), params)" in pc
