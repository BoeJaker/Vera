"""Which capabilities ``llm.route`` may try, and in what order.

``llm.route`` sends a plain prompt to "the best available LLM capability". It used
to try every registry key containing "llm" in insertion order and return the first
that did not raise, which swept in ``vllm.server.stop``, provisioning caps,
``llm.summarize`` / ``llm.classify`` (wrong task, silently) and ``llm.route``
itself (recursion). Only plain text generators take a bare ``prompt`` and return
a generation, so only they are candidates.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

# Plain text generators, in default preference order. Anything not listed here is
# never tried by llm.route, whatever its name contains.
GENERATORS = ("llm.generate", "vllm.generate")


def route_candidates(registered: Iterable[str], prefer: Optional[str] = None) -> List[str]:
    """Return the generators llm.route may try, in order.

    Only names in ``GENERATORS`` that are actually registered are returned.
    ``prefer`` moves one of them to the front; an unknown or non-generator
    ``prefer`` is ignored rather than trusted.
    """
    have = set(registered)
    order = [name for name in GENERATORS if name in have]
    if prefer and prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    return order
