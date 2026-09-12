# 12 · Execution & Network Mapping

## Dream adapter boundary

The first product migration onto Workflow IR is Dream's built-in generic,
non-iterative pipeline. Workflow IR owns its normalized ordered plan and stable
definition hash; `dream.native-stage-runner` remains the declared execution
owner. This boundary deliberately preserves Dream's HITL, cancellation,
journaling, artifact collation, progress, and persistence while making the plan
inspectable and portable. Workflow IR still has no general execution entry point,
and custom or iterative Dream pipelines remain native until separately gated.

## Plain DAG execution boundary

Exactly round-trippable unsupervised DAG definitions now use normalized
Workflow IR as their authoritative definition boundary. The existing native DAG
runner still owns node effects, so execution behavior does not fork; Run records
bind `workflow_id` to the Workflow IR content hash. Definitions that cannot be
represented exactly retain native execution with explicit compatibility
evidence. Supervised and the other enhanced DAG modes do not inherit this claim.

## Shared scheduling trigger evidence

The long-term Calendar scheduler, Dream's idle scheduler, and Research's
continuous-iteration loop now project their native fire decisions into the
same `vera.workflow-trigger/v1` event envelope.
The envelope gives downstream observers a stable trigger and idempotency key,
source revision, opaque target reference, schedule kind, explicit timezone,
occurrence identity, and native authority declaration. It never contains an
action title, goal, instructions, Dream prompt, sensor result, or workflow
payload, and it cannot execute work.

This is an observation boundary, not a replacement scheduler. Calendar still
owns timestamp and threshold evaluation, user notification, and system-action
dispatch. Dream still owns idle, hour-window, cooldown, sensor, and resource
gates plus cycle creation. Projection or event-bus failure is isolated from
those existing decisions.

Both adapters now derive their evidence from a closed,
`vera.workflow-schedule/v1` definition. Calendar one-shots are normalized to a
canonical UTC instant. Dream recurrences carry an explicit IANA timezone, a
local-hours window, and an elapsed-time interval anchored to the last completed
run. Local window checks therefore follow daylight-saving transitions while
cooldowns measure real elapsed UTC time, avoiding duplicated or skipped wall
clock hours. The definition is content-addressed, rejects unknown fields and
invalid zones, and declares `executes: false`; it is portable schedule evidence,
not another scheduler or authority path.

Misfire and catch-up behavior is now projected through a separate closed
`vera.workflow-schedule-policy/v1` contract and
`vera.workflow-schedule-decision/v1` event. Calendar's time-based one-shots
retain their native "fire once when next observed" behavior after downtime.
Dream explicitly coalesces any number of missed intervals into at most one
eligible cycle. Research exposes the same bounded catch-up evidence while
honestly retaining its native immediate-on-restart behavior rather than using
the shared decision as an execution gate. Each decision records the due instant, lateness, missed
occurrence count, chosen disposition, schedule/policy/trigger identities, and
declares both `executes: false` and `replay_effects: false`. It cannot claim a
run, invoke a target, or bypass the products' normal gates. A closed `skip`
classification exists for future adapters, but is not silently applied to
existing schedules.

A small out-of-tree SQLite ledger records each valid
trigger identity atomically and emits a separate
`vera.workflow-trigger-receipt/v1` observation. It survives process reopen,
classifies the first observation versus later duplicates, retains bounded UTC
observation times and counts, and rejects identity reuse with changed schedule
or authority semantics. The receipt is evidence only: it does not suppress a
native dispatch, schedule work, replay a trigger, or claim exactly-once effects.
Ledger, trigger-stream, or receipt-stream failure remains isolated from native
execution. Schedule-decision stream failure is isolated in the same way.

Schedule lifecycle is also explicit and portable without becoming a shared
executor. `vera.workflow-schedule-lifecycle/v1` represents `active`, `paused`,
terminal `cancelled`, `completed`, and `failed` states, with validated
active↔paused and active/paused→cancelled transitions. In-flight and
awaiting-reply Calendar actions fail closed rather than pretending that a state
flag stopped work which is already underway. Calendar and Dream persist the native
state first and emit content-safe transition evidence second. The evidence
contains opaque source/revision identities, preserves the schedule record, and
declares `executes: false`; event-stream failure cannot undo the native state
change. Deletion remains a separate destructive operation rather than an alias
for cancellation. Research iteration records project active and paused states;
its native stop currently deletes the record, so the shared lifecycle adapter
fails closed instead of manufacturing a preserved cancelled state.

