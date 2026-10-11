"""Reading a Platforms credential (platform.secrets - the one master store) from another module, in-process.

The owner, 2026-10-11: "there is already a master api key system that should be used and not re-made". Keys live in
platform.secrets (OpenBao, never returned by a capability); a module that needs one - Brave search, a GitHub source
pull, an intel API source - reads it here, by key or by an "@secret:<key>" reference, and declares that it does so,
so the Platforms page lists it under "used by" (or as "wanted" before anyone has set it).

The platforms module loads under its bare name, so it is reached through its registered capability (the same way
it reaches the secrets service) rather than imported a second time.
"""
from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

SECRET_REF = "@secret:"


def _platform(registry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fn = (registry.get("platform.secrets.list") or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(SECRET_REF)


async def secret_value(registry: Dict[str, Any], ref_or_key: str) -> str:
    """The credential's value, "" when the platforms module is not loaded, the key is unset or unreadable."""
    g = _platform(registry)
    fn = g.get("secret_value") if g else None
    if fn is None or not str(ref_or_key or "").strip():
        return ""
    try:
        return str(await fn(ref_or_key) or "")
    except Exception:
        return ""


async def resolve(registry: Dict[str, Any], value: Any, default_key: str = "") -> str:
    """A config value that may be a literal, an "@secret:<key>" reference, or empty (then `default_key`, the
    conventional record name, is tried)."""
    v = str(value or "").strip()
    if v and not is_ref(v):
        return v
    return await secret_value(registry, v or default_key)


async def connection_get(registry: Dict[str, Any], pid: str, path: str = "",
                         params: Optional[Dict[str, Any]] = None, **kw: Any) -> Dict[str, Any]:
    """GET `path` through API connection `pid` (Platforms > Connections): {response, shown_url, ms} or {error}. The
    connection supplies the base URL and the credential; the caller never sees the key."""
    g = _platform(registry)
    fn = g.get("connection_get") if g else None
    if fn is None:
        return {"error": "the platforms module is not loaded - API connections are unavailable"}
    return await fn(pid, path, params, **kw)


async def connection_info(registry: Dict[str, Any], pid: str, path: str = "") -> Dict[str, Any]:
    """What a connection is, without its credential: {id, label, base_url, auth, url (for `path`), enabled} or
    {error}."""
    g = _platform(registry)
    fn = g.get("connection_info") if g else None
    if fn is None:
        return {"error": "the platforms module is not loaded - API connections are unavailable"}
    return await fn(pid, path)


async def store_secret(registry: Dict[str, Any], key: str, value: str, label: str = "") -> bool:
    """Put a credential into the store under `key` (OpenBao; sealed inline where there is none). False when the
    platforms module is not loaded - the caller keeps what it had."""
    g = _platform(registry)
    fn = g.get("_store_secret") if g else None
    if fn is None or not key or not value:
        return False
    await fn(key, value, label)
    return True


# ── every module's credentials in the store (data-connections phases 3-4, owner 2026-10-11: "every credential
# should be in the store") ──────────────────────────────────────────────────────────────────────────────────
# A module keeps "@secret:<key>" in its own record where it used to keep a sealed value. On save it calls vault();
# on read open_value() (or open_value_sync() in sync code) - which still opens a value sealed the old way, so
# nothing breaks before a record is migrated. Each module registers an adopter that moves its old sealed values
# into the store; platform.secrets.adopt runs them, dry run first.

_ADOPTERS: Dict[str, Any] = {}


def key_for(*parts: Any) -> str:
    """A store key from parts: lowercase letters, digits and underscores, at most 64 characters."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", "_".join(str(p) for p in parts if p not in (None, "")).lower()).strip("_")[:64]


async def vault(registry: Dict[str, Any], key: str, value: str, label: str = "") -> str:
    """Put `value` in the store under `key` and return its "@secret:<key>" reference; "" when the store is not
    loaded (the caller then keeps its old way of sealing, so a save never fails for want of it)."""
    if not value:
        return ""
    try:
        ok = await store_secret(registry, key, value, label)
    except Exception:
        ok = False
    return SECRET_REF + key if ok else ""


async def open_value(registry: Dict[str, Any], raw: Any, legacy_open: Any = None) -> str:
    """A stored credential field: an "@secret:<key>" reference resolved from the store; anything else opened the
    old way by `legacy_open` (e.g. vsecrets.open_secret), or returned as it is."""
    if is_ref(raw):
        return await secret_value(registry, raw)
    if raw and legacy_open is not None:
        try:
            return str(legacy_open(raw) or "")
        except Exception:
            return ""
    return str(raw or "")


def open_value_sync(registry: Dict[str, Any], raw: Any, legacy_open: Any = None) -> str:
    """open_value for sync code: a reference is read from the platforms module's in-process copy (filled at
    startup and on every save); "" when that copy does not have it yet."""
    if is_ref(raw):
        g = _platform(registry)
        fn = g.get("secret_value_sync") if g else None
        return str(fn(raw) or "") if fn is not None else ""
    if raw and legacy_open is not None:
        try:
            return str(legacy_open(raw) or "")
        except Exception:
            return ""
    return str(raw or "")


def _vsecrets():
    try:
        from Vera.vera.security import secrets as s
    except ImportError:
        from vera.security import secrets as s
    return s


async def keep(registry: Dict[str, Any], key: str, value: Any, label: str = "") -> Any:
    """What a save stores for a secret field: a reference, or a value already sealed the old way, stays as it is
    (the adopter moves those); a new plain value goes to the store and its reference is returned - sealed the old
    way only when the store is not loaded."""
    if not value or not isinstance(value, str) or is_ref(value):
        return value
    s = _vsecrets()
    if s.is_sealed(value):
        return value
    return (await vault(registry, key, value, label)) or s.seal(value)


async def adopt_value(registry: Dict[str, Any], key: str, value: Any, label: str = "") -> Any:
    """For an adopter: an old sealed (or plain) value moved into the store -> its reference; unchanged when it is
    already a reference, empty, or the store is not loaded."""
    if not value or not isinstance(value, str) or is_ref(value):
        return value
    plain = _vsecrets().open_secret(value)
    return (await vault(registry, key, plain, label)) or value


def needs_adopting(value: Any) -> bool:
    return bool(value) and isinstance(value, str) and not is_ref(value)


def open_any_sync(registry: Dict[str, Any], raw: Any) -> str:
    """open_value_sync with the old opener (vsecrets.open_secret) built in."""
    return open_value_sync(registry, raw, _vsecrets().open_secret)


async def open_any(registry: Dict[str, Any], raw: Any) -> str:
    """open_value with the old opener (vsecrets.open_secret) built in."""
    return await open_value(registry, raw, _vsecrets().open_secret)


async def target_fields(registry: Dict[str, Any], pid: str) -> Dict[str, Any]:
    """A Platforms target's fields with its references resolved (in-process only - they can hold a key); {} when
    there is no such target or the platforms module is not loaded."""
    g = _platform(registry)
    fn = g.get("_resolved") if g else None
    if fn is None:
        return {}
    try:
        got = await fn(pid)
    except Exception:
        return {}
    return dict((got or {}).get("resolved") or {})


async def prime(registry: Dict[str, Any], *values: Any) -> None:
    """Make sure the in-process copy holds these references before sync code opens them (an async getter calls
    this, then the module's sync opener works even just after a restart)."""
    for v in values:
        if is_ref(v):
            await secret_value(registry, v)


def _dig(rec: Dict[str, Any], path: str):
    cur: Any = rec
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur, parts[-1]


async def adopt_json_hash(registry: Dict[str, Any], redis: Any, hash_key: str, fields: Any, prefix: str,
                          dry_run: bool, label: str = "") -> list:
    """The adopter for the usual layout - a Redis hash of JSON records with secret fields (dotted paths allowed):
    each old sealed value moves to the store as <prefix>_<record>_<field>."""
    import json
    if redis is None:
        return []
    moved = []
    raw = await redis.hgetall(hash_key)
    for k, v in (raw or {}).items():
        rid = k.decode() if isinstance(k, bytes) else str(k)
        try:
            rec = json.loads(v.decode() if isinstance(v, bytes) else v)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        changed = False
        for f in fields:
            parent, leaf = _dig(rec, f)
            val = parent.get(leaf) if isinstance(parent, dict) else None
            if not needs_adopting(val):
                continue
            key = key_for(prefix, rid, f)
            moved.append({"record": rid, "field": f, "key": key})
            if dry_run:
                continue
            new = await adopt_value(registry, key, val, f"{label or prefix} {rec.get('label') or rid} {f}")
            if new != val:
                parent[leaf] = new
                changed = True
        if changed:
            await redis.hset(hash_key, rid, json.dumps(rec))
    return moved


def register_adopter(name: str, fn: Any) -> None:
    """`fn(dry_run: bool) -> [{record, field, key}]` moves this module's old sealed values into the store."""
    _ADOPTERS[str(name)] = fn


def declare(registry: Dict[str, Any], key: str, who: str, *, label: str = "", why: str = "") -> bool:
    """Tell the Platforms page that `who` reads credential `key`. False when the platforms module is not loaded."""
    g = _platform(registry)
    fn = g.get("declare_secret_use") if g else None
    if fn is None:
        return False
    fn(key, who, label=label, why=why)
    return True
