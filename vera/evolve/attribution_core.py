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
