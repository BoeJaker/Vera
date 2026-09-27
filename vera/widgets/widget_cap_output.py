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
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

CAP_FORMS = ("kv", "table", "list", "json", "log", "terminal", "diff", "code", "progress", "hero", "trace", "area",
             "column", "status", "files", "media", "error", "markdown")

# the capability's own hint: what the dashboard already reads each of these as (kept in step with CAP_HINTS in the element)
CAP_HINTS: Dict[str, Dict[str, Any]] = {
    "sysmon.history": {"form": "trace", "map": {"series": "samples", "v": "cpu"}},
    "sysmon.status": {"form": "numbers", "map": {"pick": {"cpu": "resources.cpu", "memory": "resources.mem", "guests": "proxmox.running", "containers": "docker.running"}}},
    "obs.events": {"form": "log", "map": {"events": "$", "t": "ts", "kind": "type", "text": "name"}},
    "ollama.request_log": {"form": "log", "map": {"events": "entries", "t": "ts", "kind": "instance", "text": "model"}},
    "ollama.route_stats": {"form": "ranked", "map": {"values": "stats", "count": "model", "sum": "n"}},
    "obs.node_temps": {"form": "temps", "map": {"values": "hosts", "name": "label", "value": "max_c"}},
    "evolve.activity": {"form": "area", "map": {"series": "buckets", "split": ["pass", "fail"], "t": "hour"}},
    "evolve.pipeline.list": {"form": "table", "map": {"rows": "pipelines"}, "draw": {"columns": ["branch", "status", "decision", "created_at"]}},
    "evolve.unittest.history": {"form": "trace", "map": {"series": "runs", "v": "passed", "t": "ts", "reverse": True}},
    "perf.stalls": {"form": "column", "map": {"values": "events", "value": "stalled_ms", "reverse": True}},
    "syslog.errors": {"form": "log", "map": {"events": "warnings", "t": "ts", "kind": "cap_group", "text": "message"}},
    "dash.health.summary": {"form": "pills", "map": {"values": "$", "entries": "status"}},
    "obs.modules": {"form": "treemap", "map": {"parts": "modules", "name": "name", "value": "caps_added"}},
    "estate.health": {"form": "status"}, "perf.scan": {"form": "status"}, "obs.health": {"form": "status"},
    "backup.status": {"form": "table", "map": {"rows": "guests"}, "draw": {"columns": ["name", "status", "state", "backups"]}},
    "docker.ps": {"form": "containers", "map": {"rows": "containers", "name": "Names", "status": "State", "host": "host_id"}},
    "evolve.sandbox.list": {"form": "sandboxes", "map": {"rows": "sandboxes", "name": "name", "status": "running"}},
    "dream.history": {"form": "table", "map": {"rows": "history"}, "draw": {"columns": ["label", "title", "started_at", "signal"]}},
    "fabric.graphs.snapshot": {"form": "vgraph"}, "fabric.entity_graph.snapshot": {"form": "vgraph"}, "memory.graph_full": {"form": "vgraph"},
    "topology.snapshot": {"form": "vgraph"}, "mesh.topology": {"form": "vgraph"}, "cal.events.list": {"form": "schedule"},
    "exec.bash.run": {"form": "terminal"}, "code.read": {"form": "code"}, "code.diff": {"form": "diff"},
    "evolve.pipeline.diff": {"form": "diff"}, "evolve.sandbox.diff": {"form": "diff"},
}
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


def _hint(cap: str) -> Optional[Dict[str, Any]]:
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
                    max_records: int = 3) -> List[Dict[str, Any]]:
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
    h = _hint(cap)
    if h:
        r = mk(h["form"], c, "the " + cap + " hint", h.get("map"), h.get("draw"))
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
