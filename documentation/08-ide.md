# 08 · IDE Module

Vera's IDE module is a full in-harness coding environment: file tree, editor with tabs, three specialised LLM agents, a sandboxed source-inspection drawer, and an agentic tool-dispatch loop that lets an agent autonomously work on a goal across multiple files. Everything is wired to capabilities, observable via events, and recorded to the memory graph and data fabric.

The IDE consists of four modules and one panel:

- `ide_capabilities.py` — workspace, agent presets, sandbox, generation
- `ide_code_capabilities.py` — coding-agent tool dispatch + whitelist
- `ide_inspect_capabilities.py` — source inspection, review, capability generation
- `ide_panel.html` — the harness panel
- `agents.py` — chat-agent integration (the same agent presets are reused)

---

## 1. The three agents

```
┌──────────────────┬─────────────────────────────────────────────────────────┐
│  Thinker         │  High-level reasoning, planning, architectural analysis  │
│  GPU node        │  temperature 0.75, top_p 0.92, 16K context               │
│                  │  System prompt: senior software architect                │
├──────────────────┼─────────────────────────────────────────────────────────┤
│  Writer          │  Code generation, scaffolding, refactoring               │
│  CPU node A      │  Code-tuned model, medium temp, 8K context               │
│                  │  System prompt: professional code generator              │
├──────────────────┼─────────────────────────────────────────────────────────┤
│  Analyser        │  Review, debug, explain                                  │
│  CPU node B      │  Low temp (deterministic), 4K context                    │
│                  │  System prompt: code reviewer                            │
└──────────────────┴─────────────────────────────────────────────────────────┘
```

Presets are defined in `_AGENT_PRESETS` in `ide_capabilities.py`:

```python
IDE_AGENT_THINKER  = "ide-thinker"
IDE_AGENT_WRITER   = "ide-writer"
IDE_AGENT_ANALYSER = "ide-analyser"
```

The presets are auto-seeded on startup and registered in the same `AGENTS` table as the chat agents — they share the unified agent surface (`agent.list`, `agent.chat`, `agent.create`, etc.). Each preset has a fixed `tool_mode`, `prefer_gpu`, `temperature`, `top_p`, `num_ctx`, and `system_prompt`.

### Tier mapping

`ide.instances` returns each Ollama instance tagged with a tier label (`thinker`, `writer`, `analyser`) derived from `has_gpu` and the instance ID convention. This is used by the panel to show which tier is running on which node.

### Generation

| Cap | Purpose |
|---|---|
| `ide.agent.list` | Three presets in their current state |
| `ide.agent.chat` | One-shot chat against an agent |
| `ide.instances` | Per-tier instance routing info |
| `ide.models` | Models available across online instances |
| `ide.generate` | Raw generation through a named agent |
| `ide.stream` | SSE token stream (HTTP-only, not exposed via MCP) |

---

## 2. Workspace and filesystem

The IDE works against a real workspace on disk. Workspaces are mounted from `cfg.IDE_PROJECTS_ROOT` (default created on first use).

### Filesystem caps

| Cap | Purpose |
|---|---|
| `ide.fs.list` | List directory contents |
| `ide.fs.read` | Read a real file (read-only mount of real FS) |
| `ide.fs.write` | Write a real file |
| `ide.fs.delete` | Delete a real file |
| `ide.fs.exists` | Stat check |

### Code editing caps

| Cap | Purpose |
|---|---|
| `ide.code.read_lines` | Read a line range from a file |
| `ide.code.edit_lines` | Replace a line range with new content |
| `ide.code.insert_at` | Insert at a specific line |
| `ide.code.grep` | Regex search across files |
| `ide.code.replace` | Find/replace in a file (or across the tree) |
| `ide.code.list_files` | Tree listing under a root |
| `ide.code.outline` | Functions/classes outline for a file (Python AST + light multi-language) |

### Git caps

| Cap | Purpose |
|---|---|
| `ide.git.status` | Working tree status |
| `ide.git.diff` | Diff for a path |
| `ide.git.log` | Commit history |
| `ide.git.commit` | Commit staged changes |

All filesystem operations record to the memory graph (`ide.file_*` categories) and the fabric (`ide.workspaces`, `ide.generated`).

---

## 3. The sandbox

For source-inspection workflows (analysing existing code), the IDE supports a sandboxed view. Real files are copied into an in-memory dict (`IDE_SANDBOX`) per session. Agents can read and modify the sandbox but **never** touch the real filesystem from these caps.

