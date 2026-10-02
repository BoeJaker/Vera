# Performance and capacity baseline

This page covers the instruments Vera uses to **measure** its own behaviour over
time. They exist so that a regression or an improvement can be shown with
numbers rather than impressions:

- **Code-authoring timing.** A latency envelope on every successful `code.author`
  call, with a percentile summary over recent calls.
- **The loop census.** A fixed set of agentic-loop goals run serially by an
  external harness. Vera reads the results back as a queryable history through
  `census.*`, and can ask the harness to pause, yield or drop.
- **The discovery/context benchmark contract.** An offline, payload-free way to
  compare two retrieval variants on a frozen fixture, with a strict gate.

The code is in `vera/performance/timing_capabilities.py`, the
`code.author` timing helper in `vera/dag/dag_workshop_capabilities.py`,
`vera/census/`, `vera/discovery_benchmark.py` and
`vera/discovery_benchmark_runtime.py`.

Hardware sizing, model throughput benchmarking (`bench.*`) and live host metrics
are covered in [Performance and sizing](./00-performance-and-sizing.md).

**Design rule shared by all three instruments.** Instrumentation must not
change what it measures:

- the timing summary never calls a model
- the census capabilities are read-only, apart from a control *request* the
  harness acts on itself
- the benchmark comparison invokes no providers

## Contents

