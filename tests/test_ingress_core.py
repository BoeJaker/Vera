"""The ingress gateway's pure core (vera/fabric/ingress_core.py; shared-planning web-ingress WI-2): one URL key
that covers the three keys in use today, content and near-duplicate hashes checked on the fixture site's own
duplicate and near-duplicate, link extraction against the fixture's truth table, the record policy the owner set
(11 Oct 2026) and the reader route for every fixture URL."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "web-ingress-bench"))

from vera.fabric import ingress_core as ic  # noqa: E402
import fixture_site as site  # noqa: E402


# ── url_key ───────────────────────────────────────────────────────────────────

SPELLINGS = [
    "https://www.Example.com/a?utm_source=x&b=2&a=1#frag",
    "HTTPS://example.com:443/a/?a=1&b=2",
    "https://example.com//a?b=2&a=1&fbclid=abc",
    "https://example.com/a?a=1&utm_campaign=c&b=2&",
    "https://example.com/%61?a=1&b=2",
]


def test_one_key_for_every_spelling():
    keys = {ic.url_key(u) for u in SPELLINGS}
    assert keys == {"https://example.com/a?a=1&b=2"}, keys


def test_url_key_is_idempotent_and_keeps_meaningful_parameters():
    for u in SPELLINGS + ["https://api.example.org/items?cursor=p2&limit=50", "http://h.example/path/to/x.pdf",
                          "https://example.com/", "https://example.com"]:
        k = ic.url_key(u)
        assert ic.url_key(k) == k, u
    assert ic.url_key("https://api.example.org/items?limit=50&cursor=p2") == "https://api.example.org/items?cursor=p2&limit=50"
    assert ic.url_key("https://example.com") == "https://example.com/"
    assert ic.url_key("http://example.com:8080/x/") == "http://example.com:8080/x"
    assert ic.url_key("http://example.com:80/x") == "http://example.com/x"


def test_secret_parameters_never_survive():
    k, dropped = ic.url_key_detail("https://files.example/doc.pdf?token=abc&sig=xyz&X-Amz-Signature=1&page=2")
    assert k == "https://files.example/doc.pdf?page=2" and sorted(dropped) == ["X-Amz-Signature", "sig", "token"]
    for name in sorted(ic.SECRET_PARAMS):
        assert name not in ic.url_key(f"https://h.example/p?{name}=v&keep=1")
        assert "keep=1" in ic.url_key(f"https://h.example/p?{name}=v&keep=1")


def test_the_three_existing_keys_fold_into_this_one():
    """discovery._normalize_url, reader_cache.canonical and info_seek_core.canon_url each produce a spelling;
    url_key maps all of them, and the raw URL, to the same key."""
    from vera.fabric.discovery import _normalize_url as disc
    from vera.web.reader_cache import canonical as reader
    from vera.web.info_seek_core import canon_url as seek
    raw = "https://www.Example.com/news/story/?utm_source=nl&id=7&ref=tw#top"
    keys = {ic.url_key(f(raw)) for f in (disc, reader, seek)} | {ic.url_key(raw)}
    assert keys == {"https://example.com/news/story?id=7"}, keys


def test_unparseable_and_non_http_urls_still_key():
    assert ic.url_key("") == ""
    assert ic.url_key("mailto:a@b.c") == "mailto:a@b.c"
    assert ic.url_key("not a url#x") == "not a url"


def test_hosts_and_internal_detection():
    assert ic.host_of("https://WWW.Gov.UK/x") == "gov.uk" and ic.host_of("nonsense") == ""
    for u in ("https://192.168.0.138:8999/health", "http://10.1.2.3/", "http://127.0.0.1:8765/a",
              "https://llm.int:8999/mcp/call", "http://vfs-02.local/", "http://localhost/", "http://pve/",
              "http://[::1]/", "http://169.254.1.1/", "https://nwm-02/"):
        assert ic.is_internal(u), u
    for u in ("https://en.wikipedia.org/wiki/X", "https://www.gov.uk/", "http://8.8.8.8/", "https://api.example.org"):
        assert not ic.is_internal(u), u
    assert ic.is_internal("https://ops.example.org/", extra_hosts=["ops.example.org"])


# ── content identity ──────────────────────────────────────────────────────────

def test_content_hash_ignores_whitespace_only():
    assert ic.content_hash("a  b\n c") == ic.content_hash(" a b c ")
    assert ic.content_hash("a b c") != ic.content_hash("a b d")
    assert len(ic.bytes_hash(b"x")) == 64


def test_simhash_separates_the_fixture_near_duplicate_from_distinct_pages():
    a = site.respond("GET", "/a").body.decode()
    copy = site.respond("GET", "/a-copy").body.decode()
    b = site.respond("GET", "/b").body.decode()
    c = site.respond("GET", "/c").body.decode()
    ha, hcopy, hb, hc = (ic.simhash64(x) for x in (a, copy, b, c))
    assert ic.content_hash(a) != ic.content_hash(copy)           # exact dedupe does not catch it
    assert ic.near_duplicate(ha, hcopy), ic.hamming(ha, hcopy)   # the simhash does
    assert not ic.near_duplicate(ha, hb) and not ic.near_duplicate(ha, hc) and not ic.near_duplicate(hb, hc)
    assert ic.hamming(ha, hb) > 10
    assert ic.simhash64("") == 0 and ic.simhash64("one") == ic.simhash64("ONE")


def test_lsh_bands_share_a_band_for_near_duplicates():
    a = ic.simhash64("the quick brown fox jumps over the lazy dog again and again")
    b = a ^ (1 << 5) ^ (1 << 40) ^ (1 << 63)                     # 3 bits apart
    assert ic.hamming(a, b) == 3
    assert set(ic.lsh_bands(a)) & set(ic.lsh_bands(b))
    assert len(ic.lsh_bands(a)) == 4 and all(0 <= x < 65536 for x in ic.lsh_bands(a))


# ── links ─────────────────────────────────────────────────────────────────────

def test_links_from_the_fixture_home_match_the_truth_table():
    home = site.respond("GET", "/").body.decode()
    links = ic.extract_links(home, "http://fixture.test/")
    got = [l["url"].replace("http://fixture.test", "") for l in links]
    expect = []
    for p in site.HOME_LINKS:                                   # first occurrence of each KEY only
        k = ic.url_key("http://fixture.test" + p)
        if k not in {ic.url_key("http://fixture.test" + e) for e in expect}:
            expect.append(p)
    assert got == expect, (got, expect)
    assert links[0]["text"] == "/a" and all(l["key"].startswith("http://fixture.test/") for l in links)


def test_links_skip_javascript_mailto_fragments_and_honour_base():
    html = ('<base href="https://h.example/dir/"><a href="x.html">X</a><a href="#top">top</a>'
            '<a href="javascript:void(0)">js</a><a href="mailto:a@b">m</a><a href="../up?utm_source=1">Up</a>'
            '<link rel="alternate" type="application/rss+xml" href="/feed.rss"><a href="x.html">again</a>')
    links = ic.extract_links(html, "https://other.example/")
    assert [l["url"] for l in links] == ["https://h.example/dir/x.html", "https://h.example/up",
                                         "https://h.example/feed.rss"]
    assert links[0]["text"] == "X" and links[2]["rel"] == "alternate"


# ── record policy ─────────────────────────────────────────────────────────────

def test_record_policy_follows_the_owner_decision():
    assert ic.record_policy("https://en.wikipedia.org/wiki/X") == "full"
    assert ic.record_policy("https://portal.example/account", has_auth=True) == "full"      # logged in, external
    assert ic.record_policy("https://llm.int:8999/ui") == "none"
    assert ic.record_policy("http://192.168.0.96:8123/api") == "none"
    assert ic.record_policy("https://get.docker.com/", purpose="install") == "none"
    assert ic.record_policy("https://api.ebay.com/sell/x", purpose="account") == "meta"
    assert ic.record_policy("https://api.openai.com/v1", purpose="llm_provider") == "meta"
    assert ic.record_policy("https://en.wikipedia.org/wiki/X", requested="meta") == "meta"   # may lower
    assert ic.record_policy("https://api.ebay.com/x", purpose="account", requested="full") == "meta"  # never raise
    assert ic.record_policy("https://x.example/", requested="nonsense") == "full"


# ── reader routes over the whole fixture ──────────────────────────────────────

EXPECT_READER = {
    "page": "html", "feed": {"rss", "atom"}, "sitemap": "sitemap", "api": {"json", "yaml"}, "catalogue": "json",
    "media": {"image", "audio"}, "robots": "text",
}
FILE_READERS = {"/files/report.pdf": "pdf", "/files/scan.pdf": "pdf", "/files/memo.docx": "docx",
                "/files/rents.xlsx": "xlsx", "/files/data.csv.gz": "gzip", "/files/bundle.zip": "zip",
                "/files/handbook.epub": "epub", "/files/places.geojson": "geojson", "/files/big.bin": "binary",
                "/files/bomb.zip": "zip"}


def test_every_fixture_url_routes_to_the_right_reader_and_kind():
    for path, item in site.truth().items():
        r = site.respond("GET", path, {"Range": "bytes=0-65535"} if item.sample_only else {})
        if r.status in (301, 302):
            continue
        reader = ic.reader_for(r.headers.get("content-type", ""), "http://fixture.test" + path, r.body[:4096])
        if item.kind == "file":
            assert reader == FILE_READERS[path], (path, reader)
        else:
            exp = EXPECT_READER[item.kind]
            assert reader == exp or reader in exp, (path, item.kind, reader)
        assert ic.kind_of(reader) == (item.kind if item.kind != "catalogue" else "api") or item.kind == "robots", (path, reader)


def test_reader_sniffing_beats_a_vague_content_type():
    pdf = site.respond("GET", "/files/report.pdf").body
    assert ic.reader_for("application/octet-stream", "https://h.example/download?id=1", pdf) == "pdf"
    assert ic.reader_for("application/octet-stream", "https://h.example/x.docx", b"PK\x03\x04") == "docx"
    assert ic.reader_for("text/xml", "https://h.example/f", b'<?xml version="1.0"?><rss version="2.0">') == "rss"
    assert ic.reader_for("application/xml", "https://h.example/f", b'<?xml version="1.0"?><urlset>') == "sitemap"
    assert ic.reader_for("text/plain", "https://h.example/f", b"<!doctype html><html>") == "html"
    assert ic.reader_for("", "https://h.example/x.parquet", b"") == "parquet"
    assert ic.reader_for("", "https://h.example/x", b"PAR1....") == "parquet"
    assert ic.reader_for("", "https://h.example/x", b"") == "binary"
    assert ic.reader_for("application/gzip", "https://h.example/sitemap-1.xml.gz", b"\x1f\x8b") == "sitemap"
    assert ic.reader_for("video/mp4", "https://h.example/x", b"") == "video"


def test_byte_caps_and_archive_guards():
    assert ic.max_bytes_for("html") == 2 * 1024 * 1024 and ic.max_bytes_for("pdf") == 10 * 1024 * 1024
    assert ic.max_bytes_for("image") == 0 and ic.max_bytes_for("unknown") == 0
    assert ic.archive_member_ok(1000, 50_000)
    assert not ic.archive_member_ok(1000, 200_000)                      # 200x: a bomb
    assert not ic.archive_member_ok(1000, 50_000, depth=2)              # too deep
    bomb = site.respond("GET", "/files/bomb.zip").body
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(bomb)) as z:
        i = z.infolist()[0]
        assert not ic.archive_member_ok(i.compress_size, i.file_size)
