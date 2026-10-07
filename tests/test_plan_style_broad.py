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


def test_the_core_plan_goes_to_the_gpu_route_and_the_briefs_to_the_cpu_route():
    """compute-roles: the plan the run waits on is GPU work; CPU plans in parallel."""
    assert PS.PLAN_ROLE == "stream" and PS.ENRICH_ROLE == "enrich"
    assert not hasattr(PS, "ENRICH_GRACE_S")           # nothing waits for a CPU brief


def test_a_brief_becomes_a_bounded_evidence_block_and_keeps_its_years():
    note = PS.enrich_note("NEEDS:\n- the March 2024 licence text\n- Valkey's first release date\n"
                          "PITFALLS:\n- confusing SSPL with RSAL\n" + "- x%d\n" * 0)
    assert note.startswith("\n\nSTREAM BRIEF") and "March 2024" in note
    assert "confusing SSPL with RSAL" in note
    assert len(PS.enrich_note("\n".join("- line %d %s" % (i, "y" * 150) for i in range(40)))) \
        <= PS.MAX_BRIEF_CHARS + 200
    assert PS.enrich_note("") == "" and PS.enrich_note(None) == ""


def test_a_brief_written_as_one_long_line_per_section_is_kept_not_dropped():
    """Live 2026-09-27: qwen3.5/qwen3.6 write "NEEDS: a; b; c" on ONE line; the old
    formatter discarded every line over 240 chars and the brief came back empty
    (the GPU quick brief and cpu-247's first 35B brief, 0 characters each)."""
    raw = ("NEEDS: Official Redis blog posts announcing the license change to SSPL/BSL; "
           "Valkey Foundation press releases and GitHub repository links; reputable tech news "
           "coverage (e.g., TechCrunch, The Register) for context on community reaction.\n"
           "PITFALLS: Citing outdated or unverified third-party blogs instead of primary sources; "
           "missing the specific date of the license switch announcement; confusing the initial "
           "fork with the official Valkey Foundation formation date.")
    note = PS.enrich_note(raw)
    assert note, "a one-long-line brief must not come back empty"
    assert "  - NEEDS:" in note and "  - PITFALLS:" in note
    assert "Valkey Foundation press releases and GitHub repository links" in note
    assert "confusing the initial fork with the official Valkey Foundation formation date." in note
    assert len(note) <= PS.MAX_BRIEF_CHARS + 200


def test_a_single_overlong_line_with_no_separators_is_trimmed_not_dropped():
    note = PS.enrich_note("x" * 900)
    assert note and "x" * PS.MAX_BULLET_CHARS in note and "x" * (PS.MAX_BULLET_CHARS + 1) not in note

def test_the_enrich_prompt_names_the_one_stream_to_brief():
    p = PS.enrich_prompt("goal", STREAMS, STREAMS[0])
    assert "BRIEF THIS STREAM: 1. Gather" in p and "3. Write -> report.md" in p


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


def test_a_step_another_stream_already_planned_is_dropped_and_its_dependents_repointed():
    """The live case (2026-09-27): stream 2 re-planned stream 1's research."""
    steps = [
        {"id": 1, "piece": 1, "title": "Research Redis licensing shift and Valkey fork details",
         "caps": ["web.research"], "needs": []},
        {"id": 2, "piece": 2, "title": "Research Redis Licensing Change and Valkey Fork",
         "caps": ["web.research"], "needs": [1]},
        {"id": 3, "piece": 2, "title": "Produce citations list", "caps": ["prose.author"],
         "needs": [2]},
    ]
    kept, dropped = PS.dedupe_across_streams(steps)
    assert [k["title"] for k in kept] == [steps[0]["title"], "Produce citations list"]
    assert dropped[0]["duplicate_of"] == 1
    assert kept[1]["id"] == 2 and kept[1]["needs"] == [1]       # re-pointed at the kept twin


