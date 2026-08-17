"""Test-suite tiers for Loop Lab (dev-lifecycle-and-repo-hygiene.md §6).

Defines the **critical-system regression tier**: the pure, deterministic tests
that guard systems where a regression is expensive and was actually hit. They
must stay green and are the pre-merge gate set — run them with:

    pytest -m critical        (or)   make test-critical

Rather than edit every test file, this auto-applies the `critical` marker to a
known set of modules, so the tier is defined in one place.
"""

import pytest

_CRITICAL_MODULES = {
    "test_planner_guards",     # planner drift / skill-filter — the 2026-08-06 incidents
    "test_provenance",         # event -> commit/branch provenance stamping
    "test_ws_changes_guard",   # Workspace-Changes accept clobber-guard (compare-and-swap)
    "test_evolve_git_core",    # safe merge routing + worktree parsing (promote/approve)
    "test_evolve_logs_core",   # sandbox log/error/perf parsing
    "test_sandbox_reap",       # prune keep/reap/review safety — never reap WIP/unmerged/live (T1/T2)
    "test_pre_push_guard",     # pre-push force/delete refusal — guards the GitHub deploy key
    "test_main_merge_guard",   # M3.6 adopt/promote refuse to=main without explicit auth (2026-08-16 incident)
    "test_pre_merge_commit_guard",  # M3.6 part 2 — block hand-run `git merge` onto main (sanctioned override bypasses)
    "test_board_sync",         # M4 board.sync — pipeline->lane mapping, idempotency sig, human-parked-lane guard
    "test_test_gen_core",      # M3.4 test-generation — module filter, import/test-path mapping, fence strip
    "test_perf_gate_core",     # M3 perf-gating — perf.scan summary -> verdict, strict-vs-advisory blocking
    "test_attribution_core",   # honest Codex/Claude/autonomous/user controller mapping
    "test_mcp_bridge_attribution",  # agent bridge must not misattribute Codex as Claude
    "test_autonomous_lock_core",  # closed-loop Phase A — hard main-lockout while autonomous mode is engaged
    "test_orchestrator_core",  # closed-loop Phase B — orchestrator decision logic (dispatch/idle, interlock, no v7)
    "test_session_watch_core",  # closed-loop Phase C — auto-resume gate: never re-run finished/human/declared work
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "critical: critical-system regression tier (§6) — must stay green; gate set",
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        mod = getattr(item, "module", None)
        name = mod.__name__.rsplit(".", 1)[-1] if mod else ""
        if name in _CRITICAL_MODULES:
            item.add_marker(pytest.mark.critical)
