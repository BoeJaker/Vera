from vera.evolve.attribution_core import author_agent_for, controller_for, effective_controller


def test_codex_is_a_distinct_controller_and_author():
    assert controller_for("codex") == "codex"
    assert author_agent_for("codex") == "codex"


def test_legacy_mcp_remains_claude_and_background_remains_autonomous():
    assert controller_for("mcp") == "claude_code"
    assert controller_for("claude") == "claude_code"
    assert controller_for("", background=True) == "autonomous"
    assert controller_for("") == "user"


def test_transitional_user_record_recovers_explicit_codex_via():
    assert effective_controller("user", "codex") == "codex"
    assert effective_controller("claude_code", "codex") == "claude_code"
    assert effective_controller("user", "") == "user"