## Scheduler, job, and transition authority

A source-bound inventory distinguishes execution surfaces by semantic scope,
role, state owner, persistence, and whether they execute. It records source
digests rather than source bodies, and it cannot run, mutate, migrate, or remove
any reviewed component.

The process interval scheduler, Calendar action scheduler, Dream trigger
scheduler, and Research iteration scheduler own different state and timing
semantics. They are not duplicate executors merely because each advances work
over time. The shared schedule and lifecycle contracts are non-executing
projections rather than another scheduler. The current recommendation is to
retain those native authorities while continuing to project their definitions,
decisions, and lifecycle evidence through shared contracts.

The job surfaces are also layered. Worker job persistence owns durable inference
history, while the Workshop observatory tracks jobs awaited by agent and DAG
runs. One concrete overlap remains: the IDE module duplicates the idle queue's
load, save, and drop gateways even though `idle_queue_service` owns that state,
and it hosts the queue driver. Those gateways and driver placement should
migrate to the service owner behind compatibility tests; this review deletes
nothing.

Calendar and Dream transitions persist native state before emitting shared
lifecycle evidence. Fabric revision transitions and operator Run projections
belong to different state machines. Their shared word “transition” therefore
does not establish duplicated authority.

`execution/exec_capabilities.py` is two capability groups in one module: **`exec.*`** — shell, PowerShell, code, and SSH execution — and **`netscan.*`** — network asset discovery, target probing, and an auxiliary topology graph. Both are governed by a single configurable **exec sandbox policy**, and both surface their own harness tabs (the tabbed **Exec** consoles and the Cytoscape **Netmap**).

This is the module that lets Vera (and an agent driving it) actually *touch* the host and the network — so the sandbox section is the most important part of the page.

---

## 1. The exec sandbox

Every local shell/code path runs through one gate: `_sandbox_check(text, cwd=, language=)`. The same gate governs `exec.*`, the IDE **Run** action ([IDE Module](./08-ide.md)), and the Docker CLI lifecycle caps ([Docker](./13-docker.md)) — so one policy controls everything that can execute on the host.

The policy is a JSON document stored at `~/.vera_exec_sandbox.json` (override with `VERA_EXEC_SANDBOX`), written `0o600`. Fields:

| Field | Meaning |
|---|---|
| `enabled` | Master switch. When off, checks pass through (trusted host). |
| `languages` | Whitelist for `exec.code.run` (`[]` = all languages allowed). |
| `allow_paths` | If set, a command's `cwd` must live under one of these roots (a jail). |
| `deny_paths` | Roots a `cwd`/command may never reference — always override `allow_paths`. |
| `command_blocklist` | List of regexes; a match blocks the command. |
| `command_allowlist` | List of regexes; when set, a command must match one. |
| `max_timeout` | Hard cap (seconds) on any single execution. `0` = uncapped. |
| `network` | Whether executed code is allowed network access. |
| `artifact_root` | Base dir for agent-generated files (`''` = `~/.vera_artifacts`). |
| `artifact_scope` | How artifact dirs are partitioned: `artifact` \| `session` \| `project` \| `workspace`. |

Regex lists are validated on save (`exec.sandbox.set`) so a bad pattern can't silently disable a list, and an `exec.sandbox.updated` event is emitted so every open panel re-reads the policy.

| Cap | Path | Purpose |
|---|---|---|
| `exec.sandbox.get` | `GET /exec/sandbox` | Current policy + path + shipped defaults |
| `exec.sandbox.set` | `POST /exec/sandbox/set` | Patch the policy (omitted fields unchanged; `reset:true` restores defaults) |
| `exec.sandbox.artifact_dir` | `GET /exec/sandbox/artifact_dir` | Resolve (and create) the artifact dir for a run, per `artifact_scope` |
| `exec.sandbox.write_artifact` | `POST /exec/sandbox/write_artifact` | Write a file confined to the run's artifact dir (path-traversal-safe) |

The policy is edited from the shared **`<vera-sandbox-controls>`** web component, which appears in the Exec, IDE, and Workers panels — see [`sandbox_controls_element.js`](../vera/sandbox_controls_element.js). Editing it in one place changes it everywhere.

