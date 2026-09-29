"""browser.reader's extraction (vera/web/reader_extract.js), run in a real page.

2026-09-28 (owner): "the web one needs to be able to reader mode - text properly formatted and prettyfied and parsed
and rendered like markup from current bare scraped webpage and show the key body of text or a composite if its
fragmented". The extraction is a function expression page.evaluate runs as it is, so it is tested the same way:
set a page's content, evaluate the file, read what comes back. Needs Playwright's chromium (in vera:latest); skipped
where it is not installed.
"""
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "vera", "web", "reader_extract.js"), encoding="utf-8").read()

pw = pytest.importorskip("playwright.async_api")

PARA = ("The quick survey of vector stores compared recall, latency, and cost across four engines, with each "
        "measured on the same corpus, the same hardware, and the same queries.")

ARTICLE = f"""<html><head><title>Vector stores compared | Example</title>
<meta property="og:site_name" content="Example Journal"><meta name="author" content="A. Writer">
<meta property="article:published_time" content="2026-09-01T10:00:00Z"></head><body>
<header><nav><a href="/">Home</a> <a href="/a">About</a> <a href="/b">Blog</a></nav></header>
<div class="cookie-banner">We use cookies. <button>Accept all</button></div>
<div class="layout"><aside class="sidebar"><a href="/x">Popular</a><a href="/y">Trending</a></aside>
<article class="post-content"><h1>Vector stores compared</h1>
<p>{PARA}</p><h2>Method</h2><p>{PARA} It used <a href="https://example.org/bench">the public benchmark</a>, and
<strong>every</strong> run was repeated.</p>
<ul><li>Recall at ten, measured per query</li><li>Latency at the ninety-fifth percentile</li></ul>
<pre><code class="language-python">print("hello")</code></pre>
<table><tr><th>Engine</th><th>Recall</th></tr><tr><td>A</td><td>0.91</td></tr><tr><td>B</td><td>0.88</td></tr></table>
<blockquote>{PARA}</blockquote><p style="display:none">HIDDEN TEXT SHOULD NOT APPEAR</p></article></div>
<div class="related"><a href="/r1">Related one</a><a href="/r2">Related two</a></div>
<footer>Copyright Example, all rights reserved, terms and privacy.</footer></body></html>"""

FRAGMENTED = f"""<html><head><title>Fragments</title></head><body>
<div class="grid"><section class="card"><p>{PARA} First fragment, one.</p><p>{PARA} First fragment, two.</p></section>
<div class="spacer"><a href="/1">link</a><a href="/2">link</a><a href="/3">link</a></div></div>
<div class="grid"><section class="card"><p>{PARA} Second fragment, one.</p><p>{PARA} Second fragment, two.</p></section></div>
<div class="grid"><section class="card"><p>{PARA} Third fragment, one.</p><p>{PARA} Third fragment, two.</p></section></div>
</body></html>"""


async def _extract(html):
    async with pw.async_playwright() as p:
        try:
            b = await p.chromium.launch()
        except Exception as e:          # chromium not installed here
            pytest.skip("chromium unavailable: %s" % e)
        page = await b.new_page()
        await page.set_content(html)
        out = await page.evaluate(JS, 60000)
        await b.close()
        return out


def test_the_article_as_markdown_without_the_chrome():
    r = asyncio.run(_extract(ARTICLE))
    md = r["markdown"]
    assert r["title"] == "Vector stores compared | Example"
    assert r["site"] == "Example Journal" and r["byline"] == "A. Writer" and r["published"].startswith("2026-09-01")
    assert "## Method" in md                                   # headings kept as headings
    assert "[the public benchmark](https://example.org/bench)" in md   # links as links
    assert "**every**" in md
    assert "- Recall at ten, measured per query" in md          # lists as lists
    assert '```python\nprint("hello")\n```' in md               # code fenced, with its language
    assert "| Engine | Recall |" in md and "| A | 0.91 |" in md # tables as tables
    assert "> The quick survey" in md                           # quotes as quotes
    for chrome in ("Accept all", "We use cookies", "Popular", "Related one", "Copyright", "About", "HIDDEN TEXT"):
        assert chrome not in md, chrome
    assert r["composite"] is False and r["words"] > 60 and r["minutes"] >= 1


def test_a_fragmented_page_is_a_composite_in_page_order():
    r = asyncio.run(_extract(FRAGMENTED))
    md = r["markdown"]
    assert r["composite"] is True and r["parts"] >= 3
    a, b, c = md.index("First fragment, one"), md.index("Second fragment, one"), md.index("Third fragment, one")
    assert a < b < c
    assert "---" in md                                           # the parts are set apart
    assert "link" not in md.replace("](", "")                    # the link strip between them is not content
