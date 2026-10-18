---
name: write-vera-capability
description: Write a new Vera capability — @capability decorator, HTTP route, emit_event, UI panel registration. Use when asked to add a capability, add an endpoint, or extend a module.
---

# Writing Vera Capabilities

Vera capabilities are Python async functions decorated with `@capability`. One decorator handles everything: MCP registration, REST endpoint mounting, Redis event emission, schema generation, and activity recording.

## Where capabilities live

All capability modules are in `vera/` subdirectories. The orchestrator loads them at startup from a hard-coded list in `vera/capability_orchestration.py` (`_module_files = [`). **To add a new module**, append its path to `_module_files` in that list. To add to an existing module, just add the function — no registration needed beyond the decorator.

```
vera/
  capabilities/capabilities.py          # general-purpose caps (LLM, TTS, etc.)
  fabric/data_fabric.py                  # dataset / graph caps
  fabric/discovery.py                    # crawl / discovery caps
  execution/exec_capabilities.py         # shell / network caps
  <group>/<group>_capabilities.py        # pattern for new groups
```

## Minimal capability

```python
from Vera.vera.capability_orchestration import capability, emit_event, now_iso

@capability(
    "mygroup.do_thing",
    http_method="POST", http_path="/mygroup/do_thing",
    http_tags=["mygroup"],
    memory="on",
    description="One sentence. Input: param (type — description). Output: {key}.",
)
async def cap_do_thing(param: str = "", trace_id=None) -> dict:
    await emit_event({"type": "mygroup.progress", "stage": "start",
                      "message": f"doing thing with {param!r}"})
    result = {"ok": True, "param": param, "ts": now_iso()}
    return result
```

## Decorator reference

```python
@capability(
    "group.name",              # REQUIRED — dot-namespaced; group = name.split(".")[0]
    http_method="POST",        # "GET" | "POST" | "PUT" | "DELETE" — omit for MCP-only
    http_path="/group/name",   # REST path — omit for MCP-only
    http_tags=["group"],       # OpenAPI tags (default: [group])
    memory="on",               # "on" (default) | "off" — "off" skips activity recording
    silent=False,              # True suppresses cap.call/cap.ok events (polling caps)
    streams=["event.type"],    # event types to emit stream on completion
    description="...",         # REQUIRED — shown in MCP listing and cap hub
    schema={                   # optional JSON-Schema fragment to enrich auto-generated schema
        "properties": {
            "param": {"description": "...", "enum": ["a","b","c"]}
        }
    },
)
async def cap_name(..., trace_id=None) -> dict:
```

