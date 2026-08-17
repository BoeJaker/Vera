"""code.author must redirect PROSE/DOC files (.md/.txt/.rst) to prose.author.

The 2026-08-17 incident: a "Define app requirements and tech stack" step was
scoped with ONLY code.author. Handed the markdown deliverable
output_features_and_stack.md, code.author — whose entire system prompt is "You
are the IMPLEMENTER... write REAL, complete, running code" — emitted a Python
script (#!/usr/bin/env python3) INTO the .md instead of the requirements prose.
That is the reported "code author being used to generate prose".

prose.author already redirects the mirror case (a CODE file handed to it →
code.author). This adds the symmetric redirect: a DOCUMENT file handed to
code.author → prose.author, reaching the global registry directly (as the
existing redirect does) so it fires even when the step wasn't scoped with
prose.author — which was exactly the failing case.

These tests pin the redirect direction and that it does NOT fire for code files.
The redirect happens before any LLM call, so no generation is exercised.

Imports the monolith module, so it runs in-container (see test_loop_cancel.py).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def _install_fake_prose(monkeypatch):
    calls = []

    async def fake_prose(**kw):
        calls.append(kw)
        return {"ok": True, "path": kw.get("path"), "via": "prose.author-stub"}

    monkeypatch.setitem(W.CAPABILITY_REGISTRY, "prose.author", {"func": fake_prose})
    return calls


def test_md_deliverable_is_redirected_to_prose_author(monkeypatch):
    calls = _install_fake_prose(monkeypatch)
    r = asyncio.run(W.cap_code_author(
        task="Define the core feature list and tech stack in a requirements document.",
        path="output_features_and_stack.md"))
    assert r.get("via") == "prose.author-stub", r
    assert len(calls) == 1
    assert calls[0]["path"] == "output_features_and_stack.md"
    assert "requirements document" in calls[0]["task"]


def test_txt_and_rst_and_markdown_are_redirected(monkeypatch):
    for p in ("notes.txt", "design.rst", "README.markdown", "spec.adoc"):
        calls = _install_fake_prose(monkeypatch)
        r = asyncio.run(W.cap_code_author(task="write the document", path=p))
        assert r.get("via") == "prose.author-stub", (p, r)
        assert calls and calls[0]["path"] == p


def test_code_files_are_NOT_redirected_to_prose(monkeypatch):
    calls = _install_fake_prose(monkeypatch)

    # Stub llm.generate so code.author's own (non-redirected) code path returns
    # without a real model call; we only assert prose.author was never reached.
    async def fake_gen(**kw):
        return {"ok": True, "text": "```python file=script.py\nprint(1)\n```"}

    monkeypatch.setitem(W.CAPABILITY_REGISTRY, "llm.generate",
                        {"func": fake_gen, "raw": fake_gen})
    for p in ("script.py", "index.html", "app.js", "styles.css"):
        try:
            asyncio.run(W.cap_code_author(task="write a script", path=p))
        except Exception:
            pass  # the downstream save/version path may not run offline; irrelevant here
    assert calls == [], "code.author must not redirect a code file to prose.author"


def test_redirect_ext_sets_are_disjoint_and_correctly_populated():
    # Disjoint sets are what guarantee code.author<->prose.author can't ping-pong.
    assert not (W._V5_CODE_TO_PROSE_EXTS & W._V5_PROSE_TO_CODE_EXTS)
    assert {"md", "markdown", "txt", "rst"} <= W._V5_CODE_TO_PROSE_EXTS
    # html/htm are CODE (prose.author redirects them the other way) — must NOT be
    # treated as prose by code.author, or the two redirects would fight.
    assert "html" not in W._V5_CODE_TO_PROSE_EXTS
    assert "html" in W._V5_PROSE_TO_CODE_EXTS
