"""Wait a bounded time for a query embedding without throwing the work away.

A search that wants a query vector must not stall a planner or a context build
for as long as an embed can take - OLLAMA_EMBED_TIMEOUT is 300 s, because embeds
queue behind generation on the serialised embed node. But cutting the embed off
is worse than waiting less. asyncio.wait_for CANCELS what it wraps: the request
is aborted, the node's work is discarded, nothing reaches ollama_embed's result
cache, and the next identical query starts again from zero. On 2026-09-11
dag_store's 10 s cut dropped 3 of 9 query embeds, and the 6 that finished took
7.0-9.9 s.

So wait up to `wait_s`. If the vector is not back, return None - the caller
scores by keywords, as it always did on a miss - and let the embed run on, so a
late vector still lands in the cache for the next caller.
"""
import asyncio
from typing import Awaitable, Callable, List, Optional, Set

# asyncio holds only weak references to tasks; an abandoned embed with no strong
# reference can be collected before it finishes.
_RUNNING: Set["asyncio.Future"] = set()


def _finished(task: "asyncio.Future") -> None:
    _RUNNING.discard(task)
    if not task.cancelled():
        task.exception()          # retrieved, so asyncio does not warn about it


async def bounded_embed(embed: Callable[[], Awaitable[Optional[List[float]]]],
                        wait_s: float) -> Optional[List[float]]:
    """The vector if it arrives within `wait_s` seconds, else None. Never cancels
    the embed on timeout; a cancellation of the CALLER still propagates."""
    task = asyncio.ensure_future(embed())
    _RUNNING.add(task)
    task.add_done_callback(_finished)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None
