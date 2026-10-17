"""The hooked client (web-ingress WI-9a): a plain httpx client whose responses leave receipts and whose requests wait
for the host's slot - the transport the fabric's source pullers now use. Tested against the fixture site through
MockTransport with the capabilities module loaded under a stand-in orchestration."""
import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "web-ingress-bench"))

import fixture_site as site  # noqa: E402


def _load_ingress(monkeypatch):
    """ingress_capabilities under a fake orchestration: no app, no Redis, no Postgres."""
    import importlib.util
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}
    orch.COORD_REDIS = None

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "raw": fn}
            return fn
        return deco

    orch.capability = capability
    orch.schedule = lambda *a, **k: None

    async def _ensure():
        return None
    orch._ensure_coord_redis = _ensure
    sys.path.insert(0, str(ROOT / "tests"))
    from orchestration_stub import stubbed_orchestration
    from vera.web import web_client as wc
    spec = importlib.util.spec_from_file_location("ingress_under_test", ROOT / "vera" / "fabric" / "ingress_capabilities.py")
    mod = importlib.util.module_from_spec(spec)
    with stubbed_orchestration(orch):                  # the Vera.vera.* imports fall back to vera.* (ImportError path)
        spec.loader.exec_module(mod)
    return mod, mod._wc                               # whichever spelling of web_client the module bound


def test_hooked_client_records_every_response_and_waits_for_the_slot(monkeypatch):
    httpx = pytest.importorskip("httpx")
    mod, wc = _load_ingress(monkeypatch)
    monkeypatch.setitem(mod._SW, "mode", "on")
    monkeypatch.setitem(mod._SW, "at", 10 ** 12)                 # never refresh from a switchboard that is not there
    waited = []

    async def slot(domain):
        waited.append(domain)
        return 0.0
    monkeypatch.setattr(wc, "THROTTLE_HOOK", slot)
    got = []
    monkeypatch.setattr(mod.WRITER, "enqueue", lambda item: got.append(item) or True)

    async def go():
        c = mod.hooked_client(caller="fabric.sources", purpose="feed", transport=site.mock_transport())
        async with c:
            r1 = await c.get("http://fixture.test/feed.rss")
            r2 = await c.get("http://fixture.test/old")                   # a redirect chain: three responses
        return r1, r2

    r1, r2 = asyncio.new_event_loop().run_until_complete(go())
    assert r1.status_code == 200 and "<rss" in r1.text and r2.status_code == 200
    assert waited[:1] == ["fixture.test"] and len(waited) == 4            # one wait per request, redirects included
    assert len(got) == 4 and got[0]["caller"] == "fabric.sources" and got[0]["purpose"] == "feed"
    assert got[0]["content_type"].startswith("application/rss+xml") and got[0]["text"].startswith("<?xml")
    assert got[1]["status"] == 301 and got[-1]["final_url"] == "http://fixture.test/d"


def test_hooked_client_is_silent_when_recording_is_off(monkeypatch):
    pytest.importorskip("httpx")
    mod, wc = _load_ingress(monkeypatch)
    monkeypatch.setitem(mod._SW, "mode", "off")
    monkeypatch.setitem(mod._SW, "at", 10 ** 12)
    got = []
    monkeypatch.setattr(mod.WRITER, "enqueue", lambda item: got.append(item) or True)

    async def go():
        async with mod.hooked_client(caller="x", transport=site.mock_transport()) as c:
            return await c.get("http://fixture.test/a")

    r = asyncio.new_event_loop().run_until_complete(go())
    assert r.status_code == 200 and got == []


def test_the_pullers_use_the_ingress_client():
    src = (ROOT / "vera" / "fabric" / "data_fabric.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    pullers = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("_pull_")]
    assert len(pullers) >= 10
    bare = [n.name for n in pullers if "httpx.AsyncClient" in ast.dump(n)]
    assert bare == [], bare                                               # no puller opens a bare client
    using = [n.name for n in pullers if "_ingress_client" in ast.dump(n)]
    assert {"_pull_rss", "_pull_api", "_pull_wiki", "_pull_scrape", "_pull_github", "_pull_gitlab"} <= set(using)
    assert "def _ingress_client(" in src


def test_the_collectors_and_research_gatherers_use_the_ingress_client():
    src = (ROOT / "vera" / "fabric" / "data_fabric_collectors.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    get = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_get")
    assert "_ingress_client" in ast.dump(get) and "httpx.AsyncClient" not in ast.dump(get)
    src = (ROOT / "vera" / "research" / "researcher_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {"_searxng_once", "search_brave", "search_ddg", "gather_arxiv", "gather_hackernews", "_github_crawl_readme",
             "gather_github", "gather_archive", "_fetch_structured_url", "_gather_nvd"}
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name in names}
    assert set(fns) == names
    bare = [k for k, f in fns.items() if "httpx.AsyncClient" in ast.dump(f)]
    assert bare == [], bare
    assert all("_ingress_client" in ast.dump(f) for f in fns.values())
