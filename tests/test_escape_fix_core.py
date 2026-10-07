"""Double-escaped newlines in exec code are corrected before the code runs
(probe 2026-10-01 b-data-count: n.csv was one line of literal "\\n" text)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import escape_fix_core as E  # noqa: E402

# Exactly what the live probe sent (decoded tool argument).
LIVE = ("import random\nnumbers = list(set(random.randint(1, 100) for _ in range(50)))\n"
        "with open('/workspace/n.csv', 'w') as f:\n    for n in numbers:\n"
        "        f.write(str(n) + '\\\\n')")


def _run(code, tmp_path):
    p = tmp_path / "n.csv"
    exec(code.replace("/workspace/n.csv", p.as_posix()), {})
    return p.read_text()


def test_the_live_probe_code_writes_real_lines_after_the_fix(tmp_path):
    broken = _run(LIVE, tmp_path)
    assert "\n" not in broken and "\\n" in broken          # the bug, reproduced
    fixed, n = E.fix_double_escaped_newlines(LIVE)
    assert n == 1
    out = _run(fixed, tmp_path)
    assert "\\n" not in out and out.count("\n") == len(out.splitlines())


def test_join_and_tab_and_triple_quoted():
    code = "s = '\\\\n'.join(['a', 'b'])\nt = 'x\\\\ty'\nu = '''p\\\\nq'''\n"
    fixed, n = E.fix_double_escaped_newlines(code)
    assert n == 3
    ns = {}
    exec(fixed, ns)
    assert ns["s"] == "a\nb" and ns["t"] == "x\ty" and ns["u"] == "p\nq"


def test_raw_strings_comments_and_correct_code_are_untouched():
    for code in ("p = r'C:\\\\new'\n", "# write '\\\\n' here\nx = 1\n", "x = 'a\\nb'\n",
                 "re_ = r'\\\\n+'\n"):
        assert E.fix_double_escaped_newlines(code) == (code, 0), code


def test_an_escaped_backslash_before_a_newline_escape_is_left_alone():
    code = "x = 'a\\\\\\nb'\n"              # source: 'a\\\nb' = backslash + newline
    assert E.fix_double_escaped_newlines(code) == (code, 0)


def test_code_that_does_not_tokenize_is_returned_unchanged():
    code = "x = 'unterminated\\\\n\n"
    assert E.fix_double_escaped_newlines(code) == (code, 0)


def test_the_loop_corrects_exec_python_code_before_running_it():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    i = src.index("_escape_fix_core.fix_double_escaped_newlines(")
    assert i < src.index("# ── RUN-BEFORE-AUTHOR guard")
    assert '"exec.python.run"' in src[i - 400:i]
