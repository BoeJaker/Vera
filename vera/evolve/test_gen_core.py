"""Pure helpers for M3.4 test generation (evolve.tests.generate).

Kept free of I/O and LLM calls so the decision logic — which changed files are
worth generating tests for, where the test file goes, and how to unwrap an LLM's
fenced output — is unit-testable without the app or a model. The capability layer
gathers the git diff + module source and calls the LLM; these functions decide the
rest with no side effects.
"""
from __future__ import annotations

import os
from typing import List


def strip_code_fence(text: str) -> str:
    """Return just the code from an LLM reply that may wrap it in a markdown fence
    (```python … ```). No fence → the text unchanged (trimmed). Only strips a fence
    that opens on the first non-empty line, so prose-free code is untouched."""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    lines = lines[1:]                                    # drop the opening ``` / ```python
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]                               # drop the closing ```
    return "\n".join(lines).strip()


def test_target_for(path: str) -> dict:
    """For a source path ('vera/evolve/foo.py') return the LOWERCASE import module
    ('vera.evolve.foo' — the worktree-safe form, see the Vera.vera namespace trap)
    and the conventional test file ('tests/test_foo.py')."""
    p = (path or "").strip().replace("\\", "/")
    mod = p[:-3] if p.endswith(".py") else p
    import_name = mod.replace("/", ".")
    base = os.path.basename(p)
    stem = base[:-3] if base.endswith(".py") else base
    return {"import_name": import_name, "suggested_test": f"tests/test_{stem}.py", "stem": stem}


def generatable_modules(changed_paths: List[str], max_n: int = 3) -> List[str]:
    """From a branch's changed files, keep the SOURCE modules worth generating tests
    for: `vera/**/*.py`, excluding anything under a tests/ dir, `__init__.py`, and
    files already named `test_*`. Order-preserving, de-duplicated, capped at `max_n`
    to bound how many (slow) LLM calls a single generate does."""
    out: List[str] = []
    seen = set()
    for raw in changed_paths or []:
        p = (raw or "").strip().replace("\\", "/")
        if not p or p in seen:
            continue
        if not p.endswith(".py") or not p.startswith("vera/"):
            continue
        base = os.path.basename(p)
        if "/tests/" in p or base.startswith("test_") or base == "__init__.py":
            continue
        seen.add(p)
        out.append(p)
    n = max(1, int(max_n or 3))
    return out[:n]
