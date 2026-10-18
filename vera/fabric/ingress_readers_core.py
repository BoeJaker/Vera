"""File readers for the ingress (shared-planning web-ingress WI-6, PLAN §3.6 R1-R5 + R8): a body of any of the
types the fixture site serves becomes a ParsedDoc, with child items where a file holds several things.

Standard library wherever the format allows (Office and EPUB files are zips of XML; CSV, JSON, GeoJSON, gzip),
pypdf for PDF when it is installed (BSD, pure Python); a reader whose library is missing says so in the doc
(`needs_lib`) instead of failing. Every reader is capped (bytes in, rows and members out) and an archive member
is refused when it inflates past ingress_core.archive_member_ok. No I/O, no Vera imports beyond the ingress cores.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import tarfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

try:
    from Vera.vera.fabric import ingress_core as ic
    from Vera.vera.fabric import ingress_parse_core as pc
except ImportError:  # pragma: no cover
    from vera.fabric import ingress_core as ic
    from vera.fabric import ingress_parse_core as pc

MAX_ROWS = 2000
MAX_CELLS_TEXT = 200_000
MAX_MEMBERS = 50
MAX_PAGES = 200
MAX_CHILD_BYTES = 10 * 1024 * 1024

_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_NS_OPF = "{http://www.idpf.org/2007/opf}"
_NS_CNT = "{urn:oasis:names:tc:opendocument:xmlns:container}"


def _doc(reader: str, url: str, title: str, text: str, **extra: Any) -> Dict[str, Any]:
    d = pc.parse_text(text, url, reader=reader, title=title)
    d.update(extra)
    return d


# ── R1 PDF ────────────────────────────────────────────────────────────────────

def read_pdf(data: bytes, url: str) -> Dict[str, Any]:
    """Text per page through pypdf; a PDF with no text layer is marked needs_ocr (a T3 job, never inline)."""
    try:
        import pypdf  # type: ignore
    except ImportError:
        return _doc("pdf", url, "", "", needs_lib="pypdf", pages=0)
    try:
        r = pypdf.PdfReader(io.BytesIO(data))
        pages = []
        for i, p in enumerate(r.pages):
            if i >= MAX_PAGES:
                break
            try:
                pages.append(p.extract_text() or "")
            except Exception:  # noqa: BLE001
                pages.append("")
        meta = r.metadata or {}
        title = str(meta.get("/Title") or "").strip()
    except Exception as e:  # noqa: BLE001
        return _doc("pdf", url, "", "", error=f"pdf: {str(e)[:120]}", pages=0)
    text = "\n".join(t for t in pages if t).strip()
    d = _doc("pdf", url, title, text, pages=len(pages))
    d["needs_ocr"] = not text
    return d


def pdf_text_layer(data: bytes) -> bool:
    """Whether a PDF carries text operators at all (cheap, without pypdf): a scan has none."""
    return b"Tj" in data or b"TJ" in data


# ── R5 XLSX, R8 DOCX / EPUB (zips of XML) ─────────────────────────────────────

def _zip_ok(z: zipfile.ZipFile) -> Optional[str]:
    for i in z.infolist()[:MAX_MEMBERS * 4]:
        if not ic.archive_member_ok(i.compress_size, i.file_size):
            return f"archive member {i.filename} inflates {i.file_size} from {i.compress_size}: refused"
    return None


def read_docx(data: bytes, url: str) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = _zip_ok(z)
            if bad:
                return _doc("docx", url, "", "", error=bad)
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        return _doc("docx", url, "", "", error=f"docx: {str(e)[:120]}")
    paras = []
    for p in root.iter(_NS_W + "p"):
        t = "".join(x.text or "" for x in p.iter(_NS_W + "t")).strip()
        if t:
            paras.append(t)
    text = "\n".join(paras)[:MAX_CELLS_TEXT]
    return _doc("docx", url, paras[0][:200] if paras else "", text, paragraphs=len(paras))


def read_xlsx(data: bytes, url: str) -> Dict[str, Any]:
    """Every sheet as a table (header row + rows, capped); the doc's text is the cells joined, for search."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = _zip_ok(z)
            if bad:
                return _doc("xlsx", url, "", "", error=bad)
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            rid_to_target = {r.get("Id"): r.get("Target") for r in rels.iter(_NS_PR + "Relationship")}
            shared: List[str] = []
            if "xl/sharedStrings.xml" in z.namelist():
                ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in ss.iter(_NS_S + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(_NS_S + "t")))
            tables = []
            for sh in wb.iter(_NS_S + "sheet"):
                name, rid = sh.get("name") or "", sh.get(_NS_R + "id")
                target = rid_to_target.get(rid) or ""
                path = "xl/" + target.lstrip("/").replace("xl/", "", 1) if not target.startswith("xl/") else target
                if path not in z.namelist():
                    continue
                rows = _sheet_rows(ET.fromstring(z.read(path)), shared)
                tables.append({"name": name, "columns": rows[0] if rows else [], "rows": len(rows) - 1 if rows else 0,
                               "sample": rows[1:6]})
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        return _doc("xlsx", url, "", "", error=f"xlsx: {str(e)[:120]}")
    text = "\n".join(" ".join(str(c) for c in t["columns"]) + " " + " ".join(" ".join(str(c) for c in r) for r in t["sample"])
                     for t in tables)[:MAX_CELLS_TEXT]
    return _doc("xlsx", url, tables[0]["name"] if tables else "", text, tables=tables, sheets=len(tables))


