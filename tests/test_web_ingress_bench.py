"""The web-ingress bench (tools/web-ingress-bench; shared-planning web-ingress WI-0): the fixture site serves what its
truth table says, every file it serves is a valid file of its type, the bench statistics and budget verdicts are right,
and the harness refuses to run beside a census, a running loop or (live suite) the owner's evening."""
import gzip
import io
import json
import re
import sys
import threading
import urllib.request
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "web-ingress-bench"))
import bench  # noqa: E402
import fixture_site as site  # noqa: E402
import stats  # noqa: E402


def _get(path, **headers):
    return site.respond("GET", path, headers)


# ── the fixture site ──────────────────────────────────────────────────────────

def test_every_linked_url_is_in_the_truth_table_and_served():
    t = site.truth()
    for p in site.HOME_LINKS:
        assert p in t, p
    for p, item in t.items():
        r = _get(p)
        assert r.status in (200, 301, 302), (p, r.status)
        if r.status == 200 and item.kind != "robots":
            assert r.headers["content-type"].split(";")[0] == item.mime, p


def test_duplicates_are_byte_identical_and_the_near_duplicate_is_not():
    a = _get("/a").body
    assert _get("/a?utm_source=newsletter").body == a and _get("/a/").body == a
    copy = _get("/a-copy").body
    assert copy != a
    diff = sum(1 for x, y in zip(a, copy) if x != y)
    assert len(copy) == len(a) and diff <= 2          # only the date changed
    t = site.truth()
    assert t["/a?utm_source=newsletter"].same_as == "/a" and t["/a-copy"].near_dup_of == "/a"


def test_redirect_chain_ends_at_d():
    assert _get("/old").status == 301 and _get("/old").headers["location"] == "/older"
    assert _get("/older").status == 302 and _get("/older").headers["location"] == "/d"
    assert site.truth()["/old"].same_as == "/d"


def test_conditional_get_answers_304():
    r = _get("/etag")
    assert r.status == 200 and r.headers["etag"]
    assert _get("/etag", **{"If-None-Match": r.headers["etag"]}).status == 304
    assert _get("/etag", **{"If-Modified-Since": site.LAST_MODIFIED_HTTP}).status == 304
    assert _get("/etag", **{"If-None-Match": '"stale"'}).status == 200


def test_robots_disallows_private_and_names_the_sitemap():
    body = _get("/robots.txt").body.decode()
    assert "Disallow: /private/" in body and "Sitemap: /sitemap.xml" in body
    assert site.truth()["/private/page"].disallowed and _get("/private/page").status == 200


def test_slow_path_carries_its_delay():
    assert _get("/slow").delay_s == site.SLOW_S and _get("/a").delay_s == 0


def test_pdfs_are_well_formed():
    for p in ("/files/report.pdf", "/files/scan.pdf"):
        b = _get(p).body
        assert b.startswith(b"%PDF-1.4") and b.rstrip().endswith(b"%%EOF")
        off = int(re.search(rb"startxref\n(\d+)", b).group(1))
        assert b[off:off + 4] == b"xref"
        for i, m in enumerate(re.finditer(rb"(\d{10}) 00000 n ", b), 1):
            o = int(m.group(1))
            assert b[o:o + len(f"{i} 0 obj")] == f"{i} 0 obj".encode()
    assert b"Fixture PDF report" in _get("/files/report.pdf").body
    assert b"Tj" not in _get("/files/scan.pdf").body and site.truth()["/files/scan.pdf"].needs_ocr


def test_office_epub_and_archives_open():
    with zipfile.ZipFile(io.BytesIO(_get("/files/memo.docx").body)) as z:
        assert b"lab space demand" in z.read("word/document.xml")
    with zipfile.ZipFile(io.BytesIO(_get("/files/rents.xlsx").body)) as z:
        assert b"Cambridge" in z.read("xl/worksheets/sheet1.xml")
        assert b'name="Yields"' in z.read("xl/workbook.xml")
    with zipfile.ZipFile(io.BytesIO(_get("/files/handbook.epub").body)) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
    with zipfile.ZipFile(io.BytesIO(_get("/files/bundle.zip").body)) as z:
        assert sorted(z.namelist()) == ["bundle/report.pdf", "bundle/table.csv"]
    assert gzip.decompress(_get("/files/data.csv.gz").body).startswith(b"city,vacancy_pct")
    assert json.loads(_get("/files/places.geojson").body)["features"][0]["properties"]["name"]


def test_media_files_are_valid():
    png = _get("/files/photo.png").body
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    ln = int.from_bytes(png[8:12], "big")
    assert zlib.crc32(png[12:16 + ln]) & 0xFFFFFFFF == int.from_bytes(png[16 + ln:20 + ln], "big")
    wav = _get("/files/briefing.wav").body
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and int.from_bytes(wav[4:8], "little") == len(wav) - 8


def test_over_cap_file_offers_head_and_range():
    h = site.respond("HEAD", "/files/big.bin", {})
    assert h.status == 200 and int(h.headers["content-length"]) == site.OVER_CAP_BYTES and h.body == b""
    r = _get("/files/big.bin", Range="bytes=0-65535")
    assert r.status == 206 and len(r.body) == 65536
    assert r.headers["content-range"] == f"bytes 0-65535/{site.OVER_CAP_BYTES}"
    assert site.truth()["/files/big.bin"].sample_only


def test_zip_bomb_is_small_on_the_wire_and_huge_inflated():
    b = _get("/files/bomb.zip").body
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        size = z.infolist()[0].file_size
    assert len(b) < 200_000 and size / len(b) > 300 and site.truth()["/files/bomb.zip"].reject


