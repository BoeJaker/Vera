"""Choosing a driveable sandbox when the caller did not name one.

Census run 16 (2026-08-30), goal ``build-browser-verified`` - "create form.html
and verify in a browser that the validation error actually appears". Nine
``operator.run`` calls; six of them never opened a browser at all, each dying in
under a second with::

    sandbox ensure: primary sandbox is occupied by another branch

The primary sandbox is a deliberate singleton - one owner at a time, and the
guard that says so is correct. But ``ensure_target`` treated it as the ONLY way
to obtain a sandbox: a caller who said "I need a browser" without naming a
container got the primary or got nothing. On an estate that routinely runs a
dozen agent branches the primary is almost never free, so browser verification
was effectively unavailable to loops - not degraded, unavailable - while a
perfectly good pinned sandbox sat running and idle the whole time.

The rule here is about SAFETY, not availability, because a browser session is
not a read-only observer: the operator clicks things. So:

  * An EXPLICIT branch/name is matched exactly or not at all. Silently
    substituting a different container for a named one would let the operator
    click around a container the caller never asked for.
  * With nothing named, a ``pinned`` sandbox is preferred. Pinned means standing
    shared infrastructure that is deliberately kept up (the bleeding-edge
    mirror), which is exactly what a "just give me a browser" request wants.
  * Otherwise an UNOWNED sandbox will do.
  * A sandbox that is owned but not pinned is another agent's working
    container and is never chosen automatically.
  * Paused, stopped and url-less sandboxes are never chosen: the operator needs
    something it can actually reach, and unpausing someone else's container is
    a side effect, not a fallback.

Pure: no imports from the app, no I/O. ``ensure_target`` supplies the list.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable, Optional


def _s(value: Any) -> str:
    return str(value or "").strip()


def is_driveable(sandbox: Mapping[str, Any]) -> bool:
    """True when a browser could actually be pointed at this sandbox now."""
    if not isinstance(sandbox, Mapping):
        return False
    return (bool(sandbox.get("running"))
            and not bool(sandbox.get("paused"))
            and bool(_s(sandbox.get("url"))))


def _matches(sandbox: Mapping[str, Any], branch: str, name: str) -> bool:
    return ((bool(branch) and _s(sandbox.get("branch")) == branch)
            or (bool(name) and _s(sandbox.get("name")) == name))


def pick_sandbox(sandboxes: Iterable[Mapping[str, Any]], *,
                 branch: str = "", name: str = "") -> Optional[dict]:
    """The sandbox to drive, or ``None`` when none is safe to choose.

    An explicit ``branch``/``name`` is honoured exactly; with neither, falls back
    to standing shared infrastructure. Ties break on name so the choice is
    stable across calls rather than dependent on listing order.
    """
    branch, name = _s(branch), _s(name)
    live = [dict(s) for s in (sandboxes or []) if is_driveable(s)]
    live.sort(key=lambda s: _s(s.get("name")))

    if branch or name:
        for s in live:
            if _matches(s, branch, name):
                return s
        return None

    for s in live:
        if s.get("pinned"):
            return s
    for s in live:
        if not _s(s.get("owner")):
            return s
    return None


def substitution_note(sandbox: Mapping[str, Any], primary_error: str) -> str:
    """Why the operator is looking at this container and not the primary.

    Surfaced on the resolved target so a run that quietly landed somewhere other
    than the default is visible in the trace instead of being inferred later.
    """
    return (f"the primary sandbox was unavailable ({_s(primary_error)}), so this "
            f"run is driving the standing sandbox "
            f"'{_s(sandbox.get('name')) or '?'}' "
            f"(branch {_s(sandbox.get('branch')) or '?'}) at "
            f"{_s(sandbox.get('url'))} instead")
