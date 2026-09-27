"""The BROAD planning style: work-streams planned concurrently, merged host-side.

Pure tests (lowercase import) cover stream parsing, the merge and the route
rota; the app tests drive the real _v6_plan_broad with the model and the
per-stream planner stubbed, and parse the loop's source for the wiring.
"""
import ast
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.planning import planner_styles as PS  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
CAT = ["web.research", "prose.author", "code.author", "exec.python.run", "exec.bash.run"]


# ── the table ────────────────────────────────────────────────────────────────

def test_broad_replaces_the_single_planner_call_and_is_not_stepwise():
    b = PS.LOOP_STYLES["broad"]
    assert b["broad"] and not b["run_planner"] and not b["master_plan"] and not b["recon"]
    assert not b["stepwise_controller"] and b["shape_guards"]


# ── parsing the streams ─────────────────────────────────────────────────────

def test_streams_are_renumbered_and_invented_caps_dropped():
    got = PS.parse_streams({"streams": [
        {"id": 7, "title": "Gather", "objective": "find facts", "caps": ["web.research", "file.write"]},
        {"id": 9, "title": "Write", "objective": "write it", "dependencies": [7],
         "caps": ["prose.author"], "deliverable": "report.md"}]}, catalog=CAT)
    assert [s["id"] for s in got] == [1, 2]
    assert got[0]["caps"] == ["web.research"]                   # file.write is not real
    assert got[1]["dependencies"] == [1] and got[1]["deliverable"] == "report.md"


def test_a_forward_or_self_dependency_is_not_trusted():
    got = PS.parse_streams({"streams": [
        {"id": 1, "title": "A", "objective": "a", "dependencies": [2, 1]},
        {"id": 2, "title": "B", "objective": "b", "dependencies": [1]}]}, catalog=CAT)
    assert got[0]["dependencies"] == [] and got[1]["dependencies"] == [1]


def test_junk_and_overflow_are_bounded():
    assert PS.parse_streams("nonsense") == []
    many = {"streams": [{"title": "s%d" % i, "objective": "o"} for i in range(9)]}
    assert len(PS.parse_streams(many, max_streams=5)) == 5


# ── the directive, the rota, the merge ──────────────────────────────────────

STREAMS = [{"id": 1, "title": "Gather", "objective": "find facts", "deliverable": "notes.md",
            "dependencies": [], "caps": ["web.research"]},
           {"id": 2, "title": "Build", "objective": "build app", "deliverable": "app.html",
            "dependencies": [], "caps": ["code.author"]},
           {"id": 3, "title": "Write", "objective": "write up", "deliverable": "report.md",
            "dependencies": [1, 2], "caps": ["prose.author"]}]


def test_a_directive_names_its_own_stream_and_is_piecewise():
    d = PS.stream_directive("goal", STREAMS, STREAMS[2])
    assert d.startswith("[PIECEWISE]")
    assert "PLAN ONLY STREAM 3 of 3: Write" in d and "DELIVERABLE: report.md" in d
    assert "stream 1 Gather -> notes.md" in d and "stream 2 Build -> app.html" in d


def test_streams_alternate_gpu_and_cpu_routes():
    assert PS.stream_roles(3) == ["stream", "stream_cpu", "stream"]
    assert PS.stream_roles(0) == []


def test_the_merge_renumbers_and_links_streams_by_their_dependencies():
    subs = [[{"id": 1, "title": "g1"}, {"id": 2, "title": "g2", "needs": [1]}],
            [{"id": 1, "title": "b1"}],
            [{"id": 1, "title": "w1"}, {"id": 2, "title": "w2", "needs": [1]}]]
    out = PS.merge_streams(STREAMS, subs)
    assert [s["id"] for s in out] == [1, 2, 3, 4, 5]
    assert out[1]["needs"] == [1]                               # inside stream 1
    assert out[2]["needs"] == []                                # stream 2 depends on nothing
    assert out[3]["needs"] == [2, 3]                            # stream 3 needs 1's last + 2's last
    assert out[4]["needs"] == [4] and out[4]["piece_title"] == "Write"


def test_a_stream_that_planned_nothing_breaks_no_link():
    subs = [[{"id": 1, "title": "g1"}], [], [{"id": 1, "title": "w1"}]]
    out = PS.merge_streams(STREAMS, subs)
    assert [s["title"] for s in out] == ["g1", "w1"] and out[1]["needs"] == [1]


def test_the_merge_respects_the_hard_cap():
    subs = [[{"id": i, "title": "x"} for i in range(1, 10)]] * 3
    assert len(PS.merge_streams(STREAMS, subs, hard_cap=7)) == 7


# ── the loop's broad planner, for real, with the model stubbed ─────────────

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


