"""Every vera/ source compiles under the gate's Python.

A cap module that fails to compile is skipped by the loader with one log line, so its caps 404 while the rest of
prod looks healthy. On 18 Oct a converge merge left two contract= keywords on evolve.schedule.history
(SyntaxError: keyword argument repeated) and the whole scheduler went missing; a gate that selected tests by
impact never imported the module."""
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..", "vera")
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "venv", "vendor", "third_party"}


def _sources():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def test_every_vera_source_compiles():
    broken = []
    for path in _sources():
        with open(path, "rb") as fh:
            src = fh.read()
        try:
            compile(src, path, "exec", dont_inherit=True)
        except SyntaxError as e:
            broken.append(f"{os.path.relpath(path, ROOT)}:{e.lineno}: {e.msg}")
    assert not broken, "sources that do not compile:\n" + "\n".join(broken)
