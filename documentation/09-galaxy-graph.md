# 09 · Galaxy Graph

`vera_graph.js` is the reusable graph visualisation component that powers every node-edge view in Vera: the memory graph, the data fabric topology, the entity graph, the Loom cross-dataset stitch view, the network graph, and the Galaxy panel itself. It's a single web-component-style implementation, exposed on the global as `window.veraUI.Graph`, that handles fetch, layout, render, interaction, and a server-driven action registry — all in one file.

The Galaxy panel is the full-screen instance of this component, showing the entire knowledge graph across sessions and datasets. Sub-panels (memory graph, fabric panel, etc.) instantiate the same component with different sources.

---

## 1. Instantiation

A host panel mounts the component by calling:

```javascript
window.veraUI.Graph.create(containerElement, {
  source:       'fabric',            // 'fabric' | 'memory' | 'entity' | 'aux' | 'net'
  layers:       ['fabric','entity'], // available source pickers
  initialQuery: { dataset_id: 'research.results' },

  // UI options
  showLeftPanel: true,
  showSearch:    true,
  showRelayout:  true,
  showFit:       true,

  // Action options
  actionsEnabled: true,              // enable the server-driven action registry
  excludeSections: [],               // drawer sections to hide

  // Event hooks
  eventBus: hostBusSubscribe,        // optional: host-provided event bus
  onAction: (actionId, node, inst) => false,   // optional: local override
  onNodeClick: (node) => {...},
  onEdgeClick: (edge) => {...},
});
```

The component injects its own CSS (deduped by ID), builds a three-pane layout (left controls / canvas / right detail drawer), and connects to backend endpoints.

---

## 2. Sources

The `source` option controls which backend endpoint feeds the graph:

| Source | Endpoint | Returns |
|---|---|---|
| `fabric` | `GET /fabric/graphs/snapshot?graph=fabric&dataset_id=...` | Datasets, sources, records |
| `entity` | `GET /fabric/entity_graph/snapshot?dataset_id=...&include_datasets=1` | Entities + their mentions |
| `aux` | `GET /fabric/aux_graph/snapshot` | Dataset relationships, lineage |
| `memory` | `GET /memory/session/{nodes,edges}` | Memory records + session chains |
| `net` | `GET /network/topology` | Live network: hosts, instances, workers |

The source picker in the left panel switches between configured layers without reloading the component.

---

## 3. Layouts

Five layout modes are supported, switchable from the left panel:

### Default (force-directed)

Naïve force-directed simulation with:

- **Repulsion** between all visible node pairs (O(n²) up to ~225 nodes, then spatial-grid binning kicks in for O(n))
- **Spring force** along every edge (per-edge spring constants from `edgeStyleFn`)
- **Gravity** towards the centre (configurable)
- **Damping** that ramps up over time (frozen after ~280 ticks unless interacted with)

Tunable from the left panel: spread, gravity, repulsion strength.

### Force+Axis

Force-directed with axis attractors. Two axis selectors:

- **X axis**: time / importance / source / category
- **Y axis**: type / importance / session

Each node is pulled towards `_axVal(node, axis) * spread` while still respecting repulsion. Used when you want temporal or categorical structure without sacrificing visual untangling.

### Timeline

Pure static layout. X = time (with `px/hr` zoom control), Y = lane (one lane per node type, configurable lane height). No physics. Used for tracing the chronological flow of a session.

### Hierarchy

Tree layout. Pick a root type (session / Dataset / message / dag), and the component computes parent-child levels and lays out as a top-down tree. Configurable level gap and node gap. Used for clear parent-of relationships.

### Radial

Concentric rings. Configurable radius. Nodes arrange around the selected (or session) centre node. Used for showing what's directly connected to a focal node.

The mode is switched via the View Mode chips, with per-layout controls revealed below.

---

## 4. Filtering

Two chip strips in the left panel:

- **Node Types** — shows every distinct type in the loaded graph, with a count. Clicking a chip toggles visibility.
- **Edge Types** — shows every relation type. Toggling hides those edges (and breaks visual clutter).

