"""prose.author redirects a CODE file (html/css/js/…) to code.author (2026-08-17): a
specialist authored index.html / style.css via prose.author, producing ungrounded,
syntax-unchecked 'documents'. Imports the monolith, runs in-container.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def test_code_exts_are_redirected():
    for e in ("html", "htm", "css", "scss", "js", "jsx", "ts", "tsx", "py",
              "sh", "go", "rs", "php", "java", "sql", "vue", "svelte"):
        assert e in W._V5_PROSE_TO_CODE_EXTS


def test_doc_exts_are_not_redirected():
    for e in ("md", "txt", "rst", "csv", "yaml", "yml", "json"):
        assert e not in W._V5_PROSE_TO_CODE_EXTS


def test_prose_author_redirects_html_to_code_author():
    called = {}

    async def fake_code_author(**kw):
        called.update(kw)
        return {"ok": True, "path": kw.get("path"), "via": "code.author"}

    reg = W.CAPABILITY_REGISTRY
    orig = reg.get("code.author")
    prose_fn = reg["prose.author"]["func"]
    try:
        reg["code.author"] = {"func": fake_code_author}
        res = asyncio.run(prose_fn(task="build the pomodoro timer UI", path="index.html"))
        assert res.get("via") == "code.author"        # it redirected
        assert called.get("path") == "index.html"
        assert "timer UI" in (called.get("task") or "")  # task threaded through
    finally:
        if orig is not None:
            reg["code.author"] = orig
