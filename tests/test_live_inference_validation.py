import asyncio
import json

import pytest

from vera.models.live_inference_validation import (
    LIVE_INFERENCE_REPORT_SCHEMA, require_shared_gate, validate_ollama_provider)


pytestmark = pytest.mark.critical


class Runner:
    def __init__(self):
        self.calls = []

    async def __call__(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs.copy()))
        callback = kwargs.get("stream_cb")
        if callback:
            await callback("VERA_")
            await callback("OK")
        kwargs["meta_out"].update({"eval_count": 2})
        return "VERA_OK"


@pytest.mark.asyncio
async def test_live_validator_uses_portable_binding_and_emits_no_content():
    runner = Runner()
    report = await validate_ollama_provider(
        model="qwen2.5:0.5b", instance_id="gpu-250",
        artifact_sha256="a" * 64, artifact_size=42, runner=runner)

    assert report["schema"] == LIVE_INFERENCE_REPORT_SCHEMA
    assert report["passed"] is True
    assert report["privacy"] == "prompt_and_output_omitted"
    assert [case["case"] for case in report["cases"]] == ["non_stream", "stream"]
    assert all(case["output_sha256"] ==
               "627f745fdb5b4165c6c5070fc0f0720bdd29f693f87c8a28bb21b593680242a2"
               for case in report["cases"])
    encoded = json.dumps(report)
    assert "Reply with" not in encoded
    assert "VERA_OK" not in encoded
    assert len(runner.calls) == 2
    assert runner.calls[0][1]["model"] == "qwen2.5:0.5b"
    assert runner.calls[0][1]["instance_id"] == "gpu-250"
    assert runner.calls[0][1]["options"] == {"num_predict": 32, "temperature": 0}
    assert runner.calls[0][1]["think"] is False
    assert runner.calls[1][1]["stream_cb"] is not None


@pytest.mark.asyncio
async def test_failed_terminal_is_reported_without_backend_detail():
    async def failed(_prompt, **_kwargs):
        raise RuntimeError("private backend error")

    report = await validate_ollama_provider(
        model="model", instance_id="node", artifact_sha256="b" * 64,
        artifact_size=1, runner=failed)
    assert report["passed"] is False
    assert {case["error_code"] for case in report["cases"]} == {"backend_error"}
    assert "private backend error" not in json.dumps(report)


@pytest.mark.parametrize('mode', ['direct', 'controller_broker'])
def test_live_admission_accepts_connected_shared_gate_transports(mode):
    gate = {'enabled': True, 'coord_connected': True,
            'coordination_mode': mode,
            'nodes': [{'node': 'gpu', 'gated': True, 'capacity': 1}]}
    assert require_shared_gate(gate, 'gpu') == mode


@pytest.mark.parametrize('gate,instance', [
    ({}, 'gpu'),
    ({'enabled': False, 'coord_connected': True, 'nodes': []}, 'gpu'),
    ({'enabled': True, 'coord_connected': False, 'nodes': []}, 'gpu'),
    ({'enabled': True, 'coord_connected': True, 'nodes': []}, 'gpu'),
    ({'enabled': True, 'coord_connected': True,
      'nodes': [{'node': 'cpu', 'gated': False, 'capacity': 0}]}, 'cpu'),
    ({'enabled': True, 'coord_connected': True,
      'nodes': [{'node': 'gpu', 'gated': True, 'capacity': True}]}, 'gpu'),
    ({'enabled': True, 'coord_connected': True, 'coordination_mode': 'private',
      'nodes': [{'node': 'gpu', 'gated': True, 'capacity': 1}]}, 'gpu'),
])
def test_live_admission_refuses_missing_or_node_local_coordination(gate, instance):
    with pytest.raises(RuntimeError, match='live validation refused'):
        require_shared_gate(gate, instance)


def test_cli_source_uses_gate_status_not_direct_redis_access():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'vera' / 'models' /
              'live_inference_validation.py').read_text(encoding='utf-8')
    run_source = source[source.index('async def _run'):source.index('\ndef main')]
    assert '_ensure_coord_redis' not in run_source
    assert 'require_shared_gate(gate, instance_id)' in run_source
    assert '"error_code": code' in source


@pytest.mark.asyncio
async def test_timeout_reaps_runner_and_stops_additional_cases():
    calls = []
    reaped = asyncio.Event()

    async def stalled(_prompt, **_kwargs):
        calls.append(1)
        try:
            await asyncio.Future()
        finally:
            reaped.set()

    report = await validate_ollama_provider(
        model="model", instance_id="node", artifact_sha256="b" * 64,
        artifact_size=1, runner=stalled, case_timeout_seconds=0.02)
    assert not report["passed"]
    assert len(report["cases"]) == len(calls) == 1
    assert report["cases"][0]["error_code"] == "case_timeout"
    assert reaped.is_set()
