# 04 · LLM Cluster

Vera orchestrates a heterogeneous cluster of LLM instances — Ollama nodes, VLLM servers, hosted LLM APIs — with health-checking, load balancing, and automatic failover. The reference deployment is a small home network: one GPU node and two CPU nodes running Ollama. The cluster layer abstracts over the backend type, so adding a VLLM server or routing to an external API is a registration concern, not a code change.

The `cluster.py` module provides three integrated systems:

1. **Cluster monitoring** — polls every node for loaded models, VRAM, and version, publishes a snapshot to Redis.
2. **Load-aware routing** — picks the best instance for each request, factoring in latency, GPU availability, queue depth, and co-located worker load.
3. **Transparent proxy** — optionally intercepts the local `:11434` port, queues requests over a concurrency limit, and forwards to the configured target.

If an active node goes down mid-request, the call transparently retries on an available node.

---

## 1. Default cluster

The default `OLLAMA_INSTANCES` dict in `capability_orchestration.py`:

```python
OLLAMA_INSTANCES = {
    "gpu-250": {"url": "http://192.168.0.250:11435", "label": "GPU Node",   "has_gpu": True,  "priority": 0},
    "cpu-246": {"url": "http://192.168.0.246:11435", "label": "CPU Node A", "has_gpu": False, "priority": 1},
    "cpu-247": {"url": "http://192.168.0.247:11435", "label": "CPU Node B", "has_gpu": False, "priority": 2},
}
```

Override individual URLs via env vars:

| Variable | Default |
|---|---|
| `OLLAMA_GPU_URL` | `http://192.168.0.250:11435` |
| `OLLAMA_CPU_A_URL` | `http://192.168.0.246:11435` |
| `OLLAMA_CPU_B_URL` | `http://192.168.0.247:11435` |
| `OLLAMA_MODEL` | `mistral` (default model when none is specified) |
| `OLLAMA_EMBED_URL` | `http://192.168.0.246:11435` |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` |

Add a new instance at runtime:

```python
from Vera.Orchestration.capability_orchestration import add_ollama_instance

