# -*- coding: utf-8 -*-
"""
A capability's ANSWER -> the widget record(s) that draw it (the widget review, round 2).

The python mirror of VeraWidget.fromCapResult (widget_element.js): the same rules in the same order, so an agent, a
directive or the catalogue can ask what an answer should be drawn as without a browser, and get the answer the page
would have drawn. Pure: no orchestrator import, no I/O.

  from_cap_result(cap, result, args=None, title='', size='l', max_records=3) -> [record, ...]   best first; [] for nothing

Every record is a full widget record: {id, form, title, source: cap, read: {args, map}, frame: {size}, data, why}.
The rules (see widget_element.js for the long form): error . terminal . media . diff . code . the capability's hint .
progress . events . series . files . level . prose . status . table . list . numbers . record . json . text.

The result forms it picks from (CAP_FORMS) are all in widget_record.FORMS.

present() below is the mirror of VeraWidget.present (a declared presentation first); its timeline, matrix and parts kinds
(DA-01e) read onto the gantt, the heat map / status matrix and the donut.
"""
from __future__ import annotations

import calendar
import math
import re
from typing import Any, Dict, List, Optional

CAP_FORMS = ("kv", "table", "list", "json", "log", "terminal", "diff", "code", "progress", "hero", "trace", "area",
             "column", "status", "files", "media", "error", "markdown", "reading")

# the capability's own hint: what the dashboard already reads each of these as (kept in step with CAP_HINTS in the element)
# The capabilities' own widget hints are declarations on them now (DA-01d): Contract v2 presentation {kind, form, map,
# draw}, read first by present(). This table is for an answer no capability declares a drawing for - empty today.
CAP_HINTS: Dict[str, Dict[str, Any]] = {
}
#: the CI forms draw their answer whole (ci_view_core's payload): no rows companion beside them
CI_FORMS = ("status-matrix", "race-green", "test-grid", "ci-board", "run-track", "run-compare", "ci-pulse", "ci-fleet", "ci-run",
            "census-commits", "loop-perf", "census-live", "census-timeline", "trend-layers")
_ROW_KEYS = ("data", "result", "items", "rows", "results", "entries", "events", "points", "series", "values")
_TIME = ("t", "ts", "time", "when", "at", "timestamp", "created_at", "started_at", "hour", "date")
_TEXT = ("text", "msg", "message", "line", "event", "summary", "title")
_PROSE = ("report", "markdown", "md", "summary", "text", "answer", "content")
_VERDICT_WORD = re.compile(r"^(ok|up|down|healthy|unhealthy|degraded|warn|error|failed|serving|running|stopped)$", re.I)
_STATE_WORD = re.compile(r"^(ok|up|down|err|error|warn|healthy|unhealthy|running|stopped|serving|failed|pass|fail)$", re.I)
_NOT_QTY = re.compile(r"^(id|pid|port|vmid|index|idx|i|n_?id)$", re.I)


def _is_obj(x: Any) -> bool:
    return isinstance(x, dict)


