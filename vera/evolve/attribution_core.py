"""Pure attribution rules shared by Loop Lab and deterministic tests."""


def controller_for(caller_kind: str = "", background: bool = False) -> str:
    """Return the honest controller bucket for an observed caller signal."""
    kind = str(caller_kind or "").strip().lower()
    if kind == "codex":
        return "codex"
    if kind in {"mcp", "claude", "claude_code"}:
        return "claude_code"
    if background:
        return "autonomous"
    return "user"


def author_agent_for(controller: str = "") -> str:
    """Map a pipeline controller to the authorship-map category."""
    return {
        "codex": "codex",
        "claude_code": "claude",
        "autonomous": "vera-agent",
    }.get(str(controller or "").strip().lower(), "direct")


def effective_controller(controller: str = "", via: str = "") -> str:
    """Recover transitional records written before a caller kind was supported.

    Old Vera versions bucketed an unknown explicit caller as ``user`` but also
    persisted the untouched ``via`` signal. Only override that residual bucket;
    never replace an already-specific controller.
    """
    current = str(controller or "").strip().lower()
    observed = controller_for(via)
    if current in {"", "user"} and observed in {"codex", "claude_code"}:
        return observed
    return current or "user"
