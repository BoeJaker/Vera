"""export_core.py - a reply as a FILE: documents, presentations, spreadsheets (pure parts; no I/O beyond the paths given)

Owner, 2026-10-04: "add the various file output types to the menu under the responses in the chat ui - and if it doesnt
have them add options for documents, presentations and spreadsheets and rendering options for these documents ... id
like as many types as you can implement."

What a reply holds is read three ways, each into a family of files:

  document      the reply (markdown) as a page            -> html, md, txt (here); docx, odt, rtf, pdf, epub (LibreOffice)
  presentation  its slides - a ```slides fence, or the     -> html deck (here); odp (flat XML here, packed by LibreOffice),
                reply cut at its --- rules / headings          pptx, pdf (LibreOffice from that)
  spreadsheet   its tables - markdown tables, CSV/TSV, a   -> xlsx (xlsxwriter: formulas kept, header, filter, widths),
                ```sheet fence                                 csv, tsv, json (here); ods (LibreOffice from the xlsx)

Everything here is pure: the converters that shell out (LibreOffice) are the capability module's, run off the event
loop. Tested in tests/test_export_core.py.
"""
from __future__ import annotations

import csv
import html as _html
import io
import json
import re
from typing import Any, Dict, List, Optional, Sequence

# ── the formats ──────────────────────────────────────────────────────────────────────────────────────────────────────
#: id -> family, extension, media type, the label a menu shows, and how it is made ('py' here, 'lo' LibreOffice)
FORMATS: Dict[str, Dict[str, str]] = {
    # documents
    "docx": {"family": "document", "ext": "docx", "label": "Word document", "via": "lo",
             "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "pdf": {"family": "document", "ext": "pdf", "label": "PDF", "via": "lo", "mime": "application/pdf"},
    "odt": {"family": "document", "ext": "odt", "label": "OpenDocument text", "via": "lo",
            "mime": "application/vnd.oasis.opendocument.text"},
    "rtf": {"family": "document", "ext": "rtf", "label": "Rich text", "via": "lo", "mime": "application/rtf"},
    "epub": {"family": "document", "ext": "epub", "label": "E-book", "via": "lo", "mime": "application/epub+zip"},
    "html": {"family": "document", "ext": "html", "label": "Web page", "via": "py", "mime": "text/html"},
    "md": {"family": "document", "ext": "md", "label": "Markdown", "via": "py", "mime": "text/markdown"},
    "txt": {"family": "document", "ext": "txt", "label": "Plain text", "via": "py", "mime": "text/plain"},
    # presentations
    "pptx": {"family": "presentation", "ext": "pptx", "label": "PowerPoint", "via": "lo",
             "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "odp": {"family": "presentation", "ext": "odp", "label": "OpenDocument slides", "via": "lo",
            "mime": "application/vnd.oasis.opendocument.presentation"},
    "slides_pdf": {"family": "presentation", "ext": "pdf", "label": "Slides as PDF", "via": "lo", "mime": "application/pdf"},
    "deck": {"family": "presentation", "ext": "html", "label": "Web slideshow", "via": "py", "mime": "text/html"},
    # spreadsheets
    "xlsx": {"family": "spreadsheet", "ext": "xlsx", "label": "Excel workbook", "via": "py",
             "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "ods": {"family": "spreadsheet", "ext": "ods", "label": "OpenDocument spreadsheet", "via": "lo",
            "mime": "application/vnd.oasis.opendocument.spreadsheet"},
    "csv": {"family": "spreadsheet", "ext": "csv", "label": "CSV", "via": "py", "mime": "text/csv"},
    "tsv": {"family": "spreadsheet", "ext": "tsv", "label": "Tab-separated", "via": "py", "mime": "text/tab-separated-values"},
    "json": {"family": "spreadsheet", "ext": "json", "label": "JSON rows", "via": "py", "mime": "application/json"},
}
FAMILIES = ("document", "presentation", "spreadsheet")
#: LibreOffice's export filter per target
LO_FILTER = {"docx": "docx:MS Word 2007 XML", "odt": "odt", "rtf": "rtf", "pdf": "pdf:writer_pdf_Export", "epub": "epub",
             "pptx": "pptx:Impress MS PowerPoint 2007 XML", "odp": "odp", "slides_pdf": "pdf:impress_pdf_Export",
             "ods": "ods"}


def family_of(fmt: str) -> str:
    return (FORMATS.get(fmt) or {}).get("family", "")


# ── markdown, lightly ────────────────────────────────────────────────────────────────────────────────────────────────
_FENCE = re.compile(r"^(\s*)(```|~~~)")
# a whole fenced block: its opening run (three or more backticks or tildes - four let it hold ``` fences), its language
# and info, its body, closed by the same run on a line of its own (or the end, while it is still being written)
_FENCES = re.compile(r"^(`{3,}|~{3,})[ \t]*([\w+-]*)([^\n]*)\n([\s\S]*?)(?:\n\1[ \t]*(?=\n|$)|\Z)", re.M)


def _fence_of(src: str, kinds: str):
    """(language, body) of the first fence whose language is one of `kinds` (a|b|c), or None."""
    want = set(kinds.split("|"))
    for m in _FENCES.finditer(src):
        if m.group(2).lower() in want:
            return m.group(2).lower(), m.group(4)
    return None


def md_to_html(md: str) -> str:
    """Markdown -> HTML: the markdown library with tables and fences where it is installed, a small reader otherwise."""
    src = str(md or "")
    try:
        import markdown as _md  # type: ignore
        return _md.markdown(src, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:
        return _naive_html(src)


def _inline(s: str) -> str:
    s = _html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*([^*]+)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def _naive_html(md: str) -> str:
    out: List[str] = []
    lines = md.replace("\r", "").split("\n")
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if _FENCE.match(ln):
            j = i + 1
            buf = []
            while j < n and not _FENCE.match(lines[j]):
                buf.append(lines[j]); j += 1
            out.append("<pre><code>" + _html.escape("\n".join(buf)) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            k = len(m.group(1)); out.append("<h%d>%s</h%d>" % (k, _inline(m.group(2)), k)); i += 1; continue
        t = table_at(lines, i)
        if t:
            rows, i = t
            out.append(_table_html(rows)); continue
        if re.match(r"^\s*[-*+]\s+", ln) or re.match(r"^\s*\d+[.)]\s+", ln):
            tag = "ol" if re.match(r"^\s*\d+[.)]\s+", ln) else "ul"
            items = []
            while i < n and (re.match(r"^\s*[-*+]\s+", lines[i]) or re.match(r"^\s*\d+[.)]\s+", lines[i])):
                items.append("<li>" + _inline(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[i])) + "</li>"); i += 1
            out.append("<%s>%s</%s>" % (tag, "".join(items), tag)); continue
        if not ln.strip():
            i += 1; continue
        para = [ln.strip()]; i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|\s*[-*+]\s|\s*\d+[.)]\s|\s*\|)", lines[i]) and not _FENCE.match(lines[i]):
            para.append(lines[i].strip()); i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


