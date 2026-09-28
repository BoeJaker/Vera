# Canonical system inventory

`system.inventory` captures Vera's loaded architecture as canonical JSON
without changing runtime state or calling a model.

The snapshot currently includes:

- capability names, schemas, implementations, exposure, modes, sources, tags,
  streams, HTTP bindings, and conservative inferred-role hints;
- loaded modules and explicit optional-module load errors;
- UI panels and HTTP routes;
- schedule definitions without volatile run counters or timestamps;
- worker runtime records and MCP server registrations;
- declared configuration-key presence, never configuration values;
- stored DAG identities, shape, tags, and referenced capability names from both
  the DAG store and Fabric DAG store, without definitions, prompts, or state;
- table names declared by loaded local modules, without opening a database or
  returning SQL definitions or row data;
- artifact provider surfaces, explicitly excluding project artifact content;
- saved connection identity/type and whether a credential reference exists,
  without endpoints, credential IDs, metadata, or secrets;
- evidence-based caller edges for Python registration, HTTP routes, MCP
  exposure, schedules, and stored workflow nodes;
- bounded observed caller edges from recent capability telemetry, reduced to
  canonical UI, agent, or system actor class, registered capability, terminal
  outcome, and aggregate count;
- name-family signals for `loop`, `pipeline`, `workflow`, `run`, `job`, `task`,
  `scheduler`, `generate`, `query`, and `store` duplication investigations.

The capability returns a compact summary by default so routine agent and
dashboard calls do not ingest a multi-megabyte manifest. Pass `detail=true` for
the canonical records used for migration evidence. Every response carries
`schema_version`, `captured_at`, counts, and a structural
SHA-256 fingerprint. The fingerprint excludes capture time, volatile worker
state, schedule run counters, and registry load order. Two scans of an unchanged
process should therefore match even while its heartbeat and schedules advance.
Observed caller edges are operational evidence and are also excluded from the
structural fingerprint, so ordinary UI and agent activity cannot manufacture
architecture drift. They never retain session or trace IDs, timestamps,
previews, arguments, results, or error text.
The full worker records remain in the snapshot for operational evidence; they
simply do not create false architectural drift.

## First retained baseline

The isolated W0-01 sandbox produced two consecutive matching scans on
2026-08-23:

| Field | Value |
| --- | ---: |
| Structural fingerprint | `1bec8bbb4eb82077db8c2d03c3a552c0fa1358f97f9b0b0764c47f4f3d54a326` |
| Capabilities | 2,125 |
| Loaded-module records | 143 |
| Module errors | 2 |
| UI panels | 66 |
| HTTP routes | 2,882 |
| Unique method/path signatures | 2,846 |
| Schedules | 60 |
| Workers | 1 |
| MCP servers | 0 |

The module errors were retained rather than silently skipped:

- `spritegen_capabilities`: `No module named 'PIL'`;
- `worldview_jepa`: `'NoneType' object has no attribute 'Module'`.

Keyword-family counts were: `loop` 4, `pipeline` 18, `workflow` 0, `run` 54,
`job` 6, `task` 7, `scheduler` 4, `generate` 12, `query` 11, and `store` 18.
These are discovery signals, not proof that two capabilities are semantically
duplicated.

## Honest limitations and next slices

Role inference classified 2,121 capabilities as the default `public_task`, two
as `internal`, and two as `experimental`. That imbalance is evidence that current
registration metadata cannot yet support the role taxonomy reliably; inferred
roles must not be used as policy. Capability Contract v2 should replace these
hints with explicit ownership/lifecycle metadata.

W0-01 now covers every named surface in the baseline. Coverage does not mean
unbounded data extraction: workflow definitions and artifact contents are
deliberately excluded, database evidence is declared schema rather than live row
inspection, and connections are aggressively redacted. The caller graph records
interfaces and stored-workflow nodes supported by concrete metadata; it does not
pretend to be a complete dynamic Python trace. UI, agent, and background-system
caller identity is now enriched from a bounded window of the shared capability
event telemetry. Coverage remains explicitly `partial`: an absent edge means it
was not observed in the retained window, not that no caller exists.

Configuration coverage reports only whether each centrally declared key was
explicitly supplied by the environment; defaults and all values remain private.
Subsequent snapshots should be retained only
at meaningful branch or release boundaries, and comparisons should report added,
removed, and changed identities rather than committing a multi-megabyte dump on
every startup.

## Deprecation evidence

Compatibility paths are inventoried as candidates, not assumed to be dead.
Explicit alias declarations provide the candidate, replacement, and owner;
Vera does not infer lifecycle state from naming conventions. The canonical
capability inventory carries the declared replacement alongside each alias.

Usage evidence has nine named source classes: code references, HTTP callers,
MCP callers, stored workflows, schedules, UI links, configuration, artifacts,
and external consumers. Every report must account for every class as complete,
partial, or unavailable. An unavailable source remains a blocker rather than
silently becoming evidence of zero use.

HTTP and MCP alias calls increment durable, payload-free counters. These
counters retain the alias, its replacement, transport class, count, and latest
observation time; they do not retain arguments or results. Supplied code and
configuration snapshots can be scanned deterministically, retaining only a
digest of the source reference and an exact-token count. Health checks and
migration probes must be classified explicitly and are reported separately
from real consumers.

The resulting inventory is evidence only. Even complete coverage with no known
consumer produces an independent-review candidate, never removal authority.

Independent reviews are content-addressed records for exactly one candidate.
Each review binds the candidate, the exact inventory digest, evidence digests,
consumer count, coverage state, rationale, and required follow-up. Supported
recommendations are retain, adapt, migrate, insufficient evidence, or removal
candidate. The last of these requires complete zero-consumer coverage plus
semantic-contract and rollback evidence, but still carries no removal authority;
the separate removal gate remains mandatory.
