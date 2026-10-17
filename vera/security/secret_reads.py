"""In-process reads of OpenBao KV secrets, for Vera's own modules only.

`secstore.kv.get` is a capability, and every capability is reachable over the
unauthenticated `/mcp/call` and its HTTP route. Since the 11 Oct credential move
it could read every module credential (`vera/named/platform/*`) and the Redis
ACL passwords, so it now returns only metadata. Modules that genuinely need a
value (Redis ACL bootstrap, PKI records) read it here: a plain function with no
capability, no route and no event, so no caller outside the process can reach it.

The provisioning module registers the reader at import (`set_reader`).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

_READER: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None


def set_reader(fn: Callable[[str], Awaitable[Dict[str, Any]]]) -> None:
    global _READER
    _READER = fn


async def read(path: str) -> Dict[str, Any]:
    """{ok: True, data: {...}} | {ok: False} (missing) | {error: ...}."""
    if _READER is None:
        return {"error": "secret reader unavailable (provisioning module not loaded)"}
    return await _READER(path)
