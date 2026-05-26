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

`ide_panel.html` is the harness IDE tab. Sections:

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

## See also

- [Capability Framework](./01-capability-framework.md) — the underlying registration system
- [Memory Graph](./05-memory-graph.md) — where IDE activity lands
- [DAG Engine](./03-dag-engine.md) — the agentic loop is a stepwise DAG
- [Research System](./07-research.md) — the code pipeline shares the agent triplet