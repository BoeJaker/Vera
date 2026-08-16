# 03 · DAG Engine

![Agent Loop Graph captured from the running Vera UI](assets/overview/loop-graph.png)

Vera's DAG engine lets you compose capabilities into multi-step workflows. A DAG is a list of nodes, each calling one cap and writing its output into a shared state dict. Nodes can run sequentially, in parallel branches, or conditionally. An LLM-powered planner can produce a DAG from a natural-language goal, and a supervised mode inserts LLM checkpoints between every step.

The DAG Workshop tab in the harness is the interactive surface; the capabilities are also callable from MCP, REST, and as nodes inside other DAGs.

---

## 1. DAG syntax

A DAG is a JSON list of nodes. Each node is a 2- or 3-element list:

```json
[
  ["cap.name", "output_key"],
  ["cap.name", "output_key", "CONDITION:state_key"],
  [["cap.a", "out_a"], ["cap.b", "out_b"]]
]
```

Element semantics:

| Position | Meaning |
|---|---|
| 0 | Capability name (must exist in `CAPABILITY_REGISTRY`) |
| 1 | State key to write the result into |
| 2 (optional) | `"CONDITION:state_key"` — skip this node if `state[state_key]` is falsy |

A node that is itself a **list of node lists** runs all its children in parallel.

### Example: ping + summarise

```json
{
  "dag": [
    ["system.ping",    "host_status"],
    ["llm.summarize",  "summary", "CONDITION:host_status"]
  ],
  "initial_state": {
    "host": "example.com",
    "text": "(filled in by ping)"
  }
}
```

The state dict is threaded through every node. A cap's parameters are filled from the state key of the same name, so naming output keys after the next cap's input parameter is the idiomatic pattern.

### Example: parallel fan-out

```json
[
  ["fabric.query",                    "results"],
  [
    ["llm.summarize",                 "summary"],
    ["nlp.entity_extract",            "entities"],
    ["nlp.sentiment",                 "sentiment"]
  ],
  ["llm.compose_report",              "report"]
]
```

The middle node is a list of three nodes; all three run concurrently and write into `state` before the final `compose_report` runs.

---

## 2. Run modes

The DAG engine exposes three run modes via capabilities:

### `dag.run` — plain execution

```python
await run_graph(dag, state)
```

Executes the DAG linearly. State flows through. No LLM involvement — every cap runs unconditionally (except for nodes with `CONDITION:` predicates).

### `dag.run_supervised` — checkpointed

After every step, an LLM inspects the result and decides:

- `continue` — proceed to the next step
- `retry` — re-run this step (useful when the result looked wrong)
- `abort` — stop the DAG

This gives you safety on long-running plans where one bad step would waste later steps. Set via `dag.run`'s `supervised=true` flag.

### `dag.run/monitored` — auto-correcting

Detects per-step errors and asks the LLM to repair the node's parameters. If correction succeeds, the step is re-run; if not, the error is surfaced. Returns `{result, errors_found, corrections: [{step, success, ...}]}`.

---

## 3. The planner

`dag.plan` takes a natural-language goal and produces a DAG:

```python
await plan_dag(goal, capabilities=None)
```

The planner:

1. Builds a system prompt describing the goal, the available capabilities (or a filtered subset if `capabilities=...` is supplied), and the DAG JSON schema.
2. Calls the LLM to produce a DAG with `initial_state` and a `rationale`.
3. Validates that every named capability exists, every required parameter is satisfied either by `initial_state` or by an upstream node's output, and the result fits the JSON schema.
4. Returns `{dag, initial_state, rationale, warnings, error?}`.

### `dag.plan_and_run`

The combined cap: plan + immediately execute. Supports `supervised=true` to add the LLM-checkpoint layer on top of the planned DAG.

### `dag.plan_stream` — streamed planning and execution

A streaming SSE endpoint that emits events as the plan is built and executed:

```
dag.planning        — LLM is composing
dag.plan_ready      — plan validated, about to execute
dag.step_start      — node N starting
dag.step_done       — node N complete (result preview)
dag.step_error      — node N failed
dag.hitl_request    — pause for human approval (HITL mode)
dag.hitl_approved
dag.hitl_rejected
dag.complete        — final state
dag.error           — fatal error
[DONE]              — end of stream
```

This is what powers the live DAG Workshop output strip.

### Stepwise mode

`mode="stepwise"` in `dag.plan_stream` switches from "plan everything up front, then execute" to "plan one step at a time, executing each before planning the next". The LLM sees the previous results when planning the next step, so it can adapt the plan based on what actually happened. Slower but more robust for under-specified goals.

---

## 4. HITL (human in the loop)

When a DAG is run with `hitl=true`, the engine pauses before every cap call and emits a `dag.hitl_request` event:

```json
{
  "type":         "dag.hitl_request",
  "trace_id":     "...",
  "cap":          "research.run",
  "params":       {...},
  "auto_approve_secs": 30
}
```

The client (panel or external agent) responds via `POST /dag/hitl/respond`:

```json
{
  "trace_id":      "...",
  "action":        "approve|reject|edit",
  "edited_params": "..."     // when action="edit"
}
```

If no response arrives within `auto_approve_secs`, the engine auto-approves with the original parameters and emits `dag.hitl_auto_approved`.

This is wired into Telegram for off-device approval — see `agents.py` and the chat panel's HITL toggle.

---

## 5. The agentic loop

A separate variant, `dag.agent_loop` (and its more flexible cousin `dag.agent_loop_v3`), runs an **open-ended** agentic loop rather than a fixed DAG:

