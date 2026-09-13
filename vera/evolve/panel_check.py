"""Syntax gate for the UI half of a branch: panel HTML and JS.

The pipeline gate byte-compiled every changed .py and never looked at a panel.
On 2026-09-11 an adopt PASSED with `<<<<<<< HEAD` conflict markers sitting
inside an inline <script> of workers_ollama_panel.html - a page that would
have shipped and simply stopped rendering, with no gate signal at all.

This is deliberately the same shape as the .py check: it proves the file
PARSES, nothing about behaviour. Pure apart from one subprocess (`node
--check`), which is optional: without node the marker check still runs and the
result says the script check was skipped rather than pretending it passed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List

_MARKER = re.compile(r"^(<<<<<<< |=======$|>>>>>>> )", re.M)
# Inline scripts only: a <script src=...> is somebody else's file.
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
# type="module" / "importmap" / non-JS types are not classic scripts; node
# --check would parse `import` in a module wrongly, so those are skipped.
_NON_CLASSIC = re.compile(r'type\s*=\s*["\'](module|importmap|application/(ld\+)?json|text/(template|x-[^"\']+))["\']', re.I)


def conflict_markers(text: str) -> List[int]:
    """1-based line numbers of git conflict markers."""
    return [text.count("\n", 0, m.start()) + 1 for m in _MARKER.finditer(text or "")]


def inline_scripts(html: str) -> List[Dict[str, object]]:
    """Every classic inline <script> with the line it starts on."""
    out = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", html or "", re.S | re.I):
        attrs, body = m.group(1), m.group(2)
        if _NON_CLASSIC.search(attrs or ""):
            continue
        out.append({"line": html.count("\n", 0, m.start(2)) + 1, "body": body})
    return out


def node_available() -> bool:
    return shutil.which("node") is not None


def _node_check(js: str, *, module: bool = False) -> str:
    """'' if node parses it, else node's first error line.

    `module` keeps the .mjs extension on the temp file. node decides script vs
    ES module BY EXTENSION, so copying an .mjs to a temp .js made node parse
    valid ESM as a classic script and fail on its first `import` - which is
    exactly what happened to tests/test_widget_element.mjs on 2026-09-11.
    """
    fd, path = tempfile.mkstemp(suffix=".mjs" if module else ".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return ""
        # node prints the file path, the offending line, a caret, then the
        # SyntaxError. The SyntaxError line is the useful one.
        lines = [ln for ln in (r.stderr or "").splitlines() if ln.strip()]
        err = next((ln for ln in lines if "Error" in ln), lines[-1] if lines else "node --check failed")
        return err.strip()
    except subprocess.TimeoutExpired:
        return "node --check timed out"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def check_html(text: str, *, use_node: bool = True) -> Dict[str, object]:
    """Problems with one HTML file: conflict markers and unparseable inline
    scripts. `skipped` says when the script check could not run, so a caller
    can report 'not checked' instead of 'passed'."""
    problems: List[str] = []
    for ln in conflict_markers(text):
        problems.append(f"line {ln}: git conflict marker")
    scripts = inline_scripts(text)
    skipped = ""
    if scripts:
        if use_node and node_available():
            for sc in scripts:
                err = _node_check(str(sc["body"]))
                if err:
                    problems.append(f"inline <script> at line {sc['line']}: {err}")
        else:
            skipped = "node not available - inline scripts not syntax-checked"
    return {"ok": not problems, "problems": problems, "scripts": len(scripts), "skipped": skipped}


def check_js(text: str, *, use_node: bool = True, module: bool = False) -> Dict[str, object]:
    problems = [f"line {ln}: git conflict marker" for ln in conflict_markers(text)]
    skipped = ""
    if use_node and node_available():
        err = _node_check(text or "", module=module)
        if err:
            problems.append(err)
    else:
        skipped = "node not available - not syntax-checked"
    return {"ok": not problems, "problems": problems, "scripts": 1, "skipped": skipped}


def check_file(path: str, text: str, *, use_node: bool = True) -> Dict[str, object]:
    p = (path or "").lower()
    if p.endswith(".html") or p.endswith(".htm"):
        return check_html(text, use_node=use_node)
    if p.endswith(".mjs"):
        return check_js(text, use_node=use_node, module=True)
    if p.endswith(".js") or p.endswith(".cjs"):
        return check_js(text, use_node=use_node)
    return {"ok": True, "problems": [], "scripts": 0, "skipped": "not a panel file"}


def is_ui_file(path: str) -> bool:
    p = (path or "").lower()
    return p.endswith((".html", ".htm", ".js", ".mjs", ".cjs"))
