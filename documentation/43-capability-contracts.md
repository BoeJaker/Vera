# Capability contracts

Vera's capability registry is intentionally broad: local functions, distributed
workers, DAG-backed tools, and MCP providers all appear through one invocation
surface. That interoperability is useful, but a name, input schema, and prose
description are not enough for a resolver or tool-using model to choose safely
between similar implementations.

Capability Contract v2 adds an inspection layer around the current registry. It
does not replace registration, dispatch, or authorization. Existing capabilities
continue to run exactly as before while their metadata is made explicit and
improved incrementally.

## Manifest model

Every registry entry can be projected as `vera.capability-contract/v2`. The
manifest separates:

- the canonical task from the concrete implementation and aliases;
- lifecycle state from availability;
- input and output schemas;
- declared effects from policy requirements;
- streaming, retries, timeout, idempotency, cancellation, and pagination;
- health, cost, latency, quality, and resource information;
- owner, documentation, HTTP exposure, MCP exposure, tags, and provider source.

Missing declarations are represented as `status: unknown`. Vera does not infer
that a capability is safe, idempotent, cheap, or side-effect free merely because
the old registry did not record those properties. This distinction is important:
unknown metadata is migration work, not permission.

The optional `contract={...}` argument on `@capability` supplies v2 declarations.
It is stored alongside the existing registry entry but is not consumed by the
executor in this foundation slice.

## Inspection capabilities

`cap.contract.manifest` projects the live registry. Call it with an exact `name`,
a `prefix`, a bounded `limit` from 1 to 500, and optionally
`include_internal=true`. Results are sorted by capability name and include a
SHA-256 fingerprint that is stable across registry load order.

`cap.contract.lint` checks the projected manifests without invoking them. Its
result is bounded independently of the total issue count and reports truncation.
The initial rules cover:

- required inputs missing from schema properties;
- secret-like plain string inputs instead of opaque secret references;
- invalid lifecycle and effect values;
- aliases owned by more than one capability;
- mutating HTTP capabilities whose effects remain undeclared;
- provider implementations presented as user tasks without canonical mapping.

Errors identify internally contradictory or unsafe contracts. Warnings identify
migration gaps which need an explicit declaration; they do not automatically
disable existing tools in this slice.

## Declaring a contract

```python
@capability(
    "records.update.impl",
    contract={
        "canonical_task": "records.update",
        "aliases": ["records.write"],
        "lifecycle": "active",
        "effects": ["write"],
        "output_schema": {"type": "object"},
        "approval": {"status": "required"},
        "secrets": {"status": "not_required"},
        "resources": {"status": "declared", "classes": ["cpu"]},
    },
)
async def update_record(...):
    ...
```

Secret-bearing inputs should use `format: secret-ref` or
`x-vera-secret-ref: true` in their schema. The reference is opaque; later policy
work resolves it only inside the authorized execution boundary.

## Rollout path

The foundation provides projection, stable fingerprints, bounded inspection,
and deterministic lint fixtures. Subsequent W1-03 slices should:

1. inventory and classify high-use capability families;
2. declare effects and output schemas for those families;
3. add ownership, health, latency, cost, and quality feeds;
4. model deprecation and aliases without hiding the canonical task;
5. make strict lint gates incremental, starting with changed contracts;
6. hand complete manifests to W1-04 resolver shadow mode.

Execution selection remains unchanged until shadow evaluation demonstrates that
the richer contracts improve choices without unsafe exclusions or added side
effects.
