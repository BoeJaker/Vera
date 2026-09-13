# 23 · Integrations — Services, Accounts, and Agent Protocols

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

`cal.effects.status` makes the execution boundary explicit. Event, todo, note,
and brain-dump commits currently mutate only Vera's local Redis diary. ICS fetch,
CalDAV `REPORT`, and Google event listing are inbound remote reads. Google OAuth
exchange and refresh belong to credential lifecycle rather than calendar-event
mutation. Vera does not currently implement a remote event create/update/delete
adapter, so Calendar truthfully reports no external mutation evidence,
enforcement, receipts, or retry behavior instead of borrowing another family's
readiness.

The Calendar sidebar shows **local edits · remote sync read-only** alongside sync
state. When a real remote-write adapter is introduced, it must first adopt the
shared effect contract and Calendar-specific evidence ledger before this status
can claim instrumentation.

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

SMTP sends, replies, and event-bridge deliveries project into the shared
external-effect contract before account transport resolution. Plans identify the
account and destination only by digests and exclude recipients, thread IDs,
subjects, bodies, credentials, and raw approval/idempotency references. Optional
control evidence is never passed to SMTP. When a durable success receipt exists,
the projection reports that policy would suppress the replay.

The Email migration remains observe-only. It records bounded policy evidence but
does not block delivery, retry a send, manufacture a completion receipt, or turn
an evidence failure into a mail failure. Email, Telegram, and generic API
observations use separate ledgers so one family cannot satisfy another family's
enforcement-readiness thresholds. Capability activity also redacts Email
arguments and results; the existing mail event stream remains a separate legacy
surface pending its own privacy migration.

The Integrations **Effect evidence** drawer can switch among Generic API,
Telegram, Email, and Commerce observations. Enforcement readiness, approval, and
activation controls appear only for the Generic API contract; the other family
views do not borrow or imply that authority.

The same drawer also exposes a static **Provider boundaries** inventory from
`integration.effect.inventory`. It separates local business records and
simulations from marketplace reads, OAuth lifecycle, marketplace writes,
container/build mutations, and Proxmox/provisioning mutations. This inventory is
descriptive: it performs no probe and does not add Infrastructure to the evidence
families. Commerce listing push, publish, and archive now produce payload-free,
observe-only projections before marketplace credentials are opened. This does
not block provider calls, add retries, forward control references, or claim that
eBay or Vinted idempotency has been validated. Credentialed validation remains a
separate, explicitly authorized activity.

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

Outbound `tg.send`, `tg.send_markdown`, `tg.notify`, and `tg.broadcast` operations
also project into the shared external-effect contract. Each recipient becomes a
separate non-executing plan identified only by a digest; message bodies, raw chat
identifiers, credentials, and raw approval/idempotency references are excluded.
Optional approval, idempotency, and retry intent is evaluated but never forwarded
to Telegram. Durable success evidence is consulted to report whether a replay
would be suppressed, and a Telegram-specific bounded ledger receives the policy
observation through the shared inspection surface.

This Telegram migration is deliberately observe-only: existing delivery behavior
is preserved if planning or evidence storage fails, no send is blocked, no
completion receipt is manufactured, and Vera adds no retry. Broadcast records one
observation for each allowed recipient and returns only aggregate evidence rather
than a recipient list.

---

## 5. A2A agent interoperability

A2A is the boundary for communicating with an independent, potentially opaque
remote agent; MCP remains the boundary for tools and resources used by an agent.
The agent-to-agent foundation begins with a non-executing v1.0 contract in
`vera/execution/a2a_mapping.py` and the inspection capability
`interop.a2a.conformance`.

`vera/execution/a2a_adapter.py` adds deterministic client and server plans for
the next integration step. It can plan one reviewed non-mutating task and an
authenticated server exposure, but deliberately performs no discovery fetch,
credential resolution, request, listener, registration, or artifact promotion.
The Agent Bridges UI reports these contracts separately from the queued live
transport and conformance work.

The manifest pins `a2a-sdk==1.1.2` for later implementation, but does not import
it. It maps Agent Cards and skills to unauthorised remote Capability Contract v2
candidates, server-assigned task IDs to `Run.task_id`, remote context IDs to an
opaque namespaced policy field rather than a Vera session, task states to Run
states/events, and verified outputs to `ArtifactRef`. Unknown, auth-required,
input-required, and rejected states retain their A2A meaning instead of being
silently coerced.