add_ollama_instance("gpu-300", "http://192.168.0.300:11435", has_gpu=True, label="GPU Node B")
```

---

## 2. Health monitoring

Every node is pinged on a loop. Each ping records:

| Field | Source | Meaning |
|---|---|---|
| `status` | `/api/tags` reachable | `online` / `offline` / `unknown` |
| `latency_ms` | round-trip time | most recent ping latency |
| `models` | `/api/tags` response | full list of installed models |
| `running` | `/api/ps` response | currently loaded models with VRAM |
| `vram_used_gb` | sum of running models | live VRAM usage |
| `model_count` | len(models) | how many models are installed |
| `version` | `/api/version` | Ollama version string |
| `in_use` | maintained by routing | concurrent active requests |
| `errors` | counter | consecutive failed pings |
| `last_check` | ISO timestamp | last successful poll |

The cluster monitor in `cluster.py` extends this with `_fetch_instance_detail`, which adds the richer fields (`running`, `vram_used_gb`, `version`). A snapshot of all nodes is written to Redis at `vera:cluster:ollama` every `CLUSTER_POLL_INTERVAL` seconds (default 10) and broadcast as a `cluster.ollama_snapshot` event.

---

## 3. Load-aware routing

`cluster.py` patches `pick_instance()` (the default LLM router) to factor in:

- **Latency** — sub-100ms is healthy; over 500ms is a penalty.
- **GPU preference** — if the cap or the prompt prefers GPU, GPU nodes are scored higher.
- **Model affinity** — if a node has the requested model already loaded (in `running`), it's preferred (no cold-start cost).
- **Instance pin** — explicit `instance_id` always wins, overriding strategy.
- **Co-located worker load** — if Vera workers are co-located on the same host as an Ollama node, their running tasks count against that node's score.
- **Proxy queue depth** — when the proxy is enabled, the node hosting the proxy gets a penalty proportional to its queue depth.
- **Errors** — consecutive errors compound a penalty.

The routing picks the lowest-cost instance from the online set. If all instances are offline, the call fails with a clear error rather than silently retrying forever.

### Failover

Calls that fail (timeout, connection error, model not loaded) are retried automatically on a different instance. The retry chain is: prefer the next-best GPU-or-CPU node by score, exhausting all online nodes before giving up. By default, every `llm.*` cap supports retry; the retry count is per-cap (see `@capability(retries=...)`).

The transparent failover means a request to `llm.generate` against the GPU node that goes down mid-stream will silently complete on a CPU node — the caller never sees the failure unless every node fails.

---

## 4. Transparent proxy

If `LOCAL_OLLAMA_INSTANCE` env var is set to one of the known instance IDs (e.g. `gpu-250`), `cluster.py` mounts proxy routes at `/ollama/*` on Vera's own port:

```
POST http://vera-host:8999/ollama/api/generate
     ↓ proxied with concurrency control + observability
POST http://192.168.0.250:11435/api/generate
```

External clients can then point at Vera (`:8999`) instead of Ollama (`:11434`), getting:

- **Concurrency limiting** via `PROXY_MAX_CONCURRENCY` (default 3). Requests over the limit are queued.
- **Queueing** — up to 50 in-flight queued requests, with a `PROXY_QUEUE_TIMEOUT` (default 120s).
- **Memory event emission** — every prompt and completion is recorded.
- **Streaming preservation** — the proxy is fully streaming-aware; chunks are forwarded as they arrive.

This is how a node can "share" its GPU/CPU across multiple consumers without each one having to know about the cluster.

---

## 5. Inspecting cluster state

### `GET /health`

A small overall health summary — backends, worker count, cap count, MCP server count, per-Ollama-node status:

```json
{
  "redis": true,
  "postgres": true,
  "chroma": false,
  "neo4j": true,
  "workers": 3,
  "caps": 412,
  "ollama": {
    "gpu-250": {"status": "online", "latency_ms": 18, "has_gpu": true},
    "cpu-246": {"status": "online", "latency_ms": 42, "has_gpu": false},
    "cpu-247": {"status": "offline", "latency_ms": null, "has_gpu": false}
  },
  "mode": "distributed"
}
```

### `GET /cluster` (`obs.cluster`)

Full cluster view — workers cross-referenced with their Ollama nodes:

```json
{
  "workers": {...},
  "ollama": {
    "gpu-250": {
      "id": "gpu-250", "label": "GPU Node",
      "status": "online", "has_gpu": true,
      "latency_ms": 18, "in_use": 1,
      "models": ["mistral", "nomic-embed-text", "qwen2.5-coder:32b"],
      "running": [{"name": "qwen2.5-coder:32b", "size_vram": 21000000000}],
      "vram_used_gb": 21,
      "errors": 0, "version": "0.4.7"
    },
    ...
  },
  "queue": {"task_queue_len": 0, "result_queue_len": 0, "pending_tasks": 0},
  "proxy": {
    "active": 0, "local_instance": "gpu-250",
    "queue_depth": 0, "max_concurrency": 3, "enabled": true
  }
}
```

### `cluster.instance_update`

Mutate runtime fields on an instance (model context window, label):

```bash
curl -X POST http://localhost:8999/cluster/instance/update \
  -d '{"id":"gpu-250","num_ctx":32768}'
```

---

## 6. The Ollama panel

The harness's Ollama tab (rendered by `workers_ollama_panel.html`) shows:

- Per-node cards with status dot, latency, GPU/CPU badge, pin indicator
- Running model chips with VRAM bar
- Available models (capped at 12 per node)
- Routing strategy controls — prefer GPU toggle, pinned instance
- Live test runner — pick a model, an instance, send a prompt

Routing config and pinning are persisted to Redis so they survive restarts.

---

## 7. Embeddings

The embedding model has its own URL and model name (`OLLAMA_EMBED_URL`, `OLLAMA_EMBED_MODEL`). By default it points at one of the CPU nodes — embeddings are cheap and shouldn't compete for GPU VRAM. The data fabric and memory system use this dedicated endpoint via the `llm.embed` cap.

---

## 8. GPU inference server

A separate process (the GPU node's `:8765` `gpu_infer` server) handles Whisper STT, TTS, and Stable Diffusion. The `GPU_INFER_URL` config points at it. Capabilities like `gpu.stt`, `gpu.tts`, and `gpu.sd_generate` route through it without going via Ollama.

---

## See also

- [Capability Framework](./01-capability-framework.md) — `llm.*` and `gpu.*` caps that consume the cluster
- [Configuration](./10-configuration.md) — all env vars in one place
- [Research System](./07-research.md) — uses tier-based routing (THINKER→GPU, WRITER+ANALYST→CPU)