def _scalar(v: Any) -> bool:
    return v is None or not isinstance(v, (dict, list))


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _hint(cap: str, presentation: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    # the capability's declared form first (DA-01d: the hints are Contract v2 presentation declarations now)
    if isinstance(presentation, dict) and isinstance(presentation.get("form"), str) and presentation.get("form"):
        h = {"form": presentation["form"], "map": dict(presentation.get("map") or {})}
        if isinstance(presentation.get("draw"), dict):
            h["draw"] = presentation["draw"]
        return h
    if cap in CAP_HINTS:
        return CAP_HINTS[cap]
    if re.search(r"\.(diff|patch)$", cap):
        return {"form": "diff"}
    if re.search(r"\.(health|healthz)$", cap):
        return {"form": "status"}
    return None


def _rows_key(c: Any) -> str:
    if not _is_obj(c):
        return "$"
    ok = lambda v: isinstance(v, list) and v and _is_obj(v[0])  # noqa: E731
    for k in _ROW_KEYS:
        if ok(c.get(k)):
            return k
    for k in c:
        if ok(c[k]):
            return k
    return "$"


def _rows(c: Any) -> Optional[list]:
    if isinstance(c, list):
        return c
    k = _rows_key(c)
    return c.get(k) if k != "$" and _is_obj(c) else None


def _first(r: dict, keys) -> Optional[str]:
    return next((k for k in keys if r.get(k) not in (None, "")), None)


def _num_keys(r: dict) -> List[str]:
    return [k for k in r if _num(r[k]) and not _NOT_QTY.match(k) and k not in _TIME]


def _is_diff(s: Any) -> bool:
    return isinstance(s, str) and bool(re.search(r"^(diff --git |--- |\+\+\+ |@@ )", s, re.M)) and bool(re.search(r"^[+-]", s, re.M))


def _is_media(s: Any) -> bool:
    return isinstance(s, str) and bool(re.match(r"^(data:image/|data:video/|https?:.*\.(png|jpe?g|gif|webp|svg|mp4|webm|mov|mp3|wav|ogg)(\?|$))", s, re.I))


def _verdict(d: Any) -> str:
    if not _is_obj(d):
        return str(d or "")
    for k in ("status", "level", "state", "health"):
        if d.get(k) is not None:
            return str(d[k])
    if d.get("ok") is True:
        return "ok"
    if d.get("ok") is False:
        return "failed"
    if d.get("healthy") is True:
        return "healthy"
    if d.get("healthy") is False:
        return "unhealthy"
    return ""


def _checks(d: Any) -> List[dict]:
    if not _is_obj(d):
        return []
    c = next((d[k] for k in ("checks", "findings", "components", "services", "results", "backends") if d.get(k)), None)
    if isinstance(c, list):
        return [x for x in c if _is_obj(x)]
    if _is_obj(c):
        return [dict({"name": k}, **v) if _is_obj(v) else {"name": k, "status": v} for k, v in c.items()]
    return [{"name": k, "status": v} for k, v in d.items()
            if (isinstance(v, bool) or (isinstance(v, str) and _STATE_WORD.match(v)))
            and k not in ("ok", "status", "state", "level", "health", "healthy")]


def from_cap_result(cap: str, result: Any, args: Optional[dict] = None, title: str = "", size: str = "l",
                    max_records: int = 3, presentation: Optional[dict] = None) -> List[Dict[str, Any]]:
    """The widget record(s) that draw a capability's answer, best first (the mirror of VeraWidget.fromCapResult)."""
    c = result
    if _is_obj(c) and c.get("type") == "tool_result" and "content" in c:
        c = c["content"]
    if c is None or c == "":
        return []
    cap = str(cap or "")
    ttl = title or cap or "result"
    out: List[Dict[str, Any]] = []

    def mk(form, data, why, map_=None, draw=None, t=None):
        r = {"id": re.sub(r"[^a-z0-9]+", "-", (cap or "result"), flags=re.I) + "-" + form, "form": form, "title": t or ttl,
             "source": cap, "read": {"args": dict(args or {}), "map": map_ or {}}, "frame": {"size": size}, "data": data, "why": why}
        if draw:
            r["draw"] = draw
        out.append(r)
        return r

    def done():
        return out[:max(1, int(max_records or 3))]

    def companion(c0, r):
        rw0 = _rows(c0)
        if not rw0 or len(rw0) > 60 or not _is_obj(rw0[0]) or r["form"] == "ranked":
            return
        nm = next((k for k in ("name", "title", "label", "id", "key") if rw0[0].get(k) is not None), None)
        nk = [k for k in _num_keys(rw0[0]) if not re.search(r"_at$|ts$", k)]
        if nm and nk:
            mk("ranked", c0, "the rows ranked by " + nk[0], {"values": _rows_key(c0), "name": nm, "value": nk[0]},
               t=ttl + " · by " + nk[0].replace("_", " "))

    # 1 error
    if _is_obj(c) and ((c.get("ok") is False and isinstance(c.get("error", c.get("message")), str))
                       or (isinstance(c.get("error"), str) and c.get("error") and len(c) <= 3)):
        mk("error", c, "a failed answer: ok false, or an error and little else")
        return done()
    # 2 terminal
    if _is_obj(c) and (c.get("stdout") is not None or c.get("stderr") is not None) and any(c.get(k) is not None for k in ("command", "cmd", "rc", "returncode")):
        cmd = c.get("command") or c.get("cmd")
        lines = (["$ " + str(cmd)] if cmd else []) + str(c.get("stdout") or "").split("\n") + str(c.get("stderr") or "").split("\n")
        rc = c.get("rc", c.get("returncode"))
        mk("terminal", {"lines": lines, "state": ("rc %s" % rc) if rc is not None else ""}, "a command and what it printed")
        if c.get("rc") and c.get("stderr"):
            last = [x for x in str(c["stderr"]).split("\n") if x][-1:] or ["rc %s" % c["rc"]]
            mk("error", {"error": last[0], "stderr": c["stderr"]}, "the command failed")
        return done()
    # 3 media
    if _is_media(c) or (_is_obj(c) and (c.get("image_b64") or c.get("b64") or _is_media(c.get("url")) or _is_media(c.get("src"))
                                         or _is_media(c.get("image")) or (isinstance(c.get("images"), list) and c["images"]))):
        mk("media", c, "an image, a video or a sound")
        return done()
    # 4 diff . 5 code
    if _is_diff(c) or (_is_obj(c) and (_is_diff(c.get("diff")) or _is_diff(c.get("patch")) or (isinstance(c.get("diff"), str) and c["diff"].strip()))):
        mk("diff", c, "a patch")
        return done()
    if _is_obj(c) and ((isinstance(c.get("code"), str) and c["code"].strip())
                       or (isinstance(c.get("content"), str) and c["content"].strip() and (c.get("path") or c.get("filename") or c.get("file")))):
        mk("code", c, "source: code, or a file's content")
        return done()
    # 6 the capability's hint
    h = _hint(cap, presentation)
    if h:
        r = mk(h["form"], c, "the " + cap + " hint", h.get("map"), h.get("draw"))
        if h["form"] not in CI_FORMS:
            companion(c, r)
        return done()
    rw, rk = _rows(c), _rows_key(c)
    # 7 progress
    if _is_obj(c) and isinstance(c.get("steps") or c.get("stages"), list) and (c.get("steps") or c.get("stages")):
        mk("progress", c, "steps with their state")
        return done()
    if rw and all(_is_obj(r) and any(r.get(k) is not None for k in ("step", "stage", "phase")) and any(r.get(k) is not None for k in ("status", "state", "done")) for r in rw):
        mk("progress", {"steps": rw}, "rows of step and state")
        return done()
    if rw and _is_obj(rw[0]):
        r0 = rw[0]
        tk, xk, nk = _first(r0, _TIME), _first(r0, _TEXT), _num_keys(r0)
        # 7b pages: rows with a web address and a title or what they say - a reading list, before the log a dated page
        # would make and the table that cut every page to a cell (kept in step with the element's rule 7b)
        if all(_is_obj(r) and re.match(r"^https?:", str(r.get("url") or r.get("link") or r.get("href") or "")) for r in rw) \
                and any(r.get("title") or r.get("snippet") or r.get("text") for r in rw):
            mk("reading", c, "pages: a title, an address and what they say", {"rows": rk})
            return done()
        # 8 events
        if tk and xk and all(_is_obj(r) for r in rw):
            kind = _first(r0, ("kind", "level", "type", "severity", "source")) or ""
            mk("log", c, "rows with a time and a line of text", {"events": rk, "t": tk, "text": xk, "kind": kind})
            if len(rw) > 1 and any(re.search(r"err|fail|warn", str(r.get("level", r.get("kind", r.get("severity", "")))), re.I) for r in rw):
                mk("pareto", c, "the lines counted by kind", {"values": rk, "count": _first(r0, ("kind", "level", "type", "severity")) or "kind"}, t=ttl + " · by kind")
            return done()
        # 9 series
        if tk and nk and len(rw) >= 3 and len([k for k in r0 if _scalar(r0[k])]) <= len(nk) + 3:
            if len(nk) == 1:
                mk("trace", c, "a series: a time and a number", {"series": rk, "t": tk, "v": nk[0]})
            else:
                mk("area", c, "series: a time and %d numbers" % len(nk), {"series": rk, "t": tk, "split": nk[:4]})
            mk("table", c, "the readings", {"rows": rk}, t=ttl + " · readings")
            return done()
        # 11 files
        if all(_is_obj(r) and (r.get("path") is not None or r.get("file") is not None) for r in rw):
            mk("files", c, "rows with a path", {"rows": rk})
            return done()
    # 10 level
    if _is_obj(c) and _num(c.get("value")):
        tr = next((c[k] for k in ("history", "trend", "series") if isinstance(c.get(k), list)), None)
        if c.get("max") is not None and tr is None:
            mk("ring", c, "a value of a maximum")
        else:
            mk("hero", c, "a value" + (" with its trend" if tr is not None else ""))
        return done()
    # 16 prose (before status: {ok: true, report: "..."} is a report that succeeded, not a verdict)
    if _is_obj(c):
        pk = next((k for k in _PROSE if isinstance(c.get(k), str) and c[k].strip()), None)
        if pk and (len(c[pk]) >= 200 or re.search(r"^#|\n[-*] |\n\n", c[pk])):
            mk("markdown", c[pk], "prose: " + pk)
            rest = [k for k in c if k != pk and _scalar(c[k])]
            if len(rest) > 1:
                mk("kv", c, "the rest of the answer", {"keys": rest[:12]}, t=ttl + " · fields")
            return done()
    # 12 status
    has_verdict = _is_obj(c) and any(c.get(k) is not None and _scalar(c[k]) for k in ("status", "state", "level", "health", "healthy"))
    if _is_obj(c) and _verdict(c) and ((has_verdict and _checks(c)) or (has_verdict and _VERDICT_WORD.match(_verdict(c)))
                                       or (c.get("ok") is not None and len(_checks(c)) >= 2)):
        mk("status", c, "a verdict and its checks")
        ck = _checks(c)
        if len(ck) > 6:
            mk("table", ck, "every check", t=ttl + " · checks")
        return done()
    # 13 table
    if rw and _is_obj(rw[0]):
        cols = [k for k in rw[0] if _scalar(rw[0][k])]
        if len(cols) >= 2 or len(rw) > 1:
            r = mk("table", c, "rows of the same shape", {"rows": rk})
            companion(c, r)
            return done()
    # 14 list
    if isinstance(c, list) and c and all(_scalar(x) for x in c):
        mk("list", [{"name": str(x)} for x in c], "a list of plain things")
        return done()
    if _is_obj(c):
        nums = [k for k in c if _num(c[k])]
        # 15 numbers
        if len(nums) >= 2 and len(nums) == len(c):
            mk("numbers" if len(nums) <= 6 else "ranked", c, "named numbers")
            return done()
        # 17 record
        if 1 <= len(c) <= 24 and all(_scalar(v) for v in c.values()):
            mk("kv", c, "a record: plain fields")
            return done()
        # 18 json
        mk("json", c, "structure: a tree to open")
        if rw:
            mk("table", c, "its rows", {"rows": rk}, t=ttl + " · " + rk)
        return done()
    if isinstance(c, list) and c:
        mk("json", c, "a list of mixed things")
        return done()
    if isinstance(c, str):
        if _is_diff(c):
            mk("diff", c, "a patch")
        elif len(c) >= 200 or "\n" in c:
            mk("markdown", c, "prose")
        else:
            mk("string", c, "a short text")
        return done()
    if isinstance(c, bool):
        mk("status", {"status": "ok" if c else "failed"}, "yes or no")
        return done()
    if _num(c):
        mk("hero", {"value": c}, "a number")
        return done()
    return done()


# ── present(): ONE reading of an answer for the desk (DA-02b - the mirror of VeraWidget.present, vera/widgets/present.js;
#    DESK-SPINE-DESIGN.md §2). A declared presentation (Contract v2, DA-01) decides exactly; else from_cap_result's hint
#    and shape rules, unchanged and read as one kind; nothing drawable -> records [] and element 'none'. The shared
#    fixtures tests/fixtures/presentation/ run on both sides and must agree. ───────────────────────────────────────────
import json as _json  # noqa: E402

PRESENT_KINDS: Dict[str, Dict[str, Any]] = {
    "table": {"element": "table", "form": "table", "content": ["rows"], "views": ["table", "cards", "list", "chart", "graph"]},
    "records": {"element": "records", "form": "reading", "content": ["rows"], "views": ["list", "cards", "table", "graph"]},
    "reading": {"element": "records", "form": "reading", "content": ["rows"], "views": ["list", "cards", "table", "graph"]},
    "document": {"element": "markdown", "form": "markdown", "content": ["body"], "views": ["document", "outline"]},
    "status": {"element": "widget", "form": "status", "content": ["verdict"], "views": ["status", "table"]},
    "metric": {"element": "widget", "form": "hero", "content": ["value"], "views": ["hero", "ring", "trace"]},
    "series": {"element": "widget", "form": "trace", "content": ["points"], "views": ["trace", "area", "column", "table"]},
    "graph": {"element": "widget", "form": "vgraph", "content": ["nodes"], "views": ["graph", "table"]},
    "log": {"element": "widget", "form": "log", "content": ["events"], "views": ["log", "table", "pareto"]},
    "progress": {"element": "widget", "form": "progress", "content": ["steps"], "views": ["progress", "log"]},
    "terminal": {"element": "session", "form": "terminal", "content": ["stdout"], "views": ["terminal"]},
    "media": {"element": "image", "form": "media", "content": ["url", "b64"], "views": ["media", "gallery"]},
    "code": {"element": "code", "form": "code", "content": ["code"], "views": ["code", "explode"]},
    "diff": {"element": "code", "form": "diff", "content": ["diff"], "views": ["diff", "split"]},
    "kv": {"element": "kv", "form": "kv", "content": ["fields"], "views": ["tiles", "rows", "graph"]},
    "calendar": {"element": "calendar", "form": "schedule", "content": ["events"], "views": ["month", "agenda", "schedule"]},
    "timeline": {"element": "widget", "form": "gantt", "content": ["spans"], "views": ["gantt", "stepper", "table"]},
    "matrix": {"element": "widget", "form": "heat", "content": ["cells"], "views": ["heat", "matrix", "table"]},
    "parts": {"element": "widget", "form": "donut", "content": ["parts"], "views": ["donut", "treemap", "waffle", "table"]},
    "entities": {"element": "entities", "form": "json", "content": ["groups", "entities"], "views": ["chips", "list", "graph"]},
    "receipt": {"element": "receipt", "form": "", "content": [], "views": []},
    "job": {"element": "job", "form": "", "content": ["job_id"], "views": []},
    "composite": {"element": "composite", "form": "", "content": [], "views": []},
}
_FORM_KIND = {"table": "table", "ranked": "table", "containers": "table", "sandboxes": "table", "reading": "reading", "files": "table",
              "markdown": "document", "status": "status", "hero": "metric", "ring": "metric", "numbers": "kv", "kv": "kv",
              "trace": "series", "area": "series", "candles": "series", "column": "series", "log": "log", "pareto": "log",
              "progress": "progress", "terminal": "terminal", "media": "media", "code": "code", "diff": "diff", "vgraph": "graph",
              "schedule": "calendar", "agenda": "calendar", "error": "status", "list": "table", "json": "kv", "string": "document"}
_MISSING = object()


def pick(x: Any, path: Any) -> Any:
    """One dotted path out of an answer (a.b[2].c, history[-1], '$' the whole) - VeraWidget.pick. _MISSING when absent."""
    if x is None:
        return _MISSING
    p = str("" if path is None else path).strip()
    if not p or p == "$":
        return x
    parts = [s for s in re.sub(r"\[(-?\d+)\]", r".\1", re.sub(r"^\$\.?", "", p)).split(".") if s]
    cur = x
    for k in parts:
        if cur is None or cur is _MISSING:
            return _MISSING
        if isinstance(cur, list) and re.fullmatch(r"-?\d+", k):
            i = int(k)
            if -len(cur) <= i < len(cur):
                cur = cur[i]
            else:
                return _MISSING
        elif isinstance(cur, dict):
            cur = cur.get(k, _MISSING)
        else:
            return _MISSING
    return cur


def _open(c: Any, content_path: str = "") -> Any:
    """§2.3: the envelopes every answer is opened from."""
    for _ in range(4):
        if isinstance(c, str):
            s = c.strip()
            if (s[:1] == "{" and s.endswith("}")) or (s[:1] == "[" and s.endswith("]")):
                try:
                    c = _json.loads(s)
                    continue
                except ValueError:
                    pass
            return c
        if not _is_obj(c):
            return c
        if c.get("type") == "tool_result" and "content" in c:
            c = c["content"]
            continue
        if c.get("ok") is True and "result" in c and len(c) <= 4:
            c = c["result"]
            continue
        d = c.get("data")
        if d is not None and isinstance(d, (dict, list)):
            if content_path:
                if pick(c, content_path) is _MISSING and pick(d, content_path) is not _MISSING:
                    c = d
                    continue
            elif len(c) <= 2:
                c = d
                continue
        return c
    return c


def _empty(v: Any) -> bool:
    return v is None or v is _MISSING or v == "" or (isinstance(v, (list, dict)) and not v)


def _fill(t: Any, args: Optional[dict]) -> str:
    def one(m):
        v = pick(args or {}, m.group(1))
        if v is None or v is _MISSING:
            return ""
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    return re.sub(r"\s*·\s*$", "", re.sub(r"\{([a-zA-Z0-9_.]+)\}", one, str(t))).strip()


def _as_rows(v: Any) -> list:
    if isinstance(v, list):
        return v
    if _is_obj(v):
        return [dict({"name": k}, **v[k]) if _is_obj(v[k]) else {"name": k, "value": v[k]} for k in v]
    return []


def _keyed_num(x: Any) -> Any:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return x if math.isfinite(x) else None
    if isinstance(x, str) and x.strip():
        try:
            f = float(x)
        except ValueError:
            return None
        return (int(f) if f.is_integer() else f) if math.isfinite(f) else None
    return None


def _keyed_point(p: Any, i: int, tk: Any, vk: Any) -> dict:
    if not _is_obj(p):
        return {"t": i, "v": _keyed_num(p)}
    if tk:
        t = pick(p, tk)
    else:
        t = next((p[k] for k in ("t", "ts", "time") if p.get(k) is not None), None)
    v = pick(p, vk) if vk else next((p[k] for k in ("v", "value") if p.get(k) is not None), None)
    return {"t": i if t is None or t is _MISSING else t, "v": _keyed_num(None if v is _MISSING else v)}


def keyed_series(v: Any, tk: Any = None, vk: Any = None) -> Dict[str, list]:
    """Series keyed by name (DA-01e, map.keyed) - present.js keyedSeries: {name: [{t, v}, ...]} or {name: {t: [...],
    v: [...]}} as {name: [{t, v}]}, one line per key; v a number or None."""
    out: Dict[str, list] = {}
    if not _is_obj(v):
        return out
    for k, s in v.items():
        pts = None
        if isinstance(s, list):
            pts = [_keyed_point(p, i, tk, vk) for i, p in enumerate(s)]
        elif _is_obj(s):
            ts, vs = pick(s, tk or "t"), pick(s, vk or "v")
            if isinstance(vs, list):
                pts = [{"t": ts[i] if isinstance(ts, list) and i < len(ts) and ts[i] is not None else i, "v": _keyed_num(x)}
                       for i, x in enumerate(vs)]
        if pts:
            out[str(k)] = pts
    return out


# ── DA-01e (1): three kinds read onto forms the widget already draws - present.js timelineSpans / matrixOf / partsOf,
#    field for field (the shared fixtures hold both sides to it) ─────────────────────────────────────────────────────
def _nil(x: Any) -> bool:
    return x is None or x is _MISSING


def _field(r: dict, m: dict, k: str, names) -> Any:
    """The declared path (map[k]) if there is one, else the first of the usual names the row has; _MISSING when none."""
    if m.get(k):
        return pick(r, m[k])
    for n in names:
        if r.get(n) is not None:
            return r[n]
    return _MISSING


def _jstr(x: Any) -> str:
    """String(x) as the page writes it: true / false, 1 not 1.0."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float) and x.is_integer() and math.isfinite(x):
        return str(int(x))
    return str(x)


def _label(x: Any, i: int) -> str:
    return "#" + str(i + 1) if _nil(x) or isinstance(x, (dict, list)) else _jstr(x)


def _js_num(x: Any) -> Any:
    """A number as the page keeps it: an integral float as an int (JSON writes 3, not 3.0)."""
    return int(x) if isinstance(x, float) and x.is_integer() else x


_ISO_T = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$", re.I)


def _span_time(x: Any) -> Any:
    """A time in seconds - a number as it is, a plain number string, an ISO date or date-time (no zone = UTC); else None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return x if math.isfinite(x) else None
    if not isinstance(x, str):
        return None
    s = x.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return _js_num(float(s))
    t = _ISO_T.match(s)
    if not t or int(t.group(1)) < 100 or not 1 <= int(t.group(2)) <= 12:
        return None
    off = 0
    if t.group(8) and t.group(8).upper() != "Z":
        z = t.group(8).replace(":", "")
        off = (-1 if z[0] == "-" else 1) * (int(z[1:3]) * 60 + int(z[3:5]))
    ms = (calendar.timegm((int(t.group(1)), int(t.group(2)), int(t.group(3)), int(t.group(4) or 0), int(t.group(5) or 0),
                           int(t.group(6) or 0), 0, 0, 0)) * 1000 + int(((t.group(7) or "") + "000")[:3]) - off * 60000)
    return ms // 1000 if ms % 1000 == 0 else ms / 1000


_SPAN_STATE = [(re.compile(r"^(ok|done|pass|passed|success|succeeded|merged|complete|completed|finished|ended|green)$", re.I), "done"),
               (re.compile(r"^(running|active|started|live|current|in[ _-]?progress)$", re.I), "running"),
               (re.compile(r"^(fail|failed|failure|error|errored|red|timeout|timed[ _-]?out|crashed|aborted|interrupted|cancelled|canceled)$", re.I), "failed")]


def _span_state(s: str) -> str:
    return next((st for rx, st in _SPAN_STATE if rx.match(s)), "")


def _js_round3(x: float) -> Any:
    """Math.round(x * 1000) / 1000 as the page does it (half up)."""
    return _js_num(math.floor(x * 1000 + 0.5) / 1000)


def timeline_spans(rows: Any, m: dict) -> List[dict]:
    """Dated spans -> gantt rows {name, start, end, at, status?, state?}: start / end in seconds from the first start, at the
    start as given. No end: start + duration, else open (to the latest time in the answer, running). No start: skipped."""
    got = []
    for i, r in enumerate(rows if isinstance(rows, list) else []):
        if not _is_obj(r):
            continue
        a = _field(r, m, "start", ["start", "started_at", "start_at", "begin", "from"])
        t0 = _span_time(None if a is _MISSING else a)
        if t0 is None:
            continue
        e = _field(r, m, "end", ["end", "finished_at", "ended_at", "end_at", "until", "to"])
        t1 = _span_time(None if e is _MISSING else e)
        if t1 is None:
            d = _field(r, m, "duration", ["duration", "duration_s", "seconds"])
            du = _span_time(None if d is _MISSING else d)
            if du is not None:
                t1 = t0 + du
        st = _field(r, m, "status", ["status", "state", "result"])
        nm = _field(r, m, "name", ["name", "title", "label", "id"])
        got.append({"name": _label(nm, i), "t0": t0, "t1": t1, "at": a,
                    "status": "" if _nil(st) or isinstance(st, (dict, list)) else _jstr(st)})
    if not got:
        return []
    lo = min(g["t0"] for g in got)
    hi = max(g["t0"] if g["t1"] is None else max(g["t0"], g["t1"]) for g in got)
    out = []
    for g in got:
        state = _span_state(g["status"]) or ("running" if g["t1"] is None else "")
        o = {"name": g["name"], "start": _js_round3(g["t0"] - lo), "end": _js_round3((hi if g["t1"] is None else g["t1"]) - lo),
             "at": g["at"]}
        if g["status"]:
            o["status"] = g["status"]
        if state:
            o["state"] = state
        out.append(o)
    return out


def matrix_of(v: Any, m: dict) -> Optional[dict]:
    """Rows x columns of a measure -> {rows: {row: {column: value}}, form: heat | matrix} - every row has every column
    ('' where it has none); heat when every value is a number. From a grid {row: {column: cell}}, wide rows, long
    rows (map.column) or rows with their cells in a list (map.entries); an object cell is read at map.value."""
    order: List[str] = []
    cols: List[str] = []
    at: Dict[str, Dict[str, Any]] = {}

    def put(rn, cn, x):
        rn, cn = _jstr(rn), _jstr(cn)
        if cn == "name":
            return
        if rn not in at:
            at[rn] = {}
            order.append(rn)
        if cn not in cols:
            cols.append(cn)
        at[rn][cn] = x

    def scalar(x):
        return "" if _nil(x) or isinstance(x, (dict, list)) else x

    def cell_of(x):
        return scalar(pick(x, m["value"])) if _is_obj(x) and m.get("value") else scalar(x)

    def is_cell(x):
        return not isinstance(x, list) and not (_is_obj(x) and not m.get("value"))

    def value_of(e):
        return scalar(_field(e, m, "value", ["value", "v", "status", "state"]))

    if _is_obj(v):
        for k, row in v.items():
            if _is_obj(row):
                for c, x in row.items():
                    if is_cell(x):
                        put(k, c, cell_of(x))
    elif isinstance(v, list):
        for i, r in enumerate(v):
            if not _is_obj(r):
                continue
            rn = _label(_field(r, m, "row", ["name", "id", "key", "label", "title"]), i)
            if m.get("entries"):
                es = pick(r, m["entries"])
                if isinstance(es, list):
                    for j, e in enumerate(es):
                        if _is_obj(e):
                            put(rn, _label(_field(e, m, "column", ["column", "name", "key", "label", "dimension"]), j), value_of(e))
            elif m.get("column"):
                cn = pick(r, m["column"])
                if not _nil(cn) and not isinstance(cn, (dict, list)):
                    put(rn, cn, value_of(r))
            else:
                if m.get("row"):
                    own = str(m["row"]) if re.fullmatch(r"[A-Za-z0-9_-]+", str(m["row"])) else None
                else:
                    own = next((n for n in ("name", "id", "key", "label", "title") if r.get(n) is not None), None)
                for c, x in r.items():
                    if c != own and is_cell(x):
                        put(rn, c, cell_of(x))
    if not order or not cols:
        return None
    rows = {rn: {c: at[rn].get(c, "") for c in cols} for rn in order}
    vals = [rows[rn][c] for rn in order for c in cols if not (isinstance(rows[rn][c], str) and rows[rn][c] == "")]
    heat = bool(vals) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vals)
    return {"rows": rows, "form": "heat" if heat else "matrix"}


