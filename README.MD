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

<span align="center">

# **Vera:** 
<!-- Yet another agentic harness (Yaass) -->
### A Distributed Capability Runtime for Autonomous Systems Orchestration, Semantic Memory, Heterogeneous LLM Execution, and Multi-source Reasoning-Action Loops.
<!-- A Self-Modifying, Multi-Agent, Orchestratration & Cognitition Architecture with Proactive Background Reflection and  Execution Enigine (SMMAC-PBR-XE)

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

Vera is built on FastAPI, with a single abstraction at its core — the `@capability` decorator — that serves as the sole registration primitive for every function in the system. From that primitive it builds a multi-backend AI platform: a heterogeneous LLM cluster, a DAG execution engine, a polyglot data fabric, a memory graph, and a harness UI that any module can extend.

![Alt text](./images/dashboard.jpg)

---

## What is a capability

A capability is the unit of work in Vera. The same decorated function is simultaneously:

- **A unit of distributed compute** — dispatchable across a worker cluster via Redis Streams
- **A tool for Veras usage** — discoverable and invokable by agentic loops and DAG planners
- **An MCP endpoint** — callable via JSON-RPC over WebSocket or REST
- **An HTTP API endpoint** — standard FastAPI route with auto-generated OpenAPI docs
- **A Python API** — directly callable in-process via the registry
- **A Redis publisher and consumer** — every call emits structured events; capabilities can subscribe to streams
- **A UI element** — schema is reflected into the harness so the function is interactively invocable
- **A bash function** — in progress

And many things in Vera *are* capabilities: skills, ontologies, DAGs, pipelines, and external MCP servers can all be defined as, or proxied through, the capability system. There is one registry, one event surface, one observability layer — regardless of where the work comes from or where it runs.

The net effect is a single-file orchestration core that turns any Python function into a fully networked, observable, retriable, integrated, distributed capability with one decorator.

---

## What it does

**LLM orchestration.** Vera manages a heterogeneous cluster of LLM instances — Ollama nodes, VLLM servers, hosted LLM APIs — with health-checking, load balancing, and automatic failover. If an active node goes down mid-request, the call transparently retries on an available node.

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
Vera/Orchestration/
├── capability_orchestration.py     # The core: @capability, DAG engine, lifespan, routes
├── capability_orchestration.html   # The harness UI (single-page, panel-aware)
├── config.py                       # Single source of truth for all backend URLs
├── cluster.py                      # LLM cluster monitor + load-aware routing (Ollama / VLLM / APIs)
├── workers.py                      # Worker registry, metrics, /cluster/jobs feed
├── capabilities.py                 # Built-in LLM/system caps (loaded at startup)
├── context.py                      # Toolkit assembly for agentic loops
│
├── data_fabric.py                  # Polyglot data fabric core
├── data_fabric_collectors.py       # Source connectors (RSS, API, DB, etc.)
├── fabric_web_acquisition.py       # Deep web crawl + entity graph
├── fabric_panel.html               # Fabric UI panel
│
├── memory.py                       # MemoryRecord + backends (Neo4j + vector)
├── memory_hooks.py                 # Session graph helpers + memory.* caps
├── memory_second_order.py          # Inferred edges (co-occurrence, similarity)
├── memory_graph_panel.html         # Session graph UI
│
├── agents.py                       # Chat agents, presets, voice, tool_mode
├── chat_panel.html                 # Conversational UI with agent loops
├── chat_panels_capabilities.py
│
├── ide_capabilities.py             # IDE sandbox, agent presets (Thinker/Writer/Analyser)
├── ide_code_capabilities.py        # Coding-agent tool dispatch + whitelist
├── ide_inspect_capabilities.py     # Source inspection, capability generation
├── ide_panel.html                  # Full IDE harness panel
│
├── research_capabilities.py        # Wraps researcher_api as @capability functions
├── research_vera_bridge.py         # researcher_api → Vera bridge (LLM + fabric)
├── research_fabric.py              # Research artifact persistence to fabric
├── research_recall_capabilities.py # Semantic recall over the research corpus
├── research_activity_capabilities.py
├── researcher_api.py               # Standalone research pipeline server (:8765)
├── research_panel.html
├── notebook_panel.html
├── nlp_panel.html
│
├── web_capabilities.py             # web.search / web.fetch / web.crawl as caps
├── cap_ontology.py                 # Pairwise cap-to-cap relationship matrix
├── cap_hub.html                    # Capability hub + cap_ontology UI
├── cap_tracking.py                 # Per-cap usage stats + rate limiting
│
├── vera_graph.js                   # Reusable graph web component (window.veraUI.Graph)
├── vera-ui.js                      # Other shared UI primitives
├── vera_panel_bridge.js            # iframe ↔ harness message bridge
│
└── session_integration.py          # Cross-module session graph integration
```

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
export REDIS_URL=redis://llm.int:6379
export POSTGRES_URL=postgresql://admin:admin@llm.int:5433/llm
export OLLAMA_GPU_URL=http://192.168.0.250:11435

# 3. Start the orchestrator
python -m Vera.Orchestration.capability_orchestration

# 4. Open the harness
# Navigate to http://localhost:8999/  (or whichever port ORCHESTRATOR_PORT is set to)
```