| Cap | Purpose |
|---|---|
| `ide.sandbox.load` | Copy real files into the sandbox |
| `ide.sandbox.read` | Read from the sandbox draft |
| `ide.sandbox.write` | Write to the sandbox draft (sandbox-only) |
| `ide.sandbox.list` | List sandboxed files with modified flag |
| `ide.sandbox.diff` | Unified diff: draft vs original |
| `ide.sandbox.clear` | Wipe the sandbox session |

There's deliberately no `promote` operation that would flush a sandbox back to disk — the design keeps real source safe by separation, requiring an explicit user action (the panel's apply button) to write changes.

---

## 4. The inspection drawer

`ide_inspect_capabilities.py` provides higher-level review and planning over a snapshot:

| Cap | Purpose |
|---|---|
| `ide.inspect.snapshot_create` | Create a snapshot of files for review |
| `ide.inspect.outline` | Build a functions+classes outline of the snapshot |
| `ide.inspect.review_file` | Have an agent review one file (returns issues, opportunities, strengths) |
| `ide.inspect.plan_improvement` | Have the Thinker plan a cross-file improvement |
| `ide.inspect.generate_capability` | Generate a brand-new `@capability` from a spec |

`generate_capability` is the auto-generator: given a cap name, function name, HTTP path, summary, and input/output hints, it produces a complete Python file matching the project's exemplar pattern. Validates the output to reject placeholder-laden descriptions and refuses to ship until the result parses with `python3 -c "import ast; ast.parse(...)"`.

---

## 5. The coding-agent tool dispatch

The IDE's most powerful feature is the **agentic loop**: an LLM agent that works autonomously toward a goal by emitting tool calls, observing results, and iterating.

### Tool manifest

`ide.code.tool_manifest` returns the toolkit available to the agent:

- **Core tools** — always allowed: `read_file`, `write_file`, `read_lines`, `edit_lines`, `insert_at`, `grep`, `replace`, `list_files`, `outline`, `exists`, `delete_file`, `git_status`, `git_diff`, `git_log`.
- **Extra whitelist** — additional caps the admin has granted: anything from the full `CAPABILITY_REGISTRY` (e.g. `research.search`, `web.fetch`).
- **Prompt text** — a system-prompt snippet enumerating all allowed tools with their schemas.

The whitelist is persisted in `_vera_ide_whitelist.json` and mutable at runtime via `ide.code.whitelist_update`.

### Tool dispatch

`ide.code.tool_dispatch` is the meta-capability the agent calls. It:

1. Looks up the tool short name in `_TOOL_NAME_MAP` (or accepts a full cap name from the whitelist).
2. Verifies it's in the allowed list — refuses anything else.
3. Calls the underlying capability with the supplied args.
4. Records the call to the memory graph (so agent activity is visible).
5. Returns `{tool, capability, ok, result, elapsed_ms, error}`.

### The loop

In `ide_panel.html`, the agent loop:

1. Builds a system prompt from the manifest + goal + project context.
2. Sends to the selected agent (Thinker for planning, Writer for implementation).
3. Parses one of:
   - `{action:"call", tool:"...", args:{...}, thought:"..."}` — dispatch the tool
   - `{action:"done", summary:"..."}` — stop and report
   - `{action:"defer", question:"..."}` — pause and ask the user
4. Feeds the result back as an observation and re-prompts.
5. Detects read-loops (3+ reads of the same file with no edits in between) and force-breaks them with an explicit instruction to act.
6. Auto-resolves relative paths against the workspace root.
7. Refreshes the file tree when a write occurs and reloads any open tab whose file changed.

The loop runs for `maxSteps` cycles (default 8) or until done/defer/abort. A "Continue" button extends with another batch without resetting state.

---

## 6. The IDE panel

The harness **IDE tab** now mounts a merged wrapper (`vscode_panel.html`, served
at `/ide/vscode/panel`) with three views:

- **VS Code** *(default)* — a real code-server embedded through the same-origin
  proxy (see §12): the central instance, any remote code-server, or a sandbox
  interactive worker, switchable from the header dropdown.
- **Workbench** — the original custom IDE (`ide_panel.html`, everything below
  in this section) — unchanged and lazily loaded.
