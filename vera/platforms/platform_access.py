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


def declare(registry: Dict[str, Any], key: str, who: str, *, label: str = "", why: str = "") -> bool:
    """Tell the Platforms page that `who` reads credential `key`. False when the platforms module is not loaded."""
    g = _platform(registry)
    fn = g.get("declare_secret_use") if g else None
    if fn is None:
        return False
    fn(key, who, label=label, why=why)
    return True
