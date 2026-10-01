# 10 · Configuration

Vera is configured almost entirely through environment variables. The core
connection settings — hosts, ports, TLS, backing stores, the default Ollama
nodes, project roots — are centralised in `vera/config.py` as a single
`VeraConfig` class exposed as the `cfg` singleton. Subsystems with their own
tunables (the Ollama gate, the data fabric, agent loops, the browser, Loop Lab,
…) read their variables where they are used, with an inline default. Runtime
settings that operators change from the UI (Ollama routing profiles, node
priorities, themes, the PWA, policy toggles) are stored in Redis rather than in
the environment.

This page explains how configuration is resolved, documents the core settings
in context, shows how Docker Compose overrides them, and ends with a
**complete reference of every environment variable read under `vera/`**,
grouped by subsystem, with its default and the module that reads it. The
reference was produced by scanning every `os.getenv` / `os.environ` read in the
tree; defaults are the literal values in the code.

**Maturity:** stable. The `.env` loader, `VeraConfig` and the Compose file are
the supported entry points. Defaults assume a home network with `llm.int` as the
internal hostname and three Ollama nodes; every one of them can be overridden.

## Contents

- [1. How configuration is resolved](#1-how-configuration-is-resolved)
  - [Precedence](#precedence)
  - [The `.env` loader](#the-env-loader)
  - [Redis credentials from the sealed local copy](#redis-credentials-from-the-sealed-local-copy)
  - [Editing `.env` at runtime (dev mode)](#editing-env-at-runtime-dev-mode)
- [2. Network, hosts and TLS](#2-network-hosts-and-tls)
- [3. Redis](#3-redis)
- [4. PostgreSQL](#4-postgresql)
- [5. ChromaDB](#5-chromadb)
- [6. Neo4j](#6-neo4j)
- [7. Ollama cluster](#7-ollama-cluster)
- [8. GPU inference and media nodes](#8-gpu-inference-and-media-nodes)
- [9. IDE workspace and Git](#9-ide-workspace-and-git)
- [10. Research and NLP](#10-research-and-nlp)
- [11. Web search and acquisition](#11-web-search-and-acquisition)
- [12. Distributed dispatch and workers](#12-distributed-dispatch-and-workers)
- [13. Activity recording and event history](#13-activity-recording-and-event-history)
- [14. Policy, telemetry and diagnostics](#14-policy-telemetry-and-diagnostics)
- [15. Docker Compose](#15-docker-compose)
- [16. Runtime configuration stored in Redis](#16-runtime-configuration-stored-in-redis)
- [17. Common deployment patterns](#17-common-deployment-patterns)
- [18. Adding new config fields](#18-adding-new-config-fields)
- [19. Complete environment variable reference](#19-complete-environment-variable-reference)
  - [Server, HTTP and process](#server-http-and-process)
  - [Backing stores](#backing-stores)
  - [Ollama, GPU gate and LLM output budgets](#ollama-gpu-gate-and-llm-output-budgets)
  - [Distributed workers, cluster and node agents](#distributed-workers-cluster-and-node-agents)
  - [Policy, telemetry and provenance](#policy-telemetry-and-provenance)
  - [Diagnostics, GC and logging](#diagnostics-gc-and-logging)
  - [Dev sandboxes, Loop Lab and self-improvement](#dev-sandboxes-loop-lab-and-self-improvement)
  - [Chat, agents and agentic loops](#chat-agents-and-agentic-loops)
  - [Data fabric, memory and research](#data-fabric-memory-and-research)
  - [Web, search and browser](#web-search-and-browser)
  - [Execution, Docker and remote hosts](#execution-docker-and-remote-hosts)
  - [IDE, VS Code and external agent sessions](#ide-vs-code-and-external-agent-sessions)
  - [Secrets, identity and provisioning](#secrets-identity-and-provisioning)
  - [Agent runtimes and model backends](#agent-runtimes-and-model-backends)
  - [Models, catalog and machine learning](#models-catalog-and-machine-learning)
  - [Worldview model](#worldview-model)
  - [Device mesh, build service and printers](#device-mesh-build-service-and-printers)
  - [Other subsystems](#other-subsystems)
- [See also](#see-also)

---

## 1. How configuration is resolved

```python
from Vera.vera.config import cfg

print(cfg.REDIS_URL)            # → "redis://localhost:6379"
print(cfg.OLLAMA_GPU_URL)       # → "http://192.168.0.250:11435"
print(cfg)                      # → VeraConfig(redis=..., pg=..., chroma=..., neo4j=..., orchestrator=...)
```

`repr(cfg)` redacts credentials in `REDIS_URL`.

### Precedence

From highest to lowest:

1. **The process environment** — variables exported in the shell, set by
   systemd, or injected by Docker Compose's `environment:` block.
2. **`.env` files** — loaded by `vera/config.py` at import, but only for keys
   that are **not already set** in the environment.
3. **Code defaults** — the second argument of each `os.getenv(...)`.

`VeraConfig` attributes are evaluated once, at import. Most module-level
tunables are also read once at import, so changing a variable normally needs a
restart. A few are read on every use (for example `VERA_POLICY_MODE`,
`VERA_IS_DEV_SANDBOX` checks, `VERA_GC_WARN_MS`, the perf-gate thresholds).

### The `.env` loader

Docker Compose reads `.env` automatically; native launches (`./build.sh run`,
`make run`, `python -m Vera.vera.capability_orchestration`) do not, so
`vera/config.py` loads it itself, with no external dependency. It reads, in
order, the repository-root `.env` (the parent of `vera/`) and `./.env` in the
current working directory, skipping duplicates. Lines are `KEY=VALUE`; blank
lines and `#` comments are ignored, an `export ` prefix is stripped, and one
pair of matching single or double quotes around the value is removed. A key
already present in the environment is never overridden.

`.env.example` at the repository root lists the main overrides with
explanations; copy it to `.env`. Note that its `OLLAMA_MODEL=mistral` comment
predates the current default (`jaahas/qwen3.5-uncensored`, below).

### Redis credentials from the sealed local copy

The shared Redis can use ACL users. Because the host cannot fetch its Redis
password from OpenBao before it can reach Redis, it keeps a Fernet-sealed copy
in a `0600` file — `VERA_REDIS_AUTH_FILE`, default `~/.vera/redis.auth` —
opened with Vera's secret key. At import, if `REDIS_URL` carries no credentials
and that file exists, `config.py` opens it and rewrites `REDIS_URL` (also in
`os.environ`, so worker spawns and other subprocesses see it). If the copy will
not open, the error is logged and Redis will refuse the connection with
`NOAUTH`. See [Security & Secrets](./29-security.md).

### Editing `.env` at runtime (dev mode)

With `VERA_DEV_MODE=1`, two capabilities edit the repository-root `.env`:

| Capability | Route | Behaviour |
|---|---|---|
| `sys.env.get` | `GET /sys/env/get` | Returns `{path, vars}`; keys containing `SECRET`, `PASSWORD`, `PASS`, `TOKEN`, `KEY`, `CREDENTIAL`, `ACCESS`, `AUTH`, `PRIVATE`, `APIKEY` or `CERT` are redacted |
| `sys.env.set` | `POST /sys/env/set` | Sets or appends `KEY=VALUE` preserving every other line, updates the running process's `os.environ`, emits `sys.env.set`; requires `confirm=true`; `restart=true` chains `sys.dev.restart` |

Because most values are read at import, a restart is usually still needed.

---

## 2. Network, hosts and TLS

| Variable | Default | Purpose |
|---|---|---|
| `BACKEND_HOST` | `llm.int` | The single internal-domain setting. Host-derived defaults (SearXNG, research data sources, the Google OAuth redirect base, the OpenClaw Ollama base, the self-signed certificate SAN list) build on it. |
| `ORCHESTRATOR_HOST` | `0.0.0.0` | Uvicorn bind address. `0.0.0.0` keeps Vera reachable from every host for distributed dispatch. |
| `ORCHESTRATOR_PORT` | `8999` | Port for HTTP or HTTPS. |
| `TLS_ENABLED` | `0` | `1` serves HTTPS on the same port. |
| `TLS_CERTFILE` | `~/.vera/tls/cert.pem` | Certificate path (`~` expanded). |
| `TLS_KEYFILE` | `~/.vera/tls/key.pem` | Key path. |
| `TLS_EXTRA_SANS` | empty | Comma-separated extra DNS names or IPs for the generated certificate. |
| `GOOGLE_OAUTH_REDIRECT_BASE` | `http(s)://<BACKEND_HOST>:<ORCHESTRATOR_PORT>` | Base for `/cal/google/oauth_callback` and `/accounts/oauth/callback`. |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | empty | Optional shared Google OAuth client used by every account. |
| `VERA_WS_PING_INTERVAL` / `VERA_WS_PING_TIMEOUT` | `20` / `75` | Uvicorn WebSocket keep-alive (seconds); the long timeout tolerates brief event-loop stalls. |
| `VERA_HTTP_KEEPALIVE` | `75` | Uvicorn HTTP keep-alive (seconds). |
| `WS_OUT_QUEUE_MAX` | `2000` | Per-WebSocket outbound queue bound. |

**Client configuration injection.** Browser panels cannot read Python
configuration, so an HTTP middleware inserts
`<script>window.__VERA_DOMAIN__=…;window.__VERA_BASE__=…;</script>` after
`<head>` in every HTML response. `__VERA_DOMAIN__` is `BACKEND_HOST`;
`__VERA_BASE__` is the origin the browser actually used (from `Host`, or
`X-Forwarded-Host`/`X-Forwarded-Proto` behind a proxy), falling back to
`http(s)://BACKEND_HOST:ORCHESTRATOR_PORT`. Panels therefore resolve the backend
as `window.location.origin || window.__VERA_BASE__`, and no hostname is
hard-coded in the UI.

**TLS.** Browsers expose secure-context features — the Web Serial API and
`getUserMedia` (webcam/microphone), and service workers for the
[PWA](./47-pwa.md) — only over HTTPS or to `localhost`. With `TLS_ENABLED=1`
and no certificate at the configured paths, a self-signed RSA-2048 certificate
valid for ten years is generated on first start. Its SANs are `localhost`, the
hostname, `<hostname>.local`, `BACKEND_HOST`, `127.0.0.1`, `::1`, the resolved
host address, the outbound-interface address, and `TLS_EXTRA_SANS`. An existing
certificate is never rotated: delete it (or the `vera-tls` volume) and restart
to regenerate. A self-signed certificate that a user merely clicked through is
still **not** trusted for service-worker registration.

---

## 3. Redis

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Data Redis (may be rewritten with credentials, [§1](#redis-credentials-from-the-sealed-local-copy)) |
| `VERA_COORD_REDIS_DB` | `0` | Database of the shared coordination Redis used by the Ollama GPU gate |
| `VERA_COORD_REDIS_URL` | unset | Explicit coordination endpoint, or `off` to disable gate coordination |

Redis carries the task and result streams (`vera:tasks*`, `vera:results*`), the
event stream `vera:events` and the pub/sub channel `vera:events:live`, worker
registrations (`vera:workers:<id>`), the cluster snapshot
(`vera:cluster:ollama`, refreshed every `CLUSTER_POLL_INTERVAL`), per-capability
result/error caches (`vera:cap:*`), Ollama routing and node configuration
(`vera:ollama:*`), UI settings (`vera:ui:*`), the PWA configuration and many
module stores. The orchestrator connects with a 4 s socket timeout and retries
every 5 s in the background, so the HTTP server is available before Redis is.
Modules share the orchestrator's client (`_orch.REDIS`) instead of opening
their own.

The coordination Redis is the same server as `REDIS_URL` on
`VERA_COORD_REDIS_DB`; when that equals the data database (production's case)
the data client is reused. Dev sandboxes run their data on a private Redis but
share coordination, so every process sees the same GPU slot leases.

Without Redis, capabilities still run locally, but distributed dispatch,
cross-host worker observation, event history and Redis-backed settings are
unavailable; `/ws/mcp` reports `mode: "local"`.

---

## 4. PostgreSQL

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_URL` | `postgresql://admin:admin@localhost:5433/postgres` | Connection URL |

The orchestrator creates a pool (2–10 connections), retrying every 5 s in the
background. Tables created on first use include `vera_task_results` (archived
dispatch results, from the host only — not from dev sandboxes),
`vera_memories` and `vera_memory_edges` (memory store), `vera_dags` (DAG
store) and `vera_agents` (agent definitions). Job archiving is controlled by
`VERA_JOB_ARCHIVE_PG` (default `1`). The data fabric's primary store is SQLite
(`FABRIC_SQLITE`), not Postgres. If Postgres is offline, the dependent
capabilities degrade; the rest of Vera keeps running.

---

## 5. ChromaDB

| Variable | Default | Purpose |
|---|---|---|
| `CHROMA_HOST` | `localhost` | Chroma server host (data fabric and memory store) |
| `CHROMA_PORT` | `8008` | Chroma server port |
| `CHROMA_COLLECTION` | `vera_memory` | Memory store collection |

Chroma provides vector storage with metadata filtering for the data fabric and
the memory store; both fall back when it is offline (the fabric can also use
FAISS, `FABRIC_FAISS=1`). The orchestrator's own Chroma handle, used for the
health report, connects to `localhost:8008` regardless of these variables.

---

## 6. Neo4j

| Variable | Default | Purpose |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Bolt URI |
| `NEO4J_USER` | `neo4j` | Username |
| `NEO4J_PASS` | `neo4j` (Compose: `veraneo4j`) | Password |

Neo4j is the primary store for the memory graph and the auxiliary fabric graph.
At startup the orchestrator verifies connectivity with the configured
credentials and, if a user is set, also tries without authentication; it retries
for about a minute and `/health` reports `neo4j:false` if it never connects.
Graph writes degrade silently while Neo4j is down — capability calls still
succeed without their graph node.

---

## 7. Ollama cluster

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_GPU_URL` | `http://192.168.0.250:11435` | Seed URL for instance `gpu-250` |
| `OLLAMA_CPU_A_URL` | `http://192.168.0.246:11435` | Seed URL for instance `cpu-246` |
| `OLLAMA_CPU_B_URL` | `http://192.168.0.247:11435` | Seed URL for instance `cpu-247` |
| `OLLAMA_MODEL` | `jaahas/qwen3.5-uncensored` | Default generation model |
| `OLLAMA_NUM_CTX` | `32768` | Context window for source-review/planning generations (`cfg`) |
| `OLLAMA_KEEP_ALIVE` | `10m` in the orchestrator's generation path; `30m` on `cfg` | How long a node keeps a model resident |
| `OLLAMA_EMBED_URL` | `http://192.168.0.246:11435` | Embedding endpoint |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model |
| `VERA_EMBED_PROVIDER` | `ollama` | `ollama` or `fastembed` (local ONNX; changes the vector space, re-index first) |
| `VERA_FASTEMBED_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | fastembed model |
| `OLLAMA_GEN_TIMEOUT` | `900` | Generation stall timeout: the longest silence between streamed chunks, not a cap on total time |
| `OLLAMA_STALL_TIMEOUT` | `240` | Stall detection for streamed generations |
| `OLLAMA_QUEUE_TIMEOUT` | `0` | Maximum wait for a node's generation slot; `0` waits indefinitely |
| `OLLAMA_EMBED_TIMEOUT` | `300` | Embedding HTTP timeout |
| `OLLAMA_CONCURRENCY` | empty | Overrides the per-node in-process concurrency limit |

The three seed nodes get the instance ids `gpu-250`, `cpu-246` and `cpu-247`
regardless of their URLs — those ids are the keys referenced by routing,
pinning and tier mapping. Nodes added, re-prioritised or disabled at runtime
(`ollama.add_instance`, `ollama.node.config`, the Model Routing page) are
persisted in Redis (`vera:ollama:nodes`) and survive restarts. From Python:

```python
from Vera.vera.capability_orchestration import add_ollama_instance
add_ollama_instance("gpu-300", "http://gpu-node-b:11435", has_gpu=True, label="GPU Node B")
```

The context-window, output-budget and GPU-gate tunables (`OLLAMA_MAX_AUTO_CTX`,
`OLLAMA_VRAM_USABLE_FRAC`, `VERA_OUTPUT_MAX_TOKENS*`, `VERA_GATE_*`,
`VERA_GPU_GATE_N`, `VERA_NODE_GATE_N`, …) are listed in
[§19](#ollama-gpu-gate-and-llm-output-budgets) and explained in
[Ollama Cluster](./04-ollama-cluster.md).

---

## 8. GPU inference and media nodes

| Variable | Default | Purpose |
|---|---|---|
| `GPU_INFER_URL` | `http://192.168.0.250:8765` | Whisper STT, TTS and Stable Diffusion server; also the fallback media node |
| `ONNX_RUNTIME_URLS` | empty | Comma-separated edge ONNX Runtime servers advertised in the cluster view |

Media services (`stt`, `tts`, `imagegen`) are routed through a media-node
registry (`media.nodes`, `media.node.add/remove/config`, `media.ping`) persisted
in Redis (`vera:media:nodes`); `GPU_INFER_URL` seeds it and is the fallback.
The capabilities that use it include `stt.transcribe`, `tts.synthesize`,
`tts.voices`, `gpu.health` and `image.generate`.

---

## 9. IDE workspace and Git

| Variable | Default | Purpose |
|---|---|---|
| `VERA_PROJECT_ROOT` | `~/vera_projects` (Compose: `/data/projects`) | Root for IDE workspaces/projects; created on first use |
| `GIT_CLONE_ROOT` | `~/vera_repos` | Local clones land in `<root>/<project-slug>/<repo-name>` |
| `GITEA_BASE_URL`, `GITEA_TOKEN`, `GITEA_OWNER` | empty | Gitea/Forgejo mirror target (token needs repo and org create scope) |
| `VSCODE_CENTRAL_URL`, `VSCODE_PASSWORD`, `VSCODE_PROJECTS_VOLUME` | empty (Compose sets them) | Central code-server proxied at `/vscode/central/` |

See [IDE Module](./08-ide.md) for the full `VSCODE_*` and session-ingest set.

---

## 10. Research and NLP

| Variable | Default | Purpose |
|---|---|---|
| `VERA_RESEARCHER_URL` | `http://localhost:8765` | researcher_api server used by DAG, dream and project capabilities |
| `VERA_NLP_PORT` | `8771` | Port of the node NLP servers dispatched to by `nlp.*` |
| `VERA_NER_MODEL`, `VERA_SENTIMENT_MODEL`, `VERA_RERANK_MODEL` | see [§19](#data-fabric-memory-and-research) | NLP model ids |
| `VERA_RERANK_ENABLED` | `0` | Enable reranking in researcher_api |
| `RESEARCH_FAST_TIMEOUT` | `90` | researcher_api fast-path timeout |

researcher_api runs as a separate process; when it is not running the
`research.*` capabilities return clear errors and the research panel shows the
server offline. See [Research System](./07-research.md).

---

## 11. Web search and acquisition

| Variable | Default | Purpose |
|---|---|---|
| `VERA_SEARXNG_URL` | `http://<BACKEND_HOST>:8888` | SearXNG instance |
| `BRAVE_API_KEY` | empty | Brave Search API key |
| `FABRIC_CRAWL_DELAY_S` | `2` | Delay between fetches during acquisition |
| `VERA_WEB_TIMEOUT` | `8.0` | Web client timeout (seconds) |
| `VERA_WEB_READER`, `VERA_WEB_READER_KEY` | `https://r.jina.ai/`, empty | Reader service used to extract page text |

With `BRAVE_API_KEY` unset, the search chain does not use Brave. The full
`VERA_WEB_*` and `BROWSER_*` sets are in [§19](#web-search-and-browser); see
[Web & Browser](./24-web-browser.md).

---

## 12. Distributed dispatch and workers

| Variable | Default | Purpose |
|---|---|---|
| `VERA_IS_WORKER` | empty | Truthy: this process is a node worker (reads only its class streams, runs no periodic jobs) |
| `VERA_WORKER_CLASSES` | unset | Task classes of a node worker when the roles registry has none |
| `VERA_WORKER_HOST_ID` | empty | Key into the roles registry `vera:node_workers:roles` |
| `VERA_WORKER_NODE_OK` | empty | Comma-separated namespaces or exact capability names admitted to node workers |
| `VERA_WORKER_HOST_ONLY` | empty | Comma-separated namespaces or exact names forced onto the host (wins) |
| `VERA_WORKER_COMMIT` | empty | Commit a provisioned worker runs (reported in its registration) |
| `VERA_WORKER_IMAGE` | `vera:latest` | Image used to spawn Docker workers |
| `LOCAL_OLLAMA_INSTANCE` | empty | Instance id (e.g. `gpu-250`) to expose through Vera's `/ollama/*` transparent proxy |
| `PROXY_MAX_CONCURRENCY` / `PROXY_QUEUE_MAX` / `PROXY_QUEUE_TIMEOUT` | `3` / `50` / `120` | Per-node proxy concurrency, queue length and queue wait |
| `CLUSTER_POLL_INTERVAL` | `10` | Seconds between cluster polls |

See [Capability Framework §6](./01-capability-framework.md#6-distributed-dispatch)
and [Workers, Jobs & Syslog](./22-workers-jobs-syslog.md).

---

## 13. Activity recording and event history

| Variable | Default | Purpose |
|---|---|---|
| `VERA_ACTIVITY_RECORDING` | `0` | Starts the activity worker if the `cap_tracking` module did not (it is loaded by default and starts the worker itself) |
| `PANEL_CAP_MIRROR` | `1` | Mirror capability activity to the session's open panel |
| `VERA_RESUME_TTL` | `604800` | Seconds agent-loop event history is kept for resume |
| `VERA_RESUME_MAX_EVENTS` | `4000` | Events kept per loop run |
| `VERA_MODULES` | empty | Extra comma-separated module paths appended to the load list |

---

## 14. Policy, telemetry and diagnostics

| Variable | Default | Purpose |
|---|---|---|
| `VERA_POLICY_MODE` | `shadow` | `enforce` enables enforcement for selected families |
| `VERA_POLICY_ENFORCE_FAMILIES` | empty | Families to enforce; only `run.shadow` is supported |
| `VERA_OTLP_AUTO_EXPORT` | off | Preload and enable automatic OTLP export of completed Runs |
| `OTEL_EXPORTER_OTLP_*` | unset | Standard OTLP exporter settings honoured by the portable telemetry queue |
| `VERA_LOOP_LAG_WARN_MS` | `500` | Event-loop stall warning threshold |
| `VERA_LOOP_HANG_DUMP_S` | `1` | Stalls at least this long get the blocking stack dumped |
| `VERA_GC_*` | see [§19](#diagnostics-gc-and-logging) | Paced garbage collection |
| `VERA_PERF_GATE_STRICT` | empty | `1` lets a failing perf gate block a promotion |
| `VERA_DEV_MODE` | empty | Enables `sys.dev.*` and `sys.env.*` |

See [Performance and sizing](./00-performance-and-sizing.md) and
[Capability policy](./45-capability-policy.md).

---

## 15. Docker Compose

`docker-compose.yml` runs `vera` (image `vera:latest`, container
`vera-orchestrator`), `redis`, `postgres`, `chromadb`, `neo4j`, `garage` and a
one-shot `garage-init`, `vscode` (code-server) and `vera-builder`. Inside the
stack it overrides several defaults to point at service names:

| Variable | Compose value | Native default |
|---|---|---|
| `ORCHESTRATOR_HOST` / `ORCHESTRATOR_PORT` | `0.0.0.0` / `8999` (host port `${ORCHESTRATOR_PORT:-8999}`) | same |
| `REDIS_URL` | `redis://redis:6379` | `redis://localhost:6379` |
| `POSTGRES_URL` | `postgresql://admin:admin@postgres:5432/postgres` | `…@localhost:5433/postgres` |
| `CHROMA_HOST` / `CHROMA_PORT` | `chromadb` / `8000` | `localhost` / `8008` |
| `NEO4J_URI` / `NEO4J_PASS` | `bolt://neo4j:7687` / `${NEO4J_PASS:-veraneo4j}` | `bolt://localhost:7687` / `neo4j` |
| `FABRIC_OBJECT_STORE` | `${FABRIC_OBJECT_STORE:-garage}` | `none` |
| `FABRIC_S3_ENDPOINT` | `http://garage:3900` (not overridable) | `http://localhost:3900` |
| `VERA_BUILDER_URL` | `${VERA_BUILDER_URL:-http://vera-builder:8080}` | unset |
| `VERA_PROJECT_ROOT` | `/data/projects` | `~/vera_projects` |
| `VSCODE_CENTRAL_URL` / `VSCODE_PASSWORD` / `VSCODE_PROJECTS_VOLUME` | `http://vscode:8080` / `vera-code` / `vera_vera-projects` | empty |
| `VERA_SECRET_KEY` | `${VERA_SECRET_KEY:-}` | empty |

Host port mappings are set in `.env`: `ORCHESTRATOR_PORT` (8999),
`REDIS_PORT` (6379), `POSTGRES_PORT` (5433), `CHROMA_PORT` (8008),
`NEO4J_HTTP_PORT` (7474), `NEO4J_BOLT_PORT` (7687), `GARAGE_S3_PORT` (3900),
`GARAGE_ADMIN_PORT` (3903), `BUILDER_PORT` (8785), `VSCODE_PORT` (8843).

> [!WARNING]
> Set `VERA_SECRET_KEY` in `.env` for any persistent deployment. If it is
> blank, a key is generated inside the container at `~/.vera/secret.key`, which
> is lost on rebuild, and every secret sealed into the persisted Redis volume
> becomes undecryptable. The Compose file's Garage and S3 credentials are
> deterministic development values; replace them together with the matching
> `garage-init` values.

Persistent volumes: `redis-data`, `pg-data`, `chroma-data`, `neo4j-data`,
`vera-projects`, `vera-tls` (the generated certificate), `vscode-data`,
`garage-meta`, `garage-data`, `builder-cache`, `builder-pio`.

---

## 16. Runtime configuration stored in Redis

These settings are changed through capabilities and the UI, not the
environment:

| Area | Key(s) | Changed with |
|---|---|---|
| Ollama routing profiles, nodes, embedding, per-capability rules, role profiles, model tags, route statistics | `vera:ollama:routing`, `vera:ollama:nodes`, `vera:ollama:embed`, `vera:ollama:cap_routing`, `vera:ollama:role_profiles`, `vera:ollama:model_tags`, `vera:ollama:route_stats` | `ollama.*` capabilities, Model Routing page |
| Interactive priority (demote background LLM work while a person is active) | `vera:ollama:interactive_priority` | `ollama.interactive.set` |
| Media nodes | `vera:media:nodes` | `media.node.*` |
| Node-worker roles and offload stage | `vera:node_workers:roles`, `vera:node_workers:dispatch` | Workers UI |
| Active theme, custom themes, appearance, scale, loader | `vera:ui:theme`, `vera:ui:theme:*`, `vera:ui:appearance`, `vera:ui:scale`, `vera:ui:loader` | `ui.theme.*`, `ui.appearance.set`, `ui.scale.set`, `ui.loader.set` |
| Folded top-level tabs | `vera:ui:retire_overlap_tabs` (absent = on) | `ui.tabs.retired.set` |
| PWA | `vera:pwa:config` | `pwa.config.set` |

---

## 17. Common deployment patterns

### Single-host development

```bash
# Everything on one machine; defaults work once Redis/Postgres/Neo4j/Chroma run locally
python -m Vera.vera.capability_orchestration
```

### Docker Compose

```bash
cp .env.example .env        # set VERA_SECRET_KEY, Ollama URLs, TLS as needed
docker compose up -d --build
```

### Distributed cluster

On every host:

```bash
export BACKEND_HOST=your-host.lan      # single internal-domain setting
export REDIS_URL=redis://$BACKEND_HOST:6379
export POSTGRES_URL=postgresql://admin:admin@$BACKEND_HOST:5433/postgres
export NEO4J_URI=bolt://$BACKEND_HOST:7687
python -m Vera.vera.capability_orchestration
```

On the GPU host, additionally expose the local Ollama through Vera's proxy:

```bash
export LOCAL_OLLAMA_INSTANCE=gpu-250
```

On a node worker:

```bash
export VERA_IS_WORKER=1
export VERA_WORKER_HOST_ID=<host id from provisioning>
```

### Production hardening

```bash
export VERA_SECRET_KEY=<stable Fernet key>
export POSTGRES_URL=postgresql://<user>:<password>@<pg-host>/vera
export NEO4J_PASS=<strong password>
export TLS_ENABLED=1                  # plus a trusted certificate via TLS_CERTFILE/TLS_KEYFILE
```

---

## 18. Adding new config fields

A setting shared by several modules belongs on `VeraConfig`:

```python
class VeraConfig:
    ...
    MY_FEATURE_TIMEOUT: int = int(os.getenv("MY_FEATURE_TIMEOUT", "30"))
    MY_FEATURE_URL:     str = os.getenv("MY_FEATURE_URL", "http://localhost:1234")
```

```python
from Vera.vera.config import cfg
timeout = cfg.MY_FEATURE_TIMEOUT
```

A tunable private to one module may be read where it is used, but read it once
at module level with an explicit string default and an `or` fallback for empty
values (`int(os.getenv("X", "30") or 30)`), name it with a `VERA_` prefix, and
add it to [§19](#19-complete-environment-variable-reference). Host-derived
defaults should build on `cfg.BACKEND_HOST`, never on a literal hostname.

> [!NOTE]
> `VeraConfig.MODULE_FILES` and `VeraConfig.OLLAMA_INSTANCES` are legacy
> attributes. The module load list actually used is `_module_files` in
> `capability_orchestration.py` ([Capability Framework §11](./01-capability-framework.md#11-module-loading)).

---

## 19. Complete environment variable reference

Every variable read under `vera/` (excluding standard process variables such as
`HOME`, `PATH` and `USER`, thread-count variables Vera sets for libraries, and
entry-point variables of the agent-runtime containers). "Default" is the value
used when the variable is unset; `""` means empty, `(unset)` means the code
treats absence specially (described in the sections above). Where two modules
read the same variable with different defaults, both are shown. Paths are
relative to `vera/`.

### Server, HTTP and process

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `BACKEND_HOST` | `llm.int` | `config.py`, `ide/vscode_capabilities.py` |
| `GOOGLE_OAUTH_CLIENT_ID` | `""` | `config.py` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | `""` | `config.py` |
| `GOOGLE_OAUTH_REDIRECT_BASE` | `http(s)://<BACKEND_HOST>:<ORCHESTRATOR_PORT>` | `config.py` |
| `ORCHESTRATOR_HOST` | `0.0.0.0` | `config.py` |
| `ORCHESTRATOR_PORT` | `8999` | `config.py`, `estate/ops_capabilities.py`, `evolve/evolve_capabilities.py`, `ide/vscode_capabilities.py` |
| `PANEL_CAP_MIRROR` | `1` | `capability_orchestration.py` |
| `TLS_CERTFILE` | `~/.vera/tls/cert.pem` | `config.py`, `ide/vscode_capabilities.py` |
| `TLS_ENABLED` | `0` | `config.py` |
| `TLS_EXTRA_SANS` | `""` | `capability_orchestration.py` |
| `TLS_KEYFILE` | `~/.vera/tls/key.pem` | `config.py` |
| `VERA_ACTIVITY_RECORDING` | `0` | `capability_orchestration.py` |
| `VERA_ADVERTISE_HOST` | `""` | `foundry/foundry_capabilities.py`, `provisioning/components_capabilities.py`, `workers/node_activity_capabilities.py`, `workers/nodes_capabilities.py` |
| `VERA_DATA_DIR` | `~/.vera` | `execution/exec_capabilities.py`, `netmon/netmon_capabilities.py` |
| `VERA_DEV_MODE` | `""` | `capability_orchestration.py` |
| `VERA_GIT_BRANCH` | `(unset)` | `provenance.py` |
| `VERA_GIT_DIRTY` | `(unset)` | `provenance.py` |
| `VERA_GIT_SHA` | `(unset)` | `provenance.py` |
| `VERA_HOST` | `""` | `evolve/evolve_capabilities.py`, `ide/ide_remote_capabilities.py`, `ide/vscode_capabilities.py` |
| `VERA_HOST_IPS` | `""` | `catalog/benchmark_capabilities.py`, `workers/node_activity_capabilities.py` |
| `VERA_HTTP_KEEPALIVE` | `75` | `capability_orchestration.py` |
| `VERA_INSTANCE` | `hostname` | `provenance.py` |
| `VERA_MODULES` | `""` | `capability_orchestration.py` |
| `VERA_NODE_ID` | `""` | `capability_orchestration.py` |
| `VERA_ORCH_PORT` | `(unset → ORCHESTRATOR_PORT)` | `dag/dag_workshop_capabilities.py`, `operator/operator_web_capabilities.py`, `web/own_origin.py` |
| `VERA_ORIGIN_NAME` | `""` | `capability_orchestration.py` |
| `VERA_PORT` | `""` | `ide/ide_remote_capabilities.py`, `ide/vscode_capabilities.py` |
| `VERA_PUBLIC_URL` | `""` | `ide/ide_remote_capabilities.py` |
| `VERA_RESUME_MAX_EVENTS` | `4000` | `capability_orchestration.py` |
| `VERA_RESUME_TTL` | `604800` | `capability_orchestration.py` |
| `VERA_STATE_DIR` | `~/vera-state` | `state_paths.py` |
| `VERA_WS_PING_INTERVAL` | `20` | `capability_orchestration.py` |
| `VERA_WS_PING_TIMEOUT` | `75` | `capability_orchestration.py` |
| `WS_OUT_QUEUE_MAX` | `2000` | `capability_orchestration.py` |

### Backing stores

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `CHROMA_COLLECTION` | `vera_memory` | `fabric/memory.py` |
| `CHROMA_HOST` | `localhost` | `config.py` |
| `CHROMA_PORT` | `8008` | `config.py` |
| `NEO4J_PASS` | `neo4j (cfg) / veraneo4j (compose, docker caps)` | `config.py`, `workers/docker_capabilities.py` |
| `NEO4J_URI` | `bolt://localhost:7687` | `config.py` |
| `NEO4J_USER` | `neo4j` | `config.py`, `workers/docker_capabilities.py` |
| `POSTGRES_URL` | `postgresql://admin:admin@localhost:5433/postgres` | `config.py` |
| `REDIS_URL` | `redis://localhost:6379` | `config.py`, `evolve/evolve_capabilities.py`, `foundry/foundry_capabilities.py`, `provisioning/components_capabilities.py` … |
| `VERA_COORD_REDIS_DB` | `0` | `capability_orchestration.py` |
| `VERA_COORD_REDIS_URL` | `(unset → REDIS_URL)` | `capability_orchestration.py` |
| `VERA_REDIS_AUTH_FILE` | `~/.vera/redis.auth` | `security/redis_auth_core.py` |
| `VERA_REDIS_EXPORT_ROOTS` | `(built-in)` | `security/redis_auth_core.py` |

### Ollama, GPU gate and LLM output budgets

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `GPU_INFER_URL` | `http://192.168.0.250:8765` | `capability_orchestration.py`, `config.py` |
| `OLLAMA_BASE_URL` | `""` | `langgraph/langgraph_entrypoint.py`, `pydanticai/pydanticai_entrypoint.py`, `smolagents/smolagents_entrypoint.py` |
| `OLLAMA_CHARS_PER_TOKEN` | `2.3` | `capability_orchestration.py` |
| `OLLAMA_CONCURRENCY` | `""` | `capability_orchestration.py` |
| `OLLAMA_CPU_A_URL` | `http://192.168.0.246:11435` | `config.py` |
| `OLLAMA_CPU_B_URL` | `http://192.168.0.247:11435` | `config.py` |
| `OLLAMA_CTX_RESERVE_OUT` | `1024` | `capability_orchestration.py` |
| `OLLAMA_CTX_SAFETY_MARGIN` | `256` | `capability_orchestration.py` |
| `OLLAMA_DEFAULT_GPU_VRAM_GB` | `12.0` | `capability_orchestration.py` |
| `OLLAMA_EMBED_CACHE_MAX` | `512` | `capability_orchestration.py` |
| `OLLAMA_EMBED_CACHE_TTL` | `120` | `capability_orchestration.py` |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | `config.py`, `worldview/worldview_jepa.py` |
| `OLLAMA_EMBED_TIMEOUT` | `300` | `capability_orchestration.py` |
| `OLLAMA_EMBED_URL` | `http://192.168.0.246:11435` | `config.py` |
| `OLLAMA_GEN_TIMEOUT` | `900` | `capability_orchestration.py`, `dag/dag_workshop_capabilities.py` |
| `OLLAMA_GPU_URL` | `http://192.168.0.250:11435` | `config.py` |
| `OLLAMA_KEEP_ALIVE` | `10m (orchestrator) / 30m (cfg)` | `capability_orchestration.py`, `config.py`, `research/researcher_api.py` |
| `OLLAMA_MAX_AUTO_CTX` | `65536 (orchestrator) / 0 (researcher_api)` | `capability_orchestration.py`, `research/researcher_api.py` |
| `OLLAMA_MODEL` | `jaahas/qwen3.5-uncensored` | `config.py`, `langgraph/langgraph_entrypoint.py`, `pydanticai/pydanticai_entrypoint.py`, `smolagents/smolagents_entrypoint.py` |
| `OLLAMA_NUM_CTX` | `32768` | `config.py` |
| `OLLAMA_QUEUE_TIMEOUT` | `0` | `capability_orchestration.py` |
| `OLLAMA_SPILL_SAMPLE` | `0.05` | `capability_orchestration.py` |
| `OLLAMA_SPILL_TPS_HINT` | `60` | `capability_orchestration.py` |
| `OLLAMA_STALL_TIMEOUT` | `240` | `capability_orchestration.py` |
| `OLLAMA_VERSION_TTL` | `3600` | `workers/cluster.py` |
| `OLLAMA_VRAM_USABLE_FRAC` | `0.78` | `capability_orchestration.py` |
| `ONNX_RUNTIME_URLS` | `""` | `config.py`, `workers/cluster.py` |
| `VERA_AUTO_CTX_FIT` | `1` | `capability_orchestration.py` |
| `VERA_AVOID_EMBED_RELAX_AT` | `1` | `capability_orchestration.py` |
| `VERA_BACKGROUND_JOB_TYPES` | `dream_director,dream_,director_,idle_,journal_,reflect` | `capability_orchestration.py` |
| `VERA_CAPACITY_TTL_S` | `3600` | `capacity_pool.py` |
| `VERA_CAPACITY_WAIT_S` | `0` | `capacity_pool.py` |
| `VERA_CPU_NODE_THREADS` | `6` | `capability_orchestration.py` |
| `VERA_CTX_STABLE_GPU` | `1` | `capability_orchestration.py` |
| `VERA_EMBED_PROVIDER` | `ollama` | `config.py` |
| `VERA_EMBED_SLOW_COOLDOWN_S` | `30` | `fabric/data_fabric.py`, `fabric/memory.py` |
| `VERA_EMBED_WAIT_S` | `5` | `fabric/data_fabric.py`, `fabric/memory.py` |
| `VERA_FASTEMBED_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | `config.py`, `fabric/fastembed_provider.py` |
| `VERA_GATE_BROKER_SANDBOX` | `""` | `capability_orchestration.py`, `ollama_gate_broker_client.py` |
| `VERA_GATE_BROKER_TOKEN` | `""` | `evolve/evolve_capabilities.py`, `ollama_gate_broker_client.py` |
| `VERA_GATE_BROKER_URL` | `""` | `capability_orchestration.py`, `ollama_gate_broker_client.py`, `workers/routing_parity_core.py` |
| `VERA_GATE_LEASE_TTL_S` | `90` | `ollama_gate.py` |
| `VERA_GATE_MAX_HOLD_S` | `1020` | `capability_orchestration.py` |
| `VERA_GATE_RENEW_S` | `30` | `ollama_gate.py` |
| `VERA_GATE_STALL_COLD_S` | `150` | `capability_orchestration.py` |
| `VERA_GATE_STALL_HOT_S` | `60` | `capability_orchestration.py` |
| `VERA_GATE_TTL_S` | `1800` | `ollama_gate.py` |
| `VERA_GATE_WAIT_S` | `600` | `ollama_gate.py` |
| `VERA_GPU_GATE_N` | `1` | `ollama_gate.py` |
| `VERA_LLM_GEN_CTX` | `16384` | `capabilities/capabilities.py` |
| `VERA_NODE_GATE_N` | `2` | `ollama_gate.py` |
| `VERA_OLLAMA_GATE` | `0` | `evolve/evolve_capabilities.py`, `ollama_gate.py` |
| `VERA_OUTPUT_MAX_TOKENS` | `16384` | `capability_orchestration.py` |
| `VERA_OUTPUT_MAX_TOKENS_CPU` | `3072` | `capability_orchestration.py` |
| `VERA_OUTPUT_MAX_TOKENS_GPU` | `0` | `capability_orchestration.py` |
| `VERA_PROD_URL` | `""` | `workers/routing_parity_core.py` |
| `VERA_ROUTING_PARITY` | `1` | `workers/routing_parity_core.py` |

### Distributed workers, cluster and node agents

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `CLUSTER_POLL_INTERVAL` | `10` | `workers/cluster.py` |
| `LOCAL_OLLAMA_INSTANCE` | `""` | `workers/cluster.py` |
| `PROXY_MAX_CONCURRENCY` | `3` | `workers/cluster.py` |
| `PROXY_QUEUE_MAX` | `50` | `workers/cluster.py` |
| `PROXY_QUEUE_TIMEOUT` | `120` | `workers/cluster.py` |
| `SYSLOG_CODE_LINES` | `30` | `workers/syslog.py` |
| `SYSLOG_ERR_MAXLEN` | `3000` | `workers/syslog.py` |
| `SYSLOG_MAXLEN` | `5000` | `config.py`, `workers/syslog.py` |
| `SYSLOG_MONITOR` | `1 (cfg) / 0 (syslog module)` | `config.py`, `workers/syslog.py` |
| `SYSLOG_MONITOR_INT` | `300` | `config.py`, `workers/syslog.py` |
| `SYSMON_HISTORY_MAX` | `720` | `monitor/monitor_capabilities.py` |
| `SYSMON_SAMPLE_SEC` | `10` | `monitor/monitor_capabilities.py` |
| `VERA_CONSUMER_DETAIL_MAX` | `200` | `workers/job_persistance.py` |
| `VERA_CONSUMER_STALE_IDLE_MS` | `600000` | `workers/job_persistance.py` |
| `VERA_DISPATCH_CONFIRM_N` | `3` | `workers/node_agent_capabilities.py` |
| `VERA_DISPATCH_PROBE_S` | `25` | `workers/node_agent_capabilities.py` |
| `VERA_IS_WORKER` | `""` | `capability_orchestration.py` |
| `VERA_JOB_ARCHIVE_PG` | `1` | `workers/job_persistance.py` |
| `VERA_JOB_IDX_MAX` | `5000` | `workers/job_persistance.py` |
| `VERA_JOB_PROMPT_FULL_MAX` | `16000` | `workers/job_persistance.py` |
| `VERA_JOB_TTL` | `604800 (7 days)` | `workers/job_persistance.py` |
| `VERA_NODE_AGENT_PORT` | `8770` | `workers/node_agent_capabilities.py` |
| `VERA_NODE_SYNC` | `""` | `provisioning/components_capabilities.py` |
| `VERA_NODE_TOKEN` | `""` | `workers/node_agent_capabilities.py` |
| `VERA_RECOVERY_IDLE_MS` | `120000` | `workers/job_persistance.py` |
| `VERA_RUNNER_REAP` | `0` | `workers/node_agent_capabilities.py` |
| `VERA_RUNNER_REAP_MAX` | `2` | `workers/node_agent_capabilities.py` |
| `VERA_RUNNER_STUCK_S` | `0` | `workers/node_agent_capabilities.py` |
| `VERA_SPAWN_WORKERS` | `32` | `execution/spawn_core.py` |
| `VERA_VENV` | `~/vera-env` | `workers/workers.py` |
| `VERA_WORKER_CLASSES` | `(unset → roles registry, else CPU default)` | `capability_orchestration.py` |
| `VERA_WORKER_COMMIT` | `""` | `capability_orchestration.py` |
| `VERA_WORKER_HOST_ID` | `""` | `capability_orchestration.py` |
| `VERA_WORKER_HOST_ONLY` | `""` | `workers/worker_placement_core.py` |
| `VERA_WORKER_IMAGE` | `vera:latest` | `evolve/evolve_capabilities.py`, `workers/docker_capabilities.py` |
| `VERA_WORKER_NODE_OK` | `""` | `workers/worker_placement_core.py` |

### Policy, telemetry and provenance

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `(unset)` | `execution/portable_telemetry.py` |
| `OTEL_EXPORTER_OTLP_HEADERS` | `(unset)` | `execution/portable_telemetry.py` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `(unset)` | `execution/portable_telemetry.py` |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `(unset)` | `execution/portable_telemetry.py` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `(unset)` | `execution/portable_telemetry.py` |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | `(unset)` | `execution/portable_telemetry.py` |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `(unset)` | `execution/portable_telemetry.py` |
| `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` | `(unset)` | `execution/portable_telemetry.py` |
| `VERA_AGENT_WORKFLOW_RUNTIME` | `1` | `execution/workflow_runtime_adapter.py` |
| `VERA_CAP_ONTOLOGY_AUTO_GENERATION` | `(unset)` | `ontologies/capability_ontology_snapshot.py` |
| `VERA_CAP_ONTOLOGY_GENERATED_RELATIONS` | `(unset)` | `ontologies/capability_ontology_snapshot.py` |
| `VERA_INTEGRATION_API_EFFECT_ENFORCEMENT` | `""` | `integrations/integrations_capabilities.py` |
| `VERA_OTLP_AUTO_EXPORT` | `(unset → off)` | `capability_orchestration.py`, `execution/portable_telemetry.py`, `execution/run_projection.py` |
| `VERA_OTLP_BATCH_SIZE` | `8 (1–32)` | `execution/portable_telemetry.py` |
| `VERA_OTLP_DEDUPE_SIZE` | `(unset)` | `execution/portable_telemetry.py` |
| `VERA_OTLP_FLUSH_MS` | `(unset)` | `execution/portable_telemetry.py` |
| `VERA_OTLP_MAX_REQUEST_BYTES` | `(unset)` | `execution/portable_telemetry.py` |
| `VERA_OTLP_QUEUE_SIZE` | `(unset)` | `execution/portable_telemetry.py` |
| `VERA_OTLP_RETRY_BACKOFF_MS` | `(unset)` | `execution/portable_telemetry.py` |
| `VERA_OTLP_RETRY_MAX` | `(unset)` | `execution/portable_telemetry.py` |
| `VERA_POLICY_ENFORCE_FAMILIES` | `""` | `capability_enforcement.py` |
| `VERA_POLICY_MODE` | `shadow` | `capability_enforcement.py` |
| `VERA_RUN_JOURNAL_PATH` | `""` | `execution/run_projection.py` |

### Diagnostics, GC and logging

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `VERA_GC_FULL_EVERY` | `15` | `capability_orchestration.py` |
| `VERA_GC_GEN0` | `10000` | `capability_orchestration.py` |
| `VERA_GC_GEN1` | `25` | `capability_orchestration.py` |
| `VERA_GC_GEN2` | `25` | `capability_orchestration.py` |
| `VERA_GC_PACE_S` | `120` | `capability_orchestration.py` |
| `VERA_GC_WARN_MS` | `200` | `capability_orchestration.py` |
| `VERA_LOG_BACKUPS` | `5` | `log_setup.py`, `monitor/perf_capabilities.py` |
| `VERA_LOG_DIR` | `<repo>/logs` | `monitor/perf_capabilities.py` |
| `VERA_LOG_FILE` | `""` | `log_setup.py` |
| `VERA_LOG_FILE_ENABLED` | `on` | `log_setup.py` |
| `VERA_LOG_FILE_LEVEL` | `(unset)` | `log_setup.py` |
| `VERA_LOG_MAX_BYTES` | `10485760 (10 MiB)` | `log_setup.py`, `monitor/perf_capabilities.py` |
| `VERA_LOG_RING` | `3000` | `monitor/perf_capabilities.py` |
| `VERA_LOOP_HANG_DUMP_S` | `1` | `capability_orchestration.py` |
| `VERA_LOOP_LAG_WARN_MS` | `500` | `capability_orchestration.py` |
| `VERA_PERF_EVENTS_MAX` | `300` | `capability_orchestration.py` |
| `VERA_PERF_GATE_MAX_CRIT` | `0` | `monitor/perf_capabilities.py` |
| `VERA_PERF_GATE_MAX_WARN` | `4` | `monitor/perf_capabilities.py` |
| `VERA_PERF_GATE_STRICT` | `""` | `monitor/perf_capabilities.py` |

### Dev sandboxes, Loop Lab and self-improvement

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `FABRIC_SQLITE` | `vera/fabric/vera_fabric.db` | `evolve/evolve_capabilities.py`, `fabric/data_fabric.py` |
| `VERA_CENSUS_DIR` | `""` | `census/census_capabilities.py`, `evolve/evolve_capabilities.py`, `evolve/schedule_capabilities.py` |
| `VERA_CENSUS_WALL_CAP_S` | `1800` | `census/census_capabilities.py` |
| `VERA_DEV_CODE_IMAGE` | `codercom/code-server:latest` | `evolve/evolve_capabilities.py` |
| `VERA_DEV_CODE_PORT` | `8996` | `evolve/evolve_capabilities.py` |
| `VERA_DEV_FOLLOW_HOST` | `""` | `evolve/evolve_capabilities.py` |
| `VERA_DEV_PORT` | `8998` | `evolve/evolve_capabilities.py` |
| `VERA_DEV_REDIS_DB` | `3` | `evolve/evolve_capabilities.py` |
| `VERA_DISK_SWEEP_INTERVAL` | `900` | `evolve/evolve_capabilities.py` |
| `VERA_DOCKER_DISK_MOUNT` | `""` | `evolve/evolve_capabilities.py` |
| `VERA_HTTP_UA` | `(built-in UA)` | `remote/session_sandbox_capabilities.py` |
| `VERA_IS_DEV_SANDBOX` | `(unset → off)` | `capability_orchestration.py`, `evolve/evolve_capabilities.py`, `evolve/instance_identity.py`, `evolve/sandbox_registry_reconstruction.py` … |
| `VERA_LOCAL_SANDBOX_ROOT` | `""` | `remote/session_sandbox_capabilities.py` |
| `VERA_MAINLINE_MIRROR_REFRESH_INTERVAL_S` | `86400` | `evolve/evolve_capabilities.py` |
| `VERA_OPS_SECRETS_DIR` | `(unset)` | `foundry/foundry_capabilities.py` |
| `VERA_ORCHESTRATOR_ENABLED` | `(unset → off)` | `evolve/orchestrator_capabilities.py` |
| `VERA_ORCHESTRATOR_INTERVAL_S` | `60` | `evolve/orchestrator_capabilities.py` |
| `VERA_ORCHESTRATOR_LIVE` | `(unset → off)` | `evolve/orchestrator_capabilities.py` |
| `VERA_REPO_URL` | `""` | `provisioning/components_capabilities.py` |
| `VERA_SANDBOX_AUTOSYNC_SEC` | `900` | `remote/session_sandbox_capabilities.py` |
| `VERA_SANDBOX_HTTP_UA` | `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36` | `remote/session_sandbox_capabilities.py` |
| `VERA_SANDBOX_IDLE_ARCHIVE_DAYS` | `7` | `remote/session_sandbox_capabilities.py` |
| `VERA_SANDBOX_IDLE_PAUSE_S` | `1800` | `evolve/evolve_capabilities.py` |
| `VERA_SANDBOX_IDLE_SLEEP_MIN` | `30` | `remote/session_sandbox_capabilities.py` |
| `VERA_SANDBOX_IDLE_SWEEP_INTERVAL_S` | `300` | `evolve/evolve_capabilities.py` |
| `VERA_SANDBOX_LOG_INTERVAL` | `10` | `evolve/evolve_capabilities.py` |
| `VERA_SANDBOX_PKG_HEADLESS_AUTO` | `(unset → per-sandbox config)` | `remote/session_sandbox_capabilities.py` |
| `VERA_SANDBOX_READY_CAP` | `loops.run` | `evolve/evolve_capabilities.py` |
| `VERA_SANDBOX_REAP_MAX` | `200` | `remote/session_sandbox_capabilities.py` |
| `VERA_SANDBOX_WRITE_GUARD` | `1` | `sandbox_guard.py` |
| `VERA_SBX_STATE_TTL` | `3.0` | `remote/session_sandbox_capabilities.py` |
| `VERA_SCAFFOLD_SWEEP_ENABLED` | `1` | `evolve/evolve_capabilities.py` |
| `VERA_SCAFFOLD_SWEEP_INTERVAL_S` | `3600` | `evolve/evolve_capabilities.py` |
| `VERA_SESSION_SANDBOX_IMAGE` | `python:3.12-slim` | `remote/session_sandbox_capabilities.py` |
| `VERA_SESSION_SANDBOX_RETAIN_HOURS` | `24` | `evolve/evolve_capabilities.py` |
| `VERA_SWEEP_STARTUP_GRACE_S` | `180` | `evolve/evolve_capabilities.py` |
| `VERA_UPSTREAM_READ_GROUPS` | `""` | `sandbox_guard.py` |
| `VERA_UPSTREAM_READ_TIMEOUT_S` | `20` | `capability_orchestration.py` |
| `VERA_UPSTREAM_READ_URL` | `(unset)` | `sandbox_guard.py` |
| `VERA_WORKTREE_CLAIM_TTL_H` | `12` | `evolve/evolve_capabilities.py` |
| `VSCODE_PUBLIC_HOST` | `""` | `evolve/evolve_capabilities.py`, `ide/vscode_capabilities.py` |

### Chat, agents and agentic loops

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `AGENT_COMPACT_SUMMARY_TIMEOUT` | `20` | `agents/agents.py` |
| `AGENT_CTX_INJECT_TIMEOUT` | `12` | `agents/agents.py` |
| `DAG_QUERY_EMBED_WAIT_S` | `30` | `dag/dag_store.py` |
| `V5_ARTIFACT_CACHE_MAX` | `400000` | `dag/dag_workshop_capabilities.py` |
| `V5_AUTHOR_MAX_ATTEMPTS` | `3` | `dag/dag_workshop_capabilities.py` |
| `V5_EDIT_MAX_ATTEMPTS` | `3` | `dag/dag_workshop_capabilities.py` |
| `V5_GEN_INSTEP_MAX` | `24000` | `dag/dag_workshop_capabilities.py` |
| `V5_GEN_SAVE_MIN` | `300` | `dag/dag_workshop_capabilities.py` |
| `V5_INLINE_FILE_MAX` | `2` | `dag/dag_workshop_capabilities.py` |
| `V5_INLINE_FILE_TOTAL` | `4000` | `dag/dag_workshop_capabilities.py` |
| `V5_PREVIEW_FILEREAD` | `8000` | `dag/dag_workshop_capabilities.py` |
| `V5_PREVIEW_LONGFORM` | `12000` | `dag/dag_workshop_capabilities.py` |
| `V5_UTILITY_TIMEOUT` | `240` | `dag/dag_workshop_capabilities.py` |
| `VERA_CHATMEM_ACTIVE_TTL_S` | `3600` | `fabric/memory_hooks.py` |
| `VERA_CHATMEM_DECAY_FACTOR` | `0.65` | `fabric/memory_hooks.py` |
| `VERA_CHATMEM_DECAY_MAX_TURNS` | `6` | `fabric/memory_hooks.py` |
| `VERA_CHATMEM_DECAY_MIN` | `0.05` | `fabric/memory_hooks.py` |
| `VERA_CHATMEM_FAST_TIMEOUT_S` | `2.5` | `fabric/memory_hooks.py` |
| `VERA_CHATMEM_PENDING_TTL_S` | `300` | `fabric/memory_hooks.py` |
| `VERA_CHAT_CTX_RESERVE_OUT` | `8192` | `agents/agents.py` |
| `VERA_CHAT_ENTITY_EXTRACT` | `1` | `fabric/memory_hooks.py` |
| `VERA_CHAT_FIRST_TOKEN_S` | `90` | `agents/agents.py` |
| `VERA_CHAT_GATE_WAIT_S` | `600` | `agents/agents.py` |
| `VERA_CHAT_PS_TIMEOUT_S` | `3` | `agents/agents.py` |
| `VERA_CHAT_QUEUE_REFRESH_S` | `5` | `agents/agents.py` |
| `VERA_CHAT_RUNNER_PROBE_S` | `3` | `agents/agents.py` |
| `VERA_CODE_AUTHOR_REPAIR_COLLAPSE_MIN` | `400` | `dag/code_author_guards.py` |
| `VERA_CODE_AUTHOR_REPAIR_COLLAPSE_RATIO` | `0.5` | `dag/code_author_guards.py` |
| `VERA_CODE_AUTHOR_SMOKE_ATTEMPTS` | `2` | `dag/dag_workshop_capabilities.py` |
| `VERA_CODE_AUTHOR_SMOKE_RUN` | `1` | `dag/dag_workshop_capabilities.py` |
| `VERA_CODE_AUTHOR_SMOKE_TIMEOUT` | `25` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_ALLOW_LLM_CAPS` | `0` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_ALLOW_RESEARCH` | `0` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_CODE_ROLE` | `1` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_DETERMINISTIC` | `1` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_GEN_AUTOSAVE` | `1` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_MINIMAL_PLAN` | `1` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_NUM_PREDICT` | `4096` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_SEED` | `7` | `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_STALE_SECS` | `600` | `activity/activity_capabilities.py`, `dag/dag_workshop_capabilities.py` |
| `VERA_LOOP_TEMP` | `0` | `dag/dag_workshop_capabilities.py` |
| `VERA_OPERATOR_BROWSER_LINGER_S` | `300` | `operator/operator_web_capabilities.py` |
| `VERA_OPERATOR_REPEAT_LIMIT` | `5` | `operator/operator_loop.py` |
| `VERA_OPERATOR_REPEAT_TOTAL` | `5` | `operator/repeat_guard.py` |
| `VERA_OPERATOR_SESSION_IDLE_S` | `1800` | `operator/operator_web_capabilities.py` |
| `VERA_OPERATOR_STEP_BUDGET_S` | `600` | `operator/operator_step_budget.py` |
| `VERA_OPERATOR_THINK_ERROR_LIMIT` | `3` | `operator/operator_loop.py` |
| `VERA_OPERATOR_THINK_JOB` | `loop_executor` | `operator/thinker.py` |
| `VERA_OPERATOR_THINK_TOKENS` | `2048` | `operator/thinker.py` |
| `VERA_PLANNER_NONDET` | `1` | `dag/planner_core.py` |
| `VERA_PLANNER_TEMP` | `0.4` | `dag/planner_core.py` |
| `VERA_PLANNER_TIMEOUT_S` | `600` | `dag/dag_workshop_capabilities.py` |
| `VERA_RESEARCHER_URL` | `http://localhost:8765` | `dag/dag_workshop_capabilities.py`, `dream/dream_capabilities.py`, `dream/project_capabilities.py` |
| `VERA_RESEARCH_KEEP_MAX` | `24000` | `dag/dag_workshop_capabilities.py` |
| `VERA_V5_RECOVERY_ATTEMPTS` | `2` | `dag/dag_workshop_capabilities.py` |

### Data fabric, memory and research

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `COLLECTOR_ARXIV_DELAY_S` | `3` | `fabric/data_fabric_collectors.py` |
| `COLLECTOR_CVE_DELAY_S` | `6` | `fabric/data_fabric_collectors.py` |
| `COLLECTOR_DEFAULT_DELAY_S` | `2` | `fabric/data_fabric_collectors.py` |
| `COLLECTOR_DOCS_DELAY_S` | `3` | `fabric/data_fabric_collectors.py` |
| `COLLECTOR_GITHUB_DELAY_S` | `10` | `fabric/data_fabric_collectors.py` |
| `COLLECTOR_HN_DELAY_S` | `1` | `fabric/data_fabric_collectors.py` |
| `COLLECTOR_WIKI_DELAY_S` | `1` | `fabric/data_fabric_collectors.py` |
| `EMBED_CAPS_ON_START` | `1` | `config.py`, `dag/dag_store.py` |
| `FABRIC_ARTIFACT_MAX_GET_BYTES` | `8388608 (8 MiB)` | `fabric/data_fabric.py` |
| `FABRIC_ARTIFACT_MAX_PUT_BYTES` | `67108864 (64 MiB)` | `fabric/data_fabric.py` |
| `FABRIC_ARTIFACT_POLICY` | `""` | `fabric/data_fabric.py` |
| `FABRIC_ARTIFACT_REPLICA` | `none` | `fabric/data_fabric.py` |
| `FABRIC_ARTIFACT_ROOT` | `vera/fabric/artifact_store` | `fabric/data_fabric.py` |
| `FABRIC_CACHE_TTL` | `3600` | `fabric/data_fabric.py` |
| `FABRIC_CRAWL_DELAY_S` | `2` | `fabric/discovery.py`, `fabric/fabric_web_acquisition.py` |
| `FABRIC_DISCOVER_DELAY_S` | `value of FABRIC_CRAWL_DELAY_S (2)` | `fabric/discovery.py` |
| `FABRIC_FAISS` | `0` | `fabric/data_fabric.py` |
| `FABRIC_FAISS_INDEX` | `flat` | `fabric/data_fabric.py` |
| `FABRIC_FAISS_SHARDS` | `4` | `fabric/data_fabric.py` |
| `FABRIC_GLINER_LABELS` | `""` | `fabric/fabric_web_acquisition.py` |
| `FABRIC_GLINER_MODEL` | `urchade/gliner_medium-v2.1` | `catalog/specialist_capabilities.py`, `fabric/fabric_web_acquisition.py`, `research/explode_capabilities.py` |
| `FABRIC_GLINER_THRESHOLD` | `0.4` | `fabric/fabric_web_acquisition.py` |
| `FABRIC_HOST_FETCH_CONCURRENCY` | `2` | `fabric/discovery.py` |
| `FABRIC_MIN_SCORE` | `0.28` | `fabric/data_fabric.py` |
| `FABRIC_NER_BACKEND` | `auto` | `catalog/specialist_capabilities.py`, `fabric/fabric_web_acquisition.py` |
| `FABRIC_NER_MODEL` | `en_core_web_sm` | `catalog/specialist_capabilities.py`, `fabric/fabric_web_acquisition.py` |
| `FABRIC_OBJECT_STORE` | `none` | `fabric/data_fabric.py` |
| `FABRIC_REVISION_POLICY` | `""` | `fabric/data_fabric.py` |
| `FABRIC_REVISION_SQLITE` | `vera/fabric/vera_fabric_revisions.db` | `fabric/data_fabric.py` |
| `FABRIC_RRF_K` | `60` | `fabric/data_fabric.py` |
| `FABRIC_S3_ACCESS` | `""` | `fabric/data_fabric.py`, `provisioning/stores_capabilities.py` |
| `FABRIC_S3_BUCKET` | `vera-data-fabric` | `fabric/data_fabric.py`, `provisioning/stores_capabilities.py` |
| `FABRIC_S3_ENDPOINT` | `http://localhost:3900` | `fabric/data_fabric.py` |
| `FABRIC_S3_REGION` | `garage` | `fabric/data_fabric.py`, `provisioning/stores_capabilities.py` |
| `FABRIC_S3_SECRET` | `""` | `fabric/data_fabric.py`, `provisioning/stores_capabilities.py` |
| `FABRIC_SEEK_MIN_SCORE` | `0.28` | `fabric/memory_retrieval.py` |
| `FABRIC_SPEC_FETCH_BYTES` | `2000000` | `fabric/discovery.py` |
| `FABRIC_STREAM_KEY` | `vera:fabric:ingest` | `fabric/data_fabric.py` |
| `FABRIC_SUBTABLE_MAX_ROWS` | `500` | `fabric/discovery.py` |
| `FABRIC_VECTOR_DIM` | `768` | `fabric/data_fabric.py` |
| `FABRIC_WEAK_BELOW` | `0.42` | `fabric/data_fabric.py` |
| `MAX_CAPS_IN_PROMPT` | `25` | `config.py`, `dag/dag_store.py` |
| `MEMORY_AUTO_EMBED` | `1` | `fabric/memory.py` |
| `MEMORY_SEEK_EMBED_WAIT_S` | `10` | `fabric/memory_retrieval.py` |
| `RESEARCH_FAST_TIMEOUT` | `90` | `research/researcher_api.py` |
| `VERA_FABRIC_NO_EMBED` | `(unset)` | `fabric/data_fabric.py` |
| `VERA_MEMORY_BACKEND_TIMEOUT_S` | `10` | `fabric/memory.py` |
| `VERA_MEMORY_TOOLING` | `canonical` | `fabric/memory_retrieval.py` |
| `VERA_NER_MODEL` | `djagatiya/ner-roberta-base-ontonotesv5-englishv4` | `research/nlp_capabilities.py` |
| `VERA_NLP_PORT` | `8771` | `research/nlp_dispatch.py` |
| `VERA_RERANK_ENABLED` | `0` | `research/researcher_api.py` |
| `VERA_RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | `research/nlp_capabilities.py` |
| `VERA_RESEARCH_PERSIST` | `1` | `research/researcher_api.py` |
| `VERA_SENTIMENT_MODEL` | `distilbert-base-uncased-finetuned-sst-2-english` | `research/nlp_capabilities.py` |

### Web, search and browser

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `BRAVE_API_KEY` | `""` | `web/web_capabilities.py` |
| `BROWSER_HEADLESS` | `1` | `web/browser_capabilities.py` |
| `BROWSER_MAX_SESSIONS` | `3` | `web/browser_capabilities.py` |
| `BROWSER_SCREENSHOT_Q` | `85` | `web/browser_capabilities.py` |
| `BROWSER_TIMEOUT_MS` | `30000` | `web/browser_capabilities.py` |
| `BROWSER_USER_AGENT` | `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36` | `web/browser_capabilities.py` |
| `BROWSER_VIEWPORT_H` | `900` | `web/browser_capabilities.py` |
| `BROWSER_VIEWPORT_W` | `1280` | `web/browser_capabilities.py` |
| `VERA_SEARXNG_URL` | `http://<BACKEND_HOST>:8888` | `media/media_capabilities.py`, `research/researcher_api.py`, `web/web_capabilities.py` |
| `VERA_WEB_DISCOVER_MODE` | `background` | `web/web_capabilities.py` |
| `VERA_WEB_DOMAIN_INTERVAL` | `1.0` | `web/web_client.py` |
| `VERA_WEB_DOMAIN_JITTER` | `0.6` | `web/web_client.py` |
| `VERA_WEB_MAX_PAGE_CHARS` | `16000` | `web/web_client.py` |
| `VERA_WEB_READER` | `https://r.jina.ai/` | `web/web_client.py` |
| `VERA_WEB_READER_KEY` | `""` | `web/web_client.py` |
| `VERA_WEB_READER_TIMEOUT` | `25.0` | `web/web_client.py` |
| `VERA_WEB_REQUEST_ATTEMPTS` | `2` | `web/web_client.py` |
| `VERA_WEB_RETRY_BASE_S` | `0.25` | `web/web_client.py` |
| `VERA_WEB_RETRY_MAX_S` | `5.0` | `web/web_client.py` |
| `VERA_WEB_REWRITES` | `""` | `web/web_client.py` |
| `VERA_WEB_TIMEOUT` | `8.0` | `web/web_capabilities.py`, `web/web_client.py` |
| `VERA_WEB_UA` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36` | `web/web_client.py` |

### Execution, Docker and remote hosts

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `DOCKER_HOST` | `""` | `remote/remote_capabilities.py`, `remote/session_sandbox_capabilities.py`, `workers/docker_capabilities.py` |
| `DOCKER_SOCK` | `/var/run/docker.sock` | `ide/ide_capabilities.py`, `workers/docker_capabilities.py` |
| `VERA_BASH_BIN` | `/bin/bash` | `execution/exec_capabilities.py` |
| `VERA_DOCKER_AUTOFIX` | `1` | `workers/docker_host_health_capabilities.py` |
| `VERA_DOCKER_HOSTS` | `~/.vera_docker_hosts.json` | `workers/docker_capabilities.py` |
| `VERA_DOCKER_STATS_CONCURRENCY` | `8` | `workers/docker_capabilities.py` |
| `VERA_DOCKER_STUCK_EXEC_S` | `3600` | `workers/docker_host_health_capabilities.py` |
| `VERA_DOCKER_WATCH_S` | `300` | `workers/docker_host_health_capabilities.py` |
| `VERA_ENRICH_TTL` | `604800 (7 days)` | `execution/exec_capabilities.py` |
| `VERA_EXEC_SANDBOX` | `~/.vera_exec_sandbox.json` | `execution/exec_capabilities.py` |
| `VERA_EXEC_TIMEOUT` | `600` | `execution/exec_capabilities.py` |
| `VERA_PS_BIN` | `"" (auto-detect)` | `execution/exec_capabilities.py` |
| `VERA_SSH_HOSTS_TTL` | `10` | `execution/exec_capabilities.py` |
| `VERA_SSH_STORE` | `~/.vera_ssh_hosts.json` | `execution/exec_capabilities.py` |

### IDE, VS Code and external agent sessions

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `ANTHROPIC_API_KEY` | `""` | `ide/ide_remote_capabilities.py` |
| `CLAUDE_SESSION_ID` | `(unset)` | `ide/vera_mcp_bridge.py` |
| `CODEX_SESSION_ID` | `(unset)` | `ide/vera_mcp_bridge.py` |
| `GITEA_BASE_URL` | `""` | `config.py` |
| `GITEA_OWNER` | `""` | `config.py` |
| `GITEA_TOKEN` | `""` | `config.py` |
| `GIT_CLONE_ROOT` | `~/vera_repos` | `config.py` |
| `VERA_CLAUDE_PERMISSION_MODE` | `acceptEdits` | `ide/ide_remote_capabilities.py` |
| `VERA_CLAUDE_PROJECTS_ROOTS` | `""` | `ide/ide_claude_sessions_capabilities.py` |
| `VERA_CLAUDE_SESSION` | `(unset)` | `ide/vera_mcp_bridge.py` |
| `VERA_CLAUDE_SESSIONS_INGEST_INTERVAL` | `300` | `ide/ide_claude_sessions_capabilities.py` |
| `VERA_CODEX_SESSIONS_ROOTS` | `""` | `ide/ide_claude_sessions_capabilities.py` |
| `VERA_EMBED_CLAUDE_SESSIONS` | `(unset → off)` | `ide/ide_claude_sessions_capabilities.py` |
| `VERA_PROJECT_ROOT` | `~/vera_projects` | `config.py` |
| `VERA_SESSION_AUTORESUME_ENABLED` | `(unset → off)` | `ide/session_watch_capabilities.py` |
| `VERA_SESSION_AUTORESUME_INTERVAL_S` | `300` | `ide/session_watch_capabilities.py` |
| `VERA_WORKSPACE` | `""` | `ide/ide_capabilities.py` |
| `VSCODE_CENTRAL_CONTAINER` | `vera-vscode` | `ide/vscode_capabilities.py` |
| `VSCODE_CENTRAL_EXTENSIONS` | `""` | `ide/vscode_capabilities.py` |
| `VSCODE_CENTRAL_IMAGE` | `codercom/code-server:latest` | `ide/vscode_capabilities.py` |
| `VSCODE_CENTRAL_NETWORK` | `""` | `ide/vscode_capabilities.py` |
| `VSCODE_CENTRAL_PORT` | `8843` | `ide/vscode_capabilities.py` |
| `VSCODE_CENTRAL_URL` | `""` | `ide/vscode_capabilities.py` |
| `VSCODE_PASSWORD` | `""` | `ide/vscode_capabilities.py` |
| `VSCODE_PROJECTS_VOLUME` | `""` | `ide/vscode_capabilities.py` |
| `VSCODE_WORKER_IMAGE` | `value of VSCODE_CENTRAL_IMAGE` | `ide/vscode_capabilities.py` |
| `VSCODE_WORKER_PORT_BASE` | `8860` | `ide/vscode_capabilities.py` |

### Secrets, identity and provisioning

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `BAO_ADDR` | `falls back to VAULT_ADDR` | `provisioning/openbao_identity.py`, `provisioning/provisioning_capabilities.py`, `security/secrets.py`, `security/secrets_capabilities.py` |
| `BAO_KV_MOUNT` | `(unset)` | `provisioning/provisioning_capabilities.py`, `security/secrets.py` |
| `BAO_NAMESPACE` | `(unset)` | `provisioning/provisioning_capabilities.py`, `security/secrets.py` |
| `BAO_TOKEN` | `(unset)` | `provisioning/openbao_identity.py`, `provisioning/provisioning_capabilities.py`, `security/secrets.py` |
| `BAO_VERIFY_TLS` | `""` | `provisioning/provisioning_capabilities.py`, `security/secrets.py` |
| `GARAGE_ADMIN_TOKEN` | `vera-garage-admin-change-me` | `provisioning/stores_capabilities.py` |
| `GARAGE_ADMIN_URL` | `""` | `provisioning/stores_capabilities.py` |
| `VAULT_ADDR` | `(unset)` | `provisioning/openbao_identity.py`, `security/secrets.py` |
| `VAULT_TOKEN` | `(unset)` | `provisioning/openbao_identity.py`, `security/secrets.py` |
| `VERA_KEYDROP_AUTHOR` | `(unset)` | `security/secrets_capabilities.py` |
| `VERA_KEYDROP_DIR` | `~/.vera-keydrop` | `security/secrets_capabilities.py` |
| `VERA_SECRET_BACKEND` | `"" (auto; provisioning sets openbao)` | `provisioning/provisioning_capabilities.py`, `security/secrets.py` |
| `VERA_SECRET_KEY` | `""` | `security/secrets.py` |

### Agent runtimes and model backends

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `LANGGRAPH_ENABLED` | `0` | `langgraph/langgraph_capabilities.py` |
| `LANGGRAPH_IMAGE` | `vera-langgraph:latest` | `agentbridges/runtime_registry.py`, `langgraph/langgraph_capabilities.py` |
| `LANGGRAPH_MAX_STEPS` | `8` | `langgraph/langgraph_entrypoint.py` |
| `LANGGRAPH_STALL_S` | `60` | `langgraph/langgraph_capabilities.py` |
| `LANGGRAPH_TIMEOUT_S` | `300` | `langgraph/langgraph_capabilities.py` |
| `OPENCLAW_AGENT_ID` | `main` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_CLIENT_ID` | `cli` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_CLIENT_MODE` | `cli` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_ENABLED` | `0` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_OLLAMA_MODEL` | `value of OLLAMA_MODEL` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_RUN_TIMEOUT` | `900` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_TOKEN` | `""` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_USE_VERA_OLLAMA` | `0` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_VERA_BASE_URL` | `http://localhost:8000` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_VERA_OLLAMA_BASE` | `http://<BACKEND_HOST>:<ORCHESTRATOR_PORT>/ollama` | `openclaw/openclaw_capabilities.py` |
| `OPENCLAW_WS_URL` | `ws://localhost:18789` | `openclaw/openclaw_capabilities.py` |
| `PYDANTICAI_ENABLED` | `0` | `pydanticai/pydanticai_capabilities.py` |
| `PYDANTICAI_IMAGE` | `vera-pydanticai:latest` | `pydanticai/pydanticai_capabilities.py` |
| `PYDANTICAI_MAX_STEPS` | `8` | `pydanticai/pydanticai_entrypoint.py` |
| `PYDANTICAI_STALL_S` | `60` | `pydanticai/pydanticai_capabilities.py` |
| `PYDANTICAI_TIMEOUT_S` | `300` | `pydanticai/pydanticai_capabilities.py` |
| `SMOLAGENTS_ENABLED` | `0` | `smolagents/smolagents_capabilities.py` |
| `SMOLAGENTS_IMAGE` | `vera-smolagents:latest` | `smolagents/smolagents_capabilities.py` |
| `SMOLAGENTS_STALL_S` | `60` | `smolagents/smolagents_capabilities.py` |
| `SMOLAGENTS_TIMEOUT_S` | `300` | `smolagents/smolagents_capabilities.py` |
| `VLLM_API_KEY` | `""` | `vllm/vllm_capabilities.py` |
| `VLLM_CPU_OFFLOAD_GB` | `0` | `vllm/vllm_capabilities.py` |
| `VLLM_DTYPE` | `auto` | `vllm/vllm_capabilities.py` |
| `VLLM_ENABLE_LORA` | `0` | `vllm/vllm_capabilities.py` |
| `VLLM_GPU_MEM_UTIL` | `0.90` | `vllm/vllm_capabilities.py` |
| `VLLM_INSTANCES` | `""` | `vllm/vllm_capabilities.py` |
| `VLLM_MAX_LORAS` | `4` | `vllm/vllm_capabilities.py` |
| `VLLM_MAX_MODEL_LEN` | `0` | `vllm/vllm_capabilities.py` |
| `VLLM_MODEL` | `""` | `vllm/vllm_capabilities.py` |
| `VLLM_QUANTIZATION` | `""` | `vllm/vllm_capabilities.py` |
| `VLLM_SPEC_MODEL` | `""` | `vllm/vllm_capabilities.py` |
| `VLLM_TENSOR_PARALLEL` | `1` | `vllm/vllm_capabilities.py` |

### Models, catalog and machine learning

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `CIVITAI_API_BASE` | `https://civitai.com/api/v1` | `capabilities/capabilities.py` |
| `CIVITAI_PROXY` | `""` | `capabilities/capabilities.py` |
| `CIVITAI_TOKEN` | `""` | `capabilities/capabilities.py` |
| `HF_API_BASE` | `https://huggingface.co/api` | `capabilities/capabilities.py` |
| `HF_TOKEN` | `""` | `capabilities/capabilities.py`, `catalog/catalog_capabilities.py`, `workers/nodes_capabilities.py` |
| `HUGGINGFACE_TOKEN` | `(unset)` | `catalog/catalog_capabilities.py`, `workers/nodes_capabilities.py` |
| `ML_ONNX_DIR` | `<repo>/edge/models` | `machine learning/ml_onnx.py`, `workers/cluster.py` |
| `ML_ONNX_IR_VERSION` | `10` | `machine learning/ml_onnx.py` |
| `ML_ONNX_OPSET` | `17` | `machine learning/ml_onnx.py` |
| `VERA_INFERENCE_DEPLOYMENT_DB` | `<state>/models/deployments.sqlite3` | `models/model_inventory_capabilities.py` |
| `VERA_MODEL_BUILDER_URL` | `""` | `catalog/specialist_capabilities.py` |
| `VERA_MODEL_PACKAGE_DB` | `<state>/models/packages.sqlite3` | `models/model_inventory_capabilities.py` |
| `VERA_NODE_NAME` | `hostname` | `catalog/catalog_capabilities.py` |
| `VERA_PULL_AUTORESUME` | `1` | `catalog/catalog_capabilities.py` |

### Worldview model

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `WORLDVIEW_BATCH_SIZE` | `128` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_BLOB_KEY` | `worldview_v2` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_CHECKPOINT_DIR` | `~/.vera/worldview` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_DYN_CTX` | `32` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_DYN_DIM` | `192` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_DYN_HEADS` | `4` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_DYN_LAYERS` | `3` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_EMBED_DIM` | `768` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_GNN_LAYERS` | `2` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_HIDDEN_DIM` | `512` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_LATENT_DIM` | `256` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_LR` | `3e-4` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_MAX_NODES` | `20000` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_MAX_WALKS` | `20000` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_NUM_CONCEPTS` | `512` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_STREAM_AUTOSTART` | `1` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_VQ_COMMIT` | `0.25` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_VQ_DECAY` | `0.99` | `worldview/worldview_jepa.py` |
| `WORLDVIEW_WALK_LEN` | `16` | `worldview/worldview_jepa.py` |

### Device mesh, build service and printers

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `BUILDER_DEFAULT_FQBN` | `esp32:esp32:esp32` | `build/build_capabilities.py`, `build/builder_service.py` |
| `BUILDER_PORT` | `8080 (service) / 8785 (published)` | `build/build_capabilities.py`, `build/builder_service.py` |
| `VERA_BUILDER_URL` | `(unset)` | `build/build_capabilities.py`, `mesh/mesh_capabilities.py` |
| `VERA_MESH_HEARTBEAT` | `30` | `mesh/mesh_capabilities.py` |
| `VERA_MESH_SERIAL_BAUD` | `115200` | `mesh/mesh_capabilities.py` |
| `VERA_MESH_SERIAL_PORTS` | `""` | `mesh/mesh_capabilities.py` |
| `VERA_MESH_TOKEN` | `""` | `mesh/mesh_capabilities.py` |
| `VERA_MQTT_URL` | `""` | `mesh/mesh_capabilities.py` |
| `VERA_PRINTER_CHUNK` | `512` | `business/thermal_printer_capabilities.py` |
| `VERA_PRINTER_PACE` | `0.012` | `business/thermal_printer_capabilities.py` |

### Other subsystems

| Variable | Default | Read in (`vera/…`) |
|---|---|---|
| `PIXELLAB_API_BASE` | `https://api.pixellab.ai/v1` | `spritegen/providers.py` |
| `PIXELLAB_API_KEY` | `""` | `spritegen/providers.py` |
| `PIXELLAB_TIMEOUT` | `300` | `spritegen/providers.py` |
| `PIXELLAB_TOKEN` | `""` | `spritegen/providers.py` |
| `SPRITEGEN_MAX_PROMPT_WORDS` | `0` | `spritegen/prompts.py` |
| `VERA_BASE` | `http://localhost:8999` | `character/desktop/vera_companion.py` |
| `VERA_BOARD_SYNC_ENABLED` | `1` | `board/board_capabilities.py` |
| `VERA_BOARD_SYNC_INTERVAL_S` | `300` | `board/board_capabilities.py` |
| `VERA_COMPANION_AGENT` | `assistant` | `character/desktop/vera_companion.py` |
| `VERA_FLICKR_KEY` | `(unset)` | `godseye/godseye_imagery_core.py` |
| `VERA_GODSEYE_DIR` | `(unset)` | `godseye/godseye_core.py` |
| `VERA_GODSEYE_FORK` | `(unset)` | `godseye/godseye_core.py` |
| `VERA_GODSEYE_IMAGE` | `(unset)` | `godseye/godseye_capabilities.py` |
| `VERA_GODSEYE_REPO_URL` | `(unset)` | `godseye/godseye_capabilities.py` |
| `VERA_GODSEYE_TILE_URL` | `""` | `godseye/godseye_capabilities.py` |
| `VERA_MAPILLARY_TOKEN` | `(unset)` | `godseye/godseye_imagery_core.py` |
| `VERA_PXSTORE_TAB` | `(unset → no tab)` | `proxmox/pxstore_capabilities.py` |
---

## See also

- [Capability Framework](./01-capability-framework.md) — `VERA_MODULES`, the module load list, dispatch and activity recording
- [Ollama Cluster](./04-ollama-cluster.md) — Ollama, gate and routing configuration in context
- [Execution](./12-execution.md) & [Docker](./13-docker.md) — exec sandbox and Docker variables
- [Device Mesh](./14-mesh.md) · [vLLM Backend](./21-vllm.md) · [OpenClaw](./27-openclaw.md) — subsystem-specific configuration
- [Security & Secrets](./29-security.md) — `VERA_SECRET_KEY`, OpenBao and key management
- [Research System](./07-research.md) — researcher_api and NLP settings
- [Loop Lab](./33-evolve.md) — dev sandboxes and the `VERA_SANDBOX_*` / `VERA_DEV_*` family
- [Performance and sizing](./00-performance-and-sizing.md) — diagnostics and perf-gate variables
- [PWA](./47-pwa.md) — why TLS trust decides installability

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
