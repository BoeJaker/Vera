"""Feeds must index as ONE RECORD PER STORY.

The indexer used to hand each feed to web.fetch as a single document, so an
entire feed became one record full of channel boilerplate ("cs.AI updates on
arXiv.org … rss-specification"). Retrieval then scored FEEDS rather than
stories — live, a "proxmox homelab storage" query came back with the Hugging
Face blog. These guard the chunking that fixed it.
"""

import ast
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_SRC = os.path.join(_ROOT, "vera", "agents", "agents.py")


_NEEDED = ("_rag_parse_feed", "_rag_salvage_entries", "_rag_clean",
           "_ITEM_RE", "_NS_DECL", "_TAG_RE", "_WS_RE", "_FEED_ITEMS_MAX")


def _load(*names):
    """Load the feed helpers (and the module constants they close over)."""
    want = set(names) | set(_NEEDED)
    with open(_SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = [n for n in tree.body
              if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want)
              or (isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) in want for t in n.targets))]
    import hashlib
    import re as _re
    from typing import Any, Dict, List
    ns = {"hashlib": hashlib, "re": _re, "Any": Any, "Dict": Dict, "List": List}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), _SRC, "exec"), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, f"not found in agents.py: {missing}"
    return ns


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Krebs on Security</title>
  <link>https://krebsonsecurity.com</link>
  <description>In-depth security news and investigation</description>
  <item>
    <title>Who&#39;s Behind the Botnet</title>
    <link>https://krebsonsecurity.com/botnet</link>
    <description>&lt;p&gt;A &lt;b&gt;long&lt;/b&gt; investigation into a router botnet.&lt;/p&gt;</description>
    <pubDate>Fri, 14 Aug 2026 11:24:35 +0000</pubDate>
  </item>
  <item>
    <title>Patch Tuesday Roundup</title>
    <link>https://krebsonsecurity.com/patch</link>
    <description>Microsoft shipped 40 fixes.</description>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv cs.AI</title>
  <entry>
    <title>A Paper About Agents</title>
    <link href="http://arxiv.org/abs/2608.00001"/>
    <summary>We study agentic loops.</summary>
    <published>2026-08-23T00:00:00Z</published>
  </entry>