def test_similar_words_with_different_capabilities_are_not_merged():
    steps = [{"id": 1, "piece": 1, "title": "Fetch the sales data", "caps": ["http.get"]},
             {"id": 2, "piece": 2, "title": "Write about the sales data", "caps": ["prose.author"]}]
    kept, dropped = PS.dedupe_across_streams(steps)
    assert len(kept) == 2 and dropped == []


def test_repeats_inside_one_stream_are_left_to_that_streams_planner():
    steps = [{"id": 1, "piece": 1, "title": "Run the tests", "caps": ["exec.bash.run"]},
             {"id": 2, "piece": 1, "title": "Run the tests", "caps": ["exec.bash.run"]}]
    kept, dropped = PS.dedupe_across_streams(steps)
    assert len(kept) == 2 and dropped == []


def test_the_brief_asks_for_the_fewest_streams_and_no_unasked_deliverables():
    s = PS.BROAD_BRIEF_SYSTEM
    assert "FEWEST streams" in s and "at most TWO streams" in s
    assert "did not ask for" in s


def test_a_dependent_stream_is_told_not_to_regather():
    d = PS.stream_directive("goal", STREAMS, STREAMS[2])
    assert "WILL ALREADY EXIST" in d and "do NOT plan any step that gathers" in d


# ── the loop's broad planner, for real, with the model stubbed ─────────────

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


def _run_broad(monkeypatch, brief_json, enrich_delay=0.0, enrich_fail=False,
               drain=False, enrich_model="", stream_steps=0):
    seen = {"plans": [], "enrich": [], "quick": [], "in_flight": 0, "max_in_flight": 0,
            "events": [], "enrich_started_before_plans_done": False}
    plans_done = {"n": 0}

    async def _gen(prompt, system="", **kw):
        if kw.get("request_stage") == "plan_quick_brief":
            seen["quick"].append({"role": kw.get("role"), "prefer_gpu": kw.get("prefer_gpu"),
                                  "profile": kw.get("profile")})
            return "NEEDS:\n- the licence announcement\nPITFALLS:\n- secondary blogs"
        if kw.get("role") == "enrich":
            seen["enrich"].append({"profile": kw.get("profile"), "prefer_gpu": kw.get("prefer_gpu"),
                                   "model": kw.get("model")})
            seen["enrich_in_flight"] = seen.get("enrich_in_flight", 0) + 1
            seen["enrich_max"] = max(seen.get("enrich_max", 0), seen["enrich_in_flight"])
            if plans_done["n"] < 2:
                seen["enrich_started_before_plans_done"] = True
            try:
                if enrich_fail:
                    raise RuntimeError("cpu node down")
                await asyncio.sleep(enrich_delay or 0.02)
                return "NEEDS:\n- the 2024 licence text\nPITFALLS:\n- mixing up SSPL and RSAL"
            finally:
                seen["enrich_in_flight"] -= 1
        return brief_json

    both_started = asyncio.Event()

    async def _orch(goal, cat, skills, csm, **kw):
        seen["in_flight"] += 1
        seen["max_in_flight"] = max(seen["max_in_flight"], seen["in_flight"])
        seen["plans"].append({"role": kw.get("route_role"), "profile": kw.get("route_profile"),
                              "prefer_gpu": kw.get("prefer_gpu"), "directive": kw.get("master_plan", ""),
                              "max_steps": kw.get("max_steps")})
        if seen["in_flight"] >= 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        seen["in_flight"] -= 1
        plans_done["n"] += 1
        n = len(seen["plans"])
        return {"steps": [{"id": 1, "title": "s%d-a" % n, "caps": [], "needs": []},
                          {"id": 2, "title": "s%d-b" % n, "caps": [], "needs": [1]}]}

    async def _emit(ev):
        seen["events"].append(ev)

    monkeypatch.setattr(M, "_safe_ollama_generate_dw", _gen)
    monkeypatch.setattr(M, "_v5_orchestrate_plan", _orch)
    monkeypatch.setattr(M, "emit_event", _emit)

    async def _go():
        plan = await M._v6_plan_broad(
            "research small LLMs and write a report", CAT, [], {}, max_steps=8, sid="t", stream_id="",
            enrich_model=enrich_model, stream_steps=stream_steps)
        tasks = plan.pop("_enrich_tasks", {})
        if drain and tasks.get("__runner__") is not None:
            # the loop keeps running while late briefs arrive; let them finish
            await asyncio.wait_for(tasks["__runner__"], timeout=10)
        done = {k: (t.done() and not t.cancelled() and not t.exception() and t.result())
                for k, t in tasks.items() if k != "__runner__"}
        for t in tasks.values():
            t.cancel()
        return plan, done
    plan, done = asyncio.run(_go())
    return plan, seen, done


