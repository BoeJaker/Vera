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
