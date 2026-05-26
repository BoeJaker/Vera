# 10 · Configuration

All Vera configuration is centralised in `config.py` as a single `VeraConfig` class. Every other module imports `cfg` from there rather than reading `os.getenv` directly — this guarantees one source of truth and one place to override values.

Every field can be overridden via environment variable. The defaults assume a home network with `llm.int` as the internal hostname and three Ollama nodes at `192.168.0.250`, `.246`, `.247`.

---

## 1. Usage

```python
from Vera.Orchestration.config import cfg

print(cfg.REDIS_URL)            # → "redis://localhost:6379"
print(cfg.OLLAMA_GPU_URL)       # → "http://192.168.0.250:11435"
```

To override, set the env var before starting the orchestrator:

```bash
export REDIS_URL=redis://prod-redis:6379
export POSTGRES_URL=postgresql://prod:secret@prod-pg/llm
export OLLAMA_GPU_URL=http://gpu-host:11435
python -m Vera.Orchestration.capability_orchestration
```

---

## 2. Network and hosts

| Variable | Default | Purpose |
|---|---|---|
| `BACKEND_HOST` | `llm.int` | Internal hostname where backend services live |
| `ORCHESTRATOR_HOST` | `0.0.0.0` | Bind address for the orchestrator |
| `ORCHESTRATOR_PORT` | `8999` | Port for the orchestrator |

`ORCHESTRATOR_HOST=0.0.0.0` is intentional — the orchestrator should be reachable from all hosts on the network for distributed dispatch to work.

---

## 3. Redis

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Connection URL |

Redis is used for:

- Task and result streams (`vera:tasks`, `vera:results`)
- Event stream (`vera:events`) and pub/sub channel (`vera:events:pub`)
- Worker registry (`vera:workers:<id>`)
- Cluster snapshot (`vera:cluster:ollama`)
- Query result cache for the data fabric
- Capability tracking (`cap_tracking.*`)
- Theme storage
- Streaming source coordination

The orchestrator and every worker share a single connection pool via `_orch.REDIS`. Other modules access this rather than opening their own connections.

If Redis is offline, the orchestrator switches to local mode — capabilities still work but distributed dispatch, cross-host worker observation, and event streaming are disabled.

---

## 4. PostgreSQL

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_URL` | `postgresql://admin:admin@localhost:5433/postgres` | Connection URL |

Postgres tables (created lazily on first use):

- `vera_task_results` — archived task results from distributed dispatch
- `vera_capabilities` — cap usage stats (`cap_tracking`)
- `vera_research_*` — research job/citation tables (when research is enabled)
- `vera_fabric_*` — primary fabric storage (when fabric is enabled)

The connection pool retries indefinitely in the background. If Postgres is offline, the fabric falls back to SQLite, and other Postgres-dependent caps degrade gracefully.

---

## 5. ChromaDB

| Variable | Default | Purpose |
|---|---|---|
| `CHROMA_HOST` | `localhost` | Chroma server host |
| `CHROMA_PORT` | `8008` | Chroma server port |

Used by the data fabric for vector storage with metadata filtering, and by the memory system for record embedding. Optional — falls back to FAISS-only if Chroma is offline.

---

## 6. Neo4j

| Variable | Default | Purpose |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Bolt URI |
| `NEO4J_USER` | `neo4j` | Username |
| `NEO4J_PASS` | `neo4j` | Password |

The primary store for the memory graph and the auxiliary fabric graph. The orchestrator's Neo4j driver is shared across modules (`memory.py`, `data_fabric.py`, `memory_hooks.py`, `fabric_web_acquisition.py`).

If Neo4j is offline, capabilities that write to the graph degrade silently — the cap call itself still succeeds, just without a graph node. Anything that reads from Neo4j returns an empty result.

---

## 7. Ollama cluster

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_GPU_URL` | `http://192.168.0.250:11435` | GPU node URL |
| `OLLAMA_CPU_A_URL` | `http://192.168.0.246:11435` | CPU node A URL |
| `OLLAMA_CPU_B_URL` | `http://192.168.0.247:11435` | CPU node B URL |
| `OLLAMA_MODEL` | `mistral` | Default model name |
| `OLLAMA_EMBED_URL` | `http://192.168.0.246:11435` | Embedding model URL |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model name |

The three nodes get instance IDs `gpu-250`, `cpu-246`, `cpu-247` regardless of the URLs you set — these IDs are the keys in `OLLAMA_INSTANCES` and are referenced everywhere (instance pinning, routing config, tier mapping).

To add more nodes at runtime:

```python
from Vera.Orchestration.capability_orchestration import add_ollama_instance
add_ollama_instance("gpu-300", "http://192.168.0.300:11435", has_gpu=True, label="GPU Node B")
```

