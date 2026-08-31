"""The operator should be able to be pointed at a file, not just a URL.

Census run 18: two operator.run calls spent 21 minutes on Vera's own dashboard
hunting for a countdown timer that lived in timer.html, because no URL reached
them. Across twelve recorded runs the outcomes split on exactly that - every run
given a page finished; almost every run left to find one hit the ceiling.

The preview route already existed and works (verified live: a file written into
a session sandbox was fetched over HTTP). What was missing was a way to name the
FILE, which the caller already knows.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.operator.sandbox_file_target import (      # noqa: E402
    is_safe_relpath, looks_like_url, preview_url, resolve,
)

BASE = "https://localhost:8999"
SID = "chat-123"


def test_a_path_becomes_the_sandbox_preview_url():
    assert (preview_url(BASE, SID, "timer.html") ==
            "https://localhost:8999/remote/sandbox/preview/chat-123/timer.html")


def test_a_nested_path_survives():
    assert preview_url(BASE, SID, "site/index.html").endswith("/chat-123/site/index.html")


def test_a_leading_slash_is_tolerated():
    assert preview_url(BASE, SID, "/timer.html").endswith("/chat-123/timer.html")


def test_a_trailing_slash_on_the_base_does_not_double_up():
    assert "//remote" not in preview_url(BASE + "/", SID, "a.html")


# --- refusing what the route would refuse ----------------------------------

@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../b", "", "   ", "a//b", "./x"])
def test_traversal_and_empty_paths_are_refused(bad):
    assert is_safe_relpath(bad) is False
    assert preview_url(BASE, SID, bad) is None


def test_a_missing_session_yields_nothing():
    assert preview_url(BASE, "", "a.html") is None


def test_a_missing_base_yields_nothing():
    assert preview_url("", SID, "a.html") is None


# --- resolve ---------------------------------------------------------------

def test_an_explicit_url_always_wins():
    """Overriding a named URL is the same class of bug as dropping one."""
    r = resolve("https://example.com/x", "timer.html", base_url=BASE, session_id=SID)
    assert r["url"] == "https://example.com/x" and r["source"] == "url"


def test_a_path_is_resolved_when_no_url_is_given():
    r = resolve("", "timer.html", base_url=BASE, session_id=SID)
    assert r["source"] == "sandbox-path"
    assert r["url"].endswith("/chat-123/timer.html")


@pytest.mark.parametrize("val", ["https://x/y", "http://x", "file:///tmp/a.html"])
def test_a_url_passed_as_a_path_is_used_as_a_url(val):
    """Models conflate the two; treating a URL as a relative path produces a
    404 that reads like a missing file."""
    assert looks_like_url(val) is True
    r = resolve("", val, base_url=BASE, session_id=SID)
    assert r["url"] == val and r["source"] == "path-was-a-url"


def test_neither_url_nor_path_changes_nothing():
    r = resolve("", "", base_url=BASE, session_id=SID)
    assert r["url"] == "" and r["source"] == ""


def test_an_unresolvable_path_explains_itself_and_falls_back():
    r = resolve("", "timer.html", base_url=BASE, session_id="")
    assert r["url"] == ""
    assert "could not turn path" in r["note"] and "falling back" in r["note"]


# --- the callsite -----------------------------------------------------------

def test_operator_run_accepts_a_path_and_resolves_it():
    path = os.path.join(os.path.dirname(__file__), "..", "vera", "operator",
                        "operator_web_capabilities.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "path: str = \"\"" in src
    assert "_sfile.resolve(" in src
    imp = src.index("import sandbox_file_target as _sfile")
    use = src.index("_sfile.resolve(")
    assert imp < use


def test_the_think_option_reaches_the_model():
    """Each operator.think spent 35-60s on 560-946 tokens to pick one click."""
    for rel, needle in (
        (("vera", "operator", "thinker.py"), "think=think"),
        (("vera", "capabilities", "capabilities.py"), "think=think"),
    ):
        p = os.path.join(os.path.dirname(__file__), "..", *rel)
        with open(p, encoding="utf-8") as fh:
            assert needle in fh.read(), f"{rel[-1]} does not forward think"