---

## 2. Shell & code execution (`exec.*`)

| Cap | Path | Runs |
|---|---|---|
| `exec.bash.run` | `POST /exec/bash/run` | A bash command locally (captured stdout/stderr/rc) |
| `exec.ps.run` | `POST /exec/ps/run` | A PowerShell command (`pwsh` or `powershell`) |
| `exec.code.run` | `POST /exec/code/run` | A snippet in a named language (sandbox `languages` whitelist applies) |
| `exec.code.langs` | `GET /exec/code/langs` | Which language runtimes are available on this host |
| `exec.ssh.run` | `POST /exec/ssh/run` | A command on a remote host over SSH (password or key) |
| `exec.llm.models` | — | Models available for the netmap panel's LLM-assisted analysis |

For long-running commands there are **raw SSE streaming endpoints** (not `@capability`, because they need a streaming response rather than a single JSON return):

```
POST /exec/bash/stream     # stream stdout/stderr of a local bash command
POST /exec/ps/stream       # stream stdout/stderr of a local pwsh command
POST /exec/ssh/stream      # stream stdout/stderr of an SSH command
```

The captured `run` caps are what DAGs and agents call; the `stream` endpoints are what the Exec console UI uses for live output.

### SSH host registry

SSH targets are stored so you don't re-enter credentials:

| Cap | Purpose |
|---|---|
| `exec.ssh.hosts.list` | List stored SSH host credentials (secrets redacted) |
| `exec.ssh.hosts.save` | Save/replace a host credential |
| `exec.ssh.hosts.delete` | Remove a host credential |
| `exec.ssh.probe` | Quick TCP-ping `:22` connectivity check |

---

## 3. Network discovery (`netscan.*`)

The scan caps sweep an environment and persist what they find into an **auxiliary graph** (see §5). Each scanner targets a different infrastructure layer:

| Cap | Discovers |
|---|---|
| `netscan.lan.scan` | ARP + TCP port sweep of a CIDR → reachable hosts |
| `netscan.docker.scan` | `docker ps` on a host (local or over SSH) → containers |
| `netscan.proxmox.scan` | Proxmox PVE API → cluster nodes + guests (qemu/lxc) |
| `netscan.k8s.scan` | `kubectl get nodes/pods` → cluster, nodes, pods |
| `netscan.web.scan` | Web-facing surface of a target |

### Target probing

Once a host is known, the `netscan.target.*` caps enrich it:

| Cap | Probe |
|---|---|
| `netscan.target.ports` | Port scan a single target |
| `netscan.target.tech` | Technology fingerprint of a web target |
| `netscan.target.traffic` | Observe traffic characteristics |
| `netscan.target.banner` | Grab service banners |
| `netscan.target.tls` | Inspect the TLS configuration |
| `netscan.target.cert_scrape` | Pull certificate details (SANs, issuer, validity) |
| `netscan.target.fingerprint` | Composite host fingerprint |
| `netscan.target.traceroute` | Path trace to the target |

### OSINT dorking

| Cap | Purpose |
|---|---|
| `netscan.dork.search` | Run a search-engine dork query |
| `netscan.dork.targeted` | Dork scoped to a specific target/domain |

---

## 4. Maps — save, load, share

A discovered topology can be snapshotted and restored:

| Cap | Purpose |
|---|---|
| `netscan.map.save` | Persist the current aux graph as a named map |
| `netscan.map.list` | List saved maps |
| `netscan.map.load` | Restore a saved map |
| `netscan.map.delete` | Delete a saved map |
| `netscan.fabric.load_web` | Pull web-acquisition entities from the [Data Fabric](./06-data-fabric.md) into the map |

---

## 5. The auxiliary graph

Discovered assets are **not** written to the memory graph. They live in their own set of node labels under `FABRIC_NEO` (the same Neo4j instance, separate label space), so infrastructure topology never pollutes session memory.

**Node labels:** `:NetHost`, `:DockerHost`, `:Container`, `:PVENode`, `:PVEGuest`, `:K8sCluster`, `:K8sNode`, `:K8sPod`.

**Edges:**

