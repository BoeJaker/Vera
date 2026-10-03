"""The stepwise-reviewed critic: an empty labelled review is no flag, MISSING is about this
step's own job, and the file evidence is long enough for a report (3 Oct: 81 of 86 reviews
counted as flags; ~20 named work later steps were for; "truncated at 1500 chars")."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.planning import planner_styles as PS  # noqa: E402


def test_an_empty_labelled_review_is_no_flag():
    for t in ("MISSING: None. The goal is satisfied by the output `root`.\nWRONG: None.\nNEXT: OK",
              "- MISSING: none\n- WRONG: N/A\n- NEXT: nothing",
              "MISSING: None\nWRONG: None\nNEXT: OK"):
        assert PS.critic_note(t) == "", t


def test_real_content_survives_including_none_but():
    n = PS.critic_note("MISSING: None\nWRONG: None in terms of facts, but the timer was never changed\nNEXT: edit timer.html")
    assert n.splitlines() == ["  - WRONG: None in terms of facts, but the timer was never changed",
                              "  - NEXT: edit timer.html"]
    assert PS.critic_note("MISSING: the list of the five largest files") != ""


def test_missing_is_about_this_step():
    s = PS.CRITIC_SYSTEM
    assert "or that no step has covered yet" not in s
    assert "THIS step's own job" in s and "Work a LATER step is for is not missing" in s


def test_evidence_fits_a_report():
    assert PS.CRITIC_FILE_CHARS >= 4000 and PS.CRITIC_EVIDENCE_CHARS >= 2 * PS.CRITIC_FILE_CHARS
    ev = PS.critic_evidence([("report.md", "x" * 3800)], [])
    assert "first" not in ev and len(ev) >= 3800
