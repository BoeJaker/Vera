"""
render_capabilities.py — Vera document export (pandoc-backed)
=============================================================

Turns Markdown/text answers into real files (DOCX, PDF, HTML, ODT, PPTX, …) so
the standardised output-format profiles in `vera.output_formats` have somewhere
to land. Conversion shells out to the `pandoc` binary (installed in the Docker
image); pure-text targets (md / txt) are written directly and need no binary.

Capabilities
────────────
  render.formats        — which target formats are live (gated on pandoc/PDF engine)
  render.export         — markdown/text -> file in a target format; returns a URL
  render.dream_export   — pull a dream cycle's report and export it

Generated files are written under `_out/` next to this module and served
read-only via GET /render/download?name=… (filename is validated to stay inside
the output dir).
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso, register_ui,
)
from Vera.vera import capability_orchestration as _core
from Vera.vera.output_formats import FORMAT_PROFILES
from Vera.vera import delivery as _delivery

log = logging.getLogger("vera.render")

_OUT_DIR = Path(__file__).parent / "_out"
_OUT_DIR.mkdir(parents=True, exist_ok=True)

# Target formats we know how to produce. "text" formats are written directly;
# everything else goes through pandoc. "pdf" additionally needs a PDF engine.
_TEXT_FORMATS = {"md": "text/markdown", "markdown": "text/markdown",
                 "txt": "text/plain", "text": "text/plain"}
_PANDOC_FORMATS = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf":  "application/pdf",
    "html": "text/html",
    "odt":  "application/vnd.oasis.opendocument.text",
    "rtf":  "application/rtf",
    "epub": "application/epub+zip",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "latex": "application/x-latex",
    "tex":  "application/x-latex",
}

# Map an ambiguous request to the canonical pandoc writer / file extension.
_EXT_ALIAS = {"markdown": "md", "text": "txt", "tex": "latex"}


# ─────────────────────────────────────────────────────────────────────────────
# Environment detection
# ─────────────────────────────────────────────────────────────────────────────

def _pandoc_path() -> Optional[str]:
    return shutil.which("pandoc")


def _pdf_engine() -> Optional[str]:
    """Return the first available pandoc PDF engine, or None."""
    for eng in ("wkhtmltopdf", "weasyprint", "xelatex", "pdflatex", "tectonic"):
        if shutil.which(eng):
            return eng
    return None


def _live_formats() -> Dict[str, bool]:
    """Which target formats can actually be produced right now."""
    have_pandoc = bool(_pandoc_path())
    have_pdf = have_pandoc and bool(_pdf_engine())
    live: Dict[str, bool] = {"md": True, "txt": True}
    for fmt in _PANDOC_FORMATS:
        if fmt in ("pdf",):
            live[fmt] = have_pdf
        elif fmt in ("tex",):
            continue  # exposed as 'latex'
        else:
            live[fmt] = have_pandoc
    return live


def _canonical(fmt: str) -> str:
    fmt = (fmt or "md").strip().lower().lstrip(".")
    return _EXT_ALIAS.get(fmt, fmt)


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return stem[:80] or "export"


# ─────────────────────────────────────────────────────────────────────────────
# Core conversion
# ─────────────────────────────────────────────────────────────────────────────

def _write_text(content: str, out_path: Path) -> None:
    out_path.write_text(content, encoding="utf-8")


def _run_pandoc(content: str, out_path: Path, to_fmt: str,
                title: str = "") -> Dict[str, Any]:
    """Convert markdown `content` to `out_path` via pandoc. Returns {ok, error}."""
    pandoc = _pandoc_path()
    if not pandoc:
        return {"ok": False, "error": "pandoc not installed"}

    # Write the source markdown to a temp file (avoids stdin encoding issues).
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as tf:
        if title:
            tf.write(f"% {title}\n\n")
        tf.write(content)
        src = tf.name

    args = [pandoc, src, "-f", "markdown", "-o", str(out_path)]
    if title:
        args += ["--metadata", f"title={title}"]
    if to_fmt == "pdf":
        eng = _pdf_engine()
        if not eng:
            try:
                os.unlink(src)
            except OSError:
                pass
            return {"ok": False, "error": "no PDF engine available (need wkhtmltopdf/weasyprint/xelatex)"}
        args += [f"--pdf-engine={eng}"]
    elif to_fmt in ("html",):
        args += ["--standalone"]

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "pandoc failed")[:500]}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "pandoc timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


def _export(content: str, fmt: str, title: str = "",
            filename: str = "") -> Dict[str, Any]:
    fmt = _canonical(fmt)
    ext = "md" if fmt == "markdown" else ("txt" if fmt == "text" else fmt)
    stem = _safe_stem(filename or title or "export")
    out_name = f"{stem}-{uuid.uuid4().hex[:8]}.{ext}"
    out_path = _OUT_DIR / out_name

    if fmt in _TEXT_FORMATS:
        _write_text(content, out_path)
    elif fmt in _PANDOC_FORMATS:
        res = _run_pandoc(content, out_path, "pdf" if fmt == "pdf" else fmt, title=title)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error"), "format": fmt}
    else:
        return {"ok": False, "error": f"unsupported format: {fmt}",
                "supported": sorted(set(list(_TEXT_FORMATS) + list(_PANDOC_FORMATS)))}

    try:
        size = out_path.stat().st_size
    except OSError:
        size = 0
    return {
        "ok": True, "format": fmt, "filename": out_name,
        "path": str(out_path), "url": f"/render/download?name={out_name}",
        "bytes": size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "render.formats", memory="off", silent=True,
    http_method="GET", http_path="/render/formats", http_tags=["render"],
    description="List document export targets and whether each is live right now "
                "(md/txt always; docx/html/odt/rtf/epub/pptx/latex need pandoc; pdf "
                "also needs a PDF engine). Output: {pandoc, pdf_engine, formats:[{id,live}]}.",
)
async def render_formats(trace_id=None):
    live = _live_formats()
    return {
        "pandoc":     bool(_pandoc_path()),
        "pdf_engine": _pdf_engine() or "",
        "formats":    [{"id": k, "live": v} for k, v in sorted(live.items())],
        "count":      len(live),
    }


@capability(
    "render.export", memory="off",
    http_method="POST", http_path="/render/export", http_tags=["render"],
    description="Export Markdown/text content to a file in a target format and return "
                "a download URL. Inputs: content (str! — markdown source), "
                "format (md|txt|docx|pdf|html|odt|rtf|epub|pptx|latex, default md), "
                "title (str, optional), filename (str, optional base name). "
                "Output: {ok, format, filename, url, bytes}. PDF/office formats need "
                "pandoc (+ a PDF engine) — see render.formats.",
)
async def render_export(content: str = "", format: str = "md",
                        title: str = "", filename: str = "", trace_id=None):
    if not content or not content.strip():
        return {"ok": False, "error": "content required"}
    res = _export(content, format, title=title, filename=filename)
    if res.get("ok"):
        await emit_event({"type": "render.export", "format": res.get("format"),
                          "filename": res.get("filename"), "bytes": res.get("bytes")})
    return res


@capability(
    "render.dream_export", memory="off",
    http_method="POST", http_path="/render/dream_export", http_tags=["render", "dream"],
    description="Export a dream cycle's report to a file. Inputs: cycle_id (str!), "
                "format (md|docx|pdf|html|…, default docx), filename (str, optional). "
                "Pulls the report via dream.cycle.detail. Output: {ok, format, url, bytes}.",
)
async def render_dream_export(cycle_id: str = "", format: str = "docx",
                              filename: str = "", trace_id=None):
    if not cycle_id:
        return {"ok": False, "error": "cycle_id required"}
    detail_cap = CAPABILITY_REGISTRY.get("dream.cycle.detail")
    if not detail_cap:
        return {"ok": False, "error": "dream.cycle.detail not available"}
    try:
        detail = await detail_cap["func"](cycle_id=cycle_id)
    except Exception as e:
        return {"ok": False, "error": f"dream.cycle.detail failed: {e}"}

    rec = detail.get("cycle") or detail.get("detail") or detail if isinstance(detail, dict) else {}
    report = ""
    title = ""
    if isinstance(rec, dict):
        report = rec.get("report") or rec.get("synthesis") or rec.get("text") or ""
        title = rec.get("title") or rec.get("trigger") or cycle_id
    if not report:
        return {"ok": False, "error": "no report found for this cycle"}

    res = _export(report, format, title=title,
                  filename=filename or _safe_stem(title) or cycle_id)
    if res.get("ok"):
        await emit_event({"type": "render.export", "source": "dream",
                          "cycle_id": cycle_id, "format": res.get("format"),
                          "filename": res.get("filename")})
    return res


# ─────────────────────────────────────────────────────────────────────────────
# RICH HTML REPORT — report.html
# Turn ANY run's output into a durable, visually rich, self-contained HTML
# document (themed layout, Mermaid diagrams via Vera's own <vera-mermaid>
# element, hand-authored inline-SVG infographics, and embedded images from the
# imagine system via media.illustrate). Unlike render.html (an ephemeral in-chat
# snippet) this is STORED and served INLINE at /render/report/{name} so it can be
# opened in a tab/iframe, re-delivered, and pinned to the artifacts gallery.
# ─────────────────────────────────────────────────────────────────────────────

def _cap(name: str):
    entry = CAPABILITY_REGISTRY.get(name)
    return entry.get("func") if isinstance(entry, dict) else None


def _gen_text(res: Any) -> str:
    """Best-effort text extraction from an llm.generate-shaped result."""
    if isinstance(res, dict):
        for k in ("text", "response", "output", "content", "result"):
            v = res.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return ""
    return str(res or "")


def _strip_html_fence(s: str) -> str:
    """Drop a single ```html … ``` (or bare ```) fence wrapping the whole reply."""
    t = (s or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*)\n```$", t, re.DOTALL)
    return (m.group(1).strip() if m else t)


# Self-contained document theme — light/dark aware, no external assets. Kept as a
# plain constant (NOT an f-string) so the literal CSS braces are safe.
_REPORT_CSS = """
:root{--bg:#fbfbfa;--card:#fff;--ink:#1c1d22;--dim:#5c6270;--acc:#5a9e8f;
--acc2:#7d6fd1;--bd:#e6e6e9;--code:#f2f3f5;}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1b1e24;--ink:#e8e9ec;
--dim:#9aa1ad;--acc:#6fb6a5;--acc2:#9a8cf0;--bd:#2a2e37;--code:#22262e;}}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);
font:16px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}
.rpt{max-width:860px;margin:0 auto;padding:44px 24px 96px;}
.rpt-head{border-bottom:2px solid var(--acc);padding-bottom:14px;margin-bottom:28px;}
.rpt-head h1{font-size:2rem;line-height:1.2;margin:0 0 6px;}
.rpt-head .meta{color:var(--dim);font-size:.85rem;}
.rpt h2{font-size:1.4rem;margin:2.2em 0 .5em;padding-top:.2em;border-top:1px solid var(--bd);}
.rpt h3{font-size:1.12rem;margin:1.6em 0 .4em;color:var(--acc);}
.rpt p{margin:.7em 0;}
.rpt a{color:var(--acc2);}
.rpt ul,.rpt ol{margin:.6em 0 .6em 1.2em;padding:0;}
.rpt li{margin:.25em 0;}
.rpt table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.94rem;
display:block;overflow-x:auto;}
.rpt th,.rpt td{border:1px solid var(--bd);padding:7px 11px;text-align:left;}
.rpt th{background:var(--code);}
.rpt blockquote{margin:1em 0;padding:.4em 1em;border-left:3px solid var(--acc);
color:var(--dim);background:var(--code);}
.rpt pre{background:var(--code);padding:12px 14px;border-radius:8px;overflow-x:auto;}
.rpt code{background:var(--code);padding:.1em .35em;border-radius:4px;font-size:.9em;}
.rpt pre code{background:none;padding:0;}
.rpt figure{margin:1.4em 0;text-align:center;}
.rpt figure img{max-width:100%;border-radius:10px;border:1px solid var(--bd);}
.rpt figcaption{color:var(--dim);font-size:.85rem;margin-top:.5em;}
.rpt vera-mermaid{display:block;width:100%;margin:1.4em 0;}
.rpt svg{max-width:100%;height:auto;}
.rpt .card{background:var(--card);border:1px solid var(--bd);border-radius:12px;
padding:18px 20px;margin:1.2em 0;}
.rpt-foot{margin-top:48px;padding-top:14px;border-top:1px solid var(--bd);
color:var(--dim);font-size:.8rem;}
"""

_DOC_HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<title>__TITLE__</title><style>__CSS__</style></head><body>')

# Renders any <vera-mermaid> blocks after the element script loads. Guarded so it
# never double-draws if the element already auto-rendered from its text content.
_MERMAID_BOOT = (
    '<script src="/ui/elements/vera_mermaid.js"></script>'
    '<script>(function(){function go(){document.querySelectorAll("vera-mermaid")'
    '.forEach(function(el){try{if(el.querySelector("svg"))return;'
    'el.render((el.getAttribute("code")||el.textContent||"").trim());}catch(e){}});}'
    'if(document.readyState!=="loading")setTimeout(go,0);'
    'else document.addEventListener("DOMContentLoaded",go);})();</script>')


def _wrap_document(title: str, inner_html: str, *, mermaid: bool = True) -> str:
    head = _DOC_HEAD.replace("__TITLE__", _html.escape(title or "Report")) \
                    .replace("__CSS__", _REPORT_CSS)
    ts = now_iso()
    foot = ('<div class="rpt-foot">Generated by Vera · '
            + _html.escape(ts) + '</div>')
    boot = _MERMAID_BOOT if mermaid else ""
    return (head + '<main class="rpt"><header class="rpt-head"><h1>'
            + _html.escape(title or "Report") + '</h1><div class="meta">'
            + _html.escape(ts) + '</div></header>'
            + inner_html + foot + '</main>' + boot + '</body></html>')


def _naive_md_to_html(md: str) -> str:
    """Tiny, dependency-free markdown → HTML fallback (headings, lists, code,
    bold/italic/links, paragraphs). Used only when no LLM / pandoc path ran."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: List[str] = []
    in_ul = in_code = False
    def _inline(t: str) -> str:
        t = _html.escape(t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
        t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', t)
        return t
    for ln in lines:
        if ln.strip().startswith("```"):
            out.append("</pre>" if in_code else "<pre>"); in_code = not in_code; continue
        if in_code:
            out.append(_html.escape(ln)); continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            if in_ul: out.append("</ul>"); in_ul = False
            lvl = min(4, len(m.group(1))); out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>"); continue
        if re.match(r"^\s*[-*+]\s+", ln):
            if not in_ul: out.append("<ul>"); in_ul = True
            out.append("<li>" + _inline(re.sub(r"^\s*[-*+]\s+", "", ln)) + "</li>"); continue
        if in_ul: out.append("</ul>"); in_ul = False
        if ln.strip(): out.append("<p>" + _inline(ln) + "</p>")
    if in_ul: out.append("</ul>")
    if in_code: out.append("</pre>")
    return "\n".join(out)


def _headings(content: str) -> List[str]:
    return [m.group(2).strip() for m in
            re.finditer(r"^(#{1,3})\s+(.+)$", content or "", re.MULTILINE)][:8]


async def _gen_illustrations(subjects: List[str], count: int) -> List[Dict[str, str]]:
    """Ask the imagine system (media.illustrate) for up to `count` images; returns
    [{url,caption}] with same-origin /images/file URLs. Best-effort/never raises."""
    ill = _cap("media.illustrate")
    if not ill or count <= 0:
        return []
    out: List[Dict[str, str]] = []
    for subj in [s for s in subjects if s][:count]:
        try:
            r = await ill(subject=subj, style="clean editorial illustration",
                          mode="generate")
            for im in (r or {}).get("images", []):
                if im.get("url"):
                    out.append({"url": im["url"], "caption": im.get("caption") or subj})
        except Exception as e:
            log.debug("report.html illustrate failed for %r: %s", subj, e)
        if len(out) >= count:
            break
    return out


async def _author_inner_via_llm(content: str, title: str,
                                images: List[Dict[str, str]]) -> str:
    """Have llm.generate author the INNER document HTML (no <html>/<head>/<body>).
    Returns '' if the model isn't available or produced nothing usable."""
    gen = _cap("llm.generate")
    if not gen:
        return ""
    img_lines = "\n".join(f'- {im["url"]}  (suggested caption: {im.get("caption","")})'
                          for im in images) or "none"
    prompt = (
        "You are an expert editorial web designer. Turn the SOURCE MATERIAL below "
        "into the INNER HTML of a beautiful, self-contained report document titled "
        f'"{title}". Output ONLY HTML — a sequence of <section>/<h2>/<h3>/<p>/<ul>/'
        "<table>/<blockquote> etc. Do NOT include <html>, <head>, <body>, <style>, "
        "markdown, or code fences, and never reference external scripts/stylesheets.\n"
        "RICHNESS (use where they genuinely help — never fabricate data):\n"
        "• For any flow, architecture, sequence, timeline or state machine, emit a "
        "Mermaid diagram as <vera-mermaid bare>\\n<mermaid source>\\n</vera-mermaid> "
        "(flowchart/graph TD|LR, sequenceDiagram, stateDiagram-v2 or pie only).\n"
        "• For quantitative comparisons, hand-write a small inline <svg> bar/line "
        "chart, or a clean <table>. No external chart libraries.\n"
        "• Group key takeaways in <div class=\"card\">…</div>.\n"
        "• Place any of these images with <figure><img src=\"URL\" alt=\"…\">"
        "<figcaption>…</figcaption></figure>; available image URLs:\n" + img_lines + "\n"
        "Stay faithful to the source; do not invent facts, numbers, or citations.\n\n"
        "SOURCE MATERIAL:\n----------------\n" + (content or "")[:14000] +
        "\n----------------\nReturn the inner HTML now:")
    try:
        res = await gen(prompt=prompt)
    except Exception as e:
        log.debug("report.html llm author failed: %s", e)
        return ""
    inner = _strip_html_fence(_gen_text(res))
    # If the model wrapped a full doc anyway, salvage the body.
    bm = re.search(r"<body[^>]*>(.*)</body>", inner, re.DOTALL | re.IGNORECASE)
    if bm:
        inner = bm.group(1).strip()
    return inner if "<" in inner and len(inner) > 40 else ""


def _pandoc_inner(content: str) -> str:
    """Pandoc markdown → HTML *fragment* (no --standalone) for the pandoc mode /
    fallback. Returns '' if pandoc isn't installed or fails."""
    if not _pandoc_path():
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(content or ""); src = tf.name
    try:
        proc = subprocess.run([_pandoc_path(), src, "-f", "markdown", "-t", "html"],
                              capture_output=True, text=True, timeout=60)
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return ""
    finally:
        try: os.unlink(src)
        except OSError: pass


def _append_image_gallery(inner: str, images: List[Dict[str, str]]) -> str:
    """Append any imagine images the authored HTML didn't already embed."""
    extra = [im for im in images if im.get("url") and im["url"] not in inner]
    if not extra:
        return inner
    figs = "".join('<figure><img src="' + _html.escape(im["url"]) + '" alt="'
                   + _html.escape(im.get("caption", "")) + '"><figcaption>'
                   + _html.escape(im.get("caption", "")) + "</figcaption></figure>"
                   for im in extra)
    return inner + '<section><h2>Illustrations</h2>' + figs + "</section>"


@capability(
    "report.html", memory="off",
    http_method="POST", http_path="/render/report_html", http_tags=["render", "report"],
    description="Turn ANY run's output into a durable, visually rich, self-contained "
                "HTML DOCUMENT and return a viewable URL. Themed layout + Mermaid "
                "diagrams + hand-authored inline-SVG infographics + (optionally) images "
                "from the imagine system. Stored and served INLINE at /render/report/… "
                "so it opens in a tab/iframe and can be pinned to the gallery. This is "
                "the rich sibling of render.html (which is an ephemeral in-chat snippet) "
                "and render.export (plain pandoc). "
                "Input: content (str! — the report/answer text, markdown ok), "
                "title (str), mode (auto|author|template|pandoc, default auto), "
                "illustrate (int — # of imagine images to generate & embed, default 0), "
                "session_id (str), filename (str). "
                "Output: {ok, url, download_url, filename, artifact_id, title, bytes, "
                "mode_used, images}.",
)
async def report_html(content: str = "", title: str = "", mode: str = "auto",
                      illustrate: int = 0, session_id: str = "",
                      filename: str = "", trace_id=None):
    if not (content or "").strip():
        return {"ok": False, "error": "content required"}
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "author", "template", "pandoc"):
        mode = "auto"
    if not title:
        # First heading or first non-empty line makes a sensible title.
        h = _headings(content)
        title = (h[0] if h else next((l.strip().lstrip("# ").strip()
                 for l in content.splitlines() if l.strip()), "Report"))[:140]

    await emit_event({"type": "report.html", "stage": "start",
                      "title": title, "mode": mode})

    images: List[Dict[str, str]] = []
    if illustrate and int(illustrate) > 0:
        subjects = [title] + _headings(content)
        images = await _gen_illustrations(subjects, min(6, int(illustrate)))

    inner = ""
    mode_used = mode
    if mode in ("auto", "author", "template"):
        inner = await _author_inner_via_llm(content, title, images)
        mode_used = "author" if inner else mode_used
    if not inner and mode in ("auto", "pandoc"):
        pi = _pandoc_inner(content)
        if pi:
            inner = pi; mode_used = "pandoc"
    if not inner:
        inner = _naive_md_to_html(content); mode_used = "markdown"

    inner = _append_image_gallery(inner, images)
    doc = _wrap_document(title, inner, mermaid=True)

    stem = _safe_stem(filename or title or "report")
    out_name = f"{stem}-{uuid.uuid4().hex[:8]}.html"
    out_path = _OUT_DIR / out_name
    try:
        out_path.write_text(doc, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}"}
    # Sidecar metadata — feeds the artifacts gallery / re-delivery later.
    try:
        (_OUT_DIR / (out_name + ".json")).write_text(json.dumps({
            "kind": "html_report", "title": title, "created": now_iso(),
            "mode": mode_used, "bytes": len(doc.encode("utf-8", "replace")),
            "images": [im.get("url") for im in images], "session_id": session_id,
        }), encoding="utf-8")
    except Exception:
        pass

    result = {
        "ok": True, "title": title, "mode_used": mode_used,
        "filename": out_name, "artifact_id": out_name,
        "url": f"/render/report/{out_name}",
        "download_url": f"/render/download?name={out_name}",
        "bytes": len(doc.encode("utf-8", "replace")),
        "images": [im.get("url") for im in images],
    }
    await emit_event({"type": "report.html", "stage": "done",
                      "title": title, "url": result["url"],
                      "mode_used": mode_used, "session_id": session_id})
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GENERAL RE-DELIVERY — output.redeliver / output.channels
# Take ANY finished output (a chat message, a run's final text, a dream cycle's
# report) and re-deliver it through ANOTHER delivery channel — HTML report,
# podcast, email, telegram, notebook, memory, chat — WITHOUT touching the
# original. The routing twin already exists (vera/delivery.py); this cap is the
# generic engine that mirrors the dream deliver-stage invocation for one channel.
# ─────────────────────────────────────────────────────────────────────────────

