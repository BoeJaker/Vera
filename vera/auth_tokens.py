"""Caller tokens: the store, the per-request check and the evidence counters (B0.3b R2).

Records live in one Redis hash, `vera:auth:tokens` (field = token id, value = the record as JSON, digest only).
A request that carries a token is checked here (from the caller-context middleware). A valid token sets the
caller's kind and subject for the request and updates the record's last_used (at most once a minute per token).
Every request is also counted by (browser or program, peer class, token result) into a day hash
`vera:auth:token_evidence:<day>`, which says when R3 (refuse off-host program calls without a token) is safe:
"every non-browser call carries a token for 3 days".

R2 refuses nothing: an invalid token is counted and otherwise ignored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

try:
    from Vera.vera import auth_tokens_core as core
    from Vera.vera import caller_context_core as cc
except ImportError:  # tests that put the worktree root on sys.path
    from vera import auth_tokens_core as core  # type: ignore
    from vera import caller_context_core as cc  # type: ignore

log = logging.getLogger("vera.auth_tokens")

KEY = "vera:auth:tokens"
EVIDENCE_PREFIX = "vera:auth:token_evidence:"
CACHE_S = 30.0


def _text(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


class TokenService:
    def __init__(self, redis_getter: Callable[[], Any], clock: Callable[[], float] = time.time):
        self.redis_getter = redis_getter
        self.clock = clock
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._touched: Dict[str, float] = {}
        self.evidence = cc.UsageBuffer(interval=60.0)

    def _r(self):
        try:
            return self.redis_getter()
        except Exception:
            return None

    # ── store ────────────────────────────────────────────────────────────
    async def get(self, tid: str) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        hit = self._cache.get(tid)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        r = self._r()
        rec = None
        if r is not None:
            try:
                raw = await r.hget(KEY, tid)
                rec = json.loads(_text(raw)) if raw else None
            except Exception as e:
                log.debug("token get %s: %s", tid, e)
                return hit[1] if hit else None
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[tid] = (now, rec)
        return rec

    async def put(self, rec: Mapping[str, Any]) -> None:
        r = self._r()
        if r is None:
            raise RuntimeError("redis unavailable")
        await r.hset(KEY, rec["id"], json.dumps(dict(rec)))
        self._cache[rec["id"]] = (time.monotonic(), dict(rec))

    async def all(self) -> List[Dict[str, Any]]:
        r = self._r()
        if r is None:
            return []
        out = []
        for _, v in (await r.hgetall(KEY) or {}).items():
            try:
                out.append(json.loads(_text(v)))
            except ValueError:
                continue
        out.sort(key=lambda x: (-float(x.get("created") or 0)))
        return out

    async def revoke(self, tid: str) -> Optional[Dict[str, Any]]:
        self._cache.pop(tid, None)
        rec = await self.get(tid)
        if not rec:
            return None
        rec = dict(rec, revoked=True, revoked_at=self.clock())
        await self.put(rec)
        return rec

    # ── per request ──────────────────────────────────────────────────────
    async def check(self, headers: Mapping[str, str], peer: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(caller info, result) for one request; result is valid | invalid:<why> | none. Never raises."""
        try:
            token = core.from_headers(headers)
            if not token:
                result, info = "none", None
            else:
                parsed = core.parse(token)
                rec = await self.get(parsed[0]) if parsed else None
                ok, why = core.verify(token, rec, self.clock())
                if ok:
                    info = {"id": rec["id"], "kind": rec["kind"], "subject": rec["subject"],
                            "scopes": list(rec.get("scopes") or [])}
                    result = "valid"
                    await self._touch(rec, peer)
                else:
                    info, result = None, "invalid:" + why.replace(" ", "_")
            self.evidence.add(core.evidence_key(has_origin=bool(headers.get("origin")), peer=peer, result=result))
            if self.evidence.due(time.monotonic()):
                self.evidence.flushing = True
                asyncio.get_running_loop().create_task(self.flush_evidence())
            return info, result
        except Exception as e:  # observation must never break a request
            log.debug("token check: %s", e)
            return None, "error"

    async def _touch(self, rec: Mapping[str, Any], peer: str) -> None:
        now = self.clock()
        if now - self._touched.get(rec["id"], 0.0) < core.LAST_USED_EVERY_S:
            return
        self._touched[rec["id"]] = now
        try:
            await self.put(dict(rec, last_used=now, last_peer=str(peer or "")[:64]))
        except Exception as e:
            log.debug("token touch %s: %s", rec.get("id"), e)

    async def flush_evidence(self) -> None:
        batch = self.evidence.drain()
        try:
            r = self._r()
            if batch and r is not None:
                key = EVIDENCE_PREFIX + cc.day_key()[len(cc.KEY_PREFIX):]
                pipe = r.pipeline()
                for f, n in batch.items():
                    pipe.hincrby(key, f, n)
                pipe.expire(key, cc.RETENTION_S)
                await pipe.execute()
        except Exception as e:
            self.evidence.restore(batch)
            log.debug("token evidence flush: %s", e)
        finally:
            self.evidence.flushing = False

    async def evidence_summary(self, days: int = 3) -> Dict[str, Any]:
        """Counts per (browser|program, peer class, result) over the last `days` days, plus what R3a needs:
        program calls from off-host peers (lan, other) without a valid token."""
        r = self._r()
        total: Dict[str, int] = {}
        keys = [EVIDENCE_PREFIX + k[len(cc.KEY_PREFIX):] for k in cc.day_keys(days)]
        for k in keys:
            if r is None:
                break
            try:
                for f, n in (await r.hgetall(k) or {}).items():
                    total[_text(f)] = total.get(_text(f), 0) + int(_text(n))
            except Exception as e:
                log.debug("token evidence %s: %s", k, e)
        for f, n in self.evidence.snapshot().items():
            total[f] = total.get(f, 0) + n
        offhost_without = sum(n for f, n in total.items()
                              if f.startswith("program|") and f.split("|")[1] in ("lan", "other")
                              and not f.endswith("|valid"))
        return {"days": len(keys), "counts": dict(sorted(total.items(), key=lambda kv: -kv[1])),
                "offhost_program_without_valid_token": offhost_without}


_SERVICE: Optional[TokenService] = None


def service(redis_getter: Optional[Callable[[], Any]] = None) -> TokenService:
    """The process's token service (created on first use)."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = TokenService(redis_getter or (lambda: None))
    elif redis_getter is not None:
        _SERVICE.redis_getter = redis_getter
    return _SERVICE
