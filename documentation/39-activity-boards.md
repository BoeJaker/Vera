# 39 · Activity, Boards, and Work Coordination

Vera's activity and board subsystems answer two different questions:
**what is happening now?** and **what work is owned and expected next?** Activity
is an event-derived operational view; boards are durable coordination records.
Interaction tracking connects user activity to scheduling and model-routing
policy.

## Data model

| Surface | Source | Use |
|---|---|---|
| Activity timeline | capability, loop, pipeline, sandbox, and file events | Observe current/recent execution |
| Board item | durable file/provider record | Own, prioritize, and communicate work |
| Pipeline link | Loop Lab pipeline identity | Derive implementation/review state |
| Claim/heartbeat | agent/session updates | Avoid duplicate work and detect abandonment |
| Interaction signal | recent human activity | Prioritize interactive workloads |
| Run shadow | native DAG, research, agent-loop, and Operator lifecycle projected into versioned, content-free evidence | Correlate execution across Activity, Chat/LHM, Memory Graph, and the harness |
| Sandbox work plan | `.vera-work/work-plan.json` in one worktree | Describe branch purpose, current step, blockers, and durable board/notes links |

Files and Git history outrank board claims about implementation. Board state
outranks indexes or dashboards derived from it. Activity events are evidence of
execution, not proof that the intended outcome was correct.

## Work lifecycle

1. Create or select a focused item.
2. Claim it with agent/session identity.
3. Link the implementation branch and pipeline.
4. Add concise progress and blocking evidence.
5. Synchronize verified pipeline state.
6. Move to done only when the requested outcome is actually complete.

Use `blocked` for a real dependency, not merely difficult work. Preserve human
parking decisions such as done/dropped when automatic synchronization runs.

## Operational use

The Activity panel can stop loops, inspect sandboxes, flatten nested activity,
and correlate files/events. Destructive controls need exact IDs. A stale event
stream should be removed only after confirming no live producer depends on it.
For suspected stalled agents, compare board heartbeat, active session, pipeline
state, sandbox state, and recent capability events before reassignment.

Capability activity now has enough structure to explain *why* an action was
available or refused. Contract projections supply canonical task, lifecycle,
effects, and resource posture; privacy-safe observations supply sample count,
success rate, and latency; resolver shadow supplies ranked/excluded candidates;
and policy events supply allow/deny/indeterminate plus selected/would-block/
blocked state. Activity stores and renders the bounded metadata and identifiers,
never arguments, prompts, result bodies, secret values, approval receipts, or
exception text.

This evidence also feeds memory and context graphs as references rather than
duplicated authority. A graph node can navigate to the exact activity or Run,
but the native capability/DAG remains authoritative and the board remains the
source of work ownership.

## Run evidence in the Activity UI

The shared UI projects native execution lifecycles without taking authority from
them. Run cards expose parent/child task lineage, progress, failures, retries,
performance spans, checksummed journal reconciliation, partial artifact
references, structured approval/control evidence, and workflow/trace identity.
The same compact evidence appears in the harness top-bar overlay; full detail is
available in the Activity panel, with cross-links to Chat/LHM, Memory Graph, and
the native DAG Workshop.

These distinctions are deliberate:

- Run state is observed and non-authoritative; execution and control remain with
  the native runtime. Operator projections expose step status, action name, and
  screenshot references while excluding goals, arguments, thoughts, and page
  content.
- Artifact checksums are recorded metadata, not proof that content is currently
  available or verified.
- With the opt-in SQLite journal, recovery verifies each checksum chain and
  rebuilds the newest bounded searchable projection catalog. Corrupt Runs are
  isolated and reported. Activity shows aggregate recovered/quarantined counts
  and the catalog bound, while the compact harness overlay adds status badges.
  The UI still cannot replay or resume a native run.
- Telemetry projection is local and content-redacted. Activity reports the
  redacted state and bounded success/failure counters of the optional OTLP/HTTP
  JSON exporter. It claims export only after a collector accepts a request;
  endpoint and authorization-header values are never shown. When automatic
  export is explicitly enabled, the same card reports queue depth, drops,
  deduplication, retries and terminal trace failures.
- Free-text failure/control reasons and result bodies are excluded from Activity
  evidence.

The Activity timeline and top-bar overlay include keyboard navigation, labelled
regions, visible focus, reduced-motion behavior, and narrow-screen layouts.

## Work plans, boards, and notes

A sandbox work plan is operational handoff context, not another project tracker.
It links to one or more durable board items and notes references. Loop Lab applies
revision guards, rejects duplicate step IDs, requires `current_step` to name a
real step, and refuses `complete` while unfinished steps remain. The Sandbox tab
shows the linked board records directly from the plan editor.

The worktree-local plan remains the most specific description of the current
sandbox; the board communicates ownership and lane, while notes provide compact
cross-session handoff context. Delivery identifiers stay in those internal
coordination surfaces rather than product documentation or UI labels.

## Source map

- `vera/activity/` — timeline, aggregation, and operational actions.
- `vera/board/` — items, providers, claims, comments, dispatch, and sync.
- `vera/interaction/` — human-activity signals.
- `vera/evolve/` — pipelines and orchestration linked from board records.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
