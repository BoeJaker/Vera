"""Research sources keep their keys in the Platforms store (data-connections plan, owner 2026-10-11).

A literal api_key / token / password saved into a source goes to the store as research_<id>_<field>; the list shows
bullets, bullets posted back keep what is stored, and every place that reads a key resolves the reference.
"""
import ast
import asyncio
import json
import os
import re
import types
from dataclasses import dataclass, field

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "vera", "research", "researcher_api.py")
WANTED = {"_src_cfg_dict", "_src_redacted", "_cfg_secret", "_secure_sources"}
BULLETS = "•" * 8


@dataclass
class DataSource:
    id: str
    label: str
    config: dict = field(default_factory=dict)


def run(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def ns_with(store):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign))
            and (getattr(n, "name", "") in WANTED
                 or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("_SRC_SECRET_FIELDS", "_SRC_BULLETS")
                                                       for t in n.targets)))]
    assert len(keep) == len(WANTED) + 2

    async def store_secret(reg, key, value, label=""):
        store[key] = value
        return True

    async def resolve(reg, value, default_key=""):
        v = str(value or "").strip()
        if v and not v.startswith("@secret:"):
            return v
        return store.get((v or "@secret:" + default_key).replace("@secret:", ""), "")

    pa = types.SimpleNamespace(store_secret=store_secret, resolve=resolve)
    ns = {"json": json, "re": re, "CAPABILITY_REGISTRY": {}, "log": types.SimpleNamespace(debug=lambda *a, **k: None)}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    ns["_platform_access"] = lambda: pa
    return ns


def test_saving_moves_keys_and_the_list_never_shows_them():
    store = {}
    ns = ns_with(store)
    gh = DataSource("github", "GitHub", {"token": "ghp_LIT"})
    neo = DataSource("neo4j", "Neo4j Graph", {"uri": "bolt://x", "user": "neo4j", "password": ""})
    moved = run(ns["_secure_sources"]([gh, neo], []))
    assert moved == ["research_github_token"] and store == {"research_github_token": "ghp_LIT"}
    assert gh.config["token"] == "@secret:research_github_token" and neo.config["password"] == ""
    assert run(ns["_cfg_secret"](gh.config, "token", "github_token")) == "ghp_LIT"


def test_bullets_posted_back_keep_the_stored_value():
    store = {}
    ns = ns_with(store)
    old = [DataSource("redis", "Redis", {"password": "@secret:research_redis_password"})]
    new = [DataSource("redis", "Redis", {"password": BULLETS, "db": 1})]
    run(ns["_secure_sources"](new, old))
    assert new[0].config == {"password": "@secret:research_redis_password", "db": 1}
    # an older literal shown as bullets and posted back is not lost either
    old = [DataSource("brave", "Brave", {"api_key": "BSK-LIT"})]
    new = [DataSource("brave", "Brave", {"api_key": BULLETS})]
    run(ns["_secure_sources"](new, old))
    assert new[0].config["api_key"] == "@secret:research_brave_api_key" and store["research_brave_api_key"] == "BSK-LIT"


def test_redacted_list_and_defaults():
    ns = ns_with({"brave_search_api_key": "MASTER"})
    shown = ns["_src_redacted"]({"api_key": "LIT", "token": "@secret:x", "host": "h"})
    assert shown == {"api_key": BULLETS, "token": "@secret:x", "host": "h"}
    assert ns["_src_redacted"]('{"password": "p"}') == {"password": BULLETS}         # config stored as a string
    assert run(ns["_cfg_secret"]({"api_key": ""}, "api_key", "brave_search_api_key")) == "MASTER"
    assert run(ns["_cfg_secret"]({}, "password")) == ""


def test_dry_run_and_every_read_site_resolves():
    store = {}
    ns = ns_with(store)
    s = DataSource("github", "GitHub", {"token": "T"})
    assert run(ns["_secure_sources"]([s], [s], dry=True)) == ["research_github_token"] and s.config["token"] == "T"
    src = open(SRC, encoding="utf-8").read()
    for raw in ('src.config.get("password"', 'src.config.get("token"', 'cfg.get("api_key","")',
                'cfg.get("password"', 'cfg.get("token","")'):
        assert raw not in src, raw
    for cap in ("def cap_sources(", "async def get_sources("):
        body = src[src.index(cap):][:700]
        assert "_src_redacted(" in body, cap
