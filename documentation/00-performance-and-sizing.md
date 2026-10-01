# Performance and sizing

Vera's performance depends on the workload: model size, quantization, context
length, concurrency, data volume, enabled backends, and how much background
automation is running. This guide separates three things that are often mixed
together:

1. **minimum resources needed to explore Vera;**
2. **capacity planning for useful local-model deployments; and**
3. **runtime health thresholds Vera actually enforces.**

It also documents the runtime diagnostics Vera ships for finding out *why* it
is slow: the event-loop watchdog and stall stack dumper in
`vera/capability_orchestration.py`, the paced garbage collector, the
Performance Monitor (`vera/monitor/perf_capabilities.py`,
`vera/monitor/perf_gate_core.py`, `vera/monitor/stall_trace_core.py`) and the
code-author timing summary (`vera/performance/timing_capabilities.py`). These
are stable, production paths; the sizing figures are planning guidance, not
guarantees. No hardware table can guarantee tokens per second or end-to-end
agent latency — benchmark the models and workflows you intend to use.

## Contents

- [1. Deployment profiles](#1-deployment-profiles)
  - [Runtime exploration](#runtime-exploration)
  - [Single-host local AI](#single-host-local-ai)
  - [Distributed working deployment](#distributed-working-deployment)
- [2. Size models from first principles](#2-size-models-from-first-principles)
- [3. Storage planning](#3-storage-planning)
- [4. Runtime performance contract](#4-runtime-performance-contract)
  - [What `perf.scan` checks](#what-perfscan-checks)
  - [The perf gate](#the-perf-gate)
  - [Remediations](#remediations)
- [5. Runtime diagnostics](#5-runtime-diagnostics)
  - [Event-loop watchdog and stall stacks](#event-loop-watchdog-and-stall-stacks)
  - [Paced garbage collection](#paced-garbage-collection)
  - [Log capture](#log-capture)
  - [Performance capabilities](#performance-capabilities)
  - [The Performance Monitor panel](#the-performance-monitor-panel)
- [6. Recommended service objectives](#6-recommended-service-objectives)
- [7. Baseline procedure](#7-baseline-procedure)
- [8. Tuning order](#8-tuning-order)
- [9. Configuration reference](#9-configuration-reference)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Operator and documentation capture objectives](#11-operator-and-documentation-capture-objectives)
- [12. Current-runtime snapshots](#12-current-runtime-snapshots)
- [Related guides](#related-guides)

## 1. Deployment profiles

### Runtime exploration

Use hosted model providers or a separate Ollama service.

| Resource | Starting point |
|---|---|
| CPU | 4 modern cores |
| RAM | 8 GB minimum; 16 GB preferred |
| Disk | 20 GB free, plus retained data and logs |
| GPU | Not required |

This profile is appropriate for capability development, API/UI exploration,
small datasets, and low-concurrency workflows. Running Postgres, Chroma, Neo4j,
Redis, and Vera together leaves less memory for models.

### Single-host local AI

| Resource | Starting point |
|---|---|
| CPU | 8 or more modern cores |
| RAM | 32 GB |
| Disk | 100 GB free on SSD |
| GPU | Optional; 12–16 GB VRAM is useful for small/medium quantized models |

This is a practical development workstation, not a high-concurrency production
target. Keep enough host RAM free for the orchestrator and databases after model
weights and KV cache are loaded.

### Distributed working deployment

- Place the orchestrator and persistent stores on a stable host.
- Use separate Ollama or vLLM workers for model inference.
- Plan 32–64 GB RAM for CPU model workers, depending on model and context.
- Prefer at least one worker with 16 GB or more VRAM for interactive requests.
- Keep background work routable to CPU workers so it cannot monopolize the
  interactive GPU (the interactive-priority setting, `ollama.interactive.set`,
  demotes background LLM work while a person is active — see
  [Ollama cluster](04-ollama-cluster.md)).
- Use SSD storage and monitor growth of model files, vectors, objects, logs, and
  Loop Lab worktrees.

Three model workers are useful for failover and workload separation, but are not
a requirement for the core runtime.

## 2. Size models from first principles

The model worker—not Vera's web process—usually dominates memory.

Approximate weight memory:

```text
weight_bytes ≈ parameter_count × bits_per_weight ÷ 8
```

Add headroom for runtime overhead, KV cache, context length, batching, and
concurrent requests. A nominal “12B Q4” calculation is therefore not a promise
that the model fits comfortably in 6 GB. Use the catalog's hardware-fit tools,
then test the exact model build and context settings.

For GPU workers, avoid planning to 100% of VRAM. Vera's own context sizing
assumes only `OLLAMA_VRAM_USABLE_FRAC` (default `0.78`) of a GPU's memory is
usable, and `OLLAMA_DEFAULT_GPU_VRAM_GB` (default `12.0`) when a node does not
report its VRAM. For CPU workers, avoid swap: once model pages and active
context push the host into sustained swapping, latency becomes unpredictable.

Context length is the other lever. Vera automatically fits the context window to
the prompt (`VERA_AUTO_CTX_FIT=1`), caps auto-detected windows at
`OLLAMA_MAX_AUTO_CTX` (default 65536) and bounds generated output
(`VERA_OUTPUT_MAX_TOKENS` 16384 overall, `VERA_OUTPUT_MAX_TOKENS_CPU` 3072 on CPU
nodes; `VERA_OUTPUT_MAX_TOKENS_GPU`, default `0`, falls back to the overall value). A generation whose prompt plus output overruns its window makes the
runner discard prompt tokens and keep going; `perf.scan` reports such routes
([§4](#what-perfscan-checks)).

## 3. Storage planning

The 20 GB exploration figure only covers a small runtime checkout and light
use. Budget separately for:

- container images and build cache;
- local model weights;
- PostgreSQL, Chroma/FAISS, and Neo4j data;
- object-store payloads (Garage S3 when `FABRIC_OBJECT_STORE` is enabled);
- the data fabric's SQLite databases and artifact store;
- generated media and reports;
- logs (by default up to six 10 MB files under `<repo>/logs`, see
  [Log capture](#log-capture)) and activity history; and
- isolated Loop Lab worktrees and dev-sandbox volumes.

Alert before disks become full. Database compaction, Git operations, model
downloads, and container builds all need temporary free space.

## 4. Runtime performance contract

Vera's built-in monitor evaluates current health with `perf.scan`.

| Signal | Default interpretation |
|---|---|
| Event-loop stalls | None in the last 15 minutes is healthy |
| Worst recent stall | 3,000 ms or more is critical; a shorter stall is a warning |
| Redis consumers | Up to 200 consumers in the `workers` group of `vera:tasks` is healthy; more is a warning |
| Zombie Ollama jobs | A `running` entry older than 30 minutes is warned |
| Ollama nodes | Any offline node is warned; every online node busy is reported as info |
| Context overruns | A route with ≥ 5 measured calls overrunning its window on ≥ 5% of them is warned (below that, info) |
| Host resources | CPU or memory at 90% or more is warned |
| Gate critical threshold | Any critical finding produces a fail verdict |
| Gate warning threshold | More than 4 warnings produces a warn verdict |
| Promotion behavior | Advisory by default; fail blocks only with `VERA_PERF_GATE_STRICT=1` |

These are operational guardrails, not application SLOs. They detect a sick
runtime; they do not define acceptable model response time.

### What `perf.scan` checks

`perf.scan` runs six checks, each isolated so one failing check cannot hide the
others, and returns `{findings, summary: {crit, warn, info, ok}, ts}` with
findings sorted critical → ok. Each finding carries `id`, `severity`, `area`,
`title`, `detail`, `remediation`, `remediable`, `remediation_id` and `metric`.

| Check | Source | Finding ids |
|---|---|---|
| Event-loop stalls | the orchestrator's `PERF_EVENTS` ring (stalls and hangs in the last 15 minutes) | `loop` |
| Stream consumers | `XINFO GROUPS vera:tasks` | `consumers` (remediable: `prune_consumers`) |
| Zombie jobs | `jobs.stats` and `jobs.ollama_log` | `zombies` (remediable: `sweep_zombies`) |
| Ollama nodes | `ollama.instances` (nodes with ≥ 3 errors, offline nodes, saturation) | `ollama_offline`, `ollama_saturated`, `ollama` |
| Context window | route statistics (`prompt_eval_count + eval_count > num_ctx`) | `ctx_shift` |
| Host resources | `sysmon.status` | `cpu`, `mem`, `resources` |

A context-overrun finding means a generation exceeded its budget and kept
running after discarding prompt tokens; it costs compute, but it is not proof
that the answer is wrong.

### The perf gate

`perf.gate` reduces a `perf.scan` summary to a promotion verdict with the pure
function `perf_verdict` in `perf_gate_core.py`:

| Verdict | Condition (defaults) |
|---|---|
| `fail` | `crit > VERA_PERF_GATE_MAX_CRIT` (0) |
| `warn` | `warn > VERA_PERF_GATE_MAX_WARN` (4) |
| `pass` | otherwise |

It returns `{ok, verdict, blocking, strict, summary, reason, top_findings}`
(the five most severe non-ok findings). `blocking` is true only for `fail`
when `VERA_PERF_GATE_STRICT=1`; performance is a property of the running system,
not of a branch, so by default the gate is advisory and surfaced on the Loop Lab
pipeline rather than stopping a merge on transient load.

### Remediations

`perf.remediate` applies only self-contained fixes:

| `remediation_id` | Action |
|---|---|
| `prune_consumers` | Prunes stale stream consumers (idle over 10 minutes with nothing pending) |
| `sweep_zombies` | Fail-marks Ollama jobs stuck in `running` |

Each application is also recorded as a `note` event in the stall feed.

## 5. Runtime diagnostics

### Event-loop watchdog and stall stacks

Vera is an asyncio service: while the event loop is blocked by synchronous work
(CPU-bound code, a large `json.dumps`, a blocking call in a capability),
uvicorn cannot service WebSocket frames or ping/pong and connections drop.

- The **loop-lag watchdog** sleeps 0.5 s in a loop and measures how much later
  than that it wakes. A lag of at least `VERA_LOOP_LAG_WARN_MS` (500 ms) logs
  `EVENT LOOP STALLED for <n>ms` and records a `stall` event.
- A separate **stack-dumper thread** watches a heartbeat the watchdog bumps.
  When a stall lasts at least `VERA_LOOP_HANG_DUMP_S` (1 s) it captures the main
  thread's stack *while the loop is still blocked* and records a `hang` event
  with the stack and a one-line `where`. `stall_trace_core.stall_where` picks
  the most actionable frame: the deepest frame inside the Vera package, else
  the deepest non-stdlib, non-site-packages frame; a stack with no application
  frame is reported as such (typically GIL or CPU starvation).
- When the dumper misses a long stall (for example a worker thread holding the
  GIL in a C call), the watchdog samples busy worker-thread stacks into the log
  instead.

Events are kept in `PERF_EVENTS`, a ring of `VERA_PERF_EVENTS_MAX` (300)
entries of kind `stall`, `hang`, `gc` or `note`.

### Paced garbage collection

At startup Vera freezes the objects created during startup out of gen-2 scans and raises CPython's GC thresholds (`VERA_GC_GEN0` 10000,
`VERA_GC_GEN1` 25, `VERA_GC_GEN2` 25) so automatic full collections — which
were measured freezing the loop for over a second — essentially never fire
mid-request. A pacer task instead runs a young-generation collection every
`VERA_GC_PACE_S` (120 s) and a full collection every `VERA_GC_FULL_EVERY` (15)
cycles. Any collection taking at least `VERA_GC_WARN_MS` (200 ms) is recorded
as a `gc` event, so GC pauses are named in the stall feed instead of being
mistaken for whatever code was running.

### Log capture

`perf_capabilities.py` tees the whole application log to a rotating file,
`<VERA_LOG_DIR>/vera.log` (default `<repo>/logs`, `VERA_LOG_MAX_BYTES` 10 MB ×
`VERA_LOG_BACKUPS` 5), through a queue handler and background listener so file
I/O never runs on the event loop. An in-memory ring of the last
`VERA_LOG_RING` (3000) lines serves instant tails.

### Performance capabilities

| Capability | Route | Inputs | Output |
|---|---|---|---|
| `perf.scan` | `GET /perf/scan` | — | `{findings[], summary{crit,warn,info,ok}, ts}` |
| `perf.gate` | `GET /perf/gate` | — | `{ok, verdict, blocking, strict, summary, reason, top_findings}` |
| `perf.remediate` | `POST /perf/remediate` | `remediation_id` (`prune_consumers` \| `sweep_zombies`) | `{ok, detail \| error}` |
| `perf.stalls` | `GET /perf/stalls` | `limit` (100) | `{events[], count, hangs, stalls, worst_ms}` newest first |
| `perf.log.tail` | `GET /perf/log/tail` | `lines` (200, max 5000), `grep`, `file` | `{lines[], count, source}` (`ring` or a file path) |
| `perf.log.files` | `GET /perf/log/files` | — | The current and rotated log files |
| `perf.note` | `POST /perf/note` | `message`, `level` (`info`) | Pushes a diagnostic line into the log and the stall feed |
| `code.author.timing.summary` | `GET /code/author/timing/summary` | `limit` (200, 1–500) | p50/p95/max per metric and phase for recent `code.author.timing` events, plus repair/smoke activity rates |
| `ollama.gate.status` | `GET /ollama/gate` | — | GPU gate leases and queue |
| `ollama.route_stats` | `GET /ollama/route_stats` | `model`, `instance`, … | Per-route throughput, context and overrun statistics |

`code.author.timing.summary` reads the dedicated
`vera:stream:code.author.timing` stream and falls back to the generic event
stream for older samples; see [Performance baseline](42-performance-baseline.md)
for the timing envelope it summarises.

### The Performance Monitor panel

![Vera Performance Monitor](assets/overview/perf-monitor.png)

The **Perf** panel (`perf-monitor`, `mode="inject"`, served from
`GET /perf/panel`) shows the stall feed with stacks, the log tail and the scan
findings with one-click remediations. It also appears under Estate → Observe →
Perf.

## 6. Recommended service objectives

Define SLOs per workload and measure them at the caller:

| Workload | Measure |
|---|---|
| Capability API | success rate and p50/p95/p99 end-to-end latency |
| Interactive generation | time to first token, tokens/second, cancellation time |
| Agentic loop | total cycle time, model queue/provider time, controller and verification time, per-capability time, retries |
| Data ingestion | records/second, queue delay, indexing completion |
| Semantic query | p50/p95 latency at representative collection size |
| Worker dispatch | queue wait, execution time, lost/retried tasks |
| UI | navigation readiness, panel load time, WebSocket reconnects |

Record the model, quantization, context, prompt size, concurrency, cache state,
dataset size, and node used with every benchmark. Otherwise results are not
comparable.

For local Ollama-backed loop calls, `queue_ms` measures time before the selected
node slot is acquired, `provider_ms` measures the generation inside that slot,
and `total_ms` covers both. Tokens per second and routing history use
`provider_ms`; use `total_ms` when assessing the user's wait. The agent-loop UI
shows the split for controller, quality-check, and completion-check calls.
Every capability call also records `elapsed_ms` on its `cap.ok`/`cap.error`
event and in `vera:cap:recent` ([Capability Framework §7](01-capability-framework.md#7-event-emission)).

## 7. Baseline procedure

1. Start Vera and wait for `/health` to report the required backends.
2. Run `perf.scan`; resolve critical findings before benchmarking.
3. Warm the exact model with one representative request.
4. Run at least 30 requests at expected concurrency.
5. Report p50, p95, p99, failures, retries, and saturation—not only the average.
6. Repeat with background loops enabled.
7. Save the environment and model configuration beside the results.

Useful capability calls:

```bash
curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"perf.scan","arguments":{}}'

curl -s http://localhost:8999/perf/gate

curl -s 'http://localhost:8999/perf/stalls?limit=20'

curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"ollama.instances","arguments":{}}'

curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"ollama.gate.status","arguments":{}}'
```

## 8. Tuning order

1. Eliminate event-loop blocking and backend errors.
2. Ensure the selected model fits without swapping or VRAM thrashing.
3. Reduce context length and concurrency if KV cache dominates.
4. Separate interactive and background routes.
5. Add workers for throughput or failover.
6. Tune databases only after measuring the actual bottleneck.

Do not hide instability by merely raising timeouts. Use the Performance Monitor,
stall stacks, job history, and route statistics to locate the slow stage.

## 9. Configuration reference

| Variable | Default | Effect |
|---|---|---|
| `VERA_LOOP_LAG_WARN_MS` | `500` | Event-loop lag that counts as a stall |
| `VERA_LOOP_HANG_DUMP_S` | `1` | Stall length that triggers a stack capture |
| `VERA_PERF_EVENTS_MAX` | `300` | Size of the stall/hang/gc/note ring |
| `VERA_GC_GEN0` / `VERA_GC_GEN1` / `VERA_GC_GEN2` | `10000` / `25` / `25` | CPython GC thresholds set at startup |
| `VERA_GC_PACE_S` | `120` | Seconds between paced collections |
| `VERA_GC_FULL_EVERY` | `15` | Paced cycles per full collection |
| `VERA_GC_WARN_MS` | `200` | Collection time recorded as a `gc` event |
| `VERA_LOG_DIR` | `<repo>/logs` | Captured log directory |
| `VERA_LOG_MAX_BYTES` / `VERA_LOG_BACKUPS` | `10485760` / `5` | Log rotation |
| `VERA_LOG_RING` | `3000` | In-memory tail size |
| `VERA_PERF_GATE_MAX_CRIT` / `VERA_PERF_GATE_MAX_WARN` | `0` / `4` | Gate thresholds |
| `VERA_PERF_GATE_STRICT` | empty | `1` makes a `fail` verdict block promotion |
| `VERA_WS_PING_INTERVAL` / `VERA_WS_PING_TIMEOUT` | `20` / `75` | WebSocket keep-alive tolerance for brief stalls |
| `OLLAMA_VRAM_USABLE_FRAC` | `0.78` | Usable share of GPU memory in context sizing |
| `VERA_OUTPUT_MAX_TOKENS` / `_CPU` / `_GPU` | `16384` / `3072` / `0` | Output token ceilings; the GPU value `0` falls back to `VERA_OUTPUT_MAX_TOKENS` |

The full list is in [Configuration §19](10-configuration.md#19-complete-environment-variable-reference).

## 10. Troubleshooting

| Symptom | Where to look |
|---|---|
| The UI's WebSocket keeps reconnecting | `perf.stalls` — a stall longer than the ping timeout drops sockets; the `hang` row names the blocking call |
| Stall rows point at `json` or GC frames | Look one frame above in the stack for the Vera caller; `gc` rows mean a collection, see [Paced garbage collection](#paced-garbage-collection) |
| `consumers` warning | Run `perf.remediate remediation_id=prune_consumers` |
| `zombies` warning | Run `perf.remediate remediation_id=sweep_zombies` |
| `ctx_shift` warning | Check the route's `ema_chars_per_token` in `ollama.route_stats`; raise `num_ctx` for that work or reduce the prompt |
| Generations queue although nodes are idle | `ollama.gate.status` for held leases; `perf.scan` Ollama findings |
| Perf panel shows no log lines | The log directory could not be created; check `VERA_LOG_DIR` permissions |

## 11. Operator and documentation capture objectives

Browser automation has different latency characteristics from API calls. A
capture is complete only when the relevant UI state is visible and stable.

| Measure | Target | Investigate when |
|---|---:|---:|
| Browser session start | under 5 s warm | over 10 s |
| Simple panel capture | under 8 s | over 15 s |
| Graph/chart capture | under 15 s | over 30 s |
| Failed-panel isolation | one panel only | a failure aborts or erases other output |
| Screenshot usefulness | selected feature visible | landing shell, spinner, or empty canvas |
| Selective-run preservation | 100% unrelated entries retained | unrelated output disappears |

These are operational objectives, not fixed sleeps. Faster hardware must not
capture before readiness evidence appears, while WebSocket traffic must not
make a ready page wait forever for network-idle. Run captures serially per
browser session; use selective runs while authoring and a full run for release.

## 12. Current-runtime snapshots

Live screenshots and health readings are evidence of one deployment at one
moment; they are not requirements. Documentation should state the capture date
and never present current node counts, CPU percentage, or ping latency as a
guarantee for another installation.

## Related guides

- [Capability framework](01-capability-framework.md)
- [Ollama cluster](04-ollama-cluster.md)
- [vLLM](21-vllm.md)
- [Workers, jobs, and syslog](22-workers-jobs-syslog.md)
- [Loop Lab](33-evolve.md)
- [Operator](34-operator.md)
- [Performance baseline](42-performance-baseline.md)
- [Configuration](10-configuration.md)