def _table_html(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    return ("<table><thead><tr>" + "".join("<th>%s</th>" % _inline(c) for c in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join("<td>%s</td>" % _inline(c) for c in r) + "</tr>" for r in body) + "</tbody></table>")


_DOC_CSS = """body{font:11pt/1.55 Georgia,'Times New Roman',serif;color:#1d1f23;max-width:46em;margin:2.2em auto;padding:0 1.2em}
h1,h2,h3,h4{font-family:'Helvetica Neue',Arial,sans-serif;line-height:1.25;margin:1.4em 0 .5em}h1{font-size:1.9em}h2{font-size:1.45em}
h3{font-size:1.2em}table{border-collapse:collapse;margin:1em 0;font-size:.95em}th,td{border:1px solid #c9ccd2;padding:.35em .6em;text-align:left}
th{background:#eef0f3}pre{background:#f4f5f7;padding:.8em 1em;border-radius:4px;overflow:auto;font:9.5pt/1.45 Consolas,monospace}
code{font-family:Consolas,monospace;font-size:.92em}blockquote{border-left:3px solid #c9ccd2;margin:1em 0;padding:.1em 1em;color:#555}
img{max-width:100%}"""


def document_html(md: str, title: str = "") -> str:
    """The reply as a standalone, printable page (the source LibreOffice reads for docx/odt/rtf/pdf/epub too)."""
    body = md_to_html(strip_doc_fences(md))
    t = _html.escape(title or first_heading(md) or "Document")
    return ('<!doctype html><html><head><meta charset="utf-8"><title>%s</title><style>%s</style></head><body>%s</body></html>'
            % (t, _DOC_CSS, body))


def strip_doc_fences(md: str) -> str:
    """A ```document fence holds a document: its body is the document (the fence itself is chrome)."""
    m = _fence_of(str(md or ""), "document|doc|docx")
    return m[1] if m else str(md or "")


def first_heading(md: str) -> str:
    m = re.search(r"^#{1,3}\s+(.+)$", str(md or ""), re.M)
    return m.group(1).strip() if m else ""


def plain_text(md: str) -> str:
    s = re.sub(r"```[^\n]*\n", "", str(md or "")).replace("```", "")
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\((\S+)\)", r"\1 (\2)", s)
    return s


# ── slides ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def slides_of(md: str) -> List[Dict[str, Any]]:
    """A reply's slides: a ```slides fence's body, or the reply. Cut at '---' rules (outside code); with none, at its
    #/## headings; with neither, one slide. Each: {title, body (markdown), notes}. 'Note:' / '???' starts the notes."""
    src = str(md or "")
    m = _fence_of(src, "slides|deck|presentation|slide")
    if m:
        src = m[1]
    lines = src.replace("\r", "").split("\n")
    parts: List[List[str]] = [[]]
    in_code = False
    has_rule = any(re.match(r"^\s*-{3,}\s*$", l) for l in lines)
    for ln in lines:
        if _FENCE.match(ln):
            in_code = not in_code
        if not in_code and has_rule and re.match(r"^\s*-{3,}\s*$", ln):
            parts.append([]); continue
        if not in_code and not has_rule and re.match(r"^#{1,2}\s+", ln) and any(x.strip() for x in parts[-1]):
            parts.append([])
        parts[-1].append(ln)
    out = []
    for p in parts:
        if not any(x.strip() for x in p):
            continue
        title, body, notes, in_notes = "", [], [], False
        for ln in p:
            if not title and re.match(r"^#{1,6}\s+", ln):
                title = re.sub(r"^#{1,6}\s+", "", ln).strip(); continue
            if re.match(r"^\s*(Note:|Notes:|\?\?\?)", ln):
                in_notes = True; ln = re.sub(r"^\s*(Notes?:|\?\?\?)\s*", "", ln)
            (notes if in_notes else body).append(ln)
        out.append({"title": title, "body": "\n".join(body).strip("\n"), "notes": "\n".join(notes).strip()})
    return out or [{"title": "", "body": "", "notes": ""}]


_DECK_CSS = """html,body{margin:0;height:100%;background:#111318;font-family:'Helvetica Neue',Arial,sans-serif}
.s{display:none;box-sizing:border-box;width:100vw;height:100vh;padding:6vh 8vw;color:#e9ebef;background:linear-gradient(160deg,#1c2029,#12141a)}
.s.on{display:flex;flex-direction:column}.s h1{font-size:5.2vh;margin:0 0 3vh;color:#fff;font-weight:600}
.s .b{font-size:3.1vh;line-height:1.5}.s .b li{margin:.6vh 0}.s pre{background:#0b0c10;padding:2vh;border-radius:6px;font-size:2.2vh;overflow:auto}
.s table{border-collapse:collapse}.s td,.s th{border:1px solid #3a3f4a;padding:.6vh 1.2vw}.n{position:fixed;right:2vw;bottom:2vh;color:#8b93a1;font-size:1.8vh}
.s.first{justify-content:center}.s.first h1{font-size:7vh}"""


def deck_html(slides: Sequence[Dict[str, Any]], title: str = "") -> str:
    """A self-contained slideshow: arrows, space, PageUp/Down and a click move; Home/End jump; F fills the screen."""
    secs = []
    for i, s in enumerate(slides):
        secs.append('<section class="s%s%s"><h1>%s</h1><div class="b">%s</div></section>'
                    % (" on" if i == 0 else "", " first" if i == 0 and not s.get("body") else "",
                       _html.escape(s.get("title") or ""), md_to_html(s.get("body") or "")))
    js = ("var s=document.querySelectorAll('.s'),i=0,n=document.querySelector('.n');function go(k){s[i].classList.remove('on');"
          "i=Math.max(0,Math.min(s.length-1,k));s[i].classList.add('on');n.textContent=(i+1)+' / '+s.length;}"
          "document.addEventListener('keydown',function(e){if(['ArrowRight','ArrowDown','PageDown',' '].indexOf(e.key)>=0)go(i+1);"
          "else if(['ArrowLeft','ArrowUp','PageUp'].indexOf(e.key)>=0)go(i-1);else if(e.key==='Home')go(0);else if(e.key==='End')go(s.length-1);"
          "else if(e.key==='f'&&document.documentElement.requestFullscreen)document.documentElement.requestFullscreen();});"
          "document.addEventListener('click',function(e){go(e.clientX<innerWidth/3?i-1:i+1);});go(0);")
    return ('<!doctype html><html><head><meta charset="utf-8"><title>%s</title><style>%s</style></head><body>%s'
            '<div class="n"></div><script>%s</script></body></html>'
            % (_html.escape(title or (slides[0].get("title") if slides else "") or "Slides"), _DECK_CSS, "".join(secs), js))


def _x(s: str) -> str:
    return _html.escape(str(s or ""), quote=True)


def _plain_inline(s: str) -> str:
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"(?<![*\w])\*([^*]+)\*(?!\*)", r"\1", s)
    return re.sub(r"\[([^\]]+)\]\((\S+)\)", r"\1", s)


