"""Thermal-printer line fitting.

Paper does not re-flow: the raster renderer draws into a bitmap exactly as wide
as the print head, and the printer clips whatever runs past the edge. Nothing
downstream of escpos_core can recover a line that was too long, so a regression
here is SILENT — the job prints, the paper comes out, and the text is simply
missing. That is how morning news headlines were being truncated mid-sentence.
"""
from vera.printer.escpos_core import (
    wrap_measured, wrap_cols, is_rule, fit_rule,
)

# A stand-in for a proportional TTF: every char is 10 units wide.
def px(s, unit=10):
    return len(s) * unit


def test_long_headline_is_wrapped_not_truncated():
    """The reported bug: an HN headline far wider than 58 mm paper."""
    headline = ("* LG smart TVs caught logging audio with screen off and "
                "snooping on local devices")
    out = wrap_cols(headline, 32)
    assert len(out) > 1, "a 79-char headline must not stay on one 32-col line"
    assert all(len(l) <= 32 for l in out), out
    # Nothing is lost — every word survives, in order.
    assert " ".join(" ".join(out).split()) == " ".join(headline.split())


def test_every_line_fits_the_measured_width():
    lines = ["Vera thermal printer",
             "a much longer line that certainly does not fit on the paper at all",
             "short"]
    out = wrap_measured(lines, px, 200)          # 200/10 = 20 chars
    assert out, "wrapping must not swallow the input"
    for l in out:
        assert px(l) <= 200, repr(l)


def test_blank_lines_are_preserved_as_spacing():
    out = wrap_cols("News - HN front page\n\n* one\n* two", 32)
    assert out == ["News - HN front page", "", "* one", "* two"]


def test_bullet_continuations_hang_under_the_text():
    out = wrap_cols("* alpha beta gamma delta epsilon zeta", 16)
    assert out[0].startswith("* ")
    assert len(out) > 1
    # continuation rows are indented past the bullet, not restarted at the margin
    assert all(l.startswith("  ") for l in out[1:]), out
    assert all(len(l) <= 16 for l in out), out


def test_overlong_word_is_split_not_clipped():
    url = "https://example.com/" + "a" * 90
    out = wrap_cols(url, 24)
    assert all(len(l) <= 24 for l in out), out
    assert "".join(out) == url, "a hard-split URL must lose no characters"


def test_overlong_word_terminates_even_when_wider_than_the_paper():
    """A single char wider than the whole line must still make progress rather
    than spin forever (the hard-split loop's guard)."""
    out = wrap_measured(["xxxx"], lambda s: len(s) * 100, 10)
    assert out and "".join(out) == "xxxx"
    assert len(out) == 4


def test_rules_are_refitted_not_wrapped_into_ragged_rows():
    """print.push prefixes '=' * 28; at a proportional font that overflows and
    would otherwise wrap into a second, stubby row of '='."""
    assert is_rule("=" * 28)
    assert is_rule("-" * 32)
    assert not is_rule("== not a rule ==")
    assert not is_rule("--")                      # too short to be a separator
    out = wrap_measured(["=" * 28], px, 200)
    assert out == ["=" * 20], out                 # exactly one full-width rule


def test_fit_rule_fills_the_width():
    assert fit_rule("----", px, 200) == "-" * 20
    assert fit_rule("=" * 3, px, 55) == "=" * 5   # 5*10=50 fits, 6*10=60 does not


def test_already_short_text_is_untouched():
    lines = ["Today", "- 09:00-10:00  Standup   @ Zoom"]
    assert wrap_measured(lines, len, 40) == lines


def test_wrap_handles_empty_and_none_input():
    assert wrap_cols("", 32) == [""]
    assert wrap_measured([], px, 100) == []
    assert wrap_measured([None], px, 100) == [""]


def test_crlf_is_normalised():
    assert wrap_cols("a\r\nb\rc", 32) == ["a", "b", "c"]