def test_sitemap_index_children_and_lastmod():
    idx = _get("/sitemap.xml").body.decode()
    kids = re.findall(r"<loc>([^<]+)</loc>", idx)
    assert kids == ["/sitemap-1.xml.gz", "/sitemap-2.xml.gz"] and site.LASTMOD in idx
    locs = []
    for k in kids:
        locs += re.findall(r"<loc>([^<]+)</loc>", gzip.decompress(_get(k).body).decode())
    assert locs == ["/a", "/b", "/c", "/d", "/e", "/files/report.pdf"]


def test_paginated_api_and_catalogue():
    items, nxt = [], "/api/items"
    while nxt:
        d = json.loads(_get(nxt).body)
        items += d["items"]
        nxt = d["next"]
    assert [i["id"] for i in items] == [1, 2, 3, 4, 5]
    assert _get("/api/items?cursor=nope").status == 400
    res = json.loads(_get("/api/3/action/package_search").body)["result"]["results"]
    dists = [r["url"] for d in res for r in d["resources"]]
    assert len(res) == 3 and len(dists) == 5 and all(_get(u).status == 200 for u in dists)
    assert b"/api/items" in _get("/openapi.yaml").body


def test_feeds_point_at_site_pages():
    assert re.findall(r"<link>(/[^<]*)</link>", _get("/feed.rss").body.decode())[1:] == ["/a", "/c"]
    assert '<link href="/b"/>' in _get("/feed.atom").body.decode()


def test_unknown_path_is_404_and_distinct_count():
    assert _get("/nope").status == 404
    t = site.truth()
    assert site.distinct_contents() == sum(1 for i in t.values() if not i.same_as and not i.reject
                                           and i.kind != "robots")


def test_mock_transport_serves_the_site():
    httpx = pytest.importorskip("httpx")
    with httpx.Client(transport=site.mock_transport(), base_url="http://fixture.test",
                      follow_redirects=True) as c:
        r = c.get("/old")
        assert r.status_code == 200 and r.url.path == "/d" and len(r.history) == 2
        e = c.get("/etag")
        assert c.get("/etag", headers={"If-None-Match": e.headers["etag"]}).status_code == 304
        assert c.get("/a?utm_source=newsletter").content == c.get("/a").content


def test_real_server_serves_the_site():
    srv = site.serve(0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/old", timeout=10) as r:
            assert r.status == 200 and b"Destination" in r.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/files/report.pdf", timeout=10) as r:
            assert r.headers["content-type"] == "application/pdf" and r.read().startswith(b"%PDF")
    finally:
        srv.shutdown()


# ── statistics and budgets ────────────────────────────────────────────────────

def test_percentiles_and_summary():
    assert stats.percentile([], 50) is None and stats.percentile([7], 95) == 7
    assert stats.percentile([1, 2, 3, 4], 50) == 2.5
    s = stats.summarise(range(1, 101))
    assert s["n"] == 100 and abs(s["p95"] - 95.05) < 1e-9 and s["max"] == 100


def test_mann_whitney():
    same = stats.mann_whitney([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6])
    assert same["p"] > 0.9
    apart = stats.mann_whitney([1, 2, 3, 4, 5, 6, 7, 8], [11, 12, 13, 14, 15, 16, 17, 18])
    assert apart["p"] < 0.01
    assert stats.mann_whitney([1, 2], [3, 4, 5])["p"] is None


def test_compare_flags_regressions_against_the_budgets():
    base = {"fixture.web_fetch": [100.0] * 20, "live.recall": [150.0] * 5}
    ok = stats.compare(base, {"fixture.web_fetch": [103.0] * 20, "live.recall": [160.0] * 5})
    assert ok["fixture.web_fetch"]["ok"] is True and ok["live.recall"]["ok"] is True
    bad = stats.compare(base, {"fixture.web_fetch": [120.0] * 20, "live.recall": [450.0] * 5})
    assert bad["fixture.web_fetch"]["ok"] is False and "p50" in bad["fixture.web_fetch"]["why"][0]
    assert bad["live.recall"]["ok"] is False
    assert stats.verdict(bad) == "fail"
    slow = stats.compare({"live.web_search": [1000.0 + i for i in range(10)]},
                         {"live.web_search": [3000.0 + i for i in range(10)]})
    assert slow["live.web_search"]["ok"] is False
    fast = stats.compare({"live.web_search": [3000.0 + i for i in range(10)]},
                         {"live.web_search": [1000.0 + i for i in range(10)]})
    assert fast["live.web_search"]["ok"] is True      # faster is never a regression
    assert stats.compare({}, {"unbudgeted": [1.0]})["unbudgeted"]["ok"] is None


# ── the harness refuses to run at the wrong time ──────────────────────────────

def _fake(monkeypatch, health, loops, hour):
    def get_json(target, path):
        return {"sessions": loops} if "agent_loop" in path else health
    monkeypatch.setattr(bench, "get_json", get_json)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 12, hour, 0, tzinfo=tz)
    monkeypatch.setattr(bench, "datetime", _DT)


def test_preflight_refuses_a_census_a_loop_and_the_evening(monkeypatch):
    _fake(monkeypatch, {"census": {"busy": True}}, [], 3)
    with pytest.raises(SystemExit, match="census"):
        bench.preflight("https://x", "fixture", False)
    _fake(monkeypatch, {"census": {"busy": False}}, [{"session_id": "s"}], 3)
    with pytest.raises(SystemExit, match="loop"):
        bench.preflight("https://x", "fixture", False)
    _fake(monkeypatch, {"census": {"busy": False}}, [], 20)
    with pytest.raises(SystemExit, match="evening"):
        bench.preflight("https://x", "live", False)
    assert bench.preflight("https://x", "fixture", False)["running_loops"] == 0      # the fixture suite may run
    st = bench.preflight("https://x", "live", True)
    assert st["forced_past"] and st["local_hour"] == 20