_PASSTHROUGH_FMTS = {"", "markdown", "md", "standard", "text", "txt", "verbatim"}


async def _reshape_for_format(text: str, fmt: str) -> str:
    """Reshape text into a delivery channel's output-format style via one cheap
    llm.generate pass. Passthrough formats / unknown profile / no LLM → unchanged.
    output_formats.apply_format() returns a SYSTEM directive, which we prepend to
    the text (works whether or not llm.generate takes a separate `system` arg)."""
    fmt = (fmt or "").strip().lower()
    if fmt in _PASSTHROUGH_FMTS:
        return text
    try:
        from Vera.vera.output_formats import apply_format, get_profile
        if not get_profile(fmt):
            return text
        system = apply_format(
            "You reformat an existing report into the requested style WITHOUT "
            "adding, removing or inventing any facts. Output only the reformatted "
            "text, with no preamble.", fmt)
    except Exception:
        return text
    gen = _cap("llm.generate")
    if not gen:
        return text
    try:
        res = await gen(prompt=(system + "\n\n---\n\n" + (text or "")))
        out = _gen_text(res).strip()
        return out or text
    except Exception:
        return text


async def _resolve_source(source_type: str, source_id: str) -> tuple:
    """Resolve a (source_type, source_id) reference to (text, title). Currently
    handles dream cycles; other kinds should pass `content` directly."""
    st = (source_type or "").strip().lower()
    if st in ("dream_cycle", "dream", "cycle") and source_id:
        cap = _cap("dream.cycle.detail")
        if cap:
            try:
                d = await cap(cycle_id=source_id)
                rec = (d.get("cycle") or d.get("detail") or d) if isinstance(d, dict) else {}
                if isinstance(rec, dict):
                    return (rec.get("report") or rec.get("synthesis")
                            or rec.get("text") or "",
                            rec.get("title") or rec.get("trigger") or source_id)
            except Exception as e:
                log.debug("redeliver dream resolve failed: %s", e)
    return "", ""


