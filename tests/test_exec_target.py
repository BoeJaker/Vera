"""exec.<lang>.run given BOTH code and a path (vera/execution/exec_target.py).

Board loop-o43: research-web wall-capped in census runs 47 and 48 with the same
shape - the model sent a script's source in `code` and its destination in
`path` in one call; the session-sandbox route ran the absent path, the
interpreter said "can't open file", and the source was discarded. Seven of run
47's eight exec failures and run 48's one were this call.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.execution import exec_target as et  # noqa: E402

SCRIPT = "import re\n\ndef extract(text):\n    return re.findall(r'WebGPU', text)\n\nprint(extract(open('notes.html').read()))\n"


def test_the_census_shape_materialises():
    # run 48 cycle 24: real code + a /workspace path that does not exist yet.
    assert et.decide(SCRIPT, "/workspace/parse_chrome_webgpu.py", path_exists=False) == et.MATERIALISE


def test_an_existing_file_is_run_and_never_overwritten_by_inline_code():
    assert et.decide(SCRIPT, "/workspace/parse_chrome_webgpu.py", path_exists=True) == et.RUN_PATH


def test_a_path_alone_is_run_as_before_even_when_absent():
    # No source to fall back on: the interpreter's error (plus the missing-path
    # hint) is still the right answer.
    assert et.decide("", "/workspace/missing.py", path_exists=False) == et.RUN_PATH


def test_an_invocation_string_is_not_source_and_is_never_written():
    assert et.decide("python /workspace/app.py", "/workspace/app.py", path_exists=False) == et.RUN_PATH
    assert et.decide("/workspace/app.py", "/workspace/app.py", path_exists=False) == et.RUN_PATH
    assert not et.is_real_code("python3 x.py")
    assert not et.is_real_code("./run.sh")


def test_a_one_line_snippet_is_still_source():
    assert et.is_real_code("print(sum(i*i for i in range(1, 51)))")
    assert et.decide("print(1)", "/workspace/one.py", path_exists=False) == et.MATERIALISE


def test_no_path_runs_inline_and_nothing_is_nothing():
    assert et.decide(SCRIPT, "", path_exists=False) == et.RUN_INLINE
    assert et.decide(SCRIPT, None, path_exists=True) == et.RUN_INLINE
    assert et.decide("", "", path_exists=False) == et.NOTHING
    assert et.decide("   ", "  ", path_exists=False) == et.NOTHING


def test_quoted_path_is_the_same_path():
    assert et.decide(SCRIPT, "'/workspace/x.py'", path_exists=False) == et.MATERIALISE


def test_the_note_names_the_file_and_says_it_now_exists():
    n = et.materialised_note("/workspace/parse_chrome_webgpu.py")
    assert "/workspace/parse_chrome_webgpu.py" in n
    assert "now exists" in n
