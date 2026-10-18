"""memory.seek's result cache (four-minds FM-7.1; §10: a 10-minute TTL, invalidated by fabric ingests).

A loop step and the chat turns around it often seek the same thing twice within minutes: each seek embeds the query
and searches every backend (seconds at p95, FM-0.4d). The cache keeps a seek's answer for TTL_S, keyed by every
argument that shapes it plus the fabric's GENERATION - a Redis counter the fabric bumps on every ingest, so new data is
never hidden behind a cached answer. Session-memory hits (include_memory) can lag by at most TTL_S.

Pure: keys only.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PREFIX = "vera:memory:seek:"
GEN_KEY = "vera:fabric:generation"
TTL_S = 600


def key(query: str, scope: str, since: str, k: int, max_chars: int, include_memory: bool, generation: Any) -> str:
    """One cache key per (normalised query, every shaping argument, fabric generation)."""
    norm = " ".join(str(query or "").lower().split())
    raw = json.dumps([norm, str(scope or ""), str(since or ""), int(k), int(max_chars), bool(include_memory),
                      str(generation or 0)], separators=(",", ":"))
    return PREFIX + hashlib.sha1(raw.encode("utf-8")).hexdigest()
