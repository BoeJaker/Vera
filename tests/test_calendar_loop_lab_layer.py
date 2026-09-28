"""The Comms calendar shows Loop Lab's schedule as its own layer.

cal.events.list could always overlay the Loop Lab schedules (include_loop_lab), but the Calendar panel - the one the
Comms panel embeds - never asked for them, and would have filed them under Dreams (every read-only event went
there). Owner, 2026-09-28: integrate the scheduler with the calendar UI. Pins the page by its source.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "..", "vera", "calendar", "calendar_panel.html")


def _src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def test_loop_lab_is_its_own_layer_and_is_fetched():
    s = _src()
    assert "&include_loop_lab=1" in s and "function overlayQs()" in s
    assert s.count("overlayQs()") >= 5                    # the helper, and every /cal/events read
    assert 'include_dreams=1":"")+' in s                 # the dreams overlay is still asked for
    el = s[s.index("function eventLayer(e)"):s.index("function layerLabel(")]
    # the Loop Lab test comes BEFORE the read-only one, or its windows would sit under Dreams
    assert el.index('"looplab"') < el.index("e.read_only")
    assert 'map.set("looplab"' in s


def test_a_loop_lab_event_opens_loop_lab():
    s = _src()
    op = s[s.index("function openEvent(id,startISO)"):]
    assert "loop-lab.goto" in op and 'openPlace("evolve")' in op
