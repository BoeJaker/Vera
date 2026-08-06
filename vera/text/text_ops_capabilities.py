"""
text_ops_capabilities.py — FILE-NATIVE text operations (the grep/sed/awk/jq layer)
================================================================================

Why this module exists
──────────────────────
Vera already had `text.*` caps — extract_urls, find_replace, stats, split_chunks
— and agents almost never used them. Observed live: a step with a 61 KB research
JSON on disk wrote a bespoke Python script to pull URLs out of it, when
`text.extract_urls` existed the whole time.

That was NOT (only) prompt bias. Look at the old signatures: every one takes
`text: str`. To use `text.extract_urls` on a file, an agent must first read the
whole file INTO ITS PROMPT, pass it back down as an argument, and receive the
result — for a 61 KB file that is two full copies through a context window, and
for anything bigger it simply cannot be done. `text.find_replace` is worse: it
returns the modified text as a string, so "replace in a file" costs read → cap →
write with the entire file crossing the model twice.

Given those signatures, writing a Python script was the RATIONAL choice. So the
fix is not to nag the model — it is to give it operations that:

  • take a PATH, not a blob of text;
  • run WHERE THE FILE IS (the session sandbox), so the bytes never enter Vera's
    process or the model's context;
  • return bounded results — matches, counts, a preview — never the whole file;
  • write their output to a file when asked (`save_as`), so a pipeline stays on
    disk instead of being relayed through the prompt;
  • are DETERMINISTIC: no LLM anywhere in this module. A cap call that extracts
    URLs is a short-circuit around inference entirely, which is the point.

Implementation note
───────────────────
The operations are implemented as small stdlib-only Python programs executed via
`exec.python.run`, which already routes into the session's sandbox. Python
rather than literal sed/awk/jq because the behaviour must be identical
everywhere — GNU vs BusyBox sed differ on `-i`, on `\\|` alternation and on
character classes, and jq may not be installed at all. The AGENT still sees a
simple, named, deterministic operation; portability is our problem, not its.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability

log = logging.getLogger("vera.text_ops")

# Bounds. These exist so a cap can never blow a context window: an agent that
# greps a huge log gets the first N hits plus an honest `truncated` flag, not
# 40 000 lines it must then summarise.
_MAX_MATCHES = 200
_MAX_ITEMS = 1000
_PREVIEW_CHARS = 600


# ── the runner ───────────────────────────────────────────────────────────────

async def _run_py(code: str, session_id: str = "", timeout: int = 120) -> Dict[str, Any]:
    """Run a stdlib-only program next to the file and parse its JSON result.

    Goes through exec.python.run so it inherits sandbox routing, the write
    confinement and the artifact paths — a path the agent can see is a path this
    resolves identically."""
    cap = CAPABILITY_REGISTRY.get("exec.python.run")
    fn = (cap or {}).get("raw") or (cap or {}).get("func")
    if not fn:
        return {"ok": False, "error": "exec.python.run is unavailable"}
    try:
        res = await fn(code=code, session_id=session_id, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"text op failed to run: {e}"}
    if not isinstance(res, dict):
        return {"ok": False, "error": "unexpected exec result"}
    out = str(res.get("stdout") or "")
    # The program prints ONE json object on its last non-empty line, so incidental
    # output (a warning from the interpreter) can't corrupt the parse.
    for line in reversed([l for l in out.splitlines() if l.strip()]):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    err = str(res.get("stderr") or "").strip()
    return {"ok": False,
            "error": err[-800:] or "the operation produced no result",
            "stdout": out[-400:]}


def _pyq(value: Any) -> str:
    """Embed a value into the generated program as a literal, safely."""
    return json.dumps(value)


# Shared prelude: resolve the path, refuse politely when it's missing, and give
# every op the same read helper (binary-safe, encoding-tolerant).
_PRELUDE = """
import json, os, re, sys, io
def _read(p):
    if not os.path.exists(p):
        print(json.dumps({"ok": False, "error": "file not found: " + p}))
        raise SystemExit(0)
    with io.open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()
