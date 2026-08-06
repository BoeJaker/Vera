# 20 · Flow Builder & UI Elements

The `elements/` module is Vera's library of **reusable UI custom elements** — self-contained web components served as standalone JS includes and registered into the harness so any panel can drop them in without duplicating code. Its flagship is `<vera-flow-builder>`, the visual flow/DAG builder.

This is the front-end counterpart to the single-decorator backend philosophy: build a rich component once, register it, reuse it everywhere.

---

## 1. The reusable-element pattern

Each element follows the same shape (see [`flow_builder_capabilities.py`](../vera/elements/flow_builder_capabilities.py)):

1. **Serve the JS** at a stable route via a `@capability` (and a plain `@APP.get` mirror), reading the source from disk on each request so iterating on the JS needs no restart:

   ```python
   @capability("ui.elements.flow_builder_js",
       http_method="GET", http_path="/ui/elements/flow_builder.js",
       memory="off", silent=True)
   async def serve_flow_builder_js(trace_id=None): ...
   ```

2. **Register it as an injectable panel** with `register_ui(..., mode="inject")` so it appears in the panels/widget picker.

A host panel then does `<script src="/ui/elements/flow_builder.js">` once and drops the tag wherever it needs the component.

The module registers:

| Cap | Route | Element |
|---|---|---|
| `ui.elements.flow_builder_js` | `GET /ui/elements/flow_builder.js` | `<vera-flow-builder>` |
| `ui.elements.flow_caps_js` | `GET /ui/elements/flow_caps.js` | `VeraFlowCaps` palette helper |
| `ui.elements.chat_data_js` | `GET /ui/elements/chat_data.js` | `<vera-chat-data>` (from `general_widgets_capabilities.py`) |

Other elements across the codebase follow the identical pattern — `<vera-agent-loop-output>` ([Agents & Chat](./19-agents-chat.md)), `<vera-sandbox-controls>` ([Execution](./12-execution.md)), and the workers' live event-stream / system-log elements ([Workers, Jobs & Syslog](./22-workers-jobs-syslog.md)).

---

## 2. `<vera-flow-builder>` — domain-agnostic by design

The element is a DAG-Workshop-grade visual flow builder, but it is **domain-agnostic**. It knows nothing about DAGs, dream pipelines, or fabric queries on its own. Each host feeds it a small **provider** object that adapts it to that domain:

```js
el.setProvider({
  // palette source     — what nodes can be dropped on the canvas
  // node schema         — fields/ports each node type exposes
  // serialize(doc)      — flow graph → host's own document format
  // deserialize(doc)    — host document → flow graph
});
// or, declaratively, via a named provider in the registry:
//   <vera-flow-builder provider="dag"></vera-flow-builder>
```

Providers can be registered globally on `window.VeraFlowProviders` and selected by name with the `provider="…"` attribute. The full provider interface is documented in the JS file header (`flow_builder_element.js`).

`VeraFlowCaps` (served at `/ui/elements/flow_caps.js`) is a shared helper a provider can use to populate the palette from Vera's capability tree — it loads `/workshop/cap_tree` so any flow builder can drop **capabilities** onto the canvas.

---

## 3. Who uses it

Because it's provider-driven, one implementation serves many panels:

- **DAG Workshop** — build executable [DAGs](./03-dag-engine.md)
- **Dream pipelines** — assemble [dream](./17-dream.md) sensor→stage pipelines
- **Fabric query builder** — compose [data fabric](./06-data-fabric.md) queries visually
- **Research** — pipeline composition
- …and any future panel that needs a node-graph editor

---

## 4. Registration details

```python
register_ui(
    panel_id="flow-builder", label="Flow Builder", icon="🕸",
    mode="inject", tab_order=206,
    html=_INJECT_HTML,                      # <script src> + <vera-flow-builder>
    ui_caps=["ui.elements.flow_builder_js"],
)
```

The bare inject is mostly a discovery/registration hook — the element needs a provider to do anything, so hosts normally embed the `<script src>` + tag directly and call `el.setProvider(…)`.

---

## See also

- [Harness UI](./02-harness-ui.md) — `register_ui()`, panel modes, the iframe pattern
- [DAG Engine](./03-dag-engine.md) — the primary consumer (DAG Workshop)
- [Dream](./17-dream.md) — pipeline building with the same element
- [Agents & Chat](./19-agents-chat.md) — `<vera-agent-loop-output>`, a sibling reusable element

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
