# 04 · LLM Cluster

Vera orchestrates a backend-agnostic cluster of LLM instances — Ollama nodes, VLLM servers, hosted LLM APIs — with health-checking, load balancing, and automatic failover. The reference deployment is a small home network: one GPU node and two CPU nodes running Ollama. The cluster layer abstracts over the backend type, so adding a VLLM server or routing to an external API is a registration concern, not a code change.

The `cluster.py` module provides three integrated systems:

1. **Cluster monitoring** — polls every node for loaded models, VRAM, and version, publishes a snapshot to Redis.
2. **Load-aware routing** — picks the best instance for each request, factoring in latency, GPU availability, queue depth, and co-located worker load.
3. **Transparent proxy** — optionally intercepts the local `:11434` port, queues requests over a concurrency limit, and forwards to the configured target.

If an active node goes down mid-request, the call transparently retries on an available node.

---

### Portable inference compatibility

`models/ollama_inference_adapter.py` can expose the existing generation seam as
an `InferenceProvider` without changing current callers. Each adapter is bound
to one ModelPackage, model selector, and Ollama instance. Portable requests
therefore cannot silently change their model or route, while the established
Ollama layer remains the sole owner of its queue, resource gate, telemetry, and
retry behavior.

The adapter maps prompt plus optional system text, bounded sampling parameters,
streamed token callbacks, output-token usage, truncation, and cancellation into
the shared inference event contract. A truncated response is an explicit failed
terminal outcome rather than a successful partial answer. The legacy runner is
injected, so importing or testing the adapter performs no network or model call.
Existing `ollama.*` capabilities are unchanged; migration requires separate
parity evidence before any traffic is redirected.

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
from Vera.vera.capability_orchestration import add_ollama_instance

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
- **Warm residency** — a node that already has the requested model loaded (in `running`, from `/api/ps`) scores 0.3 better: no cold load (a 9B on CPU is ~10 s cold, a 35B MoE ~60 s). It is smaller than a rule's `prefer` bonus (0.5), so an explicit preference still wins while its node is idle.
- **Slots** — a GPU node serves one generation at a time, a CPU node two (`OLLAMA_NUM_PARALLEL=2`). A node with every slot taken gets a full call's penalty, so it loses to any node with a slot free.
- **Oversized prompts** — a prompt that cannot fit the GPU's safe window (measured chars/token, plus a minimum output reserve) goes to a CPU node that has the model; with warm residency that is the node keeping it loaded. The caller's `num_predict` does not count: on the GPU the window is capped and the output shrinks to fit.
- **Warm spill (opt-in)** — when every GPU slot is taken, a GPU-preferring call may take a CPU node that has its model loaded and a slot free, if the rule sets `spill: true` or an active workload scenario covers the job type, the node has proved at least `spill_min_tps` for that model, and the prompt is under `spill_max_ctx`. Off by default: a 9B does ~4 tok/s on CPU against 15–30 on the GPU, so waiting is often faster.
- **Instance pin** — explicit `instance_id` always wins, overriding strategy.
- **Co-located worker load** — if Vera workers are co-located on the same host as an Ollama node, their running tasks count against that node's score.
- **Proxy queue depth** — when the proxy is enabled, the node hosting the proxy gets a penalty proportional to its queue depth.
- **Errors** — consecutive errors compound a penalty.

The routing picks the lowest-cost instance from the online set. If all instances are offline, the call fails with a clear error rather than silently retrying forever.

### Warm model slots

