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
| **What to deliver** | telegram \| memory \| notebook \| all |

| Cap | Purpose |
|---|---|
| `dream.trigger.list` / `dream.trigger.get` | Browse triggers |
| `dream.trigger.upsert` | Create/update a trigger |
| `dream.trigger.delete` / `dream.trigger.toggle` | Remove / enable-disable |
| `dream.trigger.generate` | **LLM**: synthesise a trigger from a description |

---

## 3. Sensors — gathering context

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

---

## 5. Running cycles

| Cap | Purpose |
|---|---|
| `dream.scheduler.start` / `stop` / `status` | The idle-watcher that fires triggers automatically |
| `dream.cycle.run` | Run a cycle now (a given trigger or ad-hoc pipeline) |
| `dream.cycle.continue` | Resume a paused/HITL cycle |
| `dream.cycle.cancel` | Abort a running cycle |
| `dream.cycle.detail` | Full record of one cycle |
| `dream.history` / `dream.last` / `dream.timeline` | Past cycles, most-recent, and a time-ordered view |
| `dream.preview` / `dream.preview.last` | Dry-run a pipeline without acting |
| `dream.schedule.events` | Upcoming scheduled fires |

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

**Director** — `dream.director.assess` evaluates whether (and what) to dream about next.

**Journal** — `dream.journal.append` / `read` / `list` / `clear`: Vera's running diary of its idle thoughts.

---

## 8. Projects

Several sensors/stages (`dream.sensor.project_context`, `active_projects`, `dream.stage.project_action`) integrate with a companion **projects** subsystem (`dream/project_capabilities.py`), which gives dream cycles a notion of long-running goals to make progress against rather than one-shot reflections.

---

## 9. UI

| Panel | File | Shows |
|---|---|---|
| **Dream** | `dream_panel.html` | Triggers, live cycle viewer, sensors/stages, whitelist, journal, history/timeline |
| **Dream Pipelines** | `dream_pipelines_panel.html` | Build and run saved pipelines |
| **Dream Review** | `dream_review_panel.html` | Launch and browse autonomous reviews |

`dream.chat` provides a conversational interface to interrogate what Vera dreamed about, and `dream.preview` lets you watch a pipeline assemble before letting it act.

---

## See also

- [DAG Engine](./03-dag-engine.md) — the pipeline/execution pattern dream stages build on
- [Memory Graph](./05-memory-graph.md) & [Data Fabric](./06-data-fabric.md) — the primary sensor sources, and where deliveries land
- [Agents & Chat](./19-agents-chat.md) — `dream.stage.agent_loop` reuses the agentic loop
- [Research System](./07-research.md) — `dream.sensor.research_recent` surfaces research artifacts
- [Integrations](./23-integrations.md) — Telegram delivery + HITL approvals
