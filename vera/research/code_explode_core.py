# -*- coding: utf-8 -*-
"""
Code and pages as a STRUCTURED graph — the extractor behind `code.explode` (EXPLODE.md §7.2, track C).

Pure: no Vera imports, no I/O — sources come in as {path, text, lang?}, the Explode contract goes out:
files and classes as GROUPS (plates), modules / classes / functions / methods / elements / selectors as
CARDS each with the char SPAN it came from, and EDGES that always carry a RESOLUTION:

    exact       the target is a symbol in the parsed set, reached by name in the same file, by an import,
                or by `self.` inside its own class
    heuristic   matched by bare name across files (one candidate), or an attribute call matched by method name
    external    not in the parsed set — a grey stub card stands for it, so the edge is drawn, not invented

Three engines, and every card says which drew it (`by`):

    ast          Python, complete code — the stdlib parser (the estate's own language, exact spans)
    tree-sitter  Python and JavaScript when `tree_sitter_language_pack` is importable — it parses BROKEN and
                 PARTIAL code (an LLM snippet, a scraped fence), which `ast` cannot
    patterns     JavaScript / TypeScript, CSS and HTML through tolerant structural patterns — braces matched,
                 declarations found; good enough to draw a file, marked so nobody trusts it more than that

A source that failed to parse completely is a `partial` source: its cards still stand (whatever was
recovered), its assessment says so, and its unresolved calls go to external stubs rather than nowhere.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import os
import re
from typing import Dict, List, Optional, Tuple

_BUILTINS = set(dir(builtins)) | {"self", "cls", "super"}
try:
    import sys as _sys
    _STDLIB = set(getattr(_sys, "stdlib_module_names", ())) | {"__future__"}
except Exception:  # pragma: no cover
    _STDLIB = {"__future__"}
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "await", "async",
                "else", "do", "try", "throw", "yield", "delete", "void", "in", "of", "class", "super", "this",
                "import", "export", "const", "let", "var", "require", "console", "Math", "JSON", "Object", "Array",
                "String", "Number", "Promise", "Date", "Error", "Map", "Set", "parseInt", "parseFloat", "setTimeout",
                "clearTimeout", "setInterval", "fetch", "document", "window", "Boolean", "Symbol", "RegExp", "isNaN"}

try:  # the tolerant engine, when the package is there
    import tree_sitter_language_pack as _tslp  # type: ignore
    HAS_TREE_SITTER = True
except Exception:  # pragma: no cover - optional dependency
    _tslp = None
    HAS_TREE_SITTER = False


# ── languages and spans ───────────────────────────────────────────────────────────────────────────
_EXT = {".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".css": "css", ".html": "html", ".htm": "html"}


def detect_lang(path: str = "", text: str = "", lang: str = "") -> str:
    if lang:
        l = lang.lower()
        return {"py": "python", "js": "javascript", "ts": "typescript", "jsx": "javascript", "tsx": "typescript"}.get(l, l)
    ext = os.path.splitext(path or "")[1].lower()
    if ext in _EXT:
        return _EXT[ext]
    head = (text or "")[:400]
    if re.search(r"^\s*(def |class |import |from \S+ import )", head, re.M):
        return "python"
    if re.search(r"<!doctype html|<html|<div|<body", head, re.I):
        return "html"
    if re.search(r"^\s*[.#a-z][\w\-.#: ,>\[\]=\"']*\{", head, re.M | re.I) and "function" not in head:
        return "css"
    if re.search(r"\b(function|const|let|var|=>|import .* from)\b", head):
        return "javascript"
    return ""


class _Lines:
    """Line/column → char offset for a text, once."""

    def __init__(self, text: str):
        self.starts = [0]
        for m in re.finditer(r"\n", text):
            self.starts.append(m.end())
        self.n = len(text)

    def off(self, line: int, col: int) -> int:        # 1-based line, 0-based column
        i = max(0, min(len(self.starts) - 1, line - 1))
        return min(self.n, self.starts[i] + max(0, col))

    def line_of(self, off: int) -> int:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1


def _sid(path: str, qual: str) -> str:
    return "%s::%s" % (path, qual)


def module_names(path: str) -> List[str]:
    """The names a file can be imported by: 'vera/research/x.py' → vera.research.x, research.x, x, Vera.vera.research.x."""
    p = path.replace("\\", "/")
    p = re.sub(r"\.(py|pyi|js|mjs|cjs|ts|jsx|tsx|css|html|htm)$", "", p)
    if p.endswith("/__init__"):
        p = p[:-len("/__init__")]
    parts = [q for q in p.split("/") if q and q != "."]
    out = []
    for i in range(len(parts)):
        out.append(".".join(parts[i:]))
    if parts and parts[0] == "vera":
        out.append("Vera." + ".".join(parts))
    return out


# ── Python through ast ────────────────────────────────────────────────────────────────────────────
def _decorators(node, text: str, L: _Lines) -> List[str]:
    out = []
    for d in getattr(node, "decorator_list", []) or []:
        try:
            s, e = L.off(d.lineno, d.col_offset), L.off(d.end_lineno, d.end_col_offset)
            out.append("@" + text[s:e].split("\n")[0][:60])
        except Exception:
            pass
    return out


def _call_target(func) -> Optional[Tuple[str, str]]:
    """('name', '') for f(), ('attr', 'obj') for obj.attr(), ('attr', 'a.b') for a.b.attr()."""
    if isinstance(func, ast.Name):
        return (func.id, "")
    if isinstance(func, ast.Attribute):
        chain = []
        v = func
        while isinstance(v, ast.Attribute):
            chain.append(v.attr)
            v = v.value
        base = v.id if isinstance(v, ast.Name) else ("()" if isinstance(v, ast.Call) else "")
        chain.reverse()
        return (chain[-1], ".".join(([base] if base else []) + chain[:-1]))
    return None


def parse_python_ast(path: str, text: str) -> Dict:
    L = _Lines(text)
    out = {"engine": "ast", "path": path, "symbols": [], "imports": [], "errors": [], "aliases": {}}
    mod_id = _sid(path, "<module>")
    out["symbols"].append({"id": mod_id, "kind": "module", "name": os.path.basename(path), "qual": "<module>", "path": path,
                           "start": 0, "end": len(text), "line0": 1, "line1": L.line_of(max(0, len(text) - 1)), "parent": None,
                           "fields": [], "calls": [], "bases": [], "decorators": [], "async": False})
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        out["errors"].append("%s (line %s)" % (e.msg, e.lineno))
        return out            # the file still stands as a module card, marked partial; ast recovers nothing else
    aliases: Dict[str, str] = out["aliases"]   # local name → qualified ('mod.name' or 'mod')

    def add_import(node):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[(a.asname or a.name).split(".")[0]] = a.name
                out["imports"].append({"module": a.name, "name": "", "alias": a.asname or a.name, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for a in node.names:
                aliases[a.asname or a.name] = mod + "." + a.name
                out["imports"].append({"module": mod, "name": a.name, "alias": a.asname or a.name, "line": node.lineno})

    def span(node):
        # a decorated def or class begins at its first decorator — that is where the reader's eye starts it
        decos = getattr(node, "decorator_list", None) or []
        first = decos[0] if decos else node
        return L.off(first.lineno, first.col_offset - (1 if decos else 0)), L.off(node.end_lineno, node.end_col_offset)

    def calls_in(node, own_class: Optional[str]):
        found = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                t = _call_target(sub.func)
                if t:
                    found.append({"name": t[0], "via": t[1], "line": getattr(sub, "lineno", 0)})
        return found

    def visit(node, parent_id: Optional[str], qual: str, own_class: Optional[str]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                add_import(child)
            elif isinstance(child, ast.ClassDef):
                s, e = span(child); q = (qual + "." if qual else "") + child.name; cid = _sid(path, q)
                bases = []
                for b in child.bases:
                    try:
                        bs, be = span(b); bases.append(text[bs:be])
                    except Exception:
                        pass
                methods = [n for n in child.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                out["symbols"].append({"id": cid, "kind": "class", "name": child.name, "qual": q, "path": path, "start": s, "end": e,
                                       "line0": child.lineno, "line1": child.end_lineno, "parent": parent_id, "fields": [], "calls": [],
                                       "bases": bases, "decorators": _decorators(child, text, L), "async": False, "methods": len(methods)})
                visit(child, cid, q, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                s, e = span(child); q = (qual + "." if qual else "") + child.name; fid = _sid(path, q)
                args = [a.arg for a in child.args.args + child.args.kwonlyargs if a.arg not in ("self", "cls")]
                out["symbols"].append({"id": fid, "kind": "method" if own_class else "function", "name": child.name, "qual": q, "path": path,
                                       "start": s, "end": e, "line0": child.lineno, "line1": child.end_lineno, "parent": parent_id,
                                       "fields": [], "calls": calls_in(child, own_class), "bases": [], "decorators": _decorators(child, text, L),
                                       "async": isinstance(child, ast.AsyncFunctionDef), "args": args, "own_class": own_class})
                # nested defs are symbols too (closures, inner helpers)
                visit(child, fid, q, own_class)
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                visit(child, parent_id, qual, own_class)   # a def or an import under `if TYPE_CHECKING:` / `try:` — at any level
            elif isinstance(child, (ast.ExceptHandler,)):
                visit(child, parent_id, qual, own_class)

    visit(tree, mod_id, "", None)
    out["aliases"] = aliases
    return out


# ── Python and JavaScript through tree-sitter (tolerant) ─────────────────────────────────────────
def parse_tree_sitter(path: str, text: str, lang: str) -> Dict:
    """The same symbol shape as the ast engine, from a tree-sitter parse that survives broken code: every
    ERROR node is counted, everything around it is still a symbol."""
    out = {"engine": "tree-sitter", "path": path, "symbols": [], "imports": [], "errors": [], "aliases": {}}
    if not HAS_TREE_SITTER:
        out["errors"].append("tree_sitter_language_pack not installed")
        return out
    ts_lang = {"python": "python", "javascript": "javascript", "typescript": "typescript"}.get(lang)
    if not ts_lang:
        out["errors"].append("no tree-sitter grammar for " + lang)
        return out
    parser = _tslp.get_parser(ts_lang)
    data = text.encode("utf-8")
    tree = parser.parse(data)
    L = _Lines(text)
    # byte offsets → char offsets (the text may hold non-ASCII; the contract's spans are char offsets)
    if len(data) == len(text):
        b2c = None
    else:
        b2c = [0] * (len(data) + 1); cur = 0
        for i, ch in enumerate(text):
            n = len(ch.encode("utf-8"))
            for k in range(n):
                b2c[cur + k] = i
            cur += n
        b2c[cur] = len(text)
    conv = (lambda b: b) if b2c is None else (lambda b: b2c[min(b, len(b2c) - 1)])
    txt = lambda n: data[n.start_byte:n.end_byte].decode("utf-8", "replace")
    mod_id = _sid(path, "<module>")
    out["symbols"].append({"id": mod_id, "kind": "module", "name": os.path.basename(path), "qual": "<module>", "path": path, "start": 0,
                           "end": len(text), "line0": 1, "line1": L.line_of(max(0, len(text) - 1)), "parent": None, "fields": [], "calls": [],
                           "bases": [], "decorators": [], "async": False})
    errors = 0

    def child(n, kind):
        for c in n.children:
            if c.type == kind:
                return c
        return None

    def field(n, name):
        try:
            return n.child_by_field_name(name)
        except Exception:
            return None

    def calls_in(n):
        found = []
        stack = [n]
        while stack:
            k = stack.pop()
            if k.type in ("call", "call_expression"):
                f = field(k, "function")
                if f is not None:
                    s = txt(f)
                    if "." in s:
                        parts = s.split("."); found.append({"name": parts[-1], "via": ".".join(parts[:-1]), "line": k.start_point[0] + 1})
                    else:
                        found.append({"name": s, "via": "", "line": k.start_point[0] + 1})
            stack.extend(k.children)
        return found

    def visit(n, parent_id, qual, own_class):
        nonlocal errors
        for c in n.children:
            t = c.type
            if t == "ERROR" or c.is_missing:
                errors += 1
                visit(c, parent_id, qual, own_class)
                continue
            if t in ("import_statement", "import_from_statement"):
                s = txt(c)
                m = re.match(r"from\s+(\S+)\s+import\s+(.+)", s) if t == "import_from_statement" else None
                if m:
                    for part in m.group(2).strip("() ").split(","):
                        nm, _, al = [x.strip() for x in part.partition(" as ")]
                        if nm:
                            out["imports"].append({"module": m.group(1), "name": nm, "alias": al or nm, "line": c.start_point[0] + 1}); out["aliases"][al or nm] = m.group(1) + "." + nm
                else:
                    m2 = re.match(r"import\s+(.+)", s)
                    if m2 and lang == "python":
                        for part in m2.group(1).split(","):
                            nm, _, al = [x.strip() for x in part.partition(" as ")]
                            if nm:
                                out["imports"].append({"module": nm, "name": "", "alias": al or nm, "line": c.start_point[0] + 1}); out["aliases"][(al or nm).split(".")[0]] = nm
                    elif m2:   # a JS import: the same names the pattern engine reads, so `helper()` resolves through it
                        mj = _JS_IMPORT.match(s)
                        if mj:
                            mod = mj.group(5) or mj.group(8)
                            names = [x.strip().split(" as ")[-1].strip() for x in ((mj.group(2) or "") + "," + (mj.group(4) or "") + "," + (mj.group(6) or "")).split(",") if x.strip()]
                            for nm in [mj.group(1), mj.group(3), mj.group(7)] + names:
                                if nm:
                                    out["aliases"][nm] = mod
                            out["imports"].append({"module": mod, "name": ",".join(names), "alias": mj.group(1) or mj.group(3) or mj.group(7) or "", "line": c.start_point[0] + 1})
            elif t in ("class_definition", "class_declaration"):
                nm = field(c, "name"); name = txt(nm) if nm is not None else "?"
                q = (qual + "." if qual else "") + name; cid = _sid(path, q)
                bases = []
                sup = field(c, "superclasses") or child(c, "class_heritage")
                if sup is not None:
                    bases = [b.strip() for b in re.sub(r"^\(|\)$|^extends\s+", "", txt(sup).strip()).split(",") if b.strip()]
                body = field(c, "body")
                out["symbols"].append({"id": cid, "kind": "class", "name": name, "qual": q, "path": path, "start": conv(c.start_byte), "end": conv(c.end_byte),
                                       "line0": c.start_point[0] + 1, "line1": c.end_point[0] + 1, "parent": parent_id, "fields": [], "calls": [], "bases": bases,
                                       "decorators": [], "async": False, "methods": sum(1 for k in (body.children if body is not None else []) if k.type in ("function_definition", "method_definition"))})
                if body is not None:
                    visit(body, cid, q, name)
            elif t in ("function_definition", "method_definition", "function_declaration", "generator_function_declaration"):
                nm = field(c, "name"); name = txt(nm) if nm is not None else "?"
                q = (qual + "." if qual else "") + name; fid = _sid(path, q)
                params = field(c, "parameters")
                args = [p.strip() for p in txt(params).strip("()").split(",")] if params is not None else []
                args = [a for a in args if a and a not in ("self", "cls")]
                out["symbols"].append({"id": fid, "kind": "method" if own_class else "function", "name": name, "qual": q, "path": path,
                                       "start": conv(c.start_byte), "end": conv(c.end_byte), "line0": c.start_point[0] + 1, "line1": c.end_point[0] + 1,
                                       "parent": parent_id, "fields": [], "calls": calls_in(c), "bases": [], "decorators": [],
                                       "async": txt(c).lstrip().startswith("async"), "args": args, "own_class": own_class})
                body = field(c, "body")
                if body is not None:
                    visit(body, fid, q, own_class)
            elif t in ("lexical_declaration", "variable_declaration") and lang != "python":
                for d in c.children:
                    if d.type == "variable_declarator":
                        nm, val = field(d, "name"), field(d, "value")
                        if nm is not None and val is not None and val.type == "call_expression" and txt(val).startswith("require("):
                            mr = re.search(r"require\(\s*['\"]([^'\"]+)['\"]", txt(val))
                            if mr:
                                out["aliases"][txt(nm)] = mr.group(1)
                                out["imports"].append({"module": mr.group(1), "name": "", "alias": txt(nm), "line": c.start_point[0] + 1})
                            continue
                        if nm is not None and val is not None and val.type in ("arrow_function", "function", "function_expression"):
                            name = txt(nm); q = (qual + "." if qual else "") + name; fid = _sid(path, q)
                            out["symbols"].append({"id": fid, "kind": "method" if own_class else "function", "name": name, "qual": q, "path": path,
                                                   "start": conv(c.start_byte), "end": conv(c.end_byte), "line0": c.start_point[0] + 1, "line1": c.end_point[0] + 1,
                                                   "parent": parent_id, "fields": [], "calls": calls_in(val), "bases": [], "decorators": [],
                                                   "async": txt(val).lstrip().startswith("async"), "args": [], "own_class": own_class})
            elif t in ("decorated_definition", "export_statement", "block", "if_statement", "try_statement", "statement_block", "program", "module", "expression_statement", "with_statement"):
                visit(c, parent_id, qual, own_class)

    visit(tree.root_node, mod_id, "", None)
    # every ERROR / missing node in the whole tree counts, wherever it sits (a bad parameter list is inside the
    # def, which the walk above does not enter)
    total_err, stack = 0, [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" or n.is_missing:
            total_err += 1
        stack.extend(n.children)
    if total_err or getattr(tree.root_node, "has_error", False):
        out["errors"].append("%d syntax error node%s — partial parse" % (max(1, total_err), "" if max(1, total_err) == 1 else "s"))
    return out


# ── JavaScript / TypeScript through patterns (tolerant) ──────────────────────────────────────────
_JS_IMPORT = re.compile(r"^\s*import\s+(?:(?:\*\s+as\s+(\w+))|(?:\{([^}]*)\})|(\w+))?\s*(?:,\s*\{([^}]*)\})?\s*(?:from\s+)?['\"]([^'\"]+)['\"]|^\s*(?:const|let|var)\s+(?:\{([^}]*)\}|(\w+))\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)", re.M)
_JS_FUNC = re.compile(r"^[ \t]*(?:export\s+(?:default\s+)?)?(async\s+)?function\s*\*?\s*(\w+)\s*\(", re.M)
_JS_ARROW = re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(async\s*)?(?:\([^)]*\)|\w+)\s*=>", re.M)
_JS_CLASS = re.compile(r"^[ \t]*(?:export\s+(?:default\s+)?)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?\s*\{", re.M)
_JS_METHOD = re.compile(r"^[ \t]*(?:static\s+)?(async\s+)?(?:get\s+|set\s+)?(\w+)\s*\([^)]*\)\s*\{", re.M)
_JS_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(|\.([A-Za-z_$][\w$]*)\s*\(")


def _match_brace(text: str, open_at: int) -> int:
    """The index just past the brace that closes the one at open_at; the end of the text when unbalanced."""
    depth, i, n = 0, open_at, len(text)
    in_str = None
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2; continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i); i = n if j < 0 else j; continue
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _js_calls(body: str, line0: int) -> List[Dict]:
    found = []
    for m in _JS_CALL.finditer(body):
        name = m.group(1) or m.group(2)
        if not name or name in _JS_KEYWORDS:
            continue
        via = ""
        if m.group(2):
            back = body[max(0, m.start() - 40):m.start()]
            mm = re.search(r"([\w$.]+)$", back)
            via = mm.group(1) if mm else "?"
        found.append({"name": name, "via": via, "line": line0 + body.count("\n", 0, m.start())})
    return found


def parse_js_patterns(path: str, text: str) -> Dict:
    L = _Lines(text)
    out = {"engine": "patterns", "path": path, "symbols": [], "imports": [], "errors": [], "aliases": {}}
    mod_id = _sid(path, "<module>")
    out["symbols"].append({"id": mod_id, "kind": "module", "name": os.path.basename(path), "qual": "<module>", "path": path, "start": 0, "end": len(text),
                           "line0": 1, "line1": L.line_of(max(0, len(text) - 1)), "parent": None, "fields": [], "calls": [], "bases": [], "decorators": [], "async": False})
    for m in _JS_IMPORT.finditer(text):
        mod = m.group(5) or m.group(8)
        names = [x.strip().split(" as ")[-1].strip() for x in ((m.group(2) or "") + "," + (m.group(4) or "") + "," + (m.group(6) or "")).split(",") if x.strip()]
        for nm in [m.group(1), m.group(3), m.group(7)] + names:
            if nm:
                out["aliases"][nm] = mod
        out["imports"].append({"module": mod, "name": ",".join(names), "alias": m.group(1) or m.group(3) or m.group(7) or "", "line": L.line_of(m.start())})
    taken: List[Tuple[int, int]] = []
    for m in _JS_CLASS.finditer(text):
        s = m.start(); e = _match_brace(text, m.end() - 1); name = m.group(1); cid = _sid(path, name)
        body = text[m.end():e - 1]
        methods = []
        for mm in _JS_METHOD.finditer(body):
            if mm.group(2) in _JS_KEYWORDS:
                continue
            ms = m.end() + mm.start(); me = _match_brace(text, m.end() + mm.end() - 1)
            # a method sits at depth 1 of the class body: the braces before it in the body balance to zero
            pre = body[:mm.start()]
            if pre.count("{") - pre.count("}") != 0:
                continue
            methods.append((mm.group(2), ms, me, bool(mm.group(1))))
        out["symbols"].append({"id": cid, "kind": "class", "name": name, "qual": name, "path": path, "start": s, "end": e, "line0": L.line_of(s), "line1": L.line_of(max(s, e - 1)),
                               "parent": mod_id, "fields": [], "calls": [], "bases": [m.group(2)] if m.group(2) else [], "decorators": [], "async": False, "methods": len(methods)})
        for (mname, ms, me, is_async) in methods:
            q = name + "." + mname
            out["symbols"].append({"id": _sid(path, q), "kind": "method", "name": mname, "qual": q, "path": path, "start": ms, "end": me, "line0": L.line_of(ms), "line1": L.line_of(max(ms, me - 1)),
                                   "parent": cid, "fields": [], "calls": _js_calls(text[ms:me], L.line_of(ms)), "bases": [], "decorators": [], "async": is_async, "args": [], "own_class": name})
        taken.append((s, e))
    inside = lambda i: any(a <= i < b for a, b in taken)
    for rx, is_arrow in ((_JS_FUNC, False), (_JS_ARROW, True)):
        for m in rx.finditer(text):
            if inside(m.start()):
                continue
            name = m.group(2) if not is_arrow else m.group(1)
            if name in _JS_KEYWORDS:
                continue
            ob = text.find("{", m.end() - 1)
            e = _match_brace(text, ob) if ob >= 0 else min(len(text), text.find("\n", m.end()) + 1 if text.find("\n", m.end()) >= 0 else len(text))
            s = m.start(); fid = _sid(path, name)
            if any(x["id"] == fid for x in out["symbols"]):
                continue
            out["symbols"].append({"id": fid, "kind": "function", "name": name, "qual": name, "path": path, "start": s, "end": e, "line0": L.line_of(s), "line1": L.line_of(max(s, e - 1)),
                                   "parent": mod_id, "fields": [], "calls": _js_calls(text[s:e], L.line_of(s)), "bases": [], "decorators": [],
                                   "async": bool(m.group(1) if not is_arrow else m.group(2)), "args": [], "own_class": None})
    if text.count("{") != text.count("}"):
        out["errors"].append("unbalanced braces — partial")
    return out


# ── CSS and HTML through patterns ─────────────────────────────────────────────────────────────────
_CSS_RULE = re.compile(r"([^{}]+?)\s*\{([^{}]*)\}", re.S)
_HTML_TAG = re.compile(r"<(?!/|!|\?)([a-zA-Z][\w-]*)([^<>]*?)(/?)>", re.S)
_HTML_ATTR = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))")


def parse_css(path: str, text: str) -> Dict:
    L = _Lines(text)
    out = {"engine": "patterns", "path": path, "symbols": [], "imports": [], "errors": [], "aliases": {}}
    # the stylesheet itself is a card: what a page's <link> is an import of
    out["symbols"].append({"id": _sid(path, "<module>"), "kind": "module", "name": os.path.basename(path), "qual": "<module>", "path": path, "start": 0, "end": len(text),
                           "line0": 1, "line1": L.line_of(max(0, len(text) - 1)), "parent": None, "fields": [], "calls": [], "bases": [], "decorators": [], "async": False})
    body = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), text, flags=re.S)
    # @media / @supports blocks: their inner rules are rules too — drop the wrapper, keep the offsets
    for m in re.finditer(r"@(media|supports|layer|container)[^{]*\{", body):
        e = _match_brace(body, m.end() - 1)
        body = body[:m.start()] + " " * (m.end() - m.start()) + body[m.end():e - 1] + " " + body[e:]
    for m in _CSS_RULE.finditer(body):
        sel = " ".join(m.group(1).split())
        if not sel or sel.startswith("@"):
            continue
        decls = [d.strip() for d in m.group(2).split(";") if d.strip()]
        s, e = m.start(1) + (len(m.group(1)) - len(m.group(1).lstrip())), m.end()   # the span starts at the selector, not where the last rule ended
        out["symbols"].append({"id": _sid(path, sel + "@" + str(s)), "kind": "selector", "name": sel[:80], "qual": sel, "path": path, "start": s, "end": e, "line0": L.line_of(s), "line1": L.line_of(max(s, e - 1)),
                               "parent": None, "fields": [{"k": d.split(":")[0].strip(), "v": ":".join(d.split(":")[1:]).strip()[:40]} for d in decls[:3]], "calls": [], "bases": [],
                               "decorators": [], "async": False, "decls": len(decls)})
    if body.count("{") != body.count("}"):
        out["errors"].append("unbalanced braces — partial")
    return out


def parse_html(path: str, text: str) -> Dict:
    L = _Lines(text)
    out = {"engine": "patterns", "path": path, "symbols": [], "imports": [], "errors": [], "aliases": {}}
    # the page itself is a card: what its scripts and stylesheets are imports of
    out["symbols"].append({"id": _sid(path, "<module>"), "kind": "module", "name": os.path.basename(path), "qual": "<module>", "path": path, "start": 0, "end": len(text),
                           "line0": 1, "line1": L.line_of(max(0, len(text) - 1)), "parent": None, "fields": [], "calls": [], "bases": [], "decorators": [], "async": False})
    stack: List[Tuple[str, str]] = []   # (tag, symbol id) for nesting
    for m in _HTML_TAG.finditer(text):
        tag = m.group(1).lower(); attrs = {}
        for a in _HTML_ATTR.finditer(m.group(2) or ""):
            attrs[a.group(1).lower()] = a.group(2) if a.group(2) is not None else (a.group(3) if a.group(3) is not None else a.group(4) or "")
        s = m.start(); e = m.end()
        if tag == "script" and attrs.get("src"):
            out["imports"].append({"module": attrs["src"], "name": "", "alias": "", "line": L.line_of(s), "kind": "script"})
        if tag == "link" and "stylesheet" in (attrs.get("rel") or "").lower() and attrs.get("href"):
            out["imports"].append({"module": attrs["href"], "name": "", "alias": "", "line": L.line_of(s), "kind": "style"})
        ident = attrs.get("id"); classes = [c for c in (attrs.get("class") or "").split() if c]
        if not ident and not classes and tag not in ("html", "body", "head", "main", "nav", "header", "footer", "section", "article", "form", "table", "script", "link", "template"):
            continue
        label = "<%s%s%s>" % (tag, "#" + ident if ident else "", "".join("." + c for c in classes[:3]))
        sid = _sid(path, label + "@" + str(s))
        # the element's text head: what follows the tag until the next tag
        head = re.sub(r"\s+", " ", text[e:e + 160].split("<")[0]).strip()
        parent = None
        for ptag, pid in reversed(stack):
            parent = pid; break
        out["symbols"].append({"id": sid, "kind": "element", "name": label, "qual": label, "path": path, "start": s, "end": e, "line0": L.line_of(s), "line1": L.line_of(s),
                               "parent": parent, "fields": [{"k": "text", "v": head[:48]}] if head else [], "calls": [], "bases": [], "decorators": [], "async": False,
                               "tag": tag, "ident": ident or "", "classes": classes, "attrs": {k: v for k, v in attrs.items() if k in ("href", "src", "type", "name", "role", "data-r", "data-a")}})
        if tag in ("div", "section", "main", "nav", "header", "footer", "article", "form", "table", "ul", "ol", "aside") and not m.group(3):
            stack.append((tag, sid))
        # a closing tag for the top of the stack pops it (a crude but tolerant nesting)
    return out


def import_targets(parsed_imports: List[Dict], from_path: str) -> List[str]:
    """The repo-relative paths a file's imports could name ('vera.research.x' → 'vera/research/x.py' or
    'vera/research/x/__init__.py'; './y.js' → 'dir/y.js'), for a caller that widens the parsed set by one hop."""
    out = []
    here = os.path.dirname(from_path.replace("\\", "/"))
    for imp in parsed_imports:
        m = str(imp.get("module") or "")
        if not m:
            continue
        if m.startswith("./") or m.startswith("../") or m.startswith("/"):
            rel = os.path.normpath(os.path.join(here, m)).replace("\\", "/") if not m.startswith("/") else m.lstrip("/")
            out.append(rel)
            continue
        if m.startswith("."):
            up = len(m) - len(m.lstrip("."))
            base = here.split("/") if here else []
            pkg = base[:len(base) - (up - 1)] if up > 1 else base
            m = ".".join([x for x in pkg if x] + ([m.lstrip(".")] if m.lstrip(".") else []))
        if m.startswith("Vera."):
            m = m[len("Vera."):]
        rel = m.replace(".", "/")
        out.append(rel + ".py"); out.append(rel + "/__init__.py")
        if imp.get("name"):   # `from pkg import mod`: the name may be a module of the package
            out.append(rel + "/" + str(imp["name"]) + ".py")
    return out


# ── the resolver: symbols and imports → cards, groups and edges ───────────────────────────────────
_KIND_LABEL = {"module": "module", "class": "class", "function": "def", "method": "def", "selector": "rule", "element": "element"}


def _selector_matches(sel: str, el: Dict) -> Optional[str]:
    """Does the LAST compound of a selector hit this element? 'exact' on #id, 'heuristic' on class or tag."""
    last = re.split(r"[\s>+~]+", sel.strip())[-1]
    last = re.sub(r"::?[\w-]+(\([^)]*\))?", "", last)   # pseudo-classes / elements
    last = re.sub(r"\[[^\]]*\]", "", last)
    if not last or last in ("*", ""):
        return None
    ids = re.findall(r"#([\w-]+)", last); classes = re.findall(r"\.([\w-]+)", last)
    tag = re.match(r"^([a-zA-Z][\w-]*)", last)
    if ids:
        return "exact" if el.get("ident") in ids and all(c in el.get("classes", []) for c in classes) else None
    if classes:
        return "heuristic" if all(c in el.get("classes", []) for c in classes) and (not tag or tag.group(1).lower() == el.get("tag")) else None
    if tag and tag.group(1).lower() == el.get("tag") and el.get("tag") in ("body", "html", "main", "nav", "header", "footer", "section", "article", "form", "table"):
        return "heuristic"
    return None


