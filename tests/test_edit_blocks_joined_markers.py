"""Joined delimiter lines must not leak into a file (live fix check 2026-09-30:
`<<<END>>> <<<EDIT>>>` and `<<<END>>> <<<NOTE>>>` were written into timer.html's <script>)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("edit_blocks", ROOT / "vera" / "dag" / "edit_blocks.py")
EB = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EB)

REPLY = """<<<EDIT>>>
const duration = 60; // Duration in seconds
<<<REPLACE>>>
const duration = 90; // Duration in seconds
<<<END>>> <<<EDIT>>>
<title>60-Second Countdown Timer</title>
<<<REPLACE>>>
<title>90-Second Countdown Timer</title>
<<<END>>> <<<NOTE>>>
Updated the duration and title text as requested."""


def test_joined_markers_are_split_into_two_clean_edits():
    r = EB.parse(REPLY)
    assert r["error"] == ""
    assert r["edits"] == [
        {"find": "const duration = 60; // Duration in seconds",
         "replace": "const duration = 90; // Duration in seconds"},
        {"find": "<title>60-Second Countdown Timer</title>",
         "replace": "<title>90-Second Countdown Timer</title>"}]
    assert r["note"] == "Updated the duration and title text as requested."


def test_no_edit_text_ever_contains_a_marker():
    for e in EB.parse(REPLY)["edits"]:
        for m in (EB.EDIT, EB.FIND, EB.REPLACE, EB.END, EB.NOTE):
            assert m not in e["find"] and m not in e["replace"]


def test_a_marker_inside_real_text_refuses_the_edit_instead_of_writing_it():
    reply = ("<<<EDIT>>>\nconst x = 1;\n<<<REPLACE>>>\nconst x = 2; <<<END>>>\n"
             "<<<NOTE>>>\ndone")
    r = EB.parse(reply)
    assert r["edits"] == [] and "delimiter" in r["error"]


def test_a_line_that_merely_mentions_a_marker_word_is_still_content():
    reply = "<<<EDIT>>>\n// the END of it\n<<<REPLACE>>>\n// the end\n<<<END>>>"
    assert EB.parse(reply)["edits"] == [{"find": "// the END of it", "replace": "// the end"}]


def test_the_ordinary_format_is_unchanged():
    reply = "<<<EDIT>>>\nlet a = 1;\n<<<REPLACE>>>\nlet a = 2;\n<<<END>>>\n<<<NOTE>>>\nok"
    assert EB.parse(reply) == {"edits": [{"find": "let a = 1;", "replace": "let a = 2;"}],
                               "note": "ok", "error": ""}
