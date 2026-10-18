"""P0 (looplab-control §9.0, 18 Oct 2026): why gates waited hours for test chunks, and the first fixes.

The test nodes are LXC containers pinned to 12 of the Proxmox host's 48 cores. Their worker took one
psutil.cpu_percent() sample per tick, so a node's free cores swung 0 -> 12 within 30 s; reservations counted for a
job's whole run on top of a reading that already showed the job; busy_avoid took both CPU nodes out whenever
anything ran.
"""
import time

from vera.workers import node_compute_core as nc
from vera.workers import node_metrics_core as nm
from vera.evolve import test_runner_core as tr

NOW = 1_000_000.0


# ── P0-1: the node's own pinned cores ──
def test_own_cpu_keeps_only_the_pinned_cores_of_the_host_list():
    per = [0.0] * 48
    for c in range(24, 36):        # Ollama-C is pinned to 24-35
        per[c] = 50.0
    for c in range(0, 12):         # another guest is busy on 0-11
        per[c] = 100.0
    pct, n, scope = nm.own_cpu(per, range(24, 36))
    assert (pct, n, scope) == (50.0, 12, "pinned")


def test_own_cpu_when_the_container_already_sees_only_its_cores():
    assert nm.own_cpu([10.0, 30.0], [24, 25]) == (20.0, 2, "pinned")


def test_own_cpu_without_pinning_is_the_host_reading():
    assert nm.own_cpu([40.0, 60.0], []) == (50.0, 2, "host")
    assert nm.own_cpu([], []) == (0.0, 1, "host")


# ── P0-2: a minute's average, not one tick ──
def test_window_averages_the_last_minute_and_forgets_older_samples():
    w = nm.Window(60)
    assert w.add(100.0, now=0) == 100.0
    assert w.add(0.0, now=10) == 50.0
    assert w.add(0.0, now=75) == 0.0          # the 0 s sample fell out (75 - 60 > 0); 10 s too
    assert len(w) == 1


def _worker(**kw):
    w = {"status": "idle", "ncpu": "12", "mem_avail_gb": "40", "ram_total_gb": "64", "metrics_at": str(NOW),
         "compute_slots": "10", "compute_running": "0"}
    w.update(kw)
    return w


def test_capacity_reads_the_averaged_own_cores_when_reported():
    c = nc.capacity(_worker(cpu_pct="100", cpu_pct_avg="25", cpu_scope="pinned"))
    assert c["cpu_free"] == 9.0 and c["scope"] == "pinned"
    old = nc.capacity(_worker(cpu_pct="100"))                 # an old worker: the old reading
    assert old["cpu_free"] == 0.0 and old["scope"] == "host"


# ── the reservation rule (vera-d2's note): only a pinned reading shows a node's own jobs ──
def test_pinned_reservations_stop_counting_once_the_average_covers_them():
    res = {"cpu": 8.0, "mem_gb": 12.0, "jobs": 2.0,
           "sent": [[NOW - 200, 4.0, 6.0], [NOW - 10, 4.0, 6.0]]}
    assert nc.reserved_now(res, NOW, "pinned") == (4.0, 6.0)  # the 200 s-old job is in the reading now
    assert nc.reserved_now(res, NOW, "host") == (8.0, 12.0)   # a host reading never shows them: whole run


def test_a_refusal_names_the_reading_and_what_is_held():
    w = _worker(cpu_pct_avg="50", cpu_scope="pinned")
    res = {"cpu": 4.0, "mem_gb": 6.0, "jobs": 1.0, "sent": [[NOW - 5, 4.0, 6.0]]}
    ok, why, _ = nc.admit("pytest.shard", w, {"reserve_cpu": 2.0, "reserve_mem_gb": 8.0}, res, now=NOW)
    assert not ok
    assert "own cores, 60 s avg" in why and "held by 1 job(s)" in why and "kept spare" in why


def test_a_pinned_node_takes_a_second_chunk_the_host_reading_refused():
    # one 4-core chunk sent 2 min ago is in the own-core average (4 of 12 busy)
    res = {"cpu": 4.0, "mem_gb": 6.0, "jobs": 1.0, "sent": [[NOW - 120, 4.0, 6.0]]}
    cfg = {"reserve_cpu": 2.0, "reserve_mem_gb": 8.0}
    ok, _, _ = nc.admit("pytest.shard", _worker(cpu_pct_avg="33.3", cpu_scope="pinned"), cfg, res, now=NOW)
    assert ok                                                  # 8 free - 2 spare - 0 held >= 4
    ok_host, _, _ = nc.admit("pytest.shard", _worker(cpu_pct="33.3"), cfg, res, now=NOW)
    assert not ok_host                                         # 8 free - 2 spare - 4 held < 4 (counted twice)


# ── P0-4: busy_avoid leaves alone only the nodes the running work uses ──
def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def test_live_avoid_keeps_only_nodes_whose_instance_served_loop_work_recently():
    stats = [{"instance": "cpu-246", "job_type": "loop_coder", "last_ts": _iso(NOW - 60)},
             {"instance": "cpu-247", "job_type": "loop_executor", "last_ts": _iso(NOW - 3600)},
             {"instance": "cpu-247", "job_type": "chat", "last_ts": _iso(NOW - 5)}]
    bmap = {"Ollama-B": "cpu-246", "Ollama-C": "cpu-247"}
    assert tr.live_avoid(["Ollama-B", "Ollama-C"], bmap, stats, NOW) == ["Ollama-B"]
    assert tr.live_avoid(["Ollama-B", "unmapped"], bmap, [], NOW) == ["unmapped"]   # nothing says it is free


def test_busy_map_config_merges_from_a_dict_or_a_string():
    assert tr.merge_pool_config({"busy_map": {"Ollama-B": "cpu-246"}})["busy_map"] == {"Ollama-B": "cpu-246"}
    assert tr.merge_pool_config({"busy_map": "Ollama-B=cpu-246,Ollama-C=cpu-247"})["busy_map"] == {"Ollama-B": "cpu-246", "Ollama-C": "cpu-247"}
    assert tr.merge_pool_config({})["busy_map"] == {}
