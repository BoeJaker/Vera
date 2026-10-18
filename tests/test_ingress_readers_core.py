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


def test_child_urls_follow_catalogues_sitemaps_feeds_and_api_pages():
    c = _read("/api/3/action/package_search")
    urls = rd.child_urls(c, BASE + "/api/3/action/package_search")
    assert BASE + "/files/data.csv.gz" in urls and BASE + "/files/handbook.epub" in urls and len(urls) == 5
    s = _read("/sitemap.xml")
    assert rd.child_urls(s, BASE + "/sitemap.xml") == [BASE + "/sitemap-1.xml.gz", BASE + "/sitemap-2.xml.gz"]
    f = _read("/feed.rss")
    assert rd.child_urls(f, BASE + "/feed.rss") == [BASE + "/a", BASE + "/c"]
    a = _read("/api/items")
    assert rd.child_urls(a, BASE + "/api/items") == [BASE + "/api/items?cursor=p2"]
    z = _read("/files/bundle.zip")
    assert rd.child_urls(z, BASE + "/files/bundle.zip") == []          # archive members are read, not fetched


def test_the_crawl_reads_files_only_behind_its_switch_and_before_the_old_fallback():
    import ast
    src = (ROOT / "vera" / "fabric" / "discovery.py").read_text(encoding="utf-8")
    new = src.index('if "text/html" not in ct and "text/plain" not in ct and _ingress_read_files_on():')
    old = src.index('            if "text/html" not in ct and "text/plain" not in ct:\n                # Non-HTML, non-data file')
    assert new < old
    block = src[new:old]
    for name in ("_read_file_doc", "_file_child_urls", "crawl_record", "_write_fabric_page", "file_read", "_qn"):
        assert name in block, name
    assert "queued_norm" not in block
    tree = ast.parse(src)
    fns = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"_read_file_doc", "_file_child_urls", "_ingress_read_files_on"} <= fns
    from vera.minds import switchboard_core as SB
    assert SB.REGISTRY["discovery.read_files"].default == "off"


def test_no_crawl_function_uses_a_module_alias_it_also_assigns_locally():
    """18 Oct: _process assigns a local `_pc` (path counters), so `_pc.crawl_record` inside it was an
    UnboundLocalError that the crawl worker swallowed - files were never read on prod. Any module alias a function
    reads as `alias.attr` must not also be assigned in that function."""
    import ast
    src = (ROOT / "vera" / "fabric" / "discovery.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    aliases = {a.asname for n in tree.body if isinstance(n, ast.Try) for b in n.body if isinstance(b, ast.ImportFrom)
               for a in b.names if a.asname}
    aliases |= {a.asname for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names if a.asname}
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = [n for n in ast.walk(fn) if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn)]
        assigned = {t.id for n in ast.walk(fn) if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        used_as_module = {n.value.id for n in ast.walk(fn) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for a in aliases & assigned & used_as_module:
            bad.append((fn.name, a))
    assert ("_process", "_pc") not in bad or "_ipc.crawl_record(" in src
    block = src[src.index("and _ingress_read_files_on():"):src.index("# Non-HTML, non-data file")]
    assert "_ipc.crawl_record(" in block and "_pc.crawl_record(" not in block.replace("_ipc.crawl_record(", "")



# ── WI-7a: R7 Parquet, R10 KML + GraphQL, R9 media, R5 ODS ────────────────────

def test_parquet_reads_schema_and_a_sample():
    import io as _io
    import pytest
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq
    buf = _io.BytesIO()
    pq.write_table(pa.table({"city": ["Cambridge", "Oxford", "Leeds"], "vacancy": [2.1, 3.4, 7.0]}), buf)
    d = rd.read("parquet", buf.getvalue(), "https://data.example/lab.parquet")
    assert d["reader"] == "parquet" and d["kind"] == "file" and d["sample_only"]
    t = d["tables"][0]
    assert t["columns"] == ["city", "vacancy"] and t["total_rows"] == 3 and t["rows"][0] == ["Cambridge", 2.1]
    assert "Cambridge" in d["text"] and "vacancy (double)" in d["text"]
    bad = rd.read("parquet", b"not parquet", "https://data.example/x.parquet")
    assert bad.get("error")


def test_kml_placemarks():
    kml = b"""<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Labs</name>
    <Placemark><name>Babraham</name><description><![CDATA[<b>campus</b>]]></description>
    <Point><coordinates>0.205,52.135,0</coordinates></Point></Placemark>
    <Placemark><name>No point</name></Placemark></Document></kml>"""
    d = rd.read("kml", kml, "https://geo.example/labs.kml")
    assert d["title"] == "Labs" and d["places"][0] == {"name": "Babraham", "description": "campus", "lon": 0.205, "lat": 52.135}
    assert d["places"][1] == {"name": "No point"} and "Babraham: campus" in d["text"]


def test_graphql_introspection_answer():
    import json as _json
    body = _json.dumps({"data": {"__schema": {"queryType": {"name": "Query"}, "mutationType": None, "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [{"name": "listing"}, {"name": "search"}]},
        {"kind": "OBJECT", "name": "Listing", "fields": [{"name": "id"}, {"name": "price"}]},
        {"kind": "OBJECT", "name": "__Type", "fields": [{"name": "name"}]},
        {"kind": "SCALAR", "name": "String"}]}}}).encode()
    d = rd.read("json", body, "https://api.example/graphql")
    assert d["reader"] == "graphql" and d["kind"] == "api" and d["types"] == 2
    assert "object Listing: id, price" in d["text"] and "queryType Query" in d["text"] and "__Type" not in d["text"]


def test_media_is_described_for_a_t3_job_never_read_inline():
    for reader, op in (("image", "describe"), ("audio", "transcribe"), ("video", "transcribe")):
        d = rd.read(reader, b"\x00" * 64, f"https://m.example/f.{reader}")
        assert d["kind"] == "media" and d["text"] == "" and d["t3"]["op"] == op and d["t3"]["bytes"] == 64
    assert rd.youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1") == "dQw4w9WgXcQ"
    assert rd.youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ" and rd.youtube_id("https://example.com/v") == ""


def test_ods_sheets():
    import io as _io, zipfile as _zf
    content = (b'<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               b'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
               b'<office:body><office:spreadsheet><table:table table:name="Rents"><table:table-row>'
               b'<table:table-cell><text:p>City</text:p></table:table-cell><table:table-cell><text:p>Rent</text:p></table:table-cell>'
               b'</table:table-row><table:table-row><table:table-cell><text:p>Leeds</text:p></table:table-cell>'
               b'<table:table-cell><text:p>950</text:p></table:table-cell></table:table-row></table:table>'
               b'</office:spreadsheet></office:body></office:document-content>')
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("content.xml", content)
    d = rd.read("ods", buf.getvalue(), "https://data.example/rents.ods")
    assert d["tables"][0] == {"name": "Rents", "rows": [["City", "Rent"], ["Leeds", "950"]]} and "Rents: Leeds, 950" in d["text"]
