"""prose.author is handed what the step already fetched (live 3 Oct, session 4d4ef790:
web.research found 5 sources, the writer saw none and wrote "(Note: The provided context
files were empty ...)" into the deliverable)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rsc", ROOT / "vera" / "dag" / "research_sources_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

HIST = [
    {"tool": "web.research", "ok": True, "args": {"query": "why unit tests matter"},
     "preview": '{"sources": [{"url": "https://www.geeksforgeeks.org/unit-testing/", "snippet": "catch bugs early"}]}'},
    {"tool": "web.fetch", "ok": False, "args": {"url": "https://x"}, "preview": "ERROR: timeout"},
    {"tool": "exec.python.run", "ok": True, "preview": "hello"},
    {"tool": "web.fetch", "ok": True, "args": {"url": "https://martinfowler.com/bliki/UnitTest.html"},
     "preview": "Unit tests are low-level, focusing on a small part of the system"},
]


def test_successful_web_results_most_recent_first_with_their_query_or_url():
    b = C.sources_block(HIST)
    assert b.index("martinfowler.com") < b.index("geeksforgeeks.org")
    assert "[web.research why unit tests matter]" in b and "[web.fetch https://martinfowler.com" in b
    assert "ERROR: timeout" not in b and "hello" not in b


def test_bounded_and_empty_without_sources():
    big = [{"tool": "web.fetch", "ok": True, "args": {"url": "u%d" % i}, "preview": "x" * 9000} for i in range(6)]
    b = C.sources_block(big)
    assert len(b) <= C.MAX_CHARS and b.count("[web.fetch") == C.MAX_SOURCES
    assert C.sources_block([{"tool": "exec.bash.run", "ok": True, "preview": "x"}]) == ""


def test_empty_context_files_shapes():
    for v in (None, "", "[]", [], ["", " "], "null"):
        assert C.context_files_empty(v), v
    for v in (["data.json"], "notes.md", "[\"a.csv\"]"):
        assert not C.context_files_empty(v), v


def test_the_loop_passes_them_and_the_writer_uses_them():
    i = SRC.index("# ── code.author / prose.author / code.edit missing")
    block = SRC[i:i + 6000]
    assert "_research_sources_core.sources_block(history)" in block
    w = SRC[SRC.index("async def cap_prose_author("):]
    w = w[:w.index("gen = CAPABILITY_REGISTRY.get(\"llm.generate\")")]
    assert "sources: str = \"\"" in w and "SOURCE MATERIAL" in w
    assert "Never mention" in w