Each node keeps a planned set of models loaded (`ollama.warm.status|set|apply`, `vera/workers/warm_models_*`). A GPU node has one slot, a CPU node two; the embedding model rides beside the slots on CPU nodes. The plan per node is, in order: an explicit list for the node, else the models the routing rules point at it (a `pin`, then a `prefer`), topped up from the class default (`@default` – the configured default model – and, on CPU nodes, the long-horizon model; the GPU node's CPU sibling holds `@default` and the naming model). Models that do not fit the node's memory are dropped and reported.

A minute job loads what is missing with `keep_alive: -1` at the planned window (one load per node per pass, never on a node with a call in flight or used in the last 2 min – 10 min for the GPU), and releases a model it pinned that the plan dropped. Calls Vera routes to a planned model on that node carry `keep_alive: -1` and the planned window, so they stay on the runner the warmer spawned instead of reloading it. Nothing re-arms a resident model on a timer: a load-only call to a resident 9B on CPU takes tens of seconds and holds up the node's embeddings.

**Workload scenarios** take the slots over while a job type runs hot, e.g. `coding`: when `code`/`loop_coder` demand reaches `min_requests` in `window_s` (or `min_inflight` live), coder models go into every slot (`fill: "all"`) per node class, and stay for `hold_s` after the demand falls away. A scenario can also turn warm spill on for its job types. Scenarios stay off while a census goal is in flight. Configure them in Estate › **Models & NLP**, which also holds the NLP placement and switches and the specialist model catalog.

### Node settings

Estate › Models & NLP › **Node settings** (`nodes.ollama.settings`, read-only, one SSH read per node) shows what is tuned on each Ollama: Vera's registry values, the unit's `OLLAMA_*` / `LLAMA_ARG_*` / GPU environment, every systemd drop-in and its contents, and the runners loaded right now with the `-t` they were started with. `nodes.ollama.settings.set` changes `num_thread` (the registry value sent with every routed call, and the unit's `LLAMA_ARG_THREADS`, so a caller that sends none - a sandbox, an external client - still gets it instead of llama.cpp's 24 threads on 12 CPUs) and custom `OLLAMA_*` / `LLAMA_ARG_*` flags in a Vera-owned drop-in. Applying restarts the unit and rolls back if Ollama stops answering; dry run by default. `nodes.ollama.tune` also writes the thread default on every CPU Ollama unit.

### Failover

Calls that fail (timeout, connection error, model not loaded) are retried automatically on a different instance. The retry chain is: prefer the next-best GPU-or-CPU node by score, exhausting all online nodes before giving up. By default, every `llm.*` cap supports retry; the retry count is per-cap (see `@capability(retries=...)`).

The transparent failover means a request to `llm.generate` against the GPU node that goes down mid-stream will silently complete on a CPU node — the caller never sees the failure unless every node fails.

### Routing layers and the Model Routing page

All routing control lives in the top-level **Model Routing** tab (`/ui/panels/model-routing`). The layers, from baseline to most specific — a more specific layer always wins:

1. **Default policy** — *least-busy, GPU-first*: every untyped request load-balances across online nodes, preferring GPU nodes (`DEFAULT_ROUTING_RULES["default"]`).
2. **Job-type rules** — route by *kind* of work (`chat`, `code`, `embedding`, `research_writer`, …), organised into named, activatable profiles (`ollama.routing.*`). e.g. embeddings/naming are `deny_gpu` so they never tie up a GPU. A rule key may end in `*` (`idle_*` covers every background job type); an exact key wins over a pattern. Light work – embeddings, naming, the chat's one-line acknowledgement (`quick_opener`) – prefers the GPU node's CPU-only sibling (`gpu-250-cpu`); background `idle_*` work prefers it too and overflows to an idle GPU.
3. **Per-capability rules** — route by *who* is asking, keyed on cap name or `prefix.*` glob (`ollama.cap_routing.*`). Two sub-layers: rules **declared** in code by subsystems (`register_cap_routing`) and **user** rules edited in the UI, which win.
4. **Role profiles** — subsystems that run several LLM personas register a profile of named roles and resolve every call through it (`register_routing_profile` / `resolve_role`, caps `ollama.role_profiles.*`, preview via `llm.route.resolve`). The **research** system registers `thinker`/`writer`/`verifier`, the **IDE** registers the same trio (its analyser is the verifier role). Per-role user overrides from the Model Routing page win over the declared defaults. Roles support length escalation (`escalate_chars` + overrides, e.g. "verifier jumps to GPU above 12k chars").

`ollama_generate(profile=…, role=…)` resolves a role inline; out-of-process or pre-flight callers use `GET /llm/route/resolve`. The research system's `get_instance(tier)` resolves its role through Vera on every call (falling back to its static instance list only in standalone mode), so research traffic obeys cluster routing and shows up in the router's load accounting.

**Media routing (STT / TTS / image-gen)** — the GPU inference servers (`edge/GPU_inference.py`, port 8765) are routable nodes too (`MEDIA_INSTANCES`). Each is health-probed for which services it actually serves (`/health` → whisper/tts/stable_diffusion), and every STT/TTS/image call resolves through `resolve_media(service)` / `media_base(service)` / `media_slot(service)` — GPU-first, least-busy, honouring the `stt` / `tts` / `imagegen` job-type rules (pin, deny GPU, allow/deny). A candidate node is pre-seeded on every cluster host, so installing the inference server on a CPU node makes it routable automatically. Manage nodes via `media.nodes` / `media.node.add|remove|config` / `media.ping`. Stateful flows stay sticky: duplex voice sessions and image progress polls remember the node they started on.

**vLLM** — vLLM servers appear in the Model Routing page alongside Ollama nodes. Any job-type rule, per-cap rule, or role-profile role pinned to `vllm:<id>` (or `vllm:*` for the best one) makes `ollama_generate` delegate that traffic to `vllm_generate`, falling back to normal Ollama routing when no vLLM node is online.

### Structured output is a provider contract

vLLM's `guided_json` argument is one possible execution mechanism, not Vera's
canonical structured-output API. The provider-neutral LIB-04 foundation lives in
`vera/providers/structured_generation.py` and gives provider-native schemas,
Instructor, and Outlines one bounded schema, validation, streaming, latency, and
retry-ownership vocabulary. Its current deterministic lane performs schema/value
inspection only; no model or provider is invoked. Runtime schema/stream parity
and measured latency remain queued for explicit live testing.

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
- [vLLM Backend](./21-vllm.md) — the other backend type the agnostic router can route to
- [Docker](./13-docker.md) — `docker.worker.*` spawn containers that join this cluster
- [Workers, Jobs & Syslog](./22-workers-jobs-syslog.md) — worker registry, job persistence, the proxy log
- [Configuration](./10-configuration.md) — all env vars in one place
- [Research System](./07-research.md) — resolves its thinker/writer/verifier roles through the `research` role profile

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
