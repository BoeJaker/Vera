
# **Vera:** 
<!-- Yet another agentic harness (Yaass) -->
### A Distributed Capability Runtime for Autonomous Systems Orchestration, Semantic Memory, Agnostic LLM Execution, and Multi-source Reasoning-Action Loops.

---
> [!IMPORTANT]  
>🚀 **Help Wanted!**
>
> I'm looking for contributors to help with **Vera**.  
> 
> **Please:**
> - Clone the repo  
> - Make improvements or fixes  
> - Push them back by opening a Pull Request (PR)  
>
> **Any help is appreciated — thank you!**
>
> **Vera is still very-much in development** any issues running or using the code please post an issue, I will get back to you asap. Please don't be surprised if something doesn't work or is unfinished.
>

---

<!-- <p align="center">
<a href="#Features"><img src="https://img.shields.io/badge/✨-Features-44FFAA?style=for-the-badge"></a>
<a href="#Installation"><img src="https://img.shields.io/badge/⚡-Installation-44FFAA?style=for-the-badge"></a>
<a href="#Usage"><img src="https://img.shields.io/badge/💡-Usage-44FFAA?style=for-the-badge"></a>
<a href="#Contributing"><img src="https://img.shields.io/badge/🤝-Contributing-44FFAA?style=for-the-badge"></a>
<a href="#License"><img src="https://img.shields.io/badge/📜-License-44FFAA?style=for-the-badge"></a>
<a><img src="https://img.shields.io/badge/in_development--FF9933?style=for-the-badge&logoColor=white"></a>
<a><img src="https://img.shields.io/badge/days%20in%20development-41-blue?style=for-the-badge&logoColor=white"></a>
</p> -->
<!-- <span align="center">
A Self-Modifying, Multi-Agent, Orchestratration & Cognitition Architecture with Proactive Background Reflection and  Execution Enigine (SMMAC-PBR-XE)

Core Mechanisms: Self-Modification Engine, Multi Agent Congnition, Proactive Reflection, and Execution Engine (SMMAC-PBR-XE) -->
<!-- 

<p align="center">
  <a href="https://youtu.be/OZ_wl_s68Ho" target="_blank"><img src="https://img.youtube.com/vi/OZ_wl_s68Ho/0.jpg"></a>

</p>
<span align="center">

📺 Follow the above link for an 8 minute video overview of Vera.  

