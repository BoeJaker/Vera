"""A module that uses `os.` must import os.

Found live 2026-09-12: `agentbridge.catalog` answered every call with
HTTP 500 `name 'os' is not defined`, so the Agent Bridges panel showed
"request failed" and nothing else. Commit 61d5e72 (2026-09-11, "Synchronize
runtime matrix with adapter evidence") dropped `import os` from
vera/agentbridges/agentbridge_capabilities.py while tidying imports it had
stopped using - and missed the `os.environ.get` on line 258, which is inside
a function body and so runs only when the capability is actually called.

That is the shape of the whole bug class this guards: import-time is clean,
py_compile is clean, `ast.parse` is clean, the module loads, the app boots,
the gate goes green, and the capability dies the first time a human clicks
the panel. Nothing in the suite looked at a name that is only resolved at
call time.

The scan is deliberately narrow so it cannot cry wolf: it flags `NAME.attr`
only when NAME is a well-known stdlib module AND that name is never bound
anywhere in the file - not imported, assigned, declared global, used as a
parameter, a def/class name, an except-handler alias or a loop target. A
local variable that happens to be called `time` therefore does not trip it.
"""
import ast
import io
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))

# Stdlib modules common enough in this codebase that `NAME.attr` on a name
# that is never bound is a missing import rather than anything else.
STDLIB = {
    "os", "sys", "json", "re", "time", "shutil", "subprocess", "socket", "math",
    "random", "uuid", "hashlib", "base64", "asyncio", "logging", "glob",
    "datetime", "traceback", "tempfile", "textwrap", "itertools", "functools",
    "collections", "inspect", "string", "copy", "csv", "io", "struct", "signal",
    "platform", "secrets", "statistics", "threading", "queue", "sqlite3",
    "pickle", "zipfile", "tarfile", "gzip", "codecs", "errno", "stat",
    "fnmatch", "ast", "importlib", "contextlib", "warnings", "weakref",
    "unicodedata", "binascii", "ipaddress", "mimetypes", "difflib", "pprint",
    "decimal", "bisect", "heapq", "abc", "array", "calendar", "shlex", "typing",
}

SKIP_DIRS = {"__pycache__", "node_modules", "vendor", ".git"}


class _Bindings(ast.NodeVisitor):
    """Every name this module binds, by any means, at any scope.

    Scope-insensitive on purpose: a name bound in one function and used in
    another is not what this test is looking for, and treating the file as
    one flat namespace is what keeps the false-positive rate at zero.
    """

    def __init__(self):
        self.names = set()

    def visit_Import(self, node):
        for a in node.names:
            self.names.add((a.asname or a.name).split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for a in node.names:
            self.names.add(a.asname or a.name)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)
        self.generic_visit(node)

    def _args(self, a):
        for x in list(a.args) + list(a.kwonlyargs) + list(getattr(a, "posonlyargs", [])):
            self.names.add(x.arg)
        if a.vararg:
            self.names.add(a.vararg.arg)
        if a.kwarg:
            self.names.add(a.kwarg.arg)

    def visit_FunctionDef(self, node):
        self.names.add(node.name)
        self._args(node.args)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._args(node.args)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.names.update(node.names)
        self.generic_visit(node)

    def visit_Nonlocal(self, node):
        self.names.update(node.names)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)


def _unbound_stdlib_uses(path):
    """-> [(name, lineno)] for `name.attr` where `name` is never bound here."""
    try:
        src = io.open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []            # a syntax error is the compile gate's business
    bound = _Bindings()
    bound.visit(tree)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            name = node.value.id
            if name in STDLIB and name not in bound.names:
                found.setdefault(name, node.lineno)
    return sorted(found.items())


def _python_files():
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "vera")):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def test_every_used_stdlib_module_is_imported():
    files = list(_python_files())
    # Guard the guard: an os.walk over a path that does not exist yields
    # nothing and this test would pass by scanning air.
    assert len(files) > 100, "expected to scan the vera package, saw %d files" % len(files)

    offenders = []
    for path in files:
        for name, lineno in _unbound_stdlib_uses(path):
            offenders.append("%s:%d uses `%s.` but never imports %s"
                             % (os.path.relpath(path, ROOT), lineno, name, name))
    assert not offenders, "missing imports:\n  " + "\n  ".join(offenders)


def test_the_scan_catches_a_removed_import(tmp_path):
    """The 2026-09-12 regression itself, reduced. Without this, a scan that
    silently stopped matching would pass forever."""
    mod = tmp_path / "regressed.py"
    mod.write_text(
        "import logging\n"
        "log = logging.getLogger('x')\n"
        "def enabled(env):\n"
        "    return os.environ.get(env, '0') == '1'\n",
        encoding="utf-8",
    )
    assert _unbound_stdlib_uses(str(mod)) == [("os", 4)]

    mod.write_text(
        "import logging\n"
        "import os\n"
        "log = logging.getLogger('x')\n"
        "def enabled(env):\n"
        "    return os.environ.get(env, '0') == '1'\n",
        encoding="utf-8",
    )
    assert _unbound_stdlib_uses(str(mod)) == []


def test_a_local_name_shadowing_a_module_is_not_flagged(tmp_path):
    """The false positive that would make this test ignorable."""
    mod = tmp_path / "shadow.py"
    mod.write_text(
        "def render(time, json=None):\n"
        "    stamp = time.hour\n"
        "    body = json.dumps if json else None\n"
        "    return stamp, body\n",
        encoding="utf-8",
    )
    assert _unbound_stdlib_uses(str(mod)) == []
