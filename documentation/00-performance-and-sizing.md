# Performance and sizing

Vera's performance depends on the workload: model size, quantization, context
length, concurrency, data volume, enabled backends, and how much background
automation is running. This guide separates three things that are often mixed
together:

1. **minimum resources needed to explore Vera;**
2. **capacity planning for useful local-model deployments; and**
3. **runtime health thresholds Vera actually enforces.**

No hardware table can guarantee tokens per second or end-to-end agent latency.
Benchmark the models and workflows you intend to use.

## Deployment profiles

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
  interactive GPU.
- Use SSD storage and monitor growth of model files, vectors, objects, logs, and
  Loop Lab worktrees.

Three model workers are useful for failover and workload separation, but are not
a requirement for the core runtime.

## Size models from first principles

The model worker—not Vera's web process—usually dominates memory.

Approximate weight memory:

```text
weight_bytes ≈ parameter_count × bits_per_weight ÷ 8
```

Add headroom for runtime overhead, KV cache, context length, batching, and
concurrent requests. A nominal “12B Q4” calculation is therefore not a promise
that the model fits comfortably in 6 GB. Use the catalog's hardware-fit tools,
then test the exact model build and context settings.

For GPU workers, avoid planning to 100% of VRAM. For CPU workers, avoid swap:
once model pages and active context push the host into sustained swapping,
latency becomes unpredictable.

## Storage planning

The old 20 GB figure only covers a small runtime checkout and light use. Budget
separately for:

- container images and build cache;
- local model weights;
- PostgreSQL, Chroma/FAISS, and Neo4j data;
- object-store payloads;
- generated media and reports;
- logs and activity history; and
- isolated Loop Lab worktrees.

Alert before disks become full. Database compaction, Git operations, model
downloads, and container builds all need temporary free space.

## Runtime performance contract

Vera's built-in monitor evaluates current health with `perf.scan`.

| Signal | Default interpretation |
|---|---|
| Event-loop stalls | None in the last 15 minutes is healthy |
| Worst recent stall | 3,000 ms or more is critical; a shorter stall is a warning |
| Redis consumers | Up to 200 consumers is treated as healthy |
| Zombie Ollama jobs | A running entry older than 30 minutes is warned |
| Gate critical threshold | Any critical finding produces a fail verdict |
| Gate warning threshold | More than 4 warnings produces a warn verdict |
| Promotion behavior | Advisory by default; fail blocks only with `VERA_PERF_GATE_STRICT=1` |

These are operational guardrails, not application SLOs. They detect a sick
runtime; they do not define acceptable model response time.

## Recommended service objectives

Define SLOs per workload and measure them at the caller:

| Workload | Measure |
|---|---|
| Capability API | success rate and p50/p95/p99 end-to-end latency |
| Interactive generation | time to first token, tokens/second, cancellation time |
| Agentic loop | total cycle time, per-capability time, retries, verification time |
| Data ingestion | records/second, queue delay, indexing completion |
| Semantic query | p50/p95 latency at representative collection size |
| Worker dispatch | queue wait, execution time, lost/retried tasks |
| UI | navigation readiness, panel load time, WebSocket reconnects |

Record the model, quantization, context, prompt size, concurrency, cache state,
dataset size, and node used with every benchmark. Otherwise results are not
comparable.

## Baseline procedure

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

curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"ollama.instances","arguments":{}}'

curl -s http://localhost:8999/mcp/call \
  -H 'content-type: application/json' \
  -d '{"name":"ollama.gate.status","arguments":{}}'
```

## Tuning order

1. Eliminate event-loop blocking and backend errors.
2. Ensure the selected model fits without swapping or VRAM thrashing.
3. Reduce context length and concurrency if KV cache dominates.
4. Separate interactive and background routes.
5. Add workers for throughput or failover.
6. Tune databases only after measuring the actual bottleneck.

Do not hide instability by merely raising timeouts. Use the Performance Monitor,
stall stacks, job history, and route statistics to locate the slow stage.

![Vera Performance Monitor](assets/overview/perf-monitor.png)

## Current-runtime snapshots

Live screenshots and health readings are evidence of one deployment at one
moment; they are not requirements. Documentation should state the capture date
and never present current node counts, CPU percentage, or ping latency as a
guarantee for another installation.

## Related guides

- [Ollama cluster](04-ollama-cluster.md)
- [vLLM](21-vllm.md)
- [Workers, jobs, and syslog](22-workers-jobs-syslog.md)
- [Loop Lab](33-evolve.md)
- [Configuration](10-configuration.md)
