"""A trace that records the banner and drops the verdict is not a record.

The loop trace clipped a failed call's error to the first 160 characters. For
pytest that is exactly the useless part - census 39's build-multifile stored
this, and only this, for every one of its failing runs:

    ============================= test session starts ===============================
    platform linux -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/…

while the cause (`assert 2 == [2]`, then `7 failed, 13 passed`) was hundreds of
characters further down and had to be recovered by re-running pytest by hand in
the run's sandbox. pytest, tracebacks and compilers all put the diagnosis at the
END, so a leading clip is the wrong shape of truncation.

Pure: strings in, strings out.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag import error_excerpt as EE                   # noqa: E402

PYTEST = (
    "=" * 30 + " test session starts " + "=" * 30 + "\n"
    "platform linux -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0\n"
    + ("collecting ... collected 20 items\n" * 60) +
    "    def test_calculate_mode_mixed_integers():\n"
    ">       assert calculate_mode([-1, 2, -3, 4, -5, 2]) == [2]\n"
    "E       assert 2 == [2]\n"
    "=========================== short test summary info ============================\n"
    "7 failed, 13 passed in 0.08s\n"
)


def test_the_verdict_survives():
    """The line that actually diagnoses the run."""
    got = EE.excerpt(PYTEST)
    assert "7 failed, 13 passed" in got
    assert "assert 2 == [2]" in got


def test_the_banner_survives_too():
    """The head says WHAT ran; dropping it would trade one blind spot for
    another."""
    assert "test session starts" in EE.excerpt(PYTEST)


def test_the_old_clip_would_have_lost_the_verdict():
    """Pins the regression this exists to prevent: 160 leading characters
    contains the banner and nothing else."""
    assert "7 failed, 13 passed" not in PYTEST[:160]


def test_the_omission_is_stated():
    got = EE.excerpt(PYTEST)
    assert EE.is_truncated(got)
    assert "characters omitted" in got


def test_short_text_is_returned_untouched():
    """Anything that already fitted must be unchanged."""
    for s in ("", "boom", "missing required argument: path"):
        assert EE.excerpt(s) == s
        assert EE.is_truncated(EE.excerpt(s)) is False


def test_none_becomes_empty_not_the_word_none():
    assert EE.excerpt(None) == ""


def test_a_non_string_is_rendered():
    assert EE.excerpt(404) == "404"


def test_the_excerpt_is_bounded():
    got = EE.excerpt("x" * 100_000)
    assert len(got) < EE.HEAD + EE.TAIL + 120


def test_the_head_and_tail_are_the_real_ends():
    """The tail segment must be at least TAIL long, or part of the middle
    legitimately rides along inside it - which is what an earlier version of
    this test mistook for a bug in the code."""
    got = EE.excerpt("A" * 500 + "M" * 5000 + "Z" * (EE.TAIL + 100))
    assert got.startswith("A")
    assert got.endswith("Z")
    assert "M" * 100 not in got, "the skippable middle is what goes"


def test_a_tail_of_zero_keeps_only_the_head():
    got = EE.excerpt("A" * 500 + "Z" * 500, head=10, tail=0)
    assert got.startswith("A" * 10)
    assert "Z" not in got