_PART_VALUE = ["value", "v", "count", "n", "size", "total"]


def parts_of(v: Any, m: dict) -> List[dict]:
    """Shares of a whole -> [{name, value}], the parts more than nothing; past eight, the seven largest and 'other'."""
    out = []
    if isinstance(v, list):
        for i, r in enumerate(v):
            if _is_obj(r):
                x = _field(r, m, "value", _PART_VALUE)
                out.append({"name": _label(_field(r, m, "name", ["name", "label", "key", "id", "type"]), i),
                            "value": _keyed_num(None if x is _MISSING else x)})
    elif _is_obj(v):
        for k, x in v.items():
            if _is_obj(x):
                x = _field(x, m, "value", _PART_VALUE)
            out.append({"name": str(k), "value": _keyed_num(None if x is _MISSING else x)})
    out = [p for p in out if p["value"] is not None and p["value"] > 0]
    if len(out) > 8:
        by = sorted(out, key=lambda p: -p["value"])
        rest = 0
        for p in by[7:]:
            rest = rest + p["value"]
        out = by[:7] + [{"name": "other", "value": _js_num(rest)}]
    return out


def _declared(cap: str, c: Any, P: dict, args: Optional[dict], title: str, size: str, max_records: int) -> Dict[str, Any]:
    K = PRESENT_KINDS[P["kind"]]
    m = P.get("map") if _is_obj(P.get("map")) else {}
    ttl = title or (_fill(P["title"], args) if P.get("title") else "") or cap or "result"
    out: List[Dict[str, Any]] = []
    why = "declared: " + P["kind"]

    def rec(form, data, map_=None, t=None, draw=None):
        r = {"id": re.sub(r"[^a-z0-9]+", "-", (cap or "result"), flags=re.I) + "-" + form + ("-" + str(len(out)) if out else ""),
             "form": form, "title": t or ttl, "source": cap,
             "read": {"args": dict(args or {}), "map": {k: v for k, v in (map_ or {}).items() if v is not None}},
             "frame": {"size": size}, "data": data, "why": why, "presentation": P}
        if draw:
            r["draw"] = draw
        out.append(r)
        return r

    # a declaration that names its widget form (DA-01d): the form draws the answer through the map in its own words
    if isinstance(P.get("form"), str) and P.get("form"):
        rec(P["form"], c, dict(m), draw=P.get("draw") if _is_obj(P.get("draw")) else None)
        return {"records": out[:1]}

    def at(k):
        return pick(c, m[k]) if m.get(k) else _MISSING

    def val(k, default=None):
        v = at(k)
        return default if v is _MISSING else v

    content = next((v for v in (at(k) for k in K["content"]) if v is not _MISSING), _MISSING)
    if K["content"] and _empty(content):
        return {"records": [], "empty": _fill(P["empty"], args) if P.get("empty") else "nothing to show"}

    def renamed(rows, keys):
        res = []
        for r in rows:
            if not _is_obj(r):
                res.append(r)
                continue
            x = dict(r)
            for k in keys:
                if m.get(k) and k not in x:
                    v = pick(r, m[k])
                    if v is not _MISSING:
                        x[k] = v
            res.append(x)
        return res

    kind = P["kind"]
    if kind == "table":
        rec("table", c, {"rows": m.get("rows")})
        if m.get("name") and m.get("value"):
            rec("ranked", c, {"values": m.get("rows"), "name": m["name"], "value": m["value"]},
                t=ttl + " · by " + str(m["value"]).split(".")[-1].replace("_", " "))
    elif kind in ("records", "reading"):
        rec("reading", renamed(_as_rows(val("rows")), ["id", "title", "url", "snippet", "when", "score", "domain", "text", "cited"]))
    elif kind == "document":
        rec("markdown", str(val("body")))
    elif kind == "status":
        d = {"status": val("verdict")}
        if m.get("checks"):
            d["checks"] = _as_rows(val("checks"))
        rec("status", d)
    elif kind == "metric":
        rec("ring" if m.get("max") else "hero", c,
            {"value": m.get("value"), "max": m.get("max"), "unit": m.get("unit"), "trend": m.get("trend")})
    elif kind == "series":
        if m.get("keyed") is True:
            ks = keyed_series(val("points"), m.get("t"), m.get("v"))
            if not ks:
                return {"records": [], "empty": _fill(P["empty"], args) if P.get("empty") else "nothing to show"}
            rec("trace", ks)
        elif isinstance(m.get("split"), list) and m["split"]:
            rec("area", c, {"series": m.get("points"), "t": m.get("t"), "split": m["split"]})
        else:
            rec("trace", c, {"series": m.get("points"), "t": m.get("t"), "v": m.get("v")})
    elif kind == "log":
        rec("log", c, {"events": m.get("events"), "t": m.get("t"), "text": m.get("text"), "kind": m.get("kind") or m.get("level")})
    elif kind == "progress":
        rec("progress", {"steps": renamed(_as_rows(val("steps")), ["name", "status", "detail"])})
    elif kind == "graph":
        nid, ef, et = m.get("node_id"), m.get("edge_from"), m.get("edge_to")
        nodes = []
        for n in _as_rows(val("nodes")):
            if _is_obj(n) and nid and "id" not in n:
                v = pick(n, nid)
                n = dict({"id": None if v is _MISSING else v}, **n)
            nodes.append(n)
        edges = []
        for e in _as_rows(val("edges")):
            if _is_obj(e):
                e = dict(e)
                if ef and "source" not in e:
                    v = pick(e, ef)
                    e["source"] = None if v is _MISSING else v
                if et and "target" not in e:
                    v = pick(e, et)
                    e["target"] = None if v is _MISSING else v
            edges.append(e)
        rec("vgraph", {"nodes": nodes, "edges": edges, "links": edges})
    elif kind == "terminal":
        cmd = val("command")
        lines = ["$ " + str(cmd) if cmd else ""] + str(val("stdout") or "").split(chr(10)) + str(val("stderr") or "").split(chr(10))
        lines = [ln for i, ln in enumerate(lines) if i > 0 or ln]
        rc = val("rc")
        rec("terminal", {"lines": lines, "state": "rc " + str(rc) if rc is not None else ""})
    elif kind == "media":
        d = {}
        if val("url"):
            d["url"] = val("url")
        if val("b64"):
            d["image_b64"] = val("b64")
        if m.get("kind"):
            d["kind"] = val("kind")
        if m.get("caption"):
            d["caption"] = val("caption")
        rec("media", d)
    elif kind == "code":
        rec("code", {"code": str(val("code")), "lang": val("lang") or "", "path": val("path") or ""})
    elif kind == "diff":
        rec("diff", {"diff": str(val("diff"))})
    elif kind == "kv":
        rec("kv", val("fields"))
    elif kind == "calendar":
        rec("schedule", renamed(_as_rows(val("events")), ["start", "end", "title"]))
    elif kind == "entities":
        rec("json", {"groups": val("groups")} if m.get("groups") else {"entities": val("entities")})
    # DA-01e (1): the new kinds read to their form's own data - nothing left in it to draw: the empty line
    elif kind in ("timeline", "matrix", "parts"):
        nothing = {"records": [], "empty": _fill(P["empty"], args) if P.get("empty") else "nothing to show"}
        if kind == "timeline":
            sp = timeline_spans(_as_rows(val("spans")), m)
            if not sp:
                return nothing
            rec("gantt", sp)
        elif kind == "matrix":
            mx = matrix_of(val("cells"), m)
            if not mx:
                return nothing
            rec(mx["form"], mx["rows"])
        else:
            ps = parts_of(val("parts"), m)
            if not ps:
                return nothing
            rec("donut", ps)
    return {"records": out[:max(1, int(max_records or 3))]}


