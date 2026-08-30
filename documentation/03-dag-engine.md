# 03 · DAG Engine

![Agent Loop Graph captured from the running Vera UI](assets/overview/loop-graph.png)

Vera's DAG engine lets you compose capabilities into multi-step workflows. A DAG is a list of nodes, each calling one cap and writing its output into a shared state dict. Nodes can run sequentially, in parallel branches, or conditionally. An LLM-powered planner can produce a DAG from a natural-language goal, and a supervised mode inserts LLM checkpoints between every step.

The DAG Workshop tab in the harness is the interactive surface; the capabilities are also callable from MCP, REST, and as nodes inside other DAGs.

## Run protocol shadow

The W1-01 integration wraps the existing `dag.run` path with a
runtime-neutral Run projection. It does not replace the DAG engine: native
inputs, scheduling, HITL, cancellation, results, and failures remain
authoritative. Observation failure is isolated so it cannot change the native
result.

The projection records versioned Run events, child task lineage, causation,
attempts, progress, content-free artifact references, and a checksummed journal.
Activity UI cards link the projected workflow and trace identity back to the DAG
Workshop and clearly label the view `run_protocol_shadow` versus authority
`native_dag`. Control records describe request/acknowledgement intent only; they
never execute a native approve, reject, retry, resume, or cancel operation.

When `VERA_RUN_JOURNAL_PATH` enables the SQLite journal, new Runs also persist
immutable identity metadata and a sequence-bound, content-free projection
checkpoint. On process start, the shadow registry verifies each Run's checksum
chain and reconstructs the newest bounded catalog, including parent/child,
workflow, task, session and trace identity, progress, attempts, errors and
artifact references. One corrupt Run is omitted and reported without preventing
healthy Runs from loading. Recovery rebuilds observation only: it never invokes
the DAG, a capability, or a control request.

`run.shadow.list`, `run.shadow.get`, and `run.shadow.graph` expose the same
content-free recovery summary: whether startup recovery ran, recovered and
quarantined counts, the configured catalog bound, and hashed quarantine
references. Raw IDs from other quarantined Runs and error messages are not
returned.

Journals created before identity/checkpoint metadata was introduced can still be
verified and exported, but older event rows may recover only the fields encoded
in those events. The implementation does not invent missing lineage.

This slice is intentionally a compatibility facade. Later Workflow IR and
runtime-adapter work can emit the same contract without requiring Vera to replace
LangGraph, external runtimes, or its own established DAG execution paths.

### Runtime-neutral durability fixture

LIB-15 adds a static conformance fixture in
[`durability_fixture.py`](../vera/execution/durability_fixture.py). It combines a
normalized Workflow IR definition with pinned definition/implementation
revisions and expected Run event sequences for clean completion, retry,
timeout, cancellation, compatible-version resume, incompatible-version refusal,
and a crash immediately before and after every step. The workflow declares a durable wait, runtime-owned
retry and timeout, and an external effect protected by an idempotency key and a
durable receipt.

`RuntimeDurabilityProfile` lets an adapter declare exactly which semantics and
Run events it can preserve. `analyze_durability_profile` compares that profile
with the fixture and returns explicit gaps. It rejects an `exactly_once` claim
for external effects: the portable contract is replay plus deduplication by key
and retained receipt, because a runtime cannot manufacture exactly-once behavior
in an independent service.

The fixture and analyzer both report `executes: false`. They do not import or
start DBOS, Temporal, Prefect, Dagster, or Vera's native DAG runner, and they do
not sleep, retry, recover, cancel, or perform the declared effect. Their purpose
is to make later runtime pilots comparable before any engine is entrusted with
real workflow authority.

The same inspection boundary is available to tools as
`workflow.durability.fixture` and `workflow.durability.gaps`. The latter accepts
either a named built-in Workflow IR adapter or one strict serialized runtime
profile; ambiguous or malformed input fails closed. Both capability contracts
declare no effects.

LIB-16's first offline slice adds `workflow.durability.dbos_mapping`. It binds
the fixture identity and Workflow IR hash to a `dbos==2.30.0` review manifest,
mapping workflow IDs, application versions, steps, durable sleep, retry/timeout
options, statuses, and Run-event evidence sources. The manifest is data rather
than generated Python and reports `executes: false`, `imports_runtime: false`.

It intentionally remains `ready_for_execution: false`. Blocking gaps cover the
absolute-wake-to-duration conversion, DBOS's timeout/cancel status ambiguity,
compatible-version patch planning, independent-effect key/receipt evidence,
Run-event observation completeness, large-result ArtifactRef policy, and
cancellation boundaries. Those gaps require the separately authorized DBOS and
Postgres crash-recovery spike; the manifest does not claim that documentation
alone proves them.

LIB-17 adds the corresponding offline Temporal comparison as
`workflow.durability.temporal_paper`. It pins `temporalio==1.32.0`, binds the
same fixture and the exact DBOS mapping identity, and maps the canonical steps
to Workflows, Activities, durable timers, retry/timeout options, cancellation,
history events, Signals/Updates, patching, and Worker Versioning candidates.
The output is a deterministic review manifest: it never imports the SDK, starts
a Worker, connects to a Temporal service, or executes a workflow.

