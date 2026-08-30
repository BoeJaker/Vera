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
    "test_board_core",          # Agent Boards leasing — claim resolution + Phase E handoff (only holder can hand off)
    "test_gate_sweep",          # Ollama-gate leaked-lease sweep — MUST never clear a peer host's live slot (double-books GPU)
    "test_ollama_slot_permit",   # leaked per-node generation permit wedged ALL generation (2026-08-29)
    "test_artifact_path_segments",  # _safe_seg ate __init__.py -> a package could never import (2026-08-29)
    "test_loop_fs_cap_unification",  # one fs story: sandbox caps read, code.author writes (2026-08-29)
    "test_loop_step_cycle_burners",  # pythonpath, invented filenames, blind edit refusals (2026-08-29)
    "test_loop_liveness",           # a quiet run is not a dead run (false interrupted, 2026-08-29)
    "test_operator_run_outcome",    # operator.run reported ok after hitting its step ceiling (2026-08-29)
    "test_gate_renew",          # Ollama-gate renewable-lease heartbeat — orphaned slot self-heals fast; renew is owner-fenced
    "test_loop_prompt_rules",      # Phase 1 - rule registry: one definition, consumers named
    "test_planner_rule_parity",   # Phase 3 - a shared planner rule must reach BOTH prompt variants
    "test_loop_stage_audit",      # Phase 0 stage-context records - must never carry prompt bodies
    "test_gate_cancel_release",  # Ollama-gate heartbeat frees the slot the moment its run is cancelled (no runaway GPU hold)
    "test_code_author_repair_guard",  # code.author repair must never "fix" a file by gutting it (2026-08-24 empty-stub incident)
    "test_url_dataset_resolve",  # web.fetch stall fix - the url->dataset scan must keep its answer while off the loop (2026-08-26)
    "test_loop_cap_denylist",    # evolve.* never offered to a loop; operator.run reachable for web artifacts
    "test_operator_target_resolution",  # an explicit url must reach the browser whatever `kind` says
    "test_code_workspace_path",  # code.author and code.edit must resolve a path identically (no /workspace/workspace/)
    "test_headless_package_auto",  # a human-approval bypass must stay off by default and never override deny/blocklist
    "test_ollama_inflight",      # a routing slot whose request never returned must be reclaimed
    "test_chain_deps",           # a failed chain hop poisons what USES it, not the whole pipeline
    "test_loop_run_history",     # unified loop-run record: retention policy must never mean keep-nothing or keep-everything
    "test_loop_trace_accounting",  # every executed step must name its producer - the census reads these counters (2026-08-29)
    "test_log_setup",           # prod's file log: out-of-tree (dirty tree blocks promote) + swept off the event loop (2026-08-29)
    "test_plan_cap_routing",    # an edit step must not be a code.author re-emit - re-emits drop earlier steps' work (2026-08-30)
    "test_census_core",         # the census history must never present step counts as a verdict on a goal (2026-08-30)
    "test_role_profile_merge",   # a USER routing override must not silently discard declared sampling/num_ctx (2026-08-24)
    "test_executor_compose_callsite",  # Phase 4 - every prompt block reaches the executor, unswapped (a drop/swap is silent)
    "test_godseye_core",       # vendored-app static serving: a path-guard hole serves arbitrary host files; git argv must reject option/ext:: injection
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
