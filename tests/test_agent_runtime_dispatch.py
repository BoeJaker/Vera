import pytest

from Vera.vera.execution.agent_runtime_dispatch import (
    AgentRuntimeDispatch,
    project_agent_runtime_dispatch,
    safely_select_agent_runtime_dispatch,
    select_agent_runtime_dispatch,
)


ENGINE_CAPABILITIES = {
    "v5": "dag.agent_loop_v5", "v6": "dag.agent_loop_v6",
    "v7": "dag.agent_loop_v7",
}


def test_projection_is_stable_content_free_and_inert():
    first = project_agent_runtime_dispatch(
        session_id="v8:program:research:1", engine="v6",
        capability="dag.agent_loop_v6",
    )
    second = project_agent_runtime_dispatch(
        session_id="v8:program:research:1", engine="v6",
        capability="dag.agent_loop_v6",
    )

    assert first == second
    assert first["schema"] == "vera.agent-runtime-dispatch/v1"
    assert first["workflow_id"] == "agent-loop:v6"
    assert first["execution_authority"] == "native_capability"
    assert first["adapter_mode"] == "shadow"
    assert first["executes"] is False
    assert first["changes_dispatch"] is False
    assert "goal" not in first
    assert "prompt" not in first


def test_profile_dispatch_names_the_existing_profile_runner():
    value = select_agent_runtime_dispatch(
        session_id="v8:program:writer:2", profile="coding",
        engine_capabilities=ENGINE_CAPABILITIES,
    )

    assert value["runtime_id"] == "vera.agent-loop.profile"
    assert value["selected_capability"] == "loops.run"
    assert value["profile"] == "coding"
    assert value["deferred_semantics"] == [
        "runtime_adapter_cancel", "runtime_adapter_launch",
        "runtime_adapter_stream",
    ]


def test_configured_engine_overrides_loop_engine_using_native_selection_rules():
    value = select_agent_runtime_dispatch(
        session_id="v8:program:review:3", configured_engine="v7",
        loop_engine="v5", engine_capabilities=ENGINE_CAPABILITIES,
    )

    assert value["runtime_id"] == "vera.agent-loop.v7"
    assert value["selected_capability"] == "dag.agent_loop_v7"


def test_unknown_engines_fail_back_to_the_native_default():
    value = select_agent_runtime_dispatch(
        session_id="v8:program:review:4", configured_engine="future",
        loop_engine="missing", engine_capabilities=ENGINE_CAPABILITIES,
    )

    assert value["runtime_id"] == "vera.agent-loop.v6"
    assert value["selected_capability"] == "dag.agent_loop_v6"


def test_safe_projection_never_blocks_native_dispatch_on_legacy_state():
    value = safely_select_agent_runtime_dispatch(
        session_id="legacy\nforged", profile="Bad Profile",
        engine_capabilities=ENGINE_CAPABILITIES,
    )

    assert value["projection_status"] == "unavailable"
    assert value["execution_authority"] == "native_capability"
    assert value["portable_semantics"] == []
    assert value["executes"] is False
    assert value["changes_dispatch"] is False


@pytest.mark.parametrize("changes", [
    {"engine": "../../host"},
    {"capability": "dag.agent loop"},
    {"profile": "Coding Profile"},
    {"session_id": "x\nforged"},
])
def test_projection_rejects_unbounded_or_ambiguous_identity(changes):
    values = {"session_id": "v8:p:l:1", "engine": "v6",
              "capability": "dag.agent_loop_v6", "profile": ""}
    values.update(changes)
    with pytest.raises(ValueError):
        AgentRuntimeDispatch(**values)
