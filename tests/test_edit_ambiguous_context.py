"""An anchor that matches several places is answered with the line above each match
(prod 30 Sep - 3 Oct: the largest group of code.edit failures, 19 of 57 parts)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eah", ROOT / "vera" / "dag" / "edit_anchor_hint.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

FILE = """def load():
    try:
        return read()

def save():

    try:
        return write()
"""


def test_each_match_with_the_nearest_line_above_it():
    c = H.ambiguous_context(FILE, "    try:\n")
    assert c.startswith("where each match sits - ")
    assert "line 2 (line above, 1: 'def load():')" in c
    assert "line 7 (line above, 5: 'def save():')" in c          # skips the blank line


def test_nothing_for_a_unique_or_missing_anchor_and_bounded():
    assert H.ambiguous_context(FILE, "def load():") == ""
    assert H.ambiguous_context(FILE, "nope") == ""
    assert H.ambiguous_context("x\n" * 50, "x", limit=3).count("line above") == 3
    assert "(start of file)" in H.ambiguous_context("a\na\n", "a")


def test_the_edit_error_carries_it():
    i = SRC.index("`find` matches {n} places (lines ")
    block = SRC[i - 600:i + 600]
    assert "_edit_anchor_hint.ambiguous_context(out, find)" in block
    assert "adjacent unique line" in block                          # the old sentence stays
