"""Pure tests for node_telemetry_core — CPU tracing, the Ollama access log and
the process snapshot behind bench.node_trace and bench.node_requests.

Imports lowercase `vera.catalog.node_telemetry_core` with the repo root on
sys.path so the WORKTREE copy is exercised (`Vera.vera.…` resolves to the main
checkout on the host).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.catalog.node_telemetry_core import (  # noqa: E402
    POLL_PATHS, cpu_trace_script, cpu_trace_summary, parse_cpu_trace, parse_gin_line,
    parse_latency_s, parse_request_log, parse_top_snapshot, percentile,
    request_log_script, summarise_processes, summarise_requests)

pytestmark = pytest.mark.critical

# Captured from cpu-246 (Ollama-B, an LXC container) on 2026-09-10 between
# requests. /proc/loadavg reads 36.62 because it is the HOST's load; the cgroup
# counter shows the container itself almost idle.
CPU_CAPTURE = """T 1789079225.575864098
cpu  239380934 103740 8730555 589207231 8890337 0 166088 0 15849 0
36.62 39.88 40.74 14/4720 151408
MemTotal:       52428800 kB
MemAvailable:   50724792 kB
CG usage_usec 2371385068707 MEMCUR 1889820672 MEMMAX max
NCPU 12
T 1789079226.594701910
cpu  239380934 103740 8730560 589208431 8890347 0 166088 0 15849 0
36.62 39.88 40.74 4/4720 151422
MemTotal:       52428800 kB
MemAvailable:   50725440 kB
CG usage_usec 2371385084274 MEMCUR 1888866304 MEMMAX max
NCPU 12
T 1789079227.607149808
cpu  239380937 103741 8730567 589209567 8890411 0 166088 0 15849 0
36.62 39.88 40.74 3/4723 151529
MemTotal:       52428800 kB
MemAvailable:   50724216 kB
CG usage_usec 2371385099553 MEMCUR 1890099200 MEMMAX max
NCPU 12
"""

# Captured from cpu-246 with `top -b -n 2 -d 0.5 -w 256`, second iteration only,
# while llama-server was generating on all 12 cores. Note the comma inside TIME+.
TOP_CAPTURE = """    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
1077524 root      20   0 3235048 542228   2304 R  1200   1.0     56,05 llama-server
      1 root      20   0  168884   8448   5376 S   0.0   0.0   0:18.04 systemd
     44 root      20   0  278948 164432 163664 S   0.0   0.3   6:07.75 systemd-journal
     83 systemd+  20   0   17904   4224   3072 S   0.0   0.0   0:00.93 systemd-network
    112 root      20   0    3608   2688   2688 S   0.0   0.0   0:03.41 cron