@capability(
    "output.channels", memory="off", silent=True,
    http_method="GET", http_path="/render/output_channels", http_tags=["render", "output"],
    description="List the delivery channels output.redeliver can target (telegram, "
                "memory, notebook, email, chat, podcast, html report, + skill channels), "
                "each flagged `available` when its underlying cap is loaded. "
                "Output: {channels:[{id,label,cap,needs_target,target_label,available}], count}.",
)
async def output_channels(trace_id=None):
    chans = _delivery.list_channels()
    for c in chans:
        c["available"] = c.get("cap") in CAPABILITY_REGISTRY
    return {"channels": chans, "count": len(chans)}


@capability(
    "output.redeliver", memory="off",
    http_method="POST", http_path="/render/redeliver", http_tags=["render", "output"],
    description="Re-deliver an existing output through ANOTHER channel, leaving the "
                "original untouched — e.g. turn a chat answer into an HTML report, a "
                "podcast, an email, or a telegram note. Input: channel (str! — an id "
                "from output.channels: html|podcast|email|chat|telegram|notebook|memory|"
                "…), content (str — the text to re-deliver), OR source_type+source_id "
                "(e.g. dream_cycle + cycle id) to resolve it; title (str), "
                "target (str — channel address: email/session id, when the channel "
                "needs one), format (str — output-format override), illustrate (int — "
                "images for the html channel), html_mode (auto|author|template|pandoc). "
                "Output: {ok, channel, cap, title, url?, job_id?, filename?, result}.",
)
async def output_redeliver(channel: str = "", content: str = "", title: str = "",
                           source_type: str = "", source_id: str = "",
                           target: str = "", format: str = "", illustrate: int = 0,
                           html_mode: str = "", session_id: str = "", trace_id=None):
    channel = (channel or "").strip()
    if not channel:
        return {"ok": False, "error": "channel required (see output.channels)"}
    ch = _delivery.get_channel(channel)
    if not ch:
        return {"ok": False, "error": f"unknown channel: {channel} (see output.channels)"}
    if not (content or "").strip() and source_id:
        content, rtitle = await _resolve_source(source_type, source_id)
        if not title:
            title = rtitle
    if not (content or "").strip():
        return {"ok": False, "error": "content required (or a resolvable "
                                      "source_type+source_id)"}
    cap_name = ch.get("cap") or ""
    if cap_name not in CAPABILITY_REGISTRY:
        return {"ok": False, "error": f"channel cap unavailable: {cap_name}"}
    if not title:
        title = next((l.strip().lstrip("# ").strip()
                      for l in content.splitlines() if l.strip()), "Output")[:140]

    fmt = (format or ch.get("default_format") or "")
    rendered = await _reshape_for_format(content, fmt)
    # ctx mirrors the dream deliver stage; `target` is channel-specific (email
    # address for email, chat/html/podcast session id, …) — never auto-filled.
    ctx: Dict[str, Any] = {
        "label": title, "name": title,
        "target": (target or ch.get("fixed_target") or ""),
        "illustrate": int(illustrate or 0),
    }
    if html_mode:
        ctx["html_mode"] = html_mode
    args = _delivery.build_args(channel, rendered, ctx)

    await emit_event({"type": "output.redeliver", "stage": "start",
                      "channel": channel, "cap": cap_name, "title": title})
    cap = _cap(cap_name)
    try:
        res = await cap(**args)
    except Exception as e:
        return {"ok": False, "error": f"{cap_name} failed: {e}", "channel": channel}
    res = res if isinstance(res, dict) else {"result": res}
    ok = bool(res.get("ok", True)) and not res.get("error")
    out: Dict[str, Any] = {"ok": ok, "channel": channel, "cap": cap_name,
                           "title": title, "format_used": fmt, "result": res}
    for k in ("url", "download_url", "filename", "artifact_id", "job_id",
              "episode_id"):
        if res.get(k):
            out[k] = res[k]
    if res.get("error"):
        out["error"] = res["error"]
    await emit_event({"type": "output.redeliver", "stage": "done",
                      "channel": channel, "ok": ok, "title": title})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ARTIFACTS GALLERY — gallery.add / list / get / remove / update
