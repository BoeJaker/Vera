"""Podcast scripts may carry speech markup: the voice reads it, every transcript,
the metadata and the UI show the written form."""
import ast
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC_PATH = os.path.join(ROOT, "vera", "podcast", "podcast_capabilities.py")


def _helpers():
    src = open(SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    want = {"_markup_reader", "written_form", "split_spoken"}
    body = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in want)
            or (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_MARKUP")]
    ns = {"re": re, "os": os, "log": __import__("logging").getLogger("t"), "List": list, "Any": object,
          "Dict": dict, "__file__": SRC_PATH}
    flags = __import__("__future__").annotations.compiler_flag
    exec(compile(ast.Module(body=body, type_ignores=[]), SRC_PATH, "exec", flags=flags), ns)
    return src, ns


def test_markup_is_spoken_and_the_written_form_is_shown():
    _, ns = _helpers()
    segs = [{"speaker": "A", "text": 'Arsenal won <sub alias="three nil">3-0</sub>.<break time="300ms"/>'},
            {"speaker": "B", "text": "Plain line."}]
    out = ns["split_spoken"](segs)
    assert out[0]["spoken"].startswith("Arsenal won <sub") and out[0]["text"] == "Arsenal won 3-0."
    assert "spoken" not in out[1] and out[1]["text"] == "Plain line."
    again = ns["split_spoken"](out)                     # a script fed back in is not split twice
    assert again[0]["text"] == "Arsenal won 3-0." and again[0]["spoken"] == out[0]["spoken"]


def test_without_the_edge_reader_tags_are_still_stripped():
    _, ns = _helpers()
    ns["_MARKUP"][:] = [None]
    assert ns["written_form"]('The <say-as interpret-as="characters">FOMC</say-as> met') == "The FOMC met"


def test_the_writer_is_told_and_the_voice_reads_spoken():
    src, _ = _helpers()
    assert "SPEECH_MARKUP_GUIDE" in src[src.index("def _generate_script"):]
    assert 'seg.get("spoken") or seg["text"]' in src
    assert 'segs = split_spoken(script["segments"])' in src
