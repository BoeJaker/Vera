"""S24 / D02-2 + D02-3: a delivery happens when the request asks for one, and the channel's cap gets the content.

The explicit-ask patterns matched bare nouns: "how do I set up email on my phone?" mailed the answer, "my audio driver is
broken" queued a podcast, "the HTML canvas element" landed on the canvas. And the printer channel's cap (print.notify:
title, body) got {"text": ...}, which its wrapper drops, so it printed an empty note."""
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vera import delivery as D  # noqa: E402

ALL = ["canvas", "canvas_html", "html", "podcast", "email", "telegram", "notebook", "memory"]


@pytest.mark.parametrize("q", [
    "how do I set up email on my phone?",
    "what is a good email client for linux",
    "my audio driver is broken",
    "how do I draw on an HTML canvas element",
    "which notebook laptop should I buy",
    "do you remember that film with the robot",
    "is telegram end-to-end encrypted",
    "I like to listen to jazz",
    "my notes app crashed",
])
def test_a_mention_is_not_an_ask(q):
    assert D.explicit_ask(q, ALL) == "", q


@pytest.mark.parametrize("q,want", [
    ("email me the report", "email"),
    ("can you email it to bob", "email"),
    ("send the summary to my inbox", "email"),
    ("text me when it's done", "telegram"),
    ("ping it to my phone", "telegram"),
    ("send it over telegram", "telegram"),
    ("so I can listen to it later", "podcast"),
    ("make it a podcast", "podcast"),
    ("read it aloud", "podcast"),
    ("save this to my notebook", "notebook"),
    ("put the list in my notes", "notebook"),
    ("remember that the router is on .1", "memory"),
    ("please remember this: the NAS is VFS-02", "memory"),
    ("put it on the canvas", "canvas"),
    ("keep it somewhere I can see", "canvas"),
])
def test_an_ask_still_decides(q, want):
    assert D.explicit_ask(q, ALL) == want, q


def test_the_printer_gets_a_title_and_the_body():
    D.register_channel("printer", label="Thermal printer", cap="print.notify", default_format="plain",
                       needs_target=False, source="print")
    try:
        a = D.build_args("printer", "Backups done\nall green", {"label": "nightly"})
        assert a == {"title": "Dream: nightly", "body": "Backups done\nall green"}
    finally:
        D.unregister_channel("printer")


def test_a_skill_channel_reaches_the_argument_its_cap_takes(monkeypatch):
    reg = {"x.say": {"schema": {"type": "object", "properties": {"message": {}, "title": {}}}},
           "x.text": {"schema": {"type": "object", "properties": {"text": {}}}}}
    monkeypatch.setitem(sys.modules, "capability_orchestration", types.SimpleNamespace(CAPABILITY_REGISTRY=reg))
    D.register_channel("say", label="Say", cap="x.say")
    D.register_channel("txt", label="Txt", cap="x.text")
    D.register_channel("unknown", label="Unknown", cap="x.missing")
    try:
        assert D.build_args("say", "hello", {"label": "L"}) == {"message": "hello", "title": "Dream: L"}
        assert D.build_args("txt", "hello", {}) == {"text": "hello"}
        assert D.build_args("unknown", "hello", {}) == {"text": "hello"}       # not loaded here: as before
    finally:
        for c in ("say", "txt", "unknown"):
            D.unregister_channel(c)


def test_the_printer_registers_a_real_format():
    src = open(os.path.join(ROOT, "vera", "business", "thermal_printer_capabilities.py"), encoding="utf-8").read()
    assert 'cap="print.notify",\n            default_format="plain"' in src
    from vera import output_formats as F
    assert F.get_profile("plain") is not None
