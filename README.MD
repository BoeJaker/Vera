# Vera

### A capability runtime for building, operating, and improving AI systems

Vera turns ordinary Python functions into distributed, observable tools that
agents, people, APIs, workflows, and external MCP clients can all use. One
`@capability` decorator connects a function to the registry, HTTP, MCP, the
interactive harness, event streams, workers, and Vera's planning engines.

That simple centre supports a much larger system: local and hosted models,
agentic loops, DAG workflows, semantic memory, a polyglot data fabric, browser
operation, remote development, hardware nodes, and Loop Lab — Vera's isolated,
reviewed self-improvement workflow.

> [!NOTE]
> Vera is an ambitious, actively developed system. The core runtime is usable,
> but some integrations and studios are experimental. Issues, focused pull
> requests, documentation improvements, and reproducible bug reports are very
> welcome.

![Vera main dashboard](documentation/assets/overview/dashboard.png)

## Contents

- [Why Vera is different](#why-vera-is-different)
- [What you can build](#what-you-can-build)
- [Architecture in one minute](#architecture-in-one-minute)
- [Before you install](#before-you-install)
- [Quick start](#quick-start)
  - [1. Clone and configure](#1-clone-and-configure)
  - [2. Start the stack](#2-start-the-stack)
  - [3. Verify and make your first capability call](#3-verify-and-make-your-first-capability-call)
  - [4. Point Vera at a model](#4-point-vera-at-a-model)
- [Your first capability](#your-first-capability)
- [Documentation map](#documentation-map)
- [Performance is a runtime property](#performance-is-a-runtime-property)
- [Repository map](#repository-map)
- [Project status and contribution](#project-status-and-contribution)
- [License](#license)

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

A capability is more than a remotely callable function. Its registered name is
a stable operation identity; its JSON schema tells callers how to supply data;
its [Capability Contract](documentation/43-capability-contracts.md) describes
the task it fulfils, effects it may cause, resources and authority it needs,
lifecycle, and operational expectations. The same wrapper then applies tracing,
[policy observation or enforcement](documentation/45-capability-policy.md),
activity recording, error normalization, optional caching, and local or worker
dispatch. This lets a model choose between several implementations of the same
task using machine-readable evidence instead of guessing from similar names.

Registration does not itself grant authority. A capability can be discoverable
while policy still denies a particular call, and inspection capabilities can
describe plans, contracts, runtime mappings, or health without executing the
thing they describe. This separation — **describe, resolve, authorize, execute,
observe** — is what allows Vera to interoperate with external MCP servers and
agent or workflow runtimes without absorbing each one into a new bespoke loop.

The capability is therefore Vera's unit of interoperability, not merely its
RPC format. A complete capability has five independently inspectable layers:

| Layer | Question it answers | Typical evidence |
|---|---|---|
| Identity and schema | What stable task is this, and what data does it accept and return? | Registered name, canonical task, JSON input/output schema |
| Effects and authority | What can it change, and who may permit that change? | Effect declaration, approval mode, secret/filesystem/network policy |
| Execution | Where and how can it run? | Local/worker/provider adapter, timeout, cancellation, retry and idempotency semantics |
| Operations | Is this implementation suitable now? | Availability, health, cost, latency, resource and quality observations |
| Evidence | What actually happened? | Run events, activity records, artifacts, traces and provenance |

Those layers deliberately do not collapse into one score. Discovery does not
authorize execution; a provider's advertised tool does not become trusted
because it was found; and telemetry can inform selection without rewriting the
declared contract. Native Python, workers, MCP tools, model providers, workflow
engines, and remote agents can therefore share a control path while each runtime
retains authority over its own execution state. See
[Interoperability foundations](documentation/46-interoperability-foundations.md).

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
preventing the entire registry from starting: an orchestrator with no Redis,
Postgres, Chroma, Neo4j, or model node still serves the harness, `/health`, and
thousands of registered capabilities.

## Before you install

Vera's resource needs depend much more on model size and concurrency than on the
web orchestrator itself. Do not use blanket RAM figures as a capacity promise.

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

You need Git, Docker with Compose (for the recommended path), and Python 3.11
(for `make secret` and native runs).

## Quick start

The supported starting path is the Docker stack. It starts the orchestrator plus
Redis, Postgres, ChromaDB, Neo4j, Garage (S3-compatible object storage), a
code-server IDE, and a build service. Ollama model nodes are **not** part of the
stack; they run on separate hosts that you point Vera at.

### 1. Clone and configure

```bash
git clone https://github.com/BoeJaker/Vera.git
cd Vera
cp .env.example .env
make secret        # prints a Fernet key
```

Add the printed key to `.env` as `VERA_SECRET_KEY=<key>`. It encrypts stored
credentials; without it a key is generated inside the container and lost on
rebuild. `make secret` needs the `cryptography` package on the host
(`pip install cryptography`). Never commit `.env`.

> [!IMPORTANT]
> `docker-compose.yml` passes only the variables listed in the `vera` service's
> `environment:` block into the container, and `.env` is excluded from the
> image. A setting that is not listed there must be added to that block (or set
> in the process environment for a native run).

### 2. Start the stack

```bash
make up            # docker compose up -d (builds the vera:latest image the first time)
make logs          # follow orchestrator logs; Ctrl-C to stop following
```

Without `make`:

```bash
# Linux / macOS
./build.sh up

# Windows PowerShell
.\build.ps1 up
```

The first build can take a while: Python dependencies, Playwright Chromium, and
the backing images all download.

### 3. Verify and make your first capability call

Open:

- Harness: <http://localhost:8999/>
- OpenAPI: <http://localhost:8999/docs>
- Health: <http://localhost:8999/health>
- MCP catalog: <http://localhost:8999/mcp/tools>

Then call a capability through the MCP endpoint:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"echo","arguments":{"message":"hello"}}'
```

```json
{"type":"tool_result","tool_name":"echo","trace_id":"…",
 "content":{"echo":"hello","ts":"…","trace_id":"…"}}
```

`make health` and `make caps` do the same checks from the repository. If
`TLS_ENABLED=1`, use `https://` (the certificate is self-signed).

### 4. Point Vera at a model

The Ollama URLs in `.env.example` and `docker-compose.yml` default to example LAN
addresses. Set `OLLAMA_GPU_URL`, `OLLAMA_CPU_A_URL`, `OLLAMA_CPU_B_URL`,
`OLLAMA_EMBED_URL`, and `OLLAMA_MODEL` in `.env` to your own nodes, restart with
`make up`, and check:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"ollama.instances","arguments":{}}'
```

`/health` also reports each Ollama node as `online` or `offline`.

For native installation, model configuration, and troubleshooting, follow
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
    contract={
        "canonical_task": "example.greet",
        "effects": ["none"],
        "output_schema": {"type": "object", "required": ["message"],
                          "properties": {"message": {"type": "string"}}},
        "approval": {"status": "not_required"},
        "secrets": {"status": "not_required"},
        "filesystem": {"status": "not_required"},
        "network": {"status": "not_required"},
        "tenant": {"status": "request_scoped"},
        "idempotency": {"status": "idempotent"},
        "resources": {"status": "declared", "classes": ["cpu"]},
        "owner": "you",
    },
)
async def greet(name: str, trace_id=None):
    return {"message": f"Hello, {name}!"}
```

Load the module by listing its path in `VERA_MODULES` (comma-separated `.py`
paths, loaded after Vera's own modules) and restart. Once loaded,
`example.greet` is discoverable through MCP, callable through the registry and
its HTTP route, visible to planners, and represented by its input schema in the
harness:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"example.greet","arguments":{"name":"Vera"}}'

curl -s -X POST http://localhost:8999/example/greet \
  -H 'content-type: application/json' -d '{"name":"Vera"}'
```

The `contract` block is optional — legacy capabilities keep working — but
undeclared fields stay visible as `unknown` migration gaps rather than being
treated as safe defaults. The contract above passes the strict gate
(`cap.contract.gate`) and gets an `allow` shadow policy verdict. See
[Capability framework](documentation/01-capability-framework.md) for modes,
streams, caching, memory, worker dispatch, errors, and UI registration, and
[Capability contracts](documentation/43-capability-contracts.md) for every
contract field.

## Documentation map

Start with the [documentation hub](documentation/README.md) and the
[visual gallery](documentation/GALLERY.md). The numbered guides:

**Core runtime**

| Guide | Covers |
|---|---|
| [00 · Performance and sizing](documentation/00-performance-and-sizing.md) | Hardware planning, runtime thresholds, SLOs |
| [01 · Capability framework](documentation/01-capability-framework.md) | `@capability`, schemas, modes, events, invocation |
| [02 · Harness UI](documentation/02-harness-ui.md) | Panels, custom elements, UI extension |
| [03 · DAG and loop engine](documentation/03-dag-engine.md) | Workflows, planners, loops, Workflow IR |
| [10 · Configuration](documentation/10-configuration.md) | Environment variables and backend endpoints |
| [22 · Workers, jobs, and syslog](documentation/22-workers-jobs-syslog.md) | Distributed work, job recovery, logs |
| [29 · Security](documentation/29-security.md) | Secrets and access boundaries |
| [32 · Cluster encryption](documentation/32-cluster-encryption.md) | Encrypted cluster communication |

**Models and machine learning**

| Guide | Covers |
|---|---|
| [04 · Ollama cluster](documentation/04-ollama-cluster.md) | Nodes, routing, health, shared GPU gate |
| [21 · vLLM](documentation/21-vllm.md) | OpenAI-compatible serving |
| [16 · Machine learning](documentation/16-machine-learning.md) | Training, ModelPackage, evaluation contracts |
| [30 · ONNX](documentation/30-onnx.md) | Export and runtime |
| [11 · Worldview](documentation/11-worldview.md) · [49 · Worldview integration](documentation/49-worldview-integration.md) | JEPA world model |
| [System 1 decision models (not live)](documentation/48-system-one-decision-models.md) | Small typed decision points that could move off the LLM |

**Knowledge and data**

| Guide | Covers |
|---|---|
| [05 · Memory graph](documentation/05-memory-graph.md) | Sessions, recall, graph traversal |
| [06 · Data fabric](documentation/06-data-fabric.md) | Ingestion, vectors, graphs, SQL, objects |
| [07 · Research](documentation/07-research.md) | Search and deep research |
| [09 · Galaxy graph](documentation/09-galaxy-graph.md) · [25 · Vector browser](documentation/25-vector-browser.md) | Graph and vector inspection |
| [18 · Skills and ontologies](documentation/18-skills-ontologies.md) | Reusable instructions and domain models |

**Agents, interfaces, and applications**

| Guide | Covers |
|---|---|
| [19 · Agents and chat](documentation/19-agents-chat.md) | Personas, conversations, tools |
| [36 · Agent runtimes and providers](documentation/36-agent-runtimes-providers.md) | Container agent bridges, provider adapters |
| [27 · OpenClaw](documentation/27-openclaw.md) | External agent gateway |
| [34 · Operator](documentation/34-operator.md) · [24 · Web browser](documentation/24-web-browser.md) | Browser observation and action |
| [20 · Flow Builder](documentation/20-flow-builder.md) · [26 · UI Builder](documentation/26-ui-builder.md) · [47 · PWA](documentation/47-pwa.md) | Visual workflows, UI, installable app |
| [17 · Dream](documentation/17-dream.md) | Background reflection |
| [15 · Markets](documentation/15-markets.md) · [37 · Business and commerce](documentation/37-business-commerce.md) | Domain studios |
| [28 · Render](documentation/28-render.md) · [31 · Podcast](documentation/31-podcast.md) · [38 · Media and characters](documentation/38-media-characters.md) | Documents, audio, images |
| [23 · Integrations](documentation/23-integrations.md) | Accounts, mail, calendar, external apps |

**Operations and infrastructure**

| Guide | Covers |
|---|---|
| [08 · IDE](documentation/08-ide.md) · [12 · Execution](documentation/12-execution.md) | Workspaces, remote execution, Run protocol |
| [13 · Docker](documentation/13-docker.md) · [35 · Infrastructure and provisioning](documentation/35-infrastructure-provisioning.md) | Hosts, containers, fleet provisioning |
| [14 · Device mesh](documentation/14-mesh.md) | ESP32 nodes and firmware |
| [33 · Loop Lab](documentation/33-evolve.md) | Isolated changes, review, promotion |
| [39 · Activity and boards](documentation/39-activity-boards.md) | Activity history and work coordination |
| [41 · System inventory](documentation/41-system-inventory.md) · [42 · Performance baseline](documentation/42-performance-baseline.md) | Inventory and measured baselines |

**Interoperability and governance**

| Guide | Covers |
|---|---|
| [43 · Capability contracts](documentation/43-capability-contracts.md) | Contract v2 manifests, lint, coverage, gate, resolver |
| [44 · Evaluation corpus](documentation/44-evaluation-corpus.md) | Frozen deterministic and queued-live lanes |
| [45 · Capability policy](documentation/45-capability-policy.md) | Shadow verdicts, receipts, selective enforcement |
| [46 · Interoperability foundations](documentation/46-interoperability-foundations.md) | Shared boundaries, A2A, runtime matrix |
| [40 · AI ecosystem roadmap](documentation/40-ai-ecosystem-roadmap.md) | External libraries and standards: gaps and priorities |

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
├── capability_contract_core.py   Contract v2 projection, lint, coverage, gate
├── capability_policy_core.py     shadow policy verdicts (+ capability_enforcement.py)
├── config.py                     backend and runtime configuration
├── dag/                          DAGs, loops, profiles, authoring
├── workers/                      distributed work and model routing
├── fabric/                       ingestion, vectors, graphs, SQL, objects
├── operator/                     browser operation and documentation capture
├── evolve/                       Loop Lab, sandboxes, review, promotion
├── monitor/                      performance diagnostics and gates
├── execution/                    Run protocol, Workflow IR, telemetry, remote execution
├── agentbridges/ · providers/    external agent runtimes and provider contracts
├── models/                       ModelPackage, inference, training, evaluation contracts
├── agents/ · chat/ · research/   agent-facing applications
├── ide/                          development workspaces
└── mesh/ · render/ · media/      edge, documents, images, and audio
evaluations/                      frozen evaluation corpora (JSON)
tests/                            pytest suite (make test-unit, make test-critical)
documentation/                    numbered subsystem guides
docs/                             getting started and UI framework notes
```

The [documentation hub](documentation/README.md) maps the remaining packages
and studios.

## Project status and contribution

Vera changes quickly. Before submitting a change:

1. open an issue or describe the problem and expected behaviour;
2. keep the change focused and add deterministic tests (`make test-unit`);
3. update the relevant guide when behaviour or configuration changes;
4. never commit credentials, generated secrets, or local environment files
   (`make install-hooks` enables the repository's secret-scan and commit hooks;
   `make scan` scans tracked files); and
5. use Vera's isolated development workflow for Vera repository changes.

The canonical development and release workflow is documented in
[Loop Lab](documentation/33-evolve.md).

## License

This repository does not currently include a license file. Contact the project
maintainer before assuming rights beyond viewing or evaluating the source.
