"""A run that wrote one page must be pointed at it, not at Vera's dashboard.

2026-09-09, author-then-edit re-test (session 68b2a8d5). Three operator.run
calls. The first two were handed

    https://localhost:8999/remote/sandbox/preview/68b2a8d5-.../timer.html

The third was called with a goal and NOTHING else -- "Verify 90-second
countdown behavior". No url, no path, and no filename in the sentence for
goal_file to read. The target fell back to the orchestrator root and the run
spent its entire 480s budget observing

    'VERA localhost:8999/ws/mcp gpu-250 11ms cpu-246 14ms ... OLLAMA 0 active'

before stopping on time_budget. timer.html was in the workspace the whole time,
and the artifact it was meant to check was already CORRECT.

The nav pin is not at fault and must not be "fixed": is_pinnable is False for
the orchestrator root by design, so a run that legitimately browses the open web
stays free. The pin was never armed because there was no target to arm it with.

Pure: no browser, no session, no I/O.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import nav_fallback as NF                # noqa: E402
from vera.operator import nav_pin as NP                     # noqa: E402


# ── the census case ─────────────────────────────────────────────────────────
def test_the_one_page_the_run_wrote_is_the_target():
    """The re-test's actual workspace."""
    assert NF.sole_page(["timer.html"]) == "timer.html"


def test_assets_the_page_loads_are_not_pages():
    """A run writes a page plus the things it pulls in. Pointing a browser at
    a .js shows source, not a running interface - and a verification goal is
    always asking about the interface."""
    assert NF.sole_page(["timer.html", "style.css", "app.js", "data.json"]) == "timer.html"


def test_nothing_is_chosen_when_the_run_wrote_several_pages():
    """A wrong page is worse than no page: it reports as a real observation,
    so it looks like the check ran. Two answers is not an answer."""
    assert NF.sole_page(["timer.html", "index.html"]) == ""


def test_nothing_is_chosen_when_the_run_wrote_no_page():
    assert NF.sole_page(["notes.md", "fetch.py"]) == ""
    assert NF.sole_page([]) == ""
    assert NF.sole_page(None) == ""


# ── the listing is whatever artifact_list_files returned ────────────────────
def test_a_duplicate_listing_is_still_one_page():
    assert NF.sole_page(["timer.html", "timer.html"]) == "timer.html"


def test_blanks_and_junk_entries_are_ignored():
    assert NF.sole_page(["", None, "  ", "timer.html"]) == "timer.html"


def test_extensions_are_matched_case_insensitively_and_untrimmed():
    assert NF.sole_page(["  Timer.HTML  "]) == "Timer.HTML"
    assert NF.is_page("page.HTM") is True
    assert NF.is_page("page.js") is False
    assert NF.is_page("") is False


def test_candidates_keep_listing_order():
    assert NF.page_candidates(["b.html", "a.html", "b.html"]) == ["b.html", "a.html"]


# ── the inference must be legible ───────────────────────────────────────────
def test_the_note_says_the_target_was_inferred():
    """The caller passed no url, no path and named no file, so the target is a
    guess from context and the log must not read as if it was told."""
    note = NF.target_note("timer.html")
    assert "timer.html" in note
    assert "one page this run wrote" in note


def test_no_note_without_a_target():
    assert NF.target_note("") == ""


# ── the pin still binds afterwards, and the web stays free ──────────────────
def test_a_resolved_page_becomes_a_pinnable_preview_url():
    """The point of resolving a target: once it is a sandbox preview URL the
    existing nav pin binds, which is what keeps the run ON the file."""
    resolved = "https://localhost:8999/remote/sandbox/preview/68b2a8d5/timer.html"
    assert NP.is_pinnable(resolved) is True


def test_the_dashboard_is_still_not_pinnable():
    """Regression fence: this fix must not be 'solved' by pinning the root."""
    assert NP.is_pinnable("https://localhost:8999/") is False


# ── what may be ENFORCED is narrower than where we start ────────────────────
PREVIEW = "https://localhost:8999/remote/sandbox/preview/68b2a8d5/timer.html"


def test_a_told_target_is_pinned():
    assert NF.pin_for(PREVIEW, False, NP.is_pinnable) == PREVIEW


def test_an_inferred_target_is_never_pinned():
    """The regression this nearly shipped: a resolved preview URL IS pinnable,
    so inferring a target and then pinning it would trap a run that really did
    mean the open web on a local file it never asked for - worse than the
    dashboard. Start there, do not hold it there."""
    assert NF.pin_for(PREVIEW, True, NP.is_pinnable) == ""


def test_an_unpinnable_target_is_never_pinned_either_way():
    for inferred in (True, False):
        assert NF.pin_for("https://en.wikipedia.org/wiki/Timer", inferred,
                          NP.is_pinnable) == ""
        assert NF.pin_for("", inferred, NP.is_pinnable) == ""
