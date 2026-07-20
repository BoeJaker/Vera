# 17 · Dream — Autonomous Reflection Engine

`dream/dream_capabilities.py` is Vera's proactive background cognition system. When the orchestrator has been **idle** for a while, Dream spins up a background **dream cycle**: a pipeline of small capabilities — *sensors* that gather context and *stages* that reason and act — strung together by a **trigger** record. It's how Vera "thinks" when nobody is talking to it.

It is by far the largest module in the system (~150 capabilities). This page is a map of the architecture and the capability groups, not an exhaustive per-cap reference.

---

## 1. The core loop

A dream cycle is, at heart, an ordered list of stage capability names threaded with a shared state dict — the same pattern as the [DAG engine](./03-dag-engine.md), but self-initiated:

```
gather → themes → plan → execute → synthesize → deliver
```

Each stage is a real `@capability`. You can add stages, swap them, reorder them, or write your own — just register a new `dream.stage.X` cap and list it in a trigger's pipeline. Nothing about the pipeline is hard-coded.

---

## 2. Triggers

A **trigger** record is the unit of configuration. It declares:

| Field group | Says |
|---|---|
| **When** | hours window, idle threshold, cooldown |
| **What to sense** | which `dream.sensor.*` caps to call (memory, fabric, syslog, research, event bus, RSS news, …) |
| **How to act** | `synthesize_only` \| `plan_execute` \| `oneshot` |
| **What to deliver** | any channels from the [delivery registry](#10-delivery-channels) — `telegram` \| `memory` \| `notebook` \| `email` \| `chat` \| skill-defined — with optional per-channel output-format and target (`deliver_config`) |

| Cap | Purpose |
|---|---|
| `dream.trigger.list` / `dream.trigger.get` | Browse triggers |
| `dream.trigger.upsert` | Create/update a trigger |
| `dream.trigger.delete` / `dream.trigger.toggle` | Remove / enable-disable |
| `dream.trigger.generate` | **LLM**: synthesise a trigger from a description |

---

## 3. Sensors — firing gates (and collectors — content)

**Sensors gate, collectors feed.** A sensor's job is to decide *whether* a trigger should fire (`_trigger_due` evaluates them with per-sensor `match`/`min_signal` conditions); their thin, truncated samples are tuned for that decision, not for reasoning over. A trigger that wants a substantial working set declares **`collect`** — a list of real data-gathering cap calls:

```json
"collect": [
  {"cap": "web.search",        "label": "AI news", "args": {"query": "…", "limit": 10}},
  {"cap": "dream.journal.read","label": "Week's ledger", "args": {"journal_id": "trigger:security_watch", "limit": 250}}
]
```

When `collect` is present, `dream.stage.gather` runs the collectors for content (any cap family works — results are normalised into the working-set shape) and the trigger's sensors are only used to gate firing. Without `collect`, legacy behaviour is unchanged: sensors are run for content. Edit collectors in Pipeline config → *Content collectors*.

Sensors are read-only caps that pull a slice of Vera's world into the cycle's working set. They fall into groups:

| Group | Sensors (examples) |
|---|---|
| Internal state | `dream.sensor.memory_recent`, `memory_session`, `memory_graph_walk`, `fabric_recent`, `fabric_dataset`, `fabric_by_tag`, `fabric_by_source_type`, `cap_calls`, `notebook_recent` |
| External world | `dream.sensor.web_feed`, `news_overnight`, `research_recent`, `source_changes`, `source_review_state` |
| System health | `dream.sensor.syslog_errors`, `bus_events` |
| Workspace / projects | `dream.sensor.ide_workspace`, `project_context`, `active_projects` |

`dream.sensors.list` enumerates them, `dream.sensor.preview` runs one for inspection, and **custom sensors** can be authored at runtime:

| Cap | Purpose |
|---|---|
| `dream.sensor.custom.list` / `create` / `delete` / `run` | User-defined sensors |

---

## 4. Stages — reasoning & acting

Stages transform the working set or take action. The shipped stages cover the full reason-act surface:

| Group | Stages (examples) |
|---|---|
| Context | `dream.stage.gather`, `enrich_context`, `snapshot_source` |
| Reasoning | `think_reflect`, `themes`, `goal_refine`, `plan` |
| Execution | `execute`, `cap_execute`, `dag_execute`, `stepwise_execute`, `agent_loop`, `investigate` |
| Knowledge | `memory_deep_traverse`, `fabric_explore` |
| Projects / IDE | `project_action`, `ide_workspace_act`, `ide_agent`, `load_workspace` |
| Output | `synthesize`, `propose_action`, `quality_check`, `deliver` |
| Code review | `review_codebase`, `review_report`, `deep_review` |
| Iteration | `pivot`, `iterate` |

`dream.stages.list` enumerates them; **custom stages** mirror custom sensors:

| Cap | Purpose |
|---|---|
| `dream.stage.custom.list` / `create` / `delete` | User-defined stages |

### Prompting style — one-shot vs agentic loop

The three "brain" stages `agent_loop`, `investigate`, and `stepwise_execute` support a **per-stage prompting style**, set in `stage_config.<stage>.prompt_style`:

- **`one_shot`** — a single grounded LLM prompt (no tools), streamed to the panel via the same ollama path as the rest of the pipeline. Use it for pure **analysis or documentation** generation, where a tool loop is wasted cost.
- **`agent_loop`** — the tool-using ReAct loop (default engine v5). Use it when the task must *do* things: call tools, gather evidence iteratively, or edit.

**Defaults, by stage** — only `stepwise_execute` is agentic by default:

| Stage | Default style |
|---|---|
| `stepwise_execute` | `agent_loop` (the designated agentic stage) |
| `agent_loop` | `one_shot` |
| `investigate` | `one_shot` |

Pick it in the Composite Pipelines editor (a dropdown appears for each of these stages) or by hand in the stage-config JSON; a trigger/pipeline-level `prompt_style` sets a default for all its stages. A pipeline that genuinely needs a tool loop should use `stepwise_execute`, or set `prompt_style: agent_loop` on its stage.

**Source review is always one-shot**: `review_codebase` runs one streaming ollama review per file and `deep_review` streams per chunk — they never hand over to the agent loop. Drafting fixes (which *does* need to edit) lives in the separate `source_review_fix` pipeline via `ide_workspace_act`.

> Existing installs are migrated automatically on startup: stale `source_review*` pipelines that still contained agentic stages are rewritten to one-shot, and any pre-existing pipeline/trigger whose `agent_loop`/`investigate` stage has a whitelist that can act is pinned to `prompt_style: agent_loop` so the new one-shot default doesn't silently switch its tool loop off.

---

## 5. Running cycles

| Cap | Purpose |
|---|---|
| `dream.scheduler.start` / `stop` / `status` | The idle-watcher that fires triggers automatically |
| `dream.cycle.run` | Run a cycle now (a given trigger or ad-hoc pipeline) |
| `dream.cycle.continue` | Resume a paused/HITL cycle |
| `dream.cycle.cancel` | Abort a running cycle |
| `dream.cycle.detail` | Full record of one cycle |
| `dream.cycle.progress` | **Live, poll-able progress snapshot** — per-stage status/elapsed, LLM token heartbeat, agent-loop session (for re-attach), output files. The panel polls this every 3s so activity keeps rendering even when the event stream drops |
| `dream.cycle.files` / `dream.cycle.file` | List / read the cycle's output-workspace files |
| `dream.history` / `dream.last` / `dream.timeline` | Past cycles, most-recent, and a time-ordered view |
| `dream.preview` / `dream.preview.last` | Dry-run a pipeline without acting |
| `dream.schedule.events` | Upcoming scheduled fires |

### Output workspace — files, not context

Every (non-preview) cycle collates its material into real files under `vera/dream/outputs/<cycle_id>/` as stages complete: `01-gather.md` (full, untruncated working set), `02-themes.md`, `03-plan.md`, `04-findings.md` (appended per iteration by the investigate/agent-loop/project_action stages), `report.md`, `journal.md`, `meta.json`. The file list rides on the history record and cycle detail (chips → click to view), and agent loops launched by dream stages are instructed to collate substantial results into durable output (notebook/workspace files) rather than carrying everything in their context window.

### Daily report dreams

Six collector-based report triggers ship enabled by default (all deliver to notebook + memory; add `podcast`/`telegram`/`email` per trigger in the deliver row):

| Trigger | Cadence | Content |
|---|---|---|
| `daily_ai_report` | morning | wider AI/ML news, releases, research (web.search) |
| `daily_local_ai_report` | morning | local/self-hosted AI: open weights, llama.cpp/ollama ecosystem, hardware |
| `daily_homelab_report` | morning | homelab & self-hosting releases and community highlights |
| `security_watch` | daily | terse CVE/breach ledger; accretes into its journal + fabric, `[STACK]`-flags homelab-relevant items |
| `weekly_security_digest` | weekly | synthesises the week of `security_watch` ledgers into a digest with actions |
| `daily_ops_report` | evening | detailed self-report: cycles run/outcomes/durations, errors, cap usage, director activity, tuning suggestions |

---

## 6. Human-in-the-loop & safety

**HITL.** If a trigger has `hitl=True` and a Telegram admin chat is configured, the execute stage sends *"I've been thinking about X — should I do Y?"* and waits (up to `default_hitl_timeout_s`) for a reply. Answer `yes`/`ok`/`go`/`do it` to approve, anything else to cancel.

| Cap | Purpose |
|---|---|
| `dream.hitl.pending` | Approvals awaiting a human |
| `dream.hitl.respond` | Approve/deny from the UI |
| `dream.hitl.clear` | Drop stale pending approvals |

**Capability whitelist.** A whitelist gates which tools the planner may use while dreaming — dreams cannot run arbitrary code, only caps the admin has explicitly allowed. Sensible defaults (memory, fabric, nlp, llm, syslog, and the dream sensor/stage caps themselves) are seeded on first start.

| Cap | Purpose |
|---|---|
| `dream.whitelist.list` / `dream.whitelist.set` | Manage the allowlist |
| `dream.config.get` / `dream.config.set` | Global dream settings (idle threshold, HITL timeout, …) |

---

## 7. Higher-level constructs

**Think tasks** — standing prompts Vera works on during idle time:

| Cap | Purpose |
|---|---|
| `dream.think.create` / `list` / `delete` | Manage think tasks |
| `dream.think.run` / `dream.think.stream` | Run one (streaming) |

**Pipelines** — named, reusable stage sequences:

| Cap | Purpose |
|---|---|
| `dream.pipeline.list` / `get` / `upsert` / `delete` | Pipeline CRUD |
| `dream.pipeline.run` | Execute a saved pipeline |

**Review subsystem** — autonomous codebase/document review with selectable output styles (shares `REVIEW_STYLES` from `output_formats.py` with chat):

| Cap | Purpose |
|---|---|
| `dream.review.styles` / `dream.review.run` | List styles / launch a review |
| `dream.review.status` / `pause` / `resume` | Control a running review |
| `dream.review.list` / `get` / `search` / `grep` | Browse review results |
| `dream.review.areas` / `area_report` / `source` | Per-area drill-down |
| `dream.review.runs` / `snapshots` / `clear` | Run history & cleanup |

**Director** — `dream.director.assess` evaluates whether (and what) to dream about next. Every `dream.director.thought` raises a small clickable notification in the Dream panel (bottom-right); clicking it opens a reply popover — `dream.director.reply` feeds your text back into its train of thought and shows the director's answer in place. The Director card header also has a standing 💬 Reply button, and diamonds on the activity timeline open the same popover on a past thought.

**Journal** — `dream.journal.append` / `read` / `list` / `clear`: Vera's running diary of its idle thoughts.

---

## 8. Projects

Several sensors/stages (`dream.sensor.project_context`, `active_projects`, `dream.stage.project_action`) integrate with a companion **projects** subsystem (`dream/project_capabilities.py`), which gives dream cycles a notion of long-running goals to make progress against rather than one-shot reflections.

---

## 9. UI

| Panel | File | Shows |
|---|---|---|
| **Dream** | `dream_panel.html` | Triggers, live cycle viewer, sensors/stages, whitelist, journal, merged timeline & history |
| **Dream Pipelines** | `dream_pipelines_panel.html` | Build and run saved pipelines |
| **Dream Review** | `dream_review_panel.html` | Launch and browse autonomous reviews |

**Live view.** Besides the event-driven stream/agent-loop cards, the Activity card renders a poll-driven progress rail from `dream.cycle.progress`: pipeline chips with per-stage status + real elapsed time, the agent-loop session (with ▶ Watch re-attach), an LLM token heartbeat, and the output-file chips — so project/goal dreams and long orchestrator loops stay visible even if the event subscription drops mid-cycle.

**Timeline & History (merged).** The Timeline nav entry opens one section: an *activity timeline* (one lane per dream feature — project dreams, source review, reports, security, research, director thoughts — with bars sized by each cycle's real duration; click a bar to open the full record, click a ◆ to reply to that director thought), the compact *upcoming fire windows* gantt, and the searchable history list. The old three-card timeline (hour bars + scheduled list) is gone.

**Pipeline builder.** The canvas (`<vera-flow-builder>`) is the default view when a trigger is selected; the compact list remains available via the toggle (remembered in `localStorage`).

`dream.chat` provides a conversational interface to interrogate what Vera dreamed about, and `dream.preview` lets you watch a pipeline assemble before letting it act.

---

## 10. Delivery channels

`dream.stage.deliver` no longer hard-codes telegram/memory/notebook. Channels live in a shared registry — **`vera/delivery.py`** — the routing twin of the output-format registry (`vera/output_formats.py`). It is the same pluggable pattern: built-in channels plus runtime channels contributed by the Skills library.

| Built-in channel | Cap | Default format | Target |
|---|---|---|---|
| `telegram` | `tg.notify` | — (pick *short* to reshape) | — |
| `memory` | `memory.store` | — | — |
| `notebook` | `notebook.create` | markdown | — |
| `email` | `mail.send` | email | recipient (blank = default account) |
| `chat` | `chat.deliver` | standard | chat session id |
| `fabric` | (always-on sink) | — | — |

For each selected channel the deliver stage renders the report through the channel's **output-format profile** (`apply_format` — transformative formats like *short/email* run one LLM reshape pass; markdown-ish formats pass through), then calls the channel's cap. Per-trigger overrides live in `trigger.deliver_config[channel] = {format, target}`. `delivery.channels` lists the registry (the dream deliver UI is built from it); `chat.deliver` renders a report straight into a chat conversation via the panel-dispatch bridge.

**Authoring channels.** A `delivery_channel` skill IS a registry entry — author one in the Skills library (cap + default format + optional fixed target) and it appears in the deliver UI automatically. This mirrors how an `output_format` skill becomes a chat output style; the two systems are deliberately symmetric.

## 11. Chat → projects & thoughts

The chat panel's per-message actions can route a finished answer into the dream system:

- **+ Project** — folds the message into a project's rolling `llm_context` (`project.note.add`), creating the project if you name a new one. Same incremental-LLM-merge path a dream cycle uses.
- **💭 Think later** — marks the message for later thought via `dream.think.create`: a thinking-loop (optionally scoped to a project) Vera revisits each idle slot, with the message as its goal.

---

## See also

- [DAG Engine](./03-dag-engine.md) — the pipeline/execution pattern dream stages build on
- [Memory Graph](./05-memory-graph.md) & [Data Fabric](./06-data-fabric.md) — the primary sensor sources, and where deliveries land
- [Agents & Chat](./19-agents-chat.md) — `dream.stage.agent_loop` reuses the agentic loop
- [Research System](./07-research.md) — `dream.sensor.research_recent` surfaces research artifacts
- [Integrations](./23-integrations.md) — Telegram delivery + HITL approvals