def _run_broad(monkeypatch, brief_json):
    seen = {"calls": [], "in_flight": 0, "max_in_flight": 0}
    both_started = asyncio.Event()

    async def _gen(prompt, system="", **kw):
        return brief_json

    async def _orch(goal, cat, skills, csm, **kw):
        seen["in_flight"] += 1
        seen["max_in_flight"] = max(seen["max_in_flight"], seen["in_flight"])
        seen["calls"].append({"role": kw.get("route_role"), "profile": kw.get("route_profile"),
                              "cat": list(cat), "directive": kw.get("master_plan", "")})
        if seen["in_flight"] >= 2:
            both_started.set()
        # Concurrency proof: a sequential implementation deadlocks here and the
        # wait_for below fails the test instead of hanging it.
        await asyncio.wait_for(both_started.wait(), timeout=5)
        seen["in_flight"] -= 1
        n = len(seen["calls"])
        return {"steps": [{"id": 1, "title": "s%d-a" % n, "caps": [], "needs": []},
                          {"id": 2, "title": "s%d-b" % n, "caps": [], "needs": [1]}]}

    async def _emit(ev):
        seen.setdefault("events", []).append(ev)

    monkeypatch.setattr(M, "_safe_ollama_generate_dw", _gen)
    monkeypatch.setattr(M, "_v5_orchestrate_plan", _orch)
    monkeypatch.setattr(M, "emit_event", _emit)
    plan = asyncio.run(M._v6_plan_broad(
        "research small LLMs and write a report", CAT + ["x.%d" % i for i in range(30)],
        [], {}, max_steps=8, sid="t", stream_id=""))
    return plan, seen


BRIEF = ('{"streams":[{"id":1,"title":"Gather","objective":"find facts","caps":["web.research"],'
         '"deliverable":"notes.md"},{"id":2,"title":"Write","objective":"write report",'
         '"dependencies":[1],"caps":["prose.author"],"deliverable":"report.md"}]}')


@needs_app
def test_broad_plans_every_stream_at_the_same_time(monkeypatch):
    plan, seen = _run_broad(monkeypatch, BRIEF)
    assert seen["max_in_flight"] == 2
    assert [c["role"] for c in seen["calls"]] == ["stream", "stream_cpu"]
    assert all(c["profile"] == "planning_style" for c in seen["calls"])


@needs_app
def test_each_stream_is_planned_as_its_own_piece(monkeypatch):
    _, seen = _run_broad(monkeypatch, BRIEF)
    dirs = sorted(c["directive"] for c in seen["calls"])
    assert any("PLAN ONLY STREAM 1 of 2: Gather" in d for d in dirs)
    assert any("PLAN ONLY STREAM 2 of 2: Write" in d for d in dirs)


@needs_app
def test_a_cpu_stream_sees_a_trimmed_catalog(monkeypatch):
    _, seen = _run_broad(monkeypatch, BRIEF)
    cpu = next(c for c in seen["calls"] if c["role"] == "stream_cpu")
    gpu = next(c for c in seen["calls"] if c["role"] == "stream")
    assert len(cpu["cat"]) <= 16 < len(gpu["cat"])
    assert "prose.author" in cpu["cat"]                         # its own caps survive the trim


@needs_app
def test_the_merged_plan_links_the_write_up_to_the_gathering(monkeypatch):
    plan, seen = _run_broad(monkeypatch, BRIEF)
    steps = plan["steps"]
    assert len(steps) == 4 and plan["broad"]["streams"] == 2
    assert steps[2]["needs"] == [2]                             # Write's first step needs Gather's last
    kinds = [e["type"] for e in seen["events"]]
    assert "agent_loop_v6.broad_streams" in kinds
    assert kinds.count("agent_loop_v6.broad_stream_planned") == 2


@needs_app
def test_no_streams_means_no_steps_so_the_loop_falls_back(monkeypatch):
    plan, _ = _run_broad(monkeypatch, '{"streams": []}')
    assert plan["steps"] == [] and "did not split" in plan["broad"]["error"]


# ── wiring ──────────────────────────────────────────────────────────────────

def _body(name):
    tree = ast.parse(SRC)
    f = next(n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return "\n".join(SRC.splitlines()[f.lineno - 1:f.end_lineno])


def test_the_loop_runs_broad_and_falls_back_to_its_own_planner():
    b = _body("cap_dag_agent_loop_v6")
    assert 'if _pstyle.get("broad"):' in b and "await _v6_plan_broad(" in b
    assert 'or _broad_fell_back:' in b
    # a successful broad plan must not be overwritten by the stepwise empty plan
    assert 'elif not _pstyle.get("broad"):' in b
    assert '_chosen_stepwise = bool(_pstyle.get("stepwise_controller"))' in b


def test_the_planner_route_override_defaults_to_the_old_route():
    b = _body("_v5_orchestrate_plan")
    assert '_plan_profile = route_profile or LOOP_ROUTING_PROFILE' in b
    assert '_plan_role = route_role or "planner"' in b
    assert b.count("profile=_plan_profile, role=_plan_role") == 2
