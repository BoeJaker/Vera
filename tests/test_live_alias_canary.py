import asyncio
import json

import pytest

from vera.models.live_alias_canary import run_live_alias_canary


pytestmark = pytest.mark.critical


class Runner:
    def __init__(self):
        self.calls = 0

    async def __call__(self, _prompt, **kwargs):
        self.calls += 1
        callback = kwargs.get("stream_cb")
        if self.calls == 3:
            await asyncio.Future()
        if callback:
            await callback("VERA_OK")
        kwargs["meta_out"].update({"eval_count": 1})
        return "VERA_OK"


@pytest.mark.asyncio
async def test_live_alias_canary_joins_evidence_without_activating_traffic(tmp_path):
    ticks = iter((100, 110, 120, 130, 140))
    report = await run_live_alias_canary(
        model="qwen3:0.6b", instance_id="cpu-247", alias="portable-canary",
        state_dir=tmp_path, source_commit="a" * 40,
        source_operation="live-canary-1", runner=Runner(),
        gate={"enabled": True, "coord_connected": True,
              "coordination_mode": "direct", "nodes": [
                  {"node": "cpu-247", "gated": True, "capacity": 1,
                   "has_gpu": False}]},
        artifact={"sha256": "b" * 64, "size_bytes": 42},
        clock=lambda: next(ticks))

    assert report["readiness"]["ready_for_activation_review"] is True
    assert report["traffic_activation_allowed"] is False
    assert len(report["evidence_ids"]) == 5
    encoded = json.dumps(report)
    assert "Reply with" not in encoded
    assert "VERA_OK" not in encoded
