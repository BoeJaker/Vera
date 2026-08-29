# Capability policy boundary

W1-05 begins by making Vera's existing central capability wrapper policy-aware
without changing execution behavior. The policy evaluator is pure, bounded, and
content-free: it reads only a capability's Contract v2 declaration plus scoped
facts such as allowed effects and whether a session or tenant identity is
present. It never reads prompts, arguments, results, credentials, or secret
values.

Every non-silent capability call now carries a `policy` field on its existing
`cap.call` event. The field reports a shadow verdict:

- `allow`: declarations and simulated grants are sufficient;
- `deny`: a required effect grant, approval, scope, or opaque-secret condition
  is missing;
- `indeterminate`: required contract posture is not declared yet.

All three remain `authorized: false` and `executed: false`. This slice observes
the gap; it does not block dispatch and does not treat caller-supplied flags as
trusted approval.

`cap.policy.shadow` previews the same evaluator for one registered capability.
Its effect grants and approval flags are explicitly simulation inputs. Unknown
capabilities fail closed. The default wrapper context grants only `none` and
`read`, so sensitive calls expose the authority they currently lack while
legacy contractless capabilities remain visibly indeterminate.

## Next enforcement work

`vera.approval_receipts` now supplies the trusted receipt primitive needed by a
future enforcement boundary. HMAC-SHA256 receipts bind the exact capability,
sorted effects, tenant, session, issue/expiry times, and nonce. Verification is
strict and content-free; tampering, expanded effects, replay, alias substitution,
scope changes, unknown fields, and expired/future receipts fail closed. A
thread-safe in-process nonce ledger makes consumption single-use within one
runtime.

Issuance is deliberately a Python core operation, not a public capability:
models and MCP callers cannot ask Vera to mint their own authority, and signing
keys never enter receipt bodies or telemetry.

`RedisNonceReplayLedger` now consumes nonces across workers with one atomic
`SET NX EX`; keys contain only a nonce hash and expire with the receipt. Redis
outage fails closed. Successful consumption creates a sealed
`TrustedPolicyContext`, propagated through a `ContextVar` by trusted dispatcher
code rather than capability arguments. The central wrapper uses an exactly
capability/session-matched, still-unexpired context for its shadow verdict and
exposes only a bounded, content-free projection. Lookalike MCP arguments cannot
forge it.

This slice still does not enforce policy. Next, one small migrated capability
family can add a feature-flagged blocking boundary with shadow/enforce parity,
failure isolation, and rollback.

## Selective enforcement rollout

The first blocking boundary is deliberately restricted to the four migrated
`run.shadow.*` inspection capabilities. Default `VERA_POLICY_MODE=shadow`
preserves existing behavior. Enforcement requires both:

- `VERA_POLICY_MODE=enforce`
- `VERA_POLICY_ENFORCE_FAMILIES=run.shadow`

The wrapper records `selected`, `would_block`, and `blocked` beside the shadow
verdict. A blocked call emits `cap.denied` and raises before local or distributed
dispatch, streams, caching, or activity recording. Removing the family or
setting mode back to `shadow` is a runtime kill switch; unknown families never
become eligible. Trusted, exact, unexpired receipt context permits the call,
while lookalike arguments do not.

`cap.policy.enforcement.status` (also `GET /cap/policy/enforcement`) exposes the
bounded rollout state and rollback instruction for operator/UI use. It never
returns raw environment values, receipts, nonces, arguments, or secrets.

This is still a narrow rollout, not global authorization. Promotion of another
family requires complete contracts, frozen bypass fixtures, observed shadow
parity, and an explicit rollback plan.

The surrounding UI and activity surfaces consume only the bounded policy
projection: verdict/reason codes, whether enforcement selected the family, and
whether the call would be or was blocked. They do not receive receipt material,
nonces, signing keys, arguments, or result content. Resolver preference remains
separate from authorization—a top-ranked candidate can still be denied, and a
valid receipt cannot make an incompatible candidate eligible.

## Frozen W1-05 gate

`eval.policy.boundary` runs `evaluations/policy-boundary-v1.json` as one
deterministic completion report. The six cases cover every named W1-05 attack:
prompt injection, alias bypass, callback injection, replayed approval, secret
leakage, and confused deputy. Invalid or incomplete corpora fail closed before
case evaluation. The evaluator uses only synthetic receipts, invokes no
capability, performs no network access, and returns reason codes rather than
fixture values.
