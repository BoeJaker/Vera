# loop-census: the harness that measures Vera's agentic loop

This folder holds the census runner and its probe tools. It drives prod over HTTP, scores
each goal with its template's own checks, and records one row per goal. It was kept outside
the repo until 4 Oct 2026, with no tests, review or history. Several of that day's bugs were
in it.

## Code here, data elsewhere

| | where | what |
|---|---|---|
| **code** | this folder (the repo checkout) | runners, ingest, review and probe scripts |
| **data** | `VERA_CENSUS_DIR`, default `~/loop-census` | run archives `census.*.jsonl`, logs, `census.active.json` / `census.control.json`, `templates/`, `census_stalls/`, `helpers.json` |

The scheduler (`evolve.schedule.*`) still starts `census_all.sh` in `VERA_CENSUS_DIR`. The
copies there are thin shims that `exec` the scripts in this folder, so Vera itself is
unchanged. `templates/` stays a data folder because sessions edit it.

## Environment

| variable | default | meaning |
|---|---|---|
| `VERA_CENSUS_DIR` | `~/loop-census` | the data folder (see above) |
| `CENSUS_BASE` | `https://localhost:8999` | the Vera to measure (`https://localhost:8981` = the bleeding-edge mirror) |
| `CENSUS_TEMPLATE`, `CENSUS_MODEL`, `CENSUS_PLAN_STYLE`, `CENSUS_INTENT_CORE` | | what to run; see `run_census.py --help` |
| `CENSUS_STALL_DUMP_S` | `600` | no loop event for this long, then dump stacks and await chains to `census_stalls/` |
| `CENSUS_REDIS_URL` | from the prod process | probes only: where loop events are read |
| `CENSUS_PY` | `~/langchain/bin/python3` | probes only: a Python with redis-py |

No host addresses or credentials are written in these files. Redis is found at runtime.

## Scripts

- `census_all.sh`: runs the scheduled templates one after another, archives each run and
  ingests it. The scheduler calls this.
- `run_census.py`: one template against one Vera. `--list` lists the templates.
- `run_operator_census.py`: the same for `operator.run`.
- `ingest_census.py`, `census_ingest_watch.sh`, `fix_reroutes.py`, `backfill_code.py`: post
  archives to Vera's task system and repair old rows.
- `review_run.py`, `verify_review.py`: read a run back.
- `probes/`: short targeted loop probes, as opposed to whole censuses.
  - `probe_layout.py <template> <goal> [layout] [budget]` runs one goal under a role layout
    (L0 baseline, L1 warm CPU coder, L2 CPU MoE executor, L3/L4 deliverable review) and
    scores it.
  - `probe_batch.sh <file>` runs a list of these, waiting for a free box before each.
  - `rev.py <session>` prints a loop's events from Redis.
  - Probes declare themselves on `/health` (`activity`) while they run.

Rules learned the hard way:
- Read loop events from Redis (`rev.events`), never the SSE reattach. It returned nothing
  while prod was restarting.
- An unreadable "running" list means *unknown* (busy), never "nothing running".
- Never `pkill -f` / `pgrep -f` a pattern that appears in your own command line.
