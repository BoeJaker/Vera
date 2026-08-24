"""exec.<lang>.run must not discard inline `code` when `path` doesn't exist.

Pins the 2026-08-24 defect: a loop step called exec.python.run with BOTH the
script in `code` AND path='/workspace/check_syntax.py'. The runner preferred
`path`, failed to open it, and returned "can't open file ..." — throwing away the
code it had been handed. Nothing about the call changed, so the retry failed
identically: three consecutive cycles burned.

Exercises the real capability against a tmp dir (no network, no LLM).
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.execution import exec_capabilities as E  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _run_code(**kw):
    return run(E._run_code(language="python", **kw))


# ── the incident ─────────────────────────────────────────────────────────────
def test_missing_path_falls_back_to_inline_code(tmp_path):
    missing = str(tmp_path / "check_syntax.py")
    r = _run_code(code="print('hello from inline')", path=missing)
    assert r.get("ok"), r
    assert "hello from inline" in (r.get("stdout") or "")
    assert r.get("ran_inline_code") is True
    assert missing in (r.get("note") or "")


def test_fallback_does_not_create_the_file(tmp_path):
    """It runs the code; it must not silently materialise the path."""
    missing = str(tmp_path / "nope.py")
    _run_code(code="print(1)", path=missing)
    assert not os.path.exists(missing)


# ── existing behaviour must be unchanged ─────────────────────────────────────
def test_existing_path_still_wins_over_code(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("print('from the file')")
    r = _run_code(code="print('from inline')", path=str(real))
    assert r.get("ok"), r
    assert "from the file" in (r.get("stdout") or "")
    assert "from inline" not in (r.get("stdout") or "")
    assert not r.get("ran_inline_code")


def test_missing_path_with_no_code_still_errors(tmp_path):
    """Nothing to fall back to — must still fail loudly, not silently pass."""
    missing = str(tmp_path / "gone.py")
    r = _run_code(code="", path=missing)
    assert not r.get("ok")


def test_invocation_string_as_code_still_errors(tmp_path):
    """`code` that is only an invocation ("python app.py") is not runnable code,
    so a bad inferred path must keep failing rather than exec the string."""
    missing = str(tmp_path / "app.py")
    r = _run_code(code=f"python {missing}")
    assert not r.get("ok")


def test_plain_snippet_with_no_path_is_unaffected():
    r = _run_code(code="print('plain')")
    assert r.get("ok"), r
    assert "plain" in (r.get("stdout") or "")
    assert not r.get("ran_inline_code")
