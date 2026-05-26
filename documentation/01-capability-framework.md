# 01 · Capability Framework

The `@capability` decorator is the single registration primitive in Vera. Every function in the system goes through it, and every interface (MCP, REST, DAG, WebSocket, distributed dispatch) is wired up from the registry to the decorator builds.

Skills, ontologies, DAGs, pipelines, and external MCP servers are all defined as — or proxied through — capabilities. There is one registry, one event surface, one observability layer, regardless of where the underlying work comes from.

This document is the reference for what the decorator does, how the registry is shaped, and how each interface consumes it.

---

## 1. The decorator

```python
from Vera.Orchestration.capability_orchestration import capability

@capability(
    name,
    *,
    mode        = "local",     # "local" | "distributed"
    retries     = 0,
    streams     = None,        # list[str] of streams this cap subscribes to
    description = None,
    tags        = None,
    memory      = "on",        # "on" | "off"
    silent      = False,       # suppress cap.call/cap.ok events
    schema      = None,        # optional JSON-Schema fragment to merge
    http_method = None,        # "GET" | "POST" | "PUT" | "DELETE"
    http_path   = None,        # e.g. "/dag/run"
    http_tags   = None,        # OpenAPI tags
    mcp_expose  = True,        # include in /mcp/tools listing
)
async def my_cap(arg1: str, arg2: int = 5, trace_id=None):
    return {"result": ...}
```

### What happens at decoration time

1. The decorator inspects the function signature and calls `generate_schema(func)` to derive a JSON Schema. Types come from annotations; defaults become field defaults; parameters without defaults go into `required`.
2. If a `schema` override is passed, it's deep-merged into the auto-generated one via `_merge_schema`. Use this to add `description`, `enum`, `format`, or constraints. The top-level `"type":"object"` is always set automatically — supply only `properties` and (optionally) `required`.
3. The function is wrapped to: emit `cap.call` events (unless `silent=True`), record activity to the memory graph (unless `memory="off"`), retry up to `retries` times, capture timing, and emit `cap.ok` or `cap.error` on completion.
4. The wrapped function is registered into `CAPABILITY_REGISTRY[name]` with all its metadata.
5. The HTTP route metadata is attached but **not mounted yet** — route mounting happens during lifespan startup so modules can be loaded in any order without import-time route conflicts.

### Naming convention

Names are dot-grouped: `group.subgroup.action`. The group is everything before the first dot and is used for: OpenAPI tags, activity recording skip lists, harness filtering, and stats grouping.

Common groups already in use:

| Group | Purpose |
|---|---|
| `mcp` | MCP protocol endpoints (`mcp.tools`, `mcp.call`, `mcp.register_server`) |
| `dag` | DAG planning and execution (`dag.run`, `dag.plan`, `dag.plan_and_run`) |
| `obs` | Observability (`obs.health`, `obs.workers`, `obs.cluster`) |
| `ui` | Harness UI integration (`ui.panels`, `ui.theme`) |
| `llm` | LLM routing and generation (`llm.route`, `llm.generate`) |
| `cluster` | Ollama cluster info and control |
| `memory` | Memory graph queries and writes |
| `fabric` | Data fabric ingest, query, sources |
| `web` | Web search, fetch, crawl |
| `research` | Research pipelines and recall |
| `ide` | IDE workspace, code, agents |
| `agent` | Chat agents, presets, voice |

### The `trace_id` parameter

Every cap function takes `trace_id` as a keyword argument. The decorator passes a fresh `new_id()` if the caller didn't supply one, and threads it through every emitted event so a single logical operation can be reassembled from the event stream.

---

## 2. The registry

`CAPABILITY_REGISTRY` is a single in-process dict:

```python
{
    "math.add": {
        "func":         <wrapped async function>,
        "raw":          <original async function>,
        "schema":       {"type": "object", "properties": {...}, "required": [...]},
        "description":  "Add two numbers...",
        "streams":      [],
        "mode":         "local",
        "retries":      0,
        "tags":         ["math"],
        "source":       "local",      # or "mcp_proxy" for remote caps
        "mcp_expose":   True,
        "memory":       "on",
        "silent":       False,
        "http_method":  "POST",
        "http_path":    "/math/add",
        "http_tags":    ["math"],
    },
    ...
}
```

Anything that needs to find a capability looks it up by name in this dict. The harness UI fetches the full list via `GET /mcp/tools`. The DAG engine reads schemas to validate parameter types. The agentic toolkit assembler reads descriptions and signatures.

`raw` is the unwrapped function — used internally for the distributed worker path where the wrapper's event emission and retry would conflict with the dispatch loop's own logic.

---

