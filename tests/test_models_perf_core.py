"""Models and Performance in Loop Lab (owner, 18 Oct 2026): the pure shaping in models_perf_core.

The fixtures are trimmed copies of what prod returned on 18 Oct 2026 (bench.passive, ollama.route_stats,
bench.results, decide.shadow.report, census.runs, background.status, chat.corpus.last, obs.health).
"""
from datetime import datetime, timezone

from vera.evolve import models_perf_core as M

NOW = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)

PASSIVE = {"total_calls": 49430, "rows": [
    {"model": "jaahas/qwen3.5-uncensored", "instance_id": "gpu-250", "node_label": "GPU Node", "has_gpu": True,
     "calls": 27478, "job_types": ["chat", "loop_executor"], "obs_tps": 30.6, "avg_latency_s": 55.07},
    {"model": "jaahas/qwen3.5-uncensored:9b", "instance_id": "gpu-250", "has_gpu": True, "calls": 14529,
     "obs_tps": 20.5, "avg_latency_s": 73.11},
    {"model": "qwen2.5-coder:14b", "instance_id": "gpu-250", "calls": 662, "obs_tps": 0.5, "avg_latency_s": 456.78,
     "last_ts": "2026-09-10T10:36:19Z"},
    {"model": "qwen3-coder:30b", "instance_id": "cpu-246", "calls": 339, "obs_tps": 5.3, "avg_latency_s": 117.79,
     "last_ts": "2026-10-18T08:43:21Z"}]}
ROUTE = {"stats": [
    {"model": "jaahas/qwen3.5-uncensored:9b", "instance": "gpu-250", "job_type": "loop_executor", "n": 12945, "ema_elapsed_s": 16.5, "ema_tps": 7.5, "ema_tokens": 124},
    {"model": "jaahas/qwen3.5-uncensored", "instance": "gpu-250", "job_type": "chat", "n": 12273, "ema_elapsed_s": 64.5, "ema_tps": 9.9, "ema_tokens": 738, "ema_prompt_chars": 7814},
    {"model": "jaahas/qwen3.5-uncensored", "instance": "gpu-250", "job_type": "loop_planner", "n": 5825, "ema_elapsed_s": 31.9, "ema_tps": 10.3},
    {"model": "qwen3:0.6b", "instance": "gpu-250", "job_type": "idle_decide", "n": 1392, "ema_elapsed_s": 0.4, "ema_tps": 2.3},
    {"model": "qwen3.6:35b-a3b", "instance": "cpu-247", "job_type": "plan_enrich", "n": 1051, "ema_elapsed_s": 68.7, "ema_tps": 1.9},
    {"model": "jaahas/qwen3.5-uncensored", "instance": "gpu-250", "job_type": "code", "n": 1580, "ema_elapsed_s": 29.6, "ema_tps": 16.2}]}
BENCH = {"results": [{"id": "bench-c3f7a031", "created_at": "2026-08-23T00:37:01Z", "instance_id": "gpu-250",
                      "model": "jaahas/qwen3.5-uncensored:9b", "roles": ["instruct", "code"], "accuracy": 0.0,
                      "items_total": 29, "items_passed": 0,
                      "perf": {"gen_tps": 42.5, "ttft_ms": 3910.3, "load_ms": 5621.3}, "quant": "Q6_K"}]}
NODES = {"nodes": [{"id": "gpu-250", "label": "GPU Node", "status": "online", "has_gpu": True,
                    "loaded": [{"model": "jaahas/qwen3.5-uncensored:9b"}]},
                   {"id": "cpu-246", "status": "online", "loaded": [{"model": "qwen3-coder:30b"}, {"model": "jaahas/qwen3.5-uncensored:latest"}]}]}
SHADOW = {"loop.step_verify": {"question_id": "loop.step_verify", "live_vs_outcomes": {"n": 103, "accuracy": 1.0},
                               "by_implementation": {
                                   "knn:minilm": {"n": 799, "agreement": 0.782, "vs_outcomes": {"n": 100, "accuracy": 0.26}},
                                   "llm:qwen3:4b-instruct-2507-q4_K_M": {"n": 345, "agreement": 0.551, "ms_p50": 1069, "vs_outcomes": {"n": 100, "accuracy": 0.977}},
                                   "llm:qwen3:1.7b@cpu-247": {"n": 13, "agreement": 0.769, "vs_outcomes": {"n": 3, "accuracy": 1.0}}}},
          "intel.enrich_worth": {"error": "no data"}}


def test_job_groups():
    assert [M.job_group(j) for j in ("loop_coder", "chat", "idle_decide", "plan_enrich", "code", "")] == \
        ["loop", "chat", "background", "background", "other", "other"]


