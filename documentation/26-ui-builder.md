# 26 · UI Builder

> **Doc status:** concise reference for `ui builder/`. Expand as the surface grows.

`ui builder/ui_capabilities.py` is the UI **system** module: the shared theme engine, runtime panel CRUD (so an LLM can build UI), and capability access-control lists. These `ui.*` caps are also surfaced through the chat panels module (see [Agents & Chat §6](./19-agents-chat.md)).

---

## 1. Themes

A unified theme system — shared themes (`ash`, `dusk`, `void`, `chalk`, `ice`) plus custom themes, stored in Redis and broadcast to all UIs by event (the mechanism described in [Harness UI §6](./02-harness-ui.md)).

| Cap | Purpose |
|---|---|
| `ui.themes` | List themes with their CSS variables |
| `ui.theme.get` / `ui.theme.set` | Get / set the active theme (broadcasts) |
| `ui.theme.create` / `ui.theme.delete` | Custom theme CRUD |
| `ui.theme.css` | Theme as a servable stylesheet |

## 2. Panel CRUD — LLM-built UI

| Cap | Purpose |
|---|---|
| `ui.panel.list` | All registered panels + metadata |
| `ui.panel.get` | A panel's HTML/JS/metadata |
| `ui.panel.create` / `ui.panel.update` / `ui.panel.delete` | Dynamic panel CRUD — an LLM can author and revise panels at runtime |

## 3. Capability access control

Scoped allow/deny lists that bound which caps a given surface may use:

| Cap | Purpose |
|---|---|
| `ui.caps.acl` | Get/set the access-control lists |
| `ui.caps.scopes` | List scopes (`dag_builder`, `ui_builder`, `agent`, `general`) and their cap lists |
| `ui.caps.allowed` | The effective allowlist for a scope |

These scopes are the same allowlist mechanism agents use via `domain_caps` ([Agents & Chat](./19-agents-chat.md)) — a guardrail on what generated UI / planners / agents can invoke.

---

## See also

- [Harness UI](./02-harness-ui.md) — panel registration, the theme broadcast, the iframe pattern
- [Agents & Chat](./19-agents-chat.md) — the chat module that also surfaces `ui.*`; `domain_caps`
- [Flow Builder & UI Elements](./20-flow-builder.md) — reusable elements that drop into panels

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
