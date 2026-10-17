"""The ingress file readers (vera/fabric/ingress_readers_core.py; web-ingress WI-6): every file the fixture site
serves becomes a ParsedDoc with the right text, tables or children; archives are listed not inflated, the zip bomb
is refused, and a missing library is reported rather than raised."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "web-ingress-bench"))

from vera.fabric import ingress_core as ic  # noqa: E402
from vera.fabric import ingress_readers_core as rd  # noqa: E402
import fixture_site as site  # noqa: E402

BASE = "http://fixture.test"


def _get(path):
    r = site.respond("GET", path)
    return r.body, r.headers.get("content-type", "")


def _read(path):
    body, ct = _get(path)
    return rd.read(ic.reader_for(ct, BASE + path, body[:4096]), body, BASE + path)


def test_pdf_text_or_an_honest_needs_lib():
    d = _read("/files/report.pdf")
    assert d["reader"] == "pdf" and d["kind"] == "file"
    if d.get("needs_lib"):
        assert d["needs_lib"] == "pypdf" and d["text"] == ""
    else:
        assert "Fixture PDF report" in d["text"] and d["pages"] == 1 and d["needs_ocr"] is False
        scan = _read("/files/scan.pdf")
        assert scan["needs_ocr"] is True and scan["text"] == ""
    body, _ = _get("/files/report.pdf")
    assert rd.pdf_text_layer(body) and not rd.pdf_text_layer(_get("/files/scan.pdf")[0])


def test_docx_xlsx_epub_from_their_xml():
    d = _read("/files/memo.docx")
    assert d["reader"] == "docx" and "lab space demand in Cambridge" in d["text"] and d["paragraphs"] == 1
    x = _read("/files/rents.xlsx")
    assert x["sheets"] == 2 and [t["name"] for t in x["tables"]] == ["Rents", "Yields"]
    assert x["tables"][0]["columns"] == ["region", "rent_psf"] and x["tables"][0]["rows"] == 3
    assert x["tables"][0]["sample"][0] == ["Cambridge", 52.5] and "Milton Keynes" in x["text"]
    e = _read("/files/handbook.epub")
    assert e["title"] == "Fixture Handbook" and e["chapters"] == 1 and "Oxford-Cambridge arc" in e["text"]


def test_archives_list_members_and_read_them_one_at_a_time():
    z = _read("/files/bundle.zip")
    assert sorted(c["name"] for c in z["children"]) == ["bundle/report.pdf", "bundle/table.csv"]
    assert {c["name"]: c["reader"] for c in z["children"]}["bundle/table.csv"] == "csv"
    body, _ = _get("/files/bundle.zip")
    m = rd.read_zip_member(body, "bundle/table.csv", BASE + "/files/bundle.zip")
    assert m["reader"] == "csv" and m["tables"][0]["columns"] == ["a", "b"] and m["depth"] == 1
    assert rd.read_zip_member(body, "nope", BASE) is None
    g = _read("/files/data.csv.gz")
    assert g["reader"] == "csv" and g["compressed"] == "gzip" and g["tables"][0]["columns"] == ["city", "vacancy_pct"]
    assert g["tables"][0]["sample"][0] == ["Cambridge", "2.1"]


def test_the_zip_bomb_is_refused_without_inflating():
    b = _read("/files/bomb.zip")
    assert b.get("error") and "refused" in b["error"] and b["children"] == []
    body, _ = _get("/files/bomb.zip")
    assert rd.read_zip_member(body, "zeros.bin", BASE) is None


def test_catalogue_api_page_geojson_and_openapi():
    c = _read("/api/3/action/package_search")
    assert c["kind"] == "catalogue" and c["count"] == 3 and len(c["datasets"]) == 3
    dists = [d["url"] for ds in c["datasets"] for d in ds["distributions"]]
    assert "/files/data.csv.gz" in dists and len(dists) == 5
    assert {d["format"] for ds in c["datasets"] for d in ds["distributions"]} >= {"csv", "xlsx", "geojson", "pdf", "epub"}
    a = _read("/api/items")
    assert a["kind"] == "api" and a["items"] == 2 and a["next"] == "/api/items?cursor=p2"
    g = _read("/files/places.geojson")
    assert g["reader"] == "geojson" and g["features"] == 1 and g["names"] == ["Fixture Science Park"]
    y = _read("/openapi.yaml")
    assert y["kind"] == "api" and y["openapi"] == "3.0.0" and y["endpoints"] == ["GET /api/items"] and y["title"] == "Fixture API"


def test_feeds_and_sitemaps_have_children():
    r = _read("/feed.rss")
    assert r["reader"] == "rss" and r["title"] == "Fixture news" and [c["url"] for c in r["children"]] == ["/a", "/c"]
    a = _read("/feed.atom")
    assert a["reader"] == "atom" and a["children"][0]["url"] == "/b"
    s = _read("/sitemap.xml")
    assert s["reader"] == "sitemap" and s["title"] == "sitemap index"
    assert [c["url"] for c in s["children"]] == ["/sitemap-1.xml.gz", "/sitemap-2.xml.gz"] and all(c["index"] for c in s["children"])
    sg = _read("/sitemap-1.xml.gz")
    assert sg["reader"] == "sitemap" and [c["url"] for c in sg["children"]] == ["/a", "/b", "/c"] and sg["children"][0]["lastmod"] == site.LASTMOD


def test_media_binary_and_bad_inputs_never_raise():
    p = _read("/files/photo.png")
    assert p["reader"] == "image" and p["kind"] == "media" and p["text"] == "" and p["bytes"] > 0
    w = _read("/files/briefing.wav")
    assert w["reader"] == "audio"
    for reader in ("pdf", "docx", "xlsx", "epub", "zip", "tar", "gzip", "csv", "json", "yaml", "xml", "rss"):
        d = rd.read(reader, b"\x00\x01garbage", BASE + "/x")
        assert d["reader"] in (reader, "xml", "html") and isinstance(d.get("text"), str), reader
    assert rd.read("csv", b"", BASE + "/e.csv")["tables"][0]["rows"] == 0
