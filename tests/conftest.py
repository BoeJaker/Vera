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
    "test_sandbox_idle_plan",  # sleep the container you judged, never an alias's target (2026-09-12)
    "test_operator_session_sweep",  # a cancelled run must not leak its browser context (2026-09-16)
    "test_gpu_residency",  # media models must not sit on the LLM's GPU while idle (2026-09-16)
    "test_node_runner_reap",  # stop a runner nothing is waiting for; spare a busy one (2026-09-16)
    "test_node_threads_core",  # CPU-node runners ran 24 threads on 12 CPUs: 0.24 tok/s on a 0.5b, 4-6 s per embed (2026-09-23)
    "test_worker_placement_core",  # a worker off the host ran every cap and every ambient job; 2026-08-31 reaped the pool (2026-09-27)
    "test_node_sync_core",  # node workers stayed on the old commit after every promotion; sync must follow what the host RUNS (2026-09-28)
    "test_fabric_ner_on_nodes",  # the host had no NER model, so the entity graph ran on heuristics while every node served OntoNotes NER (2026-09-28)
    "test_media_models_from_store",  # nodes served different media models from their own caches; the component deployed to a layout no node ran (2026-09-28)
    "test_ollama_tap",  # the capture proxy must never alter a request or response (the retired wrapper did) (2026-09-28)
    "test_activity_services",  # NLP/media/worker calls in the Activity pane; payloads sized, redaction honoured (2026-09-28)
    "test_node_activity",  # one pane over every node call, prod / sandbox / external (2026-09-28)
    "test_worldview_stream_yields",  # the stream worker reattached without yielding and wedged a release restart (2026-09-29)
    "test_worker_dispatch_stages",  # node workers never received work; results went to one reader of a shared group, not the asker (2026-09-28)
    "test_warm_models_core",  # every node let its models lapse after 5 min; a CPU cold load is 10-62 s (2026-09-28)
    "test_route_warm",  # the picker scored raw in_use (a 2-slot CPU node looked full) and ignored residency (2026-09-28)
    "test_route_warm_picker",  # an oversized context went to the GPU and spilled: the patched picker dropped ctx_need (2026-09-28)
    "test_cpu_concurrency",  # a CPU node's Ollama served one request at a time and prod's own limit was 1 per node (2026-09-28)
    "test_node_models_prune",  # a node's own model copy may go only when the store holds the same bytes (2026-09-28)
    "test_specialist_catalog",  # "in use" must be what the nodes serve; a partial export must not drop the manifest (2026-09-28)
    "test_specialist_models",  # no deployed component had a version: nothing could say a node was behind (2026-09-28)
    "test_redis_auth_core",  # Redis had no password; the host must boot from its sealed copy, never log or pass on a credential (2026-09-28)
    "test_ctx_ceiling_and_utility_model",  # llm.generate's default window was a floor; a naming rule with no model took the 9b onto a CPU node (2026-09-23)
    "test_step_summary_core",  # 65 of 130 step summaries were raw tool JSON; the verifier and the next step read them (2026-09-24)
    "test_done_tool_alias",  # a tool named `done` was refused five times and the step flailed on (2026-09-24)
    "test_authored_file_shown",  # 42 read-backs of a parser-verified authored file, one executor turn each (2026-09-24)
    "test_steer_core",  # the controller steer was copied into authoring tasks; scripts were written to satisfy it (2026-09-24)
    "test_ctx_output_room",  # a five-word chat title got a 24,576-token window on a CPU node (2026-09-23)
    "test_verify_evidence_core",  # a step "run the tests" was verified met on a cat of the test file after pytest reported failures (2026-09-24)
    "test_ctx_policy",  # output must fit the window it is generated into (2026-09-16)
    "test_sandbox_reap_plan",  # an archived row IS the restore handle - never reap it (2026-09-16)
    "test_stall_trace_core",  # name the frame someone can act on, not a stdlib line (2026-09-16)
    "test_spawn_off_loop",  # a docker call must not fork the server on the loop (2026-09-12)
    "test_editor_reply",  # an editor that declines has told you something (2026-08-31)
    "test_editor_output_bound",  # bound the editor by the file it edits (2026-08-31)
    "test_edit_tag_balance",  # name the edit that unbalanced the markup (2026-08-31)
    "test_workspace_path",  # a write must land where the read looked (2026-08-31)
    "test_stop_explanation",  # an operator that stopped must say why (2026-08-31)
    "test_pool_reconcile_running",  # a live container outranks a worktree probe (2026-08-31)
    "test_census_panel_modals",  # a modal must appear on the click (2026-08-31)
    "test_fenced_json",  # a fenced reply must survive losing its fence (2026-08-31)
    "test_v6_extract_paths",    # a capability name is not a file (2026-09-08)
    "test_author_browser_observable",  # a page a browser verifies must be readable by one (2026-09-08)
    "test_node_choice_and_model_tags",  # one CPU node took 90% of every census's embeds; a 0.5b tag counted as a 7b (2026-09-22)
    "test_route_preference",   # soft node preference must stay SOFT (2026-09-08)
    "test_graph_panel_assets",  # a renamed panel JS serves a comment, silently (2026-09-20)
    "test_nlp_placement",      # the host must never silently run NLP (2026-09-20)
    "test_edge_server_body_binding",  # a POST endpoint that cannot take a body (2026-09-20)
    "test_edge_dir_resolution",  # $HOME is not writable on 2 of 3 ollama nodes (2026-09-20)
    "test_inference_is_gated",  # chat generated outside the one-queue GPU gate (2026-09-20)
    "test_gate_timeout_is_not_permission",  # waiting out the queue is not a licence (2026-09-20)
    "test_research_route_core",  # a routing escalation asked with 0 chars is dead (2026-09-18)
    "test_artifact_location",  # a file the run already made has a place (2026-09-01)
    "test_instance_identity",  # an estate write must name its writer (2026-09-01)
    "test_estate_role",  # only the estate owner may sweep it (2026-09-01)
    "test_operator_budget",
    "test_recovery_identity_core",  # error recovery moved a sandbox browser run onto prod's own UI with destructive actions allowed (2026-09-24)
    "test_edit_reanchor",  # 11 stale anchors were refused with the right line named; the retry re-typed it wrong (2026-09-24)
    "test_plan_hygiene_core",  # 17 of 40 plans re-checked settled work, 9 criteria added features the goal never named (2026-09-24)
    "test_deliverable_core",  # the delivered answer dropped the file's citations every run and echoed its own template (2026-09-25)
    "test_browser_done_core",  # the executor re-ran the browser after it reported the step done, in every census browser goal (2026-09-25)
    "test_core_aliases_are_bound",  # a merge bound _verify_evidence only in an except branch; every verify died with NameError (2026-09-25)
    "test_author_done_core",  # ten executor turns re-checked a parser-verified authored file in run74 (2026-09-25)
    "test_research_done_core",  # 44 research calls across three sets came after the sources were in hand (2026-09-25)
    "test_fix_loop_core",  # 43 cycles chased one failing test to the wall cap in run77 (2026-09-25)
    "test_operator_step_budget",  # one step called operator.run again after its 480s cap fired: 29 thinks, 1109s, on a correct artifact (2026-09-22)  # an operator run needs a clock (2026-09-01)
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
    "test_operator_nav_fallback",  # aim it at the page the run wrote (2026-09-09)
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
    "test_census_control",  # a prod restart must not cost a census; a row must name its node and model (2026-09-10)
    "test_census_control_boot",  # the control caps must load with the app (2026-09-10)
    "test_census_single_table_ui",  # one table: expand a run, click a goal (2026-09-10)
    "test_ttl_cache",  # a polled 30s endpoint must not be recomputed per poll (2026-09-10)
    "test_census_code_version",  # a row must say which code it ran on; a restart mid-run names its goal (2026-09-10)
    "test_task_history_core",  # a census goal is a task; one task through time (2026-09-10)
    "test_task_history_boot",  # the history caps must load with the app and read the archive (2026-09-10)
    "test_result_ingest_core",  # a census goal is a run record; the archive row still wins (2026-09-10)
    "test_result_ingest_boot",  # evolve.result.ingest must load, store, and read back through every view (2026-09-10)
    "test_census_results_ingest_ui",  # the Runs view shows a census goal as what it is (2026-09-10)
    "test_census_ingest_timestamps",  # a row without a time takes its archive's END time; old rows stay out of the run list (2026-09-10)
    "test_work_core",  # one Work table: every driver run in one shape, the live one first (2026-09-10)
    "test_work_page_boot",  # the Work caps must load with the app; an edit keeps what the form does not show (2026-09-10)
    "test_work_page_ui",  # the Work page is the home and one table; the absorbed pages are gone (2026-09-10)
    "test_pollers_follow_their_page",  # an element polls only while its page is on screen; one socket per panel (2026-09-10)
    "test_census_live_dash_ui",  # a running census has a live dash: the loop's own output and its numbers (2026-09-10)
    "test_loop_profiles_imports",  # a JS literal in a Python module took loops.run off prod for a day (2026-09-10)
    "test_cross_page_ids_ui",  # every id is a link to one record, and a deep link (2026-09-10)
    "test_ship_core",  # the Ship table: a row per branch from five stores; a stale pending is superseded (2026-09-10)
    "test_ship_page_boot",  # evolve.ship.branches must load with the app and read the five stores in one call (2026-09-10)
    "test_ship_page_ui",  # CI/CD, Review, Sources, Sandbox, Unit tests are one page; the infographics survived (2026-09-10)
    "test_reaper_follows_the_sidecar",  # a redis sidecar is frozen with its app, never on its own (2026-09-10)
    "test_agents_core",  # the Agents table: a row per agent session from five stores; recency decides active (2026-09-10)
    "test_agents_page_boot",  # evolve.agents.rows must load with the app and read the five stores in one call (2026-09-10)
    "test_agents_page_ui",  # Sessions, Board, Notes, Capacity, Swarm are one page (2026-09-10)
    "test_mission_core",  # Mission control: a row per event - action, error, gate; open errors lead (2026-09-10)
    "test_mission_page_boot",  # evolve.mission.events must load with the app and read its stores in one call (2026-09-10)
    "test_mission_page_ui",  # Master, Activity, Errors are one page; the theatres are its folds; Loop Lab is five pages (2026-09-10)
    "test_census_posture_boot",  # a seeded census runs on prod's own loop, whatever sandbox_mode says (2026-09-10)
    "test_census_template_store",  # a dropped goal must lose its task (2026-09-07)
    "test_unittest_history",  # green on fewer tests is not a pass (2026-09-07)
    "test_race_to_green_ui",  # the strip must show a race, not one cell per pipeline (2026-09-07)
    "test_census_setup_ui",  # a census must be startable from the UI (2026-09-07)
    "test_agent_transcripts",  # codex writes transcripts too (2026-09-07)
    "test_census_follow_ui",  # following must survive the gaps between goals (2026-09-07)
    "test_swarm_runs_ui",  # the swarm's session list was empty by construction (2026-09-07)
    "test_sandbox_redis",  # a db number is not isolation (2026-09-07)
    "test_disk_headroom",  # a full docker disk took both databases down (2026-09-08)
    "test_idle_queue",
    "test_idle_queue_service",  # the runner: dispatch + pre-emption (2026-09-08)  # nothing may start during active use (2026-09-08)
    "test_background_work",  # background work must not run during a census (2026-09-08)
    "test_ingest_defers",  # ...and the ingest must actually go through the queue (2026-09-08)
    "test_run_health",  # a contended run must not read as a regression (2026-09-08)
    "test_planner_styles",  # a criterion may not assert a value the goal never gave (2026-09-09)
    "test_ambient_dreaming_is_opt_in",  # nothing dreams on a box nobody asked (2026-08-31)
    "test_sandbox_estate_guard",  # a sandbox must not promote to main or reap the estate (2026-08-31)
    "test_sandbox_does_not_reap_the_estate",  # a dev sandbox must not reap the shared estate (2026-08-31)
    "test_own_origin_tls",  # trust OUR cert only - never the open internet (2026-08-31)
    "test_loader_modules_have_no_parent_package",  # a relative import silently unregisters a whole subsystem (2026-08-31)
    "test_mcp_bridge_utf8",  # a bullet sent through the bridge must arrive as a bullet (2026-09-09)
    "test_exec_target",  # code sent with its destination must be written there, not discarded (2026-09-10)
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
    "test_census_partial_filter",  # a run the archive named bad is not history (2026-09-09)
    "test_gate_pause_release",  # a PAUSED container holding the capacity-1 GPU lease starves everyone (2026-08-30)
    "test_operator_trace_core",  # O13 - a browser run must be readable back, and never trusted on its own word
    "test_operator_census_core",  # the operator census must not launder a run's self-report into success
    "test_operator_cancel",     # a cancelled browser run must stop early, and must never score as finished
    "test_plan_shape",          # a complex goal planned as ONE step leaves the gate to rebuild it (2026-08-30)
    "test_operator_repeat_guard",  # O19 - hammering one element on one page must stop; different elements must not
    "test_pythonpath_under_timeout",  # L5's PYTHONPATH fix was dead under the timeout wrapper (2026-08-30)
    "test_exec_result_note",
    "test_exec_result_note_test_runs",
    "test_exec_result_note_script_run",  # python test_x.py runs the module, not the tests; eleven identical tracebacks in run67 (2026-09-23)  # pytest exits non-zero when tests fail; the loop read that as a broken command and inflated the goal (2026-09-22)    # rc=0 + empty stdout must read as a result, not as no-information (2026-08-30)
    "test_role_profile_merge",   # a USER routing override must not silently discard declared sampling/num_ctx (2026-08-24)
    "test_executor_compose_callsite",  # Phase 4 - every prompt block reaches the executor, unswapped (a drop/swap is silent)
    "test_godseye_core",       # vendored-app static serving: a path-guard hole serves arbitrary host files; git argv must reject option/ext:: injection
    "test_operator_arg_key_noise",
    "test_edit_gutter_core",  # the editor copied the view back as a markdown table; neither gutter stripper saw the leading pipe (2026-09-22)
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
    "test_missing_module_import",  # a name resolved only at CALL time: agentbridge.catalog 500'd for a day (2026-09-12)
    "test_automations_hub",  # a pane swapped in after load can never be switched off again (2026-09-12)
    "test_chat_ctx_core",  # chat asked for the node max every turn and evicted the runner it could have reused (2026-09-20)
    "test_embed_policy_core",  # machine state was 59% of all embedding time while real text queued behind it (2026-09-20)
    "test_queue_status_core",  # a wait line must never claim a node is free - in_use only sees Vera (2026-09-20)
    "test_workdir_listing_core",  # the file-type gate saw only the top level: a package's .py files one dir down failed every step to the wall cap (2026-09-20)
    "test_ollama_node_fault_core",  # three 404s for a made-up model took the GPU node offline and spilled the executor to CPU for 45 min (2026-09-22)
    "test_operator_model_arg_core",  # an executor asked the operator for model "fast": Ollama 404'd every think and the goal was clicked away (2026-09-21)
    "test_schedule_core",  # a scheduled census must never start beside a census or a loop, nor outside its window (2026-09-21)
    "test_schedule_page_ui",  # the Schedule page's calendar, editor and tick controls must stay wired (2026-09-21)
    "test_release_core",  # a prod release must never restart prod under a census goal unless forced (2026-09-21)
    "test_loop_web_research_first",
    "test_mcp_call_delegate_args",  # /mcp/call dropped every argument an explicit caller passed to v7 (plan_style, model, ...) (2026-09-27)
    "test_plan_style_loop",
    "test_plan_style_broad",
    "test_broad_ui_stream",  # a loop event not in ALWAYS_FORWARD never reaches the UI - plan_style was dropped there (2026-09-27)  # broad plans every work-stream concurrently and a successful broad plan is never overwritten (2026-09-27)  # the loop takes a planning style, logs the one it used, and every other style's planning/controller path is unchanged (2026-09-27)
    "test_chat_insights",  # optional long-horizon second look at a chat reply: CPU route, one at a time, a card not a message (2026-09-27)
    "test_census_plan_style_filter",  # census runs compared per planning style, and startable with one from Loop Lab (2026-09-27)
    "test_work_loop_rows",  # recorded loops reach Loop Lab's driver list with origin/engine/style (2026-09-27)
    "test_intent_zeroshot",  # an NLP node's zero-shot intent is recorded beside the one the loop used (measured, never read back) (2026-09-27)
    "test_entity_coverage",  # the loop measures how much of the goal's named entities its final output carries (NER on the NLP nodes) (2026-09-27)
    "test_cap_relevance_core",  # the planner catalogue: embeddings loaded, whole-word matching, short earned tail, no secret/provision caps, whole-sentence lines (2026-09-27)
    "test_stepwise_reviewed",  # stepwise + a CPU critic on every step: one at a time, latest only, never waited on (2026-09-28)
    "test_role_overrides",  # per-run executor/coder models on a chosen node + bigger-coder / max-effort presets; MoEs never half-loaded onto the GPU (2026-09-28)
    "test_chat_stream_capture",  # the chat reply capture parsed frames with the wrong spacing: chars=0, no printer feed, no insights (2026-09-28)
    "test_evolve_delegate",  # delegate a code-reporting task to a Vera loop: read-only guard, jailed tools, own worktree, cleanup (2026-09-28)
    "test_loop_record_core",  # only census/task loops reached Loop Lab's run store; chat/dream/program/API loops left no record (2026-09-27)
    "test_routing_parity_core",  # a sandbox ran the code-default models, not prod's routing - its measurements measured other models (2026-09-27)
    "test_step_deps_core",  # a step that needed a failed step read the failed attempt, never the recovery that finished it (2026-09-27)
    "test_step_call_ledger_core",  # a 10-18 cycle step re-ran calls that had already failed once they left its last-4 window (2026-09-27)
    "test_discovery_imports_prod_path",  # a plain-'vera' import the running app cannot resolve took operator.* (29 caps), then worldview + model inventory, off prod (2026-09-30)
    "test_stable_ctx",  # one context window per GPU node+model: the runner reloaded on nearly every call (2026-09-22)  # the loop chained web.search -> web.fetch to the wall cap while web.research sat unnamed (2026-09-21)
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






