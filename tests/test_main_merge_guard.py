"""M3.6 main-merge guardrail (vera/evolve/evolve_git_core.py).

The one sanctioned path to the real mainline is evolve.bleeding_edge.promote_to_main,
on the user's explicit go-ahead. evolve.pipeline.adopt/promote must REFUSE a
per-feature merge toward main/master unless the explicit authorization sentinel is
passed - closing the accidental to="main" hole that caused the 2026-08-16
direct-to-main incident. Pure - no git/app needed.
"""

from vera.evolve import evolve_git_core as core


def test_protected_set_covers_main_master_and_resolved_mainline():
    got = core.protected_mainline_names("main")
    assert "main" in got and "master" in got


def test_protected_set_lowercases_and_drops_blank():
    got = core.protected_mainline_names("")
    assert "" not in got
    assert got == {"main", "master"}


def test_bleeding_edge_is_never_protected():
    # the whole point: bleeding-edge (and feature branches) land freely
    assert core.main_merge_refusal("bleeding-edge", "main", "") == ""
    assert core.main_merge_refusal("feat/some-thing", "main", "") == ""
    assert core.main_merge_refusal("fix/x", "main", "") == ""


def test_main_without_authorization_is_refused():
    msg = core.main_merge_refusal("main", "main", "")
    assert msg  # non-empty == refused
    assert "bleeding-edge" in msg
    assert "promote_to_main" in msg
    assert core.MAIN_MERGE_SENTINEL in msg  # tells the caller how to proceed


def test_master_is_also_refused():
    assert core.main_merge_refusal("master", "main", "")


def test_resolved_mainline_name_is_refused_even_if_not_literally_main():
    # a repo whose mainline is e.g. 'trunk' still gets protected
    assert core.main_merge_refusal("trunk", "trunk", "")


def test_target_is_case_and_whitespace_insensitive():
    assert core.main_merge_refusal("  MAIN  ", "main", "")
    assert core.main_merge_refusal("Master", "main", "")


def test_correct_sentinel_allows_the_deliberate_merge():
    assert core.main_merge_refusal("main", "main", core.MAIN_MERGE_SENTINEL) == ""


def test_sentinel_tolerates_surrounding_whitespace():
    assert core.main_merge_refusal("main", "main", f"  {core.MAIN_MERGE_SENTINEL}  ") == ""


def test_wrong_sentinel_is_still_refused():
    assert core.main_merge_refusal("main", "main", "please")
    assert core.main_merge_refusal("main", "main", "yes")
    assert core.main_merge_refusal("main", "main", core.MAIN_MERGE_SENTINEL.lower())
