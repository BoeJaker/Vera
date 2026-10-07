# 51 — UI design system and bootstrap

How a Vera page looks and behaves, and the files you start one from. Every panel, widget and graph is built from the
same layers, so a change to the look is made once and reaches everything. The vera-design skill
(`.claude/skills/vera-design/SKILL.md`) is the working checklist for this document.

## The layers

| Layer | File | Served at | What it gives |
|---|---|---|---|
| Theme | `vera/theme_defs.py`, custom themes in Redis | `/ui/theme`, `/ui/themes` | The colours. `vera-ui.js` writes them on `<html>` under every name the panels use. |
| Style pack | `theme_defs.STYLE_PACKS` | `[data-style]` | Form: fonts (`--f-ui`, `--f-mono`), radii (`--ui-radius`, `--r-sm`), elevation (`--elev`). Standard, Newspaper, Terminal, Pixel. |
| Appearance runtime | `vera/vera-ui.js` | `/ui/vera-ui.js` | Paints theme, style, `data-den` (Full · Hover · Zen), `data-blocks` (on/off), text size and contrast; relays them between frames; the standalone appearance button; `veraUI.color()`, `veraUI.readState()` / `renderReadState()`. Loads `design.css`. |
| The look | `vera/ui/design.css` | `/ui/design.css` | The glow, blocks on/off, the density tiers, scrollbars, focus, selection - on every page that does not draw its own design (`data-design-own`: the chat, the harness). |
| The page | `vera/ui/bootstrap.css` | `/ui/bootstrap.css` | Grid, top bar, sections, cards, buttons, badges, key/value rows, tables (`.vb-*`, `.card`, `.btn`, `.badge`, `.kv`). |
| The menu | `vera/vera-panel.css`, `vera/vera-panel.js` | `/ui/vera-panel.css`, `/ui/vera-panel.js` | The canonical sidebar, its fold, its sub-sections, its widgets and primary action; a tab-strip menu drawn as the menu column when the panel stands alone. |
| The bridge | `vera/chat/vera-panel-bridge.js` | `/ui/vera-panel-bridge.js` | Publishes the menu to the harness (drawn in its LHM when docked) and to agents (`panel.query`, `panel.dispatch`). |
| Settings | `vera/chat/vera-settings.js` | `/ui/vera-settings.js` | The chat's Settings page over any panel's settings section. |
| Widgets | `vera/widgets/widget_element.js` | `/ui/widgets/widget_element.js` | `<vera-widget>`, the widget sheet (`VeraWidgetConfig`). |
| Graphs | `vera/vera_graph.js`, `vera/graph_embed_element.js` | `/ui/vera-graph.js`, `/ui/vera-graph-embed.js` | `veraUI.Graph.create()`, `<vera-graph-embed>`. |

## Starting something: the templates

`ui.templates` (GET `/ui/templates`) lists the starting files; each is served at `/ui/templates/<name>` and lives in
`vera/ui/templates/`.

| Template | Start a… |
|---|---|
| `panel.html` | panel: menu, top bar, sections, cards, settings section, primary action, the shared scripts in order |
| `widget.json` | widget record for the registry (`widget.template.save`) - validates clean with `widget.validate {record}` |
| `widget.html` | page placing widgets: by template id, by inline record, a panel as a widget |
| `graph.html` | page with the full graph and the bare inline one |

A panel is registered by its capability module with `register_ui(panel_id=…, label=…, icon=…, html='<iframe
src="/your/panel" …></iframe>', ui_caps=[…])` and an `@APP.get("/your/panel")` route that returns the page.

## Colour

Only the theme's names, each with a fallback: `var(--bg1, #16181d)`. A tint is
`color-mix(in srgb, var(--acc) 12%, transparent)`. Never a bare hex or rgba in a page's rules: a page that hard-codes its
palette never follows a theme (ML Lab did until 2026-10-04).

| Role | Panel names (set by `vera-ui.js`) | Design names |
|---|---|---|
| page ground | `--bg0` | `--bg` |
| surfaces | `--bg1`, `--bg2`, `--bg3`, `--bg4` | `--s1`, `--s2`, `--s3`; derived `--surf`, `--surf2`, `--surf3` |
| text | `--text`, `--dim`, `--dim2` | `--t1`, `--t2`, `--t3` |
| accent | `--acc`, `--acc2`, `--acc3`, `--on-acc` | `--ac` … `--ac5`, `--on-ac` |
| state | `--ok`, `--warn`, `--err`, `--red`, `--green`, `--yellow`, `--purple` | |
| lines | `--border`, `--border2` | `--bd`, `--bd2` |
| data | `--dv1` … `--dv7` (one categorical order for every chart) | |

A canvas, a chart library or SVG built in script cannot read `var()`: use `veraUI.color('--acc')` or
`veraUI.color('--acc', .3)`. It resolves the theme's colour (fallbacks and `color-mix` work) and caches it until the
theme, style or appearance changes.

