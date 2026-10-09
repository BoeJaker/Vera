"""Fabric source shaping: nested JSON APIs give readable records, feeds are found
from a page, and any source type's record reads the same way."""
import os

from vera.fabric import source_map_core as sm

ROOT = os.path.join(os.path.dirname(__file__), "..")

# one feature of USGS's GeoJSON summary feed (all_day.geojson), trimmed
USGS = {"type": "FeatureCollection", "metadata": {"title": "USGS All Earthquakes, Past Day"},
        "features": [{"type": "Feature",
                      "properties": {"mag": 4.6, "place": "92 km SSW of Sola, Vanuatu", "time": 1791500000000,
                                     "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
                                     "title": "M 4.6 - 92 km SSW of Sola, Vanuatu", "type": "earthquake"},
                      "geometry": {"type": "Point", "coordinates": [167.1, -14.6, 35.0]}, "id": "us7000abcd"}]}
USGS_CFG = {"item_path": "features", "title_field": "properties.title", "url_field": "properties.url",
            "time_field": "properties.time", "text_fields": "properties.place, properties.type",
            "geo_field": "geometry.coordinates"}


def test_nested_json_items_become_readable_records():
    items = sm.items_at(USGS, USGS_CFG["item_path"])
    assert len(items) == 1
    rec = sm.map_item(items[0], USGS_CFG)
    d = rec["data"]
    assert d["title"] == "M 4.6 - 92 km SSW of Sola, Vanuatu"
    assert d["link"].startswith("https://earthquake.usgs.gov/")
    assert d["published"] == "2026-10-08T22:53:20Z"            # epoch milliseconds read as such
    assert d["geo"] == {"lat": -14.6, "lon": 167.1}           # GeoJSON order is [lon, lat]
    assert rec["text"].startswith("M 4.6 - 92 km SSW") and "earthquake" in rec["text"]
    assert d["item"]["id"] == "us7000abcd"                     # nothing lost


def test_paths_and_times_are_forgiving():
    assert sm.items_at({"data": {"items": [1, 2]}}, "data.items") == [1, 2]
    assert sm.items_at({"items": [1]}, "items[*]") == [1]       # the jq-ish spelling still works
    assert sm.items_at({"x": 1}, "missing") == []
    assert sm.dot_get({"a": [{"b": 3}]}, "a.0.b") == 3 and sm.dot_get({"a": 1}, "a.b") is None
    assert sm.to_iso(1791500000) == "2026-10-08T22:53:20Z"
    assert sm.to_iso("Wed, 08 Oct 2026 09:33:20 GMT") == "2026-10-08T09:33:20Z"
    assert sm.to_iso("2026-10-08T10:33:20+01:00") == "2026-10-08T09:33:20Z"
    assert sm.to_iso("not a date") == "" and sm.to_iso(None) == ""
    assert not sm.has_mapping({}) and not sm.has_mapping({"jq_path": "x"}) and sm.has_mapping(USGS_CFG)


def test_feeds_are_found_where_a_page_advertises_them():
    page = ('<html><head><link rel="stylesheet" href="/a.css">'
            '<link rel="alternate" type="application/rss+xml" title="RSS" href="https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv">'
            "<link rel='alternate' type='application/atom+xml' href='/blog/atom.xml'>"
            '<link rel="alternate" hreflang="fr" href="/fr/"></head></html>')
    feeds = sm.feed_links(page, "https://example.com/blog/")
    assert [f["url"] for f in feeds] == [
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv",
        "https://example.com/blog/atom.xml"]
    assert sm.looks_like_feed('<?xml version="1.0"?><rss version="2.0">') and not sm.looks_like_feed("<html>")


def test_preset_feeds_need_no_page():
    assert sm.preset_feed("arxiv", "cs.AI") == "https://rss.arxiv.org/rss/cs.AI"
    assert sm.preset_feed("arxiv", "drop table") == ""
    assert sm.preset_feed("youtube", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv") == \
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
    assert sm.preset_feed("github_releases", "n8n-io/n8n") == "https://github.com/n8n-io/n8n/releases.atom"
    assert sm.preset_feed("github_releases", "https://github.com/ollama/ollama.git") == \
        "https://github.com/ollama/ollama/releases.atom"
    assert sm.youtube_page("@veritasium") == "https://www.youtube.com/@veritasium"
    assert sm.youtube_page("https://example.com") == ""


def test_any_source_record_reads_the_same_way():
    rss = sm.record_view({"title": "A", "link": "https://x.test/a", "published": "Wed, 08 Oct 2026 09:33:20 GMT",
                          "summary": "<p>Hello &amp; welcome</p>"})
    assert rss == {"title": "A", "link": "https://x.test/a", "published": "2026-10-08T09:33:20Z",
                   "summary": "Hello & welcome"}
    gh = sm.record_view({"title": "Fix", "html_url": "https://github.com/o/r/issues/1", "created_at": "2026-10-08T09:33:20Z"})
    assert gh["link"].endswith("/issues/1") and gh["published"] == "2026-10-08T09:33:20Z"
    mapped = sm.record_view(sm.map_item(USGS["features"][0], USGS_CFG)["data"])
    assert mapped["geo"] == {"lat": -14.6, "lon": 167.1}
    bare = sm.record_view({}, text="Only text here. More.")
    assert bare["title"] == "Only text here" and bare["link"] == ""
    assert sm.record_view({"link": "javascript:alert(1)"})["link"] == ""      # never a non-web link


def test_the_fabric_uses_the_mapping_and_offers_discovery():
    src = open(os.path.join(ROOT, "vera", "fabric", "data_fabric.py"), encoding="utf-8").read()
    api = src[src.index("async def _pull_api"):src.index("async def _pull_wiki")]
    assert "_srcmap.has_mapping(cfg)" in api and "_srcmap.map_item" in api
    assert '"name":"geo_field"' in src[src.index("SOURCE_TYPES: Dict"):]
    assert '"fabric.sources.discover_feed"' in src