---

## 8. GPU inference server

| Variable | Default | Purpose |
|---|---|---|
| `GPU_INFER_URL` | `http://192.168.0.250:8765` | Whisper STT, TTS, Stable Diffusion server |

Hosts non-LLM GPU workloads. Used by the `gpu.stt`, `gpu.tts`, and `gpu.sd_generate` capabilities. The chat panel uses STT/TTS when the microphone/speaker toggles are on.

---

## 9. IDE workspace

| Variable | Default | Purpose |
|---|---|---|
| `IDE_PROJECTS_ROOT` | (host-specific) | Root directory for IDE workspaces |

Each IDE workspace is a folder under this root. Created on first use if it doesn't exist.

---

## 10. Research and NLP

| Variable | Default | Purpose |
|---|---|---|
| `VERA_RESEARCHER_URL` | `http://localhost:8765` | researcher_api server URL |
| `VERA_NLP_URL` | `http://localhost:8766` | NLP addon server URL |
| `VERA_BASE_URL` | (auto-detect) | Vera's own base URL, used by `research_vera_bridge` when researcher_api needs to call back |

researcher_api runs as a separate process. If it's not running, the `research.*` capabilities return clear error messages and the research panel shows the server as offline.

---

## 11. Web search and acquisition

| Variable | Default | Purpose |
|---|---|---|
| `VERA_SEARXNG_URL` | `http://llm.int:8888` | SearXNG instance |
| `BRAVE_API_KEY` | (unset) | Brave Search API key (fallback engine) |
| `FABRIC_CRAWL_DELAY_S` | `2` | Rate-limit delay between web fetches |

Without `BRAVE_API_KEY` set, the web search chain is SearXNG → DuckDuckGo. Setting it adds Brave in the middle.

---

## 12. Distributed dispatch tuning

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_OLLAMA_INSTANCE` | (unset) | If set, mount the Ollama transparent proxy targeting this instance ID |
| `PROXY_MAX_CONCURRENCY` | `3` | Max concurrent proxy passes |
| `PROXY_QUEUE_TIMEOUT` | `120` | Max seconds to queue an over-limit request |
| `CLUSTER_POLL_INTERVAL` | `10` | Seconds between cluster polls |

`LOCAL_OLLAMA_INSTANCE=gpu-250` is the typical setting on the GPU host — it makes Vera mount `/ollama/*` proxy routes so external clients can target Vera (`:8999`) instead of Ollama (`:11434`), getting concurrency control and event emission for free.

---

## 13. Activity recording

| Variable | Default | Purpose |
|---|---|---|
| `VERA_ACTIVITY_RECORDING` | `0` | Enable the activity worker that writes cap calls to the memory graph |

Set to `"1"` to turn on activity recording. With it off (the default), the activity queue silently fills and discards — graph writes only happen for explicit cap calls (workspace open, file write, research job, etc.) that record directly.

---

## 14. Common deployment patterns

### Single-host development

```bash
# Everything on one machine; defaults work after starting Redis/Postgres/Neo4j locally
python -m Vera.Orchestration.capability_orchestration
```

### Distributed cluster

On every host:

```bash
export REDIS_URL=redis://llm.int:6379
export POSTGRES_URL=postgresql://admin:admin@llm.int:5433/postgres
export NEO4J_URI=bolt://llm.int:7687
python -m Vera.Orchestration.capability_orchestration
```

On the GPU host, additionally:

```bash
export LOCAL_OLLAMA_INSTANCE=gpu-250
```

### Production deployment

Override credentials:

```bash
export POSTGRES_URL=postgresql://prod_user:secret@prod-pg-host/vera
export NEO4J_PASS=<strong-password>
export BRAVE_API_KEY=<key>
export VERA_ACTIVITY_RECORDING=1
```

---

## 15. Adding new config fields

If a module needs a new tunable, add it to `VeraConfig` in `config.py`:

```python
class VeraConfig:
    ...
    MY_FEATURE_TIMEOUT: int = int(os.getenv("MY_FEATURE_TIMEOUT", "30"))
    MY_FEATURE_URL:     str = os.getenv("MY_FEATURE_URL", "http://default-host:1234")
```

Then in the module:

```python
from Vera.Orchestration.config import cfg
timeout = cfg.MY_FEATURE_TIMEOUT
```

Don't read `os.getenv` directly outside `config.py` — it makes overrides invisible and prevents the central documentation pattern from working.

---

## See also

- [Capability Framework](./01-capability-framework.md) — env var `VERA_ACTIVITY_RECORDING`
- [Ollama Cluster](./04-ollama-cluster.md) — Ollama-specific config in context
- [Research System](./07-research.md) — researcher_api/NLP URLs