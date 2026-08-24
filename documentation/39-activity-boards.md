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
| Run shadow | native DAG lifecycle projected into versioned, content-free evidence | Correlate execution across Activity, Chat/LHM, Memory Graph, and the harness |
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

## Run evidence in the Activity UI

The W1-01 UI projects the native DAG lifecycle without taking authority from
it. Run cards expose parent/child task lineage, progress, failures, retries,
performance spans, checksummed journal reconciliation, partial artifact
references, structured approval/control evidence, and workflow/trace identity.
The same compact evidence appears in the harness top-bar overlay; full detail is
available in the Activity panel, with cross-links to Chat/LHM, Memory Graph, and
the native DAG Workshop.

These distinctions are deliberate:

- Run state is observed and non-authoritative; execution and control remain with
  the native DAG runtime.
- Artifact checksums are recorded metadata, not proof that content is currently
  available or verified.
- With the opt-in SQLite journal, recovery verifies each checksum chain and
  rebuilds the newest bounded searchable projection catalog. Corrupt Runs are
  isolated and reported; the UI still cannot replay or resume a native run.
- Telemetry coverage is local and content-redacted. No OpenTelemetry or
  OpenInference export is claimed until an exporter actually runs.
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

For W1-01 the durable coordination records are board item `ab35e45c` and
`notes:workspace:w1-01-run-protocol`. The worktree-local plan remains the most
specific description of the current sandbox; the board communicates ownership
and lane, while the note provides a compact cross-session handoff.

## Source map

- `vera/activity/` — timeline, aggregation, and operational actions.
- `vera/board/` — items, providers, claims, comments, dispatch, and sync.
- `vera/interaction/` — human-activity signals.
- `vera/evolve/` — pipelines and orchestration linked from board records.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