BRIEF = ('{"streams":[{"id":1,"title":"Gather","objective":"find facts","caps":["web.research"],'
         '"deliverable":"notes.md"},{"id":2,"title":"Write","objective":"write report",'
         '"dependencies":[1],"caps":["prose.author"],"deliverable":"report.md"}]}')


# ── broad-stepwise: broad's streams, each grown one step at a time ───────────

def test_broad_stepwise_is_broad_planning_with_the_stepwise_controller():
    b = PS.LOOP_STYLES["broad-stepwise"]
    assert b["broad"] and b["stepwise_controller"] and b["stream_steps"] == 1
    assert not b["run_planner"] and not b["master_plan"] and not b["recon"]
    assert not b["shape_guards"]           # they would "repair" the one-step openings
    assert PS.resolve_loop_style("broad-stepwise")[0] == "broad-stepwise"


def test_a_stream_can_be_asked_for_its_opening_step_only():
    streams = PS.parse_streams({"streams": [{"id": 1, "title": "Gather", "objective": "facts"},
                                            {"id": 2, "title": "Write", "objective": "report",
                                             "dependencies": [1]}]})
    one = PS.stream_directive("g", streams, streams[0], first_step_only=True)
    full = PS.stream_directive("g", streams, streams[0])
    assert "FIRST concrete step" in one and "do NOT plan them now" in one
    assert "ordered steps that end in its deliverable" in full and "FIRST" not in full


def test_controller_note_per_style():
    streams = [{"id": 1, "title": "Gather", "deliverable": "notes.md"},
               {"id": 2, "title": "Write", "deliverable": "report.md"}]
    steps = [{"id": 1, "title": "Search the licence news", "piece": 1},
             {"id": 2, "title": "Draft the report", "piece": 2}]
    assert PS.controller_note(PS.LOOP_STYLES["auto"], streams, steps) == ""
    assert PS.controller_note(PS.LOOP_STYLES["broad"], streams, steps) == ""
    assert PS.controller_note(PS.LOOP_STYLES["stepwise"]) == PS.STEPWISE_CONTROLLER_NOTE
    note = PS.controller_note(PS.LOOP_STYLES["broad-stepwise"], streams, steps)
    assert note.startswith("BROAD-STEPWISE MODE")
    assert 'stream 1: Gather -> deliverable: notes.md (opens with step 1 "Search the licence news")' in note
    assert "stream 2: Write -> deliverable: report.md (opens with step 2" in note
    # no stream map (the split failed before the note was built): plain stepwise
    assert PS.controller_note(PS.LOOP_STYLES["broad-stepwise"]) == PS.STEPWISE_CONTROLLER_NOTE


@needs_app
def test_broad_stepwise_plans_each_stream_as_its_opening_step(monkeypatch):
    plan, seen, _ = _run_broad(monkeypatch, BRIEF, stream_steps=1)
    assert [p["max_steps"] for p in seen["plans"]] == [1, 1]
    assert all("FIRST concrete step" in p["directive"] for p in seen["plans"])
    assert all(p["role"] == "stream" and p["prefer_gpu"] for p in seen["plans"])   # still GPU
    assert len(seen["enrich"]) >= 1                                          # still CPU briefs
    d = plan["broad"]
    assert d["per_stream_steps"] == 1
    assert [s["title"] for s in d["stream_map"]] == ["Gather", "Write"]
    assert d["stream_map"][1]["deliverable"] == "report.md"


