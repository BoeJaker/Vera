"""S4 / D05-1: capability lookups use a key the registry sets, so the flows behind them run.

Twelve sites in data_fabric and its collectors read `entry["fn"]`; registry entries hold "func" and "raw", never
"fn", so topic sources, docs re-crawl, auto-stitch, collector source registration, discover search, the browser
fallbacks and index seeding never ran. And the MediaWiki collector passed each page to ingest_dataset as its
dataset id. Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import re
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
import Vera.vera.fabric.data_fabric as df  # noqa: E402
import Vera.vera.fabric.data_fabric_collectors as col  # noqa: E402

REG = orch.CAPABILITY_REGISTRY


class Spy:
    def __init__(self, result=None):
        self.calls, self.result = [], result if result is not None else {}

    async def __call__(self, **kw):
        self.calls.append(kw)
        return self.result


@pytest.fixture
def fake(monkeypatch):
    def put(name, result=None):
        spy = Spy(result)
        monkeypatch.setitem(REG, name, {"func": spy, "raw": spy})
        return spy
    return put


def test_a_topic_source_reruns_discovery(fake):
    spy = fake("collector.discover", {"sources_found": 2, "ingested_total": 5})
    assert asyncio.run(df._pull_topic({"id": "s1", "config": {"topic": "ocean robotics", "max_sources": 3}})) == []
    assert spy.calls == [{"topic": "ocean robotics", "max_sources": 3, "content_type": "all"}]


def test_a_docs_source_recrawls(fake):
    spy = fake("collector.ingest_docs", {"ingested": 1})
    asyncio.run(df._pull_docs({"id": "s2", "dataset_id": "ds_docs", "url": "https://docs.example/",
                               "config": {"max_pages": 7}}))
    assert len(spy.calls) == 1
    assert spy.calls[0]["url"] == "https://docs.example/" and spy.calls[0]["dataset_id"] == "ds_docs"
    assert spy.calls[0]["max_pages"] == 7


def test_auto_stitch_fires_the_loom(fake, monkeypatch):
    spy = fake("fabric.loom.run", {"total": 3, "persisted": 3})

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(col.asyncio, "sleep", no_sleep)

    async def no_emit(evt):
        return None
    monkeypatch.setattr(col, "emit_event", no_emit, raising=False)
    asyncio.run(col._auto_stitch_hook("ds_a"))
    assert spy.calls and spy.calls[0]["dataset_a"] == "ds_a" and spy.calls[0]["persist"] is True


def test_mediawiki_pages_go_in_under_their_dataset():
    import ast
    src = open(os.path.join(ROOT, "vera", "fabric", "data_fabric_collectors.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_ingest_mediawiki")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ingest_fn"]
    assert calls and all(len(c.args) == 2 and getattr(c.args[0], "id", "") == "dataset_id" for c in calls)


_FN_LOOKUP = re.compile(r"""(?:REGISTRY|_CR|_reg|registry)\.get\(\s*["'][^"']+["']\s*\)(?:\s*or\s*\{\}\))?\s*\.get\(\s*["']fn["']\s*\)""")


def test_no_capability_lookup_reads_a_fn_key():
    hits = []
    for base, _d, files in os.walk(os.path.join(ROOT, "vera")):
        for n in files:
            if n.endswith(".py"):
                p = os.path.join(base, n)
                src = open(p, encoding="utf-8", errors="ignore").read()
                hits += [f"{os.path.relpath(p, ROOT)}:{src[:m.start()].count(chr(10)) + 1}"
                         for m in _FN_LOOKUP.finditer(src)]
    assert not hits, f"registry entries hold func/raw, never fn: {hits}"
