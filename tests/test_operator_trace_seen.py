"""The persisted trace must show what the page displayed.

A gap in my own earlier change. run_loop records `seen` on every act step and
stop_explanation quotes it to the caller, so the AGENTIC LOOP was told what the
page showed - but two things on the way to the stored trace dropped it:

  * operator_web_capabilities._on_step built the recorded event from a fixed
    field list that did not include `seen`;
  * operator_trace_core.digest_events built each step from its own whitelist,
    also without it.

So `operator.trace` reported seen="" on every step of every run. Census 38's
author-then-edit is exactly why that matters: the operator read 01:30 on load
and 01:00 after Reset - the real defect in the artifact, and the only evidence
that the page was inconsistent rather than the operator being confused - and
none of it survived into the trace a human or a later run would read.

Pure: dicts in, dicts out.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator.operator_trace_core import digest_events   # noqa: E402


def _step(i, **kw):
    base = {"type": "operator.step", "i": i, "phase": "act",
            "action": "click", "args": {"ref": "e1"},
            "url": "http://x/timer.html"}
    base.update(kw)
    return base


def test_the_displayed_text_survives_into_the_digest():
    d = digest_events([_step(1, seen="01:30")])
    assert d["steps"][0]["seen"] == "01:30"


def test_the_census_38_sequence_is_readable_from_the_trace():
    """Load shows 01:30, Reset shows 01:00. That pair IS the bug report."""
    d = digest_events([_step(1, seen="01:30"), _step(2, action="click",
                                                     args={"ref": "e3"},
                                                     seen="01:00")])
    assert [s["seen"] for s in d["steps"]] == ["01:30", "01:00"]


def test_a_step_without_it_reports_empty_not_missing():
    """Runs recorded before this landed simply have no `seen`; the key must
    still be present so readers do not have to guess whether it is absent or
    unobserved."""
    d = digest_events([_step(1)])
    assert d["steps"][0]["seen"] == ""


def test_the_quoted_text_is_bounded():
    d = digest_events([_step(1, seen="x" * 5000)])
    assert len(d["steps"][0]["seen"]) <= 250


def test_the_other_step_fields_are_untouched():
    """This adds a field; it must not disturb the ones the UI already reads."""
    d = digest_events([_step(1, seen="01:30", thought="t", url="http://x/a")])
    s = d["steps"][0]
    assert s["action"] == "click" and s["args"] == {"ref": "e1"}
    assert s["url"] == "http://x/a" and s["thought"] == "t"
    assert s["phase"] == "act" and s["i"] == 1
