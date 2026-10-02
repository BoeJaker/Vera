# 08 · IDE Module

Vera's IDE module is the in-harness coding environment. It combines a
**central VS Code** (code-server behind a same-origin proxy), the classic
**Workbench** (file tree, tabbed editor, three specialised LLM agents and an
autonomous tool-calling agent loop), **source self-inspection** through
snapshot copies, **PR-style change proposals** for edits made in sandboxes,
**remote IDE instances** that can run Claude Code or Vera's own agents over SSH,
and **Dispatch**, which ingests Claude Code and Codex transcripts into memory.
Every operation is a capability, emits events, and records activity to the
memory graph and the data fabric.

The module lives in [`vera/ide/`](../vera/ide), with one shared helper at
[`vera/workspace_path.py`](../vera/workspace_path.py) and two client-side
companions in [`tools/vera-vscode`](../tools/vera-vscode) and
[`tools/vscode-input-automator`](../tools/vscode-input-automator). The IDE tab
is the default home for coding work in the harness. The Workbench, inspection
and remote-run paths are mature; stalled-session auto-resume is deliberately
conservative and stays in observe-only mode unless you switch it on.

## Contents

- [1. Architecture](#1-architecture)
- [2. Source map](#2-source-map)
- [3. The three IDE agents](#3-the-three-ide-agents)
  - [Generation capabilities](#generation-capabilities)
- [4. Workspaces and the filesystem](#4-workspaces-and-the-filesystem)
- [5. Coding tools and the tool dispatcher](#5-coding-tools-and-the-tool-dispatcher)
- [6. The in-memory sandbox](#6-the-in-memory-sandbox)
- [7. Source self-inspection (snapshots)](#7-source-self-inspection-snapshots)
- [8. Workspace change proposals](#8-workspace-change-proposals)
- [9. Git](#9-git)
- [10. The IDE tab and the classic Workbench](#10-the-ide-tab-and-the-classic-workbench)
- [11. The Workbench agent loop](#11-the-workbench-agent-loop)
- [12. Streaming and the Run terminal](#12-streaming-and-the-run-terminal)
- [13. Central VS Code, the same-origin proxy and interactive workers](#13-central-vs-code-the-same-origin-proxy-and-interactive-workers)
- [14. Remote IDE instances, remote runs and the work queue](#14-remote-ide-instances-remote-runs-and-the-work-queue)
- [15. VS Code client windows and Claude Code auth](#15-vs-code-client-windows-and-claude-code-auth)
- [16. Quick-connect and the self-packaged extension](#16-quick-connect-and-the-self-packaged-extension)
- [17. Dispatch: Claude Code and Codex transcripts](#17-dispatch-claude-code-and-codex-transcripts)
- [18. Stalled-session watch and auto-resume](#18-stalled-session-watch-and-auto-resume)
- [19. Workspace path normalisation](#19-workspace-path-normalisation)
- [20. Events, memory graph and fabric](#20-events-memory-graph-and-fabric)
- [21. Configuration](#21-configuration)
- [22. Troubleshooting](#22-troubleshooting)
- [See also](#see-also)
- [Screenshots](#screenshots)
- [Capabilities](#capabilities)

---

## 1. Architecture

```mermaid
flowchart LR
  subgraph Tab["IDE tab (vscode_panel.html)"]
    VS["VS Code view"]
    WB["Workbench view (ide_panel.html)"]
    RQ["Remotes & Queue (ide_remote_panel.html)"]
    LL["Loop Lab (/evolve/panel)"]
    DP["Dispatch (ide_claude_sessions_panel.html)"]
  end
  VS -->|/vscode/{id}/...| PX["Same-origin proxy"]
  PX --> CS1["central code-server"]
  PX --> CS2["sandbox worker sidecars"]
  PX --> CS3["remote code-server hosts"]
  WB --> CAPS["ide.fs.* / ide.code.* / ide.git.*"]
  WB --> GEN["ide.generate / POST /ide/stream"]
  GEN --> ROUTER["shared model router (profile 'ide')"]
  WB --> INS["ide.inspect.* (snapshots)"]
  RQ --> REM["ide.remote.* (SSH, Claude Code, queue)"]
  DP --> CSES["ide.claude_sessions.*"]
  CAPS --> REC["_record(): memory graph + fabric + events"]
  GEN --> REC
  REM --> REC
  CSES --> REC
```

- **Everything is a capability.** The panels are thin clients; the same calls
  are available over REST, MCP and the agent loop.
- **One recorder.** `_record()` in `ide_capabilities.py` writes a memory-graph
  node, appends a fabric row and broadcasts a UI event. The code tools, remote
  runs and transcript ingestion all reuse it.
- **One project root.** The Workbench, the central code-server and the coding
  agents share the tree at `VERA_PROJECT_ROOT` (default `~/vera_projects`),
  which the compose stack mounts as the `vera-projects` volume.

---

## 2. Source map

| File | Responsibility |
|---|---|
| `ide/ide_capabilities.py` | Agent presets, generation, `/ide/stream`, in-memory sandbox, real-FS caps, workspaces, change proposals, git, session recording, Workbench panel, Run terminal and Docker helper routes |
| `ide/ide_code_capabilities.py` | Structured coding tools (`ide.code.*`, `ide.fs.exists`), the tool manifest, the whitelist and `ide.code.tool_dispatch` |
| `ide/ide_inspect_capabilities.py` | Source self-inspection: snapshots, diff, explicit promote, LLM review/plan, capability and panel scaffolding |
| `ide/ide_remote_capabilities.py` | Remote IDE registry, code-server provisioning over SSH, Claude Code / vera-agent runs, work queue + autopilot, MCP bridge install, VS Code client dispatch |
| `ide/remote_exec_core.py` | Pure helpers: compose the headless `claude` command line safely and parse its result |
| `ide/vscode_capabilities.py` | Central code-server, same-origin HTTP/WebSocket proxy, sandbox interactive workers, passwords, quick-connect routes, `.vsix` packaging |
| `ide/vera_mcp_bridge.py` | Stdlib-only stdio MCP shim that forwards `tools/list` / `tools/call` to Vera's REST MCP surface |
| `ide/ide_claude_sessions_capabilities.py` | Dispatch: transcript sources, scan/ingest, session list and history; also the idle-time `background.*` queue |
| `ide/agent_transcripts.py` | Pure parsers for Claude Code and Codex JSONL transcript formats |
| `ide/session_watch_capabilities.py`, `ide/session_watch_core.py` | Stalled-session classification, resume/release, policy and the auto-resume loop |
| `ide/ws_changes_core.py` | Compare-and-swap guard that stops a change proposal clobbering newer work |
| `workspace_path.py` | The single rule for collapsing a redundant `/workspace/` prefix |
| `ide/*.html` | `vscode_panel.html` (IDE tab wrapper), `ide_panel.html` (Workbench), `ide_inspect_panel.html`, `ide_remote_panel.html`, `ide_changes_panel.html`, `ide_claude_sessions_panel.html` |

Registered UI panels:

| Panel id | Title | Mode | Served from |
|---|---|---|---|
| `ide-panel` | IDE | `tab` (order 50) | iframe of `/ide/vscode/panel` |
| `ide-remote-panel` | Remote IDE | `element` (shown inside the IDE tab) | `/ide/remote/panel` |
| `ide-claude-dispatch` | Dispatch | `element` (shown inside the IDE tab) | `/ide/claude_sessions/panel` |
| `ide-workspace-changes` | Workspace Changes | `inject` | `/ide/changes/panel` |
| `ide-inspect-panel` | Source Inspection | side card / Workbench drawer | `/ide/inspect/panel` |

---

## 3. The three IDE agents

Three presets are defined in `_AGENT_PRESETS` (`ide_capabilities.py`) and
auto-seeded at startup into the shared agent registry, so they also appear in
the unified agent surface (`agent.list`, `agent.chat`, …).

| Preset | Label | Role in router | Temperature | top_p | `num_ctx` | `prefer_gpu` | Purpose |
|---|---|---|---|---|---|---|---|
| `ide-thinker` | Thinker | `thinker` | 0.75 | 0.92 | 16384 | yes | Planning, architecture, cross-file reasoning |
| `ide-writer` | Writer | `writer` | 0.2 | 0.85 | 32768 | no | Code generation, scaffolding, refactoring |
| `ide-analyser` | Analyser | `verifier` | 0.05 | 0.80 | 32768 | no | Review, debugging, explanation |

All presets use `tool_mode: "none"` and leave `model` empty, so the cluster
default applies unless a caller passes one. Generation goes through Vera's
shared model router with the `ide` routing profile, whose three roles
(`thinker`, `writer`, `verifier`) all use `job_type: "code"`. Queueing,
instance selection, cancellation and GPU admission therefore follow the same
policy as every other model-backed capability.

### Generation capabilities

| Capability | Route | Purpose |
|---|---|---|
| `ide.agent.list` | `GET /ide/agents` | The three presets with registration state, model, temperature and GPU preference |
| `ide.agent.chat` | `POST /ide/agents/chat` | One prompt to `thinker` / `writer` / `analyser` with optional `history` and `context_files`. Prompt, system, history and context files are redacted from activity |
| `ide.instances` | `GET /ide/instances` | Ollama instances with tier labels, status, latency and models |
| `ide.models` | `GET /ide/models` | Models available across online instances |
| `ide.generate` | `POST /ide/generate` | Raw generation through a named agent (used by the Workbench chat and agent loop) |
| `POST /ide/stream` | HTTP only | SSE token stream (see [§12](#12-streaming-and-the-run-terminal)); not a registered capability, but activity is recorded under `ide.stream` |

---

## 4. Workspaces and the filesystem

The IDE works against real directories. `PROJECT_ROOT` is
`cfg.VERA_PROJECT_ROOT` (env `VERA_PROJECT_ROOT`, default `~/vera_projects`),
created on first use. Named workspaces are shortcuts to project folders and
are stored in `vera/ide/.vera_workspaces.json`.

| Capability | Route | Purpose |
|---|---|---|
| `ide.fs.roots` | `GET /ide/fs/roots` | Root mounts for the folder picker: `PROJECT_ROOT`, home, `VERA_WORKSPACE` if set, saved workspaces, and any of `/opt`, `/srv`, `/data`, `/workspace`, `/projects` that exist |
| `ide.fs.browse` | `GET /ide/fs/browse` | Directory entries plus breadcrumb data |
| `ide.fs.list` | `GET /ide/fs/list` | List a directory (optionally recursive) |
| `ide.fs.read` | `GET /ide/fs/read` | Read a file |
| `ide.fs.write` | `POST /ide/fs/write` | Write a file, creating parent directories |
| `ide.fs.delete` | `POST /ide/fs/delete` | Delete a file |
| `ide.fs.exists` | `GET /ide/fs/exists` | Existence check without an error code path |
| `ide.workspace.list` | `GET /ide/workspace/list` | Saved workspaces and inspection snapshots (`kind` = `workspace` or `snapshot`) |
| `ide.workspace.create` | `POST /ide/workspace/create` | Create a named project folder |
| `ide.session.workspace_opened` | `POST /ide/session/workspace` | Record a workspace open on the session graph and in `ide.workspaces` |
| `ide.session.file_written` | `POST /ide/session/file` | Record a file write on the graph and in `ide.file_writes` |
| `ide.session.summary` | `GET /ide/session/summary` | Summarise a session's IDE events from the memory graph |

> [!NOTE]
> When a session has an **active session sandbox** (see
> [Execution](./12-execution.md)), paths under `/workspace` are routed into the
> sandbox container instead of the host filesystem. `ide.fs.exists`,
> `ide.code.*` reads and the Run terminal all honour this routing; with no
> active sandbox they act on the host as usual.

---

## 5. Coding tools and the tool dispatcher

`ide_code_capabilities.py` adds structured editing tools an LLM can use
reliably, and a single meta-capability that enforces which tools an agent may
call.

| Capability | Route | Purpose |
|---|---|---|
| `ide.code.read_lines` | `POST /ide/code/read_lines` | Read a 1-indexed, inclusive line range |
| `ide.code.edit_lines` | `POST /ide/code/edit_lines` | Replace lines `start..end` with new content |
| `ide.code.insert_at` | `POST /ide/code/insert_at` | Insert content before a given line |
| `ide.code.grep` | `POST /ide/code/grep` | Pattern search across files under a root |
| `ide.code.replace` | `POST /ide/code/replace` | Literal or regex find/replace in one file |
| `ide.code.list_files` | `POST /ide/code/list_files` | Recursive listing that skips `.git`, `node_modules` and similar |
| `ide.code.outline` | `POST /ide/code/outline` | Symbol outline (functions, classes, …) of a source file |
| `ide.code.whitelist` | `GET /ide/code/whitelist` | `{core, extra, all}` capabilities the agent may call |
| `ide.code.whitelist_update` | `POST /ide/code/whitelist` | Change the `extra` whitelist |
| `ide.code.registry_search` | `GET /ide/code/registry_search` | Search the full capability registry when picking extra tools |
| `ide.code.tool_manifest` | `GET /ide/code/tool_manifest` | `{tools, whitelist, prompt_text}` for building an agent prompt |
| `ide.code.tool_dispatch` | `POST /ide/code/tool_dispatch` | Run one tool call on behalf of an agent |

### The whitelist

- **Core** (`_CODE_CORE`, always allowed): `ide.fs.read`, `ide.fs.write`,
  `ide.fs.exists`, `ide.fs.list`, `ide.fs.delete`, `ide.code.read_lines`,
  `ide.code.edit_lines`, `ide.code.insert_at`, `ide.code.grep`,
  `ide.code.replace`, `ide.code.list_files`, `ide.code.outline`,
  `ide.git.status`, `ide.git.diff`, `ide.git.log`.
- **Extra**: any registered capability an administrator grants (for example
  `research.search` or `web.fetch`). Persisted in
  `vera/ide/.vera_ide_whitelist.json` and editable at runtime.

The manifest exposes stable short names mapped to capabilities: `grep`,
`list_files`, `read_file`, `read_lines`, `outline`, `write_file`,
`edit_lines`, `insert_at`, `replace`, `delete_file`, `git_status`, `git_diff`.
Extra-whitelisted capabilities appear with dots replaced by underscores.

### Dispatch

`ide.code.tool_dispatch(tool, args, agent, session_id)`:

1. Resolves the short name through `_TOOL_NAME_MAP` (a full capability name is
   also accepted).
2. Refuses anything outside the whitelist, recording an `ide.tool_denied`
   memory node and fabric row.
3. Checks the capability is registered, then calls it with `args`.
4. Records the call (`ide.tool_call` or `ide.tool_error`) to the memory graph
   and the `ide.tool_calls` dataset.
5. Returns `{tool, capability, ok, result, elapsed_ms, error}`.

```bash
curl -s localhost:8999/ide/code/tool_dispatch -H 'content-type: application/json' -d '{
  "tool": "grep", "args": {"pattern": "def main", "root": "/home/me/vera_projects/demo"},
  "agent": "writer"
}'
```

---

## 6. The in-memory sandbox

For analysing existing code without risk, files can be copied into an
in-memory dict (`IDE_SANDBOX`) keyed by session. Agents read and modify the
sandbox draft; these capabilities never write to the real filesystem.

| Capability | Route | Purpose |
|---|---|---|
| `ide.sandbox.load` | `POST /ide/sandbox/load` | Copy real files into the sandbox |
| `ide.sandbox.read` | `POST /ide/sandbox/read` | Read the draft |
| `ide.sandbox.write` | `POST /ide/sandbox/write` | Write or replace a draft file |
| `ide.sandbox.list` | `POST /ide/sandbox/list` | List sandboxed files with a modified flag |
| `ide.sandbox.diff` | `POST /ide/sandbox/diff` | Unified diff of draft vs original |
| `ide.sandbox.clear` | `POST /ide/sandbox/clear` | Delete the sandbox session |

There is deliberately no promote operation for this sandbox: writing a change
back requires an explicit user action in the panel.

---

## 7. Source self-inspection (snapshots)

`ide_inspect_capabilities.py` lets Vera inspect and improve **its own source**,
but only through a copy:

1. The live source root (`SOURCE_ROOT`) is resolved at import time from the
   location of `capability_orchestration.py`.
2. Each snapshot is a full copy under
   `<PROJECT_ROOT>/__vera_inspect__/<stamp>/`.
3. A path guard wraps `ide.fs.write`, `ide.fs.delete`, `ide.code.edit_lines`,
   `ide.code.insert_at` and `ide.code.replace`, and refuses any `path` inside
   the live source tree however it was constructed (emitting
   `ide.inspect.guard_block`).
4. Promotion back to source is an explicit, two-step operation that is **not**
   reachable through `ide.code.tool_dispatch`.

Because a snapshot is an ordinary directory, every coding tool works on it
unchanged; the Workbench simply opens the snapshot as its project root.

| Capability | Route | Purpose |
|---|---|---|
| `ide.inspect.source_info` | `GET /ide/inspect/source_info` | Describe the live source tree |
| `ide.inspect.snapshot` | `POST /ide/inspect/snapshot` | Create a fresh snapshot |
| `ide.inspect.list_snapshots` | `GET /ide/inspect/snapshots` | List snapshots |
| `ide.inspect.diff_snapshot` | `POST /ide/inspect/diff` | Unified diff, snapshot vs live source (modified / added / removed) |
| `ide.inspect.delete_snapshot` | `POST /ide/inspect/delete_snapshot` | Delete one snapshot |
| `ide.inspect.prune_snapshots` | `POST /ide/inspect/prune_snapshots` | Delete old snapshots |
| `ide.inspect.promote_snapshot` | `POST /ide/inspect/promote` | Two-step write-back to live source |
| `ide.inspect.review_file` | `POST /ide/inspect/review_file` | LLM review of one snapshot file |
| `ide.inspect.plan_improvement` | `POST /ide/inspect/plan` | Thinker plans a cross-file improvement |
| `ide.inspect.scaffold_capability` | `POST /ide/inspect/scaffold_capability` | Generate a new `@capability` skeleton into a snapshot file |
| `ide.inspect.scaffold_ui_panel` | `POST /ide/inspect/scaffold_ui_panel` | Generate a panel HTML file plus its `register_ui()` call into a snapshot |
| `ide.inspect.panel_html` | `GET /ide/inspect/panel` | The Source Inspection panel |

### Promoting a snapshot

```text
1. ide.inspect.promote_snapshot(snapshot_id, dry_run=true)
     -> {plan: [{path, kind: added|modified|removed}], token}
2. ide.inspect.promote_snapshot(snapshot_id, confirm_token=<token>, files=[...optional])
     -> {written, skipped, errors}
```

The token is single-use and expires after 15 minutes. `files` narrows the
promotion to specific relative paths; empty means every changed file.

> [!TIP]
> Scaffolded capabilities and panels are written into the snapshot, never into
> live source. Review them in the snapshot, run the tests, then promote.

---

## 8. Workspace change proposals

When a loop or sandbox has edited files, the edits are not written straight
into your checkout. Instead Vera builds a **PR-style proposal** you review file
by file in the **Workspace Changes** panel.

| Capability | Route | Purpose |
|---|---|---|
| `ide.workspace.changes.propose` | `POST /ide/workspace/changes/propose` | Diff a session sandbox's changed files against your workspace folder |
| `ide.workspace.changes.propose_dir` | `POST /ide/workspace/changes/propose_dir` | General form: diff a directory of new versions against a target directory |
| `ide.workspace.changes.list` | `GET /ide/workspace/changes/list` | Proposals awaiting review |
| `ide.workspace.changes.get` | `GET /ide/workspace/changes/get` | One proposal: each file's status (added / modified / binary), diff and decision |
| `ide.workspace.changes.accept` | `POST /ide/workspace/changes/accept` | Write accepted files to the target (the gated write-back) |
| `ide.workspace.changes.reject` | `POST /ide/workspace/changes/reject` | Discard files; nothing is written |
| `ide.workspace.changes.mark_merged` | `POST /ide/workspace/changes/mark_merged` | Mark a proposal merged when it landed through git, without writing files |
| `ide.workspace.changes.panel_html` | `GET /ide/changes/panel` | The review panel |

Proposals are stored in the Redis hash `vera:ide:change_proposals`.

**Clobber safety.** Each proposal file records `base_sha`, the SHA-256 of the
target file when the proposal was built. On accept, `ws_changes_core` re-hashes
the live target and skips any file whose hash no longer matches, so a proposal
can only apply onto exactly the state it was reviewed against. Proposals built
without a `base_sha` are treated as conflicts; regenerate them.

Events: `ide.workspace.changes.proposed`, `.applied`, `.rejected`, `.merged`.

---

## 9. Git

| Capability | Route | Purpose |
|---|---|---|
| `ide.git.status` | `POST /ide/git/status` | Working-tree status for a repository path |
| `ide.git.diff` | `POST /ide/git/diff` | Diff for a repository |
| `ide.git.log` | `POST /ide/git/log` | History, optionally windowed by time; used to correlate chat sessions and Loop Lab runs with commits |
| `ide.git.branches` | `POST /ide/git/branches` | Every local branch with its last commit and worktree status |
| `ide.git.commit` | `POST /ide/git/commit` | Stage all changes and commit (emits `ide.git.commit`) |

Git commands run in a worker thread with a 30-second timeout so they never
block the event loop.

---

## 10. The IDE tab and the classic Workbench

The harness **IDE** tab mounts `vscode_panel.html` (`/ide/vscode/panel`), which
switches between five lazily loaded views:

| View | Contents |
|---|---|
| **VS Code** (default) | A code-server embedded through the same-origin proxy: the central instance, a remote code-server, or a sandbox worker, chosen from the header |
| **Workbench** | The classic custom IDE, `ide_panel.html` at `/ide/panel` |
| **Remotes & Queue** | `ide_remote_panel.html`: instance registry and provisioning, Claude Code / vera-agent console, work queue and autopilot, MCP bridge |
| **Loop Lab** | The Evolve / Loop Lab panel (`/evolve/panel`), see [Evolve](./33-evolve.md) |
| **Dispatch** | Every ingested Claude Code / Codex conversation (`/ide/claude_sessions/panel`) |

### Workbench layout

- **Tree pane** — the file tree; the root is kept in `IDE._treeRoot` and
  persisted in `localStorage`. The **Workspaces** dialog lists saved
  workspaces and creates new ones.
- **Editor** — tabbed editor with modified-indicator dots; save writes through
  `ide.fs.write`.
- **Right panel**, with these tabs:

  | Tab | Purpose |
  |---|---|
  | Chat | Direct chat with an IDE agent |
  | Run | Terminal that streams shell commands ([§12](#12-streaming-and-the-run-terminal)) |
  | Agent | The autonomous agent loop ([§11](#11-the-workbench-agent-loop)) |
  | Project Builder | Describe a project; the AI plans the file structure, then generates each file into the workspace |
  | Docs | Documentation search |
  | Version Control | Git status, diff, log and commit |
  | Tests | Run the project's tests |
  | Sandbox Policy | The shared exec sandbox policy editor |

- **Inspection drawer** — a bottom drawer that hosts the Source Inspection
  panel (snapshots, review, plan, scaffolding).
- **Tools dialog** — browse the coding tools, edit the extra whitelist, and add
  capabilities from the registry.
- **Endpoints dialog** — choose orchestrator agents or custom URLs as the
  model endpoint for each tier.

---

## 11. The Workbench agent loop

The **Agent** tab runs an LLM that works toward a goal by emitting tool calls,
observing the results and iterating. The loop runs in the panel and calls the
backend through `ide.generate` and `ide.code.tool_dispatch`.

Each cycle:

1. Builds a system prompt from the tool manifest, the goal and project context.
2. Sends the last eight observations to the selected agent tier.
3. Parses one JSON action:
   - `{"action":"call","tool":"…","args":{…},"thought":"…"}` — dispatch a tool
   - `{"action":"done","summary":"…"}` — stop and report
   - `{"action":"defer","question":"…"}` — pause and ask the user
4. Feeds the result back as an observation.

The **Cycles** field (default 8, range 1 to 200) bounds each batch. **Continue**
runs another batch with the same goal and history; **Stop** aborts. Options
include running tests and fetching docs.

### Path auto-resolution

A relative `args.path` (for example `src/foo.py`) is resolved against the tree
root, and an empty `args.root` for `grep` or `list_files` defaults to the tree
root, so the model never needs the absolute path. After a write the file tree
refreshes and any open tab for that file reloads.

### Loop-break detection

If the agent calls a read tool (`read_file`, `read_lines`, `outline`) on the
same path three or more times with no write tool (`write_file`, `edit_lines`,
`insert_at`, `replace`, `delete_file`) on that path in between, the panel
injects a synthetic observation instead of reading again:

```text
STOP. You have already read /path/to/file.py 3 times this session
without making any edits. The content you have is COMPLETE — there is
no more to read. Either:
  (a) Make an edit now using edit_lines / replace / write_file, OR
  (b) Emit {"action":"done","summary":"..."} if the goal is achieved, OR
  (c) Emit {"action":"defer","question":"..."} if you genuinely need more info.
Re-reading the same file will not change its contents.
```

This addresses the common failure where a model mistakes a truncation marker
for "there is more file to read".

### Shared run projection

Agent-loop work launched from the IDE uses the shared agent-loop output
renderer and the same content-free Run projection as Chat and Activity. Each
step shows its resolved authoring task beside the scoped capabilities (for
example "Source-file authoring → `code.author`"), derived from the capability
contract rather than another model call. Capability calls are grouped beneath
the parent agent-loop Run with lifecycle metadata only; prompts, arguments,
generated content and absolute paths stay in the IDE and loop surfaces. Files
confirmed by a save event are linked through a relative-name artifact
reference, and successful review, test or commit capabilities add evidence
links to their child Runs. A failed verification remains a failed Run and is
never promoted to passed evidence.

---

## 12. Streaming and the Run terminal

### `POST /ide/stream`

An SSE endpoint (HTTP only, not exposed over MCP) that streams tokens from a
named agent.

```javascript
const res = await fetch('/ide/stream', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({ agent: 'writer', prompt: '...', system: '...', model: '' })
});
// frames:  data: {"type":"token","text":"..."}
// final:   data: {"type":"done","text":"<full text>"}
```

Body fields: `agent`, `prompt`, `system`, `model`, `instance_id`,
`context_files`. The stream sends an initial heartbeat, watches the HTTP
disconnect signal while the model is still evaluating the prompt, and cancels
the provider task if the browser goes away before the first token. Cancellation
releases the shared model lease and records terminal metadata without
retaining prompt, system, context-file or partial-response text. Completion is
broadcast as `ide.stream_done`.

### Run terminal (`/ide-api/exec/*`)

| Route | Purpose |
|---|---|
| `POST /ide-api/exec/run` | SSE-stream a shell command: frames `{"event": "pid"\|"stdout"\|"stderr"\|"exit", "data": …}` |
| `POST /ide-api/exec/stop` | Stop a running command by pid |
| `POST /ide-api/exec/stdin` | Write to a running command's stdin |

If the session has an active sandbox, the command runs inside the container.
Otherwise it is gated by the **same exec sandbox policy** as `exec.bash.run`
(working directory, blocked commands, timeout cap). A blocked command emits
`exec.sandbox.blocked` with `shell: "ide.run"`. Lifecycle events are
`ide.run.started` and `ide.run.exited`.

The Workbench also has Docker helper routes, `POST /ide-api/docker/{build,run,stop,ping}`,
which stream through the Docker module (see [Docker](./13-docker.md)).

---

## 13. Central VS Code, the same-origin proxy and interactive workers

`vscode_capabilities.py` (group `ide.vscode.*`) makes a central code-server
running next to Vera the primary IDE surface.

| Capability | Route | Purpose |
|---|---|---|
| `ide.vscode.instances` | `GET /ide/vscode/instances` | Every embeddable surface: central, sandbox workers and all Remote-IDE instances, with proxy paths |
| `ide.vscode.central.ensure` | `POST /ide/vscode/central/ensure` | Deploy or adopt the central container on a registered Docker host |
| `ide.vscode.central.status` | `GET /ide/vscode/central/status` | Container state and upstream reachability |
| `ide.vscode.password.reveal` | `GET /ide/vscode/password/reveal` | Plaintext password for the login form or clipboard |
| `ide.vscode.password.set` | `POST /ide/vscode/password/set` | Rotate an instance's password |
| `ide.vscode.sandbox.workers` | `GET /ide/vscode/sandbox/workers` | Session sandboxes joined with any attached sidecars |
| `ide.vscode.sandbox.attach` | `POST /ide/vscode/sandbox/attach` | Start a code-server sidecar sharing a sandbox's `/workspace` |
| `ide.vscode.sandbox.detach` | `POST /ide/vscode/sandbox/detach` | Remove a sidecar (sandbox and volume untouched) |
| `ide.vscode.connect.info` | `GET /ide/vscode/connect/info` | Quick-connect details ([§16](#16-quick-connect-and-the-self-packaged-extension)) |
| `ide.vscode.extension.build` | `POST /ide/vscode/extension/build` | Package the `vera-vscode` extension as a `.vsix` |
| `ide.vscode.panel.html` | `GET /ide/vscode/panel` | The IDE tab wrapper |

### Central instance

The `vscode` compose service runs `codercom/code-server` with:

- `vera-projects` mounted at `/home/coder/projects`, the same tree
  `VERA_PROJECT_ROOT` points at, so the central IDE, the Workbench and the
  coding agents edit the same files;
- `vscode-data:/home/coder` so settings and extensions persist;
- the Docker socket, so its terminal can `docker exec -it vera-sbx-… sh` into a
  session sandbox.

`ide.vscode.central.ensure` deploys the same container (`vera-vscode`, image
`VSCODE_CENTRAL_IMAGE`, port `VSCODE_CENTRAL_PORT` default 8843) onto any
registered Docker host, or adopts the compose one. It then makes best-effort
installs of the Claude Code CLI, the extensions in `VSCODE_CENTRAL_EXTENSIONS`,
the Vera MCP bridge and, when the socket is mounted, a Docker CLI. It registers
instance `central` (kind `central`) in the shared Remote-IDE registry, so the
work queue, autopilot and `ide.remote.run` drive it like any other instance.

### Same-origin proxy

Every instance is embedded at `/vscode/{instance_id}/…`, an HTTP and WebSocket
reverse proxy inside the orchestrator. When the iframe pointed at the raw
`http://host:port`, code-server's session cookie was third-party and browsers
dropped it, so login only worked standalone. Through the proxy the cookie is
first-party and is re-scoped to `Path=/vscode/{id}/` so instances cannot
overwrite each other's cookies. `X-Frame-Options` and frame-blocking CSP
headers are stripped in transit.

### Interactive workers

`ide.vscode.sandbox.attach` starts a sidecar `vera-sbx-<session>-code` that
shares a session sandbox's `/workspace` volume, so you can work alongside Vera
in that session while her `exec` and `code` calls keep running in the sandbox
container. Sidecars register as `sbxw-<session>` (kind `sandbox-worker`), get a
proxy path and a sealed password, and publish ports from
`VSCODE_WORKER_PORT_BASE` (default 8860). The **＋ Worker** menu in the IDE
header lists sandboxes to attach, open or detach.

### Passwords

Passwords are Fernet-sealed on the instance records through
`security/secrets.py`. `ide.vscode.password.set` redeploys the central
instance and sandbox workers with the new `PASSWORD`, and for remote
`kind=code-server` hosts rewrites `~/.config/code-server/config.yaml` over SSH
and restarts code-server. The **Workers → Connections** pane lists every VS
Code instance next to the SSH credential store, with open, copy-password,
rotate and deploy-central actions.

---

## 14. Remote IDE instances, remote runs and the work queue

`ide_remote_capabilities.py` (group `ide.remote.*`) extends the IDE to remote
machines. Instance records live in `vera/ide/.vera_remote_instances.json`; the
queue and settings live in `.vera_remote_queue.json` and
`.vera_remote_settings.json` beside it.

**Instance kinds** accepted by `ide.remote.register`:

| Kind | Requires | Meaning |
|---|---|---|
| `code-server` | `host_id` | Vera installs and manages code-server on an SSH host |
| `tunnel` | `url` | A pre-existing VS Code tunnel or code-server URL |
| `ssh` | `host_id` | SSH-only host (no embedded editor) |
| `vscode-client` | — | A desktop VS Code window in client mode ([§15](#15-vs-code-client-windows-and-claude-code-auth)) |

`central` and `sandbox-worker` records are created by the `ide.vscode.*`
capabilities in the same registry.

| Capability | Route | Purpose |
|---|---|---|
| `ide.remote.instances` | `GET /ide/remote/instances` | List instances |
| `ide.remote.register` | `POST /ide/remote/register` | Create or update an instance (`kind`, `host_id`, `url`, `port` default 8080, `workdir`, `auth`, `mcp_enabled`, …) |
| `ide.remote.delete` | `POST /ide/remote/delete` | Delete an instance |
| `ide.remote.detect` | `POST /ide/remote/detect` | SSH-probe a host: OS, node, npm, `claude`, code-server, MCP bridge |
| `ide.remote.provision` | `POST /ide/remote/provision` | Install code-server (and optionally the Claude Code CLI) over SSH and start it with a generated password |
| `ide.remote.status` | `POST /ide/remote/status` | code-server up, `claude` present and signed in, bridge installed, URL reachable |
| `ide.remote.start` / `ide.remote.stop` | `POST /ide/remote/start`, `/stop` | Start/restart or stop code-server |
| `ide.remote.open` | `GET /ide/remote/open` | Embeddable URL for the panel |
| `ide.remote.run` | `POST /ide/remote/run` | Run a coding task (`engine` = `claude` or `vera-agent`) |
| `ide.remote.client.dispatch` | `POST /ide/remote/client/dispatch` | Push an action into a connected VS Code client window |
| `ide.remote.queue.add` | `POST /ide/remote/queue/add` | Enqueue a task (`instance_id` default `any`, `priority` default 5) |
| `ide.remote.queue.list` | `GET /ide/remote/queue/list` | Queue items plus autopilot state |
| `ide.remote.queue.cancel` | `POST /ide/remote/queue/cancel` | Cancel an item (a running item may still finish) |
| `ide.remote.queue.clear` | `POST /ide/remote/queue/clear` | Clear finished items, or all with `which='all'` |
| `ide.remote.autopilot` | `POST /ide/remote/autopilot` | Enable the dispatcher and set `max_concurrency` |
| `ide.remote.summary` | `GET /ide/remote/summary` | Summarise remote work for a session from the memory graph |
| `ide.remote.bridge.install` | `POST /ide/remote/bridge/install` | Deploy the MCP bridge and register it with the host's Claude Code |
| `ide.remote.bridge.status` | `POST /ide/remote/bridge/status` | Whether the bridge is installed and registered |
| `ide.remote.bridge.source` | `GET /ide/remote/bridge/source` | `vera_mcp_bridge.py` as text, for local deployment |
| `ide.remote.panel.html` | `GET /ide/remote/panel` | The Remote IDE panel |

HTTP-only routes: `POST /ide-api/remote/run` streams a run live over SSE;
`POST /ide-api/remote/client/poll` and `/ide-api/remote/client/result` are the
client-window long-poll channel.

### Engines

- **`claude`** runs the `claude` CLI headless over SSH. `remote_exec_core.build_claude_cmd`
  composes the command: every interpolated value is `shlex.quote`d, credentials
  are written to a `0600` env file and sourced (so they are not visible in the
  process list), `--resume` continues an existing session, and an MCP config
  can be passed with `--mcp-config` so the remote Claude can call back into
  Vera. The permission mode defaults to `VERA_CLAUDE_PERMISSION_MODE`
  (`acceptEdits`). `parse_claude_result` captures the Claude session id so a
  Vera-driven run joins the same session graph as an MCP-driven one.
- **`vera-agent`** runs Vera's own IDE agents (`thinker`, `writer`,
  `analyser`) editing the remote filesystem over SSH.

`ide.remote.run` returns `{ok, engine, summary, changed, elapsed_ms, claude_session_id}`
and records to `ide.remote_runs`.

### Work queue and autopilot

The dispatcher ticks every 10 seconds (`ide_remote_queue`). With autopilot on
(default off, `max_concurrency` default 1) it picks queued items and runs them
on a free matching instance; `instance_id=any` also considers live VS Code
client windows. Dream and project goals can enqueue work.

### MCP bridge

`vera_mcp_bridge.py` is a stdlib-only (Python 3.7+) stdio MCP server that
forwards `tools/list` and `tools/call` to Vera's REST MCP surface
(`GET /mcp/tools`, `POST /mcp/call`). `ide.remote.bridge.install` copies it to
`~/.vera/vera_mcp_bridge.py` and registers it:

```bash
claude mcp add vera -- python3 ~/.vera/vera_mcp_bridge.py \
    --url http://<vera-host>:<port> --caller-kind claude \
    --allow ide.,fabric.,memory.,dream.,project.
```

Codex uses the same bridge with `--caller-kind codex`, which keeps pipeline and
review attribution distinct. Run `python3 vera_mcp_bridge.py --url … --selftest`
to check connectivity without entering the stdio loop.

---

## 15. VS Code client windows and Claude Code auth

Two extensions in `tools/` reach machines Vera cannot SSH into, such as a
laptop's own VS Code:

- **`tools/vera-vscode`**, the Vera sidebar and MCP-bridge connector. In
  **client mode** (`vera.clientMode`) it registers the window as an instance of
  kind `vscode-client` and long-polls `POST /ide-api/remote/client/poll`.
  `ide.remote.client.dispatch` pushes actions into it (`open_file`,
  `run_command`, `terminal`, `type_text`, `notify`, `claude_task`), and results
  return through `/ide-api/remote/client/result`. `claude_task` runs the
  client's own `claude` CLI, so it uses whatever sign-in exists on that
  machine.
- **`tools/vscode-input-automator`**, a generic input macro panel (typing,
  terminal, command palette) for driving interactive tools by hand.

**Claude Code auth modes** (`ide.remote.register auth=api-key|subscription`):

- `api-key` (default) resolves the per-instance sealed key, then the providers
  store, then the environment, and exports `ANTHROPIC_API_KEY` for headless runs.
- `subscription` exports **no** key, because an exported key would override the
  host's `claude login` credentials. Optionally a sealed `oauth_token` (from
  `claude setup-token`) is exported as `CLAUDE_CODE_OAUTH_TOKEN`.

`ide.remote.detect` and `ide.remote.status` report `claude_login` (whether
`~/.claude/.credentials.json` exists) so the panel can show sign-in state. The
one-time interactive `claude login` can be done in the embedded code-server
terminal.

**Model selection.** `ide.remote.run` and `ide.remote.queue.add` (engine
`claude` only) take `model`, an alias (`opus` / `sonnet` / `haiku`) or a full
model id, passed as `claude --model`. Left blank, the CLI's own default
applies, which may be a model the signed-in account cannot use; the task then
fails with a credits error rather than falling back silently. The Remote IDE
console and the vera-vscode enqueue flow both prompt for it, and
`vera.clientDefaultModel` (default `opus`) is the client extension's fallback.

---

## 16. Quick-connect and the self-packaged extension

The IDE header's **🔌 Connect** button (and `ide.vscode.connect.info`) opens a
download page that wires a desktop VS Code to Vera in one step.

| Route | Serves |
|---|---|
| `GET /vscode/connect` | Landing page with one-liners and download links |
| `GET /vscode/connect/extension.vsix` | The `vera-vscode` extension, packaged in-process |
| `GET /vscode/connect/connect.ps1` | Windows quick-connect script (base URL baked in per request) |
| `GET /vscode/connect/connect.sh` | macOS/Linux quick-connect script |
| `GET /vscode/connect/cert` | Vera's TLS certificate (PEM) for the client trust store |

The one-liner (`iex (irm …/connect.ps1)` on Windows, `curl -fsSLk …/connect.sh | bash`
elsewhere) finds the `code` CLI, installs the extension, sets `vera.baseUrl`
and `vera.clientMode=true` in `settings.json`, and trusts the certificate.
After a reload the window appears in **Remotes & Queue**.

**Vera packages the `.vsix` itself.** A VSIX is an OPC zip
(`[Content_Types].xml` and `extension.vsixmanifest` at the root, the extension
under `extension/`), so `_build_vsix_bytes()` builds it with `zipfile`; no Node
or `vsce` is needed. `ide.vscode.extension.build` exposes this as
`mode="zip"` (default). `mode="vsce"` instead runs the official `@vscode/vsce`
packager in a throwaway `node:20-alpine` container.

### The webview "service worker SSL error"

> `Could not register service worker: … An SSL certificate error occurred when fetching the script.`

With `TLS_ENABLED=1`, Vera serves a self-signed certificate (generated under
`~/.vera/tls`). Browsers refuse to register a service worker in a
certificate-errored context, and code-server's webviews (Claude Code's chat UI,
notebooks, markdown preview) depend on that service worker. Trust Vera's
certificate on the client (the quick-connect script does this; the landing page
lists manual `Import-Certificate`, Keychain and `update-ca-certificates` steps),
then restart the browser. For a permanent fix point `TLS_CERTFILE` /
`TLS_KEYFILE` at a CA-signed or `mkcert` certificate.

---

## 17. Dispatch: Claude Code and Codex transcripts

`ide_claude_sessions_capabilities.py` ingests coding-agent transcripts into
the memory graph and the `ide.claude_sessions` fabric dataset, so work done in
Claude Code or Codex is searchable and visible in the **Dispatch** view.

**Sources.** The local host scans these roots (labels in brackets):

| Root | Format |
|---|---|
| `~/.claude/projects` (`home`) | Claude Code |
| `<repo>/.claude/projects` (`vera-repo`) | Claude Code |
| `<git-common-dir>/codex-sessions` (`codex-repo`) | Codex |
| `~/.codex/sessions` (`codex-home`) | Codex |
| Each path in `VERA_CLAUDE_PROJECTS_ROOTS` (`extraN`) | Claude Code |
| Each path in `VERA_CODEX_SESSIONS_ROOTS` (`codex-extraN`) | Codex |

Connected `vscode-client` windows are additional sources. `agent_transcripts.py`
parses both formats; the filename decides the parser, and Codex conversation
text is taken from `event_msg` records so the injected developer preamble never
becomes a session title.

| Capability | Route | Purpose |
|---|---|---|
| `ide.claude_sessions.sources` | `GET /ide/claude_sessions/sources` | Ingestible sources |
| `ide.claude_sessions.scan` | `GET /ide/claude_sessions/scan` | Transcript files for a source |
| `ide.claude_sessions.ingest` | `POST /ide/claude_sessions/ingest` | Tail new lines from one file |
| `ide.claude_sessions.ingest_all` | `POST /ide/claude_sessions/ingest_all` | Scan and ingest a whole source |
| `ide.claude_sessions.list_sessions` | `GET /ide/claude_sessions/list_sessions` | Sessions, most recently active first |
| `ide.claude_sessions.history` | `GET /ide/claude_sessions/history` | Full ordered turns for one session |
| `ide.claude_sessions.status` | `GET /ide/claude_sessions/status` | Files tracked and bytes consumed per source |
| `ide.claude_sessions.embed` | `POST /ide/claude_sessions/embed` | Get or set whether turns are embedded (vectors and graph nodes); default from `VERA_EMBED_CLAUDE_SESSIONS` (off) |
| `ide.claude_sessions.panel_html` | `GET /ide/claude_sessions/panel` | The Dispatch panel |

**Scheduled ingestion.** Every `VERA_CLAUDE_SESSIONS_INGEST_INTERVAL` seconds
(default 300) new transcript lines are imported immediately, so new sessions
are visible at once. Embedding of the imported rows is deferred to the
idle-time background queue.

### The idle-time background queue

The same module hosts the queue that heavier producers (session and source
embedding, dream, narrator) submit work to instead of running it directly.
Nothing starts while Vera is in active use, and a running job is pre-empted
(returned to the queue) as soon as Vera is used again.

| Capability | Route | Purpose |
|---|---|---|
| `background.status` | `GET /background/status` | Waiting and running jobs, why anything is waiting, and how long the box has been quiet |
| `background.enqueue` | `POST /background/enqueue` | Queue deferrable work (emits `background.enqueued`) |
| `background.cancel` | `POST /background/cancel` | Remove a queued job (emits `background.cancelled`) |

---

## 18. Stalled-session watch and auto-resume

`session_watch_capabilities.py` joins each ingested Claude Code session with
its board claims and pipelines, and `session_watch_core.py` (pure, unit
tested) classifies it as `live`, `stalled`, `resumable`, `declared-block`,
`finished-unreported` or `human`. The class decides the action: the most
expensive mistake is re-running finished work, so `finished-unreported` is
never resumed.

| Capability | Route | Purpose |
|---|---|---|
| `ide.claude_sessions.watch` | `GET /ide/claude_sessions/watch` | Detect and classify stalled sessions |
| `ide.claude_sessions.resume` | `POST /ide/claude_sessions/resume` | Resume a stalled session via `ide.remote.run` with `resume_session_id`; refuses `human`, `declared-block` and `finished-unreported` |
| `ide.claude_sessions.release` | `POST /ide/claude_sessions/release` | Release a dead session's open board claims back to the pool |
| `ide.claude_sessions.policy` | `POST /ide/claude_sessions/policy` | Get or set thresholds and `auto` |
| `ide.claude_sessions.autoresume` | `POST /ide/claude_sessions/autoresume` | `action=start\|stop\|status\|tick` for the auto-resume loop |

Default policy (stored overrides in Redis key `vera:session_watch:policy`;
resume counts in `vera:session_watch:attempts`):

| Setting | Default |
|---|---|
| `stalled_after_s` | 2700 (45 min) |
| `resumable_after_s` | 5400 (90 min) |
| `declared_grace_s` | 18000 (5 h) |
| `max_resume_attempts` | 2 |
| `auto` | off |

> [!WARNING]
> Auto-resume is safe by default in three layers: the loop only runs when
> started (or with `VERA_SESSION_AUTORESUME_ENABLED=1`); it only acts when the
> policy has `auto` on, otherwise it observes and emits tick events; and every
> resume is guarded. The resume target is the one registered remote instance
> whose `workdir` matches the session's project directory. If zero or several
> match, the session is skipped rather than resumed on a guessed machine. The
> tick interval is `VERA_SESSION_AUTORESUME_INTERVAL_S` (default 300, minimum 30).

---

## 19. Workspace path normalisation

Session workspaces are rooted at `/workspace`, and models often echo a path
such as `/workspace/x` or `workspace/x` back from an earlier result. Joining
that onto the root again produces `/workspace/workspace/x`, a shadow file that
diverges from the real one. [`vera/workspace_path.py`](../vera/workspace_path.py)
is the single definition both the read side and the write side use:

| Function | Behaviour |
|---|---|
| `collapse_workspace_prefix(path)` | Strip one leading `workspace/` or `/workspace/` |
| `is_absolute_workspace(path)` | True when the raw path starts with `/workspace/` |
| `should_collapse(path, repo="")` | Without a repo, any leading `workspace/` is redundant. With a repo, only an absolute `/workspace/…` is, because a relative `workspace/…` can be a real directory inside that repository |

It is used by the exec code tools and the DAG workshop's file handling.

---

## 20. Events, memory graph and fabric

Activity recorded through `_record()` lands in three places at once: a memory
graph node (chained with `FOLLOWS_ACTIVITY` so a coding session reads as one
chain, using the same session id as Chat and Research), a fabric row, and a UI
broadcast.

| Memory category | Fabric dataset | Broadcast |
|---|---|---|
| `ide.agent_prompt`, `ide.agent_response` | `ide.agent_turns` | `ide.agent_prompt`, `ide.agent_response` |
| `ide.generate` | `ide.agent_turns` | `ide.generation` |
| `ide.workspace` | `ide.workspaces` | `ide.workspace_opened` |
| `ide.file_write` | `ide.file_writes` | `ide.file_written` |
| `ide.code_edit` | `ide.code_edits` | `ide.code_edited` |
| `ide.tool_call`, `ide.tool_error`, `ide.tool_denied` | `ide.tool_calls` | `ide.tool_result`, `ide.tool_error`, `ide.tool_denied` |
| `ide.inspect.snapshot` | `ide.inspect_snapshots` | `ide.inspect.snapshot` |
| `ide.inspect.review` | `ide.inspect_reviews` | `ide.inspect.review` |
| `ide.inspect.plan` | `ide.inspect_plans` | `ide.inspect.plan` |
| `ide.inspect.promote` | `ide.inspect_promotions` | `ide.inspect.promoted` |
| `ide.inspect.scaffold_cap`, `ide.inspect.scaffold_panel` | `ide.inspect_scaffolds` | `ide.inspect.scaffold` |
| `ide.remote.instance` | `ide.remote_instances` | `ide.remote.instance` |
| `ide.remote.run`, `ide.remote.agent_step` | `ide.remote_runs` | `ide.remote.run_done`, `ide.remote.agent_step` |
| `ide.claude_session_user` | `ide.claude_sessions` | `ide.claude_session_turn` |

Other events emitted directly: `ide.agent.chat`, `ide.sandbox.load`,
`ide.fs.delete`, `ide.git.commit`, `ide.run.started`, `ide.run.exited`,
`ide.workspace.changes.{proposed,applied,rejected,merged}`,
`ide.inspect.promote_prepared`, `ide.inspect.deleted`, `ide.inspect.guard_block`,
`ide.remote.provision`, `ide.remote.autopilot`,
`ide.claude_sessions.embed_toggled`, `ide.claude_sessions.ingest_all`,
`ide.claude_sessions.autoresume.start` / `.stop`.

All fabric datasets are queryable with `fabric.query` and feed recall, so "find
code I wrote about X" works as a fabric search.

---

## 21. Configuration

| Variable | Default | Effect |
|---|---|---|
| `VERA_PROJECT_ROOT` | `~/vera_projects` | IDE project root, snapshot parent, shared with code-server |
| `VERA_WORKSPACE` | — | Extra root offered in the folder picker |
| `VSCODE_CENTRAL_CONTAINER` | `vera-vscode` | Central container name |
| `VSCODE_CENTRAL_IMAGE` | `codercom/code-server:latest` | Central image |
| `VSCODE_CENTRAL_PORT` | `8843` | Central published port |
| `VSCODE_CENTRAL_URL` | — | Explicit upstream URL for the central instance |
| `VSCODE_CENTRAL_NETWORK` | — | Docker network for the central container |
| `VSCODE_CENTRAL_EXTENSIONS` | — | Comma-separated extensions installed by `central.ensure` |
| `VSCODE_PROJECTS_VOLUME` | `vera_vera-projects` | Volume mounted as the project tree |
| `VSCODE_WORKER_IMAGE` | central image | Sandbox worker image |
| `VSCODE_WORKER_PORT_BASE` | `8860` | First port for sandbox workers |
| `VSCODE_PUBLIC_HOST` | `VERA_HOST`, else `127.0.0.1` | Host used in generated links |
| `VSCODE_PASSWORD` | — | Password for the compose central instance |
| `VERA_CLAUDE_PERMISSION_MODE` | `acceptEdits` | Default `claude` permission mode for remote runs |
| `VERA_PUBLIC_URL` | — | Base URL given to remote bridges |
| `VERA_CLAUDE_PROJECTS_ROOTS`, `VERA_CODEX_SESSIONS_ROOTS` | — | Extra transcript roots (`:` or `;` separated) |
| `VERA_EMBED_CLAUDE_SESSIONS` | `0` | Embed ingested transcript turns |
| `VERA_CLAUDE_SESSIONS_INGEST_INTERVAL` | `300` | Seconds between scheduled transcript imports |
| `VERA_SESSION_AUTORESUME_ENABLED` | off | Auto-start the auto-resume loop |
| `VERA_SESSION_AUTORESUME_INTERVAL_S` | `300` | Auto-resume tick interval (minimum 30) |
| `TLS_ENABLED`, `TLS_CERTFILE`, `TLS_KEYFILE` | — | HTTPS for the proxy and quick-connect certificate |

Local state files in `vera/ide/`: `.vera_workspaces.json`,
`.vera_ide_whitelist.json`, `.vera_remote_instances.json`,
`.vera_remote_queue.json`, `.vera_remote_settings.json`.

---

## 22. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Embedded VS Code shows a login loop | Open it through `/vscode/{id}/`, not the raw port; the proxy makes the cookie first-party |
| Webviews (Claude chat, notebooks) fail with a service worker SSL error | Trust Vera's certificate on the client ([§16](#the-webview-service-worker-ssl-error)) |
| Agent tool call returns "not whitelisted" | Add the capability with `ide.code.whitelist_update` or the Tools dialog |
| Agent keeps re-reading one file | Loop-break fires after three reads; consider a stronger model or a narrower goal |
| Run terminal prints `sandbox blocked` | The shared exec sandbox policy refused the command; edit it in the Sandbox Policy tab |
| Accept skips files in a change proposal | The target changed since the proposal was built; regenerate the proposal |
| Remote Claude task fails with a credits error | Pass an explicit `model` the signed-in account can use |
| `subscription` auth still uses an API key | Make sure no key is set on the instance; `subscription` exports none |
| Auto-resume never resumes | Policy `auto` is off by default, or no single instance `workdir` matches the session |
| Snapshot promote rejected | The token expired (15 min) or was already used; run the dry run again |

---

## See also

- [Capability Framework](./01-capability-framework.md) — registration, events and routes
- [DAG Engine](./03-dag-engine.md) — the agent loop engine and loop profiles
- [Memory Graph](./05-memory-graph.md) — where IDE activity lands
- [Data Fabric](./06-data-fabric.md) — the `ide.*` datasets
- [Research System](./07-research.md) — the code pipeline shares the agent triplet
- [Execution & Network Mapping](./12-execution.md) — exec sandbox policy and session sandboxes
- [Docker](./13-docker.md) — container hosts used by central VS Code and workers
- [Agents & Chat](./19-agents-chat.md) — the shared `<vera-agent-loop-output>` renderer
- [Security](./29-security.md) — sealed secrets used for passwords and keys
- [Evolve](./33-evolve.md) — Loop Lab, shown inside the IDE tab
- [Agent Runtimes & Providers](./36-agent-runtimes-providers.md) — Claude Code and Codex runtimes

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
