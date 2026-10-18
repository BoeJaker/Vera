"""Lint findings on a change's changed lines (looplab-control §9 K6, 18 Oct 2026). Pure.

Owner, 18 Oct: "need to be able to easily read diffs from the loop lab fully linted and nice qol for the code ui".
evolve.pipeline.lint reads a pipeline's diff (evolve.pipeline.diff), and this module says what is wrong ON THE LINES
THE CHANGE TOUCHED: line checks every file gets, a Python syntax check, and the findings of external tools (node,
ruff) that the capability runs when they are installed. Every finding names the check that made it, and a check that
could not run is reported as absent, never as clean.
"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_CONFLICT = re.compile(r"^(<{7}|={7}|>{7})( |$)")
CODE_EXT = (".py", ".js", ".cjs", ".mjs", ".ts", ".html", ".css", ".json", ".sh", ".md")
LONG_LINE = 160


def parse_unified(diff: str) -> List[Dict[str, Any]]:
    """A unified git diff -> [{path, old_path, new, deleted, binary, hunks: [{header, old_start, new_start,
    lines: [{t, text, old, new}]}], added: {new line no: text}}]."""
    files: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    hunk: Optional[Dict[str, Any]] = None
    o = n = 0
    for raw in (diff or "").splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", raw)
            cur = {"path": m.group(2) if m else raw[11:], "old_path": m.group(1) if m else "", "new": False,
                   "deleted": False, "binary": False, "hunks": [], "added": {}}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None or raw.startswith("@@"):
            if raw.startswith("new file mode"):
                cur["new"] = True
            elif raw.startswith("deleted file mode"):
                cur["deleted"] = True
            elif raw.startswith("Binary files"):
                cur["binary"] = True
            elif raw.startswith("+++ "):
                p = raw[4:]
                if p != "/dev/null":
                    cur["path"] = p[2:] if p.startswith("b/") else p
            m = _HUNK.match(raw)
            if m:
                o, n = int(m.group(1)), int(m.group(3))
                hunk = {"header": raw, "old_start": o, "new_start": n, "lines": []}
                cur["hunks"].append(hunk)
            continue
        if raw.startswith("\\"):                      # "\ No newline at end of file"
            continue
        t, text = (raw[:1] or " "), raw[1:]
        if t == "+":
            hunk["lines"].append({"t": "+", "text": text, "old": None, "new": n})
            cur["added"][n] = text
            n += 1
        elif t == "-":
            hunk["lines"].append({"t": "-", "text": text, "old": o, "new": None})
            o += 1
        else:
            hunk["lines"].append({"t": " ", "text": text, "old": o, "new": n})
            o += 1
            n += 1
    return files


def _finding(path: str, line: Optional[int], code: str, message: str, source: str, severity: str = "warning") -> Dict[str, Any]:
    return {"path": path, "line": line, "code": code, "message": message, "source": source, "severity": severity}


def line_checks(path: str, added: Dict[int, str]) -> List[Dict[str, Any]]:
    """Checks every changed line gets, whatever its language."""
    out: List[Dict[str, Any]] = []
    code = path.endswith(CODE_EXT)
    for ln in sorted(added):
        s = added[ln]
        if "\x00" in s:
            out.append(_finding(path, ln, "NUL", "a NUL byte in the line", "line-checks", "error"))
        if _CONFLICT.match(s):
            out.append(_finding(path, ln, "CONFLICT", "a merge conflict marker", "line-checks", "error"))
        if code and s != s.rstrip():
            out.append(_finding(path, ln, "W291", "trailing whitespace", "line-checks"))
        if path.endswith((".py", ".js", ".cjs", ".mjs")) and "\t" in s[:len(s) - len(s.lstrip())]:
            out.append(_finding(path, ln, "W191", "indented with a tab", "line-checks"))
        if code and not path.endswith((".md", ".json")) and len(s) > LONG_LINE:
            out.append(_finding(path, ln, "E501", "line is %d characters (over %d)" % (len(s), LONG_LINE), "line-checks", "info"))
        if path.endswith((".js", ".cjs", ".mjs", ".html")) and re.search(r"(^|[;{\s])debugger\s*;", s):
            out.append(_finding(path, ln, "DEBUGGER", "a debugger statement", "line-checks"))
    return out


def py_syntax(path: str, src: str) -> List[Dict[str, Any]]:
    """The file as the change leaves it must parse (a syntax error anywhere stops the module importing)."""
    try:
        ast.parse(src, filename=path)
        return []
    except SyntaxError as e:
        return [_finding(path, e.lineno, "E999", "does not parse: %s" % (e.msg or "syntax error"), "python-ast", "error")]


def inline_scripts(html: str) -> str:
    """An HTML file's inline <script> bodies joined the way the panel tests check them (node --check)."""
    return "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html or "", re.S))


def node_finding(path: str, stderr: str, joined: bool) -> List[Dict[str, Any]]:
    """`node --check -` output -> a finding. For an HTML file the line is in the joined scripts, not the file."""
    if not (stderr or "").strip():
        return []
    m = re.search(r"\[stdin\]:(\d+)", stderr)
    msg = next((l.strip() for l in stderr.splitlines() if "Error" in l), stderr.strip().splitlines()[-1])
    return [_finding(path, None if joined or not m else int(m.group(1)), "JS-PARSE",
                     ("inline scripts do not parse: " if joined else "does not parse: ") + msg[:200], "node", "error")]


def ruff_findings(path: str, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """ruff's JSON output -> findings."""
    out = []
    for r in rows or []:
        loc = r.get("location") or {}
        out.append(_finding(path, loc.get("row"), str(r.get("code") or "RUFF"), str(r.get("message") or ""), "ruff",
                            "error" if str(r.get("code") or "").startswith(("E9", "F8")) else "warning"))
    return out


def on_changed(findings: Iterable[Dict[str, Any]], added: Dict[int, str]) -> List[Dict[str, Any]]:
    """Keep what the change made: findings on a changed line, plus every error (a file that no longer parses is
    the change's problem wherever the parser stops)."""
    keep = set(added)
    return [f for f in findings if f.get("severity") == "error" or f.get("line") is None or f.get("line") in keep]


def summary(findings: List[Dict[str, Any]], sources: Dict[str, str]) -> Dict[str, Any]:
    by = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        by[f.get("severity", "warning")] = by.get(f.get("severity", "warning"), 0) + 1
    absent = sorted(k for k, v in sources.items() if v == "absent")
    return {"errors": by["error"], "warnings": by["warning"], "info": by["info"], "absent": absent,
            "clean": not findings and not absent}
