# 19 · Agents & Chat

`agents/agents.py` defines Vera's **agents** — named, configurable LLM personas — and the **agentic loop** they run in. The chat surface (`chat/chat_panel.html`) is where a human talks to them, and the same loop renderer is reused across the harness wherever an agent runs.

---

## 1. What an agent is

An agent bundles:

- A specific model + a full set of generation parameters
- A system/personality prompt
- A **domain focus** — `domain_caps`, the subset of capabilities it may use, plus a `domain_description`
- An optional **tool mode** — whether and how it invokes capabilities

Architecturally there are three pieces: `AgentRecord` (a dataclass of all config), `AgentRegistry` (CRUD + Redis/Postgres persistence), and `AgentRunner` (executes a turn, text + optional TTS). Each agent is itself a registered `@capability` (`agent.call_with_tools`) so it can be dropped into a DAG.

### `tool_mode`

| Value | Behaviour |
|---|---|
| `''` / `none` | Pure chat — no capability access |
| `call` | The agent may invoke Vera capabilities as tools during its turn |
| `plan` | The agent emits a [DAG](./03-dag-engine.md); Vera extracts and executes it |

When `domain_caps` is set and `tool_mode != none`, the agent only sees and can call that allowlist — and the [capability ontology](./18-skills-ontologies.md) can tell it that *adjacent* caps exist without granting access.

---

## 2. Capabilities

| Cap | Purpose |
|---|---|
| `agent.create` / `agent.update` | Define/modify an agent |
| `agent.list` / `agent.get` / `agent.delete` | Registry browse + soft-delete |
| `agent.chat` | Send a message, get a text response |
| `agent.chat_voice` | Send a message, get text + TTS audio (GPU server) |
| `agent.call_with_tools` | Run an agent that invokes capabilities as tools |
| `agent.models` | Models available to agents |
| `agent.history` / `agent.restore_version` | Version history of an agent definition |
| `agent.list_fabric` / `agent.restore_from_fabric` / `agent.purge_fabric_duplicates` | Fabric-backed snapshots of agent definitions |

Agent definitions persist to Redis (always) and Postgres (when available), with fabric snapshots for versioned restore.

---

## 3. The agentic loop

Tool-using turns run as a streamed **agentic loop**, mounted outside the `@capability` system as a raw SSE endpoint so it can stream:

```
POST /workshop/agent_loop/stream   → text/event-stream of loop events
```

The loop is cyclic: it triages the request, assembles a **dynamic toolkit** (the caps relevant to this goal), then runs cycles of *think → choose args → execute → observe*, with error-recovery, long-running awaits, and a final handover-synthesis step. Events are versioned (`agent_loop_v2.*`, …); the loop has evolved through several iterations, with **V4 the current implementation**.

### The shared renderer — `<vera-agent-loop-output>`

[`agent_loop_ouput.js`](../vera/agent_loop_ouput.js) is a self-contained custom element that renders the full loop event stream: triage banner, dynamic toolkit, cycle cards (thinking / args / live progress / research streams / error-recovery boxes / awaits), **HITL pause cards**, handover synthesis, and a structured final-result pane. It is the *same* renderer used by the DAG Workshop's Agent Loop tab, lifted into a registered injectable element so chat, orchestration sub-panels, and the [Dream panel](./17-dream.md) all reuse it instead of duplicating the UI.

```js
el.bindStream('/workshop/agent_loop/stream', requestBody); // stream a run
el.appendEvent(ev);                                         // or feed events manually
el.setHitlEndpoint('/workshop/agent_loop/hitl/respond');    // HITL approvals
```

Human-in-the-loop pauses surface as cards in the stream; the human answers via the HITL respond endpoint and the loop continues.

---

## 4. Generation parameters

Every Ollama parameter is exposed per-agent (server defaults apply when unset): `temperature`, `top_p`, `top_k`, `repeat_penalty`, `repeat_last_n`, `num_ctx`, `num_predict`, `seed`, `mirostat` / `mirostat_tau` / `mirostat_eta`, `tfs_z`, and `stop` sequences.

---

## 5. Voice

`agent.chat_voice` returns text plus synthesised audio via the GPU inference server's TTS, and the chat interface captures microphone input through the GPU server's Whisper STT (see [LLM Cluster §8](./04-ollama-cluster.md)). The chat panel wires both into a hands-free conversational loop.

---

## 6. Chat panel & the panel bridge

`chat/chat_panel.html` is the full conversational UI: agent picker, the embedded `<vera-agent-loop-output>` renderer, STT mic / TTS speaker, and a progressively-enhanced options rail (config checkboxes become richer `.opt` cards at runtime).

Chat can also **drive other panels**. `chat/chat_panels_capabilities.py` registers the panel bridge — `panel.dispatch` and `panel.query` — which routes actions into whichever panel is currently mounted in the harness (e.g. chat asks the Fabric panel to run a query and reads the result back). The same file also hosts the shared `ui.theme.*` / `ui.panel.*` / `ui.caps.*` caps (see [UI Builder](./26-ui-builder.md)).

---

## 7. UI panels

| Panel | Purpose |
|---|---|
| `agents-editor` | Create/edit/delete agents, tune parameters, set `domain_caps` + `tool_mode` |
| `chat-interface` | Full chat with STT (mic) + TTS (speaker), agent loop streaming |

---

## See also

- [DAG Engine](./03-dag-engine.md) — `tool_mode="plan"` agents emit DAGs; the Agent Loop tab shares this renderer
- [Skills & Ontologies](./18-skills-ontologies.md) — skills shape agent prompts; `domain_caps` + cap-ontology adjacency
- [LLM Cluster](./04-ollama-cluster.md) — model routing, GPU STT/TTS
- [Dream](./17-dream.md) — `dream.stage.agent_loop` runs this same loop autonomously
- [Capability Framework](./01-capability-framework.md) — agents-as-capabilities, event streaming

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