# A single global, curated shelf for the outputs worth keeping: HTML reports,
# generated images, exported documents, podcasts, links. Redis-backed (a hash of
# records + a sorted set for recency), so it survives restarts and is shared
# across the cluster ([[vera-cluster-shared-backend]]). Any producer (report.html,
# output.redeliver, the image gallery, a chat action) pins into it; the /gallery
# panel browses it.
# ─────────────────────────────────────────────────────────────────────────────

_GAL_H = "vera:gallery:h"     # hash  id -> json record
_GAL_Z = "vera:gallery:z"     # zset  id -> created epoch (recency ordering)
_GAL_KINDS = {"html_report", "image", "export", "podcast", "link", "text", "other"}


def _gal_redis():
    return getattr(_core, "REDIS", None)


def _gal_norm_kind(k: str) -> str:
    k = (k or "").strip().lower()
    return k if k in _GAL_KINDS else "other"


async def _gal_save(rec: Dict[str, Any]) -> bool:
    r = _gal_redis()
    if not r:
        return False
    try:
        await r.hset(_GAL_H, rec["id"], json.dumps(rec))
        await r.zadd(_GAL_Z, {rec["id"]: float(rec.get("_score") or time.time())})
        return True
    except Exception as e:
        log.debug("gallery save failed: %s", e)
        return False