`analyze_agent_card` validates an inline card without fetching it. It bounds
size/depth, rejects plaintext credential-like values and credential-bearing or
non-HTTPS endpoints, honours ordered v1.0 interfaces, fails closed on unsupported
required extensions, and projects skills with unknown effects and
`authorized: false`, `executable: false`. It never registers those candidates.

The deterministic matrix covers card shape, malicious metadata, status coverage,
identity authority, and artifact boundaries. Actual discovery, a read-only task,
authentication/policy, duplicate sends, cancellation, disconnect/resubscribe,
push callbacks, artifact fetching, and teardown remain `queued_live`. Until those
pass, the manifest reports no client/server implementation and
`ready_for_execution: false`.

---

## 6. External source intake

Vera applies an inspection boundary in
`vera/integrations/source_intake.py` before an external source can become an
integration, MCP catalog entry, wrapper, or active capability. The lifecycle is:

`discovered → inspected → proposed → built → verified → approved → active → deprecated → removed`

The first three states are implemented. An inline MCP
descriptor or OpenAPI document is bounded, fingerprinted, checked for plaintext
credentials and unsafe endpoints, and projected into candidates with
`effects_status: unknown`, `authorized: false`, and `executable: false`.
Inspection never fetches a URL, starts an MCP command, resolves an external
`$ref`, writes a catalog record, installs a package, builds a wrapper, activates
an integration, or uses a secret.

The read-only surfaces are:

| Capability | Purpose |
|---|---|
| `integration.source.lifecycle` | Report implemented and queued lifecycle states |
| `integration.source.inspect` | Inspect one inline MCP/OpenAPI fixture |
| `integration.source.transition.plan` | Validate one adjacent transition without applying it |

Transitions through `inspected` and `proposed` can be planned deterministically.
`built` and every later state require further implementation or an operator
approval boundary. Existing MCP discovery/catalog and Fabric OpenAPI crawling
remain operational systems; they do not bypass this admission contract.

The build-planning boundary in
`vera/integrations/source_build_plan.py`. It accepts bounded inline proposals for
exactly pinned Python packages, CLI artifacts, OCI images, and repositories.
Python and CLI sources require exact versions plus artifact SHA-256 digests; OCI
references require `image@sha256`; repositories require credential-free HTTPS,
a full commit revision, and an archive digest. These are unverified provenance
claims until later evidence proves them.

Each proposal declares entry points, licence, effects, CPU/memory/accelerator
needs, opaque `secretref:` references, and a deny-or-HTTPS-allowlist network
policy. The resulting stable plan queues materialisation, SBOM/licence/malware/
vulnerability scans, manifest and policy review, conformance, rollback and
teardown proof, approval, activation, export, upgrade, rollback, and teardown.
It always reports `ready_for_build: false`, `ready_for_activation: false`, and
that no credential, network, fetch, install, build, registration, activation, or
execution occurred. A generated wrapper therefore remains a proposal.

| Capability | Purpose |
|---|---|
| `integration.source.build.status` | Report supported source kinds, required evidence, and queued execution |
| `integration.source.build.plan` | Validate one inline descriptor and return an inert reproducible plan |

Real materialisation, scans, builders, approval consumption, activation, and
rollback evidence remain `queued_live`; this contract does not call Vera's
existing image builders or repository tooling.

## 7. External-effect admission

`integration.effect.plan` is the non-executing policy boundary for outbound
operations. It classifies HTTP-shaped operations as reads, idempotent writes,
or non-idempotent writes and produces a stable `vera.external-effect-plan/v1`
record. Mutations require an opaque approval-receipt reference; POST and PATCH
also require an idempotency key. A retry is admitted only when those conditions
remain satisfied.

The plan contains connection and operation identity plus SHA-256 digests of the
opaque references. It never contains request payloads, credential values, raw
receipt references, or raw idempotency keys, and it neither resolves secrets nor
executes the operation. This is a shared contract for gradual adoption by Email,
Telegram, Calendar, commerce, generic API/MCP connectors, provisioning, and
other external-effect adapters; those existing paths are not silently treated
as migrated until they enforce and retain corresponding receipts.

Completed adapters can record each attempt in the durable, out-of-tree
`ExternalEffectReceiptLedger`. Attempt identities are hashed; response evidence
is accepted only as a SHA-256 digest; provider receipt references are hashed;
and request/response payloads are never accepted. Re-observing the same attempt
is idempotent, while different evidence for that attempt fails as a conflict.
`integration.effect.replay.status` validates the complete plan and reports
`do_not_repeat` after any matching successful receipt. The inspection surface
cannot execute or retry an operation, and there is intentionally no general
capability that lets an untrusted caller manufacture completion receipts.