def _split_tables(md: str):
    """A slide body as (its text, its tables): a slide draws a table as a table, in a frame of its own under the text."""
    lines = str(md or "").split("\n")
    text: List[str] = []
    tables: List[List[List[str]]] = []
    i = 0
    while i < len(lines):
        t = table_at(lines, i)
        if t:
            rows, i = t
            tables.append(rows); continue
        text.append(lines[i]); i += 1
    return "\n".join(text), tables


def _odp_table(rows: List[List[str]]) -> str:
    """A table on a slide: the header row shaded, every cell bordered, formulas shown as what they come to."""
    vals = evaluate(rows)
    ncol = max(len(r) for r in rows)
    out = ['<table:table><table:table-column table:number-columns-repeated="%d"/>' % ncol]
    for r, row in enumerate(rows):
        out.append("<table:table-row>")
        for c in range(ncol):
            v = row[c] if c < len(row) else ""
            if str(v).strip().startswith("="):           # a formula shows what it comes to; any other cell as written
                v = vals[r][c]
            out.append('<table:table-cell table:style-name="%s"><text:p text:style-name="%s">%s</text:p></table:table-cell>'
                       % ("ce1" if r == 0 else "ce2", "Phead" if r == 0 else "Pcell", _x(_plain_inline(fmt_value(v)))))
        out.append("</table:table-row>")
    out.append("</table:table>")
    return "".join(out)