@needs_app
def test_plain_broad_still_plans_whole_streams(monkeypatch):
    plan, seen, _ = _run_broad(monkeypatch, BRIEF)
    assert all(p["max_steps"] >= 2 for p in seen["plans"])
    assert not any("FIRST concrete step" in p["directive"] for p in seen["plans"])
    assert plan["broad"]["per_stream_steps"] >= 2


@needs_app
def test_the_loop_steers_by_the_style_controller_note():
    """The controller's instruction comes from planner_styles.controller_note,
    and a broad run that fell back to its own planner is steered as auto."""
    import inspect
    src = inspect.getsource(M)
    assert "style_note=_ctrl_style_note" in src
    assert "_plan_styles.controller_note(" in src
    assert "stream_steps=int(_pstyle.get(\"stream_steps\") or 0)" in src
    assert '"shape_guards": True, "stepwise_controller": False}' in src


@needs_app
def test_every_stream_is_planned_on_the_gpu_route_concurrently(monkeypatch):
    plan, seen, _ = _run_broad(monkeypatch, BRIEF)
    assert seen["max_in_flight"] == 2
    assert [p["role"] for p in seen["plans"]] == ["stream", "stream"]
    assert all(p["profile"] == "planning_style" and p["prefer_gpu"] for p in seen["plans"])


@needs_app
def test_the_cpu_briefs_run_beside_the_gpu_plan_on_the_enrich_route(monkeypatch):
    plan, seen, done = _run_broad(monkeypatch, BRIEF, drain=True)
    assert len(seen["enrich"]) == 2
    assert all(e["profile"] == "planning_style" and e["prefer_gpu"] is False for e in seen["enrich"])
    assert seen["enrich_started_before_plans_done"]
    assert all("STREAM BRIEF" in v and "2024" in v for v in done.values())


@needs_app
def test_only_one_heavy_cpu_generation_runs_at_a_time(monkeypatch):
    """User 2026-09-27: one CPU node does heavy generation, one at a time, so the
    embedding/worker node is never taken - two briefs at once would push the
    second off the long-horizon node."""
    _, seen, done = _run_broad(monkeypatch, BRIEF, enrich_delay=0.2, drain=True)
    assert len(seen["enrich"]) == 2 and seen["enrich_max"] == 1
    assert all(done.values())


@needs_app
def test_the_enrich_route_is_the_long_horizon_cpu_job_type():
    from Vera.vera import capability_orchestration as O
    rule = O.DEFAULT_ROUTING_RULES["plan_enrich"]
    assert rule["deny_gpu"] and rule["prefer"] == "cpu-247"          # the long-horizon node
    # the primary embedder is the GPU node's CPU sibling (user, 2026-09-28)
    assert O.DEFAULT_ROUTING_RULES["embedding"]["prefer"] == "gpu-250-cpu"
    assert "plan_enrich" in O.OLLAMA_JOB_TYPES
    role = (O.ROLE_PROFILES_DECLARED.get("planning_style") or {}).get("roles", {}).get("enrich")
    if role:                                                          # planning module loaded
        assert role["job_type"] == "plan_enrich" and role["deny_gpu"]
        assert role["model"] == "qwen3.6:35b-a3b"                    # the user's pick
        assert role["options"].get("keep_alive")                      # kept warm on cpu-247


