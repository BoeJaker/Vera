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

Enforcement requires signed, single-use, scope-bound approval receipts and a
trusted policy context set by Vera—not raw model or MCP arguments. Receipts must
bind capability, effects, tenant/session, expiry, and nonce; expanded effects,
replay, alias substitution, or scope changes must fail. Only then should a
small migrated capability family move from shadow observation to blocking.

