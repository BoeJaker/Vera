"""A failed chain hop must poison what USES it - not the whole pipeline.

`_run_chain` did `if not entry_ok: break`, so one broken hop abandoned every
later hop, including ones that never referenced it. In a pipeline like

    0: web.search   1: web.fetch($0)   2: prose.author($1)   3: notes.write("...")

a failure at hop 1 also threw away hop 3, which was independent - and the loop
then spent later cycles rediscovering the part that would have worked.

Poison is transitive: a hop skipped for needing a failed hop has no output
either, so anything referencing IT must be skipped too.

Pure - no app import, no I/O.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag.chain_deps import blocked_by, hop_references, referenced_indices  # noqa: E402


# â”€â”€ reference extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@pytest.mark.parametrize("value,expected", [
    ("$0", {0}),
    ("$12", {12}),
    ("$1:code", {1}),                       # the :code form
    ("$2:json", {2}),                       # the :json form
    ("$3.a.b", {3}),                        # dict-path walk
    ("prefix $0 and $2 after", {0, 2}),
    ("no refs here", set()),
    ("", set()),
    ("costs $5.00 usd", {5}),               # documented below
])
def test_reference_forms_are_recognised(value, expected):
    assert referenced_indices(value) == expected


def test_a_dollar_amount_is_read_as_a_reference():
    """Pinned, not desired: '$5.00' looks exactly like the `$N.path` form.

    The consequence is conservative - a hop might be skipped when it did not
    truly depend on hop 5 - which loses work but never runs a hop against
    MISSING input. Left as-is deliberately: tightening the pattern risks the
    opposite error, and _chain_ref itself has the same ambiguity.
    """
    assert referenced_indices("costs $5.00 usd") == {5}


def test_refs_are_found_in_nested_structures():
    """A reference can sit in a nested arg, not just at the top level."""
    assert referenced_indices({"body": {"text": "$2"}, "n": [1, "$3"]}) == {2, 3}


# â”€â”€ hop dependencies â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_from_map_when_guard_and_args_all_count():
    """All three are resolved through _chain_ref, so all three can carry a $N."""
    assert hop_references({"name": "x", "from": {"text": "$1"}}) == {1}
    assert hop_references({"name": "x", "when": "$2"}) == {2}
    assert hop_references({"name": "x", "args": {"path": "$3"}}) == {3}


def test_the_tool_name_is_not_a_data_dependency():
    assert hop_references({"name": "cap.$0.thing"}) == set()


# â”€â”€ the actual policy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_an_independent_hop_survives_an_unrelated_failure():
    """The whole point: hop 3 never referenced hop 1."""
    hop = {"name": "notes.write", "args": {"text": "a fixed note"}}
    assert blocked_by(hop, {1}) is None


def test_a_dependent_hop_is_skipped():
    hop = {"name": "prose.author", "from": {"context": "$1"}}
    assert blocked_by(hop, {1}) == 1


def test_the_blocking_index_is_reported_so_the_skip_is_explicable():
    """'depends on failed hop $1' is actionable; 'skipped' is not."""
    hop = {"name": "x", "from": {"a": "$4", "b": "$2"}}
    assert blocked_by(hop, {2, 4}) == 2, "report the LOWEST failed index it needs"


def test_nothing_is_blocked_when_nothing_failed():
    assert blocked_by({"name": "x", "from": {"a": "$0"}}, set()) is None
    assert blocked_by({"name": "x", "from": {"a": "$0"}}, []) is None


def test_transitive_poison():
    """Hop 2 was skipped for needing failed hop 1, so $2 is missing as well."""
    poisoned = {1}
    hop2 = {"name": "b", "from": {"x": "$1"}}
    assert blocked_by(hop2, poisoned) == 1
    poisoned.add(2)                                   # caller marks the skip
    hop3 = {"name": "c", "from": {"y": "$2"}}
    assert blocked_by(hop3, poisoned) == 2, "poison must carry through the skip"


def test_malformed_hops_do_not_raise():
    for junk in (None, "string", 42, [], {"name": None}):
        assert blocked_by(junk, {1}) is None