- **Remotes & Queue** — the old Remote-IDE panel (`ide_remote_panel.html`):
  instance registry/provisioning, the Claude-Code/vera-agent console, the
  autonomous work queue + autopilot, and the MCP bridge — merged into the IDE
  tab instead of a separate top-level tab.

`ide_panel.html` is the classic workbench view. Sections:

### Tree pane

File tree on the left. Click a folder to expand, click a file to open it as a tab. The tree root is set via `IDE._treeRoot`, persisted in localStorage.

### Editor

Tab-bar at top showing open files. Multi-tab support with modified-indicator dots. Save: writes to real FS via `ide.fs.write`.

### Right drawer

Tabs:

- **Agent** — the agentic loop UI (goal box, Run button, log, cycles counter)
- **Chat** — direct chat against an agent without the loop
- **Outline** — outline of the current file
- **Inspector** — source inspection (snapshot create, review file, plan improvement)
- **Tools** — agent tool manifest viewer (core / extra / browse-add)
- **Snapshots** — list past snapshots with review results
- **Templates** — scaffold a new project from a template (auto-runs the Writer)

### Tool modal

The "Tools" modal lets you browse all allowed coding tools, view their schemas, and (in the Browse tab) add new capabilities to the whitelist by name.

### Scaffold flow

The scaffold button starts a project-bootstrap conversation: Thinker plans, Writer implements file-by-file, with the panel showing each file as it's created. This is essentially the `research.code` pipeline run interactively in the IDE.

---

## 7. Auto-resolution and loop-breaks

Two patterns make the agentic loop robust in practice:

### Path auto-resolve

If the agent emits `args.path = "src/foo.py"` (relative), the panel prepends the tree root to make it absolute. If it emits `args.root` empty for `grep`/`list_files`, the tree root is used. This means the agent doesn't have to know or include the full absolute path everywhere.

### Loop-break detection

If the agent calls a read tool on the same path 3+ times without any write tool calls in between on that path, the panel injects a synthetic observation telling the agent to **stop reading and act**:

```
STOP. You have already read /path/to/file.py 3 times this session
without making any edits. The content you have is COMPLETE — there is
no more to read. Either:
  (a) Make an edit now using edit_lines / replace / write_file, OR
  (b) Emit {"action":"done","summary":"..."} if the goal is achieved, OR
  (c) Emit {"action":"defer","question":"..."} if you genuinely need more info.
Re-reading the same file will not change its contents.
```

This is the single biggest fix for "agent reads file forever" behaviour that LLMs default to — the model mistakes a `...` truncation marker for "there's more file to read." The truncation marker has been switched to an explicit, machine-recognisable form to reduce the misread, and the loop break catches anything still loops.

---

## 8. Streaming

`ide.stream` is an SSE endpoint (HTTP-only, not MCP-exposed) that streams tokens from a named agent:

```javascript
const res = await fetch('/ide/stream', {
  method: 'POST',
  body: JSON.stringify({ agent: 'writer', prompt: '...', system: '...', model: '...' })
});
const reader = res.body.getReader();
// chunks arrive as: data: {"type":"token","text":"..."}
// final: data: {"type":"done"}\n\ndata: [DONE]
```

The panel uses this for the chat tab and the inspector's review streaming. Internally it calls `pick_instance()` and routes through Vera's cluster.

---

## 9. Memory graph integration

Every IDE event is recorded:

- `ide.workspace.open` — workspace opened
- `ide.file_written` — file modified
- `ide.agent_turn` — chat against an agent
- `ide.inspect.review` — file reviewed
- `ide.code.tool_dispatch` — agent tool call (records the tool, args, result, elapsed time)

These chain via FOLLOWS_ACTIVITY so a multi-step coding session appears as a single linear chain in the memory graph panel. The session ID is shared across the entire harness — the IDE's session is the same as Chat's, Research's, etc.

---

## 10. Fabric integration

Workspace metadata goes to `ide.workspaces`. Files written by agents go to `ide.generated`. Inspection reviews to `ide.inspect_reviews`. Capability auto-generations to `ide.generated_capabilities`. These datasets are queryable via `fabric.query` and feed back into recall — "find me code I've written about X" works as a fabric semantic search.

---

## 11. Capability auto-generation

The `ide.inspect.generate_capability` cap is the workflow for creating new caps:

1. Provide cap name, function name, HTTP path, summary, optional input/output hints.
2. The Writer generates Python source matching the project's exemplar.
3. Output is validated by AST parse; placeholder-only descriptions are rejected.
4. The generated file is written to disk (in a `generated/` folder, not the main module path) and shown in the IDE panel for review.

