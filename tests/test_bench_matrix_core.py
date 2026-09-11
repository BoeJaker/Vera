"""Pure tests for bench_matrix_core — planning and scoring behind bench.matrix.

Imports lowercase `vera.catalog.bench_matrix_core` with the repo root on
sys.path so the WORKTREE copy is exercised (`Vera.vera.…` resolves to the main
checkout on the host).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.catalog.bench_matrix_core import (  # noqa: E402
    BASE_PROMPT, CHARS_PER_TOKEN, DEFAULT_CTXS, apply_plan, build_grid, cell_prompt,
    cell_verdict, group_variants, normalise_ctxs, normalise_models, recommend,
    residency, summarise_runs)

pytestmark = pytest.mark.critical


def test_cell_prompt_leads_with_the_nonce_so_the_kv_cache_cannot_match():
    a = cell_prompt(8192, 0, 192, "a1")
    b = cell_prompt(8192, 0, 192, "b2")
    assert a.startswith("[bench a1]\n") and b.startswith("[bench b2]\n")
    assert a[:12] != b[:12]
    assert a.endswith(BASE_PROMPT)


def test_cell_prompt_fills_the_requested_share_and_leaves_room_to_answer():
    p = cell_prompt(8192, 0.5, 192, "n")
    # 50% of 8192 minus the 192-token output and the 64-token template allowance.
    expected_chars = (8192 // 2 - 192 - 64) * CHARS_PER_TOKEN
    assert abs(len(p) - expected_chars) < 50
    assert p.startswith("[bench n]\n") and p.endswith(BASE_PROMPT)
    assert len(p) / CHARS_PER_TOKEN + 192 < 8192


def test_cell_prompt_clamps_fill_and_falls_back_when_the_window_is_tiny():
    assert cell_prompt(8192, 5, 192, "n") == cell_prompt(8192, 0.9, 192, "n")
    assert cell_prompt(512, 0.25, 192, "n") == "[bench n]\n" + BASE_PROMPT


def test_normalise_models_accepts_list_or_string_and_dedupes():
    assert normalise_models("a, b ,a,,") == ["a", "b"]
    assert normalise_models(["x", "x ", " "]) == ["x"]
    assert normalise_models(None) == []


def test_normalise_ctxs_defaults_sorts_and_filters():
    assert normalise_ctxs(None) == list(DEFAULT_CTXS)
    assert normalise_ctxs("8192, 4096,4096, 100, abc") == [4096, 8192]
    # A window the node cannot hold is dropped rather than attempted.
    assert normalise_ctxs([4096, 65536], ceiling=32768) == [4096]


def test_build_grid_orders_model_then_ascending_window():
    cells, dropped = build_grid(["m1", "m2"], [4096, 8192])
    assert [(c["model"], c["num_ctx"]) for c in cells] == [
        ("m1", 4096), ("m1", 8192), ("m2", 4096), ("m2", 8192)]
    assert dropped == []


def test_build_grid_drops_whole_models_not_half_sweeps():
    cells, dropped = build_grid(["m1", "m2", "m3"], [1, 2, 3, 4, 5], max_cells=10)
    assert len(cells) == 10
    assert dropped == ["m3"]
    assert {c["model"] for c in cells} == {"m1", "m2"}


def test_summarise_runs_separates_load_from_warm_speed():
    runs = [
        {"load_ms": 9800, "gen_tps": 20.0, "prompt_tps": 300, "ttft_ms": 10500},
        {"load_ms": 600, "gen_tps": 65.0, "prompt_tps": 640, "ttft_ms": 700},
        {"load_ms": 620, "gen_tps": 66.0, "prompt_tps": 650, "ttft_ms": 710},
        {"load_ms": 610, "gen_tps": 64.0, "prompt_tps": 645, "ttft_ms": 705},
    ]
    s = summarise_runs(runs)
    assert s["load_ms"] == 9800          # the reload is reported, not averaged in
    assert s["gen_tps"] == 65.0          # median of the warm calls only
    assert s["prompt_tps"] == 645.0
    assert s["ttft_ms"] == 705.0
    assert s["calls"] == 4
    assert s["gen_spread_pct"] == pytest.approx(3.1, abs=0.05)


def test_summarise_runs_single_call_and_empty():
    assert summarise_runs([{"load_ms": 900, "gen_tps": 60.0}])["gen_tps"] == 60.0
    assert summarise_runs([]) == {}


def test_residency_matches_implicit_latest_tag():
    ps = [{"name": "phi4:latest", "size": 9_350_000_000, "size_vram": 7_790_000_000,
           "context_length": 32768},
          {"name": "qwen3.5:9b", "size": 6_440_000_000, "size_vram": 6_440_000_000}]
    assert residency(ps, "qwen3.5:9b")["resident_pct"] == 100.0
    r = residency(ps, "phi4")
    assert r["resident_pct"] == pytest.approx(83.3, abs=0.05)
    assert r["context_length"] == 32768
    assert residency(ps, "absent:1b") == {}


def test_spill_only_counts_on_a_gpu_node():
    cell = {"resident_pct": 83.3, "gen_tps": 33.4}
    assert cell_verdict(cell, has_gpu=True)[0] == "spill"
    # A CPU node has no VRAM to spill out of.
    assert cell_verdict(cell, has_gpu=False)[0] == "ok"


def test_verdict_error_throttle_and_cpu_bound():
    assert cell_verdict({"error": "HTTP 500"}, True)[0] == "error"
    assert cell_verdict({"gen_tps": 0, "resident_pct": 100.0}, True)[0] == "error"
    throttled = {"gen_tps": 57.0, "resident_pct": 100.0,
                 "trace": {"throttle_reasons": ["sw_thermal_slowdown"]}}
    assert cell_verdict(throttled, True) == (
        "throttled", "clock throttled during this cell (sw_thermal_slowdown)")
    cpu = {"gen_tps": 9.0, "trace": {"cpu_pct": {"peak": 99.0}}}
    assert cell_verdict(cpu, False)[0] == "cpu_bound"


def test_recommend_picks_largest_clean_window_before_the_spill():
    """The sweep recorded in Vera's .env for gpu-250: 32768 spilled 17% onto the
    CPU and fell to 33.4 tok/s; 28672 was the largest window still fully on the
    GPU at full speed."""
    cells = [
        {"model": "q", "num_ctx": 16384, "gen_tps": 102.2, "status": "ok"},
        {"model": "q", "num_ctx": 24576, "gen_tps": 106.1, "status": "ok"},
        {"model": "q", "num_ctx": 28672, "gen_tps": 105.8, "status": "ok"},
        {"model": "q", "num_ctx": 32768, "gen_tps": 33.4, "status": "spill"},
    ]
    (rec,) = recommend(cells)
    assert rec["num_ctx"] == 28672
    assert rec["first_spill_ctx"] == 32768
    assert "spills" in rec["reason"]


def test_recommend_respects_tolerance_and_effective_window():
    slow_big = [{"model": "m", "num_ctx": 4096, "gen_tps": 60.0, "status": "ok"},
                {"model": "m", "num_ctx": 8192, "gen_tps": 50.0, "status": "ok"}]
    assert recommend(slow_big)[0]["num_ctx"] == 4096
    capped = [{"model": "c", "num_ctx": 65536, "effective_ctx": 32768,
               "gen_tps": 40.0, "status": "ok"}]
    rec = recommend(capped)[0]
    assert rec["num_ctx"] == 32768 and rec["requested_ctx"] == 65536


def test_recommend_reports_when_nothing_ran_cleanly():
    rec = recommend([{"model": "x", "num_ctx": 4096, "status": "spill", "gen_tps": 5.0}])[0]
    assert rec["num_ctx"] is None
    assert rec["first_spill_ctx"] == 4096


def test_group_variants_groups_quantisations_and_skips_encoders():
    tags = [
        {"name": "qwen3.5:9b", "size": 6_590_000_000,
         "details": {"family": "qwen35", "parameter_size": "9.7B",
                     "quantization_level": "Q4_K_M"}},
        {"name": "qwen3.5:9b-q8_0", "size": 10_400_000_000,
         "details": {"family": "qwen35", "parameter_size": "9.7B",
                     "quantization_level": "Q8_0"}},
        {"name": "jaahas/qwen3.5-uncensored:9b", "size": 7_360_000_000,
         "details": {"family": "qwen35", "parameter_size": "9.0B",
                     "quantization_level": "Q6_K"}},
        {"name": "nomic-embed-text:latest", "size": 270_000_000,
         "details": {"family": "nomic-bert", "parameter_size": "137M",
                     "quantization_level": "F16"}},
    ]
    groups = group_variants(tags)
    assert [(g["family"], g["params"], len(g["variants"])) for g in groups] == [
        ("qwen35", "9.7B", 2), ("qwen35", "9.0B", 1)]
    assert [v["quant"] for v in groups[0]["variants"]] == ["Q4_K_M", "Q8_0"]


def test_group_variants_folds_tags_that_share_a_blob_into_aliases():
    """Seen live on gpu-250: mistral:7b and mistral:latest are the same weights."""
    same = {"size": 4_370_000_000, "digest": "sha256:f974a74358d6",
            "details": {"family": "llama", "parameter_size": "7.2B",
                        "quantization_level": "Q4_K_M"}}
    tags = [dict(same, name="mistral:7b"), dict(same, name="mistral:latest"),
            {"name": "mistral:7b-q8_0", "size": 7_700_000_000, "digest": "sha256:0b1",
             "details": {"family": "llama", "parameter_size": "7.2B",
                         "quantization_level": "Q8_0"}}]
    (group,) = group_variants(tags)
    assert [(v["name"], v["aliases"]) for v in group["variants"]] == [
        ("mistral:7b", ["mistral:latest"]), ("mistral:7b-q8_0", [])]


def test_apply_plan_adopts_a_measured_window_over_an_estimate():
    """Vera only consults its pre-load VRAM estimate when there is no learned
    window, so adopting a measurement also takes the estimator out of the path."""
    recs = [{"model": "q", "num_ctx": 28672, "gen_tps": 105.8}]
    (row,) = apply_plan(recs, "gpu-250", learned={})
    assert row["action"] == "set"
    assert (row["instance_id"], row["model"], row["num_ctx"]) == ("gpu-250", "q", 28672)
    assert row["previous"] is None
    assert "105.8" in row["reason"]


def test_apply_plan_never_guesses_a_window_the_sweep_could_not_measure():
    recs = [{"model": "q", "num_ctx": None}]
    (row,) = apply_plan(recs, "gpu-250", learned={})
    assert row["action"] == "skip"
    assert "cleanly" in row["reason"]


def test_apply_plan_skips_what_is_already_learned_and_honours_a_filter():
    recs = [{"model": "a", "num_ctx": 8192}, {"model": "b", "num_ctx": 4096}]
    rows = apply_plan(recs, "n1", learned={"n1::a": 8192})
    assert [r["action"] for r in rows] == ["skip", "set"]
    assert rows[0]["reason"] == "already the learned window"
    only = apply_plan(recs, "n1", learned={}, only=["b"])
    assert [r["model"] for r in only] == ["b"]
