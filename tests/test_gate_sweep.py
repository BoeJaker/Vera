"""Ollama-gate leaked-lease sweep — the safety-critical decision (2026-08-17 fix).

A gate lease leaked by a dead LOCAL process wedges the GPU for its full 30-min TTL.
The startup sweep clears such orphans — but it MUST NEVER clear a peer node's live
lease (that would double-book the single GPU slot -> two concurrent generations ->
VRAM thrash/crash). These pin that decision. Pure (injected pid-liveness, no Redis,
no os.kill), so it runs in the critical gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera import ollama_gate as G  # noqa: E402


def test_parse_owner_shapes():
    assert G.parse_owner("LLM:1046580:9a6443c4") == ("LLM", 1046580)
    assert G.parse_owner("host:42") == ("host", 42)
    assert G.parse_owner("weird") == ("weird", None)          # no pid segment
    assert G.parse_owner("host:notanint:x") == ("host", None)
    assert G.parse_owner("") == ("", None)


def test_reapable_only_local_and_dead():
    dead = lambda pid: False
    alive = lambda pid: True
    # local host + dead pid -> safe to clear (the leak we want gone)
    assert G.is_reapable_local_lease("LLM:100:abc", "LLM", dead) is True
    # local host + ALIVE pid -> must NOT clear (it's a live local generation)
    assert G.is_reapable_local_lease("LLM:100:abc", "LLM", alive) is False


def test_never_clears_a_peer_hosts_lease():
    dead = lambda pid: False   # even if the pid looks dead in OUR namespace
    # a different host's lease is untouchable — we can't verify its pid, and clearing
    # a peer's live slot double-books the GPU
    assert G.is_reapable_local_lease("OTHERHOST:100:abc", "LLM", dead) is False
    assert G.is_reapable_local_lease("sandbox-abc123:100:abc", "LLM", dead) is False


def test_malformed_owner_never_reapable():
    dead = lambda pid: False
    assert G.is_reapable_local_lease("LLM:notapid", "LLM", dead) is False
    assert G.is_reapable_local_lease("", "LLM", dead) is False
    assert G.is_reapable_local_lease("LLM", "LLM", dead) is False
