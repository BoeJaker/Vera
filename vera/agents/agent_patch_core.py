"""A partial update of an agent: change the named fields, keep everything else.

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.agents.agent_patch_core`.

Why it exists: `agent.update` rebuilds the whole agent from its own parameters - every field it is not sent is reset
to the parameter's default, and the fields it does not take at all (two_tier, two_tier_decider, ...) are reset on
every save. A form that edits three fields could not save through it without wiping the rest. A patch reads the stored
agent, overlays only what was edited, coerces each value to the field's type, and refuses what may not change.
"""
from __future__ import annotations

import dataclasses
import typing
from typing import Any, Dict, Iterable, List, Tuple

# never changed by a patch: who the agent is and where it came from
PROTECTED = frozenset({"id", "created_at", "author", "updated_at"})

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def field_types(cls: Any) -> Dict[str, Any]:
    """{field name: annotation} for a dataclass, protected fields left out."""
    hints = {}
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        pass
    out = {}
    for f in dataclasses.fields(cls):
        if f.name in PROTECTED:
            continue
        out[f.name] = hints.get(f.name, f.type)
    return out


def _kind(t: Any) -> str:
    s = str(t).lower()
    if t is bool or s in ("bool", "<class 'bool'>"):
        return "bool"
    if t is int or s in ("int", "<class 'int'>"):
        return "int"
    if t is float or s in ("float", "<class 'float'>"):
        return "float"
    if "list" in s:
        return "list"
    if "dict" in s:
        return "dict"
    return "str"


def coerce(value: Any, typ: Any) -> Any:
    """A form's value as the field's type. Raises ValueError when it cannot be."""
    k = _kind(typ)
    if k == "bool":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        raise ValueError(f"not a yes/no value: {value!r}")
    if k == "int":
        if isinstance(value, bool):
            raise ValueError("a yes/no is not a number")
        return int(float(value)) if str(value).strip() != "" else 0
    if k == "float":
        if isinstance(value, bool):
            raise ValueError("a yes/no is not a number")
        return float(value) if str(value).strip() != "" else 0.0
    if k == "list":
        if isinstance(value, (list, tuple)):
            return [x for x in value if x is not None and str(x).strip() != ""]
        return [s.strip() for s in str(value or "").replace("\n", ",").split(",") if s.strip()]
    if k == "dict":
        if isinstance(value, dict):
            return value
        raise ValueError("expected an object")
    return "" if value is None else str(value)


def apply_patch(current: Dict[str, Any], fields: Dict[str, Any], types: Dict[str, Any]
                ) -> Tuple[Dict[str, Any], List[str], List[Dict[str, str]]]:
    """(the new record, the fields that changed, the fields refused with why). `current` is not modified."""
    new = dict(current or {})
    changed: List[str] = []
    rejected: List[Dict[str, str]] = []
    for k, v in (fields or {}).items():
        if k in PROTECTED:
            rejected.append({"field": k, "why": "may not be changed"})
            continue
        if k not in types:
            rejected.append({"field": k, "why": "not a field of this record"})
            continue
        try:
            cv = coerce(v, types[k])
        except (ValueError, TypeError) as e:
            rejected.append({"field": k, "why": str(e)})
            continue
        if new.get(k) != cv:
            new[k] = cv
            changed.append(k)
    return new, changed, rejected