@capability(
    "gallery.add", memory="off",
    http_method="POST", http_path="/gallery/add", http_tags=["gallery", "output"],
    description="Pin an artifact to the global artifacts gallery. Input: title (str!), "
                "url (str — viewable URL, e.g. a report.html /render/report/… link), "
                "kind (html_report|image|export|podcast|link|text|other), download_url "
                "(str), thumb (str — image URL for the card), tags (list|csv), note "
                "(str), source_id (str — session/loop/cycle it came from), artifact_id "
                "(str), bytes (int), meta (object). Output: {ok, id}.",
)
async def gallery_add(title: str = "", url: str = "", kind: str = "other",
                      download_url: str = "", thumb: str = "", tags: Any = "",
                      note: str = "", source_id: str = "", artifact_id: str = "",
                      bytes: int = 0, meta: Optional[Dict[str, Any]] = None,
                      session_id: str = "", trace_id=None):
    if not (title or "").strip() and not (url or "").strip():
        return {"ok": False, "error": "title or url required"}
    if not _gal_redis():
        return {"ok": False, "error": "gallery store unavailable (no redis)"}
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        tags = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tags = []
    gid = uuid.uuid4().hex[:12]
    rec = {
        "id": gid, "title": (title or url or "artifact")[:200],
        "url": url or "", "download_url": download_url or "",
        "kind": _gal_norm_kind(kind), "thumb": thumb or "",
        "tags": tags[:20], "note": (note or "")[:2000],
        "source_id": source_id or session_id or "", "artifact_id": artifact_id or "",
        "bytes": int(bytes or 0), "meta": meta if isinstance(meta, dict) else {},
        "created": now_iso(), "_score": time.time(),
    }
    if not await _gal_save(rec):
        return {"ok": False, "error": "gallery store write failed"}
    await emit_event({"type": "gallery.add", "id": gid, "kind": rec["kind"],
                      "title": rec["title"]})
    return {"ok": True, "id": gid, "kind": rec["kind"], "title": rec["title"]}