"""


# ── search ───────────────────────────────────────────────────────────────────

@capability(
    "text.grep",
    http_method="POST", http_path="/text/grep", http_tags=["text"],
    memory="off",
    description="SEARCH a file for lines matching a pattern — the grep you would "
                "otherwise write a script for. Runs against the file on disk, so "
                "the file is never loaded into your context. Inputs: path (str! — "
                "the file to search), pattern (str! — regex by default), regex "
                "(bool, default true — false for a literal substring), ignore_case "
                "(bool), invert (bool — lines NOT matching), context (int — lines "
                "of surrounding context), max_matches (int, default 200), "
                "count_only (bool — just the number), session_id (str). "
                "Output: {ok, count, truncated, matches:[{line, text}]}.",
)
async def cap_text_grep(path: str = "", pattern: str = "", regex: bool = True,
                        ignore_case: bool = False, invert: bool = False,
                        context: int = 0, max_matches: int = _MAX_MATCHES,
                        count_only: bool = False, session_id: str = "",
                        trace_id=None) -> Dict[str, Any]:
    if not path or not pattern:
        return {"ok": False, "error": "path and pattern are required"}
    code = _PRELUDE + f"""
txt = _read({_pyq(path)}).splitlines()
pat = {_pyq(pattern)}
flags = re.IGNORECASE if {bool(ignore_case)} else 0
if {bool(regex)}:
    try:
        rx = re.compile(pat, flags)
    except re.error as e:
        print(json.dumps({{"ok": False, "error": "bad regex: " + str(e)}})); raise SystemExit(0)
    test = lambda s: bool(rx.search(s))
else:
    needle = pat.lower() if {bool(ignore_case)} else pat
    test = lambda s: needle in (s.lower() if {bool(ignore_case)} else s)
hits = []
count = 0
ctx = max(0, {int(context)})
for i, line in enumerate(txt):
    if test(line) != {bool(invert)}:
        count += 1
        if not {bool(count_only)} and len(hits) < {int(max_matches)}:
            if ctx:
                lo, hi = max(0, i - ctx), min(len(txt), i + ctx + 1)
                hits.append({{"line": i + 1,
                              "text": "\\n".join(txt[lo:hi])[:2000]}})
            else:
                hits.append({{"line": i + 1, "text": line[:2000]}})
print(json.dumps({{"ok": True, "count": count, "matches": hits,
                   "truncated": count > len(hits) and not {bool(count_only)},
                   "lines_scanned": len(txt)}}))
"""
    return await _run_py(code, session_id)


# ── replace ──────────────────────────────────────────────────────────────────

@capability(
    "text.replace",
    http_method="POST", http_path="/text/replace", http_tags=["text"],
    memory="off",
    description="FIND AND REPLACE inside a file, in place — the sed -i you would "
                "otherwise write a script for. The file is edited on disk; nothing "
                "is relayed through your context. Inputs: path (str!), find (str!), "
                "replace (str), regex (bool, default false — true to use a regex, "
                "with \\\\1 backreferences in `replace`), ignore_case (bool), count "
                "(int — max replacements, 0 = all), dry_run (bool — report what "
                "WOULD change without writing), session_id (str). "
                "Output: {ok, replacements, changed, preview}.",
)
async def cap_text_replace(path: str = "", find: str = "", replace: str = "",
                           regex: bool = False, ignore_case: bool = False,
                           count: int = 0, dry_run: bool = False,
                           session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not path or not find:
        return {"ok": False, "error": "path and find are required"}
    code = _PRELUDE + f"""
p = {_pyq(path)}
src = _read(p)
find, repl = {_pyq(find)}, {_pyq(replace)}
flags = re.IGNORECASE if {bool(ignore_case)} else 0
n_max = {int(count)} if {int(count)} > 0 else 0
if {bool(regex)}:
    try:
        rx = re.compile(find, flags)
    except re.error as e:
        print(json.dumps({{"ok": False, "error": "bad regex: " + str(e)}})); raise SystemExit(0)
    out, n = rx.subn(repl, src, count=n_max)
else:
    if {bool(ignore_case)}:
        # Literal find, case-insensitively: escape the needle so it stays
        # literal, and pass a FUNCTION as the replacement so backslashes in
        # `repl` are never read as regex backreferences.
        rx = re.compile(re.escape(find), flags)
        out, n = rx.subn(lambda m: repl, src, count=n_max)
    else:
        found = src.count(find)
        n = found if n_max == 0 else min(found, n_max)
        out = src.replace(find, repl) if n_max == 0 else src.replace(find, repl, n_max)
changed = out != src
if changed and not {bool(dry_run)}:
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(out)
# A short window around the FIRST change, so the caller can eyeball the edit
# without the file being returned.
prev = ""
if changed:
    for i, (a, b) in enumerate(zip(src.splitlines(), out.splitlines())):
        if a != b:
            prev = "- " + a[:300] + chr(10) + "+ " + b[:300]
            break