`integration.connections.project` provides a separate, deterministic read model
over Integration, Account, and model-provider records. Every projected
connection retains its source system and record identity; explicit references
are resolved only when unique, and shared endpoint origins are reported as
collisions rather than automatically merged. Endpoint userinfo, path, query,
and fragment data are discarded, which prevents private calendar URLs and API
credentials from entering the projection. Credential state is presence-only.

The projection reports unavailable source registries and malformed endpoints as
gaps. It never opens a secret, probes a service, changes access, establishes a
connection, or becomes the authority for the underlying records. This gives UI
and tool-using models one bounded inventory while preserving the existing
registries as owners during migration.

The generic `integration.api.call` boundary emits the same policy decision as
`effect_shadow`. It remains in `observe_only` mode unless three independent
conditions agree: the deployment gate is enabled, the current operator decision
approves enforcement, and a fresh activation is bound to that exact decision
revision and enforcement contract. Optional idempotency and approval
references are evaluated but never forwarded to the remote API; the request
path is represented only by an operation digest in audit events. If a durable
success receipt already exists, telemetry reports that enforcement would
suppress the replay. When enforcement is active, a denied or already-completed
mutation is rejected before credentials are opened or an HTTP request is made.
Vera does not add automatic retries.

`integration.effect.retry.plan` turns a validated effect plan plus bounded
outcome evidence into a non-executing retry decision. It recognizes a small,
stable set of transient HTTP statuses and transport error codes, enforces a
maximum attempt budget, refuses retries after a durable success, and requires
the original plan to have explicitly requested and admitted retry behavior.
Provider `Retry-After` or exhausted-rate-limit reset evidence becomes a minimum
delay that local backoff cannot shorten. Vera returns a jitter window for a
scheduler to use later; the capability itself never sleeps, chooses random
timing, executes the operation, opens credentials, or records a receipt.

The Integrations header includes an **Effect evidence** drawer backed by
`integration.effect.receipts`. It shows aggregate plan, receipt, observation,
and outcome counts plus a bounded recent window. Entries contain effect
classification, method, connection/operation identity, shortened plan/receipt
digests, and observation counts. Request and response bodies, credentials, raw
approval receipts, raw idempotency keys, and mutation controls are absent. An
empty view means no migrated adapter has recorded durable evidence yet; it is
not presented as proof that external effects did not occur.

The same drawer reads `integration.effect.retry.policy` to explain the exact
transient HTTP/error vocabulary, refusal reasons, hard attempt/delay bounds, and
the evidence required before a scheduler may consider another attempt. This is
a policy legend, not a retry control: receipt rows alone do not contain enough
context to authorize a retry, and the view cannot execute, sleep, select jitter,
open a secret, replay an operation, or record evidence.

Observe-only decisions are accumulated separately as bounded, payload-free
evidence. The drawer reports how many generic API calls policy would admit,
execute, or suppress as replays, alongside refusal-reason counts from the recent
window. Stored observations contain only policy fields, digests, classifications,
methods, and timestamps—not paths, queries, bodies, headers, credentials, or raw
approval/idempotency references. These measurements do not block current calls
and are not themselves sufficient evidence to enable enforcement.

Vera also applies fixed, fail-closed coverage thresholds before describing the
evidence as ready for operator review: total observations, read and mutation
coverage, admitted and denied decisions, and at least one replay-suppression
example. Passing every check means only that the shadow sample is representative
enough to review. It does not prove safety, authorize a rollout, or enable
enforcement; the generic API remains observe-only.

An operator may record either `continue_observing` or an approval for a future
rollout. Decisions use optimistic revisions so a stale browser cannot overwrite
a newer choice, preserve bounded immutable history, and store operator and
approval references only as digests. Future-rollout approval is rejected until
the readiness checks pass and an approval receipt is supplied. Requested and
effective modes remain separate: recording approval changes the requested mode,
but the effective generic API mode remains `observe_only` until the deployment
gate is enabled and an operator records a separate activation receipt. Activation
is revision-guarded, reversible, stored as immutable history, and automatically
invalidated by a changed approval or enforcement contract. Operator and receipt
references are stored only as digests. If the deployment gate is enabled but
activation state cannot be read, mutations fail closed; reads and deployments
with the gate disabled retain compatibility behavior. Recording
`continue_observing` or deactivating reverses the effective mode without deleting
its audit history.

---

## 8. Common threads

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
