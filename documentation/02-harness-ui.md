# 02 · Harness UI

The harness is a single-page application served at `GET /` by the orchestrator. It's the entry point to everything Vera does: capabilities, observability, the DAG workshop, the data fabric, the memory graph, the IDE, research, chat, and any module-provided panel.

It is intentionally a thin shell. Most of the surface area lives in **panels** — standalone HTML files served at their own routes and mounted into the harness via iframes. Panels are registered by modules using `register_ui()` and discovered by the harness through `GET /ui/panels`.

---

## 1. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│   Harness (capability_orchestration.html @ GET /)                     │
│                                                                       │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐ │
│   │   Tab Bar    │  │   Tab Bar    │  │   Tab Bar    │  │  Tab Bar   │ │
│   │  Dashboard   │  │   Caps       │  │  DAG Wkshp   │  │ +auto tabs │ │
│   └─────────────┘  └─────────────┘  └─────────────┘  └────────────┘ │
│                                                                       │
│   ┌─────────────────────────────────────────────────────────────────┐│
│   │              Active panel area (one shown at a time)              ││
│   │   ┌───────────────────────────────────────────────────────────┐  ││
│   │   │   Static panels (inline HTML)   or                          │  ││
│   │   │   iframe → /<panel_route>      (auto-mounted)               │  ││
│   │   └───────────────────────────────────────────────────────────┘  ││
│   └─────────────────────────────────────────────────────────────────┘│
│                                                                       │
│   ─── WebSocket /ws/mcp ───                                          │
│        - subscribes to events on connect                             │
│        - all panels can read live events via window.parent           │
└──────────────────────────────────────────────────────────────────────┘
```

The harness opens one WebSocket connection on load and subscribes to all events. Panels running in iframes can either:

- Read events from the parent harness via `window.parent` and the postMessage bridge in `vera_panel_bridge.js`, or
- Open their own WebSocket connections to `/ws/mcp`.

The shared bus pattern (`vera_graph.js` uses this) prefers the parent's connection when available and falls back to opening its own WS only if no parent bridge exists.

---

## 2. Panel registration

Modules register panels by calling `register_ui()` (imported from `capability_orchestration`):

```python
from Vera.Orchestration.capability_orchestration import register_ui

register_ui(
    panel_id   = "my-panel",
    label      = "My Panel",
    icon       = "⬡",
    html       = '<iframe src="/my_panel" style="width:100%;height:100%;border:none"></iframe>',
    js         = "/* inline JS run when the panel activates */",
    ui_caps    = ["my.cap.one", "my.cap.two"],
    mode       = "tab",     # "tab" | "inject" | "mount"
    tab_order  = 50,
)
```

This pushes an entry into `UI_PANELS`, which is exposed via `GET /ui/panels`. The harness fetches that endpoint on load.

### `mode="tab"`

Creates a top-level tab in the tab bar. The panel HTML is injected when the tab is first activated, and an inline JS snippet (if any) runs once after injection. Used for full-screen sub-applications: Chat, IDE, Research, Fabric, Memory Graph, Galaxy, NLP, Notebook, Cap Hub.

`tab_order` controls left-to-right ordering — lower numbers are further left. Standard ranges:

| Range | Used for |
|---|---|
| 0–20 | Always-visible core tabs (dashboard, caps) |
| 20–40 | Primary tools (chat, IDE) |
| 40–60 | Domain panels (fabric, research, memory, galaxy) |
| 60–100 | Secondary / utility (NLP, notebook, ontologies) |
| 100+ | Default for unspecified |

### `mode="inject"`

Adds the panel as a named sub-panel inside the harness's "Media" tab. The Media tab has its own sub-switcher; injected panels appear there. Used for small dashboards that don't justify a full tab.

### `mode="mount"`

For panels that need to be mounted into a specific pre-declared mount point in the harness markup (e.g. an admin sidebar). The harness's `DEDICATED_PANEL_MOUNTS` dict maps panel IDs to DOM element IDs. Currently most legacy mount-mode panels have been migrated to `mode="tab"` iframes.

---

## 3. Panel discovery

`GET /ui/panels` returns the list:

```json
[
  {
    "id":         "my-panel",
    "label":      "My Panel",
    "icon":       "⬡",
    "html":       "<iframe ...></iframe>",
    "js":         "...",
    "ui_caps":    ["my.cap.one"],
    "mode":       "tab",
    "tab_order":  50
  },
  ...
]
```

The harness's `loadAllPanels()`:

1. Fetches `/ui/panels` (with a cache so it's fetched once per session).
2. Sorts by `tab_order`.
3. For each entry, dispatches by `mode`:
   - `tab` → `_createAutoTab(p)` — creates the tab button and panel div
   - `inject` → adds a sub-panel into the Media sub-switcher
   - `mount` → injects into the matching dedicated mount point

Each panel's `html` field is run through `_rewritePanelUrls()` before injection. This rewrites relative `src` and `href` attributes (`/foo`) to absolute backend URLs (`http://backend:8999/foo`) — necessary because the harness may be served from a different origin than the backend (e.g. harness on `:8888`, backend on `:8999`).

---

## 4. Iframe pattern

Almost every modern panel is structured as:

```python
register_ui(
    panel_id  = "research-panel",
    label     = "Research",
    icon      = "",
    html      = '<iframe src="/research/panel" style="width:100%;height:100%;border:none"></iframe>',
    mode      = "tab",
    tab_order = 55,
)

@APP.get("/research/panel", include_in_schema=False)
async def _research_panel():
    p = _HERE / "research_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8"))
```

