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


# -- one model, named once (owner, 12 Oct 2026) ------------------------------------------------------
# A role's `model` may name ANOTHER role of the same profile: "@executor" means "whatever model the
# executor role uses". Owner: "make sure the model it uses is the same so you don't have to update 2
# places" - the loop's coder now follows the executor, so changing the executor's model changes both.
# One hop only: a reference to a role that is itself a reference (or missing) resolves to "" (the
# caller's default model), never a loop.

def model_ref(rule) -> str:
    """The role a rule's model refers to ("executor" for "@executor"), else ""."""
    m = str((rule or {}).get("model") or "").strip()
    return m[1:].strip() if m.startswith("@") and len(m) > 1 else ""


def follow_model_ref(rule, lookup):
    """`rule` with an "@<role>" model replaced by that role's model (`lookup(role)` -> its rule).
    The reference is kept as `model_ref` so the UI can show it. Other rules come back unchanged."""
    target = model_ref(rule)
    if not target:
        return rule
    other = lookup(target) or {}
    model = str(other.get("model") or "").strip()
    if model.startswith("@"):
        model = ""
    out = dict(rule)
    out["model"] = model
    out["model_ref"] = "@" + target
    return out


def follow_model_refs(roles: dict) -> dict:
    """Every role in one profile's roles dict with its "@role" model resolved."""
    roles = dict(roles or {})
    return {name: follow_model_ref(rule, roles.get) for name, rule in roles.items()}
