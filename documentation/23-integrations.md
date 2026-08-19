# 23 · Integrations — Calendar, Email, Telegram, Accounts

Four outward-facing modules that connect Vera to the everyday world. They share two foundations: the unified **Accounts** registry (credentials configured once, reused everywhere) and **Fernet-sealed secrets** (see [Security & Secrets](./29-security.md)). Each also bridges selected `vera:events` outward and ingests inbound data into the [Data Fabric](./06-data-fabric.md).

| Module | Group | Tab |
|---|---|---|
| Accounts | `acct.*` | Accounts |
| Calendar | `cal.*` | Calendar |
| Email | `mail.*` | Email |
| Telegram | `tg.*` | Telegram |

---

## 1. Accounts registry (`accounts/`)

A single shared store of *identities*, so Calendar and Email don't each hold their own credentials. Each account is a label + email carrying whatever credential blocks it needs:

- **Mail block** — IMAP/SMTP host settings + an app-password → used by Email.
- **Calendar block** — CalDAV url/user/password and/or an ICS URL → used by Calendar.

Secrets (`app_password`, `caldav_password`) are sealed at rest with the shared Fernet helper and **never returned to the UI** — list/get redact them to `has_*` flags.

| Cap | Purpose |
|---|---|
| `acct.list` / `acct.get` | Browse accounts (secrets redacted) |
| `acct.upsert` / `acct.delete` | Account CRUD |
| `acct.test` | Test an account's credentials |

Other modules import helpers directly: `get_account(id)` (secrets opened), `list_accounts()`, `default_mail_account()`. Redis layout: `vera:accounts` (hash, secrets sealed).

---

## 2. Calendar (`calendar/`)

A personal scheduler/diary: events, todos, and notes stored in Redis (with a sorted-set index by start time for fast date-range queries), plus cloud sync and an LLM brain-dump.

- **Cloud sync (pull)** from Google Calendar, generic CalDAV, and ICS subscription URLs — all via `httpx`, no heavyweight deps.
- **Brain-dump** — free text → the local cluster turns it into a coherent set of events/todos/notes + a suggested daily plan for review.
- Optional persistence of the diary into the fabric (`diary` dataset).

| Cap group | Caps |
|---|---|
| Events | `cal.events.list`, `cal.event.upsert`, `cal.event.delete` |
| Todos | `cal.todos.list`, `cal.todo.upsert`, `cal.todo.toggle`, `cal.todo.delete` |
| Notes | `cal.notes.list`, `cal.note.upsert`, `cal.note.delete` |
| Brain-dump | `cal.braindump`, `cal.braindump.commit` |
| Sources | `cal.sources.list`, `cal.source.upsert`, `cal.source.delete` |
| Sync | `cal.sync.run`, `cal.sync.status` |
| Google OAuth | `cal.google.auth_url`, `cal.google.auth_complete`, `cal.google.calendars` |
| Misc | `cal.fabric.persist`, `cal.config.get/set`, `cal.panel.html` |

Cloud credentials (Google OAuth secret + refresh token, CalDAV app-password) are sealed before they touch Redis and never returned to the UI.

---

## 3. Email (`email/`)

Multi-account IMAP/SMTP backed by the Accounts registry, with AI assistance.

- **Reading is gated** behind a global `reading_enabled` flag (OFF by default) — inbox/search/message caps only work when enabled.
- **Send & reply** via SMTP from any configured account.
- **AI draft / summarise** using the local cluster.
- **Event bridge** — forward selected `vera:events` to an address.

| Cap group | Caps |
|---|---|
| Config | `mail.config.get/set` (reading, model, signature, default_account) |
| Accounts | `mail.accounts.list`, `mail.test` |
| Reading (gated) | `mail.inbox.list`, `mail.message.get`, `mail.search` |
| Sending | `mail.send`, `mail.reply`, `mail.draft` (all accept `account=<id>`) |
| Events | `mail.events.configure`, `mail.events.status` |
| Panel | `mail.panel.html` |

Email keeps only global settings + the notification bridge config in Redis; credentials live (sealed) in Accounts.

---

## 4. Telegram (`telegram/`)

A bidirectional bot that brings the capability framework into Telegram.

- **Long-poll `getUpdates` loop** that never blocks the orchestrator.
- **Per-chat `session_id`** (`tg:{chat_id}`) so all activity flows onto the [memory graph](./05-memory-graph.md) and shows up in the UI like web sessions.
- **Slash commands**: `/help /id /caps /agents /agent /run /status /think /reset`; free text routes to a configurable default agent (`agent.chat`).
- **Per-chat allow-list** — admin chat always allowed; others must be whitelisted.
- **Event bridge** — forward selected `vera:events` (DAG complete, research finished, errors) to a target chat. This is also the channel for [Dream](./17-dream.md) HITL approvals.
- **Fabric ingest** — inbound messages land in dataset `tg.messages`.

| Cap group | Caps |
|---|---|
| Config | `tg.config.set/get` |
| Bot | `tg.bot.start/stop/status` |
| Send | `tg.send`, `tg.send_markdown`, `tg.notify`, `tg.broadcast` |
| Chats | `tg.chats.list/allow/revoke`, `tg.history` |
| Events | `tg.events.configure/status` |
| Panel | `tg.panel.html` |

The bot token is sealed via the shared secrets helper; config persists in `vera:tg:*` and auto-resumes on restart.

---

## 5. Common threads

- **Sealed secrets** — every credential is Fernet-sealed at rest and redacted from the UI ([Security & Secrets](./29-security.md)).
- **Event bridges** — Email and Telegram can both forward `vera:events` outward, turning Vera's internal stream into notifications.
- **Per-source fabric datasets** — Telegram messages (`tg.messages`) and the diary (`diary`) become first-class fabric data, recallable like anything else.

---

## See also

- [Security & Secrets](./29-security.md) — the Fernet sealing all four rely on
- [Data Fabric](./06-data-fabric.md) — `tg.messages`, `diary`, and event ingest
- [Agents & Chat](./19-agents-chat.md) — Telegram free-text routes to `agent.chat`
- [Dream](./17-dream.md) — Telegram delivery + HITL approvals
- [Capability Framework](./01-capability-framework.md) — `acct.*` / `cal.*` / `mail.*` / `tg.*` registration

## Screenshots

## Connection and trust model

Integrations separate a service definition from credentials, granted access,
and a live connection. Accounts hold sealed provider-specific configuration;
integration records describe how Vera may use it; capability families expose
mail, calendar, messaging, or generic API/MCP operations. Disconnecting an app
should revoke Vera's active use without silently deleting unrelated local data.

Test in layers: credential presence, provider authentication, account/service
discovery, a read-only operation, then an explicitly authorized write. OAuth
redirect mismatches, expired refresh tokens, provider scopes, clock skew, and
container DNS are more common than application logic failures. Never paste
secrets into board items, traces, screenshots, or capability arguments that are
persisted as ordinary history.

`integration.access.set` is the policy boundary for general integrations.
Operator-driven web access adds Operator's allowlist/destructive-action policy;
API access remains governed by the integration and account capabilities.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
