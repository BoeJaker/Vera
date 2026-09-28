"""1,236 seconds failing to edit a file that was already correct.

Census run62, prose-only (2026-09-22). The goal scored 1.0 on all three quality
checks - the artifact was fine the whole time - and still spent 1,471s against
235s the run before. The wasted time was `code.edit` missing its anchor over and
over, because the editor had copied the line-numbered view back AS A MARKDOWN
TABLE:

    ### 2. Caching Example
    | 18 |
    | 19 | A cache stores frequently accessed data

Vera renders the view as ``"%5d | %s"`` (`_v5_numbered`) - no leading pipe. The
leading pipe is the editor's own embellishment, and BOTH gutter strippers (the
block parser's and the loop's late fallback) matched only the shape Vera emits,
so the gutter survived and `find` could never match. The error even said so:
"Closest text actually in the file - line 12: 'Imagine two users withdrawing
funds simultaneously from...'" - the anchor's own words were right there.

Pure: strings in, strings out.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.dag import edit_gutter_core as G                       # noqa: E402

#: What `_v5_numbered` actually emits.
VERA_VIEW = "   17 | ### 2. Caching Example\n   18 | \n   19 | A cache stores data"

#: What run62's editor sent back as the anchor.
RUN62_ANCHOR = "### 2. Caching Example\n| 18 | \n| 19 | A cache stores data"

#: The file those lines came from.
FILE = "### 2. Caching Example\n\nA cache stores data"


def test_the_run62_anchor_is_recognised_as_guttered():
    """The exact shape that cost the goal 1,236s."""
    assert G.looks_guttered("| 19 | A cache stores data")
    assert G.looks_guttered("| 18 | ")          # a guttered BLANK line
    assert G.strip_text(RUN62_ANCHOR) == FILE


def test_the_shape_vera_emits_still_strips():
    assert G.looks_guttered("   98 | function setMode(newMode) {")
    assert G.strip_text(VERA_VIEW) == FILE
    assert G.strip_line("    1 | for i in range(1, 4):") == "for i in range(1, 4):"


def test_content_that_merely_contains_a_pipe_is_untouched():
    for line in ("cat x | grep y",
                 "| Name | Count |",          # a table header: no number first
                 "value | 12",                 # number is not at the start
                 "  indented code | pipe",
                 ""):
        assert G.strip_line(line) == line, line


def test_a_number_alone_is_not_a_gutter():
    """It takes the pipe as well - otherwise an ordinary numbered list line
    ("3. Both compute new balances") would be mangled."""
    assert not G.looks_guttered("3. Both compute new balances")
    assert not G.looks_guttered("18")
    assert G.strip_line("18 no pipe here") == "18 no pipe here"


def test_uniform_only_stripping_is_unchanged_for_the_parser():
    """The parse-time caller has no file to check against, so it strips only
    when EVERY non-blank line is guttered - one `12 | x` among ordinary lines
    is more likely a table row, and leaving it alone fails safely."""
    mixed = ["  1 | a", "plain line"]
    assert G.strip_uniform(mixed) == mixed
    uniform = ["  1 | a", "", "  2 | b"]
    assert G.strip_uniform(uniform) == ["a", "", "b"]
    piped = ["| 1 | a", "| 2 | b"]
    assert G.strip_uniform(piped) == ["a", "b"]
    assert G.strip_uniform([]) == []
    assert G.strip_uniform(["", "  "]) == ["", "  "]


def test_only_one_gutter_is_removed_per_line():
    """`| 19 | | 20 | x` is not two gutters; stripping greedily would eat real
    content."""
    assert G.strip_line("| 19 | | 20 | x") == "| 20 | x"


def test_both_call_sites_share_this_definition():
    """The bug was two copies of one pattern drifting apart."""
    from vera.dag import edit_blocks as EB
    assert EB._GUTTER_RE is G.GUTTER_RE
    assert EB._strip_gutter(["| 1 | a", "| 2 | b"]) == ["a", "b"]
