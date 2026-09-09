"""Pure tests for gpu_trace_core — the parsing/verdict behind bench.node_trace.

Imports lowercase `vera.catalog.gpu_trace_core` with the repo root on sys.path
so the WORKTREE copy is exercised: `Vera.vera.…` is a namespace package that
resolves to whatever is first on sys.path, which on the host is the MAIN
checkout, not this branch.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.catalog.gpu_trace_core import (  # noqa: E402
    decode_throttle, parse_gpu_trace, trace_summary)

pytestmark = pytest.mark.critical

# A real capture from gpu-250 during a 900-token generation: the card boosts to
# 1380 MHz and decays to 742 under SW_THERMAL_SLOWDOWN (0x20) while the chassis
# fans stay low. This is the case a single snapshot cannot see.
THROTTLING = """\
1380, 877, 73, 174.72, 73, 0x0000000000000000
1252, 877, 80, 150.99, 73, 0x0000000000000020
1080, 877, 78, 123.37, 72, 0x0000000000000000
885, 877, 78, 113.68, 79, 0x0000000000000020
742, 877, 78, 100.42, 83, 0x0000000000000020
"""

# The same node with its clock pinned: steady, cool, no flags.
STABLE = """\
900, 877, 57, 110.03, 73, 0x0000000000000000
900, 877, 58, 107.01, 79, 0x0000000000000000
900, 877, 61, 108.56, 81, 0x0000000000000000
"""


def test_decode_throttle_names_known_bits():
    assert decode_throttle("0x0000000000000020") == ["sw_thermal_slowdown"]
    assert decode_throttle("0x0000000000000004") == ["sw_power_cap"]
    # Combined mask reports every reason, not just the first.
    assert decode_throttle("0x0000000000000024") == ["sw_power_cap",
                                                     "sw_thermal_slowdown"]


def test_decode_throttle_empty_and_unknown():
    assert decode_throttle("0x0000000000000000") == []
    assert decode_throttle("") == []
    assert decode_throttle("not-hex") == []
    # An unrecognised bit stays VISIBLE as hex rather than being dropped.
    assert decode_throttle("0x1000") == ["0x1000"]


def test_parse_skips_malformed_lines_without_losing_the_trace():
    text = THROTTLING + "garbage line\n" + "1, 2, 3\n"
    rows = parse_gpu_trace(text)
    assert len(rows) == 5
    assert rows[0]["sm_mhz"] == 1380
    assert rows[0]["mem_mhz"] == 877
    assert rows[-1]["throttle"] == ["sw_thermal_slowdown"]


def test_parse_empty_input():
    assert parse_gpu_trace("") == []
    assert parse_gpu_trace(None) == []


def test_summary_reports_throttling_and_droop():
    s = trace_summary(parse_gpu_trace(THROTTLING))
    assert s["throttle_reasons"] == ["sw_thermal_slowdown"]
    assert s["clock"]["max_mhz"] == 1380
    assert s["clock"]["min_mhz"] == 742
    # 1380 -> 742 is a ~46% droop; the number is the point of the capability.
    assert 45 <= s["clock"]["droop_pct"] <= 47
    assert s["peak_temp_c"] == 80
    assert s["throttled_samples"] == 3
    assert "THROTTLING" in s["verdict"]


def test_summary_clean_when_clock_is_pinned():
    s = trace_summary(parse_gpu_trace(STABLE))
    assert s["throttle_reasons"] == []
    assert s["clock"]["droop_pct"] == 0.0
    assert s["throttled_pct"] == 0
    assert "no throttling" in s["verdict"]


def test_summary_excludes_idle_samples():
    """Idle samples describe nothing about the load being attributed."""
    idle = "1245, 877, 52, 33.87, 0, 0x0000000000000000\n"
    s = trace_summary(parse_gpu_trace(idle + STABLE))
    # The 1245 MHz idle row must not become the max and invent a droop.
    assert s["samples"] == 3
    assert s["clock"]["max_mhz"] == 900
    assert s["clock"]["droop_pct"] == 0.0


def test_summary_empty_returns_falsy_not_a_clean_bill_of_health():
    """A failed sampler must not read as 'no throttling observed'."""
    assert trace_summary([]) == {}
    assert trace_summary(parse_gpu_trace("nvidia-smi: command not found")) == {}
