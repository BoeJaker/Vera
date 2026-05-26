"""
ide_code_capabilities.py  —  Vera IDE Coding Tool Capabilities
==================================================================
Companion module for ide_capabilities.py that adds structured
file-manipulation tools an LLM agent can actually use:

  ide.fs.exists         — dedicated existence check (no error codepath)
  ide.code.read_lines   — read a specific line range from a file
  ide.code.edit_lines   — replace a line range with new content
  ide.code.insert_at    — insert content before a given line
  ide.code.grep         — ripgrep-style search across files
  ide.code.replace      — find/replace in a file (literal or regex)
  ide.code.list_files   — recursive listing, gitignore-aware
  ide.code.outline      — extract symbols from a source file
  ide.code.tool_dispatch — meta-capability the agent calls; enforces whitelist
  ide.code.whitelist    — read/update allowed coding tools
  ide.code.tool_manifest — JSON schema of all coding tools for LLM prompts

All actions taken through tool_dispatch are recorded to the memory graph
and data fabric via the shared _record() helper from ide_capabilities,
so every agent edit is observable and replayable.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from Vera.Orchestration.capability_orchestration import (
    CAPABILITY_REGISTRY, capability, emit_event, now_iso,
)

# Borrow helpers + session recorder from the main IDE module.
# This import MUST come after ide_capabilities has registered its decorators
# (load order is handled by capabilities.py — companion modules are loaded
# sequentially, and ide_capabilities ships before this one if named *_code_*).
from Vera.Orchestration.ide_capabilities import (  # type: ignore
    PROJECT_ROOT,
    _record,
    _ide_get_session_id,
)

log = logging.getLogger("vera.ide.code")


# ─────────────────────────────────────────────────────────────────────────────
# PATH SAFETY
# ─────────────────────────────────────────────────────────────────────────────
# Every code tool accepts absolute or relative paths; relative is resolved
# against the caller-supplied `root` (or PROJECT_ROOT). We always resolve to
# an absolute Path and refuse to traverse above the root sentinel.

def _safe_path(path: str, root: str = "") -> Path:
    """Return a safe absolute Path. If root is given, refuse anything outside it."""
    if not path:
        raise ValueError("path is required")
    p = Path(path)
    if not p.is_absolute():
        base = Path(root) if root else PROJECT_ROOT
        p = (base / path).resolve()
    else:
        p = p.resolve()
    if root:
        root_p = Path(root).resolve()
        try:
            p.relative_to(root_p)
        except ValueError:
            raise ValueError(f"path escapes root: {p} not under {root_p}")
    return p


def _is_text_file(p: Path, sample: int = 2048) -> bool:
    """Heuristic: file is text if the first N bytes contain no NULs and mostly printable."""
    try:
        with p.open("rb") as f:
            chunk = f.read(sample)
        if not chunk:
            return True
        if b"\x00" in chunk:
            return False
        # Ratio of printable ASCII
        printable = sum(1 for b in chunk if 9 <= b <= 13 or 32 <= b < 127)
        return printable / max(len(chunk), 1) > 0.85
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# ide.fs.exists  — dedicated existence check (fixes the scaffold bug where
# /ide/fs/read returned 200 with {error:...} for missing files, causing the
# UI to think every file already existed).
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.fs.exists",
    http_method="GET", http_path="/ide/fs/exists", http_tags=["ide", "fs"],
    memory="off", silent=True,
    description="Check whether a path exists on the real filesystem. "
                "GET with ?path=... query param. "
                "Output: {path, exists, kind (file|directory|missing), size}.",
)
async def ide_fs_exists(path: str = "", trace_id=None):
    try:
        if not path:
            return {"path": "", "exists": False, "kind": "missing", "size": 0}
        p = Path(path)
        if not p.exists():
            return {"path": path, "exists": False, "kind": "missing", "size": 0}
        st = p.stat()
        return {
            "path":   str(p),
            "exists": True,
            "kind":   "directory" if p.is_dir() else "file",
            "size":   st.st_size,
            "mtime":  st.st_mtime,
        }
    except Exception as e:
        return {"path": path, "exists": False, "kind": "error", "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.read_lines  — read a specific line range
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.code.read_lines",
    http_method="POST", http_path="/ide/code/read_lines", http_tags=["ide", "code"],
    memory="off",
    description="Read a range of lines from a file (1-indexed, inclusive). "
                "Input: path (str!), start (int, default 1), end (int, default last), root (str). "
                "Output: {path, start, end, total_lines, lines: [str], content (joined)}.",
)
async def ide_code_read_lines(
    path:  str,
    start: int = 1,
    end:   int = 0,     # 0 = end of file
    root:  str = "",
    trace_id=None,
):
    try:
        p = _safe_path(path, root)
        if not p.exists() or not p.is_file():
            return {"error": f"File not found: {path}", "exists": False}
        if not _is_text_file(p):
            return {"error": "Not a text file", "path": str(p)}
        all_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(all_lines)
        s = max(1, int(start))
        e = total if not end else min(total, int(end))
        if s > total:
            return {"path": str(p), "start": s, "end": e, "total_lines": total,
                    "lines": [], "content": ""}
        slice_lines = all_lines[s - 1:e]
        return {
            "path":        str(p),
            "start":       s,
            "end":         e,
            "total_lines": total,
            "lines":       slice_lines,
            "content":     "\n".join(slice_lines),
        }
    except Exception as ex:
        return {"error": str(ex)}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.edit_lines  — replace a line range with new content
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.code.edit_lines",
    http_method="POST", http_path="/ide/code/edit_lines", http_tags=["ide", "code"],
    memory="off",
    description="Replace lines [start..end] (1-indexed, inclusive) with new_content. "
                "If start > last line, content is appended. "
                "Input: path (str!), start (int!), end (int!), new_content (str), "
                "root (str), agent (str), session_id (str). "
                "Output: {path, ok, lines_before, lines_after, replaced_range}.",
)
async def ide_code_edit_lines(
    path:        str,
    start:       int,
    end:         int,
    new_content: str = "",
    root:        str = "",
    agent:       str = "",
    session_id:  str = "",
    trace_id=None,
):
    try:
        p = _safe_path(path, root)
        existed = p.exists()
        if existed and not p.is_file():
            return {"error": f"Not a file: {path}", "ok": False}
        original = p.read_text(encoding="utf-8", errors="replace") if existed else ""
        lines = original.splitlines()
        lines_before = len(lines)
        s = max(1, int(start))
        e = max(s, int(end))

        # Handle append case
        if s > lines_before:
            # pad + append
            new_lines = lines + [""] * (s - lines_before - 1) + new_content.splitlines()
        else:
            new_lines = (
                lines[:s - 1]
                + new_content.splitlines()
                + lines[e:]
            )
        # Preserve trailing newline iff original had one OR new_content ends with \n
        trailing_nl = original.endswith("\n") or new_content.endswith("\n") or not existed
        new_text = "\n".join(new_lines) + ("\n" if trailing_nl else "")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_text, encoding="utf-8")

        size = len(new_text.encode("utf-8", errors="replace"))
        sid = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sid, category="ide.code_edit",
            text=f"[edit_lines] {p.name} {s}-{e}" + (f" by {agent}" if agent else ""),
            full_text=f"Path: {p}\nRange: {s}-{e}\nAgent: {agent}\n\nNew content:\n{new_content[:2000]}",
            tags=["ide", "code", "edit_lines"] + ([agent] if agent else []),
            importance=0.6, source_type="ai" if agent else "tool",
            record_type="observation",
            capability_name="ide.code.edit_lines", broadcast_type="ide.code_edited",
            fabric_dataset="ide.code_edits",
            metadata={"path": str(p), "start": s, "end": e, "agent": agent, "bytes": size},
            fabric_data={"path": str(p), "start": s, "end": e,
                         "agent": agent, "new_content": new_content[:20000]},
        ))

        return {
            "path":           str(p),
            "ok":             True,
            "created":        not existed,
            "lines_before":   lines_before,
            "lines_after":    len(new_lines),
            "replaced_range": [s, e],
            "bytes":          size,
        }
    except Exception as ex:
        return {"error": str(ex), "ok": False}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.insert_at  — insert content before a given line
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.code.insert_at",
    http_method="POST", http_path="/ide/code/insert_at", http_tags=["ide", "code"],
    memory="off",
    description="Insert content BEFORE the given line number (1-indexed). "
                "Use line=0 to prepend, line=-1 (or greater than EOF) to append. "
                "Input: path (str!), line (int!), content (str!), "
                "root (str), agent (str), session_id (str). "
                "Output: {path, ok, inserted_at, lines_after}.",
)
async def ide_code_insert_at(
    path:       str,
    line:       int,
    content:    str,
    root:       str = "",
    agent:      str = "",
    session_id: str = "",
    trace_id=None,
):
    try:
        p = _safe_path(path, root)
        existed = p.exists()
        original = p.read_text(encoding="utf-8", errors="replace") if existed else ""
        lines = original.splitlines()
        n = len(lines)
        # Normalise target
        if line < 0 or line > n:
            idx = n           # append
            actual = n + 1
        elif line == 0:
            idx = 0           # prepend
            actual = 1
        else:
            idx = line - 1
            actual = line
        new_lines = lines[:idx] + content.splitlines() + lines[idx:]
        trailing_nl = original.endswith("\n") or content.endswith("\n") or not existed
        new_text = "\n".join(new_lines) + ("\n" if trailing_nl else "")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_text, encoding="utf-8")

        sid = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sid, category="ide.code_edit",
            text=f"[insert_at] {p.name} line {actual}" + (f" by {agent}" if agent else ""),
            full_text=f"Path: {p}\nInserted at line: {actual}\nAgent: {agent}\n\n{content[:2000]}",
            tags=["ide", "code", "insert_at"] + ([agent] if agent else []),
            importance=0.55, source_type="ai" if agent else "tool",
            record_type="observation",
            capability_name="ide.code.insert_at", broadcast_type="ide.code_edited",
            fabric_dataset="ide.code_edits",
            metadata={"path": str(p), "line": actual, "agent": agent},
            fabric_data={"path": str(p), "line": actual,
                         "agent": agent, "content": content[:20000]},
        ))
        return {
            "path":        str(p),
            "ok":          True,
            "created":     not existed,
            "inserted_at": actual,
            "lines_after": len(new_lines),
        }
    except Exception as ex:
        return {"error": str(ex), "ok": False}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.grep  — ripgrep-style text search
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "htmlcov",
}

_DEFAULT_IGNORE_EXTS = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".o", ".a", ".obj",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    ".mp3", ".mp4", ".mov", ".avi", ".webm", ".wav", ".flac",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}


def _should_skip_dir(name: str) -> bool:
    return name in _DEFAULT_IGNORE_DIRS or name.startswith(".") and name not in {".env", ".gitignore", ".dockerignore"}


def _walk_files(
    root: Path,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    max_files: int = 5000,
):
    """Yield files under root, honouring default ignores + include/exclude globs."""
    include = include or []
    exclude = exclude or []
    count = 0
    for dirpath, dirnames, filenames in __import__("os").walk(root):
        # prune dirs in-place
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fn in filenames:
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            if ext in _DEFAULT_IGNORE_EXTS:
                continue
            rel = str(p.relative_to(root))
            if include and not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(fn, g) for g in include):
                continue
            if exclude and any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(fn, g) for g in exclude):
                continue
            yield p
            count += 1
            if count >= max_files:
                return


@capability(
    "ide.code.grep",
    http_method="POST", http_path="/ide/code/grep", http_tags=["ide", "code"],
    memory="off",
    description="Search for a pattern across files in a root directory. "
                "Respects .git/node_modules/etc ignores and binary-file skips. "
                "Input: pattern (str!), root (str!), is_regex (bool), "
                "case_sensitive (bool), include (list of globs like ['*.py']), "
                "exclude (list of globs), max_results (int, default 200), "
                "context_lines (int, default 1). "
                "Output: {root, pattern, total_matches, matches: "
                "[{path, line, col, text, context_before, context_after}]}.",
)
async def ide_code_grep(
    pattern:        str,
    root:           str,
    is_regex:       bool = False,
    case_sensitive: bool = True,
    include:        str  = "",   # comma-sep globs or JSON list
    exclude:        str  = "",
    max_results:    int  = 200,
    context_lines:  int  = 1,
    trace_id=None,
):
    try:
        if not pattern:
            return {"error": "pattern is required", "matches": []}
        root_p = Path(root).resolve()
        if not root_p.exists() or not root_p.is_dir():
            return {"error": f"Not a directory: {root}", "matches": []}

        # Normalise include/exclude (accept list or csv)
        def _split(x):
            if not x: return []
            if isinstance(x, list): return [str(g).strip() for g in x if g]
            x = str(x).strip()
            if x.startswith("["):
                try: return [str(g).strip() for g in json.loads(x) if g]
                except Exception: pass
            return [s.strip() for s in x.split(",") if s.strip()]

        inc = _split(include)
        exc = _split(exclude)

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            rx = re.compile(pattern if is_regex else re.escape(pattern), flags)
        except re.error as e:
            return {"error": f"Bad regex: {e}", "matches": []}

        matches: List[Dict[str, Any]] = []
        files_scanned = 0
        for p in _walk_files(root_p, inc, exc):
            files_scanned += 1
            if not _is_text_file(p):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines):
                m = rx.search(line)
                if not m:
                    continue
                cb = lines[max(0, i - context_lines):i] if context_lines else []
                ca = lines[i + 1:i + 1 + context_lines] if context_lines else []
                matches.append({
                    "path":            str(p),
                    "rel":             str(p.relative_to(root_p)),
                    "line":            i + 1,
                    "col":             m.start() + 1,
                    "text":            line[:500],
                    "match":           m.group(0)[:200],
                    "context_before":  cb,
                    "context_after":   ca,
                })
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        return {
            "root":          str(root_p),
            "pattern":       pattern,
            "is_regex":      is_regex,
            "files_scanned": files_scanned,
            "total_matches": len(matches),
            "truncated":     len(matches) >= max_results,
            "matches":       matches,
        }
    except Exception as ex:
        return {"error": str(ex), "matches": []}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.replace  — find/replace in a file (literal or regex)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.code.replace",
    http_method="POST", http_path="/ide/code/replace", http_tags=["ide", "code"],
    memory="off",
    description="Find/replace in a single file. Literal OR regex. "
                "Set first_only=True to replace just the first occurrence. "
                "Set dry_run=True to preview without writing. "
                "Input: path (str!), find (str!), replace (str), is_regex (bool), "
                "case_sensitive (bool), first_only (bool), dry_run (bool), "
                "root (str), agent (str), session_id (str). "
                "Output: {path, ok, replacements, preview (when dry_run)}.",
)
async def ide_code_replace(
    path:           str,
    find:           str,
    replace:        str  = "",
    is_regex:       bool = False,
    case_sensitive: bool = True,
    first_only:     bool = False,
    dry_run:        bool = False,
    root:           str  = "",
    agent:          str  = "",
    session_id:     str  = "",
    trace_id=None,
):
    try:
        p = _safe_path(path, root)
        if not p.exists() or not p.is_file():
            return {"error": f"File not found: {path}", "ok": False}
        original = p.read_text(encoding="utf-8", errors="replace")

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            rx = re.compile(find if is_regex else re.escape(find), flags)
        except re.error as e:
            return {"error": f"Bad regex: {e}", "ok": False}

        count = 1 if first_only else 0   # re.sub count=0 means all
        new_text, n = rx.subn(replace, original, count=count)

        if dry_run:
            # Show a diff-ish preview of first 3 occurrences
            preview = []
            for m in list(rx.finditer(original))[:3]:
                s, e = m.start(), m.end()
                line_no = original.count("\n", 0, s) + 1
                ctx_before = original.rfind("\n", 0, s) + 1
                ctx_after  = original.find("\n", e)
                ctx_after  = ctx_after if ctx_after != -1 else len(original)
                preview.append({
                    "line":    line_no,
                    "before":  original[ctx_before:ctx_after],
                    "after":   original[ctx_before:s] + replace + original[e:ctx_after],
                })
            return {"path": str(p), "ok": True, "dry_run": True,
                    "replacements": n, "preview": preview}

        if n == 0:
            return {"path": str(p), "ok": True, "replacements": 0, "changed": False}

        p.write_text(new_text, encoding="utf-8")
        sid = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sid, category="ide.code_edit",
            text=f"[replace] {p.name}: {n} × '{find[:60]}' → '{replace[:60]}'",
            full_text=(f"Path: {p}\nAgent: {agent}\nFind: {find}\n"
                       f"Replace: {replace}\nCount: {n}\nRegex: {is_regex}"),
            tags=["ide", "code", "replace"] + ([agent] if agent else []),
            importance=0.6, source_type="ai" if agent else "tool",
            record_type="observation",
            capability_name="ide.code.replace", broadcast_type="ide.code_edited",
            fabric_dataset="ide.code_edits",
            metadata={"path": str(p), "replacements": n, "agent": agent,
                      "is_regex": is_regex},
            fabric_data={"path": str(p), "find": find[:500],
                         "replace": replace[:500], "count": n, "agent": agent},
        ))
        return {"path": str(p), "ok": True, "replacements": n, "changed": True,
                "bytes": len(new_text.encode("utf-8", errors="replace"))}
    except Exception as ex:
        return {"error": str(ex), "ok": False}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.list_files  — recursive listing, gitignore-aware
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.code.list_files",
    http_method="POST", http_path="/ide/code/list_files", http_tags=["ide", "code"],
    memory="off", silent=True,
    description="Recursively list files under root, skipping .git, node_modules, etc. "
                "Input: root (str!), include (globs), exclude (globs), max_files (int). "
                "Output: {root, count, files: [{rel, path, size}]}.",
)
async def ide_code_list_files(
    root:      str,
    include:   str = "",
    exclude:   str = "",
    max_files: int = 2000,
    trace_id=None,
):
    try:
        root_p = Path(root).resolve()
        if not root_p.exists() or not root_p.is_dir():
            return {"error": f"Not a directory: {root}", "files": []}

        def _split(x):
            if not x: return []
            if isinstance(x, list): return [str(g) for g in x]
            x = str(x).strip()
            if x.startswith("["):
                try: return list(json.loads(x))
                except Exception: pass
            return [s.strip() for s in x.split(",") if s.strip()]

        out = []
        for p in _walk_files(root_p, _split(include), _split(exclude), max_files=max_files):
            try:
                st = p.stat()
                out.append({
                    "rel":   str(p.relative_to(root_p)),
                    "path":  str(p),
                    "size":  st.st_size,
                    "mtime": st.st_mtime,
                })
            except Exception:
                pass
        return {"root": str(root_p), "count": len(out), "files": out}
    except Exception as ex:
        return {"error": str(ex), "files": []}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.outline  — extract symbols from a source file (language-aware regex)
# ─────────────────────────────────────────────────────────────────────────────

# (func|class|def) regex per language — best-effort, no AST dependency.
_OUTLINE_PATTERNS = {
    "python": [
        ("class",    r"^\s*class\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    ],
    "javascript": [
        ("class",    r"^\s*class\s+([A-Za-z_$][\w$]*)"),
        ("function", r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
        ("arrow",    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
    ],
    "typescript": [
        ("interface", r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"),
        ("class",     r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"),
        ("function",  r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
    ],
    "rust": [
        ("struct",   r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)"),
        ("enum",     r"^\s*(?:pub\s+)?enum\s+([A-Za-z_]\w*)"),
        ("fn",       r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
        ("trait",    r"^\s*(?:pub\s+)?trait\s+([A-Za-z_]\w*)"),
    ],
    "go": [
        ("func",     r"^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w*)"),
        ("type",     r"^\s*type\s+([A-Za-z_]\w*)"),
    ],
}

_EXT_TO_LANG = {
    ".py":   "python",
    ".js":   "javascript",
    ".mjs":  "javascript",
    ".cjs":  "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".rs":   "rust",
    ".go":   "go",
}


@capability(
    "ide.code.outline",
    http_method="POST", http_path="/ide/code/outline", http_tags=["ide", "code"],
    memory="off", silent=True,
    description="Extract a symbol outline (functions, classes, etc) from a source file. "
                "Input: path (str!), root (str). "
                "Output: {path, language, symbols: [{kind, name, line}]}.",
)
async def ide_code_outline(path: str, root: str = "", trace_id=None):
    try:
        p = _safe_path(path, root)
        if not p.exists() or not p.is_file():
            return {"error": f"File not found: {path}", "symbols": []}
        lang = _EXT_TO_LANG.get(p.suffix.lower())
        if not lang:
            return {"path": str(p), "language": "unknown", "symbols": []}
        pats = [(k, re.compile(rx)) for k, rx in _OUTLINE_PATTERNS[lang]]
        symbols = []
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for kind, rx in pats:
                m = rx.match(line)
                if m:
                    symbols.append({"kind": kind, "name": m.group(1), "line": i})
                    break
        return {"path": str(p), "language": lang, "symbols": symbols}
    except Exception as ex:
        return {"error": str(ex), "symbols": []}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.whitelist  — the set of capabilities the LLM agent is allowed to call
# via ide.code.tool_dispatch. Admins can add/remove entries at runtime.
# ─────────────────────────────────────────────────────────────────────────────

# Core coding tools — always allowed.
_CODE_CORE = [
    "ide.fs.read",
    "ide.fs.write",
    "ide.fs.exists",
    "ide.fs.list",
    "ide.fs.delete",
    "ide.code.read_lines",
    "ide.code.edit_lines",
    "ide.code.insert_at",
    "ide.code.grep",
    "ide.code.replace",
    "ide.code.list_files",
    "ide.code.outline",
    "ide.git.status",
    "ide.git.diff",
    "ide.git.log",
]

# Extra whitelist (mutable at runtime) — lets admins grant arbitrary capabilities
# to the coding agent, e.g. "research.search" or "web.fetch".
_WHITELIST_FILE = Path(__file__).parent / ".vera_ide_whitelist.json"

def _load_whitelist() -> List[str]:
    try:
        if _WHITELIST_FILE.exists():
            data = json.loads(_WHITELIST_FILE.read_text())
            return list(data.get("extra", []))
    except Exception as e:
        log.warning("load whitelist: %s", e)
    return []

def _save_whitelist(extra: List[str]):
    try:
        _WHITELIST_FILE.write_text(json.dumps({"extra": list(set(extra))}, indent=2))
    except Exception as e:
        log.warning("save whitelist: %s", e)

# Runtime cache
_WHITELIST_EXTRA: List[str] = _load_whitelist()


def _allowed_capabilities() -> List[str]:
    return sorted(set(_CODE_CORE + _WHITELIST_EXTRA))


@capability(
    "ide.code.whitelist",
    http_method="GET", http_path="/ide/code/whitelist", http_tags=["ide", "code"],
    memory="off", silent=True,
    description="List the capabilities the coding agent is allowed to call "
                "via ide.code.tool_dispatch. Output: {core, extra, all}.",
)
async def ide_code_whitelist_get(trace_id=None):
    return {
        "core":  list(_CODE_CORE),
        "extra": list(_WHITELIST_EXTRA),
        "all":   _allowed_capabilities(),
    }


@capability(
    "ide.code.whitelist_update",
    http_method="POST", http_path="/ide/code/whitelist", http_tags=["ide", "code"],
    memory="off",
    description="Update the 'extra' agent-tool whitelist. "
                "Input: add (list of cap names), remove (list of cap names), "
                "replace (list — when given, replaces 'extra' entirely). "
                "Output: {core, extra, all}.",
)
async def ide_code_whitelist_update(
    add:     str = "",
    remove:  str = "",
    replace: str = "",
    trace_id=None,
):
    global _WHITELIST_EXTRA

    def _split(x):
        if not x: return []
        if isinstance(x, list): return [str(s).strip() for s in x if s]
        x = str(x).strip()
        if x.startswith("["):
            try: return [str(s).strip() for s in json.loads(x) if s]
            except Exception: pass
        return [s.strip() for s in x.split(",") if s.strip()]

    replace_list = _split(replace)
    if replace_list:
        _WHITELIST_EXTRA = [c for c in replace_list if c not in _CODE_CORE and c in CAPABILITY_REGISTRY]
    else:
        for c in _split(add):
            if c in _CODE_CORE or c in _WHITELIST_EXTRA:
                continue
            if c not in CAPABILITY_REGISTRY:
                log.warning("whitelist_update: unknown capability %s", c)
                continue
            _WHITELIST_EXTRA.append(c)
        for c in _split(remove):
            if c in _WHITELIST_EXTRA:
                _WHITELIST_EXTRA.remove(c)
    _save_whitelist(_WHITELIST_EXTRA)
    return {
        "core":  list(_CODE_CORE),
        "extra": list(_WHITELIST_EXTRA),
        "all":   _allowed_capabilities(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.tool_manifest  — JSON schema manifest for LLM prompts
# ─────────────────────────────────────────────────────────────────────────────

# Curated, prompt-friendly tool schemas. We don't expose every raw capability —
# we expose a stable set of coding tools with concise descriptions and arg hints.
_TOOL_MANIFEST = [
    {
        "name":        "grep",
        "capability":  "ide.code.grep",
        "description": "Search for text across files in the project. Returns matching lines with locations.",
        "args": {
            "pattern":        {"type": "string", "required": True,  "desc": "Text or regex to search for"},
            "is_regex":       {"type": "bool",   "required": False, "desc": "Treat pattern as regex (default false)"},
            "case_sensitive": {"type": "bool",   "required": False, "desc": "Default true"},
            "include":        {"type": "string", "required": False, "desc": "Comma-separated globs, e.g. '*.py,*.js'"},
            "exclude":        {"type": "string", "required": False, "desc": "Comma-separated globs to skip"},
            "max_results":    {"type": "int",    "required": False, "desc": "Cap results (default 200)"},
        },
    },
    {
        "name":        "list_files",
        "capability":  "ide.code.list_files",
        "description": "List files in the project, skipping .git/node_modules/etc.",
        "args": {
            "include": {"type": "string", "required": False, "desc": "Globs to include"},
            "exclude": {"type": "string", "required": False, "desc": "Globs to exclude"},
        },
    },
    {
        "name":        "read_file",
        "capability":  "ide.fs.read",
        "description": "Read the entire contents of a file.",
        "args": {
            "path": {"type": "string", "required": True, "desc": "File path (absolute or relative to project root)"},
        },
    },
    {
        "name":        "read_lines",
        "capability":  "ide.code.read_lines",
        "description": "Read a specific line range from a file (1-indexed, inclusive).",
        "args": {
            "path":  {"type": "string", "required": True},
            "start": {"type": "int",    "required": False, "desc": "Default 1"},
            "end":   {"type": "int",    "required": False, "desc": "Default last line"},
        },
    },
    {
        "name":        "outline",
        "capability":  "ide.code.outline",
        "description": "List functions, classes and other symbols in a source file.",
        "args": {
            "path": {"type": "string", "required": True},
        },
    },
    {
        "name":        "write_file",
        "capability":  "ide.fs.write",
        "description": "Write (create or overwrite) a file with the given content.",
        "args": {
            "path":    {"type": "string", "required": True},
            "content": {"type": "string", "required": True},
        },
    },
    {
        "name":        "edit_lines",
        "capability":  "ide.code.edit_lines",
        "description": "Replace a range of lines in a file with new content.",
        "args": {
            "path":        {"type": "string", "required": True},
            "start":       {"type": "int",    "required": True},
            "end":         {"type": "int",    "required": True},
            "new_content": {"type": "string", "required": True},
        },
    },
    {
        "name":        "insert_at",
        "capability":  "ide.code.insert_at",
        "description": "Insert content before a given line (0=prepend, -1=append).",
        "args": {
            "path":    {"type": "string", "required": True},
            "line":    {"type": "int",    "required": True},
            "content": {"type": "string", "required": True},
        },
    },
    {
        "name":        "replace",
        "capability":  "ide.code.replace",
        "description": "Find/replace text in a file (literal or regex).",
        "args": {
            "path":       {"type": "string", "required": True},
            "find":       {"type": "string", "required": True},
            "replace":    {"type": "string", "required": False},
            "is_regex":   {"type": "bool",   "required": False},
            "first_only": {"type": "bool",   "required": False},
        },
    },
    {
        "name":        "delete_file",
        "capability":  "ide.fs.delete",
        "description": "Delete a file from the filesystem.",
        "args": {
            "path": {"type": "string", "required": True},
        },
    },
    {
        "name":        "git_status",
        "capability":  "ide.git.status",
        "description": "Show git status for the project.",
        "args": {
            "path": {"type": "string", "required": True, "desc": "Project root"},
        },
    },
    {
        "name":        "git_diff",
        "capability":  "ide.git.diff",
        "description": "Show git diff for the project or a file.",
        "args": {
            "path": {"type": "string", "required": True},
        },
    },
]


def _manifest_for_prompt() -> str:
    """Format manifest as a compact JSON block an LLM can read."""
    lines = []
    for t in _TOOL_MANIFEST:
        args = ", ".join(
            f'{k}{"" if v.get("required") else "?"}:{v["type"]}'
            for k, v in t["args"].items()
        )
        lines.append(f'- {t["name"]}({args}) — {t["description"]}')
    return "\n".join(lines)


@capability(
    "ide.code.tool_manifest",
    http_method="GET", http_path="/ide/code/tool_manifest", http_tags=["ide", "code"],
    memory="off", silent=True,
    description="Return the JSON manifest of coding tools available to the agent, "
                "plus a compact text form suitable for LLM prompts. "
                "Output: {tools, whitelist, prompt_text}.",
)
async def ide_code_tool_manifest(trace_id=None):
    # Include both curated tools AND any extra whitelisted raw capabilities.
    extras = []
    for name in _WHITELIST_EXTRA:
        cap = CAPABILITY_REGISTRY.get(name, {})
        extras.append({
            "name":        name.replace(".", "_"),
            "capability":  name,
            "description": (cap.get("description") or "")[:240],
            "args":        {},   # raw — agent must read description
        })
    return {
        "tools":       _TOOL_MANIFEST + extras,
        "whitelist":   _allowed_capabilities(),
        "prompt_text": _manifest_for_prompt(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.tool_dispatch  — the meta-capability the agent calls
# ─────────────────────────────────────────────────────────────────────────────

# Map manifest short-names to real capability names
_TOOL_NAME_MAP = {t["name"]: t["capability"] for t in _TOOL_MANIFEST}


@capability(
    "ide.code.tool_dispatch",
    http_method="POST", http_path="/ide/code/tool_dispatch", http_tags=["ide", "code"],
    memory="off",
    description="Dispatch a named coding tool call on behalf of an agent. "
                "Enforces the ide.code whitelist. Every call is recorded to memory. "
                "Input: tool (str! — short name or full capability name), "
                "args (JSON object), agent (str), session_id (str). "
                "Output: {tool, capability, ok, result, elapsed_ms, error}.",
)
async def ide_code_tool_dispatch(
    tool:       str,
    args:       str = "{}",
    agent:      str = "",
    session_id: str = "",
    trace_id=None,
):
    t0 = asyncio.get_event_loop().time()
    sid = session_id or _ide_get_session_id()

    # Resolve short name → capability name
    cap_name = _TOOL_NAME_MAP.get(tool, tool)

    # Enforce whitelist
    if cap_name not in _allowed_capabilities():
        err = f"Tool '{tool}' → '{cap_name}' is not whitelisted for the coding agent"
        log.warning("tool_dispatch denied: %s (agent=%s)", cap_name, agent)
        asyncio.ensure_future(_record(
            session_id=sid, category="ide.tool_denied",
            text=f"[denied] {tool} → {cap_name}",
            full_text=f"Agent: {agent}\nTool: {tool}\nResolved: {cap_name}\nReason: not whitelisted",
            tags=["ide", "agent", "denied"] + ([agent] if agent else []),
            importance=0.7, source_type="system", record_type="event",
            capability_name="ide.code.tool_dispatch",
            broadcast_type="ide.tool_denied",
            fabric_dataset="ide.tool_calls",
            metadata={"tool": tool, "capability": cap_name, "agent": agent},
            fabric_data={"tool": tool, "capability": cap_name, "agent": agent,
                         "denied": True, "ts": now_iso()},
        ))
        return {"tool": tool, "capability": cap_name, "ok": False,
                "error": err, "result": None, "elapsed_ms": 0}

    # Check it actually exists
    entry = CAPABILITY_REGISTRY.get(cap_name)
    if not entry:
        err = f"Capability not registered: {cap_name}"
        return {"tool": tool, "capability": cap_name, "ok": False,
                "error": err, "result": None, "elapsed_ms": 0}

    # Parse args
    if isinstance(args, dict):
        kwargs = dict(args)
    else:
        try:
            kwargs = json.loads(args) if args else {}
        except Exception as e:
            return {"tool": tool, "capability": cap_name, "ok": False,
                    "error": f"Bad args JSON: {e}", "result": None, "elapsed_ms": 0}

    # Always thread agent/session_id through if the callee accepts them
    kwargs.setdefault("agent", agent)
    kwargs.setdefault("session_id", sid)

    # Broadcast call-start
    await emit_event({
        "type":       "ide.tool_call",
        "tool":       tool,
        "capability": cap_name,
        "agent":      agent,
        "session_id": sid,
        "args":       {k: (str(v)[:200] if not isinstance(v, (int, float, bool))
                           else v) for k, v in kwargs.items() if k != "session_id"},
        "ts":         now_iso(),
    })

    # Invoke
    func = entry["func"]
    try:
        # capability wrap functions accept **kwargs — but some positional-only
        # caps may reject unknown keys. Be defensive: drop keys the raw func
        # doesn't accept.
        raw = entry.get("raw", func)
        import inspect
        try:
            sig = inspect.signature(raw)
            ok_keys = set(sig.parameters.keys())
            filtered = {k: v for k, v in kwargs.items() if k in ok_keys}
            # Preserve agent/session_id if possible, else drop silently
        except (TypeError, ValueError):
            filtered = kwargs

        result = await func(**filtered)
        elapsed = int((asyncio.get_event_loop().time() - t0) * 1000)
        is_error = isinstance(result, dict) and (
            result.get("error") or result.get("ok") is False
        )

        # Record the tool call
        asyncio.ensure_future(_record(
            session_id=sid, category="ide.tool_call",
            text=f"[{agent or 'agent'}] {tool}" + (
                f" → {str(result.get('path', ''))[:60]}"
                if isinstance(result, dict) and result.get("path") else ""),
            full_text=(f"Agent: {agent}\nTool: {tool} ({cap_name})\n"
                       f"Args: {json.dumps({k:v for k,v in kwargs.items() if k!='session_id'}, default=str)[:2000]}\n"
                       f"Result: {json.dumps(result, default=str)[:3000]}"),
            tags=["ide", "agent", "tool_call", tool] + ([agent] if agent else []) +
                 (["error"] if is_error else []),
            importance=0.55 if not is_error else 0.7,
            source_type="ai" if agent else "tool",
            record_type="observation",
            capability_name="ide.code.tool_dispatch",
            broadcast_type="ide.tool_result",
            fabric_dataset="ide.tool_calls",
            metadata={"tool": tool, "capability": cap_name,
                      "agent": agent, "elapsed_ms": elapsed,
                      "error": bool(is_error)},
            fabric_data={"tool": tool, "capability": cap_name, "agent": agent,
                         "args": {k: str(v)[:500] for k, v in kwargs.items()
                                  if k != "session_id"},
                         "result_preview": json.dumps(result, default=str)[:3000],
                         "elapsed_ms": elapsed, "error": bool(is_error)},
        ))

        return {
            "tool":       tool,
            "capability": cap_name,
            "ok":         not is_error,
            "result":     result,
            "elapsed_ms": elapsed,
            "error":      result.get("error") if is_error else None,
        }
    except Exception as e:
        elapsed = int((asyncio.get_event_loop().time() - t0) * 1000)
        log.exception("tool_dispatch %s failed", cap_name)
        asyncio.ensure_future(_record(
            session_id=sid, category="ide.tool_error",
            text=f"[{agent or 'agent'}] {tool}: {str(e)[:120]}",
            full_text=f"Agent: {agent}\nTool: {tool}\nException: {e}",
            tags=["ide", "agent", "tool_error", tool] + ([agent] if agent else []),
            importance=0.8, source_type="system", record_type="event",
            capability_name="ide.code.tool_dispatch",
            broadcast_type="ide.tool_error",
            fabric_dataset="ide.tool_calls",
            metadata={"tool": tool, "capability": cap_name, "agent": agent,
                      "elapsed_ms": elapsed},
            fabric_data={"tool": tool, "capability": cap_name, "agent": agent,
                         "error": str(e), "elapsed_ms": elapsed},
        ))
        return {"tool": tool, "capability": cap_name, "ok": False,
                "error": str(e), "result": None, "elapsed_ms": elapsed}


# ─────────────────────────────────────────────────────────────────────────────
# ide.code.registry_search — lets the UI show the list of capabilities you
# can add to the 'extra' whitelist (filtered by tag / name).
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.code.registry_search",
    http_method="GET", http_path="/ide/code/registry_search", http_tags=["ide", "code"],
    memory="off", silent=True,
    description="Search the full capability registry, for picking extra agent tools. "
                "Input: query (str), tag (str), limit (int). "
                "Output: {capabilities: [{name, description, tags, in_whitelist}]}.",
)
async def ide_code_registry_search(
    query: str = "",
    tag:   str = "",
    limit: int = 100,
    trace_id=None,
):
    q = (query or "").lower()
    t = (tag   or "").lower()
    wl = set(_allowed_capabilities())
    out = []
    for name, info in CAPABILITY_REGISTRY.items():
        tags = [s.lower() for s in info.get("tags", [])]
        desc = (info.get("description") or "").strip()
        if q and q not in name.lower() and q not in desc.lower():
            continue
        if t and t not in tags:
            continue
        out.append({
            "name":          name,
            "description":   desc[:200],
            "tags":          info.get("tags", []),
            "http_method":   info.get("http_method"),
            "http_path":     info.get("http_path"),
            "in_whitelist":  name in wl,
            "is_core":       name in _CODE_CORE,
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda x: x["name"])
    return {"capabilities": out, "total": len(out)}


log.info("ide_code_capabilities loaded: %d tools, %d whitelist extras",
         len(_TOOL_MANIFEST), len(_WHITELIST_EXTRA))