def present(cap: str, result: Any, args: Optional[dict] = None, title: str = "", size: str = "l", max_records: int = 3,
            presentation: Optional[dict] = None) -> Dict[str, Any]:
    """{element, kind, map, views, title, detail, records, why, declared, empty?} - the mirror of VeraWidget.present."""
    cap = str(cap or "")
    P = presentation if (_is_obj(presentation) and presentation.get("kind") in PRESENT_KINDS
                         and presentation.get("status") != "invalid") else None
    K = PRESENT_KINDS[P["kind"]] if P else None
    path = next((P["map"][k] for k in K["content"] if _is_obj(P.get("map")) and P["map"].get(k)), "") if P and not P.get("form") else ""
    c = _open(result, path)

    def base(kind, why):
        own = P.get("views") if P and isinstance(P.get("views"), list) and P.get("views") else None
        views = own or (PRESENT_KINDS[kind]["views"] if kind in PRESENT_KINDS else [])
        return {"element": PRESENT_KINDS[kind]["element"] if kind in PRESENT_KINDS else "widget", "kind": kind,
                "map": dict(P.get("map") or {}) if P else {}, "views": list(views),
                "title": title or (_fill(P["title"], args) if P and P.get("title") else "") or cap or "result",
                "detail": P.get("detail") if P else None, "why": why, "declared": bool(P)}

    if P:
        m = P.get("map") if _is_obj(P.get("map")) else {}
        if P["kind"] == "receipt":
            def g(k):
                if m.get(k):
                    v = pick(c, m[k])
                elif k == "ok" and _is_obj(c):
                    v = c.get("ok", _MISSING)
                else:
                    v = _MISSING
                return None if v is _MISSING else v
            return dict(base("receipt", "declared: receipt - it updates what it acted on"), records=[],
                        receipt={"ok": g("ok"), "id": g("id"), "message": g("message")})
        if P["kind"] == "job":
            v = pick(c, m.get("job_id") or "job_id")
            s = pick(c, m["stream"]) if m.get("stream") else _MISSING
            return dict(base("job", "declared: job - its live item follows"), records=[],
                        job={"job_id": None if v is _MISSING else v, "stream": None if s is _MISSING else s})
        if P["kind"] == "composite":
            parts = []
            for pt in (P.get("parts") if isinstance(P.get("parts"), list) else []):
                if _is_obj(pt) and pt.get("kind") in PRESENT_KINDS and pt.get("kind") != "composite":
                    got = _declared(cap, c, dict(pt, title=pt.get("title") or ""), args, pt.get("title") or title, size, 1)["records"]
                    if got:
                        parts.append(got[0])
            r = dict(base("composite", "declared: composite of " + str(len(parts))), records=parts)
            if not parts:
                r["element"] = "none"
                r["empty"] = _fill(P["empty"], args) if P.get("empty") else "nothing to show"
            return r
        d = _declared(cap, c, P, args, title, size, max_records)
        r = dict(base(P["kind"], "declared: " + P["kind"]), records=d["records"])
        if "empty" in d:
            r["empty"] = d["empty"]
        return r
    recs = from_cap_result(cap, c, args=args, title=title, size=size, max_records=max_records)
    if not recs:
        return dict(base("none", "nothing drawable"), element="none", records=[])
    kind = _FORM_KIND.get(recs[0]["form"], "widget")
    return dict(base(kind, recs[0].get("why") or ""), records=recs)