def _odp_body(md: str) -> str:
    """A slide's body text as ODF: bullets as a list (nested by indent), numbered items numbered, the rest paragraphs;
    code in the mono style."""
    out: List[str] = []
    in_code = False
    for ln in str(md or "").split("\n"):
        if _FENCE.match(ln):
            in_code = not in_code; continue
        if in_code:
            out.append('<text:p text:style-name="Pcode">%s</text:p>' % _x(ln or " ")); continue
        if not ln.strip():
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}", ln):
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", ln)
        if m:
            depth = min(3, len(m.group(1).replace("\t", "  ")) // 2)
            num = m.group(2)[0].isdigit()
            item = '<text:list-item><text:p text:style-name="Pbody">%s</text:p></text:list-item>' % _x(_plain_inline(m.group(3)))
            for _ in range(depth):
                item = '<text:list-item><text:list>%s</text:list></text:list-item>' % item
            out.append('<text:list text:style-name="%s">%s</text:list>' % ("L2" if num else "L1", item))
            continue
        out.append('<text:p text:style-name="Pbody">%s</text:p>' % _x(_plain_inline(re.sub(r"^#{1,6}\s+", "", ln.strip()))))
    return "".join(out) or '<text:p text:style-name="Pbody"/>'


def odp_flat(slides: Sequence[Dict[str, Any]], title: str = "") -> str:
    """The slides as a flat OpenDocument presentation (.fodp): 16:9, a title and a body frame per slide, the notes in
    each slide's notes page. LibreOffice packs it into .odp, converts it to .pptx and prints it to PDF."""
    pages = []
    for i, s in enumerate(slides):
        first = i == 0 and not (s.get("body") or "").strip()
        tframe = ('<draw:frame presentation:style-name="pr1" draw:layer="layout" svg:width="25.4cm" svg:height="%s" svg:x="1.3cm" svg:y="%s" '
                  'presentation:class="title"><draw:text-box><text:p text:style-name="%s">%s</text:p></draw:text-box></draw:frame>'
                  % ("4cm" if first else "2.4cm", "5.6cm" if first else "0.9cm", "Ptitle1" if first else "Ptitle", _x(s.get("title") or "")))
        text, tables = _split_tables(s.get("body") or "")
        bframe, y = "", 3.6
        if not first and text.strip():
            nlines = len([l for l in text.split("\n") if l.strip()])
            h = 11.2 if not tables else min(11.2, 1.05 * nlines + 0.4)
            bframe = ('<draw:frame presentation:style-name="pr2" draw:layer="layout" svg:width="25.4cm" svg:height="%.2fcm" svg:x="1.3cm" '
                      'svg:y="%.2fcm" presentation:class="outline"><draw:text-box>%s</draw:text-box></draw:frame>' % (h, y, _odp_body(text)))
            y += h + 0.3
        for rows in tables:
            h = 0.85 * len(rows)
            bframe += ('<draw:frame draw:style-name="gr1" draw:layer="layout" svg:width="25.4cm" svg:height="%.2fcm" svg:x="1.3cm" svg:y="%.2fcm">%s</draw:frame>'
                       % (h, y, _odp_table(rows)))
            y += h + 0.4
        notes = ""
        if s.get("notes"):
            notes = ('<presentation:notes><draw:frame draw:layer="layout" svg:width="17cm" svg:height="12cm" svg:x="2cm" svg:y="13cm" '
                     'presentation:class="notes"><draw:text-box><text:p>%s</text:p></draw:text-box></draw:frame></presentation:notes>'
                     % _x(s["notes"]))
        pages.append('<draw:page draw:name="Slide%d" draw:style-name="dp1" draw:master-page-name="Default">%s%s%s</draw:page>'
                     % (i + 1, tframe, bframe, notes))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
            'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
            'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
            'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
            'office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.presentation">'
            '<office:meta><dc:title>%s</dc:title></office:meta>'
            '<office:styles><style:style style:name="standard" style:family="graphic"/></office:styles>'
            '<office:automatic-styles>'
            '<style:page-layout style:name="PM1"><style:page-layout-properties fo:page-width="28cm" fo:page-height="15.75cm" '
            'fo:margin-top="0cm" fo:margin-bottom="0cm" fo:margin-left="0cm" fo:margin-right="0cm" style:print-orientation="landscape"/></style:page-layout>'
            '<style:style style:name="dp1" style:family="drawing-page"><style:drawing-page-properties draw:fill="solid" draw:fill-color="#ffffff"/></style:style>'
            '<style:style style:name="pr1" style:family="presentation"><style:graphic-properties draw:fill="none" draw:stroke="none" draw:textarea-vertical-align="middle"/></style:style>'
            '<style:style style:name="pr2" style:family="presentation"><style:graphic-properties draw:fill="none" draw:stroke="none" draw:textarea-vertical-align="top"/></style:style>'
            '<style:style style:name="Ptitle" style:family="paragraph"><style:text-properties fo:font-size="30pt" fo:font-weight="bold" fo:color="#1d2330" style:font-name="Liberation Sans"/></style:style>'
            '<style:style style:name="Ptitle1" style:family="paragraph"><style:paragraph-properties fo:text-align="center"/><style:text-properties fo:font-size="40pt" fo:font-weight="bold" fo:color="#1d2330"/></style:style>'
            '<style:style style:name="Pbody" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.25cm"/><style:text-properties fo:font-size="20pt" fo:color="#2b303b"/></style:style>'
            '<style:style style:name="Pcode" style:family="paragraph"><style:text-properties fo:font-size="13pt" style:font-name="Liberation Mono" fo:font-family="Liberation Mono" fo:color="#2b303b"/></style:style>'
            '<style:style style:name="gr1" style:family="graphic"><style:graphic-properties draw:fill="none" draw:stroke="none"/></style:style>'
            '<style:style style:name="ce1" style:family="table-cell"><style:graphic-properties draw:fill="solid" draw:fill-color="#eef0f3"/>'
            '<style:paragraph-properties fo:border="0.03cm solid #9aa1ad" fo:padding="0.15cm"/></style:style>'
            '<style:style style:name="ce2" style:family="table-cell"><style:graphic-properties draw:fill="none"/>'
            '<style:paragraph-properties fo:border="0.03cm solid #9aa1ad" fo:padding="0.15cm"/></style:style>'
            '<style:style style:name="Phead" style:family="paragraph"><style:text-properties fo:font-size="15pt" fo:font-weight="bold" fo:color="#1d2330" style:font-name="Liberation Sans" fo:font-family="Liberation Sans"/></style:style>'
            '<style:style style:name="Pcell" style:family="paragraph"><style:text-properties fo:font-size="15pt" fo:color="#2b303b" style:font-name="Liberation Sans" fo:font-family="Liberation Sans"/></style:style>'
            '<text:list-style style:name="L2"><text:list-level-style-number text:level="1" style:num-format="1" style:num-suffix="."><style:list-level-properties text:space-before="0.3cm" text:min-label-width="0.9cm"/></text:list-level-style-number>'
            '<text:list-level-style-number text:level="2" style:num-format="a" style:num-suffix="."><style:list-level-properties text:space-before="1.2cm" text:min-label-width="0.9cm"/></text:list-level-style-number></text:list-style>'
            '<text:list-style style:name="L1"><text:list-level-style-bullet text:level="1" text:bullet-char="•"><style:list-level-properties text:space-before="0.3cm" text:min-label-width="0.7cm"/></text:list-level-style-bullet>'
            '<text:list-level-style-bullet text:level="2" text:bullet-char="–"><style:list-level-properties text:space-before="1.2cm" text:min-label-width="0.7cm"/></text:list-level-style-bullet>'
            '<text:list-level-style-bullet text:level="3" text:bullet-char="•"><style:list-level-properties text:space-before="2.1cm" text:min-label-width="0.7cm"/></text:list-level-style-bullet>'
            '<text:list-level-style-bullet text:level="4" text:bullet-char="–"><style:list-level-properties text:space-before="3cm" text:min-label-width="0.7cm"/></text:list-level-style-bullet></text:list-style>'
            '</office:automatic-styles>'
            '<office:master-styles><style:master-page style:name="Default" style:page-layout-name="PM1" draw:style-name="dp1"/></office:master-styles>'
            '<office:body><office:presentation>%s</office:presentation></office:body></office:document>'
            % (_x(title or (slides[0].get("title") if slides else "") or "Slides"), "".join(pages)))


