"""web.fetch says when it did not get the page (4 Oct 2026 census):
- a URL typed with a space in the host / without its scheme is cleaned up;
- the reader proxy's own 4xx JSON is an error, never the page;
- a page that is only its menu goes to the reader, and is flagged if it stays a menu."""

import asyncio
import os
import pathlib
import sys

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.web import url_core as U  # noqa: E402
from vera.web import web_client as WC  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

NAV = "\n\n".join(["Develop", "Deploy", "Ecosystem", "Learn", "API Reference", "Search", "Log in",
                   "Start Free", "Getting Started", "Overview", "What is Qdrant?", "Guides"])
PROSE = ("Qdrant is a vector similarity search engine written in Rust. It stores points with "
         "payloads and serves filtered nearest-neighbour queries over HNSW indexes.\n") * 6


# ── url_core ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, want", [
    ("https://weav iate.io/docs", "https://weaviate.io/docs"),
    ("  https://example.com/a b.html ", "https://example.com/a%20b.html"),
    ("/valkey.io/blog/2024-10-03-valkey-launch.html", "https://valkey.io/blog/2024-10-03-valkey-launch.html"),
    ("valkey.io/blog/x", "https://valkey.io/blog/x"),
    ("valkey.io", "https://valkey.io"),
    ("//cdn.example.org/lib.js", "https://cdn.example.org/lib.js"),
    ("http://x.com?q=a b", "http://x.com?q=a%20b"),
    ("<https://qdrant.tech/documentation>", "https://qdrant.tech/documentation"),
    ("https://localhost:8999/health", "https://localhost:8999/health"),
])
def test_normalize_url_fixes_typed_urls(raw, want):
    assert U.normalize_url(raw) == want


@pytest.mark.parametrize("raw", ["/workspace/report.md", "/tmp/a.json", "/report.md", "report.md",
                                 "./notes/x.txt", "", "/usr/bin", "data.json/x"])
def test_normalize_url_leaves_paths_alone(raw):
    assert U.normalize_url(raw) == raw.strip()


def test_looks_nav_only():
    assert U.looks_nav_only(NAV) is True
    assert U.looks_nav_only(NAV + "\n" + PROSE) is False
    assert U.looks_nav_only(PROSE) is False
    assert U.looks_nav_only("Develop\nDeploy") is False        # too little to judge
    assert U.looks_nav_only("") is False


# ── fetch_page, with a fake client and a fake reader ─────────────────────────

class _Resp:
    def __init__(self, text, status, url):
        self.text, self.status_code, self.url, self.headers = text, status, url, {}


class _Client:
    def __init__(self, text="", status=200, exc=None):
        self.text, self.status, self.exc, self.seen = text, status, exc, []

    async def get(self, url, **kw):
        self.seen.append(url)
        if self.exc:
            raise self.exc
        return _Resp(self.text, self.status, url)

    async def aclose(self):
        pass


def _fetch(monkeypatch, url, client, reader):
    calls = []

    async def fake_reader(u, timeout):
        calls.append(u)
        return reader

    monkeypatch.setattr(WC, "_fetch_via_reader", fake_reader)
    monkeypatch.setattr(WC, "READER_PROXY", "https://r.jina.ai/")
    out = asyncio.run(WC.fetch_page(url, client=client, use_api=False, use_rewrites=False,
                                    throttle=False))
    return out, calls


def test_reader_422_is_an_error_not_the_page(monkeypatch):
    body = ('{"data":null,"path":"url","code":422,"name":"SubmittedDataMalformedError",'
            '"message":"Domain \'https\' could not be resolved"}')
    c = _Client(exc=httpx.ConnectError("name resolution failed"))
    out, _ = _fetch(monkeypatch, "https://weav iate.io/docs", c, (body, 422, ""))
    assert c.seen and set(c.seen) == {"https://weaviate.io/docs"}   # cleaned before fetching (retried)
    assert out["error"]                                         # web.fetch -> {"error": ...}
    assert out["text"] == "" and out["via_reader"] is False
    assert "422" in out["reader_error"]


def test_reader_200_still_rescues_a_failed_fetch(monkeypatch):
    c = _Client(exc=httpx.ConnectError("reset"))
    out, _ = _fetch(monkeypatch, "https://qdrant.tech/documentation", c, (PROSE, 200, "Qdrant"))
    assert not out["error"] and out["via_reader"] is True and "vector similarity" in out["text"]
    assert out["nav_only"] is False


def test_a_menu_only_page_goes_to_the_reader(monkeypatch):
    html = "<html><body>" + "".join("<div>%s</div>" % w for w in NAV.split("\n\n")) * 3 + "</body></html>"
    c = _Client(text=html, status=200)
    out, calls = _fetch(monkeypatch, "https://qdrant.tech/documentation", c, (PROSE, 200, "Qdrant"))
    assert calls, "a menu-only page must be retried through the reader"
    assert out["via_reader"] is True and "vector similarity" in out["text"]
    assert out["nav_only"] is False


def test_a_menu_only_page_the_reader_cannot_fix_is_flagged(monkeypatch):
    html = "<html><body>" + "".join("<div>%s</div>" % w for w in NAV.split("\n\n")) * 3 + "</body></html>"
    c = _Client(text=html, status=200)
    out, _ = _fetch(monkeypatch, "https://qdrant.tech/documentation", c, ("", 0, ""))
    assert out["nav_only"] is True and not out["error"]


def test_a_real_page_is_untouched(monkeypatch):
    html = "<html><head><title>T</title></head><body><p>" + PROSE.replace("\n", "</p><p>") + "</p></body></html>"
    c = _Client(text=html, status=200)
    out, calls = _fetch(monkeypatch, "https://example.com/a", c, ("never used", 200, ""))
    assert calls == [] and out["via_reader"] is False and out["nav_only"] is False


# ── the capability and the loop ───────────────────────────────────────────────

def test_web_fetch_puts_the_nav_only_warning_first():
    cap = (ROOT / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    body = cap[cap.index("async def cap_web_fetch("):]
    assert "url = _url_core.normalize_url(url)" in body[:800]
    out_dict = body[body.index("    out = {"):]
    assert out_dict.index('"warning"') < out_dict.index('"url":')
    assert 'fp.get("nav_only")' in out_dict


def test_the_loop_cleans_a_url_arg_before_its_path_guard():
    i = SRC.index("_foreign = _v5_foreign_abs_paths(args, artifact_dir_path)")
    before = SRC[i - 1600:i]
    assert '_url_core.normalize_url(args["url"])' in before
    assert 'args = {**args, "url": _nu}' in before