**Schema is auto-generated** from the Python type annotations — you rarely need `schema=`. The decorator reads `param: str = "default"` and builds `{"type":"string","default":"default"}` automatically. Supply `schema=` only to add `enum`, `description` per-field, or `format` constraints. For multiple-choice args prefer the `enum_schema` / `multi_enum_schema` helpers (see [Multiple-choice args](#multiple-choice-args)) over hand-writing the `properties` dict.

**`trace_id=None`** must be the last parameter of every capability function. The harness injects it; never set it yourself.

## Parameter types

| Python annotation | JSON Schema type | GET coercion |
|---|---|---|
| `str = ""` | string | raw string |
| `int = 0` | integer | `int(v)` |
| `float = 0.0` | number | `float(v)` |
| `bool = False` | boolean | `v.lower() in ("1","true","yes")` |
| `Optional[str] = None` | string, nullable | raw or None |
| `List[str] = None` | array | JSON-parse or comma-split |
| `Dict = None` | object | JSON-parse |

For GET endpoints, all params come from query string and are auto-coerced by `_make_get_handler`. For POST endpoints, the body can be flat JSON `{"key":"val"}` or the MCP envelope `{"name":"cap","arguments":{...}}`.

## Multiple-choice args

When an arg accepts a **fixed set of values**, declare a real `enum` instead of only listing options in the `description`. That single declaration flows everywhere:

- **cap-hub / auto-forms** render a `<select>` dropdown (a `<select multiple>` for multi-select) instead of a free-text box.
- **LLM planners & agent loops** see the choices in the capability signature — e.g. `tts.synthesize(text:string!, engine:string=kokoro|coqui, …)` — so the model picks a valid value instead of guessing.
- **`/mcp/tools`** exposes the enum to any external MCP consumer.

Use the helpers (imported from `capability_orchestration`) — they build the `schema=` override and are deep-merged on top of the auto-detected type/default:

```python
from Vera.vera.capability_orchestration import capability, enum_schema, multi_enum_schema

# Single-select — caller picks ONE
@capability("tts.synthesize", ...,
    schema=enum_schema(engine=["kokoro", "coqui"]))
async def cap_tts(text: str, engine: str = "kokoro", trace_id=None): ...

# Integer options work too
@capability("image.upscale", ...,
    schema=enum_schema(scale=[2, 4]))
async def cap_upscale(image_b64: str, scale: int = 4, trace_id=None): ...

# Multi-select — caller picks SEVERAL; annotate the param List[str]
@capability("dream.render", ...,
    schema=multi_enum_schema(channels=["email", "chat", "project"]))
async def cap_render(channels: List[str] = None, trace_id=None): ...
```

Guidelines:

- **Closed set only.** If the set is open-ended or dynamic (e.g. installed model files, fetched voice IDs), keep it as a free string and describe it in prose — an `enum` tells the LLM those are the *only* legal values.
- **Optional args:** a param with a default is not required, so the form adds a blank "leave default" option automatically. You don't need to add `""` to the enum.
- **Nothing is enforced server-side** — enums surface choices to UIs and the LLM; they don't reject out-of-list values, so existing callers never break.

## emit_event

Use `emit_event` to push progress to the in-page terminal and any listening UI:

```python
await emit_event({
    "type": "mygroup.progress",   # arbitrary — clients filter on this
    "stage": "running",           # short slug shown in terminal
    "message": "human-readable text",
    # add any extra fields — all go through to the client
    "count": 42,
})
```

For long-running caps, emit at each meaningful step. The terminal shows `message` for known event types. Common patterns seen in the codebase:

```python
await emit_event({"type": "mygroup.progress", "stage": "start",   "message": "..."})
await emit_event({"type": "mygroup.progress", "stage": "done",    "message": "...", "total": n})
await emit_event({"type": "mygroup.progress", "stage": "error",   "message": str(e)})
```

## GET vs POST

- **GET** — read-only, query-string params, `memory="off"` recommended, `silent=True` for polling
- **POST** — writes/mutations, JSON body, `memory="on"` by default

```python
# GET example — list something
@capability("mygroup.list", http_method="GET", http_path="/mygroup/list",
            memory="off", silent=True, description="List items. Output: {items:[...]}.")
async def cap_list(limit: int = 100, trace_id=None) -> dict:
    return {"items": [...], "count": n}

# POST example — create/mutate something  
@capability("mygroup.create", http_method="POST", http_path="/mygroup/create",
            memory="on", description="Create item. Input: name (str!). Output: {ok, id}.")
async def cap_create(name: str = "", trace_id=None) -> dict:
    if not name:
        return {"error": "name required"}
    ...
    return {"ok": True, "id": new_id}
```

## Error convention

Always return `{"error": "message"}` on validation failure — never raise. The harness and UI check for `result.get("error")`.

```python
if not required_param:
    return {"error": "required_param is required"}
```

## UI panel registration

If the capability serves an HTML panel, register it with `register_ui` after your capability functions (at module bottom, not inside a function):

```python
from Vera.vera.capability_orchestration import register_ui

register_ui(
    "my-panel",         # panel_id — must match the HTML file's `data-panel` attr
    "My Panel",         # label
    "⊞",               # icon (emoji or text)
    html=open(os.path.join(os.path.dirname(__file__), "my_panel.html")).read(),
    ui_caps=["mygroup.list", "mygroup.get"],   # capabilities the panel uses
    mode="tab",         # "inject" (default) | "tab" | "mount" | "element"
    tab_order=60,       # sort key for auto-tabs (lower = further left; default 100)
)
```

The signature is `register_ui(panel_id, label, icon, html, js="", ui_caps=None,
mode="inject", tab_order=100, specialist_agent="", specialist_loop_profile="",
specialist_context_cap="", sections=None, options=None)` (`capability_orchestration.py`).
There is no `html_path`. The modes are:
- `inject`: the Media sub-switcher;
- `tab`: its own top-level tab;
- `mount`: a pre-declared mount point;
- `element`: registered and listed but not auto-rendered.

A full-page panel must be served in an iframe (memory note on panel iframe
registration); follow the `vera-design` skill for the shell.

## Standard imports for a capability module

```python
from __future__ import annotations

import asyncio, json, logging, os, re, time, uuid
from typing import Dict, List, Optional

from Vera.vera.capability_orchestration import (
    APP,           # FastAPI app — needed only if adding raw @APP.get routes
    capability,
    emit_event,
    now_iso,
    register_ui,
    schedule,      # schedule(fn, interval, name=None, skip_in_sandbox=False, singleton=False): a PERIODIC job
)
from Vera.vera.config import cfg

log = logging.getLogger("vera.mygroup")
```

## Adding a new module

1. Create `vera/<group>/<group>_capabilities.py`
2. Add its path to the `_module_files` list in `vera/capability_orchestration.py` (search for `_module_files = [`; line numbers drift)
3. The orchestrator loads it at startup — no other wiring needed. Entry modules are loaded by path, so use absolute `Vera.vera...` imports, never relative ones (`tests/test_loader_modules_have_no_parent_package.py` guards this), and never import another loader module by a second name (a double import runs it twice and splits its state).

### Rules a new capability must follow (checked in review)

- **Periodic work:** `schedule(fn, interval, name, skip_in_sandbox=..., singleton=...)`. Pass `singleton=True` when the job touches state shared by every process (registries, worktrees, Redis keys others read), or it runs in every orchestrator. Pass `skip_in_sandbox=True` for anything that writes prod state or calls a model on a timer.
- **Background inference:** once a night, capped, `job_type="idle_*"`, `prefer_gpu=False`. Never a GPU call on a timer.
- **Effects:** declare `contract=` with `effects` (`read`, `write`, `external`, `money`, `host`) for any POST capability. Upcoming checks read it: the invocation policy (backbone B0.11) and the "safe to run unasked" predicate.
- **Secrets:** never return a stored secret value from a capability. Seal through `vera/security/secrets.py`. Put secret arguments in `redact_args`. Note that `redact_result` only hides a value from logs, events and the cache, not from the HTTP response.
- **Model calls:** go through `ollama_generate`/`ollama_embed` (or `llm.*`), never a direct POST to a node.
- **Tests:** a behaviour test that calls the function. A test that only greps the source does not count.

## Accessing other backends

Backends are loaded lazily at startup. Access them via `sys.modules` to avoid circular imports:

```python
import sys

def _fabric():
    """Get data_fabric module (loaded after this module at startup)."""
    return sys.modules.get("data_fabric")

def _memory():
    return sys.modules.get("memory")

# Usage inside capability:
fab = _fabric()
if fab:
    result = await fab.FABRIC_NEO.query("MATCH (n) RETURN n LIMIT 5")
```

For SQLite (always available in fabric caps):
```python
# Already imported in fabric modules:
from Vera.vera.fabric.data_fabric import _sqlite_conn
conn = _sqlite_conn()
rows = conn.execute("SELECT * FROM fabric_records WHERE dataset_id=?", (ds_id,)).fetchall()
```

## Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Capability name | `group.verb_noun` | `fabric.discover.crawl` |
| HTTP path | `/group/verb_noun` | `/fabric/discover/crawl` |
| Function name | `cap_verb_noun` | `cap_discover_crawl` |
| Event type | `group.noun` or `group.noun.verb` | `fabric.discover.progress` |
| Module | `<group>_capabilities.py` | `exec_capabilities.py` |

## Gotchas

- **`trace_id=None` must be last** — the harness strips it before forwarding to the function; if it's not last, `_filter_kwargs_for_func` may drop your other params.
- **Don't use `@APP.get`/`@APP.post` directly** — HTTP routes declared outside `@capability` won't be tracked, won't appear in MCP listings, and won't get activity recording. Use `@capability` with `http_method`/`http_path`.
- **GET params are strings until coerced** — `_make_get_handler` coerces from the schema, but only if `generate_schema` correctly inferred the type from your annotation. Annotate explicitly (`int`, `bool`, `float`) rather than leaving params un-annotated.
- **`memory="off"` on polling caps** — any cap called frequently (health checks, obs.*, UI panel fetches) must set `memory="off"` to avoid flooding the activity graph.
- **`silent=True` on high-frequency caps** — suppresses `cap.call`/`cap.ok` events; combine with `memory="off"` for polling caps.
- **Module load order matters for cross-references** — if your module calls a cap from `data_fabric`, it must be listed after `data_fabric.py` in `_module_files`. Use `sys.modules.get()` access pattern if uncertain.
- **`emit_event` is a no-op if Redis is down** — it swallows exceptions, so capability logic always continues. Don't use it as a synchronisation primitive.


## Coordination and autopsy

- **Working alongside other agents:** follow `vera-coordinate`. Plans, handovers and evidence go only in
  `<git-common-dir>/vera-work/shared-planning/<unit>/`. Tasks and findings go on the board (search before filing;
  comment on the related item). Status rides on the records you already write, and chat gets one line.
- **Close with an autopsy:** if a step here failed, misled or slowed you, append an entry to this skill's
  `LEARNINGS.md` (format: `skill-smith` §3). If it needs a code or tool change, file a `kind:skill-issue` board item.