This is the bootstrap path for growing the system — sketch a capability in English, let the IDE write the code, review, then move the file into the live module path.

---

## 12. Central VS Code, the same-origin proxy, and interactive workers

`vscode_capabilities.py` (group `ide.vscode.*`) makes a **central code-server
running next to Vera** the primary IDE surface.

### Central instance

The `vscode` compose service runs `codercom/code-server` with:

- `vera-projects` mounted at `/home/coder/projects` — the exact tree
  `VERA_PROJECT_ROOT` points at, so the central IDE, the classic workbench and
  the coding agents all edit the same files;
- `vscode-data:/home/coder` so settings/extensions persist;
- the docker socket mounted so its integrated terminal can
  `docker exec -it vera-sbx-… sh` into any session sandbox.

`ide.vscode.central.ensure` deploys the same container onto any registered
Docker host (native/non-compose runs), or *adopts* the compose one, and then
best-effort installs the **Claude Code CLI**, extensions from
`VSCODE_CENTRAL_EXTENSIONS`, the **Vera MCP bridge** (so Claude Code inside the
container can call Vera capabilities) and, when the socket is mounted, a docker
CLI. It registers instance `central` in the shared Remote-IDE registry, so the
work queue / autopilot / `ide.remote.run` drive it like any other instance.

### Same-origin proxy — the iframe-login fix