</feed>"""


def _parse(xml, url="https://example.com/feed", title="Example"):
    return _load("_rag_parse_feed", "_rag_clean")["_rag_parse_feed"](xml, url, title)


@pytest.mark.critical
def test_rss_becomes_one_row_per_story():
    rows = _parse(RSS, "https://krebsonsecurity.com/feed/", "Krebs on Security")
    assert len(rows) == 2, "a 2-item feed must yield 2 records, not 1 feed dump"
    titles = [r["title"] for r in rows]
    assert "Who's Behind the Botnet" in titles          # entity-decoded
    assert "Patch Tuesday Roundup" in titles


@pytest.mark.critical
def test_story_text_leads_with_the_story_not_feed_boilerplate():
    """THE regression: records used to start with channel boilerplate, so
    retrieval matched the feed rather than any story in it."""
    rows = _parse(RSS, "https://krebsonsecurity.com/feed/", "Krebs on Security")
    first = rows[0]["text"]
    assert first.startswith("Who's Behind the Botnet")
    assert "In-depth security news and investigation" not in first, \
        "channel description leaked into a story record"
    assert "rss-specification" not in first
    # HTML in the summary is reduced to readable text.
    assert "<p>" not in first and "<b>" not in first
    assert "long" in first and "router botnet" in first


@pytest.mark.critical
def test_atom_entries_and_href_links():
    rows = _parse(ATOM, "http://rss.arxiv.org/rss/cs.AI", "arXiv cs.AI")
    assert len(rows) == 1
    assert rows[0]["title"] == "A Paper About Agents"
    # Atom puts the link on href=, not in the element text.
    assert rows[0]["url"] == "http://arxiv.org/abs/2608.00001"
    assert rows[0]["published"].startswith("2026-08-23")


@pytest.mark.critical
def test_ids_are_stable_so_refreshes_merge_instead_of_duplicating():
    """Feeds are re-read on a timer; the same story must keep the same key."""
    a = _parse(RSS)
    b = _parse(RSS)
    assert [r["id"] for r in a] == [r["id"] for r in b]
    assert len({r["id"] for r in a}) == 2, "distinct stories need distinct ids"
    assert all(r["id"].startswith("feed:") for r in a)


@pytest.mark.critical
def test_non_feed_input_yields_nothing_so_the_caller_falls_back():
    """A plain page must return [] so the indexer uses the page-level path."""
    for junk in ("<html><body><h1>Hi</h1></body></html>", "not xml at all", "", "   "):
        assert _parse(junk) == []


@pytest.mark.critical
def test_feed_with_no_usable_items_yields_nothing():
    empty = ('<?xml version="1.0"?><rss version="2.0"><channel>'
             '<title>Quiet</title><item><guid>x</guid></item></channel></rss>')
    assert _parse(empty) == []


# ── truncated feeds ──────────────────────────────────────────────────────────
# http.get caps its body at 65536 bytes with no way to ask for more, so any feed
# over 64KB arrives as INVALID XML. Measured live: 9 of 20 real feeds hit that
# cap and every one fell back to page-level ingest, reintroducing exactly the
# boilerplate records this chunking exists to remove.

def _truncated(n_ok: int) -> str:
    """A feed with `n_ok` complete items, then cut off mid-item."""
    items = "".join(
        f"<item><title>Story {i}</title><link>https://x/{i}</link>"
        f"<dc:creator>someone</dc:creator>"
        f"<content:encoded>body {i}</content:encoded>"
        f"<description>summary {i}</description></item>"
        for i in range(n_ok))
    return ('<?xml version="1.0"?><rss version="2.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            "<channel><title>Feed</title>" + items
            + "<item><title>Cut off here")     # deliberately unterminated


@pytest.mark.critical
def test_truncated_feed_still_yields_its_complete_items():
    """THE regression: a >64KB feed is malformed, and used to yield nothing."""
    rows = _parse(_truncated(5))
    assert len(rows) == 5, "complete items before the cut must survive"
    assert rows[0]["title"] == "Story 0"
    assert "Cut off here" not in " ".join(r["title"] for r in rows)


@pytest.mark.critical
def test_salvage_handles_namespaced_children():
    """A salvaged item is parsed alone, so prefixes used inside it (dc:,
    content:, media:) must be declared or ElementTree drops the item."""
    ns = _load("_rag_salvage_entries")
    els = ns["_rag_salvage_entries"](_truncated(3))
    assert len(els) == 3, "namespaced items must not be lost to unbound prefixes"


@pytest.mark.critical
def test_truncated_atom_feed_salvages_entries():
    entries = "".join(
        f'<entry><title>Paper {i}</title>'
        f'<link href="http://arxiv.org/abs/{i}"/>'
        f'<media:thumbnail url="http://x/{i}.png"/>'
        f'<summary>abstract {i}</summary></entry>' for i in range(4))
    xml = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
           'xmlns:media="http://search.yahoo.com/mrss/">' + entries
           + "<entry><title>truncated")
    rows = _parse(xml)
    assert len(rows) == 4
    assert rows[0]["url"] == "http://arxiv.org/abs/0"


@pytest.mark.critical
def test_salvage_is_still_bounded():
    many = ('<?xml version="1.0"?><rss version="2.0"><channel>'
            + "".join(f"<item><title>S{i}</title></item>" for i in range(300))
            + "<item><title>cut")
    assert len(_parse(many)) <= 40


@pytest.mark.critical
def test_item_count_is_bounded():
    many = ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            + "".join(f"<item><title>Story {i}</title><link>u{i}</link></item>"
                      for i in range(200))
            + "</channel></rss>")
    assert len(_parse(many)) <= 40
