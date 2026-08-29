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

The resulting control path is intentionally layered:

1. the registry exposes the implementation and input schema;
2. Contract v2 describes canonical task, effects, output, policy, and resources;
3. `cap.resolve.shadow` ranks eligible implementations without invoking them;
4. the policy boundary decides whether scoped authority is sufficient;
5. the existing dispatcher executes only after that boundary; and
6. activity and privacy-safe observations report what actually happened.

Keeping these stages separate prevents a good resolver score from becoming an
authorization grant and prevents recent runtime telemetry from silently
rewriting a stable declaration.

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

`cap.contract.coverage` measures migration rather than merely listing gaps. It
reports declaration rates for output schemas, effects, ownership, policy,
execution, operational quality, and resources; aggregates missing fields by
capability group; and returns a bounded, deterministic hotspot list. Projected
legacy defaults do not count as declarations, so the figures cannot improve
unless metadata was actually supplied.

`cap.contract.gate` is the incremental strict gate. Callers must supply an exact
CSV/list of capability names. Only that selected migration set is required to
have output schema, effects, owner, approval, secret, filesystem, network,
tenant, idempotency, and resource declarations; the untouched legacy registry
does not create a flag day. Unknown capability names fail closed. Deterministic
contract errors always fail, and callers can additionally make warnings fatal
with `fail_on_warnings=true`.

`cap.contract.observations` supplies the measured half of the contract. It
aggregates a bounded recent `cap.ok`/`cap.error` event window into call counts,
success rate, observed health, and p50/p95/maximum latency. Stable declared
manifests and their fingerprints remain unchanged: runtime evidence is a
separate, expiring view rather than silently rewritten contract metadata.

The observation output is deliberately sparse. It never includes arguments,
argument previews, prompts, result previews, returned values, exception text,
session IDs, or trace IDs. A capability with no recent samples is unknown, not
healthy. Cost and task-quality remain unknown until Vera has trustworthy usage
and evaluation sources for them.

## Lifecycle and deprecation

Lifecycle is one of `active`, `experimental`, `internal`, `deprecated`,
`removed`. A deprecated contract must declare:

- a different replacement capability;
- an ISO `YYYY-MM-DD` sunset;
- a concise `deprecation_reason`.

A removed capability cannot remain MCP-exposed. Deprecation fields on any other
lifecycle produce a warning, preventing half-applied migrations from looking
complete. These checks are inspectable and gateable but do not remove or reroute
capabilities automatically.

## First migrated family

The first explicit family covers the overlapping generation and authoring tools:

| Implementation | Canonical task | Distinguishing contract |
|---|---|---|
| `llm.generate` | `text.generate` | General routed generation; optional workspace read/write |
| `ollama.generate_raw` | `text.generate` | Direct low-level model-cluster implementation |
| `code.author` | `source_file.author` | Versioned source write, syntax/smoke execution, model use |
| `prose.author` | `document.author` | Grounded, versioned document write and model use |

This grouping does not alias the specialist file authors into generic text
generation. It makes the shared implementation family visible while preserving
the materially different task and effect contracts a resolver needs.

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

## Resolver shadow mode

`cap.resolve.shadow` is the first consumer of v2 manifests. It accepts a
canonical task plus optional allowed effects, required resource classes, and
preferred implementation names. It returns eligible candidates in deterministic
rank order and structured reasons for every exclusion within that task family.
The candidate window is bounded (1–500) and reports truncation; unrelated task
families are not copied into the response.

The preview may use redacted health, reliability, and latency observations. An
observed unhealthy implementation is excluded; absent evidence stays unknown.
Preferences precede reliability, p95 latency, locality, and a stable name
tie-breaker.

This is deliberately observational: every response reports `authorized: false`
and `executed: false`. It never invokes a candidate or grants permission.

## Rollout path

The first four slices provide projection, stable fingerprints, bounded
inspection, coverage measurement, lifecycle validation, an incremental strict
gate, privacy-safe operational observations, deterministic lint fixtures, and
explicit generation/authoring contracts. Subsequent W1-03 slices should:

1. inventory and classify the remaining high-use capability families;
2. declare effects and output schemas for those families;
3. add trustworthy cost/usage and task-quality feeds; recent health, reliability,
   and latency evidence is already available;
4. apply lifecycle/deprecation declarations to real retiring aliases as they are
   identified;
5. feed complete, gated manifests to W1-04 resolver shadow mode.

The deterministic resolver corpus now demonstrates the pure selection boundary,
and the first narrow policy family has reversible enforcement. General execution
selection remains unchanged: promotion beyond that family still requires
complete gated contracts, frozen safety cases, shadow parity, and an explicit
rollback plan.
