---
name: vera-design
description: Build or change any Vera UI - a panel, a widget, a graph, a menu (LHM), a settings page, the appearance (theme, blocks on/off, Full/Hover/Zen, glow) - so it matches every other page and follows the theme. Start from the UI bootstrap (ui.templates, /ui/bootstrap.css), keep the menu/settings/appearance contracts, verify live with Playwright. Use whenever touching vera/**/*.html, panel JS/CSS, widgets or graphs.
---

# Vera design - one look, one set of parts

The reference is `documentation/51-ui-design-system.md` (the layers, the token table, the attribute contracts). This
skill is the working checklist. Land every change through the pipeline in `improve-vera-sandboxed`; this skill only
says what a UI change must look like and how to prove it.

## 1. Start from the bootstrap, never from a blank page

- `ui.templates` (GET `/ui/templates`) lists the starting files: `panel.html`, `widget.json`, `widget.html`,
  `graph.html` (`/ui/templates/<name>`, source `vera/ui/templates/`). Copy one; replace every `EXAMPLE`.
- Page layout comes from `/ui/bootstrap.css` (`.vb-root`, `.vb-topbar`, `.vb-section`, `.card`, `.btn`, `.badge`,
  `.kv`, `table.vb-table`). The menu comes from `vera-panel.css`. Write page CSS only for what is truly the panel's own.
- Script order at the end of `<body>`: `vera-ui.js`, `vera-panel.js`, `vera-panel-bridge.js`, `vera-settings.js`, then
  `widget_element.js` / `vera-graph.js` if the page uses them. Start the page's own work on `DOMContentLoaded`, so
  `veraUI` exists.

## 2. The rules a page keeps

1. **Colour only from the theme's names, each with a fallback.** `var(--bg1, #16181d)`, never a bare hex or rgba.
   Tints are `color-mix(in srgb, var(--acc) 12%, transparent)`. Canvas, chart or script-built SVG colours come from
   `veraUI.color('--acc', .3)`. Category colours for data use `--dv1 … --dv7`.
2. **One menu.** The canonical `<aside id="sidebar" data-vera-lhm>` with `button.nav-btn` items. Each item has
   `data-label`, `data-desc` and `data-desc-long`, plus an icon (`data-icon`, `.gl` or `<svg>`) - never a letter. Group
   with `.nav-grp` (`data-icon`, `data-desc`).
3. **The top bar is status and settings.** Anything that replaces the page's content is a menu item, or a
   `data-vera-sub` sub-view mirrored under the lit item.
4. **The menu ends like the chat's menus:**
   - the panel's one primary action is marked `data-lhm-cta` (`""` for every menu, `"<item id>"` for one item's);
   - default widgets per item go in `data-lhm-widgets="template@size"`.
   Mark the action that is the panel's reason to exist: New…, Run…, Build…. A Refresh or a Save is never the primary
   action.
5. **Settings go in a section on the shared Settings page.** Mark the opener `data-vera-settings="#section"`. A panel
   that builds its settings on demand calls `VeraSettings.openSection(el, {label, onClose})`. Never copy fields;
   never use `prompt()`.
6. **Appearance is handled by the shared parts.**
   - Do not set `backdrop-filter` or opaque grounds on overlays by hand.
   - Mark content that must keep its own ground (paper, previews) `data-vera-keep-ground`.
   - A floating menu is found by its position. Give it a z-index.
7. **Widgets are `<vera-widget>`**, and graphs are `veraUI.Graph.create` or `<vera-graph-embed>`. Never a third renderer,
   never a hand-drawn widget.
8. **In generated code, block comments only.** A `//` appended to a generated or joined line swallows the code after it.
   It hid the menu groups' descriptions until 2026-10-04.

## 3. Verify live - the evidence, not the source

- **Static checks first.**
  - Run the node tests named in doc 51 §Checks.
  - Parse every edited page's inline `<script>`s: the gate does not check panel JS.
- **Then Playwright** on the host venv (`/home/boejaker/langchain/bin/python3`) against prod (`https://localhost:8999`),
  with `context.route` serving your worktree's files in place of prod's:
  - Wrap each route handler in a factory (`def mk(rel): async def h(route): …; return h`).
  - Read files with `encoding='utf-8'`.
  - HTML: `route.fetch()` then `fulfill(response=orig, body=…)`. JS/CSS: `fulfill(status=200, content_type=…, body=…)`.
- **Harness probes.**
  - Wait for `document.querySelectorAll('[data-tab]').length > 20` before `switchTab('auto-<panel-id>')`. The harness
    builds its tabs slowly, and a probe that does not wait fails as if your change broke it.
  - Put a warm-up page first: the first tab in a run comes back empty.
- **Never press a real primary action in a probe.** Swap its handler for a flag first: Run starts GPU work, and a census
  may be running.
- **Measure, and read screenshots.** Use computed colours, `elementsFromPoint`, item positions and counts, then look at
  the screenshot. A headless browser starts with no localStorage, so it shows the server-side appearance and theme.
- **Appearance across all panels:** sweep every registered panel's page (the iframe `src` in `/ui/panels`) with
  `data-blocks=off`. Count large opaque grounds, check the body's glow image, and check the menu's ground.

## 4. Anti-patterns found in the estate (do not add more)

- A page palette in `:root` with literal colours and literal rgba copies of them (ML Lab; fixed 2026-10-04).
- A custom theme saved with its vars nested (`{"theme": {...}}`); `flat_theme_vars` unwraps it now.
- A blocks-off rule that gives something a ground: blocks off only takes grounds away.
- An icon-only rail whose names live only in tooltips (Research's; fixed 2026-10-04).
- A tab strip used as the menu: draw it as a column, or `data-vp-strip-keep` when it truly must stay a strip.
- A menu's state published twice from one frame, one copy without `nav`: the harness reads "no menu".
