# 02 · Harness UI

![The harness dashboard captured from the running Vera UI](assets/overview/dashboard.png)

The harness is the single-page application served at `GET /` by the
orchestrator (`vera/capability_orchestration.html`). It is the entry point to
everything Vera does — capabilities, observability, the DAG workshop, the data
fabric, the memory graph, the IDE, research, chat, Loop Lab and every
module-provided panel — and it is the shell that the [PWA](./47-pwa.md) makes
installable.

It is intentionally a thin shell. Its only built-in tab is the **Dashboard**;
everything else is a **panel** registered by a module with `register_ui()` and
discovered at load through `GET /ui/panels`. Most panels are standalone HTML
pages served at their own routes and mounted into the shell in iframes. The
shared front-end parts the panels build on — `vera-ui.js` (theme, appearance,
scale, read states), `vera-panel.css`/`vera-panel.js` (canonical sidebar),
`vera-panel-bridge.js` (shell ↔ panel protocol), widgets and the right-click
menu — are described in [the UI framework guide](../docs/UI-FRAMEWORK.md).

**Maturity:** stable and in daily use. The panel registry, auto-tabs, lazy
iframe loading, theme broadcasting and the WebSocket event feed are
production paths; folded tabs and the panel menu declarations (`sections`,
`options`) are newer additions.

## Contents

