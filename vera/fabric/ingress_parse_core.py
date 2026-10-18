"""Parse once (shared-planning web-ingress WI-4; PLAN §15). Pure.

A fetched body becomes ONE ParsedDoc, keyed by its content hash, that every consumer reads: the crawl, the page
ingest, web.fetch's background ingest, the reader. Today each of them parses the page again its own way.

Also here, pure: the dedupe decision a page ingest makes from what the web map already knows about the content,
and the shape of the deferred-enrichment job the crawl hands to the idle queue when `discovery.defer_enrichment`
is on (the entity pass that made an 8-page crawl take 299 s instead of 28 s runs later, off the crawl).
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Dict, List, Optional, Sequence

try:
    from Vera.vera.fabric import ingress_core as ic
except ImportError:  # pragma: no cover
    from vera.fabric import ingress_core as ic

PARSER_VERSION = 1
MAX_TEXT = 400_000
MAX_HTML = 3_000_000

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SCRIPT = re.compile(r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(r'<meta\s+[^>]*(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\']([^"\']*)["\']', re.I)
_META_DESC2 = re.compile(r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\'](?:description|og:description)["\']', re.I)
_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.I | re.S)
_MAIN = re.compile(r"<(main|article)\b[^>]*>(.*?)</\1>", re.I | re.S)
_ROLE_MAIN = re.compile(r'<(\w+)\b[^>]*role=["\']main["\'][^>]*>(.*?)</\1>', re.I | re.S)
_BODY = re.compile(r"<body\b[^>]*>(.*?)</body>", re.I | re.S)
_BOILER = re.compile(r"<(nav|header|footer|aside|form)\b[^>]*>.*?</\1>", re.I | re.S)
_JSONLD = re.compile(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
_NEXT = re.compile(r'<script\b[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S)
_TABLE = re.compile(r"<table\b", re.I)
_TR = re.compile(r"<tr\b", re.I)
_PRE = re.compile(r"<(pre|code)\b[^>]*>(.*?)</\1>", re.I | re.S)
_LI = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
_BLOCK_BREAK = re.compile(r"</(p|div|li|tr|h[1-6]|section|article|br|td|th)\s*>|<br\s*/?>", re.I)
_LANG = re.compile(r'<html\b[^>]*\blang=["\']?([a-zA-Z-]{2,8})', re.I)


def _clean(fragment: str) -> str:
    """Tag-stripped, entity-decoded, whitespace-normalised text with block boundaries kept as spaces."""
    s = _BLOCK_BREAK.sub(" ", fragment or "")
    s = _TAG.sub(" ", s)
    s = _html.unescape(s)
    return _WS.sub(" ", s).strip()


def _main_html(body: str) -> str:
    m = _MAIN.search(body) or _ROLE_MAIN.search(body)
    if m:
        return m.group(2)
    return _BOILER.sub(" ", body)


def _structured(html_src: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in _JSONLD.finditer(html_src):
        try:
            d = json.loads(_html.unescape(m.group(1)).strip())
        except ValueError:
            continue
        for item in (d if isinstance(d, list) else [d]):
            if isinstance(item, dict):
                out.append({"source": "json-ld", "type": str(item.get("@type") or ""), "data": item})
    m = _NEXT.search(html_src)
    if m:
        try:
            d = json.loads(m.group(1).strip())
            props = (d.get("props") or {}).get("pageProps") if isinstance(d, dict) else None
            out.append({"source": "__NEXT_DATA__", "type": "", "data": props if isinstance(props, dict) else d})
        except ValueError:
            pass
    return out[:20]


def parse_html(html_src: str, url: str) -> Dict[str, Any]:
    """The ParsedDoc of an HTML body. Standard library only; the main-content heuristic follows the crawl's
    (main / article / role=main, else the body without nav, header, footer, aside, form)."""
    src = (html_src or "")[:MAX_HTML]
    structured = _structured(src)
    src_nc = _COMMENT.sub(" ", src)
    src_ns = _SCRIPT.sub(" ", src_nc)
    t = _TITLE.search(src_ns)
    title = _clean(t.group(1))[:500] if t else ""
    d = _META_DESC.search(src_nc) or _META_DESC2.search(src_nc)
    description = _html.unescape(d.group(1)).strip()[:500] if d else ""
    headings = [{"level": int(m.group(1)), "text": _clean(m.group(2))[:120]} for m in _HEADING.finditer(src_ns)]
    headings = [h for h in headings if h["text"]][:120]
    body_m = _BODY.search(src_ns)
    body = body_m.group(1) if body_m else src_ns
    text = _clean(_main_html(body))[:MAX_TEXT]
    if len(text) < 80:                                    # a page that is all chrome: fall back to the whole body
        text = _clean(body)[:MAX_TEXT]
    code_blocks = sum(1 for m in _PRE.finditer(src_ns) if len(_clean(m.group(2))) > 20)
    tables = [{"rows": len(_TR.findall(src_ns))}] if _TABLE.search(src_ns) else []
    lists = len([1 for _ in _LI.finditer(src_ns)])
    lang_m = _LANG.search(src)
    links = ic.extract_links(src_nc, url)
    return _doc("html", url, title, description, text, headings=headings, links=links, structured=structured,
                code_blocks=code_blocks, tables=tables, list_items=lists, lang=(lang_m.group(1).lower() if lang_m else ""))


def parse_text(text: str, url: str, reader: str = "text", title: str = "") -> Dict[str, Any]:
    """The ParsedDoc of a body that is already text (a reader-proxy answer, a .txt, CSV, JSON...)."""
    body = _WS.sub(" ", str(text or "")).strip()[:MAX_TEXT]
    return _doc(reader, url, title or "", "", body)


def _doc(reader: str, url: str, title: str, description: str, text: str, *, headings=(), links=(), structured=(),
         code_blocks: int = 0, tables=(), list_items: int = 0, lang: str = "") -> Dict[str, Any]:
    return {
        "schema": "ParsedDoc/1", "parser_version": PARSER_VERSION,
        "content_hash": ic.content_hash(text), "url_key": ic.url_key(url), "reader": reader, "kind": ic.kind_of(reader),
        "title": title, "description": description, "text": text, "word_count": len(text.split()),
        "headings": list(headings), "links": [dict(l) for l in links], "structured": list(structured),
        "code_blocks": int(code_blocks), "tables": list(tables), "list_items": int(list_items), "lang": lang,
        "nav_only": _looks_nav_only(text),
    }


def _looks_nav_only(text: str) -> bool:
    """Menu items and link labels rather than prose: very few words, or mostly very short ones."""
    words = text.split()
    if len(words) < 12:
        return True
    short = sum(1 for w in words if len(w) <= 3)
    return short / max(1, len(words)) > 0.6


def doc_key(content_hash: str) -> str:
    return "vera:ingress:doc:" + str(content_hash or "")


def crawl_record(doc: Dict[str, Any], rid: str, url: str, extra_tags: Sequence[str], **extra: Any) -> Dict[str, Any]:
    """The fabric record the crawl and the page ingest write, built from one doc (the shape both wrote by hand)."""
    rec = {
        "id": rid, "text": doc.get("text") or "", "title": doc.get("title") or "", "url": url,
        "headings": [h.get("text") if isinstance(h, dict) else h for h in (doc.get("headings") or [])][:20],
        "links": [{"url": l.get("url", ""), "anchor": (l.get("text") or "")[:80]} for l in (doc.get("links") or [])[:100]],
        "link_count": len(doc.get("links") or []), "word_count": int(doc.get("word_count") or 0),
        "content_hash": doc.get("content_hash") or "", "source": "fabric_discovery", "tags": list(extra_tags or []),
    }
    rec.update(extra)
    return rec


# ── dedupe (PLAN §3.4 step 1 and §12.6: ids stay, identical content is not ingested twice) ──────────────────────

def dedupe_decision(content_hash: str, known: Optional[Dict[str, Any]], ds_id: str, rid: str) -> Dict[str, Any]:
    """What a page ingest does with content the map has seen: `known` is the web_content row (record_id, first_key,
    seen) or None.
    - unseen, or seen but never ingested: 'ingest' (and note the record afterwards);
    - seen with THIS record id (the same URL in the same dataset again): 'skip' - the 14,750 identical repeats of
      §14.2 cost embedding, NER, Chroma and Neo4j for nothing;
    - seen under another record (another URL or dataset): 'link' - ingest a record that points at the canonical
      one; the caller may still skip the expensive passes."""
    if not content_hash or not known or not known.get("record_id"):
        return {"action": "ingest", "canonical": ""}
    if known["record_id"] == rid:
        return {"action": "skip", "canonical": rid}
    return {"action": "link", "canonical": str(known["record_id"])}


# ── deferred enrichment (T3) ──────────────────────────────────────────────────

T3_KIND = "discovery.t3"


def t3_job(ds_id: str, items: Sequence[Dict[str, Any]], *, crawl_id: str = "", llm_ent: bool = False,
           reconcile: bool = False, llm_tag_topic: str = "") -> Dict[str, Any]:
    """The idle-queue job for a crawl's deferred second-order work: one per crawl, the records it fetched."""
    recs = [{"rid": str(i.get("rid") or ""), "ck": str(i.get("ck") or "web"), "url": str(i.get("url") or "")}
            for i in items if i.get("rid")]
    return {"kind": T3_KIND, "title": f"discovery: entities for {len(recs)} pages of {ds_id}"[:120],
            "dedupe_key": f"{T3_KIND}:{crawl_id or ds_id}",
            "payload": {"dataset_id": ds_id, "crawl_id": crawl_id, "records": recs[:2000], "llm_ent": bool(llm_ent),
                        "reconcile": bool(reconcile), "topic": llm_tag_topic or ""}, "total": len(recs)}
