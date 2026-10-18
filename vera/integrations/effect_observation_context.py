"""Async-local, payload-free attribution for external-effect observations."""

from __future__ import annotations

import contextlib
import contextvars
import re
from collections.abc import Iterator


_SOURCE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CURRENT_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "vera_effect_observation_source", default="")


def normalise_source(value: str) -> str:
    source = str(value or "").strip().lower()
    if source and not _SOURCE.fullmatch(source):
        raise ValueError("effect observation source must be an opaque identifier")
    return source


def current_source() -> str:
    return _CURRENT_SOURCE.get()


@contextlib.contextmanager
def attributed(source: str) -> Iterator[None]:
    """Attribute nested observations for this async context, then restore it."""
    token = _CURRENT_SOURCE.set(normalise_source(source))
    try:
        yield
    finally:
        _CURRENT_SOURCE.reset(token)