@capability(
    "gallery.list", memory="off", silent=True,
    http_method="GET", http_path="/gallery/list", http_tags=["gallery", "output"],
    description="List gallery artifacts, newest first. Input: limit (int, default 60), "
                "kind (filter), tag (filter), q (title/note substring). "
                "Output: {items:[{id,title,url,download_url,kind,thumb,tags,note,"
                "source_id,bytes,created}], count, total}.",
)
async def gallery_list(limit: int = 60, kind: str = "", tag: str = "",
                       q: str = "", trace_id=None):
    r = _gal_redis()
    if not r:
        return {"items": [], "count": 0, "total": 0,
                "error": "gallery store unavailable (no redis)"}
    try:
        total = int(await r.zcard(_GAL_Z) or 0)
        ids = await r.zrevrange(_GAL_Z, 0, max(0, int(limit or 60)) * 3)
        recs: List[Dict[str, Any]] = []
        if ids:
            raws = await r.hmget(_GAL_H, ids)
            for raw in raws:
                if not raw:
                    continue
                try:
                    recs.append(json.loads(raw))
                except Exception:
                    continue
    except Exception as e:
        return {"items": [], "count": 0, "total": 0, "error": str(e)}
    kind = (kind or "").strip().lower()
    tag = (tag or "").strip().lower()
    ql = (q or "").strip().lower()
    out = []
    for rec in recs:
        if kind and rec.get("kind") != kind:
            continue
        if tag and tag not in [str(t).lower() for t in (rec.get("tags") or [])]:
            continue
        if ql and ql not in (rec.get("title", "") + " " + rec.get("note", "")).lower():
            continue
        rec.pop("_score", None)
        out.append(rec)
        if len(out) >= int(limit or 60):
            break
    return {"items": out, "count": len(out), "total": total}


