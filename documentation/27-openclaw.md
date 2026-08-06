# 27 · OpenClaw

> **Doc status:** concise reference for `openclaw/`. Optional, opt-in module. Expand as the surface grows.

`openclaw/openclaw_capabilities.py` bridges Vera with a running **OpenClaw** agentic-loop gateway. It's optional — load it alongside the orchestrator to wire the two together. Disabled by default (`OPENCLAW_ENABLED=0`).

The bridge runs both directions:

- **OpenClaw → Vera** — a lightweight `/openclaw/tools` + `/openclaw/call` REST surface is mounted so OpenClaw skills can call any Vera capability as an HTTP tool.
- **Vera → OpenClaw** — Vera connects to the OpenClaw WS gateway (protocol v3), creates/resumes sessions, streams agent turns, and surfaces results through Vera's event stream.

| Cap | Purpose |
|---|---|
| `openclaw.status` | Connection status + gateway info |
| `openclaw.connect` / `openclaw.disconnect` | (Re)connect / disconnect gracefully |
| `openclaw.prompt` | Send a prompt to OpenClaw, stream the response back |
| `openclaw.sessions.list` / `openclaw.sessions.reset` | List / clear OpenClaw sessions |
| `openclaw.config.get` / `openclaw.config.set` | Connection config |

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `OPENCLAW_ENABLED` | `0` | Opt-in switch |
| `OPENCLAW_WS_URL` | `ws://localhost:18789` | Gateway WebSocket |
| `OPENCLAW_TOKEN` | — | Shared secret / gateway password |
| `OPENCLAW_AGENT_ID` | `main` | Default agent to address |
| `OPENCLAW_VERA_BASE_URL` | `http://localhost:8000` | Vera's own URL for the tool bridge |

---

## See also

- [Agents & Chat](./19-agents-chat.md) — Vera's native agentic loop (the in-house counterpart)
- [Capability Framework §5](./01-capability-framework.md) — MCP proxying, the general pattern for bridging external tool servers

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
