"""Which vision model an image actually gets routed to.

``vision.describe`` tries candidates in the order ``_list_vision_models``
returns them, so that ordering *is* the model-selection policy. It used to be
raw ``/api/tags`` order, which meant a general model that merely accepts images
(gemma3) beat a dedicated vision-language model, and a CPU node beat the GPU.
For the Scan Station — reading titles and small print off product photos, with
an operator waiting — both are the wrong pick.

Pure-unit: the ranking helpers are exec'd out of the module source, so this runs
without fastapi/httpx/the orchestrator.
"""

import os
import re

import pytest

_MOD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "vera", "media", "media_capabilities.py")


@pytest.fixture(scope="module")
def rank():
    src = open(_MOD, encoding="utf-8").read()
    start = src.index("_VISION_PATTERNS = re.compile")
    end = src.index("async def _list_vision_models")
    ns: dict = {"re": re}
    exec(compile(src[start:end], _MOD, "exec"), ns)
    return ns


def _order(rank, models, instances):
    """Reproduce _list_vision_models' sort over a fake estate."""
    rows = []
    for iid, has_gpu in instances.items():
        for m in models:
            if rank["_VISION_PATTERNS"].search(m):
                rows.append({"model": m, "instance": iid,
                             "_score": rank["_vision_score"](m), "_gpu": has_gpu})
    rows.sort(key=lambda f: (-f["_score"], not f["_gpu"], f["model"]))
    return [(r["model"], r["instance"]) for r in rows]


ESTATE = {"gpu-250": True, "cpu-246": False, "cpu-247": False}
# What the nodes actually carry once the pulls land.
DEPLOYED = ["nemotron-3-super:latest", "gemma3:27b", "gpt-oss:20b", "codestral:latest",
            "gemma3:12b", "qwen3.5:9b", "mistral:7b", "qwen3-vl:8b",
            "mistral-small3.2:latest"]


def test_dedicated_vl_beats_a_general_multimodal(rank):
    assert _order(rank, DEPLOYED, ESTATE)[0] == ("qwen3-vl:8b", "gpu-250")


def test_gpu_copy_is_preferred_over_the_cpu_copies(rank):
    """Same model on three nodes — the interactive path should land on the V100."""
    top3 = _order(rank, ["qwen3-vl:8b"], ESTATE)[:3]
    assert top3[0][1] == "gpu-250"
    assert {i for _, i in top3} == set(ESTATE)      # CPUs remain as fallbacks


def test_gemma3_still_used_when_it_is_all_there_is(rank):
    """Ranking must not become a filter — a general multimodal is better than
    failing with 'no vision-capable model found'."""
    order = _order(rank, ["gemma3:27b", "qwen3.5:9b"], ESTATE)
    assert order and order[0][0] == "gemma3:27b"


def test_text_only_models_are_never_offered(rank):
    order = _order(rank, ["qwen3.5:9b", "codestral:latest", "nemotron-3-super:latest"], ESTATE)
    assert order == []


@pytest.mark.parametrize("better,worse", [
    ("qwen3-vl:8b", "qwen2.5vl:7b"),        # current generation first
    ("qwen2.5vl:7b", "llava:13b"),
    ("minicpm-v:8b", "moondream:latest"),   # moondream is a last resort
    ("llava:13b", "gemma3:27b"),            # purpose-built beats general
    ("mistral-small3.2:latest", None),      # general, but still ranked above nothing
])
def test_family_precedence(rank, better, worse):
    if worse is None:
        assert rank["_vision_score"](better) > 10
        return
    assert rank["_vision_score"](better) > rank["_vision_score"](worse)


def test_unrecognised_vision_model_still_ranks_above_nothing(rank):
    """A future VL model we have no rule for must remain selectable."""
    assert rank["_vision_score"]("some-new-vlm:7b") > 0
    assert rank["_vision_score"]("some-new-vlm:7b") < rank["_vision_score"]("qwen3-vl:8b")
