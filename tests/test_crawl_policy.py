import asyncio
from pathlib import Path

import pytest

from vera.web import web_client

try:
    import Vera.vera.capability_orchestration  # noqa: F401
    from Vera.vera.web import web_capabilities as web_caps
except Exception:  # pragma: no cover
    web_caps = None


pytestmark = pytest.mark.critical


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/path#section", "https://example.com/path"),
        ("https://example.com", "https://example.com/"),
        ("https://Example.com:443/path", "https://example.com/path"),
        ("http://Example.com:80/path", "http://example.com/path"),
        ("https://example.com/search?q=one#top",
         "https://example.com/search?q=one"),
        ("ftp://example.com/file", ""),
        ("https://user:secret@example.com/file", ""),
        ("not a url", ""),
    ],
)
def test_canonical_crawl_url_has_stable_safe_identity(raw, expected):
    assert web_client.canonical_crawl_url(raw) == expected


def test_canonical_crawl_url_rejects_invalid_port():
    assert web_client.canonical_crawl_url("https://example.com:bad/path") == ""


def test_crawl_links_are_same_scope_canonical_ordered_and_unique():
    html = """
      <a href="/guide#intro">intro</a>
      <a href="https://EXAMPLE.com/guide#other">same page</a>
      <a href="/guide?q=two">query variant</a>
      <a href="https://other.test/guide">external</a>
      <a href="mailto:test@example.com">mail</a>
      <a href="javascript:alert(1)">script</a>
    """

    assert web_client.crawl_links(html, "https://example.com/start") == [
        "https://example.com/guide",
        "https://example.com/guide?q=two",
    ]


def test_crawl_links_respect_explicit_port_boundary_and_limit():
    html = """
      <a href="https://example.com:8443/one">one</a>
      <a href="https://example.com/two">wrong port</a>
      <a href="/three">three</a>
    """

    assert web_client.crawl_links(
        html, "https://example.com:8443/start", max_links=1
    ) == ["https://example.com:8443/one"]
    assert web_client.crawl_links(html, "https://example.com/start", max_links=0) == []


def test_crawl_content_fingerprint_ignores_case_and_whitespace_only():
    first = web_client.crawl_content_fingerprint("Same\n  PAGE content")
    second = web_client.crawl_content_fingerprint(" same page CONTENT ")
    different = web_client.crawl_content_fingerprint("different content")

    assert first == second
    assert first != different
    assert web_client.crawl_content_fingerprint("") == ""


def test_web_and_research_crawls_use_shared_identity_and_visit_budgets():
    root = Path(__file__).parents[1] / "vera"
    web = (root / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    research = (root / "research" / "researcher_api.py").read_text(encoding="utf-8")

    assert "return _wc.crawl_links(" in web
    assert "len(visited) >= max_pages" in web
    assert "_wc.crawl_content_fingerprint(text)" in web
    assert "return _webclient.crawl_links(" in research
    assert "len(visited) >= 20" in research
    assert "_crawl_content_key(text)" in research


@pytest.mark.skipif(web_caps is None, reason="app module not importable here")
def test_concurrent_web_children_cannot_overshoot_page_budget(monkeypatch):
    calls = []

    class Session:
        async def aclose(self):
            return None

    async def fetch(url, **kwargs):
        calls.append(url)
        links = "".join(f'<a href="/child-{n}">c</a>' for n in range(8))
        return {"html": links, "text": f"unique {url}", "title": url,
                "status": 200, "error": "", "blocked": False}

    async def emit(event):
        return None

    monkeypatch.setattr(web_caps, "_discovery", lambda: None)
    monkeypatch.setattr(web_caps._wc, "new_session", lambda timeout: Session())
    monkeypatch.setattr(web_caps._wc, "fetch_page", fetch)
    monkeypatch.setattr(web_caps, "emit_event", emit)

    result = asyncio.run(web_caps.cap_web_crawl(
        url="https://example.test/start", depth=1, breadth=8,
        max_pages=2, ingest_to_fabric=False,
    ))

    assert result["page_count"] == 2
    assert len(calls) == 2


@pytest.mark.skipif(web_caps is None, reason="app module not importable here")
def test_duplicate_and_failed_pages_preserve_unique_partial_results(monkeypatch):
    calls = []

    class Session:
        async def aclose(self):
            return None

    async def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("/start"):
            return {"html": '<a href="/duplicate">d</a><a href="/failed">f</a>'
                            '<a href="/unique">u</a>',
                    "text": "seed content", "title": "seed", "status": 200,
                    "error": "", "blocked": False}
        if url.endswith("/failed"):
            return {"error": "offline", "blocked": False}
        text = "seed content" if url.endswith("/duplicate") else "unique content"
        return {"html": "", "text": text, "title": url, "status": 200,
                "error": "", "blocked": False}

    async def emit(event):
        return None

    monkeypatch.setattr(web_caps, "_discovery", lambda: None)
    monkeypatch.setattr(web_caps._wc, "new_session", lambda timeout: Session())
    monkeypatch.setattr(web_caps._wc, "fetch_page", fetch)
    monkeypatch.setattr(web_caps, "emit_event", emit)

    result = asyncio.run(web_caps.cap_web_crawl(
        url="https://example.test/start", depth=1, breadth=3,
        max_pages=4, ingest_to_fabric=False,
    ))

    assert [page["url"] for page in result["pages"]] == [
        "https://example.test/start", "https://example.test/unique"
    ]
    assert result["page_count"] == 2
    assert len(calls) == 4
