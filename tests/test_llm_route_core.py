"""llm.route may only try plain text generators (never itself, never caps with effects)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.llm_route_core import GENERATORS, route_candidates  # noqa: E402

pytestmark = pytest.mark.critical

REGISTRY = [
    "llm.route", "llm.summarize", "llm.classify", "llm.generate", "llm.route.resolve",
    "vllm.server.stop", "vllm.server.start", "pxstore.backend.provision_vllm",
    "vllm.generate", "vllm.instances.remove", "echo",
]


def test_only_generators_are_candidates():
    assert route_candidates(REGISTRY) == ["llm.generate", "vllm.generate"]


def test_never_routes_to_itself_or_effectful_caps():
    got = set(route_candidates(REGISTRY))
    for bad in ("llm.route", "vllm.server.stop", "pxstore.backend.provision_vllm",
                "vllm.instances.remove", "llm.summarize", "llm.classify"):
        assert bad not in got


def test_prefer_moves_a_generator_to_the_front():
    assert route_candidates(REGISTRY, prefer="vllm.generate") == ["vllm.generate", "llm.generate"]


def test_prefer_cannot_smuggle_in_a_non_generator():
    assert route_candidates(REGISTRY, prefer="vllm.server.stop") == ["llm.generate", "vllm.generate"]
    assert route_candidates(REGISTRY, prefer="llm.route") == ["llm.generate", "vllm.generate"]


def test_only_registered_generators_are_returned():
    assert route_candidates(["llm.generate", "echo"]) == ["llm.generate"]
    assert route_candidates([]) == []


def test_generators_constant_is_what_the_docstring_promises():
    assert GENERATORS == ("llm.generate", "vllm.generate")
