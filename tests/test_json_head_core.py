"""vera/json_head_core.json_head: json.dumps(x)[:n] without encoding the rest (py-spy, prod, 2026-10-05)."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.json_head_core import json_head  # noqa: E402

CASES = [
    {}, [], {"a": 1}, {"text": "x" * 5000}, [{"id": i, "v": [i] * 10} for i in range(300)],
    {"nested": {"k": [1, 2.5, None, True, "é"]}}, "plain", 42, None,
]


@pytest.mark.parametrize("value", CASES)
@pytest.mark.parametrize("n", [0, 1, 7, 200, 4096])
def test_the_same_text_as_dumps_then_slice(value, n):
    assert json_head(value, n) == json.dumps(value)[:n]


def test_the_same_error_as_dumps():
    with pytest.raises(TypeError):
        json_head({"x": object()}, 4096)


def test_a_huge_result_is_not_encoded_whole():
    big = {"rows": [{"id": i, "text": "word " * 40} for i in range(60000)]}
    t = time.perf_counter(); head = json_head(big, 4096); fast = time.perf_counter() - t
    t = time.perf_counter(); json.dumps(big); full = time.perf_counter() - t
    assert head == json.dumps(big)[:4096]
    assert fast < full / 5, (fast, full)


def test_the_wrapper_and_the_activity_preview_use_it():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert "json.dumps(result)[:" not in src
    assert "(_json_head(result, 4096)" in src and "return _json_head(result, _ACT_PREVIEW_MAX_CHARS)" in src
