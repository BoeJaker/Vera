"""A GPU model load that hangs is flagged on /health (11 Oct 2026: gpu-250's Ollama wedged -
every load hung until a restart while the resident 9b kept serving, so nothing looked wrong)."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera import gpu_slot_core as G  # noqa: E402

SRC = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical
RES = ["jaahas/qwen3.5-uncensored:9b"]


def test_a_hung_load_on_a_gpu_is_a_suspect():
    base = dict(has_gpu=True, timed_out=True, model="qwen2.5:7b", model_resident=False, resident=RES)
    assert G.load_suspect(**base)
    assert not G.load_suspect(**dict(base, has_gpu=False))          # CPU nodes: just slow
    assert not G.load_suspect(**dict(base, timed_out=False))        # a real error, not a hang
    assert not G.load_suspect(**dict(base, model_resident=True))    # the resident model itself
    assert not G.load_suspect(**dict(base, resident=[]))            # nothing resident: a cold load
    assert not G.load_suspect(**dict(base, model=""))


def test_suspects_age_out():
    s = {"gpu-250": {"model": "m", "ts": 1000.0}, "gpu-x": {"model": "m", "ts": 1000.0 - G.SUSPECT_TTL_S - 5}}
    assert list(G.live_suspects(s, 1000.0 + 60)) == ["gpu-250"]
    assert G.live_suspects({}, 1.0) == {}


def test_wired_into_failures_refresh_and_health():
    assert "_gpu_slot_core.load_suspect(" in SRC and "_GPU_LOAD_SUSPECT[chosen] = {" in SRC
    assert '"gpu_load_suspect": _gpu_slot_core.live_suspects(_GPU_LOAD_SUSPECT, time.time()),' in SRC
    ref = SRC[SRC.index("async def _refresh_gpu_resident("):]
    ref = ref[:ref.index("\nasync def ") if "\nasync def " in ref else len(ref)]
    assert "_GPU_LOAD_SUSPECT.pop(iid, None)" in ref