| Edge | Meaning |
|---|---|
| `:ON_NETWORK` | A host belongs to a subnet (implicit, via `.subnet`) |
| `:HOSTS` | `DockerHost → Container` |
| `:IN_CLUSTER` | `PVENode → PVECluster`, `K8sNode → K8sCluster` |
| `:RUNS` | `PVENode → PVEGuest` |
| `:SCHEDULED_ON` | `K8sPod → K8sNode` |
| `:SAME_IP` | Cross-source link when a `NetHost` IP matches a PVE/Docker/K8s node |

The `:SAME_IP` edge is what fuses the layers: a LAN scan finds an IP, a Proxmox scan finds the same IP as a hypervisor, and the graph stitches them so one box shows all its roles.

Graph access caps:

| Cap | Purpose |
|---|---|
| `netscan.graph` | Fetch the aux graph in Cytoscape format (for the panel) |
| `netscan.node.get` | One node + its edges |
| `netscan.nodes.clear` | Wipe discovered nodes by source |
| `netscan.graph.clear_all` | Wipe the entire aux graph |

---

## 6. UI panels

- **`exec-panel`** (`mode="tab"`, icon `>_`) — tabbed **Bash / PowerShell / SSH** consoles backed by the streaming endpoints, plus the embedded `<vera-sandbox-controls>` policy editor.
- **`netmap-panel`** (`mode="tab"`, icon `⬢`) — an interactive Cytoscape.js graph of discovered assets. Right-click a node → **"SSH here"** jumps to the Exec panel with the host pre-filled. LLM-assisted analysis uses `exec.llm.models`.

---

## 7. Security note

This module is, by design, dual-use: it runs commands and scans networks. Treat it accordingly.

- Keep `enabled: true` on any host you don't fully trust, and start from a tight `allow_paths` jail + `command_blocklist`.
- The `netscan.*` caps are intended for **your own** infrastructure and authorised assessments — the same as any port scanner. The aux graph is a homelab/asset-inventory tool.
- Because the sandbox policy gates `exec.*`, IDE Run, **and** Docker CLI caps, locking it down closes all three surfaces at once.

---

## Requirements

```
pip install asyncssh httpx
```

System tools used opportunistically (called via bash, optional): `arp`, `ping` (LAN scan); `docker` (Docker scan); `kubectl` (K8s scan). Proxmox uses its HTTP API — no shell tools required.

---

## See also

- [Capability Framework](./01-capability-framework.md) — how `exec.*` / `netscan.*` register
- [IDE Module](./08-ide.md) — shares the exec sandbox for its **Run** action
- [Docker](./13-docker.md) — container lifecycle caps gated by the same sandbox
- [Data Fabric](./06-data-fabric.md) — `netscan.fabric.load_web` source
- [Galaxy Graph](./09-galaxy-graph.md) — the graph component the Netmap panel renders with

## Screenshots

## Execution boundary and diagnostics

### Runtime-neutral Runs

The execution foundation separates Vera's durable `Run` identity and events
from the engine that performs work. Workflow IR, local DAGs, provider calls,
and remote-agent tasks can project into the same lifecycle without pretending
their native identifiers are interchangeable. Adapters preserve native
authority, report lossy state mappings, and bind cancellation, timeouts,
retries, artifacts, and teardown to the Run rather than to a UI session.

This is the basis for adding external runtimes without creating another bespoke
loop. See [agent runtimes and providers](36-agent-runtimes-providers.md) and
[interoperability foundations](46-interoperability-foundations.md).

Execution capabilities normalize local shell, PowerShell, Python, and remote
SSH work behind one result contract: command, target, exit code, standard output,
standard error, duration, and error state. Network mapping resolves a logical
machine before remote execution; it should not be bypassed with guessed host
addresses embedded in agent prompts.

Choose the narrowest executor and working directory possible. Set bounded
timeouts, avoid interactive commands, and treat stdout as untrusted data. A
successful transport does not imply a successful command—always inspect the
exit code. Conversely, an SSH connection error, command-not-found, permission
failure, timeout, and non-zero program exit are different operator actions and
should remain distinguishable.

Mutating commands need the same authorization as an equivalent direct API call.
Do not place credentials in command lines because they can appear in process
lists and traces. For repeatable automation, prefer a purpose-built capability
over a long shell string; it provides typed inputs, policy, and focused tests.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
