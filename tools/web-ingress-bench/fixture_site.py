#!/usr/bin/env python3
"""The web-ingress fixture site (shared-planning web-ingress, WI-0): one deterministic little web with every kind of
thing the ingress has to map - pages, duplicates under other URLs, a near-duplicate, a redirect chain, an ETag/304
path, robots.txt with a disallowed path, a slow path, files (PDF with text, PDF with no text, DOCX, XLSX, gzipped CSV,
ZIP, EPUB, GeoJSON, PNG, WAV, an over-cap file, a zip bomb), feeds, a sitemap index with gzipped children and
lastmod, a CKAN catalogue, a cursor-paginated JSON API and an OpenAPI YAML.

Standard library only. The same responses are served three ways:
  respond(method, path, headers)  -> Response          (pure; tests)
  mock_transport()                -> httpx.MockTransport (in-process; tests and the ingress's own tests)
  serve(port)                     -> a real HTTP server  (benchmarks against a running Vera)

TRUTH says what a correct map of the site holds: one entry per canonical URL with its kind, the links a page carries,
which URLs are the same content, which one is a near-duplicate, and which files must never be downloaded in full.

    python3 tools/web-ingress-bench/fixture_site.py --port 8765      # serve until Ctrl-C
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import struct
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

LASTMOD = "2026-10-01T00:00:00+00:00"
LAST_MODIFIED_HTTP = "Thu, 01 Oct 2026 00:00:00 GMT"
OVER_CAP_BYTES = 12 * 1024 * 1024        # above the 10 MB document cap (PLAN §3.6)
SLOW_S = 2.0


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes = b""
    delay_s: float = 0.0


@dataclass
class Item:
    """One URL of the site as a correct map should record it."""
    path: str
    kind: str                       # page | file | feed | sitemap | api | catalogue | media | robots
    mime: str
    links: List[str] = field(default_factory=list)   # paths this page links to (pages only)
    same_as: str = ""               # canonical path when this URL serves identical content
    near_dup_of: str = ""           # canonical path of a near-duplicate
    sample_only: bool = False       # over the cap: HEAD + range sample, never the full body
    reject: bool = False            # must be refused (zip bomb)
    needs_ocr: bool = False         # a PDF with no text layer
    disallowed: bool = False        # robots.txt says Disallow (recorded, not obeyed - owner, 11 Oct)


# ── file builders (valid, minimal, deterministic) ─────────────────────────────

def _pdf(lines: List[str]) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = "".join(f"BT /F1 12 Tf 72 {720 - 18 * i} Td ({esc(t)}) Tj ET\n" for i, t in enumerate(lines))
    stream = ops.encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def _zip(members: List[Tuple[str, bytes]], *, stored_first: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, (name, data) in enumerate(members):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if (stored_first and i == 0) else zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


def _docx() -> bytes:
    ct = (b'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          b'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships'
          b'+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
          b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = (b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            b'2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            b'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    doc = (b'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           b'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Fixture memo: lab space demand in Cambridge rose '
           b'for the third quarter.</w:t></w:r></w:p></w:body></w:document>')
    return _zip([("[Content_Types].xml", ct), ("_rels/.rels", rels), ("word/document.xml", doc)])


def _xlsx() -> bytes:
    ns = b'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ct = (b'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          b'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
          b'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/'
          b'workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          b'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          b'spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/'
          b'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
    rels = (b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            b'2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            b'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = (b'<?xml version="1.0" encoding="UTF-8"?><workbook ' + ns + b' ' + rns + b'><sheets>'
          b'<sheet name="Rents" sheetId="1" r:id="rId1"/><sheet name="Yields" sheetId="2" r:id="rId2"/></sheets>'
          b'</workbook>')
    wbrels = (b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
              b'2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
              b'2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://'
              b'schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/'
              b'sheet2.xml"/></Relationships>')

    def sheet(rows: List[List[object]]) -> bytes:
        out = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, v in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>')
            out.append(f'<row r="{r}">{"".join(cells)}</row>')
        return (b'<?xml version="1.0" encoding="UTF-8"?><worksheet ' + ns + b'><sheetData>'
                + "".join(out).encode() + b'</sheetData></worksheet>')
    s1 = sheet([["region", "rent_psf"], ["Cambridge", 52.5], ["Oxford", 49.0], ["Milton Keynes", 18.25]])
    s2 = sheet([["sector", "yield_pct"], ["Industrial", 5.25], ["Office", 6.75]])
    return _zip([("[Content_Types].xml", ct), ("_rels/.rels", rels), ("xl/workbook.xml", wb),
                 ("xl/_rels/workbook.xml.rels", wbrels), ("xl/worksheets/sheet1.xml", s1),
                 ("xl/worksheets/sheet2.xml", s2)])


def _epub() -> bytes:
    container = (b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                 b'container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-'
                 b'package+xml"/></rootfiles></container>')
    opf = (b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">fixture-book</dc:identifier>'
           b'<dc:title>Fixture Handbook</dc:title><dc:language>en</dc:language></metadata><manifest><item id="c1" '
           b'href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine>'
           b'</package>')
    ch = (b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head><body>'
          b'<h1>Chapter one</h1><p>Science parks along the Oxford-Cambridge arc.</p></body></html>')
    return _zip([("mimetype", b"application/epub+zip"), ("META-INF/container.xml", container),
                 ("OEBPS/content.opf", opf), ("OEBPS/c1.xhtml", ch)], stored_first=True)


def _png() -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"\x00\xff\x00\x00"                     # one red pixel, filter byte 0
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _wav(seconds: float = 0.25, rate: int = 8000) -> bytes:
    n = int(seconds * rate)
    data = b"\x00\x00" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate,
            rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


@lru_cache(maxsize=1)
def _bomb() -> bytes:
    return _zip([("zeros.bin", b"\x00" * (64 * 1024 * 1024))])     # ~64 KB on the wire, 64 MB inflated


def _gz(b: bytes) -> bytes:
    return gzip.compress(b, mtime=0)


# ── pages ─────────────────────────────────────────────────────────────────────

def _html(title: str, body: str, *, head: str = "") -> bytes:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{title}</title>{head}</head>"
            f"<body><h1>{title}</h1>{body}</body></html>").encode()


def _links(paths: List[str]) -> str:
    return "<ul>" + "".join(f'<li><a href="{p}">{p}</a></li>' for p in paths) + "</ul>"


HOME_LINKS = ["/a", "/a?utm_source=newsletter", "/a/", "/a-copy", "/b", "/c", "/d", "/e", "/old", "/etag", "/slow",
              "/private/page", "/files/report.pdf", "/files/scan.pdf", "/files/memo.docx", "/files/rents.xlsx",
              "/files/data.csv.gz", "/files/bundle.zip", "/files/handbook.epub", "/files/places.geojson",
              "/files/photo.png", "/files/briefing.wav", "/files/big.bin", "/files/bomb.zip", "/feed.rss",
              "/feed.atom", "/sitemap.xml", "/api/3/action/package_search", "/api/items", "/openapi.yaml"]

A_TEXT = ("<p>Published 1 October 2026.</p><p>Lab space vacancy in Cambridge fell to 2.1 per cent as demand from "
          "life-science firms continued. Developers brought forward three schemes on the northern fringe.</p>")


def _page_bodies() -> Dict[str, bytes]:
    a = _html("Lab space in Cambridge", A_TEXT + _links(["/b", "/c"]))
    return {
        "/": _html("Fixture home", "<p>Everything the ingress must map.</p>" + _links(HOME_LINKS)),
        "/a": a,
        "/a-copy": _html("Lab space in Cambridge", A_TEXT.replace("1 October 2026", "2 October 2026")
                         + _links(["/b", "/c"])),
        "/b": _html("Rents table", "<table><tr><th>Region</th><th>Rent psf</th></tr><tr><td>Cambridge</td><td>52.5"
                    "</td></tr><tr><td>Oxford</td><td>49.0</td></tr></table>" + _links(["/a", "/files/rents.xlsx"])),
        "/c": _html("Science park sale", "<p>A science park changed hands.</p>" + _links(["/d"]), head=(
            '<script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle",'
            '"headline":"Science park sale","datePublished":"2026-10-01","author":{"@type":"Organization",'
            '"name":"Fixture News"}}</script><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"price_gbp":125000000,"buyer":"Fixture REIT"}}}</script>')),
        "/d": _html("Destination", "<p>The end of the redirect chain.</p>" + _links(["/"])),
        "/e": _html("Feeds and data", "<p>Feeds and catalogues.</p>" + _links(["/feed.rss", "/sitemap.xml"])),
        "/etag": _html("Revalidated page", "<p>Answers 304 to a matching If-None-Match.</p>"),
        "/slow": _html("Slow page", "<p>This host is slow.</p>"),
        "/private/page": _html("Disallowed page", "<p>robots.txt disallows this path.</p>"),
    }


def _feeds() -> Dict[str, Tuple[str, bytes]]:
    rss = ('<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture news</title><link>/</link>'
           '<item><title>Lab space</title><link>/a</link><pubDate>Thu, 01 Oct 2026 00:00:00 GMT</pubDate></item>'
           '<item><title>Sale</title><link>/c</link><pubDate>Thu, 01 Oct 2026 00:00:00 GMT</pubDate></item>'
           '</channel></rss>').encode()
    atom = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Fixture atom</title>'
            f'<updated>{LASTMOD}</updated><entry><title>Rents</title><link href="/b"/><updated>{LASTMOD}</updated>'
            '<id>fixture-b</id></entry></feed>').encode()
    index = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             f'<sitemap><loc>/sitemap-1.xml.gz</loc><lastmod>{LASTMOD}</lastmod></sitemap>'
             f'<sitemap><loc>/sitemap-2.xml.gz</loc><lastmod>{LASTMOD}</lastmod></sitemap></sitemapindex>').encode()

    def urlset(paths: List[str]) -> bytes:
        return _gz(('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(f"<url><loc>{p}</loc><lastmod>{LASTMOD}</lastmod></url>" for p in paths)
                    + "</urlset>").encode())
    return {
        "/feed.rss": ("application/rss+xml", rss),
        "/feed.atom": ("application/atom+xml", atom),
        "/sitemap.xml": ("application/xml", index),
        "/sitemap-1.xml.gz": ("application/gzip", urlset(["/a", "/b", "/c"])),
        "/sitemap-2.xml.gz": ("application/gzip", urlset(["/d", "/e", "/files/report.pdf"])),
    }


CKAN = {"success": True, "result": {"count": 3, "results": [
    {"name": "lab-vacancy", "title": "Lab vacancy by city", "resources": [
        {"url": "/files/data.csv.gz", "format": "CSV"}, {"url": "/files/rents.xlsx", "format": "XLSX"}]},
    {"name": "science-parks", "title": "Science parks", "resources": [
        {"url": "/files/places.geojson", "format": "GeoJSON"}, {"url": "/files/report.pdf", "format": "PDF"}]},
    {"name": "handbook", "title": "Fixture handbook", "resources": [
        {"url": "/files/handbook.epub", "format": "EPUB"}]}]}}

API_PAGES = {"": {"items": [{"id": 1}, {"id": 2}], "next": "p2"},
             "p2": {"items": [{"id": 3}, {"id": 4}], "next": "p3"},
             "p3": {"items": [{"id": 5}], "next": None}}

OPENAPI = (b"openapi: 3.0.0\ninfo:\n  title: Fixture API\n  version: '1'\npaths:\n  /api/items:\n    get:\n"
           b"      summary: List items\n      parameters:\n        - name: cursor\n          in: query\n"
           b"          schema: {type: string}\n      responses:\n        '200': {description: ok}\n")

ROBOTS = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 1\nSitemap: /sitemap.xml\n"


@lru_cache(maxsize=1)
def _files() -> Dict[str, Tuple[str, bytes]]:
    report = _pdf(["Fixture PDF report", "Region   Rent psf", "Cambridge   52.5", "Oxford   49.0"])
    return {
        "/files/report.pdf": ("application/pdf", report),
        "/files/scan.pdf": ("application/pdf", _pdf([])),
        "/files/memo.docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _docx()),
        "/files/rents.xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _xlsx()),
        "/files/data.csv.gz": ("application/gzip", _gz(b"city,vacancy_pct\nCambridge,2.1\nOxford,3.4\n")),
        "/files/bundle.zip": ("application/zip", _zip([("bundle/table.csv", b"a,b\n1,2\n"),
                                                       ("bundle/report.pdf", report)])),
        "/files/handbook.epub": ("application/epub+zip", _epub()),
        "/files/places.geojson": ("application/geo+json", json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "Fixture Science Park"},
             "geometry": {"type": "Point", "coordinates": [0.1218, 52.2053]}}]}).encode()),
        "/files/photo.png": ("image/png", _png()),
        "/files/briefing.wav": ("audio/wav", _wav()),
    }


# ── the truth table ───────────────────────────────────────────────────────────

def truth() -> Dict[str, Item]:
    pages = _page_bodies()
    t: Dict[str, Item] = {}
    for p in pages:
        t[p] = Item(p, "page", "text/html", links=_page_links(p))
    t["/a?utm_source=newsletter"] = Item("/a?utm_source=newsletter", "page", "text/html", links=_page_links("/a"),
                                         same_as="/a")
    t["/a/"] = Item("/a/", "page", "text/html", links=_page_links("/a"), same_as="/a")
    t["/a-copy"].near_dup_of = "/a"
    t["/private/page"].disallowed = True
    t["/old"] = Item("/old", "page", "text/html", same_as="/d")           # 301 -> /older -> 302 -> /d
    t["/older"] = Item("/older", "page", "text/html", same_as="/d")
    for p, (mime, _) in _files().items():
        kind = "media" if mime.startswith(("image/", "audio/")) else "file"
        t[p] = Item(p, kind, mime, needs_ocr=(p == "/files/scan.pdf"))
    t["/files/big.bin"] = Item("/files/big.bin", "file", "application/octet-stream", sample_only=True)
    t["/files/bomb.zip"] = Item("/files/bomb.zip", "file", "application/zip", reject=True)
    for p, (mime, _) in _feeds().items():
        t[p] = Item(p, "sitemap" if "sitemap" in p else "feed", mime)
    t["/api/3/action/package_search"] = Item("/api/3/action/package_search", "catalogue", "application/json")
    t["/api/items"] = Item("/api/items", "api", "application/json")
    t["/openapi.yaml"] = Item("/openapi.yaml", "api", "application/yaml")
    t["/robots.txt"] = Item("/robots.txt", "robots", "text/plain")
    return t


def _page_links(path: str) -> List[str]:
    import re
    body = _page_bodies().get(path, b"").decode()
    return re.findall(r'href="([^"]+)"', body)


def distinct_contents() -> int:
    """How many distinct bodies a correct map stores after dedupe (same_as URLs collapse; near-duplicates do not)."""
    return sum(1 for it in truth().values() if not it.same_as and not it.reject and it.kind != "robots")


# ── responses ─────────────────────────────────────────────────────────────────

def _etag(b: bytes) -> str:
    return '"' + hashlib.sha256(b).hexdigest()[:16] + '"'


def respond(method: str, target: str, headers: Optional[Dict[str, str]] = None) -> Response:
    """The site's answer to one request. target is the path plus query; headers are matched case-insensitively."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    u = urlsplit(target)
    path, q = u.path or "/", parse_qs(u.query)
    head = method.upper() == "HEAD"

    def ok(mime: str, body: bytes, *, extra: Optional[Dict[str, str]] = None, delay: float = 0.0) -> Response:
        hd = {"content-type": mime, "content-length": str(len(body)), "last-modified": LAST_MODIFIED_HTTP,
              "etag": _etag(body)}
        hd.update(extra or {})
        if h.get("if-none-match") == hd["etag"] or h.get("if-modified-since") == LAST_MODIFIED_HTTP:
            return Response(304, {"etag": hd["etag"], "last-modified": LAST_MODIFIED_HTTP}, b"", delay)
        return Response(200, hd, b"" if head else body, delay)

    pages = _page_bodies()
    if path == "/robots.txt":
        return ok("text/plain", ROBOTS)
    if path == "/old":
        return Response(301, {"location": "/older"})
    if path == "/older":
        return Response(302, {"location": "/d"})
    if path == "/a/":
        path = "/a"
    if path == "/slow":
        return ok("text/html; charset=utf-8", pages["/slow"], delay=SLOW_S)
    if path in pages:
        return ok("text/html; charset=utf-8", pages[path])
    files = _files()
    if path in files:
        mime, body = files[path]
        return ok(mime, body)
    if path == "/files/big.bin":
        rng = h.get("range", "")
        if rng.startswith("bytes=0-"):
            end = min(int(rng.split("-")[1] or 65535), OVER_CAP_BYTES - 1)
            body = b"\x00" * (end + 1)
            return Response(206, {"content-type": "application/octet-stream", "content-length": str(len(body)),
                                  "content-range": f"bytes 0-{end}/{OVER_CAP_BYTES}"}, b"" if head else body)
        return Response(200, {"content-type": "application/octet-stream", "content-length": str(OVER_CAP_BYTES),
                              "accept-ranges": "bytes"}, b"" if head else b"\x00" * OVER_CAP_BYTES)
    if path == "/files/bomb.zip":
        return ok("application/zip", _bomb())
    feeds = _feeds()
    if path in feeds:
        mime, body = feeds[path]
        return ok(mime, body)
    if path == "/api/3/action/package_search":
        return ok("application/json", json.dumps(CKAN).encode())
    if path == "/api/items":
        cur = (q.get("cursor") or [""])[0]
        if cur not in API_PAGES:
            return Response(400, {"content-type": "application/json"}, b'{"error":"bad cursor"}')
        page = dict(API_PAGES[cur])
        if page["next"]:
            page["next"] = f"/api/items?cursor={page['next']}"
        return ok("application/json", json.dumps(page).encode())
    if path == "/openapi.yaml":
        return ok("application/yaml", OPENAPI)
    return Response(404, {"content-type": "text/plain"}, b"not found")