**Why iframes and not innerHTML injection?** Because `<script>` tags injected via `innerHTML` do not execute. Iframes give panels their own `window` and run scripts normally. The harness's URL rewriter handles the cross-origin asset issue.

**Why standalone HTML files and not inline Python strings?** Because they're enormously easier to edit and validate. Large files (>1400 lines) hit a silent-failure limit on inline file creation tools — keeping HTML in separate `.html` files sidesteps that and makes the panels editable in any editor.

---

## 5. Shared bridge utilities

### `vera_panel_bridge.js`

Posted into the harness; provides postMessage helpers for iframe ↔ parent communication. Used for:

- Theme synchronisation (parent broadcasts CSS variables; iframes apply them)
- Session ID sharing (`window.parent._veraSessionId`)
- Event forwarding (parent's WS events → iframe via `vera_fabric_event` messages)

### `vera_graph.js`

The reusable graph web component. Mounts as `window.veraUI.Graph` and is used wherever a panel needs to render a node-edge graph (memory, fabric, entities, network topology). See [Galaxy Graph](./09-galaxy-graph.md) for full details.

### `vera-ui.js`

Other shared primitives — buttons, panels, modals, the toast helper. Smaller surface than `vera_graph.js`.

---

## 6. Themes

The harness ships several themes (`ash`, `dusk`, `void`, `chalk`, `ice`, `joe`) selectable from the palette button. Each theme is a set of CSS custom properties served by `GET /ui/theme`. When the user switches themes:

1. The harness updates `document.documentElement.style` with the new variables.
2. It broadcasts a `vera:theme` postMessage to all iframes.
3. Each iframe applies the variables to its own root.

A theme change is persistent — saved to `localStorage` and restored on next load.

Custom themes can be saved via `POST /ui/theme` (with `{name, vars}`) and they're loaded from Redis. The harness's theme cycler picks them up automatically.

---

## 7. Session ID

A single session ID (`window._veraSessionId`) is shared across the entire harness and all iframes. It's generated when the harness loads (or restored from `localStorage`) and is used to:

- Tag all memory graph nodes for this session
- Build the FOLLOWS_ACTIVITY chain
- Group capability calls into one logical session
- Drive the memory graph panel's session selector

Iframes read it via `window.parent._veraSessionId` or fall back to their own `localStorage` if they're loaded standalone. The legacy name `_chatSessionId` is kept as a mirror for older panels.

---

## 8. Tabs structure

The harness's built-in tabs (always present, hard-coded in the HTML):

| Tab | Purpose |
|---|---|
| Dashboard | Health, cluster, memory stats overview |
| Caps | Capability browser with parameter-aware test runner |
| DAG Workshop | DAG editor, planner, supervised/stepwise runner, loop builder |
| Servers | MCP server registry; register new proxies |
| Ollama | Per-node status, models, VRAM, queue depth |
| Workers | Per-host worker grid with task throughput |
| Redis | Connection info, stream info, pending tasks |
| Syslog | Live event feed with filters |
| Media | Sub-switcher for `mode="inject"` panels |
| Reload | Re-fetches `/ui/panels` and re-builds all auto-tabs |

Auto-tabs (registered by modules) appear interleaved with built-in tabs according to their `tab_order`.

---

## 9. Live updates

When the harness's WebSocket receives events, it:

1. Pushes a line into the syslog feed (filterable by group).
2. Updates the dashboard's per-cap counters (driven by `cap.call` events).
3. Refreshes any panel that has subscribed to a relevant event type.

Panel-specific live-update wiring is the panel's responsibility. The common pattern is for the panel to subscribe to a specific event prefix:

```javascript
// in a panel's JS, accessing the parent's bus
window.parent.addEventListener('message', (e) => {
  if (e.data?.type === 'vera_fabric_event' && e.data.event?.type.startsWith('fabric.')) {
    // re-render
  }
});
```

Or, if the panel opens its own WS:

```javascript
ws.send(JSON.stringify({action: 'subscribe_events'}));
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  const ev = msg.type === 'event' ? msg.data : msg;
  // handle ev.type
};
```

---

## 10. Reloading panels

The harness has a reload button. Hitting it:

1. Clears the `/ui/panels` cache.
2. Removes all auto-created tab elements (`[data-auto-panel-id]`).
3. Resets the Media sub-switcher.
4. Re-fetches and rebuilds.

This is how you get a freshly registered panel to appear without restarting the orchestrator — useful when iterating on panel registration code.

---

## 11. Style and conventions

- **Monochrome icons**, no bare emoji in chrome. SVG paths or text glyphs (`⬡ ⧗ ↪ ↳ ▦ ▸`).
- **Dense layouts**: collapsible drawers rather than spreading controls across tabs.
- **Status bars** at the bottom of action panes: `<span class="status-bar ok">✓ Done</span>`.
- **Monospace** for IDs, paths, code, and capability names. Sans-serif for prose.
- **CSS variables**: every theme respects the same variable set (`--bg0/--bg1/--bg2`, `--acc/--acc2/--acc3`, `--err/--ok/--warn`, `--text/--dim/--dim2`, `--border`, `--mono`). Panels should use these, never hard-coded colours.

---

## See also

- [Galaxy Graph](./09-galaxy-graph.md) — the `vera-graph.js` web component
- [DAG Engine](./03-dag-engine.md) — the DAG Workshop tab
- [IDE Module](./08-ide.md) — the IDE auto-tab
- [Research System](./07-research.md) — the Research auto-tab