print(json.dumps({{"ok": True, "replacements": n, "changed": changed,
                   "written": bool(changed and not {bool(dry_run)}),
                   "dry_run": {bool(dry_run)}, "preview": prev[:{_PREVIEW_CHARS}]}}))
"""
    return await _run_py(code, session_id)


# ── extract ──────────────────────────────────────────────────────────────────

# Named extractors — the "well-known patterns" that should never need a model to
# think about them. `kind` short-circuits inference completely.
_EXTRACTORS = {
    "urls":    r"https?://[^\s<>\"'{}|\\^`\[\]]+",
    "emails":  r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "ipv4":    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "numbers": r"-?\d+(?:\.\d+)?",
    "dates":   r"\b\d{4}-\d{2}-\d{2}\b",
    "domains": r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b",
    "hashtags": r"#[A-Za-z0-9_]+",
    "paths":   r"(?:\./|/)[\w./-]+",
}


@capability(
    "text.extract",
    http_method="POST", http_path="/text/extract", http_tags=["text"],
    memory="off",
    description="EXTRACT every occurrence of a well-known pattern from a file — "
                "urls, emails, ipv4, numbers, dates, domains, hashtags, paths — or "
                "your own regex. This is the deterministic answer to 'pull the list "
                "of X out of this file'; do NOT write a script for it. Inputs: path "
                "(str!), kind (str — one of urls|emails|ipv4|numbers|dates|domains|"
                "hashtags|paths, or 'regex'), pattern (str — required when "
                "kind='regex'), group (int — capture group to take, default 0), "
                "unique (bool, default true), sort (bool), limit (int, default 1000), "
                "save_as (str — write the list to this file instead of returning it "
                "all, one per line or .json for an array), session_id (str). "
                "Output: {ok, count, items, truncated, saved_to}.",
)
async def cap_text_extract(path: str = "", kind: str = "urls", pattern: str = "",
                           group: int = 0, unique: bool = True, sort: bool = False,
                           limit: int = _MAX_ITEMS, save_as: str = "",
                           session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not path:
        return {"ok": False, "error": "path is required"}
    kind = (kind or "urls").strip().lower()
    if kind == "regex":
        if not pattern:
            return {"ok": False, "error": "pattern is required when kind='regex'"}
        rx = pattern
    elif kind in _EXTRACTORS:
        rx = _EXTRACTORS[kind]
    else:
        return {"ok": False,
                "error": f"unknown kind '{kind}' — use one of "
                         f"{', '.join(sorted(_EXTRACTORS))} or 'regex' with a pattern"}
    code = _PRELUDE + f"""
src = _read({_pyq(path)})
try:
    rx = re.compile({_pyq(rx)})
except re.error as e:
    print(json.dumps({{"ok": False, "error": "bad regex: " + str(e)}})); raise SystemExit(0)
g = {int(group)}
items = []
for m in rx.finditer(src):
    try:
        v = m.group(g)
    except Exception:
        v = m.group(0)
    if v:
        items.append(v)
total = len(items)
if {bool(unique)}:
    seen, uniq = set(), []
    for v in items:
        if v not in seen:
            seen.add(v); uniq.append(v)
    items = uniq
if {bool(sort)}:
    items = sorted(items)
saved = ""
dest = {_pyq(save_as)}
if dest:
    with io.open(dest, "w", encoding="utf-8") as f:
        if dest.endswith(".json"):
            json.dump(items, f, indent=2)
        else:
            f.write("\\n".join(items) + ("\\n" if items else ""))
    saved = dest
shown = items[:{int(limit)}]
print(json.dumps({{"ok": True, "count": len(items), "total_matches": total,
                   "items": shown, "truncated": len(items) > len(shown),
                   "saved_to": saved}}))
"""
    return await _run_py(code, session_id)


# ── json query ───────────────────────────────────────────────────────────────

@capability(
    "text.json",
    http_method="POST", http_path="/text/json", http_tags=["text", "data"],
    memory="off",
    description="QUERY a JSON file with a dotted path and optional filter — the jq "
                "you would otherwise write a script for. Inputs: path (str! — the "
                ".json file), query (str! — a dotted path into the document, e.g. "
                "'sources', 'sources[].url', 'data.items[].name'; '.' for the root), "
                "where (str — optional filter on the selected records, "
                "'field=value', 'field!=value', 'field~substring', 'field>n'), "
                "fields (str — comma-separated keys to keep from each record), "
                "limit (int, default 1000), save_as (str — write the result to this "
                "file instead of returning it all), session_id (str). "
                "Output: {ok, count, result, truncated, saved_to}.",
)
async def cap_text_json(path: str = "", query: str = ".", where: str = "",
                        fields: str = "", limit: int = _MAX_ITEMS,
                        save_as: str = "", session_id: str = "",
                        trace_id=None) -> Dict[str, Any]:
    if not path:
        return {"ok": False, "error": "path is required"}
    code = _PRELUDE + f"""
