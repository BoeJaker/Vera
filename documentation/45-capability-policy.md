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
keys never enter receipt bodies or telemetry. This slice still does not enforce
policy. Next, Vera needs a durable cross-process replay ledger and a trusted dispatcher
context before one small capability family can move from shadow observation to
blocking.
