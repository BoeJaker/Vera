# 20 · Flow Builder & UI Elements

The `vera/elements/` module is Vera's library of **reusable UI custom
elements**: self-contained web components served as standalone JS includes and
registered into the harness, so any panel can drop them in without duplicating
code. Its flagship is `<vera-flow-builder>`, a domain-agnostic visual
flow/DAG builder with a loss-aware Workflow IR bridge. This page also covers the
**n8n integration** (`vera/n8n/`), the other way Vera composes workflows
visually: n8n workflows become Vera capabilities, and Vera DAGs export to n8n.

This is the front-end counterpart to the single-decorator backend philosophy:
build a rich component once, register it, reuse it everywhere.

**Status:** `<vera-flow-builder>` is stable and used by the Dream, Fabric,
Research, Markets and Chat panels. The Workflow IR bridge is descriptive and
non-executing. The n8n integration is functional; it needs a reachable n8n
instance and API key, and is reached through the Automations hub.

## Contents

- [1. Source map](#1-source-map)
- [2. The reusable-element pattern](#2-the-reusable-element-pattern)
- [3. `<vera-flow-builder>` — domain-agnostic by design](#3-vera-flow-builder--domain-agnostic-by-design)
  - [3.1 Generic graph model](#31-generic-graph-model)
  - [3.2 Provider interface](#32-provider-interface)
  - [3.3 Public API and events](#33-public-api-and-events)
  - [3.4 Capabilities palette — `VeraFlowCaps`](#34-capabilities-palette--veraflowcaps)
  - [3.5 Running a graph as a DAG](#35-running-a-graph-as-a-dag)
- [4. Who uses it](#4-who-uses-it)
- [5. Workflow IR compatibility](#5-workflow-ir-compatibility)
- [6. Graph contract and execution handoff](#6-graph-contract-and-execution-handoff)
- [7. Other reusable elements](#7-other-reusable-elements)
- [8. n8n integration](#8-n8n-integration)
  - [8.1 Interop model](#81-interop-model)
  - [8.2 Capabilities](#82-capabilities)
  - [8.3 Configuration and storage](#83-configuration-and-storage)
  - [8.4 Worked example](#84-worked-example)
- [9. Troubleshooting](#9-troubleshooting)
- [See also](#see-also)

---

## 1. Source map

| Path | Responsibility |
|---|---|
| `vera/elements/flow_builder_element.js` | `<vera-flow-builder>` custom element (palette, SVG canvas, inspector, compatibility badge, IR import/export, `toDag`/`runAsDag`). |
| `vera/elements/flow_caps.js` | `VeraFlowCaps` — shared capability palette helper. |
| `vera/elements/flow_builder_capabilities.py` | Serves the JS, registers `workflow.flow_builder.*` caps and the `flow-builder` inject panel. |
| `vera/elements/flow_builder_workflow.py` | Pure, non-executing Flow Builder ↔ Workflow IR conversion and semantic diff. |
| `vera/elements/general_widgets_capabilities.py`, `chat_data_element.js` | `<vera-chat-data>` "Chat with Data" widget. |
| `vera/n8n/n8n_capabilities.py` | n8n REST and MCP integration caps, panel. |
| `vera/n8n/n8n_core.py` | Pure URL derivation and DAG ↔ workflow translation. |
| `vera/n8n/n8n_panel.html` | n8n panel (served at `/n8n/panel`). |

---

## 2. The reusable-element pattern

Each element follows the same shape (see [`flow_builder_capabilities.py`](../vera/elements/flow_builder_capabilities.py)):

1. **Serve the JS** at a stable route via a `@capability` (and a plain `@APP.get` mirror), reading the source from disk on each request with `Cache-Control: no-cache`, so iterating on the JS needs no restart:

   ```python
   @capability("ui.elements.flow_builder_js",
       http_method="GET", http_path="/ui/elements/flow_builder.js",
       memory="off", silent=True)
   async def serve_flow_builder_js(trace_id=None): ...
   ```

2. **Register it as an injectable panel** with `register_ui(..., mode="inject")` so it appears in the panels/widget picker.

A host panel then includes `<script src="/ui/elements/flow_builder.js">` once and drops the tag wherever it needs the component.

| Cap | Route | Element |
|---|---|---|
| `ui.elements.flow_builder_js` | `GET /ui/elements/flow_builder.js` | `<vera-flow-builder>` |
| `ui.elements.flow_caps_js` | `GET /ui/elements/flow_caps.js` | `VeraFlowCaps` palette helper |
| `ui.elements.chat_data_js` | `GET /ui/elements/chat_data.js` | `<vera-chat-data>` |

```python
register_ui(
    panel_id="flow-builder", label="Flow Builder", icon="⊸",
    mode="inject", tab_order=206,
    html=_INJECT_HTML,                      # <script src> + <vera-flow-builder>
    ui_caps=["ui.elements.flow_builder_js"],
)
```

The bare inject is mostly a discovery and registration hook. The element needs a provider to do anything, so hosts normally embed the `<script src>` and tag directly and call `el.setProvider(…)`.

---

## 3. `<vera-flow-builder>` — domain-agnostic by design

The element provides a searchable, draggable palette, an auto-laid-out SVG canvas with bezier edges and inferred data wires, and a schema-driven inspector. Its layout constants match the DAG Workshop builder. It knows nothing about DAGs, dream pipelines or fabric queries on its own: each host supplies a small **provider** object that adapts it to a domain.

```html
<script src="/ui/elements/flow_builder.js"></script>
<vera-flow-builder id="fb"></vera-flow-builder>
<script>
  const el = document.getElementById('fb');
  el.setProvider(myProvider);          // or <vera-flow-builder provider="name">
  el.loadFromSource(existingDoc);      // hydrate from the host's document
  el.addEventListener('flow:change', e => save(el.serialize()));
</script>
```

Providers can be registered globally with `window.registerFlowProvider(name, obj)` (stored on `window.VeraFlowProviders`) and selected with the `provider="…"` attribute.

### 3.1 Generic graph model

```text
node  = {id, type, out, params:{name:{source:'value'|'state', value}},
         output_map, condition, parallel_with, pos:{x,y}|null, meta:{}}
graph = {nodes:[], meta:{}}
```

The node schema mirrors Vera capability descriptors: `{name, description, long_running, params:[{name,type,required,enum,default,description,properties,items}], outputs, output_keys, streams}`.

### 3.2 Provider interface

`*` = required.

| Member | Purpose |
|---|---|
| `id` | Short provider name. |
| `loadPalette()` * | `→ [{group, items:[{type, label, description, schema, long_running, badges}]}]`. |
| `schemaFor(typeOrNode)` | Schema lookup (else `item.schema`). |
| `createNode(item, ctx)` | Custom node creation (else built from the schema). |
| `validateNode(node, schema)` | `→ [error strings]` (else a required-param check). |
| `renderInspector(node, ctx)` | Custom inspector HTML or `{html, bind(rootEl)}`. |
| `globalSection(graph, ctx)` | Inspector content when nothing is selected (e.g. DAG initial state). |
| `serialize(graph)` * | Model → host document. |
| `deserialize(doc)` * | Host document → model. |
| `nodeCard(node, schema)` | `{title, sub, line}` override for node cards. |
| `onChange(graph)` | Called after any edit. |
| `insertIndex(node, graph)` | Where a new node is spliced (keeps sources → logic → sinks order). |
| `stateKeysBefore(idx, graph)` | Keys offered by from-state pickers. |
| `sequenceEdges: false` | Suppress implicit row-to-row edges for pure dataflow domains. |
| `toWorkflowIR(graph)` / `fromWorkflowIR(workflow)` | Optional overrides of the shared conversion (must declare `executes:false`). |

Hooks receive `ctx = {el, graph, schema, esc, update(), select(id)}`.

### 3.3 Public API and events

| Method | Purpose |
|---|---|
| `setProvider(obj)` | Set the domain adapter (loads the palette). |
| `getGraph()` / `setGraph(graph)` | Read or replace the model. |
| `serialize()` / `loadFromSource(doc)` | Provider round-trip. |
| `getCompatibility()` / `requireCompatible()` | Workflow IR evidence; fail closed before execution. |
| `exportWorkflowIR()` / `previewWorkflowIR(ir)` / `applyWorkflowIR(preview)` | IR export and two-stage import. |
| `toDag()` / `runAsDag(opts)` | Native DAG export and run. |
| `reset()`, `refreshPalette()`, `getSelected()`, `autoLayout()` | Utilities. |

| Event (bubbling `CustomEvent`) | Detail |
|---|---|
| `flow:change` | `{graph}` |
| `flow:select` | `{node\|null}` |
| `flow:node-add` / `flow:node-remove` | `{node}` / `{id}` |
| `flow:compatibility` | Compatibility report |
| `flow:workflow-export` | `{report}` |
| `flow:workflow-import-preview` | `{report}` (canvas unchanged) |
| `flow:workflow-import` | `{content_hash, classification}` |
| `flow:run` | `{phase: start\|event\|error\|done, …}` |

### 3.4 Capabilities palette — `VeraFlowCaps`

`VeraFlowCaps` (`/ui/elements/flow_caps.js`) loads the capability registry once from `GET /workshop/cap_tree` and exposes it as palette groups and per-cap schemas, so any provider can let users drop **capabilities** onto the canvas:

```js
const caps = await window.VeraFlowCaps.paletteGroups();   // [{group, items}]
return [...domainGroups, ...caps];
// in schemaFor: return window.VeraFlowCaps.schemaFor(type) || …
```

| API | Purpose |
|---|---|
| `setBase(url)` | Orchestrator base (default same-origin). |
| `paletteGroups(force)` | Palette groups (cached; `force` reloads). |
| `schemaFor(type)` / `isCap(type)` | Cached descriptor / known-cap test. |
| `ioSchema(name)` | Lazy `GET /workshop/cap_io_schema` (enums, output keys). |

### 3.5 Running a graph as a DAG

`toDag()` converts the graph into native DAG tuples `[cap, out, cond?, input_map?, output_map?]`, grouping `parallel_with` peers into parallel lists. A literal param becomes initial state, coerced to its declared type. `runAsDag({base, state, into})` first calls `requireCompatible()`; missing analysis or blocking gaps stop the run locally and are shown in the log. It then streams `POST /workshop/dag/run_stream`, logging `node_start`, `node_done`, `node_error` and `done`, and dispatching `flow:run` events.

---

## 4. Who uses it

Because it is provider-driven, one implementation serves many panels:

| Host | Provider | Saves as |
|---|---|---|
| **Dream** (`dream_panel.html`) | `dreamTriggerProvider` — sensors, stages and caps | A dream trigger (`flow_graph` + sensors/pipeline). See [Dream](./17-dream.md). |
| **Fabric** (`fabric_panel.html`) | `fabricProvider` | Fabric query pipelines. See [Data Fabric](./06-data-fabric.md). |
| **Research** (`research_panel.html`) | `researchProvider` | Research pipelines. See [Research](./07-research.md). |
| **Markets** (`markets_panel.html`, `markets_studio_panel.html`) | `PIPE_PROVIDER` | Market pipelines and notebooks. See [Markets](./15-markets.md). |
| **Chat** command builder (`chat_panel.html`) | DAG (caps) or Dream (sensors · stages · caps) | **Run as DAG** or **Save as dream**. |

> [!NOTE]
> The DAG Workshop has its own builder, from which this element was lifted; it does not embed `<vera-flow-builder>`.

---

## 5. Workflow IR compatibility

Flow Builder graphs can be inspected and converted through a non-executing,
loss-aware Workflow IR bridge (`flow_builder_workflow.py`). The portable subset
covers stable node IDs, task/capability types, outputs, literal parameters,
state references, and basic name/description metadata. Canvas coordinates,
conditions, host-specific node fields and other provider metadata are retained
exactly under the namespaced `vera.flow_builder.graph` extension and reported
as native extensions rather than silently presented as portable semantics.

| Cap | Purpose |
|---|---|
| `workflow.flow_builder.analyze` | Classification, gaps and opaque semantic differences, without returning the graph payload. |
| `workflow.flow_builder.to_ir` | Validated, content-addressed Workflow IR, preserving the exact source graph. |
| `workflow.flow_builder.from_ir` | Reconstructs an exact preserved graph, or refuses IR structures and contracts the generic canvas cannot represent. |
| `workflow.flow_builder.diff` | Changed paths and hashes (`added`, `removed`, `type_changed`, `value_changed`), not document values. |

Classifications include `portable`, `invalid`, `unsupported` and `inconsistent`. Gap codes include `native_extension` (non-blocking), `invalid_graph`, `invalid_binding`, `invalid_state_reference`, `unsupported_binding_source`, `unsupported_contract`, `unsupported_structure` and `extension_ir_mismatch`.

The preserved graph and visible IR steps are checked against each other. If
one side changes independently, import fails with an `extension_ir_mismatch`
gap, and the UI or caller must resolve the discrepancy before execution. Every
response declares `executes: false`, and existing providers and native DAG
execution remain authoritative.

The builder displays this evidence beside the canvas node count:

| Badge | Meaning |
|---|---|
| **Portable** (green) | The graph fits the shared subset. |
| **Native details** (amber) | Provider-specific fields are retained exactly but will not be understood by every runtime. |
| **Blocked** (red) | Conversion is invalid, inconsistent or unavailable. |

Selecting the badge opens paths and explanations without exposing document
values. The status refreshes after graph edits (debounced), and a
`flow:compatibility` event lets host panels present the same result.

Generic **Run as DAG** obtains fresh compatibility evidence before building or
submitting the native DAG request. Preserved native details remain visible but
do not block the current native engine. Host-specific save formats remain under
their providers, because saving a Dream trigger or Fabric pipeline is not
equivalent to claiming portable execution.

The canvas also provides explicit **Export IR** and **Import IR** controls.
Export returns the validated, content-addressed document from the shared
conversion capability and identifies any native details retained in its Vera
extension. Import is deliberately two-stage: pasted JSON is previewed through
the shared reader without changing the canvas, then **Apply preview** is
enabled only for the exact response that was validated. Invalid documents,
blocking gaps, semantic differences, stale or edited previews and unavailable
analysis all leave the existing graph untouched. Import evidence lists affected
paths and change kinds without echoing document values. Successful imports
preserve stable node IDs.

---

## 6. Graph contract and execution handoff

Flow Builder is an authoring surface, not a second workflow runtime. Nodes hold
capability references and input bindings; edges express data and control flow;
the saved graph is serialised by its provider or handed to the DAG subsystem
for execution. This keeps retry, tracing, supervision and worker dispatch
consistent with programmatically authored DAGs.

Before saving, validate that every capability exists, required inputs are
bound, and no illegal cycle has been introduced. Before running, inspect the
compiled preview rather than assuming the canvas layout equals execution order.
When a flow fails, use the DAG run record and the failing node's output; UI
coordinates and edge rendering are not execution evidence.

Exported flows should be reviewed as data, because they can reference mutating
capabilities.

---

## 7. Other reusable elements

Other elements across the codebase follow the identical pattern:

| Element | Served at | Page |
|---|---|---|
| `<vera-chat-data>` ("Chat with Data", panel `chat-with-data`) | `/ui/elements/chat_data.js` | This page. Talks to `POST /agents/chat` (default agent `assistant`); events `vcd:sent`, `vcd:reply`. |
| `<vera-agent-loop-output>` | `/ui/elements/agent_loop_output.js` | [Agents & Chat](./19-agents-chat.md) |
| `<vera-mermaid>` | `/ui/elements/vera_mermaid.js` | [Render](./28-render.md) |
| `<vera-sandbox-controls>` | `/ui/elements/sandbox_controls.js` | [Execution](./12-execution.md) |
| `<vera-character>` | `/ui/elements/character.js` | [Media & Characters](./38-media-characters.md) |
| Loop graph, loop throbber, sparkline, topology map, agent-loop config, markdown, widget | `/ui/elements/*.js`, `/ui/widgets/widget_element.js` | [UI Builder](./26-ui-builder.md) |
| Workers live event stream and system log | — | [Workers, Jobs & Syslog](./22-workers-jobs-syslog.md) |

---

## 8. n8n integration

### 8.1 Interop model

`vera/n8n/` wires a self-hosted n8n instance into Vera in both directions. The model is deliberately **asymmetric**:

| Direction | How | Why |
|---|---|---|
| **Vera → n8n** | `n8n.workflow.*` drives n8n's public REST API (`/api/v1`) to list, author, activate and inspect workflows. | |
| **n8n → Vera** | `n8n.mcp.connect` opens a real MCP session to an n8n **MCP Server Trigger** and registers every exposed tool as a live capability `n8n.<tool>`. | DAG nodes are just capability names, so those workflows become DAG nodes immediately — no format translation. |
| **Vera DAG → n8n workflow** | `n8n.dag.export` builds a workflow whose HTTP Request nodes call back into Vera's `POST /mcp/call`. | Faithful for the subset a DAG actually is (ordered calls plus parallel groups). Anything not representable exactly (callable conditions, parallel re-join) is reported in `notes`. |
| **n8n workflow → Vera DAG** | **Not** a node-by-node translation. A workflow is surfaced as **one** capability. | n8n has hundreds of node types with no Vera equivalent; translating them node by node would drop or invent logic. |

URL conventions (`n8n_core.py`): MCP trigger at `<base>/mcp/<path>` (editor test URL `<base>/mcp-test/<path>`); webhooks at `<base>/webhook/<path>` (`<base>/webhook-test/<path>`).

### 8.2 Capabilities

| Cap | Route | Purpose |
|---|---|---|
| `n8n.config.get` | `GET /n8n/config` | Config with secrets redacted, plus derived MCP URLs and `configured` / `api_configured` / `mcp_configured` flags. |
| `n8n.config.set` | `POST /n8n/config` | `base_url`, `api_key`, `mcp_path`, `mcp_token`, `verify_tls`, `auto_connect`. Secrets are sealed at rest; an empty string keeps the stored secret. |
| `n8n.workflow.list` | `GET /n8n/workflows` | Workflows with their triggers and live webhook/MCP URLs (`active_only`, `limit`). |
| `n8n.workflow.get` | `GET /n8n/workflow` | One workflow (`full` for the node graph). |
| `n8n.workflow.create` | `POST /n8n/workflow/create` | Create from a node graph (`name`, `nodes`, `connections`, `settings`, `activate`). Emits `n8n.workflow.created`. |
| `n8n.workflow.activate` | `POST /n8n/workflow/activate` | Activate/deactivate (an inactive workflow's production URL returns 404). |
| `n8n.workflow.delete` | `POST /n8n/workflow/delete` | Delete. |
| `n8n.execution.list` | `GET /n8n/executions` | Recent executions (`workflow_id`, `status` success/error/waiting, `limit` 20). |
| `n8n.webhook.call` | `POST /n8n/webhook/call` | Call a webhook by path (production or test URL). |
| `n8n.mcp.probe` | `POST /n8n/mcp/probe` | Open a real MCP session and list tools — a "does it work" check. |
| `n8n.mcp.connect` | `POST /n8n/mcp/connect` | Register every MCP tool as `n8n.<tool>` (`prefix`, `test`). Emits `n8n.mcp.connected`. |
| `n8n.mcp.disconnect` | `POST /n8n/mcp/disconnect` | Remove the registered caps. |
| `n8n.dag.export` | `POST /n8n/dag/export` | Vera DAG → n8n workflow (`dag`, `name`, `vera_url`, `create`, `activate`) → `{workflow, notes, node_count, id?}`. |
| `n8n.dag.node` | `POST /n8n/dag/node` | The literal `[cap, out_key]` node for a workflow, whether it is usable (needs an inbound MCP or webhook trigger), and how to register it. |
| `n8n.panel.html` | `GET /n8n/panel` | The n8n panel. |

The panel is registered as `n8n-panel` with `mode="element"`: it is reached through the **Automations** hub (which embeds `/n8n/panel` as a sub-tab), not as a top-level tab.

### 8.3 Configuration and storage

| Key | Holds |
|---|---|
| Redis hash `vera:n8n:config` | `base_url`, `api_key` (sealed), `mcp_path`, `mcp_token` (sealed), `verify_tls`, `auto_connect`, `updated`. |
| Redis `vera:n8n:registered_tools` | Names of caps registered by `n8n.mcp.connect`. |

Registered MCP tools live in the process registry. With `auto_connect` on, a startup task re-runs `n8n.mcp.connect` five seconds after boot, so DAGs that reference `n8n.*` caps keep working after a restart. Calls use a 45 s timeout for REST, 120 s for webhooks, and the per-connection timeout (default 120 s) for MCP tools.

### 8.4 Worked example

```json
{"name": "n8n.config.set", "arguments": {
  "base_url": "https://n8n.example.org", "api_key": "<api key>",
  "mcp_path": "vera-tools", "auto_connect": true}}
{"name": "n8n.mcp.connect", "arguments": {}}
```

Now use an n8n tool inside a Vera DAG:

```json
{"dag": [["n8n.enrich_lead", "lead"], ["llm.summarize", "summary"]],
 "initial_state": {"email": "someone@example.org"}}
```

Export a Vera DAG to n8n (without creating it yet):

```json
{"name": "n8n.dag.export", "arguments": {
  "name": "nightly-digest", "create": false,
  "dag": [["fabric.query", "rows"], ["llm.summarize", "digest"]]}}
```

---

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| Builder renders empty | No provider set, or `loadPalette()` failed; check the console. |
| No capabilities in the palette | `GET /workshop/cap_tree` failed or `flow_caps.js` was not loaded before the builder. |
| Badge shows **Blocked** | Invalid bindings or state references, unsupported structures, or an `extension_ir_mismatch`; open the badge for paths. |
| **Run as DAG** does nothing | Compatibility blocked, or no cap nodes in the graph. |
| IR import never applies | The preview was stale or edited after validation; re-preview. |
| `n8n.*` caps vanished after restart | `auto_connect` off; run `n8n.mcp.connect`. |
| n8n production URL 404 | The workflow is inactive; `n8n.workflow.activate`. |
| n8n TLS errors | Internal CA: set `verify_tls: false` (or install the CA). |

---

## See also

- [Harness UI](./02-harness-ui.md) — `register_ui()`, panel modes, the iframe pattern
- [DAG Engine](./03-dag-engine.md) — DAG syntax and `run_stream`; the DAG Workshop builder
- [Dream](./17-dream.md) — pipeline building with the same element
- [Agents & Chat](./19-agents-chat.md) — `<vera-agent-loop-output>`, a sibling reusable element
- [UI Builder](./26-ui-builder.md) — other `/ui/elements/*` routes
- [Interoperability Foundations](./46-interoperability-foundations.md) — Workflow IR
- [Integrations](./23-integrations.md) — external services

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