1. Build a system prompt with the available toolkit (filtered by relevance to the goal).
2. Loop:
   - LLM emits one tool call in JSON: `{action:"call", tool:"...", args:{...}}` or `{action:"done", summary:"..."}` or `{action:"defer", question:"..."}`.
   - Engine dispatches the tool via `ide.code.tool_dispatch` or directly via the cap registry.
   - Result is fed back into the LLM context as an observation.
3. Stop when the LLM emits `done`, the user clicks Stop, or `max_cycles` is reached.

The loop registers itself as a `streams.agent_loop` stream so observers can watch live. Each cycle emits `agent_loop.cycle_planning`, `agent_loop.tool_done`, etc.

### Phase model (think → explore → act → validate)

v2 and v3 run a **phased** loop (on by default, `phased=True`) that forces information-gathering before action:

- **Think / Explore** — until `min_explore_cycles` (default 2) cheap read-only calls (get/list/search/query/describe — see `_cap_phase`) have succeeded, the agent is in the explore phase. Action ("act") tools are **blocked** with a nudge if requested early, and **long-running** tools (research, ml, exec…) trigger a **forced HITL approval** (`long_running_force_hitl=True`) regardless of the global HITL setting — the run pauses for a human go/no-go.
- **Act** — once exploration is satisfied, action tools run normally.
- **Validate** — when `require_validate=True`, the agent must run one read-only check after acting before its `final`/`done` is accepted; the first attempt to finish without validating is bounced once.

Phase transitions emit `agent_loop_v{2,3}.phase` events, surfaced as badges in the loop output renderer.

### Continue (budget extension)

When the cycle budget is reached, instead of forcing a final answer the loop emits `agent_loop_v{2,3}.budget_pause` and waits (`allow_continue=True`). The output renderer shows a **Continue (+N)** / **Wrap up now** card that POSTs to `/workshop/agent_loop/hitl/respond` (decision `continue`/`wrap`, on a negative `step` id). `continue_increment` (default 8) cycles are added per continue; `auto_continue_max` (default 0) auto-extends N times before asking.

### The loop builder

The DAG Workshop has a visual loop builder (`dag_workshop_panel.html`) that compiles a flow of named blocks (Triage, Seed toolkit, **Think, Explore**, Planner, HITL, **Act**, Executor, **Validate**, Satisfy, Expand toolkit, Loop, DAG, Prompt) into the kwargs for `dag.agent_loop_v3`. The four phase blocks (Think/Explore/Act/Validate) drive the phase model: presence of any of them sets `phased`, the Explore block sets `min_explore_cycles`, Validate sets `require_validate`, Act sets `long_running_force_hitl`, and the Loop block carries the continue settings. Each block has a small config form and the compiled config is shown live in the inspector. A **Load variant** menu populates the canvas with the real v1/v2/v3 pipeline as editable blocks (`LB_VARIANT_PIPELINES`) so each variant's steps can be seen and modified.

---

## 6. Storage and reuse

DAGs can be saved to Redis via `dag.store.save`:

```python
await api("/dag/store/save", "POST", {
    "name": "my-dag",
    "dag":   dag_array,
    "initial_state": {...},
    "description": "..."
})
```

Saved DAGs are listed by `dag.store.list` and invoked by `dag.store.run`. The DAG Workshop's "Store" tab is the UI.

---

## 7. Common patterns

### Threading session_id through every step

Capabilities that record to the memory graph need a `session_id`. The DAG engine doesn't auto-inject it, so put it in `initial_state`:

```json
{
  "initial_state": {
    "session_id": "abc123",
    "query":      "find me X"
  }
}
```

State key lookup matches by name, so every cap whose signature has `session_id: str = ""` will pick it up automatically.

### Conditional branches

```json
[
  ["fabric.query",      "results"],
  ["llm.compose",       "answer", "CONDITION:results"],
  ["llm.apologise",     "answer", "CONDITION:!results"]
]
```

Note: the engine supports `CONDITION:state_key` (truthy) but **not** `!state_key` directly. To implement a "false" branch, run an intermediate cap that negates the value, or wrap the logic in a single cap that handles both.

### Avoiding capability invention

The planner sometimes makes up capability names. The DAG engine validates against `CAPABILITY_REGISTRY` and emits a warning if a planned cap doesn't exist. The `dag-fixer` agent preset (see `agents.py`) is designed to repair these — it sees the error, the cap manifest, and the broken DAG, and returns a corrected version.

---

## 8. From the harness

The DAG Workshop tab has four panes:

- **Planner** — enter a goal, hit Plan or Plan+Run. `dag.plan` returns the DAG plus a **problem → subgoals → validation** framing (the same convention as the agentic loop's phases): `problem` restates the goal, `subgoals` is an ordered list of `{step, description, caps}`, and `validation` describes how success is verified. The review card shows the SVG-rendered DAG, the rationale, these three sections, and any warnings.
- **Editor** — raw JSON for the DAG and initial state, plus a parameter-aware cap picker that builds nodes interactively.
- **Loop builder** — drag-and-drop visual composer for agent_loop flows.
- **Store** — save / load / browse stored DAGs.

The Fix / Explain / Modify buttons feed the current DAG into the planner with a directive ("fix this", "explain in plain English", "modify to ...") and update the editor with the result.

---

## See also

- [Capability Framework](./01-capability-framework.md) — what each DAG node calls
- [IDE Module](./08-ide.md) — the agentic loop powering the coding agent
- [Research System](./07-research.md) — research jobs as multi-step DAGs

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