The orchestrator binds to `0.0.0.0:8999` by default and begins serving immediately. Backends connect lazily — anything that needs Postgres or Neo4j will wait for those backends, but core capability registration and HTTP endpoints come up right away.

---

## Defining a capability

Everything in Vera is a capability. Here's the minimum:

```python
from Vera.Orchestration.capability_orchestration import capability

@capability(
    "math.add",
    http_method="POST", http_path="/math/add", http_tags=["math"],
    description="Add two numbers. Input: a (number), b (number). Output: {sum}.",
)
async def cap_add(a: float, b: float, trace_id=None):
    return {"sum": a + b}
```

That one decoration gives you:

- `POST /math/add` — a REST endpoint with OpenAPI docs
- `math.add` listed in `GET /mcp/tools`
- Invokable via `POST /mcp/call` `{"name":"math.add","arguments":{"a":1,"b":2}}`
- Invokable over the `/ws/mcp` WebSocket
- Callable as a node in any DAG
- Every call emits `cap.call` and `cap.ok` events to the Redis event stream
- The schema is auto-derived from the function signature

The decorator accepts many other options for distributed dispatch, retries, memory recording behaviour, schema overrides, and silent (event-suppressing) mode. Read the [Capability Framework docs](./docs/01-capability-framework.md) for the full surface.

---

## Documentation

| Document | What it covers |
|---|---|
| [Capability Framework](./docs/01-capability-framework.md) | The `@capability` decorator, registry, MCP, REST, retries, distributed dispatch, schema generation |
| [Harness UI](./docs/02-harness-ui.md) | The single-page harness, panel registration, iframe mounting, themes |
| [DAG Engine](./docs/03-dag-engine.md) | DAG syntax, run modes, planner, supervised/stepwise execution, HITL streaming |
| [LLM Cluster](./docs/04-ollama-cluster.md) | Heterogeneous LLM cluster (Ollama / VLLM / APIs), load-aware routing, failover, VRAM tracking |
| [Memory Graph](./docs/05-memory-graph.md) | MemoryRecord, FOLLOWS_ACTIVITY chains, session graphs, second-order edges |
| [Data Fabric](./docs/06-data-fabric.md) | Polyglot fabric, ingestion pipeline, query DSL, entity graph, sources |
| [Research System](./docs/07-research.md) | researcher_api, 11 pipeline caps, notebook, NLP addon, recall |
| [IDE Module](./docs/08-ide.md) | Workspace, agent presets, tool dispatch, sandbox, capability generation |
| [Galaxy Graph](./docs/09-galaxy-graph.md) | `vera-graph.js` web component, layouts, action registry, live updates |
| [Configuration](./docs/10-configuration.md) | Environment variables, `cfg`, networking, backend URLs |

---

## Style and conventions

- **No bare emojis in UIs or output.** Use monochrome SVG icons or text glyphs (`⬡ ⧗ ↪ ↳ ▦`).
- **Standalone HTML panels.** Panels live as separate `.html` files served at HTTP routes, mounted via iframes from the harness — never injected as `innerHTML` (scripts wouldn't execute correctly).
- **Single-decorator architecture.** Don't reach for `@APP.get`/`@APP.post` — use `@capability` with `http_method` and `http_path`. The lifespan auto-mounts everything.
- **Dense, information-rich UI.** Controls in collapsible panels rather than spread across tabs. Monospace font for IDs and code, sans-serif for content.
- **Resilient by default.** Every backend connection retries indefinitely in the background. Capabilities should degrade gracefully when a backend is offline.

---

## License

Internal project — license terms apply per host repository.