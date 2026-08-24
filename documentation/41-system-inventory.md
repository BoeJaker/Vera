# Canonical system inventory

`system.inventory` is the first executable slice of master-roadmap package
W0-01. It captures Vera's loaded architecture as canonical JSON without changing
runtime state or calling a model.

The snapshot currently includes:

- capability names, schemas, implementations, exposure, modes, sources, tags,
  streams, HTTP bindings, and conservative inferred-role hints;
- loaded modules and explicit optional-module load errors;
- UI panels and HTTP routes;
- schedule definitions without volatile run counters or timestamps;
- worker runtime records and MCP server registrations;
- declared configuration-key presence, never configuration values;
- name-family signals for `loop`, `pipeline`, `workflow`, `run`, `job`, `task`,
  `scheduler`, `generate`, `query`, and `store` duplication investigations.

The capability returns a compact summary by default so routine agent and
dashboard calls do not ingest a multi-megabyte manifest. Pass `detail=true` for
the canonical records used for migration evidence. Every response carries
`schema_version`, `captured_at`, counts, and a structural
SHA-256 fingerprint. The fingerprint excludes capture time, volatile worker
state, schedule run counters, and registry load order. Two scans of an unchanged
process should therefore match even while its heartbeat and schedules advance.
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

W0-01 remains active. The inventory still needs stored workflow definitions,
database schemas, artifacts, connections, and a static/dynamic caller graph.
Configuration coverage now reports only whether each centrally declared key was
explicitly supplied by the environment; defaults and all values remain private.
Subsequent snapshots should be retained only
at meaningful branch or release boundaries, and comparisons should report added,
removed, and changed identities rather than committing a multi-megabyte dump on
every startup.