- [1. Architecture](#1-architecture)
- [2. Panel registration](#2-panel-registration)
  - [`register_ui` parameters](#register_ui-parameters)
  - [`mode="tab"`](#modetab)
  - [Folded tabs](#folded-tabs)
  - [`mode="inject"`](#modeinject)
  - [`mode="element"`](#modeelement)
  - [`mode="mount"`](#modemount)
- [3. Panel discovery and loading](#3-panel-discovery-and-loading)
- [4. Iframe pattern](#4-iframe-pattern)
- [5. Shell features](#5-shell-features)
- [6. Shared front-end scripts](#6-shared-front-end-scripts)
- [7. Themes and appearance](#7-themes-and-appearance)
- [8. Session ID](#8-session-id)
- [9. Live updates](#9-live-updates)
- [10. Reloading panels](#10-reloading-panels)
- [11. Panel-related HTTP routes and capabilities](#11-panel-related-http-routes-and-capabilities)
- [12. Style and conventions](#12-style-and-conventions)
- [13. Troubleshooting](#13-troubleshooting)
- [See also](#see-also)

---

## 1. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│   Harness (capability_orchestration.html @ GET /)                    │
│                                                                      │
│   Tab bar / LHM:  Dashboard │ auto-tabs from /ui/panels │ custom tabs │
│                   controls: ☰ LHM · ◫ events · ⇕ autohide · + pane   │
│                             ⊞ promote panel · ↗ / ⧉ standalone       │
│                                                                      │
│   ┌──────────────────────────────────────────────────────────────┐   │
│   │  Active panel area (one or more panes)                       │   │
│   │    static Dashboard markup   or                              │   │
│   │    iframe → /<panel route>   (parked until first shown)      │   │
│   └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   Live events sidebar  ◄── WS /ws/mcp  {action:"subscribe_events"}  │
│   Theme / scale / session id ──postMessage──► every panel iframe     │
└──────────────────────────────────────────────────────────────────────┘
```

On load the harness opens one WebSocket to `/ws/mcp`, sends
`{"action":"subscribe_events"}`, and fetches `/ui/panels`. Panels running in
iframes either receive what they need from the parent via `postMessage`
(theme, events, navigation) or open their own WebSocket. The shared graph bus
(`vera_graph.js`) prefers the parent's connection when one exists.

## 2. Panel registration

Modules register panels by calling `register_ui()` from
`capability_orchestration`:

```python
from Vera.vera.capability_orchestration import APP, register_ui

register_ui(
    panel_id  = "my-panel",
    label     = "My Panel",
    icon      = "⬡",
    html      = '<iframe src="/my/panel" style="width:100%;height:100%;border:none"></iframe>',
    js        = "",                      # optional inline JS run once after injection
    ui_caps   = ["my.cap.one", "my.cap.two"],
    mode      = "tab",                   # "tab" | "inject" | "element" | "mount"
    tab_order = 50,
)
```

This writes `UI_PANELS[panel_id]`, which `GET /ui/panels` (`ui.panels`) exposes.
Registering the same id again replaces the earlier entry.

### `register_ui` parameters

| Parameter | Default | Meaning |
|---|---|---|
| `panel_id`, `label`, `icon` | required | Identity and display. Top-level tabs show the label only (no glyph). |
| `html` | required | Markup injected for the panel — normally a single iframe. |
| `js` | `""` | Inline script executed once after the HTML is injected. |
| `ui_caps` | `[]` | Capabilities the panel uses; also how `caps.specialist` maps a capability to its panel. |
| `mode` | `"inject"` | Where the panel appears (below). |
| `tab_order` | `100` | Sort key; lower is further left. |
| `specialist_agent`, `specialist_loop_profile` | `""` | Declarative binding to the agent persona / loop preset that is "expert" in this panel — read by `ui.panel.specialist`, `caps.specialist` and `<vera-panel-copilot>`. |
| `specialist_context_cap` | `""` | A capability returning a fresh context snapshot to hand the specialist before it answers. |
| `sections`, `options` | `[]` | The panel's own menu: `sections [{id, label, tabs:[{id,label}]}]` and `options [{id, label, kind, get, set}]`, so the shell's left-hand menu, the chat rail and the panel's side menu are built from one declaration. |

Conventional `tab_order` ranges:

| Range | Used for |
|---|---|
| 0–20 | Core tabs |
| 20–40 | Primary tools (chat, IDE) |
| 40–60 | Domain panels (fabric, research, memory, galaxy) |
| 60–100 | Secondary and utility panels |
| 100+ | Default when unspecified |

### `mode="tab"`

Creates a top-level tab and panel div (`_createAutoTab`). Iframe `src`
attributes are parked as `data-lazy-src` before the panel is attached, so
nothing loads until the tab is first shown; a spinner appears on activation and
a Retry affordance replaces it if the frame never loads. The panel's `js`
snippet is injected once.

### Folded tabs

Some registered tabs are folded into a broader host tab and get no tab button
of their own. `vera/estate/estate_nav_core.py` (`RETIRED_TABS`) holds the map:

| Folded panel id | Opens |
|---|---|
| `proxmox-panel`, `remote-connections` | Estate → Machines (`proxmox`, `remote` panes) |
| `netgraph-panel` | Estate → Network & Access (`network` → `graph`) |
| `provisioning-panel`, `identity-panel` | Estate → Identity & Trust (`provision` → `security` / `identity`) |
| `provision-panel` | Estate → Build (`software`) |
| `integrations`, `platform-config` | Estate → Integrations |
| `cap-ontology`, `mcp-catalog-panel` | Capabilities (`cap-hub`) → `ontology` / `mcp` → `catalog` |
| `agentbridge-catalog-panel` | Agents (`agents-skills-ontologies`) → `bridges` |
| `character-studio` | Image Studio → `companion` |

(The Estate host is the `workers-ollama` panel.) While folding is on,
`GET /ui/panels` marks each folded tab with
`retired_into: {panel, pane, sub, section}`; opening one switches to the host
tab and posts `vera:estate:open {pane, sub, entity, rid}` to its iframe, which
acknowledges with `vera:estate:opened`. Routes and capabilities are unchanged.
The switch is stored in Redis at `vera:ui:retire_overlap_tabs` (absent = on);
`ui.tabs.retired.set enabled=false` brings the separate tabs back on the next
page load, and `ui.tabs.retired` reports the state.

### `mode="inject"`

Adds the panel as a sub-panel of the shell's Media switcher (the Elements
switcher). Used for small widgets that do not justify a tab — for example the
Perf monitor and the Capability Hub's reusable elements. Iframes inside are
parked until the sub-tab is shown. A panel registered without a `mode` is
treated as `inject` (three legacy media ids are mapped explicitly).

### `mode="element"`

Registered and listed — available to the ⊞ tab picker, the standalone picker,
dashboard widget loaders and `GET /ui/panel/window?id=` — but not rendered
anywhere automatically. Used for panels that live inside another panel's
sub-tabs or are settings pages: for example the agent Registry (inside
Agents/Skills/Ontologies) and Install / PWA.

### `mode="mount"`

Injects into a pre-declared mount point listed in the shell's
`DEDICATED_PANEL_MOUNTS`. That map is currently empty — the former mount-mode
panels (skills, ontologies) were migrated to iframe tabs — so the mode is kept
only for compatibility.

## 3. Panel discovery and loading

`GET /ui/panels` returns the list of registered panels:

```json
[
  {
    "id": "my-panel", "label": "My Panel", "icon": "⬡",
    "html": "<iframe ...></iframe>", "js": "",
    "ui_caps": ["my.cap.one"], "mode": "tab", "tab_order": 50,
    "specialist_agent": "", "specialist_loop_profile": "", "specialist_context_cap": "",
    "sections": [], "options": []
  }
]
```

The harness's `loadAllPanels()`:

1. Fetches `/ui/panels` once per session (cached; retries every 3 s while the
   list is still empty during startup).
2. Sorts by `tab_order`.
3. Dispatches by mode: `tab` → `_createAutoTab(p)` (or records a redirect if
   `retired_into` is set); `inject` → the Media switcher; `element` → counted
   only; `mount` → lazily on activation.
4. Restores the user's custom tabs (promoted with ⊞, stored per browser in
   `localStorage` under `vera.customTabs.v1`).
5. Reports `✓ <n> panels — <n> media · <n> auto-tabs · <n> elements`.

Before injection, tab HTML goes through `_rewritePanelUrls()`, which rewrites
root-relative `src="/…"` attributes (iframes, images, scripts) to the backend
origin returned by `base()`, so a shell served from a different origin still
loads panel assets from the orchestrator. `href` attributes are not rewritten.

## 4. Iframe pattern

Almost every panel is structured like this:

```python
from pathlib import Path
from fastapi.responses import HTMLResponse
from Vera.vera.capability_orchestration import APP, register_ui

_HERE = Path(__file__).parent

@APP.get("/research/panel", include_in_schema=False)
async def _research_panel():
    p = _HERE / "research_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>research_panel.html not found</p>")

register_ui(
    panel_id  = "research-panel",
    label     = "Research",
    icon      = "",
    html      = '<iframe src="/research/panel" style="width:100%;height:100%;border:none"></iframe>',
    mode      = "tab",
    tab_order = 55,
)
```

**Why iframes rather than `innerHTML`?** Scripts inserted with `innerHTML` do
not execute; an iframe gives each panel its own `window`, its own scripts and
isolation from other panels' globals and CSS. The panel route reads the HTML
file on every request, so an edited panel is live on the next load without a
restart.

**Why separate HTML files rather than Python strings?** They are far easier to
edit, lint and validate, and they can be served standalone. Panels that need
their configuration can rely on the injected `window.__VERA_BASE__` /
`window.__VERA_DOMAIN__` ([Configuration §2](./10-configuration.md#2-network-hosts-and-tls)).

For pages that only exist as a registration fragment (no route of their own),
`GET /ui/panel/window?id=<panel_id>` wraps the panel's `html` and `js` in a
standalone document with `vera-ui.js` and an `api(path, method, body)` helper,
so it can be popped out into its own window. `GET /ui/panels/file/<name>_panel.html`
serves whitelisted `*_panel.html` files from `vera/` directly (no path
traversal).

## 5. Shell features

| Control | What it does |
|---|---|
| ☰ (LHM) | Switches between the horizontal tab bar and the left-hand menu (LHM). Panels that use the canonical sidebar markup, or call `VeraPanelBridge.registerNav()`, have their sections docked into the LHM. |
| ◫ | Shows or hides the live events sidebar. |
| ⇕ | Auto-hides the tab bar; ⧈ keeps a panel's own inner menu visible while it is hidden. |
| + | Opens another panel as an additional pane beside the current one. |
| ⊞ | Promotes any registered panel (a hub sub-tab, an element) to a top-level custom tab; right-click a custom tab to remove it. |
| ↗ / ⧉ | Opens the current tab, or any panel, standalone in a new browser tab. |
| `?place=<name>` | Opens a named place on load. Places (`vera/ui/places_core.py`, served by `ui.places`) map names such as `estate/perf` to a panel, pane and sub-tab, so dashboard drill-throughs never guess a destination. |
| Driven ribbon | Shown over a tab opened by an assistant directive: who opened it, the policy in force, Undo and the directive log (`ui.directive.*`, `ui.policy.*` — see [the UI framework guide](../docs/UI-FRAMEWORK.md#4-driving-the-ui-the-aide-and-scripts)). |

The **Dashboard** tab is static markup in the shell: a cluster overview with a
live toggle, health tiles and summaries refreshed from capabilities such as
`dash.health.summary` and `topology.snapshot`.

## 6. Shared front-end scripts

| Route | Source | Role |
|---|---|---|
| `/ui/vera-ui.js` | `vera/vera-ui.js` | Theme, appearance (style pack, density, blocks), text size, contrast and UI scale; maps theme variables onto every namespace; `window.veraUI` (`setTheme`, `applyVars`, `setAppearance`, `setScale`, `openEntity`, `openPlace`, `pulseOnce`, `readState`, `renderReadState`, …) |
| `/ui/vera-panel.css`, `/ui/vera-panel.js` | `vera/vera-panel.css`, `vera/vera-panel.js` | Canonical panel chrome: collapsible sidebar/icon rail, collapsible sections, automatic nav bridging |
| `/ui/vera-panel-bridge.js` | `vera/chat/vera-panel-bridge.js` | Shell/chat ↔ panel protocol: state publishing, action handlers (`panel.dispatch`), nav docking, top-bar absorption |
| `/ui/vera-lhm.js` | `vera/chat/vera-lhm.js` | The left-hand menu |
| `/ui/vera-loader.js` | `vera/vera-loader.js` | Configurable loading animation (`ui.loader.get/set`) |
| `/ui/vera-select.js` | `vera/vera-select.js` | Searchable single/multi-select over an existing `<input>` (`data-select=…`) |
| `/ui/vera-graph.js` | `vera/vera_graph.js` | The graph component (`window.veraUI.Graph`), see [Galaxy Graph](./09-galaxy-graph.md) |
| `/ui/vera-dashboard.js` | `vera/chat/vera-dashboard.js` | `<vera-dashboard layout="key">` widget dashboards |
| `/ui/widgets/widget_element.js` | `vera/widgets/widget_element.js` | `<vera-widget>` and `VeraWidget.fromCapResult`/`fromCapStream` |
| `/ui/menus.js`, `/ui/rcm.js` | `vera/ui/` | Shared menus and the right-click menu |
| `/ui/themes.css`, `/ui/design.css` | generated from `vera/theme_defs.py`; `vera/ui/design.css` | Theme variables, style packs and the shared design layer |
| `/ui/elements/*.js` | various `*_element.js` | Reusable custom elements (`<vera-markdown>`, `<vera-research-card>`, `<vera-panel-copilot>`, `<vera-sparkline>`, `<vera-loop-graph>`, `<vera-chat-data>`, `<vera-flow-builder>`, …) |

### Shared read states

`vera-ui.js` exposes `readState` and `renderReadState` helpers for read-only
panel data. They keep four states distinct — `loading`, `ready`, `empty` and
`error` — render service messages as text nodes with bounded labels,
accessible status/alert roles and an optional retry action. They do not own
fetching, caching, domain data or workflow state; each panel keeps those.
Agent Bridges is the first consumer: its catalog and interoperability summary
load independently, so one stays usable when the other fails, and a rejected
request becomes a visible, retryable error instead of a permanent loading
placeholder.

### Execution and capability feedback

Activity cards and graph sidebars can link Run, workflow, task, capability,
session, trace and parent/child identities; distinguish a native authority
from a shadow projection; and show attempts, progress, artifacts, controls and
terminal state. Capability views can additionally show the canonical task,
declared effects, lifecycle, operational observations, resolver
exclusions/ranking and policy verdict or enforcement selection.

These displays are projections. A UI button or graph edge must not imply that a
shadow Run owns execution, that a resolver authorized its preferred candidate,
or that a static DBOS/Temporal mapping is runnable. Panels should surface the
authority and evidence source beside the status, and link back to Activity,
Loop Lab or the native studio for the actual operation.

## 7. Themes and appearance

Theme definitions live in `vera/theme_defs.py`, the single source for both the
generated stylesheet (`/ui/themes.css`) and the JSON API:

- **Built-in themes:** `ash`, `dusk` (default), `void`, `chalk`, `ice`,
  `solar-light`, `solar-dark`, `gruvbox`, `nord`, `dracula`, `monokai`,
  `tokyonight`, `matrix`, `amber`, `c64`, `paperwhite`. Custom themes created
  with `ui.theme.create` are validated for text contrast and stored in Redis.
- **Style packs:** `standard` (default), `newspaper`, `terminal`, `pixel`.
- **Density tiers:** `full` (default), `hover`, `zen`; plus a blocks on/off
  switch, text size, contrast and a global UI scale.

| Capability | Route | Purpose |
|---|---|---|
| `ui.themes` | `GET /ui/themes` | All themes with variables and metadata |
| `ui.theme.get` | `GET /ui/theme` | Active theme id and variables |
| `ui.theme.set` | `POST /ui/theme/set` | Set the active theme (persisted in `vera:ui:theme`, broadcast as `ui.theme.changed`) |
| `ui.theme.create` / `ui.theme.delete` | `POST /ui/theme/create`, `/ui/theme/delete` | Manage custom themes |
| `ui.theme.css` | `GET /ui/theme/css` | Theme CSS |
| `ui.appearance.get` / `.set` | `GET /ui/appearance`, `POST /ui/appearance/set` | Style pack, density, blocks |
| `ui.scale.get` / `.set` | `GET /ui/scale`, `POST /ui/scale/set` | Shared UI scale (a per-browser choice wins) |
| `ui.loader.get` / `.set` | `GET /ui/loader`, `POST /ui/loader/set` | Loading animation |

When the theme changes, the shell applies the variables to its root, caches
the theme id and its resolved variables in `localStorage`
(`vera:ui:theme`, `vera:ui:themeVars`, stamped by `vera:ui:themeVarsFor` so
stale variables are never replayed under another theme), and posts
`{type: "vera:theme", theme, vars}` to every panel iframe; `ui.theme.changed`
events update other open windows. A small boot script in `<head>` paints the
cached theme and UI scale before CSS parses, so pages do not flash. See
[UI Builder](./26-ui-builder.md) for theme authoring.

## 8. Session ID

A single session id is shared across the harness and its iframes. The shell
creates it on every page load as `chat-<epoch ms>`, writes it to
`localStorage` (`vera_session_id`) and exposes it as `window._veraSessionId`,
with `window._chatSessionId` kept as a legacy mirror. It is used to:

- tag memory graph nodes and build the `FOLLOWS_ACTIVITY` chain;
- group capability calls into one logical session (passed as `session_id` on
  `/mcp/call`);
- scope directive logs, the panel activity mirror and the memory graph
  panel's session selector.

Iframes read `window.parent._veraSessionId`, or fall back to their own state
when loaded standalone. The chat panel maintains its own session id for its
conversations.

## 9. Live updates

When the harness's WebSocket receives an event, it:

1. Appends it to the live events feed (filterable, pausable).
2. Updates dashboard counters driven by `cap.*` events.
3. Forwards relevant events to panels — for example
   `{type: "vera_fabric_event", event}` to the fabric panel, and theme changes
   as `vera:theme`.

Panel-specific live updates are the panel's responsibility. Either listen to
the parent:

```javascript
window.addEventListener('message', (e) => {
  if (e.data?.type === 'vera_fabric_event' && e.data.event?.type?.startsWith('fabric.')) {
    // re-render
  }
});
```

or open a WebSocket of your own:

```javascript
const ws = new WebSocket(location.origin.replace(/^http/, 'ws') + '/ws/mcp');
ws.onopen = () => ws.send(JSON.stringify({action: 'subscribe_events'}));
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type !== 'event') return;     // greeting, tool results, etc.
  const ev = msg.data;                  // {type, name, trace_id, ...}
};
```

The shell reconnects automatically (an 8 s connect timeout, then a 5 s retry)
and re-subscribes on every reconnect. The full WebSocket protocol is in
[Capability Framework §4](./01-capability-framework.md#ws-wsmcp).

## 10. Reloading panels

`reloadPanels()` clears the `/ui/panels` cache, removes every auto-created tab
and panel element (`[data-auto-panel-id]`, `[id^="panel-auto-"]`), resets the
Media switcher and dedicated mounts, then runs `loadAllPanels()` again. This is
how a freshly registered panel appears without restarting the orchestrator.

## 11. Panel-related HTTP routes and capabilities

| Route / capability | Purpose |
|---|---|
| `GET /` | The harness |
| `GET /ui/panels` (`ui.panels`) | Registered panels, with `retired_into` for folded tabs |
| `GET /ui/panel/specialist?panel_id=` (`ui.panel.specialist`) | A panel's specialist binding |
| `GET /caps/specialist?cap_name=` (`caps.specialist`) | The specialist bound to a capability, via the panel whose `ui_caps` contains it (or its dot-family) |
| `GET /ui/tabs/retired`, `POST /ui/tabs/retired/set` | Folded-tab switch |
| `GET /ui/places` (`ui.places`) | Named places for drill-through |
| `GET /ui/panel/window?id=` | Any registered panel as a standalone page |
| `GET /ui/panels/file/<name>_panel.html` | Whitelisted panel files |
| `GET /ui/panels/agents-skills-ontologies`, `/ui/panels/workers-ollama`, `/ui/panels/model-routing`, `/ui/panels/agents-panel`, `/ui/panels/skills-panel`, `/ui/panels/ontologies-panel` | Combined and standalone shell panels served by the orchestrator |
| `ui.panel.list/get/create/update/delete` | User-built panels ([UI Builder](./26-ui-builder.md)) |
| `ui.directive*`, `ui.policy.*`, `ui.event`, `ui.room`, `ui.script.*` | Assistant-driven UI directives and UI scripts ([UI framework guide](../docs/UI-FRAMEWORK.md)) |

## 12. Style and conventions

- **Monochrome icons**, no bare emoji in chrome: SVG paths or text glyphs
  (`⬡ ⧗ ↪ ↳ ▦ ▸`). Top-level tabs are text-only.
- **Dense layouts**: collapsible drawers rather than spreading controls across
  tabs.
- **Status bars** at the bottom of action panes:
  `<span class="status-bar ok">✓ Done</span>`.
- **Monospace** for ids, paths, code and capability names; sans-serif for
  prose.
- **CSS variables**: every theme provides the same variable set (`--bg0`…`--bg3`,
  `--acc`…`--acc4`, `--ok`/`--err`/`--warn`, `--text`/`--dim`/`--dim2`,
  `--border`/`--border2`, `--mono`/`--sans`, `--on-ac` for text on accent
  surfaces). Panels use these, never hard-coded colours.
- **No polling while hidden**: a panel should not poll while `document.hidden`
  and should never start a second read while one is pending; parked iframes are
  not loaded until shown.

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "⚠ No panels yet — capabilities not loaded" | The orchestrator is still loading modules; the shell retries every 3 s |
| A module's tab is missing | The module failed to import (`GET /modules`), it registers with `mode="element"`/`"inject"`, or it is folded into a host tab (`GET /ui/panels` shows `retired_into`) |
| A panel shows a blank frame and Retry | Its route returned an error or never loaded; open the route directly to see the error |
| Panel assets load from the wrong origin | Use root-relative `src="/…"` (rewritten) and resolve fetches against `window.location.origin` or `window.__VERA_BASE__` |
| Theme flashes or shows dark colours under a light theme | Stale cached variables; clear `vera:ui:themeVars*` in `localStorage` |
| Live events stop | The WebSocket dropped; the status indicator shows the close reason and the shell reconnects. Long event-loop stalls cause drops — see [Performance and sizing](./00-performance-and-sizing.md#5-runtime-diagnostics) |

## See also

- [Capability Framework](./01-capability-framework.md) — the registry behind `/ui/panels` and `/mcp/call`
- [UI framework guide](../docs/UI-FRAMEWORK.md) — shared parts and contracts for panels and capabilities
- [Galaxy Graph](./09-galaxy-graph.md) — the `vera-graph.js` component
- [DAG Engine](./03-dag-engine.md) — the DAG Workshop
- [IDE Module](./08-ide.md) — the IDE tab
- [Research System](./07-research.md) — the Research tab
- [UI Builder](./26-ui-builder.md) — themes, user-built panels and the loading animation
- [PWA](./47-pwa.md) — installing the harness as an app

## Screenshots (operator-captured)

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
