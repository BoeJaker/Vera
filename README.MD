# Vera

### A capability runtime for building, operating, and improving AI systems

Vera turns ordinary Python functions into distributed, observable tools that
agents, people, APIs, workflows, and external MCP clients can all use. One
`@capability` decorator connects a function to the registry, HTTP, MCP, the
interactive harness, event streams, workers, and Vera's planning engines.

That simple centre supports a much larger system: local and hosted models,
agentic loops, DAG workflows, semantic memory, a polyglot data fabric, browser
operation, remote development, hardware nodes, and Loop Lab—Vera's isolated,
reviewed self-improvement workflow.

> [!NOTE]
> Vera is an ambitious, actively developed system. The core runtime is usable,
> but some integrations and studios are experimental. Issues, focused pull
> requests, documentation improvements, and reproducible bug reports are very
> welcome.

![Vera main dashboard](documentation/assets/overview/dashboard.png)

## Why Vera is different

A Vera capability is simultaneously:

- a Python function callable in-process;
- an HTTP endpoint with a generated schema;
- an MCP tool for Claude, Codex, and other clients;
- a distributed job that can run on a worker;
- a building block for DAGs and agentic loops;
- an observable event source with trace and provenance data; and
- an interactive action in Vera's web harness.

There is one registry and one execution contract. A tool does not need separate
implementations for the UI, API, agent runtime, and worker cluster.

## What you can build

| Area | What Vera provides |
|---|---|
| **Model orchestration** | Ollama, vLLM, and hosted-provider routing with health checks, priorities, failover, usage, and shared GPU coordination |
| **Agents and workflows** | DAG execution, supervised and stepwise loops, reusable profiles, tools, skills, and human approval |
| **Knowledge and memory** | Chroma/FAISS vectors, Neo4j relationships, SQL records, object storage, ingestion, recall, and context assembly |
| **Operations** | Health, topology, events, jobs, logs, event-loop stall detection, performance scanning, and guarded remediation |
| **Operator** | Browser sessions that observe, reason, and act through accessibility references and screenshots |
| **Development** | IDE workspaces, external coding-agent bridges, isolated Loop Lab branches, tests, review, and staged promotion |
| **Edge and media** | ESP32 mesh nodes, image/audio pipelines, rendering, galleries, and hardware-facing capabilities |

| Operator Studio | Loop Lab |
|---|---|
| ![Operator Studio](documentation/assets/overview/operator-studio.png) | ![Loop Lab](documentation/assets/overview/evolve.png) |

## Architecture in one minute

```text
 People · Agents · MCP clients · REST clients · Scheduled programs
                              │
                    ┌─────────▼─────────┐
                    │ Capability registry│
                    │ schema · policy   │
                    │ events · tracing  │
                    └─────────┬─────────┘
                              │
       ┌──────────────┬───────┼────────┬───────────────┐
       ▼              ▼       ▼        ▼               ▼
   LLM routing    DAG/loops  Workers  Operator     UI panels
       │              │       │        │               │
       └──────────────┴───────┼────────┴───────────────┘
                              ▼
        Redis · Postgres · Chroma/FAISS · Neo4j · object storage
```

Vera starts the orchestrator first and connects optional backends lazily.
Unavailable integrations degrade the capabilities that need them rather than
preventing the entire registry from starting.

## Before you install

Vera's resource needs depend much more on model size and concurrency than on the
web orchestrator itself. Do not use the old blanket RAM figures as a capacity
promise.

| Deployment | Practical starting point | What it is for |
|---|---|---|
| **Explore the runtime** | 4 CPU cores, 8–16 GB RAM, 20 GB free disk; hosted models or a separate Ollama node | Capability development, UI exploration, light workflows |
| **Single-host local AI** | 8+ CPU cores, 32 GB RAM, 100+ GB free disk; optional 12–16 GB VRAM | One orchestrator plus small/medium quantized models |
| **Distributed working setup** | Separate orchestrator and model workers; 32–64 GB RAM per CPU model node; one 16+ GB VRAM worker | Concurrent agents, background work, and failover |

These are planning baselines, not hard limits. Model weights, quantization,
context length, parallel requests, vector collections, logs, and retained
artifacts all change the answer. Read the
[performance and sizing guide](documentation/00-performance-and-sizing.md)
before choosing hardware or defining an SLO.

## Quick start

The supported starting path is the Docker stack:

```bash
git clone https://github.com/BoeJaker/Vera.git
cd Vera
cp .env.example .env
make secret
make up
make logs
```

Without `make`:

```bash
# Linux / macOS
./build.sh up

# Windows PowerShell
.\build.ps1 up
```