def _sheet_rows(sheet: ET.Element, shared: List[str]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for r in sheet.iter(_NS_S + "row"):
        if len(rows) >= MAX_ROWS:
            break
        cells: List[Any] = []
        for c in r.iter(_NS_S + "c"):
            t = c.get("t")
            v = c.find(_NS_S + "v")
            if t == "s" and v is not None:
                try:
                    cells.append(shared[int(v.text or 0)])
                except (ValueError, IndexError):
                    cells.append("")
            elif t == "inlineStr":
                cells.append("".join(x.text or "" for x in c.iter(_NS_S + "t")))
            elif v is not None and v.text is not None:
                try:
                    f = float(v.text)
                    cells.append(int(f) if f.is_integer() else f)
                except ValueError:
                    cells.append(v.text)
            else:
                cells.append("")
        rows.append(cells)
    return rows


def read_epub(data: bytes, url: str) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = _zip_ok(z)
            if bad:
                return _doc("epub", url, "", "", error=bad)
            cont = ET.fromstring(z.read("META-INF/container.xml"))
            rf = cont.find(".//" + _NS_CNT + "rootfile")
            opf_path = rf.get("full-path") if rf is not None else "OEBPS/content.opf"
            opf = ET.fromstring(z.read(opf_path))
            base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
            title_el = next(opf.iter("{http://purl.org/dc/elements/1.1/}title"), None)
            title = (title_el.text or "").strip() if title_el is not None else ""
            items = {i.get("id"): i.get("href") for i in opf.iter(_NS_OPF + "item")}
            chapters = []
            for ref in opf.iter(_NS_OPF + "itemref"):
                href = items.get(ref.get("idref"))
                if not href:
                    continue
                path = base + href
                if path in z.namelist() and len(chapters) < MAX_MEMBERS:
                    chapters.append(pc.parse_html(z.read(path).decode("utf-8", "replace"), url + "#" + href)["text"])
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        return _doc("epub", url, "", "", error=f"epub: {str(e)[:120]}")
    return _doc("epub", url, title, "\n\n".join(chapters)[:MAX_CELLS_TEXT], chapters=len(chapters))


# ── archives (R8): members become children ────────────────────────────────────

def read_zip(data: bytes, url: str) -> Dict[str, Any]:
    """A zip's members as children (name, size, reader) - never inflated here beyond the bomb check; a child is
    read on demand by its own reader."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = _zip_ok(z)
            if bad:
                return _doc("zip", url, "", "", error=bad, children=[])
            children = []
            for i in z.infolist()[:MAX_MEMBERS]:
                if i.is_dir():
                    continue
                children.append({"name": i.filename, "size": i.file_size, "stored": i.compress_size,
                                 "reader": ic.reader_for("", i.filename, b""), "depth": 1})
    except zipfile.BadZipFile as e:
        return _doc("zip", url, "", "", error=f"zip: {str(e)[:120]}", children=[])
    return _doc("zip", url, "", " ".join(c["name"] for c in children), children=children)


def read_zip_member(data: bytes, name: str, url: str) -> Optional[Dict[str, Any]]:
    """One member of a zip, read by its own reader (depth 1 only: an archive inside an archive is listed, not read)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            i = z.getinfo(name)
            if not ic.archive_member_ok(i.compress_size, i.file_size) or i.file_size > MAX_CHILD_BYTES:
                return None
            body = z.read(name)
    except (zipfile.BadZipFile, KeyError):
        return None
    reader = ic.reader_for("", name, body[:4096])
    if reader in ("zip", "tar", "gzip"):
        return _doc(reader, url + "#" + name, name, "", children=[], depth=1, note="nested archive: listed, not read")
    d = read(reader, body, url + "#" + name)
    d["depth"] = 1
    return d


def read_tar(data: bytes, url: str) -> Dict[str, Any]:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
            children = [{"name": m.name, "size": m.size, "stored": m.size, "reader": ic.reader_for("", m.name, b""), "depth": 1}
                        for m in t.getmembers()[:MAX_MEMBERS] if m.isfile()]
    except (tarfile.TarError, EOFError) as e:
        return _doc("tar", url, "", "", error=f"tar: {str(e)[:120]}", children=[])
    return _doc("tar", url, "", " ".join(c["name"] for c in children), children=children)


def read_gzip(data: bytes, url: str) -> Dict[str, Any]:
    """A gzipped file: inflated once (bomb-checked) and read by the inner type (csv.gz, xml.gz sitemaps...)."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            inner = g.read(MAX_CHILD_BYTES + 1)
    except (OSError, EOFError) as e:
        return _doc("gzip", url, "", "", error=f"gzip: {str(e)[:120]}")
    if len(inner) > MAX_CHILD_BYTES or not ic.archive_member_ok(max(1, len(data)), len(inner)):
        return _doc("gzip", url, "", "", error="gzip: inflates past the cap: refused")
    inner_url = url[:-3] if url.lower().endswith(".gz") else url
    reader = ic.reader_for("", inner_url, inner[:4096])
    d = read(reader, inner, inner_url)
    d["compressed"] = "gzip"
    return d


# ── tabular and structured text ───────────────────────────────────────────────

def read_csv(data: bytes, url: str) -> Dict[str, Any]:
    text = data.decode("utf-8-sig", "replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for r in csv.reader(io.StringIO(text), dialect):
        rows.append(r)
        if len(rows) > MAX_ROWS:
            break
    cols = rows[0] if rows else []
    table = {"name": url.rsplit("/", 1)[-1], "columns": cols, "rows": max(0, len(rows) - 1), "sample": rows[1:6]}
    body = " ".join(cols) + " " + " ".join(" ".join(r) for r in rows[1:200])
    return _doc("csv", url, table["name"], body[:MAX_CELLS_TEXT], tables=[table])


def read_json(data: bytes, url: str, reader: str = "json") -> Dict[str, Any]:
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError as e:
        return _doc(reader, url, "", "", error=f"json: {str(e)[:120]}")
    kind, extra = _json_shape(obj, url)
    text = _json_text(obj)
    title = str(extra.pop("title", "") or "")
    if kind == "geojson":
        reader = "geojson"
    d = _doc(reader, url, title, text, structured=[{"source": "json", "type": kind, "data": _trim(obj)}], **extra)
    if kind in ("ckan", "geojson", "api_page", "graphql"):
        d["kind"] = {"ckan": "catalogue", "geojson": "file", "api_page": "api", "graphql": "api"}[kind]
    if kind == "graphql":
        d["reader"] = "graphql"
        d["text"] = extra.get("summary") or d["text"]
    return d


def graphql_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """An introspection answer as the API's shape: its object types with their fields, the query and mutation roots,
    built-in (__) types left out. The text is one line per type - what a search over APIs should find."""
    types = []
    for t in schema.get("types") or []:
        if not isinstance(t, dict) or str(t.get("name") or "").startswith("__") or t.get("kind") not in ("OBJECT", "INPUT_OBJECT", "ENUM", "INTERFACE"):
            continue
        fields = [str(f.get("name") or "") for f in (t.get("fields") or t.get("inputFields") or t.get("enumValues") or [])
                  if isinstance(f, dict)]
        types.append({"name": str(t.get("name") or ""), "kind": t.get("kind"), "fields": fields[:60]})
    roots = {k: str((schema.get(k) or {}).get("name") or "") for k in ("queryType", "mutationType", "subscriptionType")}
    lines = [f"{t['kind'].lower()} {t['name']}: {', '.join(t['fields'])}" for t in types[:400]]
    return {"title": "GraphQL schema", "types": len(types), "roots": roots,
            "summary": "GraphQL API. " + "; ".join(f"{k} {v}" for k, v in roots.items() if v) + "\n" + "\n".join(lines)}


def _json_shape(obj: Any, url: str) -> Tuple[str, Dict[str, Any]]:
    """What a JSON body is: a CKAN package_search answer (R3), a GeoJSON feature collection, a cursor-paginated page
    (R4), a GraphQL introspection answer (R10), or plain data."""
    if isinstance(obj, dict):
        schema = (obj.get("data") or {}).get("__schema") if isinstance(obj.get("data"), dict) else obj.get("__schema")
        if isinstance(schema, dict) and isinstance(schema.get("types"), list):
            return "graphql", graphql_schema(schema)
        if obj.get("type") == "FeatureCollection" and isinstance(obj.get("features"), list):
            names = [str((f.get("properties") or {}).get("name") or "") for f in obj["features"][:50] if isinstance(f, dict)]
            return "geojson", {"features": len(obj["features"]), "names": [n for n in names if n]}
        res = obj.get("result")
        if isinstance(res, dict) and isinstance(res.get("results"), list) and "package_search" in url:
            datasets = []
            for pkg in res["results"][:200]:
                if not isinstance(pkg, dict):
                    continue
                dists = [{"url": r.get("url") or "", "format": str(r.get("format") or "").lower(), "name": r.get("name") or ""}
                         for r in (pkg.get("resources") or []) if isinstance(r, dict) and r.get("url")]
                datasets.append({"name": pkg.get("name") or "", "title": pkg.get("title") or "", "distributions": dists})
            return "ckan", {"datasets": datasets, "count": int(res.get("count") or len(datasets)), "title": "CKAN catalogue"}
        items = obj.get("items") if isinstance(obj.get("items"), list) else (obj.get("results") if isinstance(obj.get("results"), list) else None)
        nxt = obj.get("next") or obj.get("next_url") or obj.get("next_cursor") or (obj.get("links") or {}).get("next") if isinstance(obj.get("links"), dict) else obj.get("next")
        if items is not None and ("next" in obj or "next_cursor" in obj or "cursor" in obj):
            return "api_page", {"items": len(items), "next": str(nxt) if nxt else ""}
    return "data", {}


def _json_text(obj: Any, limit: int = 20_000) -> str:
    out: List[str] = []

    def walk(x: Any, depth: int = 0) -> None:
        if sum(len(s) for s in out) > limit or depth > 6:
            return
        if isinstance(x, dict):
            for k, v in list(x.items())[:200]:
                if isinstance(v, (str, int, float)) and str(k) not in ("url", "href", "id", "@context"):
                    out.append(str(v)[:200])
                else:
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x[:200]:
                walk(v, depth + 1)
        elif isinstance(x, str):
            out.append(x[:200])
    walk(obj)
    return " ".join(out)[:limit]


def _trim(obj: Any, limit: int = 50_000) -> Any:
    s = json.dumps(obj)
    return obj if len(s) <= limit else {"truncated": True, "chars": len(s)}


def read_yaml(data: bytes, url: str) -> Dict[str, Any]:
    text = data.decode("utf-8", "replace")
    try:
        import yaml  # type: ignore
        obj = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        obj = None
    extra: Dict[str, Any] = {}
    if isinstance(obj, dict) and ("openapi" in obj or "swagger" in obj):
        paths = obj.get("paths") or {}
        extra = {"openapi": str(obj.get("openapi") or obj.get("swagger")), "title": str((obj.get("info") or {}).get("title") or ""),
                 "endpoints": [f"{m.upper()} {p}" for p, ops in list(paths.items())[:200] if isinstance(ops, dict)
                               for m in ops if m.lower() in ("get", "post", "put", "delete", "patch")]}
    title = str(extra.pop("title", "") or "")
    d = _doc("yaml", url, title, text[:MAX_CELLS_TEXT], **extra)
    if extra.get("openapi"):
        d["kind"] = "api"
    return d


def read_xml(data: bytes, url: str) -> Dict[str, Any]:
    """Feeds and sitemaps: entries / locations as children; other XML as text."""
    text = data.decode("utf-8", "replace")
    reader = ic.reader_for("application/xml", url, data[:4096])
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        return _doc(reader if reader in ("rss", "atom", "sitemap") else "xml", url, "", "", error=f"xml: {str(e)[:120]}")
    children: List[Dict[str, Any]] = []
    if reader == "rss":
        for it in list(root.iter("item"))[:MAX_ROWS]:
            children.append({"title": (it.findtext("title") or "").strip(), "url": (it.findtext("link") or "").strip(),
                             "published": (it.findtext("pubDate") or "").strip()})
        title = (root.findtext("./channel/title") or "").strip()
    elif reader == "atom":
        ns = "{http://www.w3.org/2005/Atom}"
        for e in list(root.iter(ns + "entry"))[:MAX_ROWS]:
            link = e.find(ns + "link")
            children.append({"title": (e.findtext(ns + "title") or "").strip(), "url": (link.get("href") if link is not None else "") or "",
                             "published": (e.findtext(ns + "updated") or "").strip()})
        title = (root.findtext(ns + "title") or "").strip()
    elif reader == "sitemap":
        ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        for tag in (ns + "sitemap", ns + "url"):
            for e in list(root.iter(tag))[:MAX_ROWS]:
                children.append({"url": (e.findtext(ns + "loc") or "").strip(), "lastmod": (e.findtext(ns + "lastmod") or "").strip(),
                                 "index": tag.endswith("sitemap")})
        title = "sitemap index" if root.tag.endswith("sitemapindex") else "sitemap"
    else:
        title = ""
    body = " ".join(filter(None, [c.get("title", "") for c in children])) or " ".join(t.strip() for t in root.itertext())
    return _doc(reader if reader in ("rss", "atom", "sitemap") else "xml", url, title, body[:MAX_CELLS_TEXT], children=children)


# ── R7: Parquet (schema and a sample; the whole file only under the reader's byte cap) ───────────────────────────

PARQUET_SAMPLE = 20


def read_parquet(data: bytes, url: str) -> Dict[str, Any]:
    """A Parquet file's schema, row count and first rows (pyarrow, read from the bytes in memory). Remote range
    reads for big files are not done here: a body past the reader's cap never reaches a reader."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return _doc("parquet", url, "", "", needs_lib="pyarrow")
    pf = pq.ParquetFile(io.BytesIO(data))
    cols = [f"{f.name} ({f.type})" for f in pf.schema_arrow]
    rows: List[List[Any]] = []
    if pf.num_row_groups:
        tbl = pf.read_row_group(0).slice(0, PARQUET_SAMPLE)
        rows = [list(r.values()) for r in tbl.to_pylist()]
    names = [f.name for f in pf.schema_arrow]
    text = "Parquet table: " + ", ".join(cols) + "\n" + "\n".join(", ".join(str(v)[:80] for v in r) for r in rows)
    return _doc("parquet", url, url.rsplit("/", 1)[-1], text[:MAX_CELLS_TEXT], kind="file", sample_only=True,
                tables=[{"name": "parquet", "columns": names, "rows": rows, "total_rows": int(pf.metadata.num_rows)}])


# ── R10: KML (placemarks, for godseye places) ─────────────────────────────────

def read_kml(data: bytes, url: str) -> Dict[str, Any]:
    """KML placemarks: name, description and the first coordinate (lon, lat) of each - the places a geo file names."""
    root = ET.fromstring(data)
    places = []
    for el in root.iter():
        if not el.tag.endswith("Placemark"):
            continue
        rec: Dict[str, Any] = {}
        for sub in el.iter():
            tag = sub.tag.rsplit("}", 1)[-1]
            if tag == "name" and "name" not in rec:
                rec["name"] = (sub.text or "").strip()
            elif tag == "description" and "description" not in rec:
                rec["description"] = re.sub(r"<[^>]+>", " ", sub.text or "").strip()[:500]
            elif tag == "coordinates" and "lon" not in rec:
                first = (sub.text or "").strip().split()
                parts = first[0].split(",") if first else []
                try:
                    rec["lon"], rec["lat"] = float(parts[0]), float(parts[1])
                except (IndexError, ValueError):
                    pass
        if rec:
            places.append(rec)
        if len(places) >= MAX_ROWS:
            break
    title = next((t.text.strip() for t in root.iter() if t.tag.endswith("name") and t.text), "")
    text = "\n".join(f"{p.get('name', '')}: {p.get('description', '')}".strip(": ") for p in places)
    return _doc("kml", url, title, text[:MAX_CELLS_TEXT], kind="file", places=places)


# ── R9: media - described for a T3 job, never processed inline ────────────────

MEDIA_OPS = {"image": "describe", "audio": "transcribe", "video": "transcribe"}


def read_media(reader: str, data: bytes, url: str) -> Dict[str, Any]:
    """An image, audio or video file: no text now. The doc carries what a background (T3) job should do with it -
    describe an image, transcribe audio or a video's sound - so the crawl queues it and never runs a model inline."""
    op = MEDIA_OPS.get(reader, "")
    return _doc(reader, url, url.rsplit("/", 1)[-1][:200], "", kind="media",
                t3={"op": op, "url": url, "reader": reader, "bytes": len(data or b"")} if op else None)


def youtube_id(url: str) -> str:
    """The video id of a YouTube watch/short/embed/youtu.be URL ('' otherwise) - captions are a T3 job too."""
    m = re.match(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)([\w-]{11})", url or "")
    return m.group(1) if m else ""


# ── R5 (ODS): an OpenDocument spreadsheet is a zip of content.xml ──────────────

_NS_T = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def read_ods(data: bytes, url: str) -> Dict[str, Any]:
    z = zipfile.ZipFile(io.BytesIO(data))
    bad = _zip_ok(z)
    if bad:
        return _doc("ods", url, "", "", error=bad)
    root = ET.fromstring(z.read("content.xml"))
    tables, lines = [], []
    for tb in root.iter(_NS_T + "table"):
        rows: List[List[str]] = []
        for tr in tb.iter(_NS_T + "table-row"):
            cells = [" ".join(t.strip() for t in c.itertext()).strip() for c in tr.iter(_NS_T + "table-cell")]
            if any(cells):
                rows.append(cells)
            if len(rows) >= MAX_ROWS:
                break
        name = tb.get(_NS_T + "name") or "sheet"
        tables.append({"name": name, "rows": rows})
        lines += [name + ": " + ", ".join(r) for r in rows[:200]]
    return _doc("ods", url, url.rsplit("/", 1)[-1], "\n".join(lines)[:MAX_CELLS_TEXT], kind="file", tables=tables)


# ── the router ────────────────────────────────────────────────────────────────

READERS = {
    "html": lambda b, u: pc.parse_html(b.decode("utf-8", "replace"), u),
    "text": lambda b, u: pc.parse_text(b.decode("utf-8", "replace"), u, reader="text"),
    "pdf": read_pdf, "docx": read_docx, "xlsx": read_xlsx, "epub": read_epub, "zip": read_zip, "tar": read_tar,
    "gzip": read_gzip, "csv": read_csv, "json": read_json, "geojson": lambda b, u: read_json(b, u, "geojson"),
    "yaml": read_yaml,
    "xml": read_xml, "rss": read_xml, "atom": read_xml, "sitemap": read_xml,
    "parquet": read_parquet, "kml": read_kml, "ods": read_ods,
    "image": lambda b, u: read_media("image", b, u), "audio": lambda b, u: read_media("audio", b, u),
    "video": lambda b, u: read_media("video", b, u),
}


def read(reader: str, data: bytes, url: str) -> Dict[str, Any]:
    """The ParsedDoc for a body of a known reader type; media and binary give a doc with no text (metadata only)."""
    if (data or b"")[:2] == b"\x1f\x8b" and reader != "gzip":      # a .xml.gz sitemap routes as sitemap: inflate first
        fn = read_gzip
    else:
        fn = READERS.get(reader)
    if fn is None:
        return _doc(reader or "binary", url, "", "", bytes=len(data or b""))
    try:
        d = fn(data or b"", url)
    except Exception as e:  # noqa: BLE001 - a bad file is a doc with an error, never an exception on the pipeline
        d = _doc(reader, url, "", "", error=f"{reader}: {str(e)[:120]}")
    d.setdefault("bytes", len(data or b""))
    return d


def child_urls(doc: Dict[str, Any], base_url: str, limit: int = 200) -> List[str]:
    """The URLs a read file points at, for a crawl to follow: a catalogue's distributions, a sitemap's or feed's
    entries. Archive members are not URLs (they are read from the archive itself)."""
    from urllib.parse import urljoin
    out: List[str] = []

    def add(u: Any) -> None:
        u = str(u or "").strip()
        if not u:
            return
        a = urljoin(base_url, u)
        if a.startswith(("http://", "https://")) and a not in out:
            out.append(a)
    for ds in doc.get("datasets") or []:
        for d in ds.get("distributions") or []:
            add(d.get("url"))
    if doc.get("reader") in ("sitemap", "rss", "atom"):
        for c in doc.get("children") or []:
            add(c.get("url"))
    if doc.get("next"):
        add(doc["next"])
    return out[:limit]