def test_a_benchmark_where_nothing_passed_is_broken_not_zero():
    b = M.bench_row(BENCH["results"][0], NOW)
    assert b["broken"] and "0 of 29" in b["broken"]
    assert b["accuracy"] is None                       # never shown as a 0% score
    assert b["age_days"] == 56.4 and b["gen_tps"] == 42.5


def test_model_rows_join_live_bench_and_warm():
    rows = M.model_rows(PASSIVE["rows"], ROUTE["stats"], BENCH["results"], M.warm_set(NODES), NOW)
    assert [r["model"] for r in rows][:2] == ["jaahas/qwen3.5-uncensored", "jaahas/qwen3.5-uncensored:9b"]
    big, nine = rows[0], rows[1]
    assert [j["job"] for j in big["jobs"]] == ["chat", "loop_planner", "code"]   # most calls first
    assert big["bench"] is None and nine["bench"]["id"] == "bench-c3f7a031"
    assert nine["warm"] is True and big["warm"] is False
    assert next(r for r in rows if r["node"] == "cpu-246")["warm"] is True


def test_role_matrix():
    mx = M.role_matrix(ROUTE["stats"], max_models=3)
    assert mx["models"][0] == "jaahas/qwen3.5-uncensored@gpu-250"           # 12273 + 5825 + 1580 calls
    assert "qwen3.6:35b-a3b@cpu-247" not in mx["models"]                     # cut by max_models
    assert mx["cells"]["chat"]["jaahas/qwen3.5-uncensored@gpu-250"]["wall_s"] == 64.5
    assert "plan_enrich" not in mx["jobs"]                                   # no cell left for it


def test_students_leader_and_copying_bias():
    rows = M.student_rows(SHADOW)
    assert {r["question"] for r in rows} == {"loop.step_verify"}             # the errored report is skipped
    lead = [r for r in rows if r["leader"]]
    assert len(lead) == 1 and lead[0]["impl"].startswith("llm:qwen3:4b")
    knn = next(r for r in rows if r["impl"] == "knn:minilm")
    assert knn["agrees_not_right"] is True
    small = next(r for r in rows if r["impl"].startswith("llm:qwen3:1.7b"))
    assert small["leader"] is False                                          # 3 outcomes is too few to lead
    assert rows[0]["impl"].startswith("llm:qwen3:4b")


def test_models_view_summary_and_issues():
    v = M.models_view(PASSIVE, ROUTE, BENCH, NODES, SHADOW, now=NOW)
    s = v["summary"]
    assert s["calls"] == 49430 and s["bench_runs"] == 1
    assert s["fastest"]["model"] == "jaahas/qwen3.5-uncensored"
    assert s["slowest"]["model"] == "qwen3-coder:30b"     # coder:14b is slower but last used 10 Sep: history
    old = next(r for r in v["models"] if r["model"] == "qwen2.5-coder:14b")
    assert old["stale"] is True and old["last_age_days"] == 38.0
    whats = [i["what"] for i in v["issues"]]
    assert whats[0] == "benchmarks" and "56 days" in v["issues"][0]["why"]
    assert any(w.startswith("benchmark bench-c3f7a031") for w in whats)


CENSUS = {"runs": [
    {"run_id": "run88", "goals": 12, "done": 11, "wall_capped": 1, "wall_total_s": 8728.7, "quality_mean": 1.0, "quality_scored": 12, "ended_at": NOW.timestamp() - 3600},
    {"run_id": "run87", "goals": 12, "done": 9, "wall_capped": 2, "wall_total_s": 7200.0, "quality_mean": 0.5, "quality_scored": 12, "ended_at": NOW.timestamp() - 2 * 86400},
    {"run_id": "run80", "goals": 10, "done": 5, "wall_total_s": 6000.0, "quality_mean": 0.8, "quality_scored": 10, "ended_at": NOW.timestamp() - 9 * 86400},
    {"run_id": "partial", "goals": 3, "done": 0, "excluded": True, "ended_at": NOW.timestamp() - 3600}]}


def test_census_kpis_window_previous_and_series():
    c = M.census_kpis(CENSUS, NOW, days=7)
    assert (c["runs"], c["goals"], c["done"]) == (2, 24, 20)
    assert c["done_rate"] == round(20 / 24, 3) and c["quality"] == 0.75
    assert c["wall_capped"] == 3 and c["wall_per_goal_min"] == round(statistics_median([8728.7 / 12 / 60, 7200 / 12 / 60]), 1)
    assert c["prev"]["runs"] == 1 and c["prev"]["done_rate"] == 0.5
    assert len(c["series"]) == 7 and c["series"][-1]["runs"] == 1