Every instance is embedded via `/vscode/{instance_id}/…`, a reverse proxy
(HTTP + WebSocket) inside the orchestrator. code-server's session cookie was
**third-party** when the iframe pointed at the raw `http://host:port` URL, so
browsers dropped it and login only worked standalone. Through the proxy the
cookie is first-party (and re-scoped to `Path=/vscode/{id}/`, so instances
can't clobber each other), which makes in-page login work for the central
instance *and* every remote code-server. Frame-blocking headers
(`X-Frame-Options`, CSP) are stripped in transit.

### Interactive workers (sandbox sidecars)

`ide.vscode.sandbox.attach` starts a code-server sidecar
(`vera-sbx-<session>-code`) sharing a session sandbox's `/workspace` volume —
you work alongside Vera in that session's filespace while her `exec`/`code`
calls keep running inside the sandbox container itself. Sidecars register as
`sbxw-<session>` (kind `sandbox-worker`), get a proxy path and a sealed
password, and are detached with `ide.vscode.sandbox.detach` (sandbox + volume
untouched). The `＋ Worker` menu in the IDE header lists sandboxes to
attach/open/detach.

### Password management

Passwords are Fernet-sealed on the instance records (`security/secrets.py`):

- `ide.vscode.password.reveal` — plaintext for the login form / clipboard
  (IDE header 🔑, Workers → Connections ⧉ pw);
- `ide.vscode.password.set` — rotate: central + sandbox workers are redeployed
  with the new `PASSWORD`, remote `kind=code-server` hosts get
  `~/.config/code-server/config.yaml` rewritten + restarted over SSH.

The **Workers → Connections** pane lists all VS Code instances next to the SSH
credential store, with Open (proxy), copy-password, rotate and deploy-central
actions.

---

## 13. VS Code client windows & Claude Code auth

Two extensions in `tools/` extend the remote system to machines Vera can't SSH
into (a laptop's own VS Code):

- **`tools/vera-vscode`** — the Vera sidebar + MCP-bridge connector. In
  **client mode** (`vera.clientMode`) it registers the window as instance
  `kind=vscode-client` and long-polls `POST /ide-api/remote/client/poll`;
  `ide.remote.client.dispatch` pushes actions into it (`open_file`,
  `run_command`, `terminal`, `type_text`, `notify`, `claude_task`), results
  come back via `/ide-api/remote/client/result` and resolve the dispatcher's
  future. The work queue picks live client windows for `instance_id=any`
  items, and `claude_task` runs the **client's own** `claude` CLI — so it
  uses whatever sign-in exists on that machine.
- **`tools/vscode-input-automator`** — a generic input macro panel (typing,
  terminal, command palette), useful for driving interactive tools manually.

**Claude Code auth modes** (`ide.remote.register auth=api-key|subscription`):
`api-key` (default) resolves per-instance sealed key → providers store → env
and exports `ANTHROPIC_API_KEY` for headless runs. `subscription` exports
**nothing** (an exported key would override the host's `claude login`
credentials); optionally a sealed `oauth_token` (from `claude setup-token`)
is exported as `CLAUDE_CODE_OAUTH_TOKEN`. `ide.remote.detect` / `.status`
report `claude_login` (whether `~/.claude/.credentials.json` exists) so the
panel can show sign-in state. The one-time interactive `claude login` can be
done through the embedded code-server terminal.

**Model selection**: `ide.remote.run`/`ide.remote.queue.add` (engine=claude
only) take a `model` param — an alias (`opus`/`sonnet`/`haiku`) or full model
id — passed as `claude --model`. Left blank, the CLI's own default is used,
which can be a paid/credit-gated model the signed-in account has no
entitlement for (the task then fails with a credits error instead of quietly
falling back). The Remote IDE panel's Console tab and the vera-vscode
sidebar's enqueue flow both prompt for it; `vera.clientDefaultModel`
(default `opus`) is the client extension's own fallback when a dispatched
`claude_task` doesn't specify one.

## 14. Quick-connect (download-and-run) + self-packaged extension

The IDE panel's **🔌 Connect** button (and `ide.vscode.connect.info`) opens a
download page at `GET /vscode/connect` that wires a user's **desktop** VS Code
to Vera in one shot — no manual `vsce`, no marketplace, no SSH:

| Route | Serves |
|---|---|
| `GET /vscode/connect` | Landing page: copy-paste one-liners + download links |
| `GET /vscode/connect/extension.vsix` | The `vera-vscode` extension, **packaged in-process** |
| `GET /vscode/connect/connect.ps1` | Windows quick-connect script (base URL baked in per-request) |
| `GET /vscode/connect/connect.sh` | macOS/Linux quick-connect script |
| `GET /vscode/connect/cert` | Vera's TLS cert (PEM) for the client trust store |

The one-liner (`iex (irm …/connect.ps1)` on Windows, `curl -fsSLk …/connect.sh
| bash` on Unix) finds the `code` CLI, installs the extension, sets
`vera.baseUrl` + `vera.clientMode=true` in `settings.json`, and trusts the cert
— after a reload the window appears in **Remotes & Queue** and Vera can queue
Claude Code tasks straight into it (via the §13 client-dispatch channel).

**Vera packages the `.vsix` itself.** A VSIX is just an OPC zip
(`[Content_Types].xml` + `extension.vsixmanifest` at the root, the extension
under `extension/`), so `vscode_capabilities._build_vsix_bytes()` builds a valid
package with `zipfile` — no Node, no `vsce`, no container. `code
--install-extension` accepts it directly. `ide.vscode.extension.build` exposes
this (`mode="zip"`, default); `mode="vsce"` is an optional path that spins up a
throwaway `node:20-alpine` container and runs the official `@vscode/vsce`
packager when you want a canonical package (source injected as a base64 tar.gz,
result read back as base64 — the extension is tiny so both fit the exec cap).

### The webview "service worker SSL error"

> `Could not register service worker: … An SSL certificate error occurred when
> fetching the script.`

The browser IDE is code-server behind Vera's same-origin proxy over
**self-signed HTTPS** (`TLS_ENABLED=1`, cert auto-generated at `~/.vera/tls`).
Browsers refuse to register a service worker in a *cert-errored* secure context,
and code-server's **webviews** (Claude Code's chat UI, notebooks, markdown
preview, …) all depend on that service worker — so they fail to load even though
the main page rendered after you clicked through the cert warning.

The fix is to **trust Vera's certificate** on the client machine (the
quick-connect script does this automatically; the landing page lists the manual
`Import-Certificate` / Keychain / `update-ca-certificates` steps), then restart
the browser. A real CA-signed or `mkcert` certificate for the host removes it
permanently and is the recommended long-term fix — point `TLS_CERTFILE` /
`TLS_KEYFILE` at it.

---

## See also

- [Capability Framework](./01-capability-framework.md) — the underlying registration system
- [Memory Graph](./05-memory-graph.md) — where IDE activity lands
- [DAG Engine](./03-dag-engine.md) — the agentic loop is a stepwise DAG
- [Execution & Network Mapping](./12-execution.md) — the exec sandbox the IDE **Run** action shares
- [Agents & Chat](./19-agents-chat.md) — the shared `<vera-agent-loop-output>` renderer
- [Research System](./07-research.md) — the code pipeline shares the agent triplet

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