raw = _read({_pyq(path)})
try:
    doc = json.loads(raw)
except Exception as e:
    print(json.dumps({{"ok": False, "error": "not valid JSON: " + str(e)}})); raise SystemExit(0)

def walk(node, q):
    q = (q or ".").strip()
    if q in (".", "", "$"):
        return node
    cur = node
    for part in [p for p in q.replace("[]", ".[]").split(".") if p]:
        if part == "[]":
            if isinstance(cur, list):
                continue
            return None
        if isinstance(cur, list):
            out = []
            for it in cur:
                if isinstance(it, dict) and part in it:
                    out.append(it[part])
            cur = out
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur

sel = walk(doc, {_pyq(query)})
if sel is None:
    keys = list(doc)[:20] if isinstance(doc, dict) else "(top level is a list)"
    print(json.dumps({{"ok": False, "error": "query matched nothing",
                       "top_level_keys": keys}})); raise SystemExit(0)
recs = sel if isinstance(sel, list) else [sel]

w = {_pyq(where)}.strip()
if w:
    import operator
    m = re.match(r"^\\s*([\\w.]+)\\s*(!=|>=|<=|=|~|>|<)\\s*(.*)$", w)
    if m:
        key, op, val = m.group(1), m.group(2), m.group(3).strip()
        def keep(r):
            if not isinstance(r, dict) or key not in r:
                return False
            got = r[key]
            if op == "~":
                return val.lower() in str(got).lower()
            if op == "=":
                return str(got) == val
            if op == "!=":
                return str(got) != val
            try:
                a, b = float(got), float(val)
            except Exception:
                return False
            return {{">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}}[op]
        recs = [r for r in recs if keep(r)]

fl = [f.strip() for f in {_pyq(fields)}.split(",") if f.strip()]
if fl:
    recs = [{{k: r.get(k) for k in fl}} if isinstance(r, dict) else r for r in recs]

saved = ""
dest = {_pyq(save_as)}
if dest:
    with io.open(dest, "w", encoding="utf-8") as f:
        json.dump(recs, f, indent=2, default=str)
    saved = dest
shown = recs[:{int(limit)}]
print(json.dumps({{"ok": True, "count": len(recs), "result": shown,
                   "truncated": len(recs) > len(shown), "saved_to": saved}},
                 default=str))
"""
    return await _run_py(code, session_id)


# ── columns ──────────────────────────────────────────────────────────────────

@capability(
    "text.fields",
    http_method="POST", http_path="/text/fields", http_tags=["text", "data"],
    memory="off",
    description="Pull COLUMNS out of a delimited file — the awk/cut you would "
                "otherwise write a script for. Inputs: path (str!), fields (str! — "
                "1-based column numbers, e.g. '1,3' or '2-4'), delimiter (str — "
                "default any whitespace; use ',' for CSV, '\\t' for TSV), "
                "skip_header (bool), join (str — how to join the kept columns, "
                "default tab), limit (int, default 1000), save_as (str), "
                "session_id (str). Output: {ok, count, rows, truncated, saved_to}.",
)
async def cap_text_fields(path: str = "", fields: str = "1", delimiter: str = "",
                          skip_header: bool = False, join: str = "\t",
                          limit: int = _MAX_ITEMS, save_as: str = "",
                          session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not path:
        return {"ok": False, "error": "path is required"}
    cols: List[int] = []
    for part in str(fields or "1").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                cols.extend(range(int(a), int(b) + 1))
            except Exception:
                return {"ok": False, "error": f"bad field range: {part}"}
        else:
            try:
                cols.append(int(part))
            except Exception:
                return {"ok": False, "error": f"bad field number: {part}"}
    if not cols:
        return {"ok": False, "error": "fields is required, e.g. '1,3' or '2-4'"}
    code = _PRELUDE + f"""
lines = _read({_pyq(path)}).splitlines()
if {bool(skip_header)} and lines:
    lines = lines[1:]
delim = {_pyq(delimiter)}
cols = {_pyq([c - 1 for c in cols])}
rows = []
for ln in lines:
    if not ln.strip():
        continue
    parts = ln.split(delim) if delim else ln.split()
    rows.append([parts[c] if 0 <= c < len(parts) else "" for c in cols])
saved = ""
dest = {_pyq(save_as)}
if dest:
    with io.open(dest, "w", encoding="utf-8") as f:
        if dest.endswith(".json"):
            json.dump(rows, f, indent=2)
        else:
            f.write("\\n".join({_pyq(join)}.join(r) for r in rows) + ("\\n" if rows else ""))
    saved = dest
shown = rows[:{int(limit)}]
print(json.dumps({{"ok": True, "count": len(rows), "rows": shown,
                   "truncated": len(rows) > len(shown), "saved_to": saved}}))
"""
    return await _run_py(code, session_id)


# ── frequency / dedupe ───────────────────────────────────────────────────────

@capability(
    "text.uniq",
    http_method="POST", http_path="/text/uniq", http_tags=["text"],
    memory="off",
    description="DEDUPE or COUNT the distinct lines of a file — the sort|uniq -c "
                "you would otherwise write a script for. Inputs: path (str!), count "
                "(bool, default true — return occurrence counts, most frequent "
                "first), top (int — keep only the N most common), ignore_case "
                "(bool), strip (bool, default true), save_as (str), session_id "
                "(str). Output: {ok, distinct, total, items:[{value, count}], "
                "saved_to}.",
)
async def cap_text_uniq(path: str = "", count: bool = True, top: int = 0,
                        ignore_case: bool = False, strip: bool = True,
                        save_as: str = "", session_id: str = "",
                        trace_id=None) -> Dict[str, Any]:
    if not path:
        return {"ok": False, "error": "path is required"}
    code = _PRELUDE + f"""
from collections import Counter
lines = _read({_pyq(path)}).splitlines()
vals = []
for ln in lines:
    v = ln.strip() if {bool(strip)} else ln
    if not v:
        continue
    vals.append(v.lower() if {bool(ignore_case)} else v)
c = Counter(vals)
items = [{{"value": v, "count": n}} for v, n in c.most_common()]
if {int(top)} > 0:
    items = items[:{int(top)}]
saved = ""
dest = {_pyq(save_as)}
if dest:
    with io.open(dest, "w", encoding="utf-8") as f:
        if dest.endswith(".json"):
            json.dump(items, f, indent=2)
        else:
            f.write("\\n".join(
                (str(i["count"]) + "\\t" + i["value"]) if {bool(count)} else i["value"]
                for i in items) + ("\\n" if items else ""))
    saved = dest
print(json.dumps({{"ok": True, "distinct": len(c), "total": len(vals),
                   "items": items[:{_MAX_ITEMS}],
                   "truncated": len(items) > {_MAX_ITEMS}, "saved_to": saved}}))
"""
    return await _run_py(code, session_id)


# ── slice ────────────────────────────────────────────────────────────────────

@capability(
    "text.slice",
    http_method="POST", http_path="/text/slice", http_tags=["text"],
    memory="off",
    description="Read a LINE RANGE from a file — the head/tail/sed -n 'a,bp' you "
                "would otherwise write a script for, and the safe way to look at "
                "part of a file too big to read whole. Inputs: path (str!), start "
                "(int — 1-based first line, negative counts from the end e.g. -20 "
                "for the last 20), end (int — inclusive last line, 0 = to the end), "
                "max_lines (int, default 200), save_as (str), session_id (str). "
                "Output: {ok, total_lines, returned, text, saved_to}.",
)
async def cap_text_slice(path: str = "", start: int = 1, end: int = 0,
                         max_lines: int = 200, save_as: str = "",
                         session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not path:
        return {"ok": False, "error": "path is required"}
    code = _PRELUDE + f"""
lines = _read({_pyq(path)}).splitlines()
total = len(lines)
s, e = {int(start)}, {int(end)}
if s < 0:
    sel = lines[s:]
    first = max(1, total + s + 1)
else:
    s = max(1, s)
    stop = total if e <= 0 else min(total, e)
    sel = lines[s - 1:stop]
    first = s
sel = sel[:{int(max_lines)}]
saved = ""
dest = {_pyq(save_as)}
if dest:
    with io.open(dest, "w", encoding="utf-8") as f:
        f.write("\\n".join(sel) + ("\\n" if sel else ""))
    saved = dest
print(json.dumps({{"ok": True, "total_lines": total, "returned": len(sel),
                   "first_line": first, "text": "\\n".join(sel)[:60000],
                   "saved_to": saved}}))
"""
    return await _run_py(code, session_id)
