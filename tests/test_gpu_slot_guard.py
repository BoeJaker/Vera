"""A model that is not resident on a busy GPU goes to a CPU node instead of waiting for an
unload (11 Oct 2026: every loop/coder call timed out at 300 s while the warm 9b stayed busy)."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera import gpu_slot_core as G  # noqa: E402

SRC = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical
NOW = 1000.0
RES = ["jaahas/qwen3.5-uncensored:9b"]


def kw(**over):
    d = dict(model="qwen2.5:7b", model_resident=False, resident=RES, resident_at=NOW - 5,
             busy=True, cpu_alternative=True, now=NOW)
    d.update(over)
    return d


def test_skips_only_when_every_fact_says_it_would_wait():
    skip, why = G.skip_gpu(**kw())
    assert skip and "qwen2.5:7b" in why and "qwen3.5-uncensored:9b" in why
    assert G.skip_gpu(**kw(model_resident=True)) == (False, "")          # it is the resident one
    assert G.skip_gpu(**kw(busy=False)) == (False, "")                   # idle GPU: swap is quick
    assert G.skip_gpu(**kw(cpu_alternative=False)) == (False, "")        # nowhere else to go
    assert G.skip_gpu(**kw(resident=[])) == (False, "")                  # nothing resident
    assert G.skip_gpu(**kw(resident_at=0)) == (False, "")                # never read
    assert G.skip_gpu(**kw(resident_at=NOW - 999)) == (False, "")        # stale reading
    assert G.skip_gpu(**kw(model="")) == (False, "")


def test_busy_is_in_flight_or_recent():
    assert G.is_busy(1, 0.0, NOW)
    assert G.is_busy(0, NOW - 10, NOW)
    assert not G.is_busy(0, NOW - G.BUSY_WINDOW_S - 1, NOW)
    assert not G.is_busy(0, 0.0, NOW)


def test_wired_into_the_router_and_refreshed_before_routing():
    pick = SRC[SRC.index("def pick_instance("):]
    pick = pick[:pick.index("\ndef ")]
    i = pick.index("_gpu_slot_core.skip_gpu(")
    assert i < pick.index("    if prefer_gpu:\n        # Best: GPU node that has the model")
    assert "_model_tag_core.is_served(model, _res)" in pick
    gen = SRC[SRC.index("async def ollama_generate("):]
    assert gen.index("await _refresh_gpu_resident()") < gen.index("chosen = pick_instance(")