🎧 [Listen to the Podcast](https://drive.google.com/file/d/1ATDbvlXLcZzK2E6FDir5GP_R54NE07MP/view?usp=sharing) - A 40 minute deep-dive podcast discussing the architecture of Vera.   -->

<!-- </span>

</span> -->

---

>[!WARNING]
>**Vera has high system requirements**  
>The system requirement largely depend on the models you intend to use, the below are based on models the size of qwen3.5 ~11Gb
>
>**The minimum viable setup:** one orchestrator, one ollama worker, (optional 12Gb+ GPU). they can be run one one machine via docker or on independent hosts.
>
>To run a single orchestrator a minimum of 16Gb idle system RAM, 6 cores (12 hyperthreaded) running at 3Ghz+. 
>
>To run a single ollama worker: 16GB of RAM, 6 cores (12 hyperthreaded) running at 3Ghz+.
>
>**The reccomended setup:** three Ollama workers, one orchestrator, at least one worker in the pool should have a 16Gb+ GPU. 
(64gb total RAM) (18 total cores).
>
>Please check the **[System Requirements](#system-requirements)** section for more info.

>[!NOTE]  
>**Vera utilises the [Agentic-Stack-POC](https://github.com/BoeJaker/AgenticStack-POC) repository**  
> To bootstrap the various services required for vera we have built an AI development framework called **[Agentic-Stack-POC](https://github.com/BoeJaker/AgenticStack-POC)** its not required but recommended.

---

Vera is built on FastAPI, with a single abstraction at its core — the `@capability` decorator — that serves as the sole registration primitive for every function in the system. From that primitive it builds a multi-backend AI platform: a backend-agnostic LLM cluster, a DAG execution engine, a polyglot data fabric, a memory graph, and a harness UI that any module can extend.

![Alt text](https://github.com/BoeJaker/Vera/blob/main/images/dashboard.jpg)

---

## What is a capability

A capability is the unit of work in Vera. The same decorated function is simultaneously:

- **A unit of distributed compute** — dispatchable across a worker cluster via Redis Streams
- **A tool for Veras usage** — discoverable and invokable by agentic loops and DAG planners
- **An MCP endpoint** — callable via JSON-RPC over WebSocket or REST
- **An HTTP API endpoint** — standard FastAPI route with auto-generated OpenAPI documentation
- **A Python API** — directly callable in-process via the registry
- **A Redis publisher and consumer** — every call emits structured events; capabilities can subscribe to streams
- **A UI element** — schema is reflected into the harness so the function is interactively invocable
- **A bash function** — in progress

And many things in Vera *are* capabilities: skills, ontologies, DAGs, pipelines, and external MCP servers can all be defined as, or proxied through, the capability system. There is one registry, one event surface, one observability layer — regardless of where the work comes from or where it runs.

The net effect is a single-file orchestration core that turns any Python function into a fully networked, observable, retriable, integrated, distributed capability with one decorator.

---

## What it does

**LLM orchestration.** Vera manages a backend-agnostic cluster of LLM instances — Ollama nodes, VLLM servers, hosted LLM APIs — with health-checking, load balancing, and automatic failover. If an active node goes down mid-request, the call transparently retries on an available node.

**DAG execution.** Beyond simple request-response, Vera includes a DAG execution engine. Multi-step workflows are directed acyclic graphs of capabilities, executed sequentially, in parallel branches, or conditionally, threading a shared state dictionary through each node. An LLM-powered DAG planner takes a natural-language goal, inspects the available capability schemas, and produces an executable plan. A *supervised* mode inserts LLM checkpoints between steps to decide whether to continue, retry, or abort; a *stepwise* mode has the LLM plan one step at a time in an agentic loop. Human-in-the-loop approval is supported via Telegram or SSE streaming with configurable auto-approve timeouts.

**Data fabric.** The fabric discovers, ingests, verifies, and stores data. It unifies vector (FAISS + Chroma), graph (Neo4j), relational (SQLite + PostgreSQL), and object storage (Garage / Ceph) into a single ingestion pipeline and query DSL. Anything Vera produces or consumes that's worth keeping ends up in the fabric, where it can be recalled semantically, by relation, by exact filter, or by any combination of the three.

**Resilient by design.** Redis, PostgreSQL, ChromaDB, and Neo4j connections are attempted in the background with indefinite retry loops, so the server starts serving immediately even if databases aren't ready yet. Capability results are cached in Redis for state inspection, errors are captured and surfaced through a syslog feed, and an activity recording subsystem can optionally write a `FOLLOWS_ACTIVITY` chain into the memory graph for session-level traceability.

**Extensible.** Companion modules (agents, research, IDE integration, data fabric, memory, etc.) are dynamically loaded at startup from sibling files. A UI panel registry lets any module inject frontend components into the harness.

---

## Architecture at a glance

```
                          ┌────────────────────────────────┐
                          │       Vera Orchestrator         │
                          │   (capability_orchestration)    │
                          └──────────────┬─────────────────┘
                                         │
       ┌─────────────────────────────────┼─────────────────────────────────┐
       │                                 │                                 │
   ┌───▼────┐   ┌────────────┐    ┌─────▼──────┐   ┌─────────────┐   ┌────▼─────┐
   │  MCP   │   │  REST API  │    │ DAG Engine │   │  Workers /   │   │   WS    │
   │ Server │   │  (OpenAPI) │    │ + Planner  │   │ Task Streams │   │ /ws/mcp │
   └────────┘   └────────────┘    └─────┬──────┘   └──────┬───────┘   └─────────┘
                                         │                 │
                                         ▼                 ▼
                       ┌─────────────────────────────────────────────┐
                       │           CAPABILITY_REGISTRY                │
                       │   (single source of truth, all caps)         │
                       └─────────────────────┬───────────────────────┘
                                             │
       ┌─────────────────────────────────────┼────────────────────────────────┐
       │                                     │                                │
  ┌────▼────┐  ┌─────────┐  ┌─────────┐ ┌───▼────┐  ┌────────────┐  ┌───────▼────┐
  │   LLM   │  │  Data   │  │ Memory  │ │  IDE   │  │  Research  │  │   Galaxy   │
  │ Cluster │  │ Fabric  │  │  Graph  │ │ Module │  │   Module   │  │   Graph    │
  │ Ollama  │  │(polyglt)│  │ (neo4j) │ │ (agents│  │ (pipelines │  │ (vera-graph│
  │  VLLM   │  └─────────┘  └─────────┘ │ + DAGs)│  │  + recall) │  │  element)  │
  │  APIs   │                            └────────┘  └────────────┘  └────────────┘
  └─────────┘
```

---

## Repository layout

```
Vera/vera/
├── capability_orchestration.py      # The core: @capability, DAG engine, lifespan, routes
├── capability_orchestration.html    # The harness UI (single-page, panel-aware)
├── config.py                        # Single source of truth for all backend URLs
├── output_formats.py                # Shared REVIEW_STYLES / export profiles
├── agent_loop_output_capabilities.py + agent_loop_ouput.js  # <vera-agent-loop-output> renderer
├── vera_graph.js / vera-ui.js / vera_panel_bridge.js        # Shared UI primitives
│
├── capabilities/     # Built-in LLM/system caps, cap hub, per-cap usage tracking
├── core/   data/     # (reserved)
│
│   ── Compute & LLM backends ─────────────────────────────────────────────
├── workers/          # Worker registry, LLM cluster routing, jobs, syslog, docker   → 04·13·22
├── vllm/             # vLLM backend integration                                     → 21
├── execution/        # Shell / PowerShell / SSH / code exec + network mapping        → 12
├── machine learning/ # Neural-net workshop + training engine                        → 16
├── markets/          # CCXT OHLCV ingestion                                         → 15
├── mesh/             # ESP32 / edge device fleet                                     → 14
│
│   ── Knowledge & memory ─────────────────────────────────────────────────
├── fabric/           # Polyglot data fabric + memory graph + context assembly        → 05·06
├── vector browser/   # Chroma / FAISS audit & browser                               → 25
├── skills/           # Skills + domain ontologies (+ OWL)                           → 18
├── ontologies/       # Capability ↔ capability relationship matrix                  → 18
├── research/         # Research pipeline server + fabric bridge                      → 07
├── worldview/        # JEPA world-model module                                      → 11
│
│   ── Agents & cognition ─────────────────────────────────────────────────
├── agents/           # Agent personas + agentic loop                                → 19
├── chat/             # Conversational UI + panel bridge                             → 19
├── dream/            # Autonomous idle-time reflection engine + projects            → 17
├── ide/              # IDE workspace, coding agents, inspection                      → 08
├── dag/              # DAG Workshop panel + store                                   → 03
│
│   ── UI, integrations & ops ─────────────────────────────────────────────
├── elements/         # Reusable UI custom elements (flow builder, widgets)          → 20
├── ui builder/       # Theme system, panel CRUD, capability ACLs                     → 02·26
├── calendar/  email/  telegram/  accounts/   # Outward integrations                 → 23
├── security/         # Fernet-sealed secrets                                        → 29
├── web/              # web.search/fetch/crawl + Playwright browser                   → 24
├── openclaw/         # OpenClaw agentic-gateway bridge                              → 27
└── render/           # pandoc-backed document export                                → 28
```

> The `→ NN` markers point to the numbered guides in the [Documentation](#documentation) table below. `core/` and `data/` are reserved package slots. Each package registers its capabilities and UI panels as a side effect of import — there is no central manifest.

---


# Orchestrator / Worker System Requirements
14gb of idle RAM
3Ghz on 4 idle cores
20gb Storage (increases over time as data is stored in the fabric)

# Ollama Node System Requirements
A GPU node and two CPU nodes

CPU Node:
50 Gb RAM
3Ghz on 12 idle cores
500GB Storage

GPU Node:

---

## Quick start

```bash
# 1. Install dependencies
pip install fastapi uvicorn httpx redis asyncpg chromadb neo4j

# 2. Set backend URLs (or rely on defaults in config.py)
#    BACKEND_HOST is the single internal-domain knob; service URLs build on it.
export BACKEND_HOST=your-host.lan
export REDIS_URL=redis://$BACKEND_HOST:6379
export POSTGRES_URL=postgresql://admin:admin@$BACKEND_HOST:5433/llm
export OLLAMA_GPU_URL=http://192.168.0.250:11435

# 3. Start the orchestrator
python -m Vera.vera.capability_orchestration

# 4. Open the harness
# Navigate to http://localhost:8999/  (or whichever port ORCHESTRATOR_PORT is set to)
```

The orchestrator binds to `0.0.0.0:8999` by default and begins serving immediately. Backends connect lazily — anything that needs Postgres or Neo4j will wait for those backends, but core capability registration and HTTP endpoints come up right away.

---

## Defining a capability

Everything in Vera is a capability. Here's the minimum:

```python
from Vera.vera.capability_orchestration import capability

@capability(
    "math.add",
    http_method="POST", http_path="/math/add", http_tags=["math"],
    description="Add two numbers. Input: a (number), b (number). Output: {sum}.",
)
async def cap_add(a: float, b: float, trace_id=None):
    return {"sum": a + b}
```

That one decoration gives you:

- `POST /math/add` — a REST endpoint with OpenAPI documentation
- `math.add` listed in `GET /mcp/tools`
- Invokable via `POST /mcp/call` `{"name":"math.add","arguments":{"a":1,"b":2}}`
- Invokable over the `/ws/mcp` WebSocket
- Callable as a node in any DAG
- Every call emits `cap.call` and `cap.ok` events to the Redis event stream
- The schema is auto-derived from the function signature

The decorator accepts many other options for distributed dispatch, retries, memory recording behaviour, schema overrides, and silent (event-suppressing) mode. Read the [Capability Framework documentation](https://github.com/BoeJaker/Vera/blob/main/documentation/01-capability-framework.md) for the full surface.

---

## Documentation

The full guide set lives in [`documentation/`](documentation/), grouped by theme below.

**Core**

| Doc | What it covers |
|---|---|
| [01 · Capability Framework](documentation/01-capability-framework.md) | The `@capability` decorator, registry, MCP, REST, retries, distributed dispatch, schema generation |
| [02 · Harness UI](documentation/02-harness-ui.md) | The single-page harness, panel registration, iframe mounting, themes |
| [03 · DAG Engine](documentation/03-dag-engine.md) | DAG syntax, run modes, planner, supervised/stepwise execution, HITL streaming |
| [10 · Configuration](documentation/10-configuration.md) | Environment variables, `cfg`, networking, backend URLs |

**LLM & compute**

| Doc | What it covers |
|---|---|
| [04 · LLM Cluster](documentation/04-ollama-cluster.md) | Backend-agnostic LLM cluster (Ollama / VLLM / APIs), load-aware routing, failover, VRAM tracking |
| [21 · vLLM Backend](documentation/21-vllm.md) | OpenAI-compatible vLLM integration, PagedAttention, LoRA, quantisation |
| [13 · Docker](documentation/13-docker.md) | Server-side Docker backend, engine proxy, sandbox-gated lifecycle, Vera workers |
| [22 · Workers, Jobs & Syslog](documentation/22-workers-jobs-syslog.md) | Worker registry, durable job persistence/recovery, syslog feed + LLM monitor |
| [12 · Execution & Network Mapping](documentation/12-execution.md) | Sandboxed shell/PowerShell/SSH/code exec; `netscan.*` discovery + topology graph |
| [16 · Machine Learning](documentation/16-machine-learning.md) | Neural-net workshop (graph modules) + pure-NumPy training engine + datasets |
| [15 · Markets](documentation/15-markets.md) | CCXT OHLCV ingestion, watchlist, fabric-backed bars |
| [14 · Device Mesh](documentation/14-mesh.md) | ESP32 / edge fleet — durable job queue, multi-transport, telemetry → fabric |

**Knowledge & memory**

| Doc | What it covers |
|---|---|
| [05 · Memory Graph](documentation/05-memory-graph.md) | MemoryRecord, FOLLOWS_ACTIVITY chains, session graphs, second-order edges |
| [06 · Data Fabric](documentation/06-data-fabric.md) | Polyglot fabric, ingestion pipeline, query DSL, entity graph, sources |
| [25 · Vector Browser](documentation/25-vector-browser.md) | Chroma / FAISS audit, browse, dimension-alignment checks |
| [18 · Skills & Ontologies](documentation/18-skills-ontologies.md) | Prompt skills, domain ontologies (+ OWL), the capability-relationship matrix |
| [07 · Research System](documentation/07-research.md) | researcher_api, pipeline caps, notebook, NLP addon, recall |
| [11 · Worldview](documentation/11-worldview.md) | The JEPA world-model module |

**Agents & cognition**

| Doc | What it covers |
|---|---|
| [19 · Agents & Chat](documentation/19-agents-chat.md) | Agent personas, `tool_mode`/`domain_caps`, the V4 agentic loop, shared renderer, voice |
| [17 · Dream](documentation/17-dream.md) | Autonomous idle-time reflection — triggers, sensors, stages, HITL, reviews |
| [08 · IDE Module](documentation/08-ide.md) | Workspace, agent presets, tool dispatch, sandbox, capability generation |

**UI, integrations & ops**

| Doc | What it covers |
|---|---|
| [09 · Galaxy Graph](documentation/09-galaxy-graph.md) | `vera-graph.js` web component, layouts, action registry, live updates |
| [20 · Flow Builder & UI Elements](documentation/20-flow-builder.md) | The reusable `<vera-flow-builder>` element + provider pattern |
| [26 · UI Builder](documentation/26-ui-builder.md) | Theme system, runtime panel CRUD, capability ACL scopes |
| [23 · Integrations](documentation/23-integrations.md) | Calendar, Email, Telegram, and the shared Accounts registry |
| [24 · Web & Browser](documentation/24-web-browser.md) | `web.search/fetch/crawl` + Playwright browser automation |
| [27 · OpenClaw](documentation/27-openclaw.md) | Bridge to an external OpenClaw agentic gateway |
| [28 · Render](documentation/28-render.md) | pandoc-backed document export (DOCX/PDF/…) |
| [29 · Security & Secrets](documentation/29-security.md) | Fernet sealing for stored credentials |

---

## Style and conventions

<!-- - **No bare emojis in UIs or output.** Use monochrome SVG icons or text glyphs (`⬡ ⧗ ↪ ↳ ▦`). -->
- **Standalone HTML panels.** Panels live as separate `.html` files served at HTTP routes, mounted via iframes from the harness — never injected as `innerHTML` (scripts wouldn't execute correctly).
- **Single-decorator architecture.** Don't reach for `@APP.get`/`@APP.post` — use `@capability` with `http_method` and `http_path`. The lifespan auto-mounts everything.
- **Dense, information-rich UI.** Controls in collapsible panels rather than spread across tabs. Monospace font for IDs and code, sans-serif for content.
- **Resilient by default.** Every backend connection retries indefinitely in the background. Capabilities should degrade gracefully when a backend is offline.

---

## License

Internal project — license terms apply per host repository.

---

<!-- BEGIN GETTING STARTED (managed by tools/scaffold.py) -->

## Getting Started

Vera can run two ways: the **full docker stack** (everything wired up for you) or
a **native** orchestrator process talking to backends you host elsewhere.

### Option A — Docker (everything in one command)

```bash
cp .env.example .env          # then edit secrets/hosts
make secret                   # generate VERA_SECRET_KEY -> paste into .env
make up                       # start vera + redis + postgres + chromadb + neo4j
make logs                     # watch it boot
```

Or without `make`:

```bash
# Linux / macOS
./build.sh up

# Windows
.\build.ps1 up
```

Then open the harness at **http://localhost:8999/** and the API docs at **http://localhost:8999/docs**.

### Option B — Native (no docker)

You supply Redis / Postgres / Chroma / Neo4j (or let caps degrade gracefully —
core capability registration and HTTP come up even without backends).

```bash
make venv                     # create .venv + install requirements
make run                      # python -m Vera.vera.capability_orchestration
```

`make run` sets `PYTHONPATH` to the repo's parent so the `Vera.vera.capability_orchestration`
package resolves, and binds `0.0.0.0:8999`.

### Verify it's alive

```bash
make health                   # GET /health
make caps                     # GET /mcp/tools  (list every capability)
```

Or invoke a capability directly:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"echo","arguments":{"message":"hello"}}'
```

### Explore interactively

| What | How |
|---|---|
| **Guided terminal tour** | `make tour` (or `python welcome/welcome.py`) |
| **HTML welcome guide** | `make welcome` — opens `welcome/index.html` |
| **Quickstart notebook** | `make notebook` — boot & test caps from Jupyter |
| **Swagger API docs** | `make docs` — opens `http://localhost:8999/docs` |

### Common commands

| Command | Action |
|---|---|
| `make up` / `make down` | start / stop the docker stack |
| `make build` | rebuild the vera image and start |
| `make logs` | follow orchestrator logs |
| `make run` | run the orchestrator natively |
| `make health` / `make caps` | smoke-test a running instance |
| `make nuke` | stop **and delete volumes** (destructive) |


<!-- END GETTING STARTED -->
