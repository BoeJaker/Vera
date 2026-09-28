"""A healthy node that refuses a made-up model has not faulted.

Census run59 (2026-09-22): three 404s for `fast-preview` in two seconds put
gpu-250 offline; the next executor call spilled to a CPU node for 45 min and
every embed behind it timed out. Pure: no orchestrator, no Ollama.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.ollama_node_fault_core import is_model_not_found, is_node_fault  # noqa: E402

NOT_FOUND = "Exception: ollama returned 404: {\"error\":\"model 'fast-preview' not found\"}"


def test_the_census_404_is_not_a_node_fault():
    assert is_model_not_found(NOT_FOUND)
    assert not is_node_fault(NOT_FOUND)
    assert not is_node_fault({"error": "model 'x' not found"})


def test_transport_errors_still_are():
    for e in ("ReadTimeout: timed out waiting for response (generation exceeded the request timeout)",
              "ConnectError: connection refused", "ollama returned 500: internal", "",
              None):
        assert is_node_fault(e), repr(e)
    assert not is_model_not_found("")


def test_three_not_found_answers_leave_the_node_online():
    """The orchestrator's rule, run on a fake instance record exactly as the
    error path applies it."""
    inst = {"errors": 0, "status": "online"}
    for err in (NOT_FOUND, NOT_FOUND, NOT_FOUND):
        if is_node_fault(err):
            inst["errors"] += 1
            if inst["errors"] >= 3:
                inst["status"] = "offline"
    assert inst == {"errors": 0, "status": "online"}
    for err in ("timed out", "timed out", "timed out"):
        if is_node_fault(err):
            inst["errors"] += 1
            if inst["errors"] >= 3:
                inst["status"] = "offline"
    assert inst["status"] == "offline"
