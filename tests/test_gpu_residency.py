"""The media models must not sit on the LLM's GPU while idle.

Pins edge/gpu_residency_core.py, the decision half of GPU_inference.py's
residency manager.

Measured on CT126, 2026-09-16: one Tesla V100-PCIE-12GB (12,288 MiB) shared by
ollama and the Whisper/TTS/Stable-Diffusion server. The media server held
3,548 MiB continuously for 2 days 18 hours while idle, leaving ollama
8,740 MiB. At the configured OLLAMA_MAX_AUTO_CTX=28672 the LLM needs ~10,560 MiB
(6,844 weights + 3,760 KV at 128 KiB/token), so it was ~1.8 GB short and
silently offloaded layers to CPU: loop_executor throughput was 2.53 tok/s
against 12-30 tok/s when it fits, and 0.05 on a genuine CPU node.

The LLM is the priority tenant. These tests pin that.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge"))

from gpu_residency_core import can_place, new_state, parkable  # noqa: E402

CARD_MB = 12288          # Tesla V100-PCIE-12GB, the real one
RESERVE = 512
IDLE = 120.0
NOW = 1_000_000.0


def _st(*, resident=True, busy=0, last=NOW, mb=0):
    s = new_state()
    s.update({"resident": resident, "busy": busy, "last": last, "mb": mb})
    return s


# ── admission: may a media model take the card? ──────────────────────────────

def test_takes_the_card_when_there_is_room():
    assert can_place(free_mb=8000, need_mb=3548, reserve_mb=RESERVE) is True


def test_refuses_when_it_would_eat_the_llm_reserve():
    # 3548 + 512 = 4060 wanted, only 3600 free.
    assert can_place(free_mb=3600, need_mb=3548, reserve_mb=RESERVE) is False


def test_the_real_situation_llm_loaded_media_stays_off():
    """ollama resident at 10,560 MiB leaves 1,728 free — media must stay on CPU."""
    free = CARD_MB - 10560
    assert can_place(free_mb=free, need_mb=3548, reserve_mb=RESERVE) is False


def test_the_real_situation_llm_idle_media_may_load():
    """With the LLM unloaded the whole card is free; 3.5GB fits comfortably."""
    assert can_place(free_mb=CARD_MB, need_mb=3548, reserve_mb=RESERVE) is True


def test_unmeasured_model_uses_the_default_not_zero():
    """need_mb=0 means 'never parked yet'. It must NOT read as 'needs nothing',
    which would let a model take a nearly-full card."""
    assert can_place(free_mb=600, need_mb=0, reserve_mb=RESERVE,
                     default_need_mb=1024) is False
    assert can_place(free_mb=2000, need_mb=0, reserve_mb=RESERVE,
                     default_need_mb=1024) is True


def test_a_full_card_never_admits_anything():
    assert can_place(free_mb=0, need_mb=10, reserve_mb=0) is False


# ── parking: give the card back when idle ────────────────────────────────────

def test_idle_model_is_parked():
    state = {"sd": _st(last=NOW - IDLE - 1)}
    assert parkable(state, now=NOW, idle_s=IDLE) == ["sd"]


def test_recently_used_model_is_kept():
    state = {"sd": _st(last=NOW - 5)}
    assert parkable(state, now=NOW, idle_s=IDLE) == []


def test_a_model_with_a_job_in_flight_is_never_taken_from_under_it():
    state = {"sd": _st(busy=1, last=NOW - IDLE - 999)}
    assert parkable(state, now=NOW, idle_s=IDLE) == []


def test_already_parked_model_is_not_parked_again():
    state = {"sd": _st(resident=False, last=NOW - IDLE - 1)}
    assert parkable(state, now=NOW, idle_s=IDLE) == []


def test_freshly_loaded_model_is_not_reaped_before_its_first_job():
    """last=0 means never stamped; reaping on that would park a model that was
    loaded moments ago and is about to be used."""
    state = {"sd": _st(last=0)}
    assert parkable(state, now=NOW, idle_s=IDLE) == []


def test_idle_zero_disables_parking():
    """The escape hatch back to the old always-resident behaviour."""
    state = {"sd": _st(last=NOW - 99999)}
    assert parkable(state, now=NOW, idle_s=0) == []


def test_both_models_park_independently():
    state = {"sd": _st(last=NOW - IDLE - 1), "whisper": _st(last=NOW - 1)}
    assert parkable(state, now=NOW, idle_s=IDLE) == ["sd"]


def test_empty_and_malformed_state_do_not_raise():
    assert parkable({}, now=NOW, idle_s=IDLE) == []
    assert parkable({"x": None}, now=NOW, idle_s=IDLE) == []


def test_new_state_starts_parked_and_idle():
    s = new_state()
    assert s["resident"] is False and s["busy"] == 0 and s["mb"] == 0