## 3. HTTP route mounting

Routes are mounted during the FastAPI lifespan, after all modules have loaded. This is why you can decorate first and set `cap["http_method"]` afterward — the decorator just records metadata; the lifespan handler does the actual `APP.add_api_route` calls.

A generic handler is installed per route. For `POST` it reads JSON body, maps keys to the function's kwargs, calls the cap, and returns the result. For `GET` it reads query string. Errors are converted to `HTTPException` with appropriate status codes.

You don't have to declare HTTP routes — a capability without `http_method`/`http_path` is still fully usable via MCP, the WebSocket, DAG nodes, and distributed dispatch. The HTTP route is purely convenience.

---

## 4. MCP interface

Three endpoints expose the capability set to MCP clients:

### `GET /mcp/tools`

Returns the full tool manifest:

```json
[
  {
    "name": "math.add",
    "description": "Add two numbers...",
    "schema": {"type": "object", "properties": {...}, "required": [...]},
    "mode": "local",
    "source": "local",
    "streams": [],
    "tags": ["math"]
  },
  ...
]
```

Capabilities with `mcp_expose=False` are filtered out (used for the MCP endpoints themselves, to avoid infinite recursion).

### `POST /mcp/call`

```json
{"name": "math.add", "arguments": {"a": 1, "b": 2}, "trace_id": "optional"}
```

Returns:

```json
{"type": "tool_result", "tool_name": "math.add", "trace_id": "...", "content": {"sum": 3}}
```

### `WS /ws/mcp`

The persistent WebSocket interface. On connection, the server sends:

```json
{
  "type": "connected",
  "client_id": "abcd1234",
  "capabilities": ["math.add", ...],
  "servers": {...},
  "ollama_instances": {...},
  "mode": "distributed"
}
```

Clients then send action messages:

```json
{"action": "call", "name": "math.add", "arguments": {...}, "trace_id": "..."}
{"action": "subscribe_events"}
{"action": "register_server", "url": "http://...", "name": "..."}
```

Results stream back over the same connection. The `subscribe_events` action turns the WS into a live feed of every `cap.call`, `cap.ok`, `worker.*`, `fabric.*`, etc. — this is what powers the live activity strips in the harness panels.

---

## 5. MCP proxying

Vera can proxy other MCP servers' capabilities into its own registry:

```python
await register_mcp_server("http://other-server:8000", "external")
```

This calls `GET /mcp/tools` on the remote, then registers each remote tool locally as `external.<tool_name>` with `source="mcp_proxy"`. Calls are forwarded to the remote's `/mcp/call`. The harness UI flags proxied caps with a `proxy` badge.

---

## 6. Distributed dispatch

When a capability is registered with `mode="distributed"`, calling it does this:

1. Push a task onto `vera:tasks` (Redis Stream `TASK_STREAM`) with the cap name, payload, and trace_id.
2. Create a future in this host's `PENDING_RESULTS` dict.
3. Wait for the future with a per-cap timeout (LLM caps get 240–300s, research 60s, DAG composer 600s; overridable via `_timeout` in the payload).

Worker hosts (any host that has registered the cap locally) read from `TASK_STREAM` via the `workers` consumer group, execute the cap's `raw` function, and push the result to `vera:results` (`RESULT_STREAM`).

Every host runs a `result_listener` that reads from `RESULT_STREAM` using its own hostname as consumer name. Only the host whose `PENDING_RESULTS` dict has the matching task ID resolves the future — other hosts ACK without doing anything (no-op). This means tasks can be dispatched from any host and executed on any other, transparently.

Worker state is mirrored to Redis at `vera:workers:<worker_id>` with a 120s TTL, refreshed every poll loop. The `obs.workers` cap merges Redis state with the local registry so dashboards see all hosts.

---

## 7. Event emission

Every non-silent capability call produces three events on `EVENT_STREAM` (`vera:events`) and the `vera:events:pub` pub/sub channel:

```
cap.call   { type, name, attempt, trace_id, session_id, trigger_id, trigger_cap, group }
cap.ok     { type, name, trace_id, elapsed_ms, group }
cap.error  { type, name, trace_id, error, group }  (on failure)
```

Plus, for distributed dispatch:

```
worker.start  { type, worker, task, capability }
worker.done   { type, worker, task }
worker.error  { type, worker, task, error }
```

The WebSocket `/ws/mcp` rebroadcasts all events to subscribers. Panels use this to render live activity logs, syslog feeds, and progress indicators.

---

## 8. Activity recording

When `memory="on"` (the default) and the cap has a `session_id`, the wrapper enqueues a lightweight dict onto `_ACT_QUEUE`. A background `_activity_worker` drains the queue every 2 seconds and writes:

