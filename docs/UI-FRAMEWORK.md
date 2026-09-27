# The Vera UI framework — for capabilities and UI work

The redesigned UI (the LHM, the one top bar, the right-click menu, widgets, the one design) is not a
look to copy: it is a set of shared parts with contracts. Build on them and a new capability or panel
gets the whole system — docking, absorption, theming, text size, the menu, widgets — without code of
its own. This page says what each part is, where it lives, and what a capability or a panel does to
use it.

> Status: every part below is on `bleeding-edge-design`. "Planned" marks the parts that are not
> built yet; the master roadmap carries them (W4-11).

## 1. A page (panel) in the framework

A panel is an HTML page served by the backend and embedded in the harness as a tab. To be part of
the framework it loads, in this order:

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
| **Themes and style packs** | vera-ui.js paints `data-theme`, `data-style` on `<html>`; packs are tokens (`theme_defs.py STYLE_PACKS`) | Nothing, as long as the page uses the tokens. |
| **Blocks, density, text, contrast, motion** | `data-blocks`, `data-den` (full/hover/zen), `data-text`, `data-contrast` on `<html>` | Name surfaces by their role (`…sidebar`, `…rail`, `…-bar`, `…toolbar`, `…tabs`, `…card`, `…tile`, `…detail`, `…-panel`) — design.css turns them see-through with blocks off and raises cards/tiles with blocks on; popovers, menus, dialogs, tips, code and fields keep their ground. Small px font sizes are raised to the text-size floor by vera-ui.js. |
| **The LHM** | vera-panel.js publishes the sidebar's `.nav-btn` items (grouped by the `.nav-grp` headings before them, iconed by their `.gl` glyph) through the bridge; the harness docks them as a rail + list, as the chat's menu | Use the canonical sidebar: `<aside id="sidebar" data-vera-lhm><nav id="nav"><div class="nav-grp">Group</div><button class="nav-btn" data-section="x" title="Label"><span class="gl">▦</span>Label</button>…` |
| **Any menu docks** | A page's menu in markup of its own (an icon rail, a strip of view tabs) docks too; the bridge loads vera-panel.js for a page that lacks it, and folds the menu away while docked | Mark the menu `data-vera-lhm`; each item a button with a `title` (its label), a unique `id` or data attribute (its id) and optionally `data-icon`; put what is not navigation (a theme palette) under `data-lhm-skip`. A page whose layout reserves a column for the menu gives it back under `html.vpb-nav-hosted`. |
| **Widgets in the LHM** | A docked menu's ✎ adds any widget (the widget sheet), kept per panel and menu; a click on an item opens the item drawer (as on the dashboards and the canvas; from the chat's narrow menu frame it opens in the harness) | To give the menu widgets from the start, name templates on the sidebar: `data-lhm-widgets="cal:controls cal:month@m"` (the Calendar does). A page that frames panels (Comms) passes its shown child's up. |
| **Side by side** | Ctrl/⌘/middle-click or the hover ⧉ on any LHM option opens it beside what is open — a second instance when the panel is open already | Nothing. |
| **The one top bar** | The bridge finds the page's top bar (`[data-vera-topbar]`, else `#topbar`/`.topbar`/`.hdr`/… when it IS the top bar) and offers its buttons, selects, fields and toggles to the harness (`vera:hdr:offer` / `absorbed` / `act`) | Mark the bar `data-vera-topbar` (or `data-vera-topbar="keep"` to keep it). Keep controls as real `<button>`/`<select>`/`<input>`. |
| **The right-click menu** | The bridge loads `/ui/menus.js` + `/ui/rcm.js` | Mark things: `data-entity="host:ct126"` / `data-ref`, or `data-rcm-kind="…" data-rcm-name="…"`; for page-only actions `VeraRCM.attach({target(el), act(id, kind, name, el, x)})`. Thermal print, copy, open and ask are built in. |
| **Loading** | Parked until the tab is shown; spinners are drawn only when visible | Do not poll while `document.hidden`; never start a second read while one is pending. |

## 2. A capability in the framework

A capability's answer is drawn by **one mapping**, used by the chat's capability cards, the session
canvas and anything else that shows results:

- `VeraWidget.fromCapResult(capName, result, {args})` → widget records, best first (form, source,
  args, map, data, `why`). Forms for results: `kv`, `table`, `list`, `json`, `log`, `terminal`,
  `diff`, `code`, `progress`, `status`, `media`, `error`, `markdown`, series (`trace`/`area`/
  `column`), `files`, numbers (`hero`).