def explode_sources(sources: List[Dict], *, max_external: int = 40, prefer_tree_sitter: bool = False, label: str = "") -> Dict:
    """sources: [{path, text, lang?}] → the Explode contract (kind 'code', or 'page' when HTML leads)."""
    parsed: List[Dict] = []
    receipts: Dict[str, Dict] = {}
    for src in sources:
        path = str(src.get("path") or "snippet")
        text = src.get("text") or ""
        lang = detect_lang(path, text, src.get("lang") or "")
        if lang == "python":
            p = parse_python_ast(path, text) if not (prefer_tree_sitter and HAS_TREE_SITTER) else parse_tree_sitter(path, text, "python")
            if p["errors"] and p["engine"] == "ast" and HAS_TREE_SITTER:
                p = parse_tree_sitter(path, text, "python")   # broken code: the tolerant engine reads what it can
        elif lang in ("javascript", "typescript"):
            p = parse_tree_sitter(path, text, lang) if HAS_TREE_SITTER else parse_js_patterns(path, text)
            if p["errors"] and p["engine"] == "tree-sitter" and not p["symbols"][1:]:
                p = parse_js_patterns(path, text)
        elif lang == "css":
            p = parse_css(path, text)
        elif lang == "html":
            p = parse_html(path, text)
        else:
            p = {"engine": "none", "path": path, "symbols": [], "imports": [], "errors": ["unknown language"], "aliases": {}}
        p["lang"] = lang; p["text"] = text
        parsed.append(p)
        r = receipts.setdefault(p["engine"], {"id": "code." + p["engine"].replace("-", ""), "label": "symbols · " + p["engine"], "by": p["engine"], "kind": "symbol", "on": True, "count": 0, "files": 0, "where": "host"})
        r["files"] += 1
    kind = "page" if parsed and all(p["lang"] in ("html", "css", "javascript", "typescript") for p in parsed) and any(p["lang"] == "html" for p in parsed) else "code"
    # ── groups: a plate per file, a plate per class inside it
    groups: List[Dict] = []
    cards: List[Dict] = []
    edges: List[Dict] = []
    card_by_id: Dict[str, Dict] = {}
    sym_by_id: Dict[str, Dict] = {}
    by_name: Dict[str, List[Dict]] = {}
    mod_index: Dict[str, str] = {}      # importable name → module symbol id
    for p in parsed:
        path = p["path"]
        groups.append({"id": path, "label": path, "kind": "file", "parent": None, "span": {"path": path, "start": 0, "end": len(p["text"])}})
        for s in p["symbols"]:
            sym_by_id[s["id"]] = s
            by_name.setdefault(s["name"], []).append(s)
            if s["kind"] == "module":
                for nm in module_names(path):
                    mod_index.setdefault(nm, s["id"])
                mod_index.setdefault(path.replace("\\", "/"), s["id"])            # a path import names the file itself
                mod_index.setdefault(os.path.basename(path), s["id"])             # 'panel.css' and 'panel.js' stay apart
            if s["kind"] == "class":
                q = s.get("parent")
                parent_group = q if q and sym_by_id.get(q, {}).get("kind") == "class" else path   # a nested class: a plate in its class's plate
                groups.append({"id": s["id"], "label": "class " + s["name"], "kind": "class", "parent": parent_group,
                               "span": {"path": path, "start": s["start"], "end": s["end"]}})
    # ── cards
    def group_of(s: Dict) -> str:
        q = s.get("parent")
        while q:
            ps = sym_by_id.get(q)
            if not ps:
                break
            if ps["kind"] == "class":
                return ps["id"]
            q = ps.get("parent")
        return s["path"]
    ext_group_added = False
    for p in parsed:
        for s in p["symbols"]:
            lines = max(1, int(s.get("line1", s.get("line0", 1))) - int(s.get("line0", 1)) + 1)
            k = s["kind"]
            if k == "module" and p["lang"] == "css":
                sub = "stylesheet · %d rule%s" % (len(p["symbols"]) - 1, "" if len(p["symbols"]) == 2 else "s")
            elif k == "module" and p["lang"] == "html":
                sub = "page · %d element%s · %d script%s / style%s" % (len(p["symbols"]) - 1, "" if len(p["symbols"]) == 2 else "s", len(p["imports"]), "" if len(p["imports"]) == 1 else "s", "s")
            elif k == "module":
                sub = "module · %d lines · %d import%s" % (lines, len(p["imports"]), "" if len(p["imports"]) == 1 else "s")
            elif k == "class":
                sub = "class · %d method%s · %d lines" % (s.get("methods", 0), "" if s.get("methods", 0) == 1 else "s", lines)
            elif k in ("function", "method"):
                sub = "%s · %d lines" % ("async" if s.get("async") else "def", lines)
            elif k == "selector":
                sub = "rule · %d declaration%s" % (s.get("decls", 0), "" if s.get("decls", 0) == 1 else "s")
            elif k == "element":
                sub = "element · line %d" % s.get("line0", 0)
            else:
                sub = k
            fields = list(s.get("fields") or [])
            if s.get("decorators"):
                fields.append({"k": "decorators", "v": " ".join(s["decorators"])[:60]})
            if s.get("bases"):
                fields.append({"k": "bases", "v": ", ".join(s["bases"])[:60]})
            if s.get("args") and k in ("function", "method"):
                fields.append({"k": "args", "v": ", ".join(s["args"])[:60]})
            card = {"id": s["id"], "group": group_of(s) if k != "element" else p["path"], "layer": "code." + p["engine"].replace("-", ""), "kind": k, "title": s["name"], "subtitle": sub,
                    "span": {"path": p["path"], "start": s["start"], "end": s["end"], "line": s.get("line0"), "line_end": s.get("line1")},
                    "fields": fields, "badges": [], "by": p["engine"]}
            cards.append(card); card_by_id[card["id"]] = card
            receipts[p["engine"]]["count"] += 1
    # ── edges
    externals: Dict[str, str] = {}
    def external(name: str, why: str) -> Optional[str]:
        nonlocal ext_group_added
        if name in externals:
            return externals[name]
        if len(externals) >= max_external:
            return None
        if not ext_group_added:
            groups.append({"id": "ext", "label": "external", "kind": "external", "parent": None}); ext_group_added = True
        cid = "ext::" + name
        externals[name] = cid
        cards.append({"id": cid, "group": "ext", "layer": "code.external", "kind": "external", "title": name, "subtitle": why, "span": {"path": "", "start": 0, "end": 0},
                      "fields": [], "badges": ["external"], "by": "resolver"})
        card_by_id[cid] = cards[-1]
        return cid

    def stub_for(qualified: str, name: str) -> Optional[str]:
        """The stub an unresolved import stands behind: for a third-party package, the package ('httpx'); for a
        module of our own that was not parsed, the imported name with its module — 'nlp_ner · vera.research…'."""
        top = qualified.lstrip(".").split(".")[0]
        if top in ("vera", "Vera") or qualified.startswith("."):
            mod = qualified if not qualified.endswith("." + name) else qualified[: -len(name) - 1]
            return external(name, (mod or "this repo") + " · not in the parsed set")
        return external(top, "package · not in the parsed set")

    def resolve_module(mod: str, from_path: str) -> Optional[str]:
        if re.search(r"\.(js|mjs|cjs|ts|tsx|jsx|css|html|htm|py)$", mod):    # a path import: the file, by its full name first
            here = os.path.dirname(from_path.replace("\\", "/"))
            rel = os.path.normpath(os.path.join(here, mod)).replace("\\", "/") if mod.startswith(".") else mod.lstrip("/")
            for cand in (rel, mod.lstrip("./"), os.path.basename(mod)):
                if cand in mod_index:
                    return mod_index[cand]
            return None
        m = mod.lstrip(".")
        if mod.startswith("."):   # a relative import: from the importing file's package
            base = os.path.dirname(from_path).replace("\\", "/").split("/")
            up = len(mod) - len(m)
            pkg = base[:max(0, len(base) - (up - 1))] if up > 1 else base
            m = ".".join([x for x in pkg if x] + ([m] if m else []))
        for cand in (m, m.split(".")[-1]):
            if cand in mod_index:
                return mod_index[cand]
        # a JS path import: './x.js', '/ui/routes.js'
        base = os.path.splitext(os.path.basename(m))[0]
        for nm, sid in mod_index.items():
            if nm.split(".")[-1] == base:
                return sid
        return None

    seen_edges = set()
    def add_edge(a: str, b: str, kind: str, res: str, layer: str, label: str = "", by: str = "resolver"):
        key = (a, b, kind)
        if a == b or key in seen_edges or a not in card_by_id or b not in card_by_id:
            return
        seen_edges.add(key)
        edges.append({"from": a, "to": b, "layer": layer, "kind": kind, "resolution": res, "label": label, "by": by})

    for p in parsed:
        mod_sym = next((s for s in p["symbols"] if s["kind"] == "module"), None)
        # imports: module → module (exact), a standard-library name → a field on the module card (not a stub),
        # anything else → an external stub
        stdlib_seen: List[str] = []
        for imp in p["imports"]:
            target = None
            if imp.get("name"):                                       # `from pkg import module`
                target = resolve_module(imp["module"].rstrip(".") + "." + str(imp["name"]) if imp["module"] else str(imp["name"]), p["path"])
            target = target or resolve_module(imp["module"], p["path"])
            if target and mod_sym:
                add_edge(mod_sym["id"], target, "IMPORTS", "exact", "code.imports", imp.get("name") or "", p["engine"])
            elif mod_sym:
                m_ = imp["module"]
                if "/" in m_ or m_.endswith((".js", ".mjs", ".css", ".ts")):   # a path import: the file it names
                    top = os.path.basename(m_)
                else:
                    top = m_.split(".")[0] if not m_.startswith(".") else m_
                if p["lang"] == "python" and top in _STDLIB:
                    if top not in stdlib_seen:
                        stdlib_seen.append(top)
                    continue
                if top and top not in ("Vera", "vera"):
                    ext = external(top, "package · not in the parsed set")
                    if ext:
                        add_edge(mod_sym["id"], ext, "IMPORTS", "external", "code.imports", imp.get("name") or "", p["engine"])
        if stdlib_seen and mod_sym and mod_sym["id"] in card_by_id:
            card_by_id[mod_sym["id"]]["fields"].append({"k": "stdlib", "v": ", ".join(stdlib_seen)[:60]})
        for s in p["symbols"]:
            # inheritance
            for b in s.get("bases") or []:
                bname = b.split(".")[-1].split("(")[0].strip()
                if not bname:
                    continue
                cands = [x for x in by_name.get(bname, []) if x["kind"] == "class"]
                same = [x for x in cands if x["path"] == p["path"]]
                if same:
                    add_edge(s["id"], same[0]["id"], "INHERITS", "exact", "code.symbols", "", p["engine"])
                elif len(cands) == 1:
                    add_edge(s["id"], cands[0]["id"], "INHERITS", "heuristic", "code.symbols", "by name", p["engine"])
                elif cands:
                    add_edge(s["id"], cands[0]["id"], "INHERITS", "heuristic", "code.symbols", "one of %d by name" % len(cands), p["engine"])
                else:
                    ext = external(bname, "class · not in the parsed set")
                    if ext:
                        add_edge(s["id"], ext, "INHERITS", "external", "code.symbols", "", p["engine"])
            # containment: a class holds its methods (a layer of its own, off by default — the plate already says it)
            if s["kind"] == "method" and s.get("parent") in sym_by_id and sym_by_id[s["parent"]]["kind"] == "class":
                add_edge(s["parent"], s["id"], "CONTAINS", "exact", "code.contains", "", p["engine"])
            # calls
            n_ext = 0
            for c in s.get("calls") or []:
                name, via = c["name"], c.get("via") or ""
                if not via and name in _BUILTINS:
                    continue
                target = None; res = "exact"; lbl = ""
                if via in ("self", "cls", "this") and s.get("own_class"):
                    own = [x for x in by_name.get(name, []) if x["kind"] == "method" and x.get("own_class") == s.get("own_class") and x["path"] == p["path"]]
                    if own:
                        target = own[0]["id"]
                elif via:
                    root = via.split(".")[0]
                    al = p.get("aliases", {}).get(root)       # `mod.fn()` / `alias.fn()`: the module the alias names
                    if al:
                        msid = resolve_module(al, p["path"])
                        if msid:
                            mpath = sym_by_id[msid]["path"]
                            hit = [x for x in by_name.get(name, []) if x["path"] == mpath and x["kind"] in ("function", "class", "method")]
                            if hit:
                                target = hit[0]["id"]
                        elif al.split(".")[0] not in _STDLIB:  # a package (or an unparsed module of our own) called through: a stub
                            ext = stub_for(al, name)
                            if ext:
                                target = ext; res = "external"
                    if not target:
                        cands = [x for x in by_name.get(name, []) if x["kind"] in ("method", "function")]
                        if len(cands) == 1:
                            target = cands[0]["id"]; res = "heuristic"; lbl = "by method name"
                        elif cands:
                            target = cands[0]["id"]; res = "heuristic"; lbl = "one of %d by name" % len(cands)
                else:
                    local = [x for x in by_name.get(name, []) if x["path"] == p["path"] and x["kind"] in ("function", "class")]
                    if local:
                        target = local[0]["id"]
                    else:
                        al = p.get("aliases", {}).get(name)
                        if al:
                            msid = resolve_module(".".join(al.split(".")[:-1]) or al, p["path"])
                            if msid:
                                mpath = sym_by_id[msid]["path"]
                                hit = [x for x in by_name.get(name, []) if x["path"] == mpath]
                                if hit:
                                    target = hit[0]["id"]
                            if not target:
                                ext = stub_for(al, name)
                                if ext:
                                    target = ext; res = "external"
                        if not target:
                            cands = [x for x in by_name.get(name, []) if x["kind"] in ("function", "class")]
                            if len(cands) == 1:
                                target = cands[0]["id"]; res = "heuristic"; lbl = "by name"
                            elif cands:
                                target = cands[0]["id"]; res = "heuristic"; lbl = "one of %d by name" % len(cands)
                if not target:
                    if via and via not in ("self", "cls", "this", "?", "()"):
                        n_ext += 1
                    continue
                if res == "external":
                    n_ext += 1
                add_edge(s["id"], target, "CALLS", res, "code.calls", lbl, p["engine"])
            if n_ext and s["id"] in card_by_id:
                card_by_id[s["id"]]["badges"].append("external:%d" % n_ext)
    # a page: selectors hit elements, scripts and styles are imports
    els = [s for p in parsed if p["lang"] == "html" for s in p["symbols"] if s["kind"] == "element"]
    if els:
        for p in parsed:
            if p["lang"] != "css":
                continue
            for s in p["symbols"]:
                for el in els:
                    res = _selector_matches(s["qual"], el)
                    if res:
                        add_edge(s["id"], el["id"], "MATCHES", res, "page.css", "", "patterns")
    # ── layers, assessments, counts
    layers = [dict(r) for r in receipts.values()]
    layers += [{"id": "code.calls", "label": "calls", "kind": "relation", "by": "resolver", "on": True, "count": sum(1 for e in edges if e["kind"] == "CALLS"), "where": "host"},
               {"id": "code.imports", "label": "imports", "kind": "relation", "by": "resolver", "on": True, "count": sum(1 for e in edges if e["kind"] == "IMPORTS"), "where": "host"},
               {"id": "code.contains", "label": "contains", "kind": "relation", "by": "resolver", "on": False, "count": sum(1 for e in edges if e["kind"] == "CONTAINS"), "where": "host"}]
    if any(e["kind"] == "MATCHES" for e in edges):
        layers.append({"id": "page.css", "label": "selectors → elements", "kind": "relation", "by": "patterns", "on": True, "count": sum(1 for e in edges if e["kind"] == "MATCHES"), "where": "host"})
    if externals:
        layers.append({"id": "code.external", "label": "external stubs", "kind": "symbol", "by": "resolver", "on": True, "count": len(externals), "where": "host"})
    errors = [(p["path"], err) for p in parsed for err in p["errors"]]
    partial = bool(errors)
    n_files = max(1, len(parsed))
    assessments = [{"key": "syntax", "label": "parses", "score": round(1 - len({e[0] for e in errors}) / n_files, 3), "confidence": 1.0, "by": ", ".join(sorted(receipts)) or "none", "on": "source",
                    "evidence": [{"span": {"path": pth, "start": 0, "end": 0}, "note": err} for pth, err in errors[:12]]}]
    n_calls = sum(1 for e in edges if e["kind"] == "CALLS"); n_ext = sum(1 for e in edges if e["resolution"] == "external")
    if n_calls + n_ext:
        assessments.append({"key": "resolved", "label": "calls resolved", "score": round(sum(1 for e in edges if e["kind"] == "CALLS" and e["resolution"] == "exact") / max(1, n_calls), 3),
                            "confidence": 0.9, "by": "resolver", "on": "source"})
    if partial:
        for c in cards:
            if c["kind"] != "external" and any(pth == c["span"]["path"] for pth, _ in errors):
                c["badges"].append("partial")
    text_all = "".join(p["text"] for p in parsed)
    return {"ok": True, "kind": kind,
            "source": {"path": parsed[0]["path"] if len(parsed) == 1 else "", "paths": [p["path"] for p in parsed], "label": label or (parsed[0]["path"] if len(parsed) == 1 else "%d files" % len(parsed)),
                       "partial": partial, "errors": [{"path": a, "error": b} for a, b in errors], "text_hash": hashlib.sha1(text_all.encode("utf-8", "replace")).hexdigest()[:16],
                       "chars": len(text_all), "engines": sorted(receipts), "tree_sitter": HAS_TREE_SITTER},
            "layout": {"direction": "LR", "mode": "dependency"}, "layers": layers, "groups": groups, "cards": cards, "edges": edges, "assessments": assessments,
            "counts": {"groups": len(groups), "cards": len(cards), "edges": len(edges), "files": len(parsed), "external": len(externals)}}