- One **graph node** (Memory record) per cap call, with category `cap.<group>`, capturing both input and output
- A **FOLLOWS_ACTIVITY** edge linking the previous chain step to this node
- One **fabric record** in dataset `caps.<group>` for semantic recall later

Groups in `_SKIP_GROUPS` (`fabric`, `memory`, `obs`, `health`, `ui`) skip the fabric write to avoid recursion and noise — they still get graph nodes if they have session IDs.

Activity recording is fire-and-forget: the cap returns immediately while the worker handles graph and fabric writes asynchronously. If Neo4j or the fabric is down, recording silently degrades — the cap call itself never fails because of activity recording.

The `VERA_ACTIVITY_RECORDING` env var defaults to `"0"`, so the worker doesn't start unless explicitly enabled. Set it to `"1"` to turn on recording.

---

## 9. Schema overrides

The auto-generated schema captures types and defaults, but not descriptions or constraints. Use the `schema` parameter to enrich it:

```python
@capability(
    "research.run",
    http_method="POST", http_path="/research/run",
    description="Run a research pipeline.",
    schema={
        "properties": {
            "query": {
                "type":        "string",
                "description": "Natural-language research question.",
                "examples":    ["What is the impact of AI on healthcare?"],
            },
            "depth": {
                "type":    "integer",
                "minimum": 1, "maximum": 5,
                "default": 3,
            },
            "mode": {
                "type": "string",
                "enum": ["single", "parallel", "deep"],
            },
        },
    },
)
async def cap_research_run(query: str, depth: int = 3, mode: str = "single", trace_id=None):
    ...
```

The override's descriptions, enums, and constraints win; the auto-detected type and `required` list fill in anything you omit.

---

## 10. Built-in capabilities

A small set of capabilities is built into `capability_orchestration.py` itself:

| Cap | Path | Purpose |
|---|---|---|
| `mcp.tools` | `GET /mcp/tools` | Tool manifest |
| `mcp.call` | `POST /mcp/call` | Invoke any cap by name |
| `mcp.servers` | `GET /mcp/servers` | List registered MCP proxies |
| `mcp.register_server` | `POST /mcp/servers/register` | Add an MCP proxy |
| `obs.health` | `GET /health` | Overall health |
| `obs.workers` | `GET /workers` | Worker registry |
| `obs.cluster` | `GET /cluster` | Full cluster view (workers + Ollama) |
| `dag.run` | `POST /dag/run` | Execute a DAG |
| `dag.plan` | `POST /dag/plan` | LLM-generated plan |
| `dag.plan_and_run` | `POST /dag/plan_and_run` | Plan then execute |
| `llm.route` | `POST /llm/route` | Route a prompt to best instance |
| `echo` | `POST /debug/echo` | Echo with timestamp |

Plus capabilities from any companion module that loads at startup: `capabilities.py` (the full LLM group), `data_fabric.py` (`fabric.*`), `memory.py` and `memory_hooks.py` (`memory.*`), and so on.

---

## 11. Module loading

At startup, `capability_orchestration.py` scans for sibling Python files (in the same directory) matching common patterns (`*_capabilities.py`, `data_fabric*.py`, `memory*.py`, `agents.py`, etc.) and imports them. Each module that defines `@capability`-decorated functions or calls `register_ui()` registers its content as a side effect of import.

Modules can also pull in their own siblings. There's no manifest — registration is purely the side-effect of importing a module that uses the decorator.

If a module fails to import, the orchestrator logs the error and continues. The harness UI's "Loaded modules" panel shows the status of every attempted import, with the failure reason for any that crashed.

---

## 12. Common pitfalls

- **`interval=0` in the scheduler** fires every second. Don't pass 0 expecting "off" — pass a very large number like 999999, or simply don't call `schedule()`.
- **`memory="auto"`** is a legacy value, accepted for compatibility, treated as `"on"`.
- **Don't call `record_cap_interaction` directly** — it's a deprecated no-op kept only so older modules don't crash. Activity recording is handled inside the wrapper now.
- **Don't reach for `@APP.get`/`@APP.post`** for new code — use `@capability(http_method=..., http_path=...)` so the cap is uniformly available via every interface.
- **HTTP routes don't exist until lifespan startup**, so you can mutate `cap["http_method"]` after decoration if you need to wire a route conditionally based on config.

---

## See also

- [Harness UI](./02-harness-ui.md) — how the registry drives the UI
- [DAG Engine](./03-dag-engine.md) — composing capabilities into workflows
- [Ollama Cluster](./04-ollama-cluster.md) — distributed LLM dispatch