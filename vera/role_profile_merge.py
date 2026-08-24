"""Pure merge rule for USER role-profile overrides (Model Routing).

A USER role rule REPLACES the DECLARED one wholesale — `_effective_role_profiles`
does `merged["roles"][role] = r`, not a field-wise merge. So a partial override
silently DISCARDS everything the declared rule tuned.

THE INCIDENT (2026-08-24). On prod, `loop/coder` was overridden to name a
different model. Because the override carried no `options`, it also dropped the
declared `{"temperature": 0.7, "top_p": 0.9}` (commit 7e0e974), and the sibling
loop roles lost `num_ctx: 16384` the same way. Two separately-landed tuning fixes
were inert on prod with nothing in the UI to indicate it. The routing panel's
"add role" path sends no `options` key at all, which is how such an override
gets created in the first place.

The rule: a field the caller actually SENT wins, even when falsy (an explicit
`prefer_gpu: false` must stay false). A field the caller OMITTED inherits from
the declared rule.
"""

# Identity/display fields — rebuilt by `_role_rule`, never inherited.
IDENTITY_FIELDS = ("pattern", "label", "declared_by", "role")


def inherit_declared_fields(declared: dict, supplied: dict) -> dict:
    """Overlay `supplied` on `declared`, inheriting only genuinely ABSENT keys."""
    if not declared:
        return dict(supplied or {})
    out = dict(supplied or {})
    for k, dv in declared.items():
        if k in IDENTITY_FIELDS:
            continue
        if k not in out:
            out[k] = dv
    return out