- `VeraWidget.fromCapStream(capName)` → a sink (`push(event)`, `end(result)`, `record()`,
  `subscribe(fn)`, `attach(el)`). The first events decide what the stream is: steps `{step|stage|phase,
  status|state, …}` become `progress` (updated in place by step id), chunks `{delta|token|chunk}` a growing
  `markdown`, samples `{t|ts, v|value}` a `trace`, anything else (lines `{text|msg|line}`, strings) a `log`.
- Python mirror: `vera/widgets/widget_cap_output.py`, capability `widget.from_result`
  (POST `/ui/widgets/from_result`).
- Draw a record anywhere with `<vera-widget record='…' size="s|m|l|xl">`; a tile's deep dive is
  `VeraWidget.dive(el)`.

To make a capability render well:

1. **Return a shape, not prose about a shape.** Rows as a list of objects; a series as
   `[{t, v}]`; steps as `[{name, state, ms}]`; a verdict as `{status, checks:[{name, ok, detail}]}`;
   a command as `{command, stdout, stderr, rc}`; a diff as unified text.
2. **Put the reading words in the name** (`.status`, `.list`, `.history`, `.summary`…) so it is a
   read, and use GET routes for reads — a dev sandbox reads those through from production
   (`sandbox_guard.READ_THROUGH_GROUPS`).
3. **Stream steps** as events a sink can take (`{step:'fetch', status:'running'}`, then
   `{step:'fetch', status:'done', ms:1200}`) rather than one final blob, when the work takes time.
4. *Planned (W4-11):* declare the view in the Capability Contract v2 `contract=` of `@capability`
   (for example `contract={"output": {"view": "progress", "map": {...}}}`) so the mapping reads the
   declaration instead of guessing from the answer.

## 3. Elements to reuse

| Element | Where | What it is |
| --- | --- | --- |
| `<vera-chat agent system session title>` | `/ui/chat.js` | The chat itself, embedded: its streaming, capability cards, canvas hand-off, design. `system` goes before the agent's own prompt. Use it for every chat in the product (the Agents panel's test chat does). |
| `<vera-widget record size>` | `/ui/widgets/widget_element.js` | Any widget record, live. |
| `<vera-canvas>` | `/ui/elements/canvas_element.js` | The session canvas. |
| `<vera-exploded>` | `/ui/exploded_element.js` | The exploded scene (`setScene({turns})`, `mode('cards'|'front'|'iso')`). |
| Vera graph | `/ui/vera-graph.js` (`veraUI.Graph.create`) | The graph, with sidebar panels (`registerPanel`) and display modes (`registerMode`): Graph, Exploded, Estate 3D, Estate 2D (`/ui/vera-graph-modes.js`). |
| `VeraISO` | `/ui/iso.js` | The one isometric projection (never copy the maths). |

## 4. Driving the UI (the aide and scripts)

Directives (`vera/ui/directives.py`, policy-gated, logged, undoable) include `panel.open {id, at,
section}` — `at: "harness"` opens it side by side in the harness (a second instance when open
already), `section` opens it at one of its menu items — `panel.dispatch`, `panel.query`,
`panel.close`, the `canvas.*` family and `lhm.focus`.

## 5. Everything is a panel or a widget

A full page (its own route, its own menu) is a **panel**: the includes of §1, the shell sidebar, a top bar, docked by the
harness. A panel that shows other panels in frames (Comms) lifts the shown one's menu into its own. Anything smaller
that shows data is a **widget**: a form in the registry, a record naming its source, drawn by `<vera-widget>` — on a
dashboard, the canvas, a docked menu or inside a panel. A panel's sidebar content that is not navigation (the
Calendar's layers, events, assistant) is on its way to being widgets so it can live in the LHM (W4-11).

## 6. Checklist

- New panel: shell markup, the four includes, tokens not hard-coded colours, `.card`-style
  surfaces, a `data-vera-topbar` bar with real controls, `data-entity` on things, no polling while
  hidden.
- New capability: a shape per §2, reading words for reads, steps as events.
- New visual part: a widget form (`widget_element.js` + `widget_record.FORMS`) or a registered mode
  or element — never a one-off drawing inside a page.
