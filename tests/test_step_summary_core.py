"""Step summaries are sentences (plan item 19).

run70-73 (24 Sep 2026): 65 of 130 step summaries began with `{` - the last
tool preview verbatim - because every path that ends a step for the executor
summarised that way. Pure tests of the sentence builder, plus a source pin
that the loop's fallback sites use it.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag.step_summary_core import sentence  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_an_exec_result_reads_as_what_ran_and_what_came_back():
    prev = ('{"ok": true, "rc": 0, "stdout": "200 /workspace/nums.csv\\n54\\n814", "stderr": "", '
            '"timed_out": false, "sandboxed": true, "command": "wc -l /workspace/nums.csv"}')
    s = sentence("exec.bash.run", prev, "The loop ended this step (a stall guard fired).")
    assert s.startswith("The loop ended this step (a stall guard fired). exec.bash.run ran wc -l /workspace/nums.csv: rc=0")
    assert "stdout: 200 /workspace/nums.csv" in s
    assert "{" not in s


def test_a_failed_exec_shows_the_traceback_line():
    prev = ('{"ok": false, "rc": 1, "stdout": "", "stderr": "Traceback (most recent call last):\\n  File x", '
            '"command": "python3 /workspace/parse.py"}')
    s = sentence("exec.python.run", prev)
    assert "rc=1" in s and "stderr: Traceback (most recent call last):" in s


def test_an_authored_file_reads_as_written_and_parsed():
    prev = ('{"ok": true, "path": "statkit/stats.py", "fs_path": "/workspace/statkit/stats.py", "version": 4, '
            '"lang": "python", "applied": [{"find_preview": "x"}], "errors": [], "syntax_ok": true, '
            '"checked_with": "python-compile"')            # truncated, as previews are
    s = sentence("code.edit", prev)
    assert s == "code.edit wrote statkit/stats.py, python-compile ok"


def test_a_truncated_preview_still_yields_a_sentence():
    prev = ('{"ok": true, "path": "clock.html", "fs_path": "/workspace/clock.html", "version": 1, "bytes": 1634, '
            '"lang": "html", "chars": 1634, "truncated": false, "syntax_o')
    s = sentence("code.author", prev)
    assert s.startswith("code.author wrote clock.html, 1634 bytes")


def test_plain_text_is_kept_to_its_first_line_and_a_fence_is_parsed():
    assert sentence("web.research", "Research complete: three sources.\nmore") == "web.research: Research complete: three sources."
    s = sentence("sandbox.session.fs.read", '```json\n{"ok": true, "path": "a.md", "text": "# Title\\nbody"}\n```')
    assert s == "sandbox.session.fs.read returned: # Title"


def test_empty_and_junk_never_raise():
    assert sentence("x", "", "Ended.") == "Ended."
    assert sentence("", "{not json at all", "") == "{not json at all"
    assert sentence(None, None, "") == ""


def test_the_loops_fallback_sites_use_the_sentence():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "def _v5_sentence(" in src
    assert 'result_summary = list(outputs.values())[-1][:_V5_DONE_SUMMARY]' not in src
    assert 'result_summary = history[-1]["preview"][:_V5_DONE_SUMMARY]' not in src
    assert 'result_summary = (_got or preview)[:_V5_DONE_SUMMARY]' not in src
    assert 'result_summary = _cached_preview[:_V5_DONE_SUMMARY]' not in src
    assert 'result_summary = ((list(outputs.values())[-1] if outputs else "")' not in src
    assert src.count("_v5_sentence(") >= 7
    ast.parse(src)
