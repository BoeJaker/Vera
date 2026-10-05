"""The first n characters of a value's JSON, without encoding the rest.

Pure (stdlib only) so it is testable from a worktree as `vera.json_head_core`.

The capability wrapper keeps a 4 KB preview of every result (the Redis result cache, the activity preview) and made
it with json.dumps(result)[:4096] - encoding the WHOLE result on the event loop to keep its first 4 KB. py-spy on
prod's main thread (2026-10-05) put that one line at 114 of 663 samples during a single slow chat call: graph
snapshots, sandbox lists and the like were encoded in full on every call.
"""
from __future__ import annotations

import json
from typing import Any

_ENCODER = json.JSONEncoder()


def json_head(value: Any, limit: int) -> str:
    """Exactly json.dumps(value)[:limit] - same text, same errors - but stops encoding once `limit` characters exist."""
    limit = max(0, int(limit))
    out = []
    size = 0
    for chunk in _ENCODER.iterencode(value):
        out.append(chunk)
        size += len(chunk)
        if size >= limit:
            break
    return "".join(out)[:limit]
