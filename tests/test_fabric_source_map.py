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


# the shape of https://t.me/s/<channel> (trimmed from a real page, 9 Oct 2026)
TG = ('<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap '
      'js-widget_message" data-post="durov/528" data-view="x"><div class="tgme_widget_message_bubble">'
      '<div class="tgme_widget_message_text js-message_text" dir="auto"><tg-emoji><i class="emoji"><b>⛔️</b></i></tg-emoji>'
      ' The UK government wants to ban social media for children under 16.<br/><br/>But banning it only puts them in '
      'greater danger &amp; more.</div><div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" '
      'href="https://t.me/durov/528"><time datetime="2026-10-08T14:01:23+00:00" class="time">14:01</time></a></div></div></div>'
      '<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" '
      'data-post="durov/529"><div class="tgme_widget_message_photo_wrap"></div><time datetime="2026-10-08T15:00:00+00:00"></time></div></div>')


def test_public_telegram_channels_become_records():
    posts = sm.telegram_posts(TG, "durov")
    assert len(posts) == 1                                     # a photo with no words is not a record
    d = posts[0]["data"]
    assert d["link"] == "https://t.me/durov/528" and d["published"] == "2026-10-08T14:01:23Z"
    assert d["title"].startswith("⛔️ The UK government wants to ban social media")
    assert "\n\nBut banning it only puts them in greater danger & more." in posts[0]["text"]
    assert sm.record_view(d)["link"] == "https://t.me/durov/528"
    for v, name in (("@durov", "durov"), ("t.me/durov", "durov"), ("https://t.me/s/durov", "durov"),
                    ("https://telegram.me/durov/", "durov"), ("not a channel!", ""), ("@ab", "")):
        assert sm.telegram_channel(v) == name, v


def test_a_feed_is_named_after_its_site():
    page = '<head><title>Simon Willison’s Weblog</title><meta property="og:site_name" content="Simon Willison&#39;s Weblog"></head>'
    assert sm.page_title(page) == "Simon Willison's Weblog"
    assert sm.page_title("<title> A  page </title>") == "A page" and sm.page_title("") == ""
    assert sm.feed_label("Atom", "Simon Willison's Weblog") == "Simon Willison's Weblog"
    assert sm.feed_label("Engineering blog", "Example") == "Engineering blog" and sm.feed_label("", "Example") == "Example"


def test_the_fabric_reads_telegram_and_passes_rss_headers():
    src = open(os.path.join(ROOT, "vera", "fabric", "data_fabric.py"), encoding="utf-8").read()
    assert 'elif stype == "telegram":' in src and "_srcmap.telegram_posts" in src
    rss = src[src.index("async def _pull_rss"):src.index("async def _pull_api")]
    assert 'headers.update(' in rss
    assert '"site": _srcmap.page_title(body)' in src


RSS2 = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Guardian property</title>
<item><title><![CDATA[Higher mortgage rates inflict 'pain' on UK housing market]]></title>
<link>https://www.theguardian.com/money/2026/oct/10/mortgage</link>
<description><![CDATA[<p>Rates rose &amp; demand fell.</p>]]></description>
<pubDate>Fri, 10 Oct 2026 07:00:00 GMT</pubDate></item>
<item><title>No link here</title><guid isPermaLink="false">abc-123</guid></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Weblog</title>
<entry><title>Release 1.2</title><link rel="replies" href="https://x.test/c"/><link rel="alternate" href="https://x.test/r"/>
<updated>2026-10-09T12:00:00Z</updated><summary type="html">&lt;b&gt;Faster&lt;/b&gt; builds</summary></entry></feed>"""
RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/"><item rdf:about="https://arxiv.org/abs/1"><title>A paper</title>
<link>https://arxiv.org/abs/1</link><description>Abstract.</description><dc:date>2026-10-09</dc:date></item></rdf:RDF>"""


def test_feeds_read_without_feedparser():
    a = sm.parse_feed(RSS2)
    assert a[0]["data"]["title"] == "Higher mortgage rates inflict 'pain' on UK housing market"        # CDATA unwrapped
    assert a[0]["data"]["link"].startswith("https://www.theguardian.com/") and a[0]["data"]["summary"] == "Rates rose & demand fell."
    assert sm.record_view(a[0]["data"])["published"] == "2026-10-10T07:00:00Z"
    assert a[1]["data"]["link"] == ""                                       # a guid that is not a URL is not a link
    b = sm.parse_feed(ATOM)
    assert b[0]["data"]["link"] == "https://x.test/r" and b[0]["data"]["summary"] == "Faster builds"
    c = sm.parse_feed(RDF)
    assert c[0]["data"]["link"] == "https://arxiv.org/abs/1" and c[0]["data"]["published"] == "2026-10-09"
    assert sm.parse_feed(b"<html><body>not a feed") == [] and sm.parse_feed(RSS2, limit=1)[0]["data"]["title"]


def test_the_rss_puller_falls_back_to_the_stdlib_parser():
    src = open(os.path.join(ROOT, "vera", "fabric", "data_fabric.py"), encoding="utf-8").read()
    rss = src[src.index("async def _pull_rss"):src.index("async def _pull_api")]
    assert "_srcmap.parse_feed(body" in rss and 're.findall(r"<title>' not in rss
