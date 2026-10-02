"""operator.run's target file (live 2026-10-02: failed live runs had no usable url -
the file was named in the goal, or given as a filesystem path)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ouc", ROOT / "vera" / "dag" / "operator_url_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
pick = C.pick_preview_file
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_file_named_in_the_goal_with_no_url():
    # 154db5a7d8
    assert pick("", "Load timer_app.html from local HTTP server at port 8080 (sandbox mode)",
                ["timer_app.py", "timer_app.html", "notes.md"]) == ("timer_app.html", "named in goal")


def test_page_named_by_its_stem():
    # 61073fd42c
    assert pick("", "Navigate to the timer page and observe the countdown display",
                ["index.html", "timer.html"])[0] == "timer.html"


def test_a_filesystem_path_as_the_url():
    # 3ad3229ac5
    assert pick("/workspace/answer.html", "Render it", ["answer.html"]) == ("answer.html", "path url")
    assert pick("file:///workspace/site/a.html", "", ["site/a.html"])[0] == "site/a.html"
    assert pick("answer.html", "", [])[0] == "answer.html"


def test_only_one_page_is_used_when_nothing_is_named():
    assert pick("", "check the countdown works", ["clock.html", "x.py"]) == ("clock.html", "only page")


def test_ambiguous_or_foreign_calls_are_left_alone():
    assert pick("", "check the page", ["a.html", "b.html"]) == ("", "")
    assert pick("https://example.com/x", "timer.html", ["timer.html"]) == ("", "")
    assert pick("https://localhost:8999/remote/sandbox/preview/s/t.html", "", ["t.html"]) == ("", "")
    assert pick("", "open the census panel", ["timer.html"], kind="panel") == ("", "")
    assert pick("", "x", []) == ("", "")
    assert pick("notes.md", "", ["notes.md"]) == ("", "")


def test_browser_navigate_keeps_the_old_gate():
    assert pick("", "timer.html", ["timer.html"], operator=False) == ("", "")
    assert pick("", "timer.html", ["timer.html"], operator=False, ui_verify_step=True)[0] == "timer.html"


def test_short_stems_never_match_by_word():
    assert pick("", "open a page", ["a.html", "b.html"]) == ("", "")


def test_the_loop_uses_it_for_missing_and_path_urls():
    i = SRC.index("# ── UI-verify preview-URL self-heal")
    block = SRC[i:i + 9000]
    assert "_operator_url_core.pick_preview_file(" in block
    assert "elif not _u and _V5_UI_VERIFY_STEP_RE.search" not in block
