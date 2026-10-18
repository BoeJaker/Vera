"""Builders for a capability's `presentation` declaration (DA-01b, DESK-SPINE-DESIGN.md §1.3).

    from vera import presentation_core as P
    @capability("markets.quotes", contract={"effects": ["read", "network"],
                                            "presentation": P.table("quotes", name="symbol", value="last", refresh=30)})

One builder per kind in `capability_contract_core.PRESENTATION_KINDS`. The first argument is the path to the kind's
content (rows, body, value, points, ...); keyword arguments named after the kind's other map keys are map paths; the
rest are the declaration's options:

    views=[...]                 the views a person may switch to (default: the kind's own set)
    title="Quotes · {symbols}"  a template over the call's arguments
    empty="No quotes"           what an empty answer says
    refresh=30 | {"every_s": 30}            an app window re-reads on this beat
    live="markets.tick" | {"event": ..., "key": ...}   the event that updates it in place
    detail="markets.chart" | {"cap": ..., "args": {...}}   what a row opens

A builder never raises - a bad declaration must not stop a capability module loading. `project_contract` validates
it and the manifest says what is wrong (status "invalid" with its problems); `cap.contract.lint` lists it.
"""

from __future__ import annotations

from typing import Any

from .capability_contract_core import PRESENTATION_KINDS

_OPTIONS = ("views", "title", "empty", "refresh", "live", "detail", "form", "draw")   # form + draw: DA-01d
# the key the first argument fills, where it is not the kind's first map key
_CONTENT = {"terminal": "stdout", "media": "url", "entities": "entities", "receipt": "ok"}


def _options(out: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
    for key in _OPTIONS:
        if key not in opts or opts[key] is None:
            continue
        value = opts[key]
        if key == "refresh" and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = {"every_s": value}
        elif key == "live" and isinstance(value, str):
            value = {"event": value}
        elif key == "detail" and isinstance(value, str):
            value = {"cap": value}
        elif key == "views" and isinstance(value, tuple):
            value = list(value)
        out[key] = value
    return out


def _builder(kind: str):
    keys = PRESENTATION_KINDS[kind]["keys"]
    content = _CONTENT.get(kind, keys[0])

    def build(path: str | None = None, /, **kw: Any) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        if path is not None:
            mapping[content] = path
        for key in keys:
            if key in kw and kw[key] is not None:
                mapping[key] = kw.pop(key)
        out: dict[str, Any] = {"kind": kind, "map": mapping}
        # anything left that is neither a map key nor an option stays in the map, so validation names it
        for key in [k for k in kw if k not in _OPTIONS]:
            mapping[key] = kw.pop(key)
        return _options(out, kw)

    build.__name__ = kind
    build.__qualname__ = kind
    build.__doc__ = (f"presentation kind '{kind}': first argument = map.{content}; map keys {', '.join(keys)}; "
                     f"options {', '.join(_OPTIONS)}")
    return build


table = _builder("table")
records = _builder("records")
reading = _builder("reading")
document = _builder("document")
status = _builder("status")
metric = _builder("metric")
series = _builder("series")
graph = _builder("graph")
log = _builder("log")
progress = _builder("progress")
terminal = _builder("terminal")
media = _builder("media")
code = _builder("code")
diff = _builder("diff")
kv = _builder("kv")
calendar = _builder("calendar")
timeline = _builder("timeline")    # DA-01e: P.timeline("runs", name="title", start="started_at", end="finished_at")
matrix = _builder("matrix")        # DA-01e: P.matrix("nodes") - {row: {column: value}}; row=, column=, value=, entries=
parts = _builder("parts")          # DA-01e: P.parts("by_type", name="label", value="size")
entities = _builder("entities")
receipt = _builder("receipt")
job = _builder("job")


def composite(*parts: dict[str, Any], **opts: Any) -> dict[str, Any]:
    """Several kinds in one card (tabs or a split): each part is a builder's result, its title kept."""
    out: dict[str, Any] = {"kind": "composite",
                           "parts": [{k: v for k, v in p.items() if k in ("kind", "map", "title")} if isinstance(p, dict) else p
                                     for p in parts]}
    return _options(out, opts)


BUILDERS = {kind: globals()[kind] for kind in PRESENTATION_KINDS}
