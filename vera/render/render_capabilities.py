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

from fastapi.responses import FileResponse, JSONResponse

from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso,
)
from Vera.vera.output_formats import FORMAT_PROFILES

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


log.info("render_capabilities: ready (pandoc=%s, pdf_engine=%s)",
         bool(_pandoc_path()), _pdf_engine() or "none")
