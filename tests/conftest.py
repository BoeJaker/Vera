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
    "test_editor_reply",  # an editor that declines has told you something (2026-08-31)
    "test_editor_output_bound",  # bound the editor by the file it edits (2026-08-31)
    "test_edit_tag_balance",  # name the edit that unbalanced the markup (2026-08-31)
    "test_workspace_path",  # a write must land where the read looked (2026-08-31)
    "test_stop_explanation",  # an operator that stopped must say why (2026-08-31)
    "test_pool_reconcile_running",  # a live container outranks a worktree probe (2026-08-31)
    "test_census_panel_modals",  # a modal must appear on the click (2026-08-31)
    "test_fenced_json",  # a fenced reply must survive losing its fence (2026-08-31)
    "test_artifact_location",  # a file the run already made has a place (2026-09-01)
    "test_instance_identity",  # an estate write must name its writer (2026-09-01)
    "test_estate_role",  # only the estate owner may sweep it (2026-09-01)
    "test_operator_budget",  # an operator run needs a clock (2026-09-01)
    "test_two_tier_chat",  # answer first, continue with context (2026-09-01)
    "test_two_tier_decider",  # who decides the second pass is needed (2026-09-01)
    "test_two_tier_switch",  # a feature with no switch is unusable (2026-09-01)
    "test_loop_liveness",  # a busy loop is not a dead loop (2026-09-02)
    "test_progress_content",  # a countdown is progress (2026-09-02)
    # Census 35/36. Each of these guards a failure that was OBSERVED burning a
    # goal's whole budget, and none of them was covered by this tier while the
    # gate reported "2450 passed" on the branches that introduced them.
    "test_operator_repeat_structural",  # thrash on a page whose text moves (2026-09-06)
    "test_operator_nav_pin",  # a run aimed at one file must stay on it (2026-09-06)
    "test_operator_think_budget",  # one decision must not cost 199s (2026-09-06)
    "test_missing_path_hint",  # name the files that exist (2026-09-06)
    "test_workdir_note",  # do not tell a step to look at what it was shown (2026-09-06)
    "test_operator_think_recovery",  # one bad reply is not a broken run (2026-09-06)
    "test_operator_observed_finding",  # what the page showed is the finding (2026-09-06)
    "test_operator_goal_observable_rule",  # ask for something observable (2026-09-06)
    # In the tier BECAUSE it went red unnoticed: nothing ran it, so a prompt
    # change landed without its golden and the guard sat broken for days. It
    # skips cleanly where the app module is not importable. (2026-09-06)
    "test_planner_prompt_golden",  # the planner prompt must not change by accident
    "test_search_engines",  # one search implementation, and it reaches page 2 (2026-09-06)
    "test_operator_trace_seen",  # the trace must show what the page displayed (2026-09-06)
    "test_test_target",  # a test file is written against the file it tests (2026-09-06)
    "test_error_excerpt",  # a trace must keep the verdict, not just the banner (2026-09-06)
    "test_verify_fastpath",  # went stale outside the tier, asserting the opposite of the fix (2026-09-06)
    "test_repeat_failure",  # do not re-buy an answer you already have (2026-09-06)
    "test_safety_non_network",  # a blank page is not a foreign host (2026-09-07)
    "test_suite_file_checks",  # a task must assert what it PRODUCED (2026-09-07)
    "test_gate_politeness",  # the suite must yield the box like the census (2026-09-07)
    "test_engine_params",  # a dropped model override is worse than an error (2026-09-07)
    "test_census_seed",  # a template must not silently rebase the timeline (2026-09-07)
    "test_census_template_store",  # a dropped goal must lose its task (2026-09-07)
    "test_unittest_history",  # green on fewer tests is not a pass (2026-09-07)
    "test_race_to_green_ui",  # the strip must show a race, not one cell per pipeline (2026-09-07)
    "test_census_setup_ui",  # a census must be startable from the UI (2026-09-07)
    "test_agent_transcripts",  # codex writes transcripts too (2026-09-07)
    "test_census_follow_ui",  # following must survive the gaps between goals (2026-09-07)
    "test_ambient_dreaming_is_opt_in",  # nothing dreams on a box nobody asked (2026-08-31)
    "test_sandbox_estate_guard",  # a sandbox must not promote to main or reap the estate (2026-08-31)
    "test_sandbox_does_not_reap_the_estate",  # a dev sandbox must not reap the shared estate (2026-08-31)
    "test_own_origin_tls",  # trust OUR cert only - never the open internet (2026-08-31)
    "test_loader_modules_have_no_parent_package",  # a relative import silently unregisters a whole subsystem (2026-08-31)
    "test_prose_author_think",  # the writer must write, not ruminate (2026-08-31)
    "test_sandbox_file_target",  # the operator can be pointed at a file it just wrote (2026-08-31)
    "test_operator_progress",  # a run that changes nothing must stop; a long run must not (2026-08-31)
    "test_output_budget",  # a stated length must bound the generation (2026-08-31)
    "test_edit_anchor_hint",  # a rejected edit must leave the model something to correct (2026-08-31)
    "test_scheduler_leadership",  # sweeps that mutate shared state run in ONE instance (2026-08-31)
    "test_capability_decorator_binding",  # a decorator binds to whatever def comes next (2026-08-30)
    "test_sandbox_pool_reconcile",  # a failed read must never be read as "it is gone" (2026-08-30)
    "test_ide_instance_store_await",  # a sync mirror of an async helper loses writes silently (2026-08-30)
    "test_operator_target_fallback",  # a busy primary must not make browser verification impossible (2026-08-30)
    "test_result_failure_reason",  # a failed cap must say why, whatever it names the field (2026-08-30)
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
    "test_gate_pause_release",  # a PAUSED container holding the capacity-1 GPU lease starves everyone (2026-08-30)
    "test_operator_trace_core",  # O13 - a browser run must be readable back, and never trusted on its own word
    "test_operator_census_core",  # the operator census must not launder a run's self-report into success
    "test_operator_cancel",     # a cancelled browser run must stop early, and must never score as finished
    "test_plan_shape",          # a complex goal planned as ONE step leaves the gate to rebuild it (2026-08-30)
    "test_operator_repeat_guard",  # O19 - hammering one element on one page must stop; different elements must not
    "test_pythonpath_under_timeout",  # L5's PYTHONPATH fix was dead under the timeout wrapper (2026-08-30)
    "test_exec_result_note",    # rc=0 + empty stdout must read as a result, not as no-information (2026-08-30)
    "test_role_profile_merge",   # a USER routing override must not silently discard declared sampling/num_ctx (2026-08-24)
    "test_executor_compose_callsite",  # Phase 4 - every prompt block reaches the executor, unswapped (a drop/swap is silent)
    "test_godseye_core",       # vendored-app static serving: a path-guard hole serves arbitrary host files; git argv must reject option/ext:: injection
    "test_operator_arg_key_noise",
    "test_edit_blocks_and_target",
    "test_operator_says_done",
    "test_probe_backoff",
    "test_fabric_boot_storm",
    "test_editor_format_agreement",
    "test_poll_cache",
    "test_anchor_and_decline",
    "test_census_quality",
    "test_census_landed",
    "test_openclaw_handshake",  # a handshake that violates the gateway's schema can never connect (2026-09-05)
    "test_emit_event_arity",  # emit_event takes ONE dict; the wrong arity hides until the line first runs (2026-09-05)
    "test_openclaw_stream",  # the same answer arrives twice; reading both halves doubles every token (2026-09-05)
    "test_openclaw_supervisor",  # interval=0 means every tick: one reconnect loop per second, all racing (2026-09-05)
    "test_openclaw_run_buffers",  # two prompts in one session gave one run both answers and the other none (2026-09-07)
    "test_openclaw_prompt_queue",  # a second prompt into a busy session is accepted and answered with nothing (2026-09-07)
    "test_census34_failures",
    "test_edit_already_applied",
    "test_printer_wrap",  # paper does not re-flow: an unwrapped line is SILENTLY clipped off the page (2026-09-07)
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


@pytest.fixture(scope="session")
def orch():
    """The imported orchestrator module, with operator caps registered.

    Restored 2026-08-30. This fixture existed until `9ecf714` (2026-08-08),
    the commit that rewrote this file to define the critical tier; it was
    dropped in that rewrite and nothing noticed, because the eleven tests that
    depend on it - test_capabilities_contract (8) and test_ui_panels (3) - are
    NOT in the critical tier, so the merge gate never runs them. They have
    errored with `fixture 'orch' not found` for twenty-two days.

    The sys.path setup lives INSIDE the fixture rather than at module scope,
    where the original had it. Module scope would apply it to all ~2300 tests
    including the 810 in the critical tier that are green today, and adding
    repo-root entries to sys.path can change which `vera`/`Vera` package an
    import resolves to. Only the tests that ask for the orchestrator need it.

    Skips cleanly - it does not fail - when the full runtime is not installed
    in this environment, which is what lets the pure suite stay green anywhere.
    """
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (root, os.path.dirname(root)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import Vera.vera.capability_orchestration as _orch  # noqa
        import Vera.vera.operator.operator_web_capabilities  # noqa: F401  registers operator.*
        return _orch
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"orchestrator/app unavailable in this env: {e}")