# ── tables ───────────────────────────────────────────────────────────────────────────────────────────────────────────
_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _cells(ln: str) -> List[str]:
    return [c.strip() for c in ln.strip().strip("|").split("|")]


def table_at(lines: Sequence[str], i: int):
    """A markdown pipe table starting at line i: (rows, next index), or None."""
    if i + 1 >= len(lines) or "|" not in lines[i] or not _SEP.match(lines[i + 1]):
        return None
    rows = [_cells(lines[i])]
    j = i + 2
    while j < len(lines) and "|" in lines[j] and lines[j].strip():
        rows.append(_cells(lines[j])); j += 1
    w = len(rows[0])
    rows = [(r + [""] * w)[:w] for r in rows]
    return rows, j


def _clean(c: str) -> str:
    """A cell without its emphasis marks: **Total** is Total, **=SUM(B2:B5)** a formula."""
    s = str(c if c is not None else "").strip()
    m = re.match(r"^(\*\*|__)(.*)\1$", s) or re.match(r"^(`)(.*)`$", s)
    return m.group(2).strip() if m else s


def _delimited(body: str, delim: Optional[str] = None) -> List[List[str]]:
    """A sheet fence's rows. A model writes markdown in one (a real turn, 2026-10-04: a '# title', a pipe table with
    **bold** formulas, a '> tip'): a pipe table in it is the table; otherwise the separator is read from the data lines,
    never from a heading or a note."""
    lines = str(body or "").replace("\r", "").split("\n")
    for i in range(len(lines)):
        t = table_at(lines, i)
        if t:
            return [[_clean(c) for c in r] for r in t[0]]
    text = "\n".join(l for l in lines if l.strip() and not re.match(r"^\s*(#{1,6}\s|>)", l))
    if not text.strip():
        return []
    if delim is None:
        first = text.split("\n", 1)[0]
        delim = "\t" if first.count("\t") > first.count(",") else ("," if "," in first else ("|" if "|" in first else ","))
    if delim == "|":
        return [[_clean(c) for c in _cells(l)] for l in text.split("\n") if l.strip() and not _SEP.match(l)]
    return [[_clean(c) for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def tables_of(md: str) -> List[Dict[str, Any]]:
    """Every table a reply holds, in order: ```sheet / ```csv / ```tsv fences (a 'name=' on the fence names the sheet),
    then the markdown pipe tables in its prose. Each {name, rows} with the header as the first row."""
    src = str(md or "")
    out: List[Dict[str, Any]] = []
    for m in _FENCES.finditer(src):
        kind, info, body = m.group(2).lower(), m.group(3), m.group(4)
        if kind not in ("sheet", "spreadsheet", "csv", "tsv", "xlsx", "table"):
            continue
        nm = re.search(r"(?:name|title)=\"?([^\"\n]+)\"?", info or "")
        hd = re.search(r"^#{1,6}\s+(.+)$", body, re.M)            # a heading inside the fence names the sheet
        rows = _delimited(body, "\t" if kind == "tsv" else ("," if kind == "csv" else None))
        if rows:
            out.append({"name": (nm.group(1).strip() if nm else (hd.group(1).strip() if hd else "")), "rows": rows})
    prose = _FENCES.sub("", src)
    lines = prose.replace("\r", "").split("\n")
    i = 0
    last_heading = ""
    while i < len(lines):
        h = re.match(r"^#{1,6}\s+(.+)$", lines[i])
        if h:
            last_heading = h.group(1).strip()
        t = table_at(lines, i)
        if t:
            rows, i = t
            out.append({"name": last_heading, "rows": rows}); continue
        i += 1
    for k, t in enumerate(out):
        t["name"] = _sheet_name(t["name"] or "Table %d" % (k + 1), [x["name"] for x in out[:k]])
    return out


def _sheet_name(name: str, taken: Sequence[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", str(name)).strip()[:28] or "Sheet"
    nm, n = base, 2
    while nm in taken:
        nm = "%s %d" % (base[:25], n); n += 1
    return nm


_NUM = re.compile(r"^\s*[-+(]?\s*[$£€¥]?\s*\d[\d,]*(\.\d+)?\s*%?\s*\)?\s*$")


def cell_value(s: str) -> Any:
    """A cell as the spreadsheet should hold it: a formula kept ("=SUM(A2:A4)"), a number read ("1,234", "$5", "12%",
    "(3)"), anything else text."""
    t = _clean(s)
    if t.startswith("="):
        return {"formula": t}
    if _NUM.match(t):
        neg = t.startswith("(") and t.endswith(")") or t.lstrip().startswith("-")
        pct = t.endswith("%") or t.rstrip(")").endswith("%")
        d = re.sub(r"[^\d.]", "", t)
        try:
            v = float(d)
        except ValueError:
            return t
        v = -v if neg else v
        v = v / 100.0 if pct else v
        return {"number": int(v) if v == int(v) and "." not in d and not pct else v, "pct": pct}
    return t


def _col(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def evaluate(rows: List[List[str]]) -> List[List[Any]]:
    """The table as its values: numbers read, and the formulas a spreadsheet would compute - SUM, AVERAGE (AVG), MIN,
    MAX, COUNT over ranges and cells, + - * / between them and numbers - worked out (A1 is the header's first cell).
    What it cannot work out stays as written."""
    vals: List[List[Any]] = []
    for r in rows:
        vals.append([(lambda v: v["number"] if isinstance(v, dict) and "number" in v else v)(cell_value(c)) for c in r])

    def cell(ref: str, depth: int) -> Any:
        m = re.match(r"^([A-Za-z]+)(\d+)$", ref)
        if not m:
            return None
        r, c = int(m.group(2)) - 1, _col(m.group(1))
        if r < 0 or r >= len(vals) or c < 0 or c >= len(vals[r]):
            return None
        v = vals[r][c]
        if isinstance(v, dict) and "formula" in v:
            v = calc(v["formula"], depth + 1)
        return v

    def rng(a: str, b: str, depth: int) -> List[float]:
        ma, mb = re.match(r"^([A-Za-z]+)(\d+)$", a), re.match(r"^([A-Za-z]+)(\d+)$", b)
        if not (ma and mb):
            return []
        out = []
        for r in range(min(int(ma.group(2)), int(mb.group(2))), max(int(ma.group(2)), int(mb.group(2))) + 1):
            for c in range(min(_col(ma.group(1)), _col(mb.group(1))), max(_col(ma.group(1)), _col(mb.group(1))) + 1):
                v = cell("%s%d" % (_letters(c), r), depth)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.append(v)
        return out

    def calc(f: str, depth: int = 0) -> Any:
        if depth > 20:
            return f
        expr = f.lstrip("=")
        fns = {"SUM": sum, "AVERAGE": lambda xs: sum(xs) / len(xs) if xs else 0, "AVG": lambda xs: sum(xs) / len(xs) if xs else 0,
               "MIN": lambda xs: min(xs) if xs else 0, "MAX": lambda xs: max(xs) if xs else 0, "COUNT": len}

        def fn(m):
            name, args = m.group(1).upper(), m.group(2)
            xs: List[float] = []
            for a in args.split(","):
                a = a.strip()
                if ":" in a:
                    xs += rng(*a.split(":", 1), depth)
                else:
                    v = cell(a, depth) if re.match(r"^[A-Za-z]+\d+$", a) else (float(a) if re.match(r"^-?\d+(\.\d+)?$", a) else None)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        xs.append(v)
            return repr(fns[name](xs))
        try:
            expr = re.sub(r"\b(SUM|AVERAGE|AVG|MIN|MAX|COUNT)\(([^()]*)\)", fn, expr, flags=re.I)
            expr = re.sub(r"\b([A-Za-z]{1,3}\d+)\b", lambda m: repr(cell(m.group(1), depth) if isinstance(cell(m.group(1), depth), (int, float)) else 0), expr)
            if not re.match(r"^[\d\s.+\-*/()e]+$", expr):
                return f
            v = eval(expr, {"__builtins__": {}}, {})   # digits and + - * / ( ) only, checked above
            return int(v) if isinstance(v, float) and v.is_integer() else v
        except Exception:
            return f

    return [[calc(v["formula"]) if isinstance(v, dict) and "formula" in v else v for v in r] for r in vals]


def _letters(c: int) -> str:
    s = ""
    c += 1
    while c:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return s


def fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v if v is not None else "")


def write_xlsx(tables: Sequence[Dict[str, Any]], path: str) -> None:
    """The tables as a workbook: a sheet each, the header bold and frozen with a filter, numbers as numbers (percent
    formatted), formulas kept as formulas, columns as wide as their text."""
    import xlsxwriter  # type: ignore
    wb = xlsxwriter.Workbook(path)
    head = wb.add_format({"bold": True, "bg_color": "#EEF0F3", "border": 1})
    pctf = wb.add_format({"num_format": "0.0%"})
    for t in tables:
        ws = wb.add_worksheet(t["name"][:31])
        rows = t["rows"]
        widths: Dict[int, int] = {}
        for r, row in enumerate(rows):
            for c, raw in enumerate(row):
                widths[c] = max(widths.get(c, 0), min(60, len(str(raw))))
                if r == 0:
                    ws.write_string(r, c, str(raw), head); continue
                v = cell_value(raw)
                if isinstance(v, dict) and "formula" in v:
                    ws.write_formula(r, c, v["formula"])
                elif isinstance(v, dict) and "number" in v:
                    ws.write_number(r, c, v["number"], pctf if v.get("pct") else None)
                else:
                    ws.write_string(r, c, str(v))
        for c, w in widths.items():
            ws.set_column(c, c, max(6, w + 2))
        if rows:
            ws.freeze_panes(1, 0)
            ws.autofilter(0, 0, max(0, len(rows) - 1), max(0, len(rows[0]) - 1))
    wb.close()


def delimited_text(tables: Sequence[Dict[str, Any]], delim: str) -> str:
    """CSV/TSV: one table as is; several, each under a '# name' line and a blank line between."""
    parts = []
    for t in tables:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delim, lineterminator="\n")
        for r in t["rows"]:
            w.writerow(r)
        parts.append(("# %s\n" % t["name"] if len(tables) > 1 else "") + buf.getvalue())
    return "\n".join(parts)


def tables_json(tables: Sequence[Dict[str, Any]]) -> str:
    """Rows as objects keyed by the header, numbers as numbers; several tables keyed by name."""
    def objs(rows):
        if not rows:
            return []
        hd = [h or "col%d" % (i + 1) for i, h in enumerate(rows[0])]
        out = []
        for r in rows[1:]:
            o = {}
            for k, raw in zip(hd, r):
                v = cell_value(raw)
                o[k] = v["number"] if isinstance(v, dict) and "number" in v else (v["formula"] if isinstance(v, dict) else v)
            out.append(o)
        return out
    data: Any = objs(tables[0]["rows"]) if len(tables) == 1 else {t["name"]: objs(t["rows"]) for t in tables}
    return json.dumps(data, indent=2, ensure_ascii=False)