"""

# Real [GIN] lines from gpu-250's ollama-vera.service journal.
GIN_SHOW = ('[GIN] 2026/09/10 - 22:24:52 | 200 |    5.035706ms |   192.168.0.138 | '
            'POST     "/api/show"')
GIN_GEN_90 = ('[GIN] 2026/09/10 - 21:25:49 | 200 |         1m30s |   192.168.0.138 | '
              'POST     "/api/generate"')
GIN_GEN_124 = ('[GIN] 2026/09/10 - 21:27:54 | 200 |          2m4s |   192.168.0.138 | '
               'POST     "/api/generate"')
# Constructed: a client other than Vera. No such traffic existed when captured.
GIN_EXT_OK = ('[GIN] 2026/09/10 - 22:30:01 | 200 |          2.5s |    192.168.0.77 | '
              'POST     "/api/chat"')
GIN_EXT_ERR = ('[GIN] 2026/09/10 - 22:30:09 | 500 |       812.4ms |    192.168.0.77 | '
               'POST     "/api/chat"')


# ── CPU trace ────────────────────────────────────────────────────────────────

def test_cpu_script_substitutes_its_placeholder():
    assert "seq 1 5" in cpu_trace_script(5)
    assert "__" not in cpu_trace_script(5)
    assert "seq 1 2" in cpu_trace_script(1)   # a delta needs two samples


def test_parse_cpu_trace_reads_every_field():
    samples = parse_cpu_trace(CPU_CAPTURE)
    assert len(samples) == 3
    s0 = samples[0]
    assert s0["ncpu"] == 12
    assert s0["load1"] == 36.62
    assert s0["mem_total_kb"] == 52428800
    assert s0["cg_usage_usec"] == 2371385068707
    assert s0["mem_current"] == 1889820672


def test_cpu_summary_uses_the_cgroup_counter_not_host_load():
    s = cpu_trace_summary(parse_cpu_trace(CPU_CAPTURE))
    assert s["source"] == "cgroup"
    assert s["ncpu"] == 12
    assert s["cpu_pct"]["peak"] < 1           # container idle...
    assert s["host_load1"]["start"] == 36.62  # ...while the host is busy
    assert "host-wide" in s["host_load1"]["note"]
    assert s["cores_busy"]["mean"] == pytest.approx(0.015, abs=0.01)
    assert s["mem_total_gb"] == 50.0
    assert s["mem_used_gb"]["peak"] == 1.63
    assert s["status"] == "ok"


def test_cpu_summary_falls_back_to_proc_stat_without_cgroup():
    text = "\n".join(l for l in CPU_CAPTURE.splitlines() if not l.startswith("CG "))
    s = cpu_trace_summary(parse_cpu_trace(text))
    assert s["source"] == "procstat"
    assert 0 < s["cpu_pct"]["peak"] < 2


def test_cpu_summary_flags_a_saturated_node_as_cpu_bound():
    text = "T 100.0\nCG usage_usec 0\nNCPU 4\nT 101.0\nCG usage_usec 3900000\nNCPU 4\n"
    s = cpu_trace_summary(parse_cpu_trace(text))
    assert s["cpu_pct"]["peak"] == 97.5
    assert s["status"] == "cpu_bound"
    assert "CPU-bound" in s["verdict"]


def test_cpu_summary_is_empty_rather_than_idle_without_two_samples():
    one = CPU_CAPTURE.split("T 1789079226")[0]
    assert cpu_trace_summary(parse_cpu_trace(one)) == {}
    assert cpu_trace_summary(parse_cpu_trace("")) == {}


# ── Access log ───────────────────────────────────────────────────────────────

def test_parse_latency_handles_every_go_duration_form_in_the_log():
    assert parse_latency_s("42.075µs") == pytest.approx(42.075e-6)   # micro sign
    assert parse_latency_s("42.075μs") == pytest.approx(42.075e-6)   # greek mu
    assert parse_latency_s("5.035706ms") == pytest.approx(0.005035706)
    assert parse_latency_s("1.5s") == 1.5
    assert parse_latency_s("1m30s") == 90.0
    assert parse_latency_s("2m4s") == 124.0
    assert parse_latency_s("1h2m3s") == 3723.0


def test_parse_latency_rejects_partial_or_unitless_text():
    for bad in ("", "12", "fast", "1m30", None):
        assert parse_latency_s(bad) is None


def test_parse_gin_line_real_lines():
    r = parse_gin_line(GIN_SHOW)
    assert r["ts"] == "2026-09-10 22:24:52"
    assert (r["status"], r["client"], r["method"], r["path"]) == (
        200, "192.168.0.138", "POST", "/api/show")
    assert r["inference"] is False
    g = parse_gin_line(GIN_GEN_90)
    assert g["inference"] is True and g["latency_s"] == 90.0


def test_parse_gin_line_strips_query_tolerates_prefix_and_rejects_other_lines():
    q = parse_gin_line('[GIN] 2026/09/10 - 21:25:49 | 200 |      12ms |   ::1 | '
                       'GET      "/api/tags?x=1"')
    assert q["path"] == "/api/tags" and q["client"] == "::1"
    prefixed = parse_gin_line("Sep 10 22:24:52 Ollama ollama[306241]: " + GIN_SHOW)
    assert prefixed["path"] == "/api/show"
    assert parse_gin_line("llama_context: n_ctx_seq = 28672") is None


def test_request_log_script_substitutes_and_matches_poll_paths():
    s = request_log_script(11435, 60, 200)
    assert "sport = :11435" in s
    assert '--since "-60min"' in s
    assert "tail -n 200" in s
    assert "__" not in s
    # The node-side poll filter and POLL_PATHS must describe the same endpoints.
    assert '"/api/(ps|version|tags)"' in s
    assert set(POLL_PATHS) == {"/api/ps", "/api/version", "/api/tags"}


LOG = "\n".join([
    "UNIT ollama-vera.service", "NCPU 12", "TOTAL 5003",
    "POLL 192.168.0.138 4990", "POLL 192.168.0.77 6", "WORKTOTAL 7", "WORK",
    GIN_SHOW, GIN_GEN_90, GIN_GEN_124, GIN_EXT_OK, GIN_EXT_ERR, "TOP",
]) + "\n" + TOP_CAPTURE


def test_parse_request_log_sections():
    log = parse_request_log(LOG)
    assert log["unit"] == "ollama-vera.service"
    assert (log["ncpu"], log["total"], log["work_total"]) == (12, 5003, 7)
    assert log["polls"] == {"192.168.0.138": 4990, "192.168.0.77": 6}
    assert len(log["rows"]) == 5
    assert len(log["processes"]) == 5


def test_summary_surfaces_clients_other_than_vera_first():
    s = summarise_requests(parse_request_log(LOG), vera_ips=["192.168.0.138"])
    assert s["external_clients"] == 1
    assert s["external_inference_calls"] == 2
    ext, vera = s["by_client"][0], s["by_client"][1]
    assert ext["client"] == "192.168.0.77" and not ext["is_vera"]
    assert (ext["errors"], ext["polls"], ext["p95_s"]) == (1, 6, 2.5)
    assert vera["is_vera"] and vera["inference"] == 2 and vera["requests"] == 3
    assert (vera["p50_s"], vera["p95_s"], vera["max_s"]) == (90.0, 124.0, 124.0)
    assert s["inference_calls"] == 4
    assert s["polls"] == 4996
    # The window held 7 non-poll requests but only 5 came back.
    assert s["truncated"] is True
    assert [r["latency_s"] for r in s["long_requests"]] == [124.0, 90.0]
    assert s["by_endpoint"][0]["path"] == "/api/generate"
    chat = next(e for e in s["by_endpoint"] if e["path"] == "/api/chat")
    assert chat["statuses"] == {"200": 1, "500": 1}
    assert chat["errors"] == 1
    assert "other than Vera" in s["verdict"]


def test_summary_when_only_vera_called():
    log = parse_request_log("\n".join(["UNIT ollama-vera.service", "WORKTOTAL 2", "WORK",
                                       GIN_SHOW, GIN_GEN_90]))
    s = summarise_requests(log, vera_ips=["192.168.0.138"])
    assert s["external_clients"] == 0
    assert s["truncated"] is False
    assert s["verdict"] == "Every request in this window came from Vera."


def test_summary_says_so_when_the_service_was_not_found():
    s = summarise_requests(parse_request_log("UNIT\nNCPU 12\nTOP\n"))
    assert s["unit"] == ""
    assert "Could not find" in s["verdict"]


# ── Processes ────────────────────────────────────────────────────────────────

def test_parse_top_snapshot_real_capture():
    rows = parse_top_snapshot(TOP_CAPTURE)
    assert len(rows) == 5
    assert rows[0] == {"pid": 1077524, "user": "root", "cpu_pct": 1200.0,
                       "mem_pct": 1.0, "command": "llama-server"}
    assert rows[3]["user"] == "systemd+"


def test_parse_top_snapshot_keeps_only_the_last_iteration():
    hdr = "    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND"
    text = "\n".join([hdr, "  9 root 20 0 1 1 1 R 900.0 1.0 1:00.00 llama-server",
                      hdr, "  9 root 20 0 1 1 1 R  12.0 1.0 1:00.00 llama-server"])
    rows = parse_top_snapshot(text)
    assert len(rows) == 1 and rows[0]["cpu_pct"] == 12.0


def test_processes_attribute_load_to_ollama_or_not():
    s = summarise_processes(parse_top_snapshot(TOP_CAPTURE), ncpu=12)
    assert s["ollama_cores"] == 12.0
    assert s["other_cores"] == 0.0
    assert s["top_other"] == []
    assert "verdict" not in s


def test_processes_flag_other_load_and_ignore_the_samplers_own_top():
    hdr = "    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND"
    text = "\n".join([hdr,
                      "  9 root 20 0 1 1 1 R  1200 1.0 1:00.00 llama-server",
                      " 10 root 20 0 1 1 1 R 180.0 2.0 1:00.00 python3",
                      " 11 root 20 0 1 1 1 R   1.0 0.0 0:00.00 top"])
    s = summarise_processes(parse_top_snapshot(text), ncpu=12)
    assert s["other_cores"] == 1.8
    assert [p["command"] for p in s["top_other"]] == ["python3"]
    assert "1.8 cores" in s["verdict"]


def test_percentile_nearest_rank():
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([5], 95) == 5
    assert percentile([], 95) is None
