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


def pick_reserved(sandboxes: Iterable[Mapping[str, Any]]) -> Optional[dict]:
    """The RESERVED standing container, or None if no reservation is up.

    A reservation is a pinned, driveable sandbox. Pinned is the whole signal:
    it means somebody deliberately keeps this container standing as shared
    infrastructure, which is precisely what "I just need a browser" wants.

    Deliberately NARROWER than pick_sandbox's fallback chain, which will also
    take an unowned unpinned container. That is a reasonable last resort but it
    is not a reservation - an unpinned container can be reaped or paused out
    from under a run mid-click.
    """
    live = [dict(s) for s in (sandboxes or []) if is_driveable(s)]
    live.sort(key=lambda s: _s(s.get("name")))
    for s in live:
        if s.get("pinned"):
            return s
    return None


def reservation_note(sandbox: Mapping[str, Any]) -> str:
    """Why this run is on the reserved container - said positively.

    substitution_note explains a CONSOLATION ("the primary was unavailable, so
    ..."). This is not that: the reserved container is the intended home for a
    browser step, so the trace should not read like a degradation.
    """
    return (f"browser step running on the reserved standing sandbox "
            f"'{_s(sandbox.get('name')) or '?'}' "
            f"(branch {_s(sandbox.get('branch')) or '?'}) at "
            f"{_s(sandbox.get('url'))}; the primary singleton was not touched")


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


def decline_reason(sandboxes, *, branch: str = "", name: str = "") -> str:
    """Why nothing was chosen - the line that was missing.

    Census run 19: an `operator.run` died in 2.4s on "primary sandbox is
    occupied by another branch" while a pinned, running, driveable sandbox was
    registered the whole time. Nothing logged the refusal, so the cause could
    not be told apart from the list simply being empty. A guard that declines
    silently is a guard nobody can debug.
    """
    items = list(sandboxes or [])
    if not items:
        return "the sandbox list was empty (or could not be read)"
    live = [s for s in items if is_driveable(s)]
    if not live:
        states = ", ".join(
            "%s(running=%s paused=%s url=%s)" % (
                _s(s.get("name")) or "?", s.get("running"), s.get("paused"),
                "yes" if _s(s.get("url")) else "no")
            for s in items[:4])
        return f"none of {len(items)} sandbox(es) was driveable: {states}"
    if branch or name:
        return (f"no sandbox matches the requested "
                f"{'branch ' + branch if branch else 'name ' + name!r}; "
                f"{len(live)} other(s) are live")
    owned = [_s(s.get("name")) for s in live if _s(s.get("owner")) and not s.get("pinned")]
    if owned:
        return (f"{len(live)} live sandbox(es), but all are another agent's "
                f"working containers (owned and unpinned): {', '.join(owned[:4])}")
    return "no safe candidate among %d live sandbox(es)" % len(live)
