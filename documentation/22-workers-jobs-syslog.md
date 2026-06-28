# 22 · Workers, Jobs & Syslog

The `workers/` package holds Vera's distributed-execution and operational-observability internals. The LLM routing half of `cluster.py` is covered in [LLM Cluster](./04-ollama-cluster.md) and the Docker backend in [Docker](./13-docker.md); this page covers the rest: the **worker registry**, **durable job persistence**, the **syslog** subsystem, and the reusable observability elements.

---

## 1. Worker registry & cluster jobs

`workers.py` maintains the worker registry and per-host metrics. Every Vera process is a worker (it runs `worker_loop` and drains `vera:tasks`), and each mirrors its state to Redis at `vera:workers:<worker_id>` with a TTL, refreshed every poll loop. The `obs.workers` cap merges Redis state with the local registry so a dashboard on any host sees every host.

`cluster.py` adds the cross-referenced views and proxy controls:

| Cap | Path | Purpose |
|---|---|---|
| `obs.cluster` | `GET /cluster` | Full cluster view — workers cross-referenced with their Ollama nodes (see [04 §5](./04-ollama-cluster.md)) |
| `obs.proxy_log` | — | Recent entries from the transparent Ollama proxy |
| `cluster.job.stop` | — | Stop an in-flight cluster job |

See [Capability Framework §6](./01-capability-framework.md#6-distributed-dispatch) for how a task dispatched on one host is executed on another and resolved back.

---

## 2. Durable job persistence — `jobs.*`

`job_persistance.py` records dispatched work so it survives a restart. Jobs aren't only in-memory futures — they're persisted, so after a crash Vera can report what was running and recover it.

| Cap | Purpose |
|---|---|
| `jobs.history` | Recent job records (cap, status, timing, host) |
| `jobs.stats` | Aggregate throughput / success-failure stats |
| `jobs.ollama_log` | Log of Ollama calls made through the cluster |
| `jobs.recover_now` | Trigger recovery of recoverable jobs immediately |
| `jobs.running_at_boot` | Jobs that were in-flight when the process last stopped |
| `jobs.delete_consumer` | Remove a dead consumer from the Redis Stream group |
| `jobs.purge_pending` | Clear stuck pending entries from the task stream |

The `jobs.delete_consumer` / `jobs.purge_pending` caps are the maintenance tools for the Redis Streams consumer groups that back distributed dispatch — they clear out dead consumers and orphaned pending messages that would otherwise accumulate.

UI: **`job-persistence-panel`** (`job_persistence_panel.html`) — job history, stats, recovery controls, and the consumer-group maintenance actions.

---

## 3. Syslog — captured events & errors

`syslog.py` is Vera's internal log feed. It captures the event stream and errors, surfaces them in the harness's **Syslog** tab, and can run an LLM **monitor** that watches for and explains problems.

| Cap | Purpose |
|---|---|
| `syslog.query` | Query the captured log feed (filters) |
| `syslog.errors` | Just the errors |
| `syslog.ask` | **LLM**: ask a natural-language question over the logs |
| `syslog.monitor_start` / `monitor_stop` / `monitor_run` | The background monitor that watches for error patterns |
| `syslog.status` | Monitor + feed status |
| `syslog.clear` | Clear the captured feed |

The monitor is gated by the `SYSLOG_MONITOR` env var (see [Configuration](./10-configuration.md)). `syslog.ask` is what powers "why did that fail?" — it feeds recent log context to the cluster and returns an explanation. It is also a [Dream](./17-dream.md) sensor source (`dream.sensor.syslog_errors`).

---

## 4. Reusable observability elements

`observe_elements_capabilities.py` serves two reusable UI custom elements (the same pattern as [Flow Builder & UI Elements](./20-flow-builder.md)):

| Cap | Element | Renders |
|---|---|---|
| `ui.elements.live_event_stream_js` | `<vera-live-event-stream>` | A live, filterable feed of `vera:events` |
| `ui.elements.system_log_js` | `<vera-system-log>` | The syslog feed as a drop-in component |

Any panel can embed these to get a live event/log view without re-implementing the WebSocket plumbing.

---

## See also

- [LLM Cluster](./04-ollama-cluster.md) — the routing half of `cluster.py`; the Ollama node view
- [Docker](./13-docker.md) — `docker.worker.*` spawn containers that join this registry
- [Capability Framework](./01-capability-framework.md#6-distributed-dispatch) — task streams, consumer groups, result listeners
- [Dream](./17-dream.md) — consumes `syslog.errors` and the event bus as sensors
- [Harness UI](./02-harness-ui.md) — the Workers / Redis / Syslog built-in tabs