def mock_transport(*, sleep: bool = False):
    """An httpx.MockTransport serving the site (any host name); sleep=True honours the slow path."""
    import httpx

    def handler(request: "httpx.Request") -> "httpx.Response":
        r = respond(request.method, request.url.raw_path.decode(), dict(request.headers))
        if sleep and r.delay_s:
            time.sleep(r.delay_s)
        return httpx.Response(r.status, headers=r.headers, content=r.body)
    return httpx.MockTransport(handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "VeraFixture/1"

    def _go(self, method: str) -> None:
        r = respond(method, self.path, dict(self.headers.items()))
        if r.delay_s:
            time.sleep(r.delay_s)
        self.send_response(r.status)
        for k, v in r.headers.items():
            self.send_header(k, v)
        if "content-length" not in r.headers:
            self.send_header("content-length", str(len(r.body)))
        self.end_headers()
        if method != "HEAD" and r.body:
            self.wfile.write(r.body)

    def do_GET(self) -> None:  # noqa: N802
        self._go("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._go("HEAD")

    def log_message(self, *a) -> None:  # quiet
        pass


def serve(port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """A real server for benchmarks; the caller runs serve_forever() (or a thread)."""
    return ThreadingHTTPServer((host, port), _Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = serve(a.port, a.host)
    print(f"fixture site on http://{a.host}:{a.port}/  ({len(truth())} URLs)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
