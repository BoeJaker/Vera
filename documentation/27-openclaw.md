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
| `OPENCLAW_CLIENT_ID` | `cli` | Gateway client id — a **closed enum** (see below) |
| `OPENCLAW_CLIENT_MODE` | `cli` | Gateway client mode — a **closed enum**; `operator` is a *role*, not a mode |

---

## The connect handshake

The gateway validates `connect.params` against its published JSON schema
(`@openclaw/gateway-protocol`) and refuses anything else outright:

- **`client.id` and `client.mode` are closed enums.** An invented id (Vera used
  to send `vera-bridge`) or a role in the mode slot (`operator`) fails with
  *"must be equal to one of the allowed values"*. Vera presents as the generic
  `cli`/`cli` operator client; `openclaw.config.set` rejects a value outside the
  enum instead of letting every reconnect fail.
- **A real device proof is required.** Vera keeps one **Ed25519 identity**
  (`<state dir>/openclaw/device-identity.json`, private key sealed with
  `security/secrets.py`), derives `device.id` as `sha256(raw public key)`, and
  signs the challenge-bound payload the gateway expects
  (`v3|deviceId|clientId|clientMode|role|scopes|signedAt|token|nonce|platform|deviceFamily`,
  falling back to `v2`/`v1` for older gateways). `signedAt` is the `ts` from the
  gateway's `connect.challenge`, not local time.

The identity is persisted precisely because an operator approves that
fingerprint once: `openclaw.status` returns `device_id`, `device_public_key`
and `pairing_required`, so a `PAIRING_REQUIRED` refusal tells you which device
to approve in the OpenClaw Control UI (Devices) or via `openclaw devices`. A
trusted local backend (`client_id: gateway-client`, `client_mode: backend`) may
skip the proof, but only over a direct loopback connection with the shared
gateway token.

---

## See also

- [Agents & Chat](./19-agents-chat.md) — Vera's native agentic loop (the in-house counterpart)
- [Capability Framework §5](./01-capability-framework.md) — MCP proxying, the general pattern for bridging external tool servers

## Screenshots

## Session and gateway lifecycle

OpenClaw is an external agent-gateway integration. Configuration establishes
the endpoint and credentials; connect negotiates a usable session; prompt sends
work through that session; session listing/reset provides recovery. Vera still
owns capability policy and records the bridge call like any other integration.

Check status before prompting and distinguish gateway reachability from model
availability. A connected gateway can still fail because its selected model is
missing, its upstream provider rejected authentication, or a prior session is
stale. Reset only the affected session rather than deleting global integration
configuration.

Treat all gateway-returned tool requests as untrusted external proposals. They
must pass Vera's normal allowlists and confirmation rules; connection trust does
not imply permission to mutate local files, services, or third-party accounts.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
