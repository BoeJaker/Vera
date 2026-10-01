# 39 · Activity, Boards, and Work Coordination

Vera's activity and board subsystems answer two different questions:
**what is happening now?** and **what work is owned and expected next?**
Activity is an event-derived operational view; boards are durable coordination
records. Interaction tracking connects user activity to scheduling and
model-routing policy.

The **Activity** layer (`vera/activity/activity_capabilities.py`) is a
read/compose layer with no storage of its own: it assembles one timeline from
dream cycles, agentic loop runs, V8 loop programs, live background loop
sessions, capability activity, Run projections and sandbox containers, and adds
a few operational actions (stop or deduplicate loops, remove a stream, browse a
scope's files). The **Board** (`vera/board/`) is a markdown-file work plane with
lanes, lease-style claims, an agent message envelope, plan import, pipeline
dispatch and sync. Both are in daily use; the board's only provider today is the
file tier, which is always present, human-editable and kept outside the
repository tree.

## Contents

- [1. Data model](#1-data-model)
- [2. Source map](#2-source-map)
- [3. Activity timeline](#3-activity-timeline)
  - [3.1 Scopes and events](#31-scopes-and-events)
  - [3.2 Capabilities](#32-capabilities)
  - [3.3 Operational use](#33-operational-use)
  - [3.4 Capability evidence](#34-capability-evidence)
- [4. Run evidence in the Activity UI](#4-run-evidence-in-the-activity-ui)
- [5. Interaction signals](#5-interaction-signals)
- [6. Boards](#6-boards)
  - [6.1 Items, lanes and metadata](#61-items-lanes-and-metadata)
  - [6.2 Claims, comments and handoff](#62-claims-comments-and-handoff)
  - [6.3 Board capabilities](#63-board-capabilities)
  - [6.4 Dispatch and pipeline sync](#64-dispatch-and-pipeline-sync)
  - [6.5 Plan import, context and index](#65-plan-import-context-and-index)
- [7. Work lifecycle](#7-work-lifecycle)
- [8. Work plans, boards, and notes](#8-work-plans-boards-and-notes)
- [9. Configuration and storage](#9-configuration-and-storage)
- [10. Troubleshooting](#10-troubleshooting)
- [See also](#see-also)

---

## 1. Data model

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

## 2. Source map

| Path | Responsibility |
|---|---|
| `vera/activity/activity_capabilities.py` | `activity.*` timeline, scopes, session grouping, loop/stream/sandbox/file actions; the **Activity** tab and `<vera-activity-timeline>` widget |
| `vera/activity/activity_panel.html` | Activity panel (`/activity/panel`) |
| `vera/board/board_core.py` | Pure item model, board-meta parsing, message envelopes, lane rules, `FileBoardProvider` |
| `vera/board/board_capabilities.py` | `board.*` capabilities, secret scan on the write path, scheduled pipeline sync |
| `vera/capability_orchestration.py` | `activity.ping`, `ollama.interactive.*`, `run.shadow.*`, `run.telemetry.*` |
| `vera/execution/run_projection.py`, `run_journal.py`, `run_shadow.py`, `portable_telemetry.py` | Run shadow registry, journal and OTLP projection behind Run evidence |
| `vera/evolve/sandbox_workplan.py` | Sandbox work plan file (`evolve.sandbox.workplan.get` / `.update`) |
| `vera/evolve/` | Pipelines and orchestration linked from board records ([33](./33-evolve.md)) |

## 3. Activity timeline

### 3.1 Scopes and events

`activity.timeline` returns a time-ordered list of events for a **scope**:

| Scope | Shows |
|---|---|
| `all` | The master timeline (default) |
| `project:<slug>` / `goal:<slug>` | One project or goal |
| `dream` / `dream:<trigger>` | Dream pipelines, all or one trigger |
| `program:<pid>` | One V8 loop program |
| `run:<run_id>` | One Run protocol projection |
| `chat:<session_id>` | One chat session's activity |
| `narrator` | Narrator takes |

Each event is `{kind, ts, title, summary, status, session_id, cap, ui, extra}`.
The `ui` block (`panel`, `url`, `element`, `session_id`) lets the front end open
the associated UI — the discovery graph, the netmap, an agent-loop re-attach —
and watch a running loop live. Sources are read from keys owned by other
modules (`vera:dream:projects`, `vera:dream:project_loops`,
`vera:dream:project_artifacts`, `vera:loop:sessions`, `vera:v8:programs`) and
recent `cap.ok` events. A loop session whose last event is older than
`VERA_LOOP_STALE_SECS` (600 s) counts as **interrupted**, not running.

### 3.2 Capabilities

| Cap | Route | Purpose |
|---|---|---|
| `activity.timeline` | `GET /activity/timeline` | Unified timeline: `scope`, `limit` (120), `kinds` (comma filter) |
| `activity.pipelines` | `GET /activity/pipelines` | Scopes the timeline can show (projects, goals, dream triggers, programs) |
| `activity.sessions` | `GET /activity/sessions` | Recent `cap.ok` activity grouped by session and attributed to `you` / `agent:*` / `system:*`; silent monitoring caps are excluded. `window_min` (120), `per_session` (14), `max_sessions` (10), `actor` |
| `activity.loops.stop` | `POST /activity/loops/stop` | Stop a background loop by `session_id`: pause its V8 program, cancel the engine task, remove it from the live set |
| `activity.loops.flatten` | `POST /activity/loops/flatten` | Prune stale sessions and stop duplicate running loops sharing owner + goal (keeps the newest); `scope`, `dry_run` |
| `activity.sandboxes` | `GET /activity/sandboxes` | Sandbox containers tied to a scope, with terminal URLs and session counts |
| `activity.stream.remove` | `POST /activity/stream/remove` | Remove a stream and its work: `target` = `program:<pid>` / `project:<slug>` / `goal:<slug>`; `archive` keeps the project |
| `activity.files` | `GET /activity/files` | Browse the scope's sandbox `/workspace` (or `path`) plus recorded artifacts |
| `activity.ping` | `POST /activity/ping` | Mark the human as active now ([§5](#5-interaction-signals)) |

UI: the **Activity** tab (`activity`, `tab_order=42`, `/activity/panel`) and an
injectable **Activity** widget (`activity-timeline`, the
`<vera-activity-timeline>` element).

### 3.3 Operational use

The Activity sidebar reads its available scopes independently from the
timeline. Its first load, empty result, and request failure are distinct
accessible states. Failures remain visible with a retry action instead of being
silently replaced by a healthy-looking "Everything" scope, while an empty
response is reported as an empty work plane rather than an outage. Periodic
refresh retains the existing scope-selection and timeline semantics.

The Activity panel can stop loops, inspect sandboxes, flatten nested activity,
and correlate files/events. Destructive controls need exact IDs. A stale event
stream should be removed only after confirming no live producer depends on it.
For suspected stalled agents, compare board heartbeat, active session, pipeline
state, sandbox state, and recent capability events before reassignment.

> [!WARNING]
> `activity.stream.remove` deletes a V8 program and, for a project or goal,
> deletes the project unless `archive=true`. Run `activity.loops.flatten` with
> `dry_run=true` first when cleaning up.

### 3.4 Capability evidence

Capability activity has enough structure to explain *why* an action was
available or refused. Contract projections supply canonical task, lifecycle,
effects, and resource posture; privacy-safe observations supply sample count,
success rate, and latency; resolver shadow supplies ranked/excluded candidates;
and policy events supply allow/deny/indeterminate plus selected/would-block/
blocked state. Activity stores and renders the bounded metadata and identifiers,
never arguments, prompts, result bodies, secret values, approval receipts, or
exception text. See [Capability Contracts](./43-capability-contracts.md) and
[Capability Policy](./45-capability-policy.md).

This evidence also feeds memory and context graphs as references rather than
duplicated authority. A graph node can navigate to the exact activity or Run,
but the native capability/DAG remains authoritative and the board remains the
source of work ownership.

## 4. Run evidence in the Activity UI

The shared UI projects native execution lifecycles without taking authority from
them. Run cards expose parent/child task lineage, progress, failures, retries,
performance spans, checksummed journal reconciliation, partial artifact
references, structured approval/control evidence, and workflow/trace identity.
The same compact evidence appears in the harness top-bar overlay; full detail is
available in the Activity panel, with cross-links to Chat/LHM, Memory Graph, and
the native DAG Workshop.

| Cap | Purpose |
|---|---|
| `run.shadow.list` / `run.shadow.get` | Recent non-authoritative Run projections and their children |
| `run.shadow.graph` | Bounded, content-free Run graph (`GET /run/shadow/graph`) |
| `run.shadow.export` | Checksummed event journal for a recent shadow Run |
| `run.telemetry.preview` / `.status` / `.export` | Content-redacted portable trace, exporter status, explicit OTLP export |

These distinctions are deliberate:

- Run state is observed and non-authoritative; execution and control remain with
  the native runtime. Operator projections expose step status, action name, and
  screenshot references while excluding goals, arguments, thoughts, and page
  content.
- Artifact checksums are recorded metadata, not proof that content is currently
  available or verified.
- With the opt-in SQLite journal (`VERA_RUN_JOURNAL_PATH`; in-memory otherwise),
  recovery verifies each checksum chain and rebuilds the newest bounded
  searchable projection catalog. Corrupt Runs are isolated and reported.
  Activity shows aggregate recovered/quarantined counts and the catalog bound,
  while the compact harness overlay adds status badges. The UI still cannot
  replay or resume a native run.
- Telemetry projection is local and content-redacted. Activity reports the
  redacted state and bounded success/failure counters of the optional OTLP/HTTP
  JSON exporter (standard `OTEL_EXPORTER_OTLP_*` settings). It claims export
  only after a collector accepts a request; endpoint and authorization-header
  values are never shown. When automatic export is explicitly enabled
  (`VERA_OTLP_AUTO_EXPORT`), the same card reports queue depth, drops,
  deduplication, retries and terminal trace failures.
- Backend comparison remains an offline evidence operation. Langfuse and
  Phoenix observations must refer to the identical redacted portable trace and
  OTLP export digests. Fidelity, query/UI value, evaluation linkage,
  governance, resource use, portability, outage isolation, and teardown remain
  separate; the comparison neither selects a backend nor changes Activity's
  exporter configuration.
- Free-text failure/control reasons and result bodies are excluded from Activity
  evidence.

The Activity timeline and top-bar overlay include keyboard navigation, labelled
regions, visible focus, reduced-motion behavior, and narrow-screen layouts.

## 5. Interaction signals

Vera tracks when a human last interacted so background work yields to them. The
chat generation path stamps activity automatically, and UIs call
`activity.ping` on real interactions (sending a message, clicking run). The
interactive-priority policy (`ollama.interactive.get` / `ollama.interactive.set`,
persisted at `vera:ollama:interactive_priority`) then decides what happens
inside the activity window:

| Field | Default | Effect |
|---|---|---|
| `enabled` | `true` | Demote background LLM work (dream cycles, V8 programs, fabric NLP) off the GPU while a human is active |
| `window_s` | 180 | How long after the last interaction the human counts as active |
| `defer_background` | `true` | Also skip *starting* new background runs (dream scheduler fires, V8 program ticks) in the window |
| `background_always_cpu` | `false` | Keep background LLM work off the GPU at all times |

See [LLM Cluster](./04-ollama-cluster.md) for how routing applies this.

## 6. Boards

### 6.1 Items, lanes and metadata

Each item is one markdown file `<id>.md` under the out-of-tree board directory
(`<state dir>/board`), written atomically. Board metadata lives **in the item
body** as a fenced `board-meta` block, so items stay portable across providers.
Ids are 8-hex-character UUID prefixes unless imported deterministically.

Lanes, in order: `inbox` (default), `ready`, `in_progress`, `blocked`,
`needs_review`, `review`, `done`, `dropped`, `queued_vera`, `in_progress_vera`.
`blocked` and `dropped` are lanes, not deletions; `done` and `dropped` are
terminal and are never changed by automatic sync.

Metadata fields: `lane`, `labels`, `agent`, `repo` (where it lands; blank =
Vera), `project`, `plan` (umbrella plan id), `branch`, `pipeline`, `session`,
`executor`, `model`, `reviewed_by`, `heartbeat`, `hops`, `created_at`,
`updated_at`, `sync_sig`. Labels carry routing such as `agent:<name>` and
`needs:help`.

### 6.2 Claims, comments and handoff

Comments are typed **agent-message envelopes** `{kind, from, to, item, hops,
done_when, body}` with kinds `claim`, `withdraw`, `handoff`, `help-request`,
`reply`, `progress`, `blocked`, `steer`, `resume`, `note`. `done_when` is
mandatory when asking another agent to act, and bodies should point at
`file:line` rather than paste dumps.

A **claim is a lease** decided by an append-only, totally ordered primitive:
the lowest comment id wins. A later claimant receives `lost: true`, a
`withdraw` is posted for it, and the item stays with the earlier holder.
`board.handoff` is guarded — only the current holder can hand off, never to
itself — and writes the handoff envelope, the holder's withdraw and the target's
claim in one write.

Every comment passes a **secret scan** on the write path that blocks
unambiguous secrets (private-key headers, AWS access-key ids, GitHub tokens,
Slack tokens). It is deliberately conservative so ordinary prose never trips it.

### 6.3 Board capabilities

| Cap | Route | Purpose / key args |
|---|---|---|
| `board.items` | `GET /board/items` | Compact list; filter by `lane`, `label`, `agent`, `mentions`, `text` |
| `board.item.get` | `GET /board/item` | Full record with body and comment thread |
| `board.item.upsert` | `POST /board/item/upsert` | Create (blank `id`) or update; comments are preserved |
| `board.item.move` | `POST /board/item/move` | Move to a lane |
| `board.claim` | `POST /board/claim` | Lease claim: posts a claim, takes `agent:<name>`, moves to `in_progress`, stamps the heartbeat |
| `board.comment` | `POST /board/comment` | Post an envelope (`frm`, `kind`, `to`, `body`, `done_when`, `hops`) |
| `board.inbox` | `GET /board/inbox` | An agent's inbox as a query: assigned, mentioned, and unassigned `needs:help` |
| `board.help` | `POST /board/help` | Add `needs:help` and post a help request |
| `board.handoff` | `POST /board/handoff` | Atomic claim transfer (`frm` → `to`) |
| `board.provider` | `GET /board/provider` | Active provider, storage root, degraded flag |
| `board.import_plan` | `POST /board/import_plan` | Import a markdown plan ([§6.5](#65-plan-import-context-and-index)) |
| `board.dispatch` | `POST /board/dispatch` | Run an item through the pipeline ([§6.4](#64-dispatch-and-pipeline-sync)) |
| `board.context` | `GET /board/context` | Full brief for an agent: umbrella plan guidance, the item, repo/branch, sibling items |
| `board.index` | `POST /board/index` | Rebuild the derived fabric index |
| `board.sync` | `POST /board/sync` | Reflect linked pipeline state onto items (`id`, or all) |

Events: `board.dispatch`, `board.dispatch.done`, `board.handoff`, `board.synced`.

### 6.4 Dispatch and pipeline sync

`board.dispatch` orchestrates one item through the same pipeline used to build
Vera: claim → `evolve.pipeline.begin` (typed branch, worktree, and for model
executors a dev sandbox and pipeline record) → run the executor → move to
`needs_review`, or `blocked` on failure. It runs in the background so a caller's
timeout cannot cancel it mid-flight. A human always promotes the result.

| Executor | Runs |
|---|---|
| `deterministic` | No model: a supplied `check` command, or a marker commit, in the worktree |
| `vera` | Local Ollama agent via `ide.remote.run` on a target instance |
| `capable` | A Claude seat chosen from available pinned capacity; refuses rather than downgrading when none is free |

`board.sync` reflects each linked pipeline's current decision/gate/review state
onto its items (lane plus a progress comment). It is idempotent through each
item's `sync_sig`, and never moves an item out of `done` or `dropped` — those
are human decisions. A scheduled poll (`board.sync.poll`) runs it every
`VERA_BOARD_SYNC_INTERVAL_S` (300 s) unless `VERA_BOARD_SYNC_ENABLED=0`.

### 6.5 Plan import, context and index

`board.import_plan` (from a file `path` or inline `text`) separates overall
**context** from discrete **work** by default: it creates one umbrella plan item
(`plan-<project>`) holding the why/overview/principles/decisions sections, and
work items for the actionable sections (phases, tasks, or anything with a
checklist), each tagged `plan=<umbrella id>`. Ids are deterministic, so
re-importing updates in place; `group=false` restores one item per heading.
`board.context` then hands an agent the plan guidance, its own item and its
siblings. `board.index` rebuilds the fabric dataset `board_index` so questions
like "what is blocked?" work through `fabric.query` / `memory.seek`; the index is
derived and rebuilt wholesale each time, never authoritative.

## 7. Work lifecycle

1. Create or select a focused item.
2. Claim it with agent/session identity.
3. Link the implementation branch and pipeline.
4. Add concise progress and blocking evidence.
5. Synchronize verified pipeline state.
6. Move to done only when the requested outcome is actually complete.

Use `blocked` for a real dependency, not merely difficult work. Preserve human
parking decisions such as done/dropped when automatic synchronization runs.

```bash
# Claim an item and post progress
curl -s -X POST http://localhost:8999/board/claim \
  -H 'Content-Type: application/json' -d '{"id":"a1b2c3d4","agent":"coder"}'
curl -s -X POST http://localhost:8999/board/comment \
  -H 'Content-Type: application/json' \
  -d '{"id":"a1b2c3d4","frm":"coder","kind":"progress","body":"parser fixed in vera/x.py:120"}'
```

## 8. Work plans, boards, and notes

A sandbox work plan is operational handoff context, not another project
tracker. It links to one or more durable board items and notes references.
Loop Lab applies revision guards, rejects duplicate step IDs, requires
`current_step` to name a real step, and refuses `complete` while unfinished
steps remain. The Sandbox tab shows the linked board records directly from the
plan editor (`evolve.sandbox.workplan.get` / `evolve.sandbox.workplan.update`).

The worktree-local plan remains the most specific description of the current
sandbox; the board communicates ownership and lane, while notes provide compact
cross-session handoff context. Delivery identifiers stay in those internal
coordination surfaces rather than product documentation or UI labels.

## 9. Configuration and storage

| Setting / store | Default | Meaning |
|---|---|---|
| `<state dir>/board/<id>.md` | — | Board items (outside the repository so the checkout stays clean) |
| `VERA_BOARD_SYNC_ENABLED` | `1` | Scheduled pipeline → board sync |
| `VERA_BOARD_SYNC_INTERVAL_S` | 300 | Sync interval |
| `VERA_LOOP_STALE_SECS` | 600 | Age after which a loop session counts as interrupted |
| `vera:ollama:interactive_priority` | see [§5](#5-interaction-signals) | Interactive-priority policy |
| `VERA_RUN_JOURNAL_PATH` | unset (memory) | SQLite Run journal |
| `VERA_OTLP_AUTO_EXPORT`, `OTEL_EXPORTER_OTLP_*` | off | Run telemetry export |
| fabric dataset `board_index` | — | Derived board index |

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| Dozens of "running" loops after a restart | `activity.loops.flatten` with `dry_run=true`, then without |
| A loop shows interrupted but is alive | Its last event is older than `VERA_LOOP_STALE_SECS` |
| `board.claim` returns `lost: true` | Another agent's earlier claim holds the lease; coordinate via `board.comment` or wait for a handoff |
| Comment rejected as a secret | Remove the credential; reference it by name instead |
| Item lane not following its pipeline | Item is `done`/`dropped`, sync disabled, or no `pipeline` link — run `board.sync` with the id |
| Dispatch left the item `blocked` | Read the item's last `blocked` comment; `evolve.pipeline.begin` or the executor failed |
| Background work still on the GPU while chatting | `ollama.interactive.get`; is `enabled` on and the UI calling `activity.ping`? |

---

## See also

- [Agents & Chat](./19-agents-chat.md) — loops whose sessions the timeline shows
- [Dream](./17-dream.md) — dream cycles and projects
- [Evolve](./33-evolve.md) — pipelines and sandboxes that boards dispatch into
- [IDE](./08-ide.md) — `ide.remote.run` coding agents
- [DAG Engine](./03-dag-engine.md) — the native runtime behind Run evidence
- [Capability Contracts](./43-capability-contracts.md) and [Capability Policy](./45-capability-policy.md)
- [Agent Runtimes & Providers](./36-agent-runtimes-providers.md)

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
