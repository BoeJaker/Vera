from pathlib import Path

from Vera.vera.capability_orchestration import _generation_phase_timing_ms


ROOT = Path(__file__).resolve().parents[1]


def test_phase_timing_uses_monotonic_boundaries_without_overlap():
    assert _generation_phase_timing_ms(10.0, 10.25, 11.0) == {
        "queue_ms": 250, "provider_ms": 750, "total_ms": 1000,
    }


def test_queue_failure_has_zero_provider_time():
    assert _generation_phase_timing_ms(10.0, None, 10.4) == {
        "queue_ms": 400, "provider_ms": 0, "total_ms": 400,
    }


def test_ollama_completion_event_separates_queue_provider_and_total_time():
    source = (ROOT / "vera" / "capability_orchestration.py").read_text(
        encoding="utf-8")

    assert '"queue_ms": _queue_ms' in source
    assert "_generation_phase_timing_ms(" in source
    assert "eval_count / _provider_elapsed" in source
    assert '"request_stage": str(request_stage or "")[:64]' in source


def test_loop_reasoning_calls_are_labeled_without_changing_their_roles():
    source = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(
        encoding="utf-8")

    for label in ("planner", "executor", "controller", "critic", "gate"):
        assert f'request_stage="{label}"' in source
    assert 'profile=LOOP_ROUTING_PROFILE, role="controller"' in source


def test_loop_ui_presents_human_labels_and_split_timings():
    source = (ROOT / "vera" / "agent_loop_ouput.js").read_text(encoding="utf-8")

    assert "quality check" in source
    assert "completion check" in source
    assert "queue ${fmt(queueMs)}" in source
    assert "provider ${fmt(providerMs)}" in source
    assert "W4-" not in source
