"""Do not tell a step to go and look at what it has just been shown.

Census 36, `underspecified` ("Make the timer better", empty workspace, wall-cap
at 1802s). Thirteen executor cycles went on discovering the directory was empty:
ten different shell probes in step 1, three more in a controller-inserted
"Discover existing files", and one more in a failure-recovery "Discover and Read
... via Shell". Only ONE of the ten was flagged a duplicate, so the loop's
duplicate-call guard was right not to fire - they were genuinely different
commands.

The step context was not silent. It said "It is EMPTY so far - nothing has been
written yet this run", and eleven lines later "Need to know what's on disk? Run
`ls -la` with exec.bash.run". Both, every time. Faced with a fact and an
instruction that contradicts it, the model followed the instruction.

Pure: no loop, no sandbox, no LLM.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag import workdir_note as WN                   # noqa: E402


# ── the contradiction ───────────────────────────────────────────────────────
def test_a_known_listing_is_never_told_to_go_looking():
    """The census 36 case: the directory is known, so nothing is left to find."""
    assert WN.invites_listing(WN.disk_advice(["timer.html"], has_bash=True)) is False


def test_a_known_EMPTY_listing_is_never_told_to_go_looking():
    """[] is an answer. This is the exact state `underspecified` was in while it
    ran ten shell probes."""
    assert WN.invites_listing(WN.disk_advice([], has_bash=True)) is False


def test_an_unknown_listing_still_says_how_to_look():
    """None is the one state where looking buys something, and the advice must
    survive - removing it outright would be a different bug."""
    advice = WN.disk_advice(None, has_bash=True)
    assert WN.invites_listing(advice) is True


def test_without_bash_an_unknown_listing_points_at_the_read_cap():
    advice = WN.disk_advice(None, has_bash=False, read_cap="sandbox.session.fs.read")
    assert "sandbox.session.fs.read" in advice
    assert WN.invites_listing(advice) is False


def test_a_known_listing_ignores_whether_bash_is_available():
    """Having a shell is not a reason to re-derive a fact already supplied."""
    with_bash = WN.disk_advice(["a.html"], has_bash=True)
    without = WN.disk_advice(["a.html"], has_bash=False)
    assert with_bash == without


def test_the_known_case_says_not_to_re_check():
    advice = WN.disk_advice([], has_bash=True)
    assert "do not run" in advice.lower() or "not to re-check" in advice.lower()


# ── the three states are not two ────────────────────────────────────────────
def test_empty_and_unknown_produce_different_advice():
    """The collapse that caused this: [] and None must not be interchangeable."""
    assert WN.disk_advice([], has_bash=True) != WN.disk_advice(None, has_bash=True)


def test_an_empty_workspace_is_stated_as_a_fact_to_the_author():
    block = WN.author_files_block([])
    assert "EMPTY" in block and "self-contained" in block


def test_an_unknown_workspace_says_nothing_to_the_author():
    """Claiming emptiness off a probe that failed would be a lie."""
    assert WN.author_files_block(None) == ""


def test_a_populated_workspace_lists_its_real_files():
    block = WN.author_files_block(["timer.html", "notes.md"])
    assert "timer.html" in block and "notes.md" in block
    assert "EMPTY" not in block


def test_the_author_listing_is_capped():
    block = WN.author_files_block(["f%d.txt" % i for i in range(200)], limit=5)
    assert block.count(".txt") == 5


def test_blank_entries_do_not_fake_a_populated_directory():
    """A listing of empty strings is an empty directory, not a populated one."""
    assert "EMPTY" in WN.author_files_block(["", "   "])


# ── the detector itself ─────────────────────────────────────────────────────
def test_invites_listing_recognises_the_sentence_that_caused_this():
    assert WN.invites_listing(
        "  - Need to know what's on disk? Run `ls -la` with exec.bash.run") is True


def test_invites_listing_is_not_fooled_by_a_mention_of_files():
    assert WN.invites_listing("The listing above is current.") is False
    assert WN.invites_listing("") is False