@needs_app
def test_long_horizon_jobs_queue_on_cpu247_and_never_spill_to_the_embedder(monkeypatch):
    """User 2026-09-27: one CPU node does heavy generation; the other keeps
    embeddings and system work moving. A busy cpu-247 must make a 35B job WAIT
    for it (generation there is one at a time), not load ~23 GB onto cpu-246 -
    only an offline cpu-247 lets the job route elsewhere."""
    from Vera.vera import capability_orchestration as O
    monkeypatch.setattr(O, "_inflight_sweep", lambda: None)

    def _nodes(c247_status="online", c247_busy=1):
        return {
            "gpu-250": {"has_gpu": True, "enabled": True, "status": "online", "in_use": 0,
                        "priority": 0, "models": ["qwen3.6:35b-a3b"]},
            "cpu-246": {"has_gpu": False, "enabled": True, "status": "online", "in_use": 0,
                        "priority": 1, "models": ["qwen3.6:35b-a3b", "nomic-embed-text"]},
            "cpu-247": {"has_gpu": False, "enabled": True, "status": c247_status,
                        "in_use": c247_busy, "priority": 2,
                        "models": ["qwen3.6:35b-a3b", "nomic-embed-text"]},
        }
    for jt in ("dream_director", "plan_enrich", "chat_enrich"):
        assert O.DEFAULT_ROUTING_RULES[jt]["pin"] == O.LONG_HORIZON_CPU_NODE == "cpu-247"
        monkeypatch.setattr(O, "OLLAMA_INSTANCES", _nodes())
        assert O.pick_instance(job_type=jt, model="qwen3.6:35b-a3b") == "cpu-247"   # busy: waits
        monkeypatch.setattr(O, "OLLAMA_INSTANCES", _nodes(c247_status="offline"))
        assert O.pick_instance(job_type=jt, model="qwen3.6:35b-a3b") == "cpu-246"   # down: falls back
    # the embedder's own routing is untouched: still soft, still cpu-246 first
    assert not O.DEFAULT_ROUTING_RULES["embedding"].get("pin")


@needs_app
def test_every_long_horizon_caller_shares_one_runner():
    """User 2026-09-27: the dream director and narrator run the long-horizon model
    too. On a CPU node a different window is a different runner - a 62 s reload
    of ~23 GB - so every job type that runs it must ask for the same window and
    keep it resident."""
    from Vera.vera import capability_orchestration as O
    shared = O.LONG_HORIZON_CPU_OPTIONS
    assert shared.get("num_ctx") and shared.get("keep_alive")
    director = O.DEFAULT_ROUTING_RULES["dream_director"]
    assert director["model"] == O.LONG_HORIZON_CPU_MODEL == "qwen3.6:35b-a3b"
    assert director["deny_gpu"] and director["prefer"] == "cpu-247"
    for jt in ("dream_director", "plan_enrich"):
        opts = O.DEFAULT_ROUTING_RULES[jt].get("options") or {}
        assert opts.get("num_ctx") == shared["num_ctx"] and opts.get("keep_alive") == shared["keep_alive"]
    role = (O.ROLE_PROFILES_DECLARED.get("planning_style") or {}).get("roles", {}).get("enrich")
    if role:
        eff = O._merge_rule_over_base(role, O.DEFAULT_ROUTING_RULES["plan_enrich"])
        assert eff["options"]["num_ctx"] == shared["num_ctx"]
        assert eff["options"]["keep_alive"] == shared["keep_alive"]


@needs_app
def test_nothing_waits_for_a_cpu_brief(monkeypatch):
    """User 2026-09-27: the CPU node is a non-blocking supplicant. A 30 s CPU brief
    must not hold planning at all."""
    import time as _t
    t0 = _t.monotonic()
    plan, _, done = _run_broad(monkeypatch, BRIEF, enrich_delay=30.0)
    assert _t.monotonic() - t0 < 5
    assert plan["steps"] and plan["broad"]["enriched_before_run"] == 0
    assert not any(done.values())                       # still running: applied later by the loop


@needs_app
def test_step_one_starts_with_a_quick_gpu_brief(monkeypatch):
    plan, seen, _ = _run_broad(monkeypatch, BRIEF, enrich_delay=30.0)
    assert len(seen["quick"]) == 1
    q = seen["quick"][0]
    assert q["role"] == "stream" and q["prefer_gpu"] and q["profile"] == "planning_style"
    note = plan["_gpu_briefs"][1]
    assert note.startswith("\n\nSTREAM BRIEF (a quick first look") and plan["broad"]["quick_brief"]