A third strip (Source Types) appears for sources that distinguish them (memory mode does, fabric mode doesn't).

User preferences (which chips are off) persist across reloads via `_userOffTypes`, `_userOffEdges`, `_userOffSources` sets, so deselecting "Hub" edges once keeps them off until explicitly re-enabled.

### Search

The top toolbar has a search input. As you type, nodes whose label matches are highlighted, and the camera centres on them.

---

## 5. The detail drawer

Clicking a node opens the right drawer with:

- **Header**: node label, type chip, close button
- **Built-in actions strip**: pin/unpin, focus, hide, expand neighbours
- **Server actions section**: dynamically loaded from `GET /fabric/graph/node_actions?node_label=...&node_id=...`
- **Properties**: every property on the node, rendered as key→value
- **Edges**: the node's incoming and outgoing edges, clickable to traverse
- **Expand**: three buttons — Context (semantic neighbours), Traverse (graph walk), Edges (expand current edges into visible nodes)
- **Cap runner**: ad-hoc capability invocation against the node (e.g. "run fabric.entity_graph.extract on this Dataset")

The drawer can be closed by clicking the × or anywhere outside the node.

---

## 6. The server action registry

Every node has a set of context-aware actions defined server-side. When the drawer opens for a node:

1. The component calls `GET /fabric/graph/node_actions?node_label=Dataset&node_id=research.results`.
2. The server returns a list of actions applicable to this node's label:
   ```json
   [
     {
       "id":         "extract_entities",
       "label":      "Extract entities",
       "icon":       "⬡",
       "capability": "fabric.entity_graph.extract",
       "options":    [
         {"key": "min_mention_count", "type": "integer", "default": 2, "label": "Min mentions"},
         {"key": "purge_first",       "type": "boolean", "default": false, "label": "Purge existing"}
       ],
       "context":    "Walk this dataset's records, extract named entities, write to entity graph",
       "progress_event": "fabric.entity_graph.progress"
     },
     ...
   ]
   ```
3. Each action renders with its options as inline form controls.
4. Hitting Run posts to `POST /fabric/graph/run_node_action` with `{node_label, node_id, action_id, options}`.
5. A live output strip opens (collapsible) and subscribes to the action's declared `progress_event` via the shared event bus.
6. As entities/records/edges are emitted, they're added to the graph live.
7. On completion, the graph re-fetches its current snapshot so any persisted side-effects show up.

The component ships a `_LOCAL_FALLBACK` action map used when the server registry is unreachable, with common actions for `Dataset`, `Source`, `Entity`, and `FabricRecord`.

The host can override with `onAction: (action_id, node, inst) => false` to suppress the default server roundtrip and handle the action locally.

---

## 7. Live updates

The component subscribes to the orchestrator's event stream and reacts to relevant event types. Three subscription paths, tried in order:

1. **`opts.eventBus`** — function passed by the host that takes `(typePrefix, cb)` and returns an unsubscribe function. Used when the host already maintains a WS or SSE bridge.
2. **Parent harness** — `vera_fabric_event` postMessages from `window.parent` (set up by `vera_panel_bridge.js`).
3. **Direct WS** — opens its own `/ws/mcp` connection and sends `{action: "subscribe_events"}`.

Events that drive updates:

- `fabric.ingested` — refetch dataset count
- `fabric.entity_graph.progress` — add new entity nodes / edges to the graph
- `fabric.web.crawl.page` — add a page node to crawl visualisations
- `memory.record.created` — append to the memory graph
- `cap.call` / `cap.ok` — animate the relevant cap node (cluster topology view)

---

## 8. Theme integration

The component reads CSS variables from the parent document so it stays in step with the active theme:

```javascript
function themeColor(v, fallback) {
  const s = getComputedStyle(window.parent.document.documentElement).getPropertyValue(v).trim();
  return s || fallback;
}
```

Standard variables used:

| Variable | Use |
|---|---|
| `--bg0` / `--bg1` / `--bg2` | Backgrounds |
| `--acc` / `--acc2` / `--acc3` / `--acc4` / `--acc5` | Node type colours |
| `--text` / `--dim` / `--dim2` | Text colours |
| `--border` | Borders |
| `--ok` / `--err` / `--warn` | Status indicators |
| `--mono` | Monospace font |

Theme changes are propagated via the `vera:theme` postMessage; the component reapplies on receive.

---

## 9. Node rendering

Each node is drawn as:

- A circle (radius scaled by `n.r`, derived from `importance` or other metric)
- A type-specific colour (from a fixed palette per `n.type`)
- An outline (heavier when hovered, selected, or highlighted by search)
- A text label below (clipped to ~24 chars)
- A small type indicator below the label (zoom-gated)
- An "expanded" dot in the corner if the node has hidden related nodes

The render loop uses HTML5 Canvas (not SVG), so it scales smoothly to thousands of nodes. The canvas is auto-sized to its container with `ResizeObserver`.

---

## 10. Edge classification

Edges fall into three categories:

- **Structural** — define the primary topology (e.g. `CONTAINS`, `MENTIONED_IN`). Strong spring force; influence layout.
- **Inferred** — semantic relationships (`SIMILAR_TO`, `CO_OCCURS`). Weak spring; visible but don't distort.
- **Hub** — connect everything to a hub node (the session in memory mode). Hidden by default — toggling them on creates the "everything points to one node" view useful for session inspection.

Edge styling is per-source: the memory graph uses Bezier curves; the fabric uses straight lines; entity graph uses dashed for inferred and solid for direct.

---

## 11. Layered host configuration

A host panel with multiple sources (e.g. the Galaxy panel) passes `layers: ['fabric','memory','entity','net']` to expose a source picker. Switching sources:

1. Clears the current graph state.
2. Calls the new source's fetch path.
3. Rebuilds chips, layout controls, and the action registry.

The component maintains independent layout state per source so flipping back to a previous layer preserves the prior layout.

---

## 12. The Galaxy panel

`memory_galaxy_panel.html` (mounted at `/galaxy/panel`, registered with `tab_order=58`) is the dedicated full-screen instance. It opens with `source='memory'`, `layers=['memory','fabric','entity','net','aux']`, and `showLeftPanel=true`. The Galaxy panel is the canonical place to visualise the entire system — all sessions, all datasets, all activity, in one navigable canvas.

It's the most computationally expensive view (the graph can grow to thousands of nodes for long-running deployments) and reuses the same physics-budget grid binning that keeps the other views responsive.

---

## 13. Performance budgets

Two soft caps keep the physics loop responsive:

- `MG_REPEL_BUDGET_PAIRS = 25000` — max pair-wise repulsion comparisons per frame (√50000 ≈ 225 nodes for full O(n²); above that, spatial grid binning takes over)
- `MG_GRID_CELL = 320` (px) — spatial grid cell size, slightly larger than `repelDist`

Above ~225 visible nodes, repulsion is restricted to the 3×3 spatial neighbourhood of each node — O(n) per frame instead of O(n²). All visible nodes still get axis attraction and integration on every frame (always cheap, O(n)).

Static views (timeline, hierarchy, radial) skip physics entirely.

---

## 14. Integration patterns

A panel that just wants to drop in a graph:

```javascript
const container = document.getElementById('my-graph');
const graph = window.veraUI.Graph.create(container, {
  source: 'fabric',
  initialQuery: { dataset_id: 'web.crawl.example_com' },
  actionsEnabled: true,
});
```

A panel that wants to override an action locally:

```javascript
const graph = window.veraUI.Graph.create(container, {
  source: 'fabric',
  onAction: (id, node, inst) => {
    if (id === 'browse') {
      // Open in my own UI instead of the default
      myPanel.openDataset(node.id);
      return false;   // suppress default server call
    }
    return true;      // let default fire
  },
});
```

A panel that wants to feed events from its own WebSocket:

```javascript
function busSubscribe(typePrefix, cb) {
  myWs.on('message', (ev) => {
    if (ev.type.startsWith(typePrefix)) cb(ev);
  });
  return () => { /* unsubscribe */ };
}
const graph = window.veraUI.Graph.create(container, {
  source: 'memory',
  eventBus: busSubscribe,
});
```

---

## See also

- [Memory Graph](./05-memory-graph.md) — the data behind the memory source
- [Data Fabric](./06-data-fabric.md) — the data behind the fabric / entity / aux sources
- [Harness UI](./02-harness-ui.md) — how the Galaxy tab is registered