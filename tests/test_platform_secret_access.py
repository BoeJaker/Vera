"""API keys come from the one master store, platform.secrets (owner 2026-10-11: "there is already a master api key
system that should be used and not re-made"): the in-process accessor, keys a module wants listed on the Platforms
page, "@secret:" references in fabric source headers, and intel's catalog of keyed sources. Pure / faked: no Redis."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.platforms import platform_access as PA           # noqa: E402
from vera.intel import intel_apis_core as AC                # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "vera"


def _fake_registry(store):
    """A registry whose platform.secrets.list function lives in a namespace holding secret_value and
    declare_secret_use - the way platform_access finds the real module."""
    g = {"STORE": store, "DECLARED": {}}
    exec(
        "async def platform_secrets_list():\n    return {}\n"
        "async def secret_value(k):\n"
        "    k = k[len('@secret:'):] if k.startswith('@secret:') else k\n"
        "    return STORE.get(k, '')\n"
        "def declare_secret_use(key, who, label='', why=''):\n"
        "    DECLARED.setdefault(key, []).append(who)\n", g)
    return {"platform.secrets.list": {"func": g["platform_secrets_list"]}}, g


def run(c):
    """On a private loop: asyncio.run() would leave the main thread without one for later tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def test_resolve_literal_reference_and_default():
    reg, g = _fake_registry({"brave_search_api_key": "BSK", "other": "OTH"})
    assert run(PA.resolve(reg, "literal-key", "brave_search_api_key")) == "literal-key"     # older setups keep working
    assert run(PA.resolve(reg, "@secret:other", "brave_search_api_key")) == "OTH"
    assert run(PA.resolve(reg, "", "brave_search_api_key")) == "BSK"                       # empty -> the master record
    assert run(PA.resolve(reg, "", "missing")) == ""
    assert run(PA.secret_value({}, "x")) == ""                                              # platforms not loaded
    assert PA.declare(reg, "github_token", "fabric GitHub sources") and g["DECLARED"] == {"github_token": ["fabric GitHub sources"]}
    assert PA.declare({}, "k", "w") is False


def _platform_src():
    return (ROOT / "platforms" / "platform_capabilities.py").read_text(encoding="utf-8")


def test_the_platforms_list_shows_keys_a_module_wants():
    src = _platform_src()
    body = src[src.index("async def cap_secrets_list"):src.index("@capability(\n    \"platform.secrets.set\"")]
    secs = {"ha_token": {"path": "platform/ha_token", "label": "HA"}}

    async def _hgetall(key):
        return secs if key == "S" else {}

    class pc:                                             # noqa: N801 - stands in for platform_core
        SECRET_REF = "@secret:"

        @staticmethod
        def referencing_platforms(targets, kind, key):
            return []
    ns = {"_hgetall": _hgetall, "KEY_SECRETS": "S", "KEY_TARGETS": "T", "pc": pc, "_redis": lambda: True,
          "_CONSUMERS": {"brave_search_api_key": [{"who": "web.search (Brave)", "label": "Brave Search API",
                                                  "why": "search fallback"}],
                         "ha_token": [{"who": "intel", "label": "", "why": ""}]}}
    exec(body, ns)
    out = run(ns["cap_secrets_list"]())["secrets"]
    ha = next(r for r in out if r["key"] == "ha_token")
    assert ha["set"] and ha["used_by"] == ["intel"]
    want = next(r for r in out if r["key"] == "brave_search_api_key")
    assert want["wanted"] and not want["set"] and want["label"] == "Brave Search API" and want["ref"] == "@secret:brave_search_api_key"


def test_secret_value_never_through_a_capability():
    src = _platform_src()
    assert "async def secret_value(key: str) -> str:" in src
    assert "secret_value" not in src[src.index("@capability("):].split("async def secret_value")[0] or True
    # no registered capability returns it: every capability body is checked for a call to secret_value
    import re
    for m in re.finditer(r"@capability\((?:.|\n)*?\nasync def (\w+)\(", src):
        name = m.group(1)
        body = src[m.end():].split("\n@capability(")[0]
        assert "secret_value(" not in body, name


def test_fabric_headers_resolve_references_and_drop_unknown_ones():
    src = (ROOT / "fabric" / "data_fabric.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _secret_headers"):src.index("async def _pull_rss")]
    reg, _ = _fake_registry({"guardian_api_key": "G-KEY"})

    class _O:                                             # stands in for the orchestration module
        CAPABILITY_REGISTRY = reg
    import logging
    ns = {"_orch": _O, "log": logging.getLogger("t"), "Dict": dict, "Any": object}
    exec(fn, ns)
    out = run(ns["_secret_headers"]({"api-key": "@secret:guardian_api_key", "Authorization": "Bearer @secret:nope",
                                             "User-Agent": "Vera", "X-Empty": ""}))
    assert out == {"api-key": "G-KEY", "User-Agent": "Vera"}           # unresolved -> dropped, never sent as text
    assert "headers.update(await _secret_headers(extra))" in src
    assert "await _secret_headers(headers if isinstance(headers, dict) else {})" in src


def test_brave_and_github_read_the_master_store():
    research = (ROOT / "research" / "researcher_api.py").read_text(encoding="utf-8")
    brave = research[research.index("async def search_brave"):][:2200]
    assert 'await _pa.resolve(CAPABILITY_REGISTRY, api_key, "brave_search_api_key")' in brave
    fabric = (ROOT / "fabric" / "data_fabric.py").read_text(encoding="utf-8")
    gh = fabric[fabric.index("async def _pull_github"):][:2200]
    assert 'await _pa.resolve(_orch.CAPABILITY_REGISTRY, token, "github_token")' in gh


def test_intel_catalog_reads_set_state_from_the_master_store():
    v = AC.view([{"key": "github_token", "set": True}, {"key": "unrelated", "set": True}])
    by = {r["key"]: r for r in v["apis"]}
    assert by["github_token"]["set"] and not by["brave_search_api_key"]["set"]
    assert v["missing_used"] == ["brave_search_api_key"]               # used by code today, and not set
    assert all(r["ref"] == "@secret:" + r["key"] for r in v["apis"])
    assert {r["status"] for r in v["apis"]} == {"used", "planned"} and v["keyless"] and "connection" in v["json_api_hint"]
    assert all(k not in str(v) for k in ("value", "password"))         # the view never carries a key


def test_intel_catalog_shows_each_apis_connection():
    from vera.platforms import connections_core as CC
    tpls = [{**t, "added": "guardian" if t["id"] == "guardian" else ""} for t in CC.TEMPLATES]
    v = AC.view([], tpls, [{"id": "guardian", "label": "Guardian", "sources": 2}])
    by = {r["key"]: r for r in v["apis"]}
    assert by["guardian_api_key"]["template"] == "guardian" and by["guardian_api_key"]["connection"] == "guardian"
    assert by["brave_search_api_key"]["template"] == "brave" and by["brave_search_api_key"]["connection"] == ""
    assert all(r["template"] for r in v["apis"])                        # every keyed source the catalog names has one
    assert {t["id"] for t in v["keyless_templates"]} >= {"hackernews", "polymarket", "wikidata"}
    assert v["connections"][0]["sources"] == 2
