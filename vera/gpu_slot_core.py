"""Do not send a model to a GPU that is full of a busy other model.

11 Oct 2026: every loop/coder call (qwen2.5:7b) on gpu-250 timed out at 300 s, all night.
The GPU's warm plan keeps the 9b resident and the loop's executor/planner/chat hit it every
few seconds, so it is never idle; Ollama's scheduler waits for the resident runner to go idle
before unloading it, so the 7b load starved until the client gave up and fell back to a CPU
node - 6-8 min per code.author, every build goal wall-capped. (Diagnosis with four-minds,
vera-62: Vera's gate/routing passed each call in ms; the wait was inside Ollama.)

Rule: when the requested model is NOT among the GPU's resident models, the GPU is busy, and a
CPU node can serve the model, route to the CPU node now instead of queueing behind the
resident one. Anything unknown (no fresh residency reading, nothing resident) leaves the
routing as it was. Keys on "not resident while busy", not on one slot, so it holds for a
multi-slot warm plan too.

Pure: facts in, a decision out.
"""

from __future__ import annotations

from typing import Iterable, Tuple

#: A residency reading older than this is not trusted (the cache is refreshed per call).
RESIDENT_TTL_S = 30.0
#: A GPU that served a request this recently, or has one in flight, counts as busy.
BUSY_WINDOW_S = 45.0


#: How long a load-wedge suspicion is shown without a fresh one.
SUSPECT_TTL_S = 7200.0
WEDGE_ADVICE = ("a model LOAD on this GPU timed out while its resident model kept serving - most likely "
                "a wedged Ollama scheduler (11 Oct 2026); restarting ollama-vera on the node clears it")


def load_suspect(*, has_gpu: bool, timed_out: bool, model: str, model_resident: bool,
                 resident: Iterable[str]) -> bool:
    """Was this failure a GPU model LOAD that hung? A timeout, on a GPU, for a model that was
    not resident while something else was (11 Oct: every such load hung until restart)."""
    res = [str(m) for m in (resident or []) if str(m).strip()]
    return bool(has_gpu and timed_out and model and not model_resident and res)


def live_suspects(suspects: dict, now: float, ttl_s: float = SUSPECT_TTL_S) -> dict:
    """The suspicions still worth showing (younger than ttl_s)."""
    return {k: v for k, v in (suspects or {}).items() if now - float((v or {}).get("ts") or 0) < ttl_s}


def is_busy(in_use: int, last_used_at: float, now: float, window_s: float = BUSY_WINDOW_S) -> bool:
    return int(in_use or 0) > 0 or (bool(last_used_at) and now - float(last_used_at) < window_s)


def skip_gpu(*, model: str, model_resident: bool, resident: Iterable[str], resident_at: float,
             busy: bool, cpu_alternative: bool, now: float,
             ttl_s: float = RESIDENT_TTL_S) -> Tuple[bool, str]:
    """(skip, why). Skip the GPU only when every fact says the request would wait for an
    unload: the model is not resident, something else is, the reading is fresh, the GPU is
    busy, and a CPU node has the model."""
    res = [str(m) for m in (resident or []) if str(m).strip()]
    if not model or model_resident or not cpu_alternative:
        return False, ""
    if not res or not resident_at or now - float(resident_at) > ttl_s:
        return False, ""                      # unknown or empty: the GPU can load it
    if not busy:
        return False, ""                      # idle: the swap is quick
    return True, ("gpu slot: '%s' not resident (resident: %s) and the GPU is busy - "
                  "a CPU node with the model instead of waiting for an unload"
                  % (model, ", ".join(res[:3])))
