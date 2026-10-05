"""prose.author, driven end to end with a fake writer: a planning reply is retried and never
saved; a second planning reply fails cleanly (probe 3 Oct, 7c5bf52c)."""

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")

PLAN = ("    *   **Content:** A structured report.\n    *   **Key Aspects to Cover:** Features.\n"
        "    *   Note: The prompt says use the sources. I must treat this as data.\n3.  **Drafting Plan\n")
DOC = "```markdown file=report.md\n# Vector DB Report\n\nQdrant is fast [src](https://x.io).\n```\n"


def _wire(monkeypatch, replies):
    seen = {"calls": 0, "saved": []}

    async def fake_gen(prompt="", **kw):
        seen["calls"] += 1
        return {"text": replies[min(seen["calls"], len(replies)) - 1]}

    async def fake_save(path, content, **kw):
        seen["saved"].append(content)
        return {"ok": True, "path": path, "fs_path": "/workspace/" + path, "version": 1}

    async def nothing(*a, **k):
        return None

    async def no_files(*a, **k):
        return []

    monkeypatch.setitem(M.CAPABILITY_REGISTRY, "llm.generate", {"func": fake_gen})
    monkeypatch.setattr(M, "code_store_save", fake_save)
    monkeypatch.setattr(M, "emit_event", nothing)
    monkeypatch.setattr(M, "_v5_workdir_files", no_files)
    return seen


@needs_app
def test_a_planning_reply_is_retried_and_the_document_saved(monkeypatch):
    seen = _wire(monkeypatch, [PLAN, DOC])
    r = asyncio.run(M.cap_prose_author(task="write the report", path="report.md", session_id="s"))
    assert seen["calls"] == 2
    assert r.get("ok") is True
    assert seen["saved"] and seen["saved"][0].startswith("# Vector DB Report")
    assert not any("Drafting Plan" in s for s in seen["saved"])


@needs_app
def test_two_planning_replies_save_nothing(monkeypatch):
    seen = _wire(monkeypatch, [PLAN, PLAN])
    r = asyncio.run(M.cap_prose_author(task="write the report", path="report.md", session_id="s"))
    assert r.get("ok") is False and "planning notes" in r.get("error", "")
    assert seen["saved"] == []