@needs_app
def test_the_run_can_choose_the_brief_model(monkeypatch):
    plan, seen, _ = _run_broad(monkeypatch, BRIEF, enrich_model="qwen2.5:7b", drain=True)
    assert seen["enrich"] and all(e["model"] == "qwen2.5:7b" for e in seen["enrich"])
    assert plan["broad"]["enrich_model"] == "qwen2.5:7b"
    _, seen2, _ = _run_broad(monkeypatch, BRIEF, drain=True)
    assert all(e["model"] == "" for e in seen2["enrich"])         # '' = the route's model

@needs_app
def test_a_failed_cpu_brief_costs_the_plan_nothing(monkeypatch):
    plan, seen, done = _run_broad(monkeypatch, BRIEF, enrich_fail=True)
    assert len(plan["steps"]) == 4 and not any(done.values())
    errs = [e for e in seen["events"] if e["type"] == "agent_loop_v6.broad_stream_enriched"]
    assert len(errs) == 2 and all("cpu node down" in e["error"] for e in errs)


@needs_app
def test_each_stream_is_planned_as_its_own_piece(monkeypatch):
    _, seen, _ = _run_broad(monkeypatch, BRIEF)
    dirs = [p["directive"] for p in seen["plans"]]
    assert any("PLAN ONLY STREAM 1 of 2: Gather" in d for d in dirs)
    assert any("PLAN ONLY STREAM 2 of 2: Write" in d for d in dirs)


@needs_app
def test_the_merged_plan_links_the_write_up_to_the_gathering(monkeypatch):
    plan, seen, _ = _run_broad(monkeypatch, BRIEF)
    steps = plan["steps"]
    assert len(steps) == 4 and plan["broad"]["streams"] == 2
    assert steps[2]["needs"] == [2]
    kinds = [e["type"] for e in seen["events"]]
    assert "agent_loop_v6.broad_streams" in kinds
    assert kinds.count("agent_loop_v6.broad_stream_planned") == 2


@needs_app
def test_no_streams_means_no_steps_so_the_loop_falls_back(monkeypatch):
    plan, _, _ = _run_broad(monkeypatch, '{"streams": []}')
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


def test_the_loop_applies_a_brief_to_each_step_of_its_stream_before_it_runs():
    b = _body("cap_dag_agent_loop_v6")
    apply_at = b.index('step["goal"] = str(step.get("goal") or "") + _brief')
    assert '_brief, _kind = _et.result(), "deep"' in b and "elif _gpu_briefs.get(_pc):" in b
    run_at = b.index("res = await _run_one(step, gcycle)")
    assert apply_at < run_at
    assert '_enrich_tasks = plan.pop("_enrich_tasks", {}) or {}' in b


def test_leftover_briefs_are_cancelled_when_v6_ends_or_broad_falls_back():
    b = _body("cap_dag_agent_loop_v6")
    ret = b.rindex('"plan_style": _plan_style_rec,')
    assert b.rindex("_t.cancel()", 0, ret) > b.index("# BROAD: a CPU brief still running")
    assert "for _t in _enrich_tasks.values():\n                _t.cancel()" in b


def test_v5_never_touches_broads_tasks():
    """The v5 and v6 returns open with the same lines; a cleanup once landed in v5
    by mistake, where _enrich_tasks does not exist - every v5 run would crash."""
    assert "_enrich_tasks" not in _body("cap_dag_agent_loop_v5")

def test_a_routes_keep_alive_is_lifted_out_of_the_sampling_options():
    """keep_alive is an Ollama REQUEST field; left inside options it would be sent
    as a bogus sampling option and the model would not be kept warm."""
    src = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
    lift = src.index('_route_ka = _merged_opts.pop("keep_alive", None)')
    send = src.index('body["options"] = _merged_opts')
    assert lift < send
    assert 'if keep_alive is None and _route_ka not in (None, ""):' in src