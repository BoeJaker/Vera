"""App manifests - what a panel offers as a window on the desk (DESK-SPINE-DESIGN.md section 4.1, DA-05a).

A manifest names one app: the capability it reads, the presentation kind it is drawn as, the actions it offers, what a
row opens. Manifests live in ``vera/apps/<panel>.json`` - one file per panel, an array. This module only reads and
checks them (pure functions, no registry import): ``ui.apps.lint`` / ``ui.apps.list`` / ``ui.apps.get`` call it with the
live registry's capability names, and the tests call it with none.

A problem is ``{file, id, level: "error"|"warning", code, message}``. Errors make an app unusable (no read, a capability
that does not exist, a kind outside the vocabulary); warnings are worth fixing (an unknown panel, a view the kind does not
have, a refresh faster than 5 s).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Optional

try:  # the namespace the server loads, else the plain one the tests use
    from Vera.vera.capability_contract_core import PRESENTATION_KINDS
except Exception:  # pragma: no cover - import path depends on the runner
    from vera.capability_contract_core import PRESENTATION_KINDS

APPS_DIR = os.path.dirname(os.path.abspath(__file__))
ID_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")   # panel ids have dashes (system-monitor)
WHERE = ("header", "row", "menu")
PATCH = ("row", "replace", "append")
EFFECTS = ("remove", "create", "update")   # what an action does to its rows (DA-08a)
SIZES = ("s", "m", "l", "xl")
SETTING_TYPES = ("multi", "select", "toggle", "number", "text")
TEMPLATE = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def _p(out: list, file: str, app: str, level: str, code: str, message: str) -> None:
    out.append({"file": file, "id": app, "level": level, "code": code, "message": message})


def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def needs_args(m: dict) -> list:
    """the arguments the person must give before the app can read: {arg.x} templates in its read args"""
    reads = m.get("read") if isinstance(m.get("read"), list) else [m.get("read")]
    out = []
    for r in reads:
        if _is_obj(r) and _is_obj(r.get("args")):
            for v in r["args"].values():
                for name in TEMPLATE.findall(str(v)):
                    if name.startswith("arg.") and name[4:] not in out:
                        out.append(name[4:])
    return out


def check_manifest(m: Any, file: str = "", caps: Optional[Iterable[str]] = None, panels: Optional[Iterable[str]] = None) -> list:
    """the problems of one manifest; caps / panels, when given, are the names that exist"""
    caps = set(caps) if caps is not None else None
    panels = set(panels) if panels is not None else None
    out: list = []
    if not _is_obj(m):
        _p(out, file, "", "error", "manifest.type", "a manifest is an object")
        return out
    app = str(m.get("id") or "")
    if not ID_RE.match(app):
        _p(out, file, app, "error", "id.invalid", "id must be dotted lower case, like panel.name")
    panel = m.get("panel")
    if not isinstance(panel, str) or not panel:
        _p(out, file, app, "error", "panel.missing", "panel is required")
    elif panels is not None and panel not in panels:
        _p(out, file, app, "warning", "panel.unknown", "no registered panel named %r" % panel)
    title = m.get("title")
    if not isinstance(title, str) or not title.strip():
        _p(out, file, app, "error", "title.missing", "title is required")
    elif len(title) > 60:
        _p(out, file, app, "warning", "title.long", "title over 60 characters")
    if m.get("summary") is not None and (not isinstance(m["summary"], str) or len(m["summary"]) > 200):
        _p(out, file, app, "warning", "summary.invalid", "summary is a sentence of at most 200 characters")
    # the view: a presentation kind, views of that kind
    view = m.get("view")
    kind = view.get("kind") if _is_obj(view) else None
    if kind not in PRESENTATION_KINDS or kind in ("receipt", "job"):
        _p(out, file, app, "error", "view.kind", "view.kind must be a presentation kind that draws (got %r)" % kind)
    elif _is_obj(view) and view.get("views") is not None:
        vs = view["views"]
        if not isinstance(vs, list) or not all(isinstance(x, str) for x in vs):
            _p(out, file, app, "error", "view.views", "view.views is a list of names")
        else:
            odd = [x for x in vs if x not in PRESENTATION_KINDS[kind]["views"]]
            if odd:
                _p(out, file, app, "warning", "view.views.unknown", "views %s are not views of %s (it has %s)" % (odd, kind, list(PRESENTATION_KINDS[kind]["views"])))
    # read: one, or a list with `as` names (a composite)
    read = m.get("read")
    reads = read if isinstance(read, list) else [read]
    if read is None or (isinstance(read, list) and not read):
        _p(out, file, app, "error", "read.missing", "read is required: the capability the window reads")
    seen_as = set()
    for i, r in enumerate(reads if read is not None else []):
        where = "read" if not isinstance(read, list) else "read[%d]" % i
        if not _is_obj(r) or not isinstance(r.get("cap"), str) or not r.get("cap"):
            _p(out, file, app, "error", "read.cap", "%s.cap is required" % where)
            continue
        if caps is not None and r["cap"] not in caps:
            _p(out, file, app, "error", "read.cap.unknown", "%s.cap %r is not a capability" % (where, r["cap"]))
        if r.get("args") is not None and not _is_obj(r["args"]):
            _p(out, file, app, "error", "read.args", "%s.args is an object" % where)
        if isinstance(read, list):
            a = r.get("as")
            if not isinstance(a, str) or not a:
                _p(out, file, app, "error", "read.as", "%s needs `as` - a composite names its parts" % where)
            elif a in seen_as:
                _p(out, file, app, "error", "read.as.duplicate", "%s.as %r is used twice" % (where, a))
            else:
                seen_as.add(a)
        ref = r.get("refresh")
        if ref is not None:
            every = ref.get("every_s") if _is_obj(ref) else None
            if not isinstance(every, (int, float)) or every <= 0:
                _p(out, file, app, "error", "read.refresh", "%s.refresh.every_s is a positive number of seconds" % where)
            elif every < 5:
                _p(out, file, app, "warning", "read.refresh.fast", "%s refreshes every %ss - at least 5 s" % (where, every))
    live = m.get("live")
    if live is not None:
        if not _is_obj(live) or not isinstance(live.get("event"), str) or not live.get("event"):
            _p(out, file, app, "error", "live.event", "live.event names the event that updates the window")
        elif live.get("patch") is not None and live["patch"] not in PATCH:
            _p(out, file, app, "error", "live.patch", "live.patch is one of %s" % (PATCH,))
        elif live.get("patch") == "row" and not live.get("key"):
            _p(out, file, app, "error", "live.key", "a row patch needs live.key - the field that names the row")
    # actions
    acts = m.get("actions")
    if acts is not None:
        if not isinstance(acts, list):
            _p(out, file, app, "error", "actions.type", "actions is a list")
            acts = []
        ids = set()
        for i, a in enumerate(acts):
            w = "actions[%d]" % i
            if not _is_obj(a):
                _p(out, file, app, "error", "action.type", "%s is an object" % w)
                continue
            aid = a.get("id")
            if not isinstance(aid, str) or not aid:
                _p(out, file, app, "error", "action.id", "%s.id is required" % w)
            elif aid in ids:
                _p(out, file, app, "error", "action.id.duplicate", "%s.id %r is used twice" % (w, aid))
            else:
                ids.add(aid)
            if not isinstance(a.get("label"), str) or not a.get("label"):
                _p(out, file, app, "error", "action.label", "%s.label is required" % w)
            for key in ("cap", "undo"):
                c = a.get(key)
                if key == "cap" and (not isinstance(c, str) or not c):
                    _p(out, file, app, "error", "action.cap", "%s.cap is required" % w)
                elif c is not None and caps is not None and c not in caps:
                    _p(out, file, app, "error", "action.%s.unknown" % key, "%s.%s %r is not a capability" % (w, key, c))
            if a.get("where") is not None and a["where"] not in WHERE:
                _p(out, file, app, "error", "action.where", "%s.where is one of %s" % (w, WHERE))
            if a.get("form") is not None and a["form"] != "schema":
                _p(out, file, app, "error", "action.form", "%s.form is \"schema\" (a form from the capability's input) or absent" % w)
            if a.get("args") is not None and not _is_obj(a["args"]):
                _p(out, file, app, "error", "action.args", "%s.args is an object" % w)
            # what it does to its rows, and how a removal is put back (DA-08a): effect, undo_args (templates like args)
            if a.get("effect") is not None and a["effect"] not in EFFECTS:
                _p(out, file, app, "error", "action.effect", "%s.effect is one of %s" % (w, EFFECTS))
            if a.get("undo_args") is not None:
                if not _is_obj(a["undo_args"]):
                    _p(out, file, app, "error", "action.undo_args", "%s.undo_args is an object" % w)
                elif a.get("undo") is None:
                    _p(out, file, app, "warning", "action.undo_args.orphan", "%s.undo_args without an undo capability" % w)
            if a.get("where") == "row" and a.get("confirm") is None and a.get("undo") is None:
                _p(out, file, app, "warning", "action.unguarded", "%s acts on a row with neither confirm nor undo" % w)
    det = m.get("detail")
    if det is not None and not (_is_obj(det) and (isinstance(det.get("app"), str) or det.get("reader") is True)):
        _p(out, file, app, "error", "detail", "detail opens an app ({app, args}) or a reader ({reader: true})")
    rel = m.get("related")
    if rel is not None and (not isinstance(rel, list) or not all(isinstance(x, str) for x in rel)):
        _p(out, file, app, "error", "related", "related is a list of capability or app names")
    size = m.get("size")
    if size is not None:
        if not _is_obj(size) or any(size.get(k) is not None and size.get(k) not in SIZES for k in ("default", "min")):
            _p(out, file, app, "error", "size", "size.default / size.min are one of %s" % (SIZES,))
    sets = m.get("settings")
    if sets is not None:
        if not isinstance(sets, list):
            _p(out, file, app, "error", "settings.type", "settings is a list")
        else:
            for i, s in enumerate(sets):
                if not _is_obj(s) or not s.get("key") or not s.get("label") or s.get("type") not in SETTING_TYPES:
                    _p(out, file, app, "error", "setting", "settings[%d] needs key, label and a type of %s" % (i, SETTING_TYPES))
    return out


def load(directory: str = APPS_DIR) -> tuple:
    """(manifests, problems) from every <panel>.json in the directory; a manifest carries its file in `_file`"""
    apps: list = []
    problems: list = []
    if not os.path.isdir(directory):
        return apps, problems
    for f in sorted(os.listdir(directory)):
        if not f.endswith(".json"):
            continue
        p = os.path.join(directory, f)
        try:
            data = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError) as e:
            _p(problems, f, "", "error", "file.json", "not readable JSON: %s" % str(e)[:120])
            continue
        if not isinstance(data, list):
            _p(problems, f, "", "error", "file.type", "a manifest file is an array of apps")
            continue
        for m in data:
            if _is_obj(m):
                m = dict(m)
                m["_file"] = f
                if m.get("panel") and m["panel"] != f[:-5]:
                    _p(problems, f, str(m.get("id") or ""), "warning", "panel.file", "panel %r lives in %s.json" % (m["panel"], m["panel"]))
            apps.append(m)
    return apps, problems


def lint(directory: str = APPS_DIR, caps: Optional[Iterable[str]] = None, panels: Optional[Iterable[str]] = None) -> dict:
    apps, problems = load(directory)
    caps = list(caps) if caps is not None else None
    seen = {}
    for m in apps:
        f = m.get("_file", "") if _is_obj(m) else ""
        problems += check_manifest({k: v for k, v in m.items() if k != "_file"} if _is_obj(m) else m, f, caps, panels)
        if _is_obj(m) and m.get("id"):
            if m["id"] in seen:
                _p(problems, f, m["id"], "error", "id.duplicate", "id also in %s" % seen[m["id"]])
            else:
                seen[m["id"]] = f
    errors = sum(1 for p in problems if p["level"] == "error")
    return {"ok": errors == 0, "files": len({m.get("_file") for m in apps if _is_obj(m)}), "apps": len(apps),
            "errors": errors, "warnings": len(problems) - errors, "problems": problems}


def summary(m: dict) -> dict:
    """what ui.apps.list shows of an app"""
    view = m.get("view") if _is_obj(m.get("view")) else {}
    return {"id": m.get("id"), "panel": m.get("panel"), "title": m.get("title"), "summary": m.get("summary", ""),
            "icon": m.get("icon", ""), "kind": view.get("kind"), "needs_args": needs_args(m)}
