# -*- coding: utf-8 -*-
"""The Loop Lab's LENSES as layout files (vera/widgets/layouts/looplab*.json).

A lens is one way of looking at the automated development estate - what is live, the gates and their tests, the
census beside the commits, the agentic loops, the agents and their board, the branches - and each is a VeraDash
grid of widget RECORDS: every tile is a widget drawn by <vera-widget> from its own source (the ci.* / loop.ci.* /
census.* / evolve.* readings), or a Loop Lab element placed as a widget (form `element`). So each tile is also a
thing the canvas, any dashboard or the chat can place - the Loop Lab is widgets all the way down, and the page
rotates through the lenses (evolve_panel.html's lens strip).

Run from the repo root:  python3 vera/widgets/gen_looplab_lenses.py   (writes the files; idempotent)
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from vera.widgets.widget_record import size_for_span  # noqa: E402

GRID = {"cols": 12, "row": 58, "gap": 10, "widths": [2, 3, 4, 6, 8, 12]}
OUT = os.path.join(os.path.dirname(__file__), "layouts")


def tile(id, title, form, source, span, at, shape="matrix", args=None, map=None, draw=None, refresh="60s", note=""):
    w, h = span
    rec = {"id": id, "form": form, "source": source, "shape": shape, "title": title,
           "read": {"refresh": refresh, "args": args or {}, "map": map or {}},
           "frame": {"size": size_for_span(w, h), "span": [w, h], "max_body": None},
           "draw": dict({"body": "record"}, **(draw or {})), "actions": ["dive", "pin", "ask"], "place": "dashboard"}
    if note:
        rec["note"] = note
    return {"record": rec, "at": list(at), "span": [w, h], "hidden": False, "refresh": refresh, "floated": False}


def element(id, title, tag, span, at, attrs=None, note=""):
    t = tile(id, title, "element", "", span, at, shape="panel", draw={"tag": tag, "attrs": attrs or {}}, refresh="", note=note)
    t["record"]["read"] = {"refresh": "", "args": {}, "map": {}}
    return t


LENSES = {
    "looplab": ("Live", "What is happening now: the pulse of the gates, who is working where, the loops and the race, "
                "the pipelines, the open board, the test activity of the last day.", [
        tile("pulse", "Pulse", "ci-pulse", "ci.pulse", (6, 3), (0, 0), shape="values", args={"buckets": "day"}, refresh="30s"),
        tile("fleet", "Fleet", "ci-fleet", "ci.fleet", (6, 3), (6, 0), shape="items", refresh="20s"),
        tile("loops", "Agentic loops", "status-matrix", "loop.ci.matrix", (6, 4), (0, 3), refresh="20s"),
        tile("race", "Race to green", "race-green", "ci.race", (6, 4), (6, 3), refresh="30s"),
        tile("pipes", "Pipelines", "status-matrix", "evolve.pipeline.list", (6, 4), (0, 7), args={"limit": 80}, refresh="20s"),
        tile("board", "Open work", "ci-board", "ci.board", (6, 4), (6, 7), shape="items", args={"include_done": False}),
        element("activity", "Test activity · 24h", "vera-test-activity-timeline", (12, 4), (0, 11), attrs={"hours": 24}),
    ]),
    "looplab-gates": ("Gates & tests", "Every gate run as a lane, red first; the tests behind them across runs; "
                      "the pass rate by day; the coverage the suite has and the critical tier that gates a merge.", [
        tile("matrix", "Status matrix", "status-matrix", "ci.matrix", (8, 6), (0, 0), args={"order": "red"}, refresh="30s"),
        tile("pulse", "Pass rate by day", "ci-pulse", "ci.pulse", (4, 3), (8, 0), shape="values", args={"buckets": "day"}),
        tile("tests", "Tests across runs", "test-grid", "ci.tests", (4, 3), (8, 3), refresh="60s"),
        tile("race", "Race to green", "race-green", "ci.race", (6, 4), (0, 6)),
        tile("coverage", "Test coverage by module", "treemap", "evolve.tests.matrix", (6, 4), (6, 6), shape="parts",
             map={"parts": "modules", "name": "module", "value": "tests"}, refresh="10m",
             note="collect-only: every test module sized by its test count"),
        tile("agents", "Runs by agent", "status-matrix", "ci.matrix", (12, 3), (0, 10), args={"group": "controller"}),
    ]),
    "looplab-census": ("Census × commits", "The census run by run beside the commits that landed before each run - "
                       "a real gain or loss lit, the noise named - the goal matrix, and the commit graph it came from.", [
        tile("census", "Census × commits", "census-commits", "ci.census", (12, 10), (0, 0), args={"template": "default"}, refresh="5m"),
        # the commit graph NEXT TO the census runs: one timeline (runs as bands where they ended, the commits that
        # landed before each on their lanes) beside the repository's own graph
        tile("timeline", "Census timeline · runs among the commits", "census-timeline", "ci.census", (6, 9), (0, 10),
             shape="events", args={"template": "default"}, refresh="5m"),
        element("graph", "Commit graph", "vera-git-graph", (6, 9), (6, 10)),
        tile("done", "Goals done per run", "trace", "census.runs", (6, 2), (0, 19), shape="series",
             map={"series": "runs", "v": "done"}, refresh="5m"),
        tile("quality", "Quality per run", "trace", "census.runs", (6, 2), (6, 19), shape="series",
             map={"series": "runs", "v": "quality_mean"}, refresh="5m"),
    ]),
    "looplab-perf": ("Performance", "Where the time goes: the census in flight and its routing, each model's latency, "
                     "every agentic loop's wall time split by tool and model calls, census and loop wall time run by run, "
                     "and the request log.", [
        tile("live", "Census, live", "census-live", "census.live", (6, 4), (0, 0), shape="stages", refresh="5s"),
        tile("latency", "Models · latency and throughput", "table", "ollama.route_stats", (6, 4), (6, 0), shape="items",
             map={"rows": "stats"}, draw={"columns": ["model", "job_type", "instance", "n", "ema_elapsed_s", "ema_tps"], "sort": "ema_elapsed_s"},
             refresh="30s", note="every model x job type x node the router has timed: calls, EMA seconds, EMA tokens/s"),
        tile("loops", "Agentic loop performance", "loop-perf", "loop.ci.perf", (12, 7), (0, 4), shape="items",
             args={"limit": 24}, refresh="30s"),
        # trends drawn from real readings only: an empty source (perf.stalls with no stalls) draws its SAMPLE face,
        # which on a performance lens reads as an alarm that is not there
        tile("census-wall", "Census wall time per run (s)", "trace", "census.runs", (6, 2), (0, 11), shape="series",
             map={"series": "runs", "v": "wall_total_s"}, refresh="5m"),
        tile("loop-wall", "Loop wall time per run (s)", "trace", "loop.ci.perf", (6, 2), (0, 13), shape="series",
             args={"limit": 40}, map={"series": "rows", "v": "wall_s", "reverse": True}, refresh="60s"),
        tile("requests", "Request log", "log", "ollama.request_log", (6, 4), (6, 11), shape="events",
             map={"events": "entries", "t": "ts", "kind": "instance", "text": "model"}, refresh="10s"),
    ]),
    "looplab-work": ("Work", "The work in flight: the census as it runs, the loops and where their time goes, the census "
                     "runs among the commits, and every task's recent runs.", [
        tile("live", "Census, live", "census-live", "census.live", (6, 4), (0, 0), shape="stages", refresh="5s"),
        tile("loops", "Loop performance", "loop-perf", "loop.ci.perf", (6, 4), (6, 0), shape="items", args={"limit": 12}, refresh="30s"),
        tile("timeline", "Census timeline", "census-timeline", "ci.census", (6, 7), (0, 4), shape="events",
             args={"template": "default"}, refresh="5m"),
        tile("tasks", "Tasks · recent runs", "status-matrix", "evolve.tasks.overview", (6, 7), (6, 4), refresh="2m"),
    ]),
    "looplab-loops": ("Agentic loops", "Every loop run by goal, the newest loop's race through its gate and steps, "
                      "its plan as a board, and every task's recent runs.", [
        tile("loops", "Loops by goal", "status-matrix", "loop.ci.matrix", (12, 5), (0, 0), refresh="20s"),
        tile("race", "Newest loop · race", "race-green", "loop.ci.race", (6, 4), (0, 5), refresh="20s"),
        tile("plan", "Newest loop · plan", "ci-board", "loop.ci.board", (6, 4), (6, 5), shape="items", refresh="20s"),
        tile("tasks", "Tasks · recent runs", "status-matrix", "evolve.tasks.overview", (12, 5), (0, 9), refresh="2m"),
    ]),
    "looplab-agents": ("Agents & board", "Who is doing what: the board by lane with each card's gate, the fleet, "
                       "who wrote which commits, and the gate record split by agent.", [
        tile("board", "Board", "ci-board", "ci.board", (12, 5), (0, 0), shape="items"),
        tile("fleet", "Fleet", "ci-fleet", "ci.fleet", (6, 4), (0, 5), shape="items", refresh="20s"),
        element("authors", "Authorship", "vera-author-map", (6, 4), (6, 5)),
        tile("by-agent", "Gate runs by agent", "race-green", "ci.race", (12, 3), (0, 9), args={"group": "controller"}),
    ]),
    "looplab-branches": ("Branches", "Every branch through its pipeline, the commit graph, the errors the loops "
                         "raised, and the gate lanes of the last week.", [
        element("pipeline", "Branch pipeline", "vera-branch-pipeline", (12, 5), (0, 0)),
        element("graph", "Commit graph", "vera-git-graph", (6, 5), (0, 5)),
        element("errors", "Error radar", "vera-error-radar", (6, 5), (6, 5)),
        tile("week", "Gate lanes", "status-matrix", "ci.matrix", (12, 4), (0, 10), args={"order": "recent"}),
    ]),
}

# a stock chart form (trace) at XL composes a table under itself and measures the body it grows - keep them at L
ORDER = ["looplab", "looplab-gates", "looplab-census", "looplab-perf", "looplab-loops", "looplab-agents", "looplab-branches", "looplab-work"]


def build():
    out = {}
    for key in ORDER:
        name, note, tiles = LENSES[key]
        # a tile's id is a DOM id on the Loop Lab page, where every lens's grid lives at once: "pulse" in two lenses
        # was one element, and the second lens lost its tile - so every id carries its lens
        for t in tiles:
            if not t["record"]["id"].startswith(key + "-"):
                t["record"]["id"] = key + "-" + t["record"]["id"]
        out[key] = {"v": 2, "dashboard": key, "layout": "default", "key": key, "user": "", "grid": GRID,
                    "name": name, "note": note, "widgets": tiles}
    return out


if __name__ == "__main__":
    for key, lay in build().items():
        with open(os.path.join(OUT, key + ".json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(lay, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print(key, len(lay["widgets"]))