The decision gate is deliberately asymmetric with a benchmark. Temporal is
recorded as a candidate for cross-service workers, long-lived histories,
versioned routing, messages, child workflows, schedules, visibility/retention,
and in-flight migration. Every category remains `requires_live_evidence` until
a named DBOS limitation is measured against the same Vera fixture. Consequently
the manifest reports `recommendation: defer`, `decision_ready: false`, and
`live_pilot_approved: false`; documentation breadth is not treated as proof of
runtime superiority.

## Workflow IR inspection facade

The held W1-02 foundation introduces a versioned, runtime-neutral description
layer without changing execution. `workflow.ir.import_dag` converts supported
native DAG structure into Workflow IR; `workflow.ir.export_dag` performs the
reverse conversion; and `workflow.ir.validate` returns the normalized document
and stable SHA-256 content hash. All three report `executes: false`.

W4-02 begins product convergence with `dag.workflow.inspect`. Unlike the raw
array converter, this read-only facade starts from a persisted DAG ID or name
and preserves the record identity, a stable hash of the DAG plus initial state,
the stored content-hash status, and every currently registered `dag.*`
capability alias. The DAG Workshop shows this evidence beside the saved
definition. It also lists conversion gaps and states plainly that plain,
supervised, monitored, streamed, and stepwise execution remain native. This
slice neither edits the saved record nor claims execution parity; those modes
move only after their separate Run/control/recovery gates pass.

Supported unsupervised `dag.run` definitions now pass through an exact Workflow
IR boundary. Vera imports the submitted graph, normalizes it, exports it, and
requires canonical round-trip equality before the Workflow IR definition becomes
authoritative. The materialized graph still runs through the established native
node executor, preserving capability invocation, conditions, parallel state
merge, errors, and Run observation. The Workflow IR content hash becomes the
Run `workflow_id`; callers may request the corresponding provenance metadata
without changing the legacy response shape. Callable conditions, malformed
nodes, or any non-exact definition stay on an explicitly labelled
`native_compatibility` path; no lossy execution is permitted. Supervised,
monitored, streamed, and stepwise modes remain native and separately gated.

The initial portable core covers sequential capability tasks, flat parallel
groups, output state keys, and `CONDITION:<state-key>` guards. Native input/output
maps round-trip under namespaced extensions but are reported as non-blocking gaps
because the core runner stores rather than interprets them. Callable conditions,
unknown fields, unknown extensions, malformed nodes, and nested non-task parallel
branches are rejected or returned as blocking gaps. A caller must explicitly set
`allow_lossy=true` to receive a partial conversion; doing so still cannot execute
the result.

This strict boundary is what makes later LangGraph, Temporal, ONNX workflow, and
other runtime adapters honest: unsupported semantics are visible before any
engine is selected. Retry, timeout, effects, schedules, loops/maps/reducers,
compensation, HITL, and subworkflows remain subsequent W1-02 slices rather than
being inferred from Vera's compact DAG arrays.

The second held slice adds explicit JSON-schema port descriptors and typed value
references for state, secrets, artifacts, records, and literals. References are
validated and hashed as opaque descriptions; the adapter never resolves a
secret, fetches an artifact, or reads a record. Task contracts can also declare
retry/backoff ownership, timeout ownership, idempotency keys, and effects across
filesystem, network, database, process, model, device, notification, and external
services. Native DAG export reports all of these as blocking gaps because the
compact array cannot preserve or enforce them. Explicit `allow_lossy` is the only
way to obtain an array with those contracts removed.

The third held slice represents subworkflow references, conditional choices,
bounded maps, and reducers as strictly validated structural nodes. Nested step
IDs remain globally unique within the document, subworkflows use opaque artifact
or record references, and all values must be canonical JSON. The native DAG
adapter reports each structural node as `unsupported_structure`; it never
flattens a branch, guesses collection semantics, resolves a child workflow, or
executes a reducer. With explicit lossy export, unsupported nodes are omitted and
the returned gap report remains attached.

The fourth held slice adds workflow-level schedules, resource envelopes, and
opaque provider requirements, plus task-level HITL approval and compensation
contracts. Schedule ownership is explicit (`runtime` or `external`), resource
numbers must be finite and positive, approval declarations cannot masquerade as
optional, and compensation triggers are limited to failure, cancellation, and
timeout. These records are descriptive only: Vera does not schedule work,
consume an approval, reserve a provider, or invoke rollback through Workflow IR.
Native DAG export reports every operational contract as a blocking gap.

The fifth held slice adds explicit current-version migration and declarative
adapter profiles. `workflow.ir.migrate` normalizes IR 1.0 without changing its
hash semantics and refuses unknown source or target versions. It never invents a
migration. `workflow.ir.adapters` distinguishes schema availability from
execution availability, while `workflow.ir.gaps` analyzes compatibility without
loading a runtime. The LangGraph and Temporal names are reserved profiles marked
unavailable with no claimed feature support; only a separately implemented and
tested adapter may change those declarations.

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
