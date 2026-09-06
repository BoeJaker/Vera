"""A test file must be written against the file it tests.

Census 39, build-multifile. code.author produced test_stats.py at cycle 4 and
only read stats.py at cycle 5 - tests written before the implementation was
looked at. The author guessed the contract:

    assert calculate_mode([-1, 2, -3, 4, -5, 2]) == [2]
    E   assert 2 == [2]

stats.py returns the mode as a SCALAR, the tests assert a LIST. Seven failures,
all that one mismatch, and roughly twenty cycles spent re-running pytest against
it without ever reconciling the halves.

prose.author already auto-attaches its source data when the caller names none -
"grounded in them by default, not by luck". This is the same rule for the
write-a-test case, and the tests below pin the narrowness as hard as the
behaviour: attaching the WRONG file would be worse than attaching none.

Pure: the caller supplies the listing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag import test_target as TT                     # noqa: E402


# ── recognising a test file ─────────────────────────────────────────────────
def test_the_census_39_file_is_recognised():
    assert TT.is_test_path("statkit/test_stats.py") is True


def test_both_naming_conventions_are_recognised():
    assert TT.is_test_path("test_stats.py") is True
    assert TT.is_test_path("stats_test.py") is True


def test_an_ordinary_module_is_not_a_test_file():
    assert TT.is_test_path("statkit/stats.py") is False
    assert TT.is_test_path("main.py") is False


def test_a_name_that_merely_contains_test_is_not_a_test_file():
    """`latest_prices.py` and `contest.py` must not be treated as tests."""
    assert TT.is_test_path("latest_prices.py") is False
    assert TT.is_test_path("contest.py") is False


def test_non_python_files_are_left_alone():
    """The naming convention is only unambiguous where this says it is."""
    assert TT.is_test_path("test_page.html") is False
    assert TT.is_test_path("test_thing.js") is False


# ── deriving the module under test ──────────────────────────────────────────
def test_the_target_is_derived_from_either_convention():
    assert TT.module_under_test("statkit/test_stats.py") == "stats.py"
    assert TT.module_under_test("stats_test.py") == "stats.py"


def test_a_non_test_path_has_no_target():
    assert TT.module_under_test("stats.py") == ""


def test_a_bare_prefix_is_not_a_target():
    """`test_.py` names nothing."""
    assert TT.module_under_test("test_.py") == ""


# ── choosing what to attach ─────────────────────────────────────────────────
def test_the_implementation_is_attached_when_it_exists():
    """The census 39 case, with the listing the run actually had."""
    got = TT.pick_context("statkit/test_stats.py",
                          ["statkit/stats.py", "statkit/test_stats.py"])
    assert got == ["statkit/stats.py"]


def test_it_matches_across_directories():
    """Tests in tests/, implementation in the package."""
    got = TT.pick_context("tests/test_stats.py", ["statkit/stats.py", "README.md"])
    assert got == ["statkit/stats.py"]


def test_nothing_is_attached_when_the_target_is_absent():
    """A guess is worse than nothing: it grounds the author in something
    irrelevant while looking like it worked."""
    assert TT.pick_context("test_stats.py", ["other.py", "README.md"]) == []


def test_nothing_is_attached_for_an_ordinary_file():
    assert TT.pick_context("stats.py", ["stats.py"]) == []


def test_an_unknown_listing_attaches_nothing():
    """None means the directory could not be read - not that it is empty."""
    assert TT.pick_context("test_stats.py", None) == []


def test_directories_are_never_attached():
    assert TT.pick_context("test_stats.py", ["stats.py/"]) == []


def test_the_attachment_is_capped():
    many = ["pkg%d/stats.py" % i for i in range(10)]
    assert len(TT.pick_context("test_stats.py", many)) <= TT.MAX_ATTACHED


# ── what the author is told ─────────────────────────────────────────────────
def test_the_prompt_line_names_the_file_and_the_rule():
    d = TT.describe("test_stats.py", ["statkit/stats.py"])
    assert "statkit/stats.py" in d
    assert "scalar" in d and "list" in d, "the exact mistake that was made"


def test_nothing_is_said_when_nothing_was_attached():
    assert TT.describe("test_stats.py", []) == ""
