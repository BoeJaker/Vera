"""The node agent reports every GPU, not the first line of nvidia-smi.

Pins edge/node_runner_core.parse_gpus, which edge/vera_node_agent.py uses for
/node/status `gpus` (and `gpu`, the first card, kept for older readers).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge"))

from node_runner_core import GPU_QUERY, parse_gpus  # noqa: E402


def test_query_order_matches_the_parser():
    assert GPU_QUERY.split(",") == ["index", "name", "memory.total", "memory.used",
                                    "memory.free", "utilization.gpu", "temperature.gpu"]


def test_one_card_as_gpu_250_reports_it():
    rows = parse_gpus("0, Tesla V100-PCIE-12GB, 12288, 8200, 3853, 0, 41\n")
    assert rows == [{"index": 0, "name": "Tesla V100-PCIE-12GB", "total_mb": 12288, "used_mb": 8200,
                     "free_mb": 3853, "util_pct": 0, "temp_c": 41}]


def test_every_card_is_kept_in_order():
    rows = parse_gpus("0, A, 100, 10, 90, 5, 40\n1, B, 200, 20, 180, 97, 71\n")
    assert [(r["index"], r["name"], r["util_pct"]) for r in rows] == [(0, "A", 5), (1, "B", 97)]


def test_unreported_fields_are_none_not_a_dropped_card():
    rows = parse_gpus("0, Old Card, 4096, 100, 3996, [N/A], [Not Supported]\n")
    assert len(rows) == 1
    assert rows[0]["util_pct"] is None and rows[0]["temp_c"] is None
    assert rows[0]["total_mb"] == 4096


def test_no_output_and_short_lines():
    assert parse_gpus("") == []
    assert parse_gpus(None) == []
    assert parse_gpus("No devices were found\n") == []
