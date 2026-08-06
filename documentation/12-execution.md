# 12 · Execution & Network Mapping

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

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