@capability(
    "gallery.get", memory="off", silent=True,
    http_method="GET", http_path="/gallery/get", http_tags=["gallery", "output"],
    description="Get one gallery artifact by id. Input: id (str!). Output: {ok, item}.",
)
async def gallery_get(id: str = "", trace_id=None):
    r = _gal_redis()
    if not r:
        return {"ok": False, "error": "gallery store unavailable"}
    raw = await r.hget(_GAL_H, (id or "").strip())
    if not raw:
        return {"ok": False, "error": "not found"}
    try:
        rec = json.loads(raw); rec.pop("_score", None)
        return {"ok": True, "item": rec}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@capability(
    "gallery.remove", memory="off",
    http_method="POST", http_path="/gallery/remove", http_tags=["gallery", "output"],
    description="Remove an artifact from the gallery (does NOT delete the underlying "
                "file/output). Input: id (str!). Output: {ok, removed}.",
)
async def gallery_remove(id: str = "", trace_id=None):
    r = _gal_redis()
    if not r:
        return {"ok": False, "error": "gallery store unavailable"}
    gid = (id or "").strip()
    try:
        n = await r.hdel(_GAL_H, gid)
        await r.zrem(_GAL_Z, gid)
        await emit_event({"type": "gallery.remove", "id": gid})
        return {"ok": True, "removed": bool(n)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@capability(
    "gallery.update", memory="off",
    http_method="POST", http_path="/gallery/update", http_tags=["gallery", "output"],
    description="Edit a gallery artifact's title / note / tags. Input: id (str!), "
                "title (str), note (str), tags (list|csv). Only provided fields change. "
                "Output: {ok, item}.",
)
async def gallery_update(id: str = "", title: str = "", note: str = "",
                         tags: Any = None, trace_id=None):
    r = _gal_redis()
    if not r:
        return {"ok": False, "error": "gallery store unavailable"}
    gid = (id or "").strip()
    raw = await r.hget(_GAL_H, gid)
    if not raw:
        return {"ok": False, "error": "not found"}
    try:
        rec = json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if title.strip():
        rec["title"] = title[:200]
    if note:
        rec["note"] = note[:2000]
    if tags is not None:
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        rec["tags"] = [str(t).strip() for t in (tags or []) if str(t).strip()][:20]
    await _gal_save(rec)
    rec.pop("_score", None)
    return {"ok": True, "item": rec}


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD ROUTE — serve generated files read-only from the output dir
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/render/download", include_in_schema=False)
async def render_download(name: str = ""):
    # Validate the requested name stays within _OUT_DIR (no traversal).
    safe = os.path.basename(name or "")
    target = (_OUT_DIR / safe).resolve()
    try:
        target.relative_to(_OUT_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(target), filename=safe)


@APP.get("/render/report/{name}", include_in_schema=False)
async def render_report_view(name: str):
    """Serve a report.html document INLINE (text/html) so it renders in a tab or
    iframe. Same-origin, so its <vera-mermaid> script + /images/file URLs resolve."""
    safe = os.path.basename(name or "")
    target = (_OUT_DIR / safe).resolve()
    try:
        target.relative_to(_OUT_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    if not target.is_file() or target.suffix.lower() != ".html":
        return JSONResponse({"error": "not found"}, status_code=404)
    return HTMLResponse(target.read_text(encoding="utf-8", errors="replace"))


# ─────────────────────────────────────────────────────────────────────────────
# GALLERY PANEL — a self-contained browser page (served same-origin so /mcp/call
# works) iframed by the "Artifacts" tab. Plain string (NOT an f-string) so the
# CSS/JS braces are literal.
# ─────────────────────────────────────────────────────────────────────────────

_GALLERY_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Artifacts</title>
<style>
:root{--bg:#14161a;--card:#1b1e24;--ink:#e8e9ec;--dim:#9aa1ad;--acc:#6fb6a5;--bd:#2a2e37;}
@media (prefers-color-scheme:light){:root{--bg:#fbfbfa;--card:#fff;--ink:#1c1d22;--dim:#5c6270;--acc:#5a9e8f;--bd:#e6e6e9;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}
.bar{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--bd);
padding:10px 14px;display:flex;gap:8px;align-items:center;z-index:2;}
.bar b{font-size:1rem;margin-right:auto;}
.bar .cnt{color:var(--dim);font-size:.82rem;}
select,input,button{background:var(--card);color:var(--ink);border:1px solid var(--bd);
border-radius:7px;padding:6px 9px;font-size:.85rem;}
button{cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;padding:16px;}
.c{background:var(--card);border:1px solid var(--bd);border-radius:12px;overflow:hidden;
display:flex;flex-direction:column;}
.mlink{display:block}
.thumb{height:130px;background:#0000000f;display:flex;align-items:center;justify-content:center;overflow:hidden;}
.thumb img{width:100%;height:100%;object-fit:cover;}
.ph{font-size:2.6rem;opacity:.6}
.body{padding:11px 12px;display:flex;flex-direction:column;gap:6px;flex:1;}
.k{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;color:var(--acc);}
.t{font-weight:600;line-height:1.3;}
.note{color:var(--dim);font-size:.82rem;}
.tags{display:flex;flex-wrap:wrap;gap:4px;}
.tag{background:#6fb6a522;color:var(--acc);border-radius:5px;padding:1px 6px;font-size:.72rem;}
.meta{color:var(--dim);font-size:.72rem;margin-top:auto;}
.acts{display:flex;gap:6px;flex-wrap:wrap;}
.acts .btn{font-size:.78rem;padding:4px 9px;border-radius:6px;text-decoration:none;color:var(--ink);
background:var(--bg);border:1px solid var(--bd);}
.acts .btn:hover{border-color:var(--acc);color:var(--acc);}
.acts .del:hover{border-color:#c96;color:#c96;}
.empty{grid-column:1/-1;text-align:center;color:var(--dim);padding:60px 20px;}
</style></head><body>
<div class="bar"><b>&#128444; Artifacts Gallery</b>
<span class="cnt" id="cnt"></span>
<select id="fKind"><option value="">All kinds</option><option value="html_report">HTML reports</option>
<option value="image">Images</option><option value="export">Exports</option>
<option value="podcast">Podcasts</option><option value="link">Links</option>
<option value="text">Text</option><option value="other">Other</option></select>
<input id="fQ" placeholder="Search…" style="width:150px">
<button id="ref">&#8635; Refresh</button></div>
<div class="grid" id="grid"></div>
<script>
const $=s=>document.querySelector(s);
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function call(name,args){try{const r=await fetch('/mcp/call',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({name,arguments:args||{}})});
const j=await r.json();return j&&j.content;}catch(e){return {error:String(e&&e.message||e)};}}
const ICON={html_report:'\\uD83D\\uDDBC',image:'\\uD83D\\uDDBC',export:'\\uD83D\\uDCC4',
podcast:'\\uD83C\\uDF99',link:'\\uD83D\\uDD17',text:'\\uD83D\\uDCDD',other:'\\uD83D\\uDCE6'};
function card(it){
  const thumb=it.thumb||(it.kind==='image'?it.url:'');
  const media=thumb?'<div class="thumb"><img src="'+esc(thumb)+'" loading="lazy"></div>'
    :'<div class="thumb"><div class="ph">'+(ICON[it.kind]||ICON.other)+'</div></div>';
  const tags=(it.tags||[]).map(t=>'<span class="tag">'+esc(t)+'</span>').join('');
  const open=it.url?'<a class="btn" href="'+esc(it.url)+'" target="_blank" rel="noopener">Open</a>':'';
  const dl=it.download_url?'<a class="btn" href="'+esc(it.download_url)+'" target="_blank" rel="noopener">Download</a>':'';
  return '<div class="c"><a class="mlink" href="'+esc(it.url||'#')+'" target="_blank" rel="noopener">'+media+'</a>'
    +'<div class="body"><div class="k">'+esc(it.kind)+'</div><div class="t">'+esc(it.title)+'</div>'
    +(it.note?'<div class="note">'+esc(it.note)+'</div>':'')
    +(tags?'<div class="tags">'+tags+'</div>':'')
    +'<div class="meta">'+esc((it.created||'').replace('T',' ').slice(0,16))+'</div>'
    +'<div class="acts">'+open+dl+'<button class="btn del" onclick="del(\\''+esc(it.id)+'\\')">Delete</button></div></div></div>';
}
async function load(){
  const kind=$('#fKind').value,q=$('#fQ').value.trim();
  $('#grid').innerHTML='<div class="empty">Loading…</div>';
  const c=await call('gallery.list',{limit:120,kind:kind,q:q});
  const items=(c&&c.items)||[];
  if(!items.length){$('#grid').innerHTML='<div class="empty">'+(c&&c.error?esc(c.error)
    :'No saved artifacts yet. Use \\u201c\\u2605 Save to gallery\\u201d in chat.')+'</div>';
    $('#cnt').textContent='0';return;}
  $('#cnt').textContent=((c.total||items.length)+' item(s)');
  $('#grid').innerHTML=items.map(card).join('');
}
async function del(id){if(!confirm('Remove from gallery? (the underlying file is kept)'))return;
  await call('gallery.remove',{id:id});load();}
$('#fKind').onchange=load;$('#ref').onclick=load;
$('#fQ').oninput=()=>{clearTimeout(window._t);window._t=setTimeout(load,250);};
load();
</script></body></html>"""


@APP.get("/gallery/view", include_in_schema=False)
async def gallery_view():
    """Self-contained gallery browser, served same-origin so its /mcp/call works.
    Iframed by the Artifacts tab (register_ui below)."""
    return HTMLResponse(_GALLERY_PAGE)


register_ui(
    panel_id="gallery", label="Artifacts", icon="🖼", mode="tab", tab_order=212,
    html=('<iframe src="/gallery/view" title="Artifacts Gallery" '
          'style="width:100%;height:100%;border:0;display:block;background:transparent">'
          '</iframe>'),
    ui_caps=["gallery.list", "gallery.get", "gallery.remove", "gallery.update", "gallery.add"],
)


log.info("render_capabilities: ready (pandoc=%s, pdf_engine=%s)",
         bool(_pandoc_path()), _pdf_engine() or "none")
