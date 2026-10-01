# 26 · UI Builder

The UI Builder is Vera's UI **system** module: the shared theme engine
(themes, style packs, density tiers, UI scale, loading animation), runtime
**panel CRUD** that lets a person or an LLM author new harness panels, and
per-scope capability **access-control lists**. It also serves the shared
front-end assets (`vera-ui.js`, `vera-panel.css/js`, `vera-select.js`,
`vera-loader.js`, `vera-graph.js`) and several reusable element scripts.

The backend is `vera/ui builder/ui_capabilities.py` (note the space in the
directory name) and the panel is `vera/ui builder/ui_builder_panel.html`. Theme
tables live in `vera/theme_defs.py`, the single source shared by the JSON API
and the served stylesheet.

**Status:** stable. Themes, appearance and panel CRUD are used throughout the
harness. The capability ACL is **advisory**: it is stored and reported, but
nothing in the request path currently enforces it ([§5](#5-capability-access-control)).

## Contents

- [1. Source map](#1-source-map)
- [2. Themes](#2-themes)
- [3. Appearance, scale and loading animation](#3-appearance-scale-and-loading-animation)
- [4. Panel CRUD — LLM-built UI](#4-panel-crud--llm-built-ui)
- [5. Capability access control](#5-capability-access-control)
- [6. Served assets and elements](#6-served-assets-and-elements)
- [7. The UI Builder panel](#7-the-ui-builder-panel)
- [8. Events and storage](#8-events-and-storage)
- [9. Worked examples](#9-worked-examples)
- [10. Panel lifecycle, access and troubleshooting](#10-panel-lifecycle-access-and-troubleshooting)
- [See also](#see-also)

---

## 1. Source map

| Path | Responsibility |
|---|---|
| `vera/ui builder/ui_capabilities.py` | `ui.theme*`, `ui.loader.*`, `ui.scale.*`, `ui.appearance.*`, `ui.panel.*`, `ui.caps.*` caps; asset routes; `ui-builder` panel registration; startup reload from Redis. |
| `vera/ui builder/ui_builder_panel.html` | The UI Builder panel (served at `/ui/builder/panel`). |
| `vera/theme_defs.py` | `BUILTIN_THEMES`, `DEFAULT_THEME` (`dusk`), `STYLE_PACKS`, `DENSITY_TIERS`, data-viz ramps, `ensure_contrast`, `appearance_css`. |
| `vera/vera-ui.js`, `vera/vera-panel.css`, `vera/vera-panel.js`, `vera/vera-select.js`, `vera/vera-loader.js` | Shared front-end primitives served under `/ui/…`. |
| `vera/chat/chat_panels_capabilities.py` | Registers a second copy of the `ui.theme*`, `ui.panel.*` and `ui.caps.*` caps, plus the panel bridge (see [Agents & Chat §13](./19-agents-chat.md#13-panel-bridge)). |

> [!WARNING]
> Both modules register the same `ui.theme*`, `ui.panel.*` and `ui.caps.*` capability names and paths. `ui builder/ui_capabilities.py` is loaded earlier in the module list and `chat/chat_panels_capabilities.py` later. The copies are close but not identical (only the UI Builder copy adds data-viz ramps to custom themes, style packs and the `ui.loader` / `ui.scale` / `ui.appearance` caps), so keep them in sync when changing either.

---

## 2. Themes

A unified theme system: built-in themes plus custom themes, stored in Redis and broadcast to every open UI by event (the mechanism described in [Harness UI](./02-harness-ui.md)).

Built-in themes (`theme_defs.BUILTIN_THEMES`): `ash` (light), `dusk` (default), `void`, `chalk`, `ice`, `solar-light`, `solar-dark`, `gruvbox`, `nord`, `dracula`, `monokai`, `tokyonight`, `matrix`, `amber`, `c64`, `paperwhite`. Each has `label`, `type` (light/dark), `accent` and CSS `vars`.

| Cap | Route | Purpose |
|---|---|---|
| `ui.themes` | `GET /ui/themes` | All themes with CSS variables and the active id. |
| `ui.theme.get` | `GET /ui/theme` | Active theme id, vars and type. |
| `ui.theme.set` | `POST /ui/theme/set` | Set the active theme (`theme`); persists and broadcasts `ui.theme.changed`. |
| `ui.theme.create` | `POST /ui/theme/create` | Custom theme: `id` (alphanumeric), `label`, `type` (`light`/`dark`), `accent`, `vars` (JSON). Unreadable colour choices are repaired (`ensure_contrast`) and a data-viz ramp is added for the theme type. |
| `ui.theme.delete` | `POST /ui/theme/delete` | Delete a custom theme. Built-ins cannot be deleted; deleting the active theme falls back to `dusk`. Emits `ui.theme.deleted`. |
| `ui.theme.css` | `GET /ui/theme/css` | All themes as a CSS string. |

`GET /ui/themes.css` serves the same stylesheet directly: every theme's colour block, the derived surfaces, and the style packs.

---

## 3. Appearance, scale and loading animation

These mirror the theme API. The live per-device value lives in `localStorage` (via `vera-ui.js`); the server value seeds fresh browsers and broadcasts programmatic changes.

| Cap | Route | Values |
|---|---|---|
| `ui.appearance.get` / `ui.appearance.set` | `GET /ui/appearance`, `POST /ui/appearance/set` | `style` ∈ `standard` (default), `newspaper`, `terminal`, `pixel`; `density` ∈ `full` (default — citations, actions and tool detail inline), `hover` (chrome floats out until you point at a turn), `zen` (words, rendered output and timestamps; detail on click); `blocks` (bool, default true — paint per-turn block backgrounds). Emits `ui.appearance.changed`. |
| `ui.scale.get` / `ui.scale.set` | `GET /ui/scale`, `POST /ui/scale/set` | Global zoom 0.6–2.0 (default 1.0), applied as CSS `zoom` across every panel. Emits `ui.scale.changed`. |
| `ui.loader.get` / `ui.loader.set` | `GET /ui/loader`, `POST /ui/loader/set` | Loading animation `type` ∈ `graph` (default), `pulse`, `orbit`, `spinner`, `sprite`; `speed` 0.1–4; `density` 0.3–3; `sprite` `{url, frames, fps, w, h}` (use a `data:` URI under CSP). Emits `ui.loader.changed`. |

---

## 4. Panel CRUD — LLM-built UI

Panels created at runtime live in `DYNAMIC_PANELS`, are registered with `register_ui()` so they appear in the harness immediately, and are persisted to Redis so they survive restarts.

| Cap | Route | Purpose |
|---|---|---|
| `ui.panel.list` | `GET /ui/panel/list` | Every registered panel (built-in and dynamic) with `mode`, `tab_order`, `ui_caps`, a `dynamic` flag and HTML/JS sizes. |
| `ui.panel.get` | `GET /ui/panel/get` | A panel's full HTML, JS and metadata (`id`). |
| `ui.panel.create` | `POST /ui/panel/create` | `id` (required), `label`, `icon`, `html`, `js`, `mode` (`tab` default, or `inject`), `tab_order` (200), `ui_caps` (CSV). Emits `ui.panel.created`. |
| `ui.panel.update` | `POST /ui/panel/update` | Replace `html`, `js` and/or `label`. Emits `ui.panel.updated`. |
| `ui.panel.delete` | `POST /ui/panel/delete` | Delete a **dynamic** panel; built-ins are refused. Emits `ui.panel.deleted`. |

Notes:

- `ui_caps` grants the agent those capabilities while the panel is open in chat (see [Agents & Chat §6](./19-agents-chat.md#6-tool-and-capability-selection-in-chat)).
- In the panel's `js`, use `VeraPanelBridge.registerActionHandler(name, fn)` so chat can drive it with `panel.dispatch`. The built-in skill `sys-ui-building` teaches agents this pattern.
- `ui.panel.update` on a **built-in** panel stores the override as a dynamic panel and persists it, so it survives restarts. That panel then also becomes deletable through `ui.panel.delete`.
- `render.screen` can float any registered panel, including one just created, over the chat (see [Render](./28-render.md#6-in-chat-rendering)).

---

## 5. Capability access control

Scoped allow/deny lists describe which caps a given surface should use. If a scope's whitelist is empty, all caps except the blacklist are allowed; if it is non-empty, only whitelisted caps (minus the blacklist) are.

| Scope | Default whitelist | Default blacklist |
|---|---|---|
| `general` | all | `obs.health`, `obs.cluster`, `obs.redis`, `obs.events`, `health.check`, `cluster.status`, `ui.panel.delete`, `ui.panel.update` |
| `dag_builder` | all | `ui.panel.create`, `ui.panel.delete`, `ui.panel.update` |
| `ui_builder` | `ui.panel.create/update/list/get`, `ui.themes`, `ui.theme.get/set/create`, `ui.caps.acl/scopes`, `fabric.datasets/query/stats`, `memory.search/recall`, `llm.generate`, `echo` | — |
| `agent` | all | `ui.panel.delete`, `fabric.reset` |

| Cap | Route | Purpose |
|---|---|---|
| `ui.caps.acl` | `POST /ui/caps/acl` | `scope` only → read; with `whitelist` and/or `blacklist` (CSV) → replace and persist. |
| `ui.caps.scopes` | `GET /ui/caps/scopes` | Scopes with whitelist/blacklist counts and effective count. |
| `ui.caps.allowed` | `GET /ui/caps/allowed` | Effective allowlist for a `scope` (default `general`). |

> [!IMPORTANT]
> These lists are **advisory**. `get_allowed_caps()` is not called from the capability dispatcher, the chat cap hint or the agent runtime, so changing an ACL does not by itself block a call. Agent tool access is governed by each agent's `domain_caps` ([Agents & Chat](./19-agents-chat.md)) and runtime enforcement by [Capability Policy](./45-capability-policy.md). Earlier docs described these scopes as "the same allowlist mechanism agents use"; that was not accurate.

---

## 6. Served assets and elements

| Route | Source |
|---|---|
| `/ui/themes.css` | Generated theme + appearance CSS |
| `/ui/vera-ui.js` | Shared UI primitives (theme/scale/appearance application) |
| `/ui/vera-panel.css`, `/ui/vera-panel.js` | Canonical left-hand-menu and form-control chrome |
| `/ui/vera-select.js` | Searchable select (e.g. capability pickers) |
| `/ui/vera-loader.js` | Loading animation |
| `/ui/vera-graph.js` | Unified reusable graph element |
| `/ui/elements/agent_loop_output.js` | `<vera-agent-loop-output>` |
| `/ui/elements/vera_markdown.js` | Markdown element |
| `/ui/widgets/widget_element.js` | Widget element |
| `/ui/elements/loop_graph.js`, `loop_throbber.js`, `sparkline.js`, `topology_map.js`, `sandbox_controls.js`, `agent_loop_config.js`, `character.js` | Further reusable elements |
| `/ui/builder/panel` | The UI Builder panel |

All are read from disk on each request, so editing them needs no restart.

---

## 7. The UI Builder panel

Registered as panel `ui-builder` ("UI Builder", tab, `tab_order` 85). Its sections:

| Section | What it does |
|---|---|
| **Themes** | Active theme, a variable editor (Save & Apply), New Theme, full preview, and **Store in Fabric** (dataset `ui_themes`). |
| **Loader** | Loading animation type, speed, density and sprite sheet; Save & Apply everywhere. |
| **Builder** | Hand-author a panel (id, label, mode, HTML, JS) and save via `ui.panel.create`; optionally store to fabric (`ui_panels`). |
| **Generate** | Describe a panel or a theme; an LLM streams HTML or CSS variables (via `/llm/stream`), which are registered as a panel or created and applied as a theme. |
| **Panels** | Browse every registered panel, inspect HTML/JS, edit in the builder, delete dynamic panels. |

---

## 8. Events and storage

| Redis key | Holds |
|---|---|
| `vera:ui:theme` | Active theme id |
| `vera:ui:theme:<id>` | Custom theme JSON |
| `vera:ui:appearance`, `vera:ui:scale`, `vera:ui:loader` | Appearance, scale, loader |
| `vera:ui:panel:<id>` | Dynamic panel JSON |
| `vera:ui:acl:<scope>` | ACL overrides |

All are reloaded at startup. Events: `ui.theme.changed`, `ui.theme.deleted`, `ui.appearance.changed`, `ui.scale.changed`, `ui.loader.changed`, `ui.panel.created`, `ui.panel.updated`, `ui.panel.deleted`.

---

## 9. Worked examples

Create a custom dark theme and apply it:

```bash
curl -s -X POST "$VERA/ui/theme/create" -H 'Content-Type: application/json' -d '{
  "id": "forest", "label": "Forest", "type": "dark", "accent": "#5a9e6f",
  "vars": "{\"--bg0\": \"#0d140f\", \"--text\": \"#dfe8df\", \"--ac\": \"#5a9e6f\"}"}'
curl -s -X POST "$VERA/ui/theme/set" -H 'Content-Type: application/json' -d '{"theme": "forest"}'
```

Let an agent build and show a panel in one turn:

```json
{"name": "ui.panel.create", "arguments": {
  "id": "disk-usage", "label": "Disk usage", "mode": "tab",
  "html": "<div id='out'>loading…</div>",
  "js": "fetch('/mcp/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'sysinfo.disk',arguments:{}})}).then(r=>r.json()).then(j=>out.textContent=JSON.stringify(j.content,null,2));",
  "ui_caps": "sysinfo.disk"}}
{"name": "render.screen", "arguments": {"panel_id": "disk-usage", "session_id": "<chat session>"}}
```

Switch to the terminal style pack at zen density:

```json
{"name": "ui.appearance.set", "arguments": {"style": "terminal", "density": "zen"}}
```

---

## 10. Panel lifecycle, access and troubleshooting

UI Builder persists presentation metadata — panel identity, title, layout,
theme and capability lists — not a fork of the capability registry. At runtime
the harness loads registered panels and custom elements, and panel calls cross
the same backend bridge (`/mcp/call`) as the rest of Vera.

Keep panel IDs stable, because layouts and links refer to them. Treat capability
list changes as security-relevant documentation, not enforcement: hiding a
button is not authorization, so the server must still enforce policy. A panel
should render useful empty, loading, degraded and error states; an unavailable
optional backend must not leave an indefinite spinner.

| Symptom | Check |
|---|---|
| Theme change not visible | The panel does not load `/ui/vera-ui.js` or ignores `ui.theme.changed`; the per-device `localStorage` value may override. |
| Custom theme unreadable | `ensure_contrast` repairs only text/background and on-accent contrast; check other vars. |
| Dynamic panel missing after restart | Redis was unavailable when it was created (persistence is best-effort). |
| `ui.panel.delete` refused | The panel is built-in and has not been overridden. |
| Behaviour differs between calls | The duplicated `ui.*` caps in the chat module may have drifted ([§1](#1-source-map)). |

Debug in layers: confirm the panel appears in `ui.panel.list`, open the
standalone panel window, inspect browser console and network errors, then call
the backing capability directly.

---

## See also

- [Harness UI](./02-harness-ui.md) — panel registration, the theme broadcast, the iframe pattern
- [Agents & Chat](./19-agents-chat.md) — the chat module that also registers `ui.*`; `domain_caps`; the panel bridge
- [Flow Builder & UI Elements](./20-flow-builder.md) — reusable elements that drop into panels
- [Render](./28-render.md) — `render.screen` floats panels over chat
- [Capability Policy](./45-capability-policy.md) — runtime enforcement
- [PWA](./47-pwa.md) — the installable shell that hosts these panels

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