## The menu

The canonical shape is `<aside id="sidebar" data-vera-lhm>` holding `#side-head` and `<nav id="nav">` of
`button.nav-btn` items. Everything the harness, agents and the shared parts know about a menu is read from attributes:

| Attribute | On | Meaning |
|---|---|---|
| `data-vera-lhm` | the menu | this is the panel's menu |
| `data-section` (or `data-sec`, `data-tab`, …) | an item | its id - the first attribute every item carries with a different value |
| `data-label` | an item | its name (else its title, else its text) |
| `data-desc` | an item | one line for the pointer |
| `data-desc-long` | an item | a paragraph for agents (`panel.query` returns it) |
| `data-icon` or a `.gl` / `<svg>` child | an item | its icon. Never a letter: an item with none takes `VeraLHM.glyphFor(label)` |
| `.nav-grp` with `data-icon`, `data-desc` | a heading | a group: one rail icon per group when docked |
| `data-lhm-widgets="id@size …"` | an item | the widgets that item's menu shows by default |
| `data-lhm-cta` (`""` or `"<item id>"`) | any button | the menu's primary action, for every menu or one item's |
| `data-vera-settings="<selector>"` | an item | opens the shared Settings page over that section |
| `data-vera-sub="<id>"` | a tab inside the page | a sub-view, mirrored under the lit item |
| `data-vp-strip-keep` | the menu | keep a tab-strip menu as a strip when standalone |
| `data-lhm-skip` | a part of the menu | not an item (a theme palette, a fold button) |

The top bar carries the page's name, its status and its settings. It never switches the page: a part that changes the
page's content is a menu item or a `data-vera-sub` sub-view.

Viewers can add widgets to any menu with its ✎. Those lists are kept by `ui.lhm.widgets` / `ui.lhm.widgets.set`, per
panel and per menu (`i:<item id>` or `g:<group name>`). The docked menu and the standalone one share them.

## Appearance on a panel

`design.css` applies the appearance by role, and `vera-ui.js` adds what names cannot say:

- **Blocks off** strips grounds: cards, tiles, bars, side columns. Anything drawn fixed or absolute over the page with a
  z-index is marked `data-vera-float` and keeps its ground (menus, sheets, popovers, dialogs). A large opaque part in the
  page's flow is measured and marked `data-vera-ground`; with blocks off it loses its ground. Content keeps its own:
  paper, previews, canvases, anything marked `data-vera-keep-ground`.
- **Hover** hides a bar's buttons and a row's actions until pointed at. **Zen** also hides hints
  (`.hint`, `.help`, `.subtitle`, …). A selected tab and anything inside an open menu always show.
- **The glow** is on the body's own background, over whatever colour the page paints.

## Page height

A page's root is `height: 100%` on `html`, `body` and the layout root, never `100vh`. With the UI zoom on, `vera-ui.js`
zooms `<html>` and sizes it to fit the window, so a `100vh` root runs past the bottom of the window.

## Settings

Give the panel a settings section: a heading, a line, then one block per group of fields. Mark the item or button that
opens it `data-vera-settings="#that-section"`. The shared page lists each block and moves the real fields in, and puts
them back on close. A panel that builds its settings on demand hands the element over with
`VeraSettings.openSection(el, {label, onClose})`.

## Widgets

A widget is one element, `<vera-widget>`. It draws a form from the catalogue (`/ui/widgets/forms`) from a reading (a
capability, `/ui/widgets/sources`). Give it a saved template (`template-id`), an inline `record`, or a panel
(`template-id="panel:<id>"`). Save a template with `widget.template.save`, place one with
`widget.template.instantiate {id, where}`. Never draw a widget by hand.

## Graphs

One renderer. The full graph: `veraUI.Graph.create(host, {height:'fill', …}).load({nodes:[{id,label,type}],
edges:[{from,to,rel}]})`. The bare graph: `<vera-graph-embed layer="entity" limit="60" height="240">` (and
`renderer="struct"` for one record as a structured diagram). Both follow the theme and blocks off.

## Checks

The node tests guard these contracts (`node tests/<name>.cjs`):

- `test_ui_bootstrap.cjs`: the templates, the stylesheet, `veraUI.color`.
- `test_appearance_everywhere.cjs`: blocks / floats / grounds, density, glow, standalone appearance.
- `test_lhm_shared_settings.cjs`: the Settings page.
- `test_lhm_chat_anatomy.cjs`: strip menus, primary actions, per-item widgets.
- `test_lhm_menu_widgets_store.cjs`: menu widgets on the server.
- `test_lhm_menu_descriptions.cjs`: every menu item described.

Inline `<script>` blocks in panel HTML are not checked by the gate: parse them yourself before landing.
