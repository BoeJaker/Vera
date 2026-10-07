# The Vera UI framework — for capabilities and UI work

The Vera UI (the left-hand menu, the one top bar, the right-click menu,
widgets, the one design) is not a look to copy: it is a set of shared parts
with contracts. Build on them and a new capability or panel gets the whole
system — docking, absorption, theming, text size, the menu, widgets — without
code of its own. This page says what each part is, where it lives, and what a
capability or a panel does to use it.

The parts live in a handful of places: the shell is
`vera/capability_orchestration.html` (see
[Harness UI](../documentation/02-harness-ui.md)); the shared scripts are
`vera/vera-ui.js`, `vera/vera-panel.css`, `vera/vera-panel.js`,
`vera/chat/vera-panel-bridge.js` and `vera/chat/vera-lhm.js`; the design layer
and UI libraries are under `vera/ui/` (`design.css`, `menus.js`, `rcm.js`,
`iso.js`, `chat.js`, `routes.js`, `directives.py`, `scripts.py`,
`places_core.py`); widgets are under `vera/widgets/`; themes are defined in
`vera/theme_defs.py`.

**Status:** the parts in sections 1–5 are shipped and in use. Items marked
*Planned* are not built yet.

## Contents

- [1. A page (panel) in the framework](#1-a-page-panel-in-the-framework)
- [2. A capability in the framework](#2-a-capability-in-the-framework)
  - [Result forms and capability hints](#result-forms-and-capability-hints)
  - [Read states](#read-states)
- [3. Elements to reuse](#3-elements-to-reuse)
- [4. Driving the UI (the aide and scripts)](#4-driving-the-ui-the-aide-and-scripts)
  - [Directive vocabulary](#directive-vocabulary)
  - [Policy, log and undo](#policy-log-and-undo)
  - [UI scripts](#ui-scripts)
- [5. Everything is a panel or a widget](#5-everything-is-a-panel-or-a-widget)
- [6. Widget capabilities and routes](#6-widget-capabilities-and-routes)
- [7. Checklist](#7-checklist)
- [Related](#related)

## 1. A page (panel) in the framework

A panel is an HTML page served by the backend and embedded in the harness as a
tab (registered with `register_ui`, see
[Harness UI §2](../documentation/02-harness-ui.md#2-panel-registration)). To be
part of the framework it loads, in this order:

```html
<link rel="stylesheet" href="/ui/vera-panel.css">   <!-- the shell: sidebar (#sidebar[data-vera-lhm]), controls -->
<script src="/ui/vera-ui.js"></script>              <!-- theme, style pack, blocks, density, text, contrast, zoom;
                                                         loads /ui/themes.css and /ui/design.css -->
<script src="/ui/vera-panel.js"></script>           <!-- the shell's behaviour: collapse, sections, the nav bridge -->
<script src="/ui/vera-panel-bridge.js"></script>    <!-- the harness bridge: state, nav, top bar, the RCM -->
```

What the page then gets for free:

| Part | How it reaches the page | What the page does |
| --- | --- | --- |
| **The one design** (`vera/ui/design.css`) | vera-ui.js adds it | Use the shared tokens (`--bg0..3`, `--text`, `--dim`, `--acc`, `--font-ui`, `--mono`, `--ui-radius`); mark surfaces `.card`/`.tile`/`.box`/`.twrap` so blocks on/off applies. Change the look of every panel in design.css, not per panel. |
| **Themes and style packs** | vera-ui.js paints `data-theme`, `data-style` on `<html>`; packs are tokens (`theme_defs.py STYLE_PACKS`: `standard`, `newspaper`, `terminal`, `pixel`) | Nothing, as long as the page uses the tokens. |
| **Blocks, density, text, contrast, motion** | `data-blocks`, `data-den` (`full`/`hover`/`zen`), `data-text`, `data-contrast` on `<html>` | Name surfaces by their role (`…sidebar`, `…rail`, `…-bar`, `…toolbar`, `…tabs`, `…card`, `…tile`, `…detail`, `…-panel`) — design.css turns them see-through with blocks off and raises cards/tiles with blocks on; popovers, menus, dialogs, tips, code and fields keep their ground. Small px font sizes are raised to the text-size floor by vera-ui.js. |
| **The LHM** | vera-panel.js publishes the sidebar's `.nav-btn` items (grouped by the `.nav-grp` headings before them, iconed by their `.gl` glyph) through the bridge; the harness docks them as a rail + list, as the chat's menu | Use the canonical sidebar: `<aside id="sidebar" data-vera-lhm><nav id="nav"><div class="nav-grp">Group</div><button class="nav-btn" data-section="x" title="Label"><span class="gl">▦</span>Label</button>…`. A panel whose switcher is not one of those can call `VeraPanelBridge.registerNav([{id, label}, …])`. |
| **Any menu docks** | A page's menu in markup of its own (an icon rail, a strip of view tabs) docks too; the bridge loads vera-panel.js for a page that lacks it, and folds the menu away while docked | Mark the menu `data-vera-lhm`; each item a button or a `.tab` with a `title` or `data-label` (its label — `data-label` wins when the title is a description), a unique `id` or data attribute (its id) and optionally `data-icon`; put what is not navigation (a theme palette) under `data-lhm-skip`. A page whose layout reserves a column for the menu gives it back under `html.vpb-nav-hosted`. |
| **A dashboard in any page** | `<vera-dashboard layout="key">` (`/ui/vera-dashboard.js`) draws the layout file `vera/widgets/layouts/<key>.json` as widget tiles with the whole dashboard kit: Configure (move, resize, hide), + Widget (the widget sheet), Layouts, Arrange, the item drawer; its grid styles come with it | Write the layout file (records: form · source · read.map · span), drop the tag in. `storage` names the saved arrangement, `no-bar` hides the toolbar, `.reload()` re-reads every tile. Stack Monitor is built this way (`sysmon`). An empty pane ("select a … to …") is a glance, not a blank: the Fabric panel shows `fabric-glance` until a dataset is chosen, Cap Hub `caps-glance` until a capability is, Mesh `mesh-glance` until a node is. |
| **Widgets in the LHM** | A docked menu's ✎ adds any widget (the widget sheet), kept per panel and menu; a click on an item opens the item drawer (as on the dashboards and the canvas; from the chat's narrow menu frame it opens in the harness) | To give the menu widgets from the start, name templates on the sidebar: `data-lhm-widgets="cal:controls cal:month@m"` (the Calendar does). A page that frames panels (Comms) passes its shown child's up. |
| **Side by side** | Ctrl/⌘/middle-click or the hover ⧉ on any LHM option opens it beside what is open — a second instance when the panel is open already | Nothing. |
| **The one top bar** | The bridge finds the page's top bar (`[data-vera-topbar]`, else `#topbar`/`.topbar`/`.hdr`/… when it is the top bar) and offers its buttons, selects, fields and toggles to the harness (`vera:hdr:offer` / `vera:hdr:absorbed` / `vera:hdr:act`) | Mark the bar `data-vera-topbar` (or `data-vera-topbar="keep"` to keep it). Keep controls as real `<button>`/`<select>`/`<input>`. |
| **The right-click menu** | The bridge loads `/ui/menus.js` + `/ui/rcm.js` | Mark things: `data-entity="host:ct126"` / `data-ref`, or `data-rcm-kind="…" data-rcm-name="…"`; for page-only actions `VeraRCM.attach({target(el), act(id, kind, name, el, x)})`. Thermal print, copy, open and ask are built in. |
| **State and actions for the assistant** | The bridge publishes a debounced state snapshot (`vera:panel:state`) and runs action handlers dispatched by `panel.dispatch` | `VeraPanelBridge.registerStateProvider(() => ({…}))`, `VeraPanelBridge.registerActionHandler('name', payload => result)` (may return a Promise), `registerCapActivityHandler(pattern, fn)` for live capability activity. |
| **Loading** | Iframes are parked until the tab is shown; spinners are drawn only when visible | Do not poll while `document.hidden`; never start a second read while one is pending. |

## 2. A capability in the framework

A capability's answer is drawn by **one mapping**, used by the chat's
capability cards, the session canvas and anything else that shows results:

- `VeraWidget.fromCapResult(capName, result, {args})` → widget records, best
  first (form, source, args, map, data, `why`); `[]` for nothing to draw.
- `VeraWidget.fromCapStream(capName)` → a sink (`push(event)`, `end(result)`,
  `record()`, `subscribe(fn)`, `attach(el)`). The first events decide what the
  stream is: steps `{step|stage|phase, status|state, …}` become `progress`
  (updated in place by step id), chunks `{delta|token|chunk}` a growing
  `markdown`, samples `{t|ts, v|value}` a `trace`, anything else (lines
  `{text|msg|line}`, strings) a `log`. `end(result)` draws the final answer
  through `fromCapResult` beside it.
- Python mirror: `vera/widgets/widget_cap_output.py`
  (`from_cap_result(cap, result, args=None, title='', size='l', max_records=3)`),
  exposed as the capability `widget.from_result` (`POST /ui/widgets/from_result`).
  It is pure and applies the same rules in the same order as the browser.
- Draw a record anywhere with `<vera-widget record='…' size="s|m|l|xl">`; a
  tile's deep dive is `VeraWidget.dive(el)`.

To make a capability render well:

1. **Return a shape, not prose about a shape.** Rows as a list of objects; a
   series as `[{t, v}]`; steps as `[{name, state, ms}]`; a verdict as
   `{status, checks:[{name, ok, detail}]}`; a command as
   `{command, stdout, stderr, rc}`; a diff as unified text.
2. **Put the reading words in the name** (`.status`, `.list`, `.history`,
   `.summary`…) so it is a read, and use GET routes for reads — a dev sandbox
   reads those through from production for the groups listed in
   `sandbox_guard.READ_THROUGH_GROUPS`.
3. **Stream steps** as events a sink can take
   (`{step:'fetch', status:'running'}`, then
   `{step:'fetch', status:'done', ms:1200}`) rather than one final blob, when
   the work takes time.
4. *Planned:* declare the view in the Capability Contract v2 `contract=` of
   `@capability` (for example `contract={"output": {"view": "progress", "map": {...}}}`)
   so the mapping reads the declaration instead of guessing from the answer.

### Result forms and capability hints

The forms `fromCapResult` picks from (`widget_cap_output.CAP_FORMS`) are `kv`,
`table`, `list`, `json`, `log`, `terminal`, `diff`, `code`, `progress`, `hero`,
`trace`, `area`, `column`, `status`, `files`, `media`, `error`, `markdown` and
`reading`; every one is a form in the widget registry
(`widget_record.FORMS`, which also holds the gallery forms such as `counter`,
`meter`, `gauge`, `radial`, `dial`, `tank`). The rules are tried in order:
error · terminal · media · diff · code · the capability's own hint · progress ·
events · series · files · level · prose · status · table · list · numbers ·
record · json · text. A capability's hint (`CAP_HINTS`, kept in step with the
element) pins the form and map for known reads — for example `sysmon.history`
→ `trace`, `obs.events` → `log`, `ollama.request_log` → `log`.

### Read states

For read-only panel data, `window.veraUI.readState({loading, error, hasData,
label, retryable})` returns a frozen `{kind, label, retryable}` with `kind` one
of `loading`, `ready`, `empty`, `error`, and
`veraUI.renderReadState(target, state, {onRetry, retryLabel})` draws it with a
`status`/`alert` role and an optional retry button. They keep loading, empty
and error visibly distinct; they do not own fetching or caching.

## 3. Elements to reuse

| Element | Where | What it is |
| --- | --- | --- |
| `<vera-chat agent system session title>` | `/ui/chat.js` | The chat itself, embedded: its streaming, capability cards, canvas hand-off, design. `system` goes before the agent's own prompt. Use it for every chat in the product (the Agents panel's test chat does). |
| `<vera-widget record size>` | `/ui/widgets/widget_element.js` | Any widget record, live. |
| `<vera-dashboard layout>` | `/ui/vera-dashboard.js` | A widget dashboard over a layout file in `vera/widgets/layouts/`. |
| `<vera-canvas>` | `/ui/elements/canvas_element.js` | The session canvas. |
| `<vera-exploded>` | `/ui/exploded_element.js` | The exploded scene (`setScene({turns})`, `mode('cards'\|'front'\|'iso')`). |
| `<vera-structgraph>` | `/ui/structgraph.js` | Structured graph rendering. |
| `<vera-markdown>`, `<vera-research-card>`, `<vera-panel-copilot>` | `/ui/elements/vera_markdown.js`, `/ui/elements/research_card.js`, `/ui/elements/panel_copilot.js` | Markdown rendering; the research progress/report card; a scoped copilot bound to a panel's specialist. |
| `<vera-sparkline>`, `<vera-loop-graph>`, `<vera-topology-map>`, `<vera-chat-data>`, `<vera-flow-builder>` | `/ui/elements/*.js` | Sparkline, agent-loop graph, topology map, chat-with-data widget, flow builder. |
| Vera graph | `/ui/vera-graph.js` (`veraUI.Graph.create`) | The graph, with sidebar panels (`registerPanel`) and display modes (`registerMode`): Graph, Exploded, Estate 3D, Estate 2D (`/ui/vera-graph-modes.js`). |
| `VeraISO` | `/ui/iso.js` | The one isometric projection (never copy the maths). |
| `window.veraSelect` | `/ui/vera-select.js` | `data-select="caps\|models\|tags\|static…"` turns an `<input>` into a searchable single/multi select (`veraSelect.enhance`, `scan`). |
| `VeraLoader` | `/ui/vera-loader.js` | The configurable loading animation on `.vera-loading` overlays. |

## 4. Driving the UI (the aide and scripts)

The assistant drives panels, widgets, the chat and the canvas through **one
vocabulary of directives and one dispatcher** (`vera/ui/directives.py`), by two
paths: *direct* — the model issues a directive as a capability call
(`ui.directive`, in its tool loop or inline in a reply) — and *scripted*
(`vera/ui/scripts.py`) — deterministic rules run the same directives on events
with no model call. Policy, the log, the ribbon and undo apply to both, and the
model sees the room it changed on its next turn (`ui.room`).

### Directive vocabulary

| Directive | Args | Undo |
|---|---|---|
| `panel.open` | `{id, at, section}` — `at: "harness"` opens it side by side in the harness (a second instance when open already); `section` opens it at one of its menu items | close |
| `panel.dispatch` | `{id, action, args}` — drive a panel through the bridge | — |
| `panel.query` | `{id, what}` — read a panel's state, nav or actions | — |
| `panel.close` | `{id}` | reopen |
| `canvas.show` / `canvas.add` / `canvas.pin` / `canvas.park` / `canvas.size` / `canvas.ask` | `{key}`, `{kind, ref, at}`, `{key}`, `{key}`, `{key, size}`, `{q, choices}` | —, remove, park, pin, restore, dismiss |
| `lhm.focus` / `lhm.compose` | `{menu}`, `{menu, widgets}` | —, restore |
| `widget.place` / `widget.update` / `widget.template.save` | `{template, into, at}`, `{id, config}`, `{from}` | remove, restore, delete |
| `chat.card` / `chat.mode` | `{kind, payload}`, `{mode, on}` | —, off |
| `graph.focus` / `graph.view` | `{turn\|step\|record}`, `{view}` (`galaxy`, `iso`, `flow`, `time`) | — |
| `ui.script.run` / `ui.script.save` | `{name, args}`, `{script}` | the run, delete |

`GET /ui/directive/vocab` (`ui.directive.vocab`) returns the vocabulary.

### Policy, log and undo

Each directive is checked against a policy target (`drive`, `ask` or `never`;
for scripts `run`, `ask` or `off`). Defaults: `ops:*` drive, `notebook:*` ask,
`settings:*` never, `panels`/`canvas`/`widgets` drive, scripts run, and
`chat.mode`, `lhm.compose` and `ui.script.save` ask. Every directive writes a
log row (who, path, outcome, note, the undo) and an event
(`ui.directive.applied`, `refused` or `asked`). The harness shows a **Driven**
ribbon over a tab a directive opened, with Undo and the log.

| Capability | Route |
|---|---|
| `ui.directive` | `POST /ui/directive` |
| `ui.directive.vocab`, `ui.directive.log` | `GET /ui/directive/vocab`, `GET /ui/directive/log` |
| `ui.directive.answer`, `ui.directive.undo` | `POST /ui/directive/answer`, `POST /ui/directive/undo` |
| `ui.policy.get`, `ui.policy.set` | `GET /ui/policy`, `POST /ui/policy/set` |
| `ui.event` | `POST /ui/event` — the UI's own reports (panel opened, loop step waiting, widget value, …) |
| `ui.room` | `GET /ui/room` — the manifest of what is open |

Redis keys: `vera:ui:directive:log:<sid>`, `vera:ui:directive:ask:<sid>`,
`vera:ui:policy:<sid>` (and `:global`), `vera:ui:policy:allow:<sid>`. The
Driven panel itself is served at `GET /ui/driven`.

### UI scripts

A script is a deterministic, versioned record —
`on <event> [match {k: v}] if <expression> do [directives] else [directives]` —
run by the same dispatcher with no model call, marked `script · name` in the
log and on the ribbon. Triggers are events on `vera:events` (`cap.ok`,
`cap.error`, the UI's own reports, `ui.directive.refused`) and a daily
`HH:MM` timer. The expression language is a restricted Python subset
(comparisons, boolean and arithmetic operators, `event`, `room`, `value`,
`result`, `args`, attribute and item access, literals, `open(panel_id)` and
`has(x)`). Scripts are stored at `vera:ui:script:<name>`; shipped scripts are
built in and can be shadowed. A runner ticks every 2 s in one instance.

| Capability | Route |
|---|---|
| `ui.script.list`, `ui.script.get` | `GET /ui/scripts`, `GET /ui/script` |
| `ui.script.save`, `ui.script.enable`, `ui.script.run`, `ui.script.delete` | `POST /ui/scripts/save`, `/ui/scripts/enable`, `/ui/scripts/run`, `/ui/scripts/delete` |

## 5. Everything is a panel or a widget

A full page (its own route, its own menu) is a **panel**: the includes of §1,
the shell sidebar, a top bar, docked by the harness. A panel that shows other
panels in frames (Comms) lifts the shown one's menu into its own. Anything
smaller that shows data is a **widget**: a form in the registry, a record
naming its source, drawn by `<vera-widget>` — on a dashboard, the canvas, a
docked menu or inside a panel. A panel's sidebar content that is not
navigation (the Calendar's layers, events, assistant) is moving towards being
widgets so it can live in the LHM.

## 6. Widget capabilities and routes

| Capability | Route | Purpose |
|---|---|---|
| `widget.forms` | `GET /ui/widgets/forms` | The form registry |
| `widget.sources` | `GET /ui/widgets/sources` | Capabilities and streams a widget can read |
| `widget.validate` | `POST /ui/widgets/validate` | Validate a record |
| `widget.render_spec` | `POST /ui/widgets/render_spec` | Resolve a record into a render spec |
| `widget.from_result` | `POST /ui/widgets/from_result` | A capability answer → widget records |
| `widget.read` | `POST /ui/widgets/read` | Read a record's source and map it |
| `widget.layouts`, `widget.layout.migrate` | `GET /ui/widgets/layouts`, `POST /ui/widgets/layouts/migrate` | Dashboard layout files |
| `widget.template.list/get/save/delete` | `GET /ui/widgets/templates`, `GET /ui/widgets/template`, `POST /ui/widgets/templates/save`, `POST /ui/widgets/templates/delete` | Widget templates (`vera:ui:widget:tpl:*`) |
| `widget.template.instantiate` | `POST /ui/widgets/instantiate` | Place a template |
| `widget.instance.list/remove` | `GET /ui/widgets/instances`, `POST /ui/widgets/instances/remove` | Placed instances (`vera:ui:widget:inst:*`) |
| `redis.info`, `redis.keys`, `redis.get`, `redis.stream.tail` | `GET /ui/widgets/redis/{info,keys,get,stream}` | Redis as a widget source |

The **Widgets** (`widget-registry`) and **Widget gallery** (`widget-gallery`)
panels are served from `GET /ui/widgets/registry` and `GET /ui/widgets/gallery`.

## 7. Checklist

- New panel: shell markup, the four includes, tokens not hard-coded colours,
  `.card`-style surfaces, a `data-vera-topbar` bar with real controls,
  `data-entity` on things, no polling while hidden.
- New capability: a shape per §2, reading words for reads, steps as events.
- New visual part: a widget form (`widget_element.js` + `widget_record.FORMS`)
  or a registered mode or element — never a one-off drawing inside a page.

## Related

- [Harness UI](../documentation/02-harness-ui.md) — the shell, panel registration, themes
- [Capability Framework](../documentation/01-capability-framework.md) — how capabilities register and are called
- [UI Builder](../documentation/26-ui-builder.md) — themes, user-built panels, the loading animation
- [PWA](../documentation/47-pwa.md) — the installable shell
