from pathlib import Path

import pytest


pytestmark = pytest.mark.critical
ROOT = Path(__file__).parents[1]
ORCHESTRATION = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
WORKSHOP = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_shared_event_boundary_observes_after_session_stamp_and_before_serialization():
    emit = ORCHESTRATION[ORCHESTRATION.index("async def emit_event(event: dict):"):]
    scheduled = "call_soon(observe_agent_loop_event, dict(event))"
    assert emit.index("_session_stamp(event)") < emit.index(scheduled)
    assert emit.index(scheduled) < emit.index("ev_json = json.dumps(event)")
    assert "journal I/O must never add latency" in emit
    assert "Shared Run history is a shadow read model, never event authority" in emit


def test_both_redis_and_no_redis_stream_paths_finish_the_exact_projection():
    endpoint = WORKSHOP[WORKSHOP.index('@APP.post("/workshop/agent_loop/stream")'):]
    assert "start_agent_loop_projection(" in endpoint
    assert endpoint.count("finish_agent_loop_projection(") >= 5
    assert "result=result" in endpoint
    assert "cancelled=True" in endpoint
    assert "error_type=type(e).__name__" in endpoint
    assert "bind_agent_loop_projection(_run_projection)" in endpoint
    assert endpoint.count("reset_agent_loop_projection(_projection_token)") == 2
    start_sse = endpoint.index('yield _sse({')
    first_projection = endpoint.index("_run_projection = start_agent_loop_projection(")
    assert start_sse < first_projection


def test_projection_does_not_change_sse_payload_or_model_prompt():
    endpoint = WORKSHOP[WORKSHOP.index('@APP.post("/workshop/agent_loop/stream")'):]
    sse = endpoint.index("yield _sse({")
    projection = endpoint.index("_run_projection = start_agent_loop_projection(")
    redis_branch = endpoint.index("r = _redis()")
    assert sse < redis_branch < projection
    assert '"type":             "start"' in endpoint[sse:redis_branch]
    call = endpoint[projection:endpoint.index(")", projection) + 1]
    assert "system_prompt" not in call