def statistics_median(xs):
    import statistics
    return statistics.median(xs)


REL = {"columns": {"gating": [{"id": "g1", "tests": {}, "started_at": "2026-10-18T09:00:00Z", "last_at": "2026-10-18T09:05:00Z"}],
                   "edge": [{"id": "e1", "tests": {"ok": True, "passed": 7670}, "minutes": 12.0, "started_at": "2026-10-18T06:00:00Z", "last_at": "2026-10-18T06:12:00Z"}],
                   "released": [{"id": "r1", "tests": {"ok": True}, "minutes": 10.0, "started_at": "2026-10-17T20:00:00Z", "last_at": "2026-10-17T20:10:00Z"}],
                   "failed": [{"id": "f1", "tests": {"ok": False, "failed": 1}, "minutes": 8.0, "started_at": "2026-10-16T10:00:00Z", "last_at": "2026-10-16T10:08:00Z"},
                              {"id": "old", "tests": {"ok": False}, "minutes": 9.0, "started_at": "2026-09-01T10:00:00Z", "last_at": "2026-09-01T10:09:00Z"}]},
       "releases": [{"id": "rel1", "finished_at": "2026-10-18T02:00:00Z", "carried": [{"sha": "abc", "pipeline": "r1"}]}],
       "edge_ahead": 4}


def test_gate_kpis_pass_rate_and_lead_time():
    g = M.gate_kpis(REL, NOW, days=7)
    assert (g["gates"], g["passed"], g["failed"]) == (3, 2, 1)              # "old" is outside the week; g1 untested
    assert g["pass_rate"] == round(2 / 3, 3) and g["gate_min"] == 10.0
    assert g["lead_h"] == 6.0 and g["lead_n"] == 1                          # r1: adopted 20:00, released 02:00
    assert g["on_edge"] == 1 and g["edge_ahead"] == 4 and g["releases"] == 1


BG = {"jobs": [{"name": "ide.claude_sessions.ingest", "runs": 0, "defers": 10,
                "last_defer": "only 542s of quiet so far, need 600s"}, {"name": "ok.job", "runs": 3, "defers": 1}],
      "queue": {"depth": 22, "running": {"title": "shadow judge", "waiting_for_s": 39001, "stranded": 1},
                "waiting": [{"title": "intel", "waiting_for_s": 32346, "stranded": 0}]}}


def test_queue_kpis():
    q = M.queue_kpis(dict(BG, min_quiet_s=600))
    assert q["depth"] == 22 and q["oldest_h"] == 10.8 and q["stranded"] == 1 and q["min_quiet_s"] == 600
    assert "quiet_gated" not in q          # the jobs row is the drain's own label, not held-back work (18 Oct)


CORPUS = {"run": {"run_id": "20261011T130117", "finished": "2026-10-11T14:37:43+00:00"},
          "summary": {"cases": 30, "status": {"error": 30}, "pass_rate": None,
                      "errors": [{"id": 1, "reason": "the page sent no chat turn"}]}}


def test_corpus_all_errors_is_broken_and_stale():
    c = M.corpus_kpi(CORPUS, NOW)
    assert c["broken"].startswith("every case errored (30 of 30)") and "no chat turn" in c["broken"]
    assert c["pass_rate"] is None and c["stale"] is False and c["age_days"] == 6.8


def test_chat_share_services_and_whole_view():
    ch = M.chat_kpis(ROUTE)
    assert ch["replies"] == 12273 and ch["wall_s"] == 64.5 and ch["tokens"] == 738
    sh = M.share(ROUTE)
    pct = {g["group"]: g["pct"] for g in sh["groups"]}
    assert sh["total"] == 35066 and pct["loop"] == round(100 * 18770 / 35066, 1) and pct["chat"] == round(100 * 12273 / 35066, 1)
    sv = M.services({"redis": True, "postgres": True, "chroma": False, "neo4j": True,
                     "ollama": {"gpu-250": {"status": "online"}, "cpu-246": {"status": "offline"}}})
    assert (sv["ok"], sv["of"]) == (4, 6)
    v = M.performance_view(CENSUS, REL, BG, ROUTE, CORPUS, {}, days=7, now=NOW, bench=BENCH)
    whats = [b["what"] for b in v["broken"]]
    assert whats[0].startswith("benchmark bench-c3f7a031")
    assert "chat test corpus" in whats and not any("quiet" in w for w in whats)
    assert v["loop_lab"]["gates"]["passed"] == 2 and v["vera_use"]["chat"]["replies"] == 12273
