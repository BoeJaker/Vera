"""rich_cap_signature shows a cap's FULL description, not a mid-sentence chop.

A capability's description is its usage CONTRACT (required args, meaning, an
example). It used to be hard-cut to 300 chars mid-sentence in the loop's tool
signature, which chopped the contract off the end — e.g. code.author's
'path AND task required' landed past the cut, so the model saw every param as
optional and its calls fumbled. Now the full description shows up to a generous
safety ceiling, and only a pathologically long one is trimmed — at a WORD
boundary, never mid-sentence.

Imports the monolith module, so it runs in-container.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def _with_fake_cap(monkeypatch, desc):
    fake = {"description": desc, "schema": {"properties": {"path": {}, "task": {}},
                                            "required": []}}
    monkeypatch.setitem(W.CAPABILITY_REGISTRY, "x.test.cap", fake)


def test_a_normal_description_is_shown_in_full(monkeypatch):
    # A 500-char contract (longer than the old 300 cut) must appear COMPLETE.
    desc = ("CALL x.test.cap(path='<f>', task='<spec>'). path AND task are REQUIRED. "
            + "detail " * 60).strip()   # ~500 chars
    _with_fake_cap(monkeypatch, desc)
    sig = W.rich_cap_signature("x.test.cap")
    assert "path AND task are REQUIRED" in sig
    assert desc.split(".")[0] in sig            # the contract head is present
    assert " …" not in sig                       # nothing trimmed at this length


def test_pathological_length_trims_at_a_word_boundary(monkeypatch):
    desc = "word " * 400   # 2000 chars, well over the ceiling
    _with_fake_cap(monkeypatch, desc)
    sig = W.rich_cap_signature("x.test.cap")
    line = [l for l in sig.splitlines() if l.strip().startswith("→")][0]
    shown = line.split("→", 1)[1]
    assert shown.rstrip().endswith("…")          # trimmed with an ellipsis
    assert "wor\n" not in shown and "wor …" not in shown  # not mid-word
    # every kept token is a whole word
    assert all(tok == "word" for tok in shown.replace("…", "").split())


def test_code_author_contract_survives(monkeypatch):
    # The real cap: its 'path AND task are REQUIRED' contract must be visible.
    sig = W.rich_cap_signature("code.author")
    assert "REQUIRED" in sig
    assert "code.author(path=" in sig