- [1. Code-author timing envelope](#1-code-author-timing-envelope)
- [2. Recent-window summary](#2-recent-window-summary)
- [3. Storage and retention](#3-storage-and-retention)
- [4. The loop census](#4-the-loop-census)
  - [4.1 Files and configuration](#41-files-and-configuration)
  - [4.2 Capability reference](#42-capability-reference)
  - [4.3 Reading a run honestly](#43-reading-a-run-honestly)
  - [4.4 Controlling a running census](#44-controlling-a-running-census)
- [5. Operator census](#5-operator-census)
- [6. Discovery/context benchmark contract](#6-discoverycontext-benchmark-contract)
- [7. Producing the next baseline](#7-producing-the-next-baseline)
- [8. Troubleshooting](#8-troubleshooting)
- [Related pages](#related-pages)

---

## 1. Code-author timing envelope

When `code.author` streams a file, the code can finish streaming and the user
then waits without knowing what Vera is doing. Disabling validation would make
that pause shorter, at the cost of returning broken files. The first step is to
measure the pause precisely.

Every successful `code.author` result contains `timing`, using schema
`vera.code-author-timing/v1`. Vera also emits a compact `code.author.timing`
event with `path`, `session_id`, `trace_id` and the same `timing` object.

| Field | Meaning |
|---|---|
| `total_ms` | Whole authoring cycle, finalised just before the event is emitted |
| `generation_ms` | Model generation call |
| `post_generation_ms` | From generation return to result ready |
| `last_stream_to_generation_return_ms` | Delay from the last visible streamed chunk until generation returns. A model that stays busy after its last displayed token shows up here. |
| `last_stream_to_result_ready_ms` | Delay from the last visible chunk (generation or repair stream) until the result is ready |
| `phases_ms.preparation` | Before generation |
| `phases_ms.generation` | Generation |
| `phases_ms.parse_and_syntax_repair` | Parsing and syntax-repair model calls |
| `phases_ms.persistence` | File and version persistence |
| `phases_ms.smoke_and_runtime_repair` | Bounded Python smoke execution and runtime-repair calls |
| `counters.syntax_repairs`, `smoke_runs`, `runtime_repairs` | Activity counts |
| `telemetry_emit_ms` | Time spent emitting the event. It is added to the returned result **after** emission, so it is not present in the stored event, and the stream summary does not report it. |

This separates five possible causes of a pause:

- a model still busy after its last displayed token
- syntax repair
- another repair-model call
- file and version persistence
- bounded Python execution with runtime repair

Existing generation, syntax, save and smoke behaviour is unchanged.

---

## 2. Recent-window summary

| Capability | Route | Inputs |
|---|---|---|
| `code.author.timing.summary` | `GET /code/author/timing/summary` | `limit` (1–500, default 200) |

It reads the dedicated, bounded `code.author.timing` Redis stream through
`obs.stream_history`. If that stream is empty (a deployment that predates it),
it falls back to the generic `obs.events` window. It never mixes the two, so
recent calls are not double-counted.

The output (`vera.code-author-timing-summary/v1`) contains:

| Key | Contents |
|---|---|
| `metrics_ms` | For each scalar field: `samples`, `p50`, `p95`, `max` |
| `phases_ms` | The same statistics for each phase |
| `counters` | For each counter: `samples`, `total`, `runs_with_activity`, `activity_rate` |
| `events` | `supplied`, `accepted`, `ignored` |
| `window` | `requested`, `returned`, and the `source` that was used (`code.author.timing` or `events`) |
| `method` | `inclusive-linear-interpolation` |

The summary is strict about what it counts:

- **Percentiles** use inclusive linear interpolation.
- **Ignored inputs.** Invalid values (negative, non-finite, booleans), unrelated
  events and unknown schema versions are ignored and counted, rather than
  silently mixed into the baseline.
- **No content.** The result contains no task text, generated code, session or
  trace identifiers, or paths.
- **No extra infrastructure.** It reuses Redis rather than introducing another
  storage system, and makes no model call.

```bash
curl -s 'localhost:8999/code/author/timing/summary?limit=200' \
  | jq '{events, window, p95_total: .metrics_ms.total_ms.p95, phases: (.phases_ms | map_values(.p95))}'
```

---

## 3. Storage and retention

| Store | Contents |
|---|---|
| Redis stream `vera:stream:code.author.timing` | The full `code.author.timing` event envelope, capped at **500** entries (`maxlen`). It is written by `emit_event` alongside the generic stream, because the busy generic stream would otherwise push out sparse timing samples. |
| Generic event stream / `obs.events` | The same event. This is the fallback source. |

---

## 4. The loop census

The census is the measuring instrument for the agentic loop: a fixed template of
goals, run serially by the harness `run_census.py`. The harness lives outside
the repository. Each finished goal appends one trace record to `census.jsonl`.

The `census.*` capabilities turn those files into a history you can query: runs
over time, per-goal comparisons, what each run found and what landed between runs.

> [!IMPORTANT]
> The census capabilities are **read-only by design**. Nothing in them starts,
> stops or edits a run, because a UI that could quietly perturb the measuring
> instrument would make every number it displays suspect. The one deliberate
> exception is `census.control.set`, which writes a *request* the harness polls
> and acts on itself.

### 4.1 Files and configuration

| Item | Default | Notes |
|---|---|---|
| `VERA_CENSUS_DIR` | `~/loop-census` | The harness directory. Files over 8 MB are refused, in case the variable points somewhere unexpected. |
| `VERA_CENSUS_WALL_CAP_S` | `1800` | Per-goal wall cap shown in live progress, used when the harness does not report its own. |
| `census*.jsonl` | — | One file per run, plus the live file. |
| `goals.json` | — | The queue in real order, used by `census.live`. |
| `census.control.json` | — | The control request Vera writes. The harness polls it every 15 seconds. |
| `census.active.json` | — | The harness's own state report: template, goal in flight, done/total, running/paused/done/dropped. |
| `operator-census*.jsonl` | — | Operator (browser) census runs (§5). |

### 4.2 Capability reference

All of these have `memory="off"`. Every one except `census.control.set` is a
silent GET.

| Capability | Route | Purpose / inputs |
|---|---|---|
| `census.runs` | `GET /census/runs` | Every run with headline numbers: goals, done, `wall_capped`, `unaccounted_total`, `gate_inserted_total`, `warnings_total`, `wall_total_s`, `counters_reconcile`, plus a trend. Partial or failed runs are excluded unless `include_partial=true`. |
| `census.run` | `GET /census/run` | One run in full. `run` (e.g. `run11`, or `current`). |
| `census.compare` | `GET /census/compare` | Two runs **per goal**: `outcome_change` (improved/held/regressed/missing), wall-time and step deltas, worst news first. `base`!, `head`!. |
| `census.live` | `GET /census/live` | The run in flight: progress from `goals.json`, the running goal, elapsed time, live counters, steps so far. Safe to poll. |
| `census.goal` | `GET /census/goal` | Drill into one goal: `done_when`, the steps actually chosen (with caps, tools, cycles and failures), steps the planner never listed, completion-gate state, warnings. `run`, `goal`!. |
| `census.board` | `GET /census/board` | Board items labelled `census:found:<run>` and `census:fixed:<run>`, grouped by run. |
| `census.landed` | `GET /census/landed` | Commits attributed to the first census that finished after them. Commits made while a run was in flight are flagged `during_run`. `repo` (default `vera`), `limit` (300). |
| `census.control` | `GET /census/control` | Control state (run, pause or drop) and the harness's own report. `live` is false when that report is stale. |
| `census.control.set` | `POST /census/control/set` | `action`! (`pause`, `resume`, `drop` or `yield`), `reason`, `by`, `wait_s` (0–300). |
| `census.operator.runs` / `.run` / `.compare` | `GET /census/operator/...` | Operator census (§5). |

### 4.3 Reading a run honestly

The census modules are careful about what a number can and cannot tell you.

**Per goal, never averaged.** Goals differ in cost by two orders of magnitude,
from about a minute to a 25-minute cap. A run-level mean says nothing, so every
comparison is per goal, and a goal missing from one side is reported as missing.

**Counter reconciliation.** If a run's step accounting does not add up
(`unaccounted > 0`), or the run predates the counter, it is marked
`counters_reconcile: false`. A number drawn from that run may be measuring the
instrument rather than the loop.

**Step counts are a diagnostic, not a verdict.**

- Fewer steps can mean a tighter plan, or a plan that dropped half the request.
- `done` is a coarse harness outcome.
- `outcome_change` is a transition worth investigating, not a score.

Open `census.goal` on anything that is flagged.

**Quality checks** (`census/quality.py`) score a goal against **declared,
objective expectations**. There is no LLM judgement, because a model marking its
own homework is the false positive the verifier exists to prevent. The check kinds are:

| Check | Example |
|---|---|
| `exists` | `{"file": "clock.html", "exists": true}` |
| `contains` | `{"file": "clock.html", "contains": "setInterval"}` |
| `regex` | `{"file": "clock.html", "regex": "12h\|24h\|toggle"}` |
| `min_bytes` | `{"file": "clock.html", "min_bytes": 200}` |
| `any_of` | `{"file": "stats.py", "any_of": ["def mean", "def median"]}` |
| `absent` | `{"file": "index.html", "absent": "TODO"}` |
| `answer_contains` | `{"answer_contains": "391"}` |

Every check carries a `why`. Unknown check kinds are reported as errors, never
silently passed. Loop Lab task suites reuse the same evaluator
([33](./33-evolve.md)).

**Run health** (`census/run_health.py`) is a pure reader that asks whether a run
measured the loop or measured its environment. The signature of a contended
network is a *contrast*: network-bound goals degrade together while
compute-bound goals hold their times. The reader computes drift against a
baseline run and flags a degraded run, so that its numbers are not read as a
code change.

**Landed work** (`census/landed.py`) attributes commits to runs by time window.
A commit made while a run was in flight is flagged, because the loop was still
executing the previous build. The run start is estimated as the end time minus
the sum of goal wall times, which errs towards *fewer* flags. A flag that does
appear is worth believing.

### 4.4 Controlling a running census

| Action | Effect |
|---|---|
| `pause` | Cancels the goal in flight. Once resumed and prod is healthy, that goal **re-runs from scratch**. The abandoned attempt is noted on the row (`reruns`) and never recorded as a result. |
| `yield` | The polite pause. The goal in flight finishes untouched, then the harness parks before the next goal (`active.state = paused`, `pause_kind = yield`). Nothing is cancelled or re-run. |
| `resume` | Lifts a pause or yield. |
| `drop` | Cancels the goal and ends the set. Finished goals are archived as `-partial-dropped`. |

`sys.dev.restart` writes a pause before it re-execs, and the census module
lifts it on the way back up. Passing `resume_census=false` writes a drop
instead. A prod restart therefore no longer costs a census.

**Protocol for a test that needs the GPU while a census runs:**

1. Call `census.control.set` with `action=yield`.
2. Wait until it is acknowledged, either by passing `wait_s`, or by polling
   `census.control` for `active.state = paused`.
3. Run the test.
4. Call `census.control.set` with `action=resume`.

A yield is acknowledged only when the goal in flight ends, which can be up to a
whole wall cap away. `wait_s` is therefore capped at 300 seconds; poll for
anything longer.

```json
{"name": "census.control.set", "arguments": {"action": "yield", "reason": "gpu benchmark", "wait_s": 300}}
```

Scheduling censuses on a calendar, and gating a production release on the
census, are described in [Evolve / Loop Lab](./33-evolve.md).

---

## 5. Operator census

`operator.run` (the browser operator, [34](./34-operator.md)) has its own census
files (`operator-census*.jsonl`). It reuses the loop census's file machinery
but deliberately not its scoring, because a browser run fails differently.

Outcomes are ranked worst to best:

| Rank | Meaning |
|---|---|
| `incomplete` | No completion event at all. Nothing about the run can be trusted, including its duration. |
| `ceiling` | It ran out of **steps**. Its own `reason` may still read like success, so this ranks below an honest error. |
| `errored` | It finished, but steps failed along the way. |
| `finished` | It ran cleanly to a stop. This does **not** mean the goal was achieved. |

A run that is both at its ceiling and errored takes the lower rank. There is no
`success` rank, because only a human or a checked assertion can decide that.

| Capability | Route | Purpose |
|---|---|---|
| `census.operator.runs` | `GET /census/operator/runs` | Per run: goals finished, errored, at ceiling or incomplete; total steps and errors; thrash count; `comparable`. |
| `census.operator.run` | `GET /census/operator/run` | One run. Each row carries the browser `run_id`, which opens in `operator.trace`. |
| `census.operator.compare` | `GET /census/operator/compare` | Per-goal comparison on the outcome ladder. |

---

## 6. Discovery/context benchmark contract

`vera/discovery_benchmark.py` (schema `vera.discovery-context-benchmark/v1`)
defines how two discovery/context retrieval **variants** are compared on a frozen
**fixture**. The comparison is pure: `effect: "none"`, `providers_invoked: false`.
The fixture contains:

- a dataset snapshot
- cases, with relevant ids and sources
- variants
- repeated observations

Its limits are 10,000 cases, 64 variants, 1,000 repetitions, 1,000 hits per
observation and 1,000,000 observations.

**Metrics per completed observation:**

- `ndcg`, `mrr`
- `source_recall`, `source_precision`
- `citation_coverage`, `answer_support`
- `freshness` (exact revision match)
- `redundancy`

**Aggregates per variant:**

- unsuccessful and useful-context rates
- mean quality
- latency (scout, first useful context, end-to-end), including `first_useful_p95`
- resources (bytes, cost units, CPU ms, GPU ms)
- policy violations

**The gate.** `compare_context_benchmark(fixture, baseline, candidate, gate)`
passes only when the candidate:

| Requirement | Default threshold |
|---|---|
| Improves p95 time-to-first-useful-context | by at least **5%** |
| Improves nDCG | by at least **0.01** |
| Regresses any other quality metric (MRR, citation coverage, answer support, freshness, recall, precision, redundancy) | by no more than **0** |
| Unsuccessful rate, useful-context rate, policy violations | No regression |
| Per-case nDCG | No regression |
| Per-case p95 latency | Regression of at most **10%** |

Every failure becomes a named blocker, such as `p95_useful_context_speed_not_improved`
or `case_latency_regressed:<case>`.

**Running variants live.** `vera/discovery_benchmark_runtime.py` runs variants
against a fixture to produce the observations:

- `ContextBenchmarkRuntime` drives the runs.
- Per-variant timeouts record `timed_out` / `variant_timeout`.
- `BenchmarkMilestones` marks `scout_complete` and `useful_context`.
- `snapshot_retrieval_runner` adapts a snapshot retrieval binding.

**Recording a result.** `operator.discovery.benchmark.record` stores an
already-computed comparison in the bounded operator read model.
`operator.discovery.evidence` (`GET /operator/discovery/evidence`) reads it
back, payload-free.

The route surface being benchmarked is inventoried in
[41](./41-system-inventory.md#8-source-bound-authority-reviews).

---

## 7. Producing the next baseline

The timing instrumentation, the aggregation and their tests are deterministic.
They deliberately do not launch a model workload of their own.

A representative baseline should:

1. Freeze a set of representative authoring tasks.
2. Run them **serially** through the shared model gate, and not while a census is
   running. Use `census.control.set action=yield` if one is in flight.
3. Publish p50/p95 values by phase, language, file size, repair count, route and
   model, using `code.author.timing.summary`.

The same vocabulary can then cover prose authoring, DAG and loop transitions,
queue admission, and the gap from one capability result to the next loop cycle.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `code.author.timing.summary` reports `error: timing event observers are unavailable` | Neither `obs.stream_history` nor `obs.events` is registered in this process. |
| `window.source` is `events` and there are few samples | The dedicated stream is empty (an older deployment, or Redis was flushed). It fills as `code.author` runs. |
| `census.runs` shows fewer runs than files | Partial or failed runs are hidden by default; pass `include_partial=true`. |
| `census.control` shows `live: false` | The harness's active report is stale. The harness probably died without saying so. |
| `census.control.set` returns `acked: false` after a yield | The goal in flight has not finished yet. Keep polling `census.control`. |

---

## Related pages

- [Performance and sizing](./00-performance-and-sizing.md) — hardware sizing, `bench.*` model benchmarks, live metrics
- [Evolve / Loop Lab](./33-evolve.md) — census schedules, release gating on the census, task suites
- [Operator](./34-operator.md) — the browser runs the operator census measures
- [System inventory](./41-system-inventory.md) — architecture snapshots and authority reviews
- [DAG engine](./03-dag-engine.md) — `code.author` and the Workshop