Then open:

- Harness: <http://localhost:8999/>
- OpenAPI: <http://localhost:8999/docs>
- Health: <http://localhost:8999/health>
- MCP catalog: <http://localhost:8999/mcp/tools>

For native installation, verification, and troubleshooting, follow
[Getting started](docs/GETTING_STARTED.md).

## Your first capability

```python
from Vera.vera.capability_orchestration import capability


@capability(
    "example.greet",
    description="Return a friendly greeting.",
    http_method="POST",
    http_path="/example/greet",
    http_tags=["example"],
)
async def greet(name: str, trace_id=None):
    return {"message": f"Hello, {name}!"}
```

After the module is loaded, `example.greet` is discoverable through MCP,
callable through the registry and HTTP route, visible to planners, and
represented by its input schema in the harness.

```bash
curl http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"example.greet","arguments":{"name":"Vera"}}'
```

See [Capability framework](documentation/01-capability-framework.md) for modes,
streams, caching, memory, worker dispatch, errors, and UI registration.

## Explore by goal

| If you want to… | Start here |
|---|---|
| Understand capabilities and the UI | [Capability framework](documentation/01-capability-framework.md) · [Harness UI](documentation/02-harness-ui.md) |
| Build workflows or agent loops | [DAG and loop engine](documentation/03-dag-engine.md) · [Agents and chat](documentation/19-agents-chat.md) |
| Configure local models | [Ollama cluster](documentation/04-ollama-cluster.md) · [vLLM](documentation/21-vllm.md) |
| Store and retrieve knowledge | [Memory graph](documentation/05-memory-graph.md) · [Data fabric](documentation/06-data-fabric.md) |
| Operate websites or Vera itself | [Operator](documentation/34-operator.md) |
| Develop Vera safely | [Loop Lab](documentation/33-evolve.md) · [Development lifecycle](documentation/specs/dev-lifecycle-and-repo-hygiene.md) |
| Size or troubleshoot a deployment | [Performance and sizing](documentation/00-performance-and-sizing.md) · [Workers and syslog](documentation/22-workers-jobs-syslog.md) |
| Browse every subsystem | [Documentation hub](documentation/README.md) |

| Data Fabric | Agent Loop Graph |
|---|---|
| ![Data Fabric](documentation/assets/overview/fabric-panel.png) | ![Agent Loop Graph](documentation/assets/overview/loop-graph.png) |

## Performance is a runtime property

Vera monitors the system it is actually running on rather than pretending one
hardware list guarantees performance. The Performance Monitor covers:

- event-loop stalls and blocking-call stack traces;
- CPU and memory pressure;
- Ollama health, availability, and saturation;
- stale Redis consumers and zombie jobs;
- rotating logs and an in-memory tail; and
- advisory or strict performance gates during Loop Lab promotion.

![Vera Performance Monitor](documentation/assets/overview/perf-monitor.png)

The default gate fails on any critical finding, warns above four warnings, and
blocks promotion only when `VERA_PERF_GATE_STRICT=1`. A loop stall of 3 seconds
or more is critical; shorter recent stalls are warnings. Full semantics and
measurement guidance are in
[Performance and sizing](documentation/00-performance-and-sizing.md).

## Repository map

```text
vera/
├── capability_orchestration.py   core registry, HTTP/MCP surface, lifecycle
├── config.py                     backend and runtime configuration
├── dag/                          DAGs, loops, profiles, authoring
├── workers/                      distributed work and model routing
├── fabric/                       ingestion, vectors, graphs, SQL, objects
├── operator/                     browser operation and documentation capture
├── evolve/                       Loop Lab, sandboxes, review, promotion
├── monitor/                      performance diagnostics and gates
├── agents/ · chat/ · research/   agent-facing applications
├── ide/ · execution/             development and remote execution
└── mesh/ · render/ · media/      edge, documents, images, and audio
```

The [documentation hub](documentation/README.md) maps the remaining packages
and studios.

## Project status and contribution

Vera changes quickly. Before submitting a change:

1. open an issue or describe the problem and expected behavior;
2. keep the change focused and add deterministic tests;
3. update the relevant guide when behavior or configuration changes;
4. never commit credentials, generated secrets, or local environment files; and
5. use Vera's isolated development workflow for Vera repository changes.

The canonical development and release rules are documented in
[Development lifecycle and repository hygiene](documentation/specs/dev-lifecycle-and-repo-hygiene.md).

## License

This repository does not currently include a license file. Contact the project
maintainer before assuming rights beyond viewing or evaluating the source.
