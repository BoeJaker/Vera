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

## Source map

- `vera/activity/` — timeline, aggregation, and operational actions.
- `vera/board/` — items, providers, claims, comments, dispatch, and sync.
- `vera/interaction/` — human-activity signals.
- `vera/evolve/` — pipelines and orchestration linked from board records.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
