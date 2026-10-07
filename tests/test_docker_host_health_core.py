"""docker_host_health_core: the Docker host watchdog's pure logic (2026-09-29: dockerd OOM-killed at 14 GiB after a
two-week leak; its restart hung 16 h behind two `docker exec` helpers deadlocked in `runc init`)."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from vera.workers import docker_host_health_core as core  # noqa: E402

CID = "8b85e03711dcddf97e3474b7f1e2b6a774558b487a6774c0d34ebb1025db91a8"
EXEC = (f"runc --root /var/run/docker/runtime-runc/moby --log /run/containerd/io.containerd.runtime.v2.task/moby/{CID}/log.json "
        f"--log-format json --systemd-cgroup exec --process /tmp/runc-process3134976402 --detach --pid-file /run/x/871651e5c")
PS = "\n".join([
    f" 1823591       1 1452356 Sl   /usr/bin/containerd-shim-runc-v2 -namespace moby -id {CID} -address /run/containerd/containerd.sock",
    " 1823612 1823591 1452356 Ss   tail -f /dev/null",
    f" 1823779 1823591 1452354 Sl   {EXEC}",
    " 1823794 1823779 1452354 Ssl  runc init",
    f" 2000001 1823591      12 Sl   {EXEC.replace('3134976402', '999')}",       # a young exec: healthy, left alone
    " 2000002 2000001      12 Ssl  runc init",
    " 2100000       1 5000000 Sl   runc --root /var/run/docker/runtime-runc/moby state abc",   # old, but not an exec
    "garbage line",
])


def test_parse_ps_reads_the_rows_and_skips_garbage():
    p = core.parse_ps(PS)
    assert len(p) == 7 and p[0]["pid"] == 1823591 and p[2]["age_s"] == 1452354 and p[3]["args"] == "runc init"


def test_a_deadlocked_exec_is_found_with_its_init_and_container():
    s = core.find_stuck_execs(core.parse_ps(PS), min_age_s=3600)
    assert len(s) == 1
    assert s[0]["runc_pid"] == 1823779 and s[0]["init_pids"] == [1823794] and s[0]["shim_pid"] == 1823591
    assert s[0]["container_id"] == CID and s[0]["age_h"] == round(1452354 / 3600, 1)


def test_young_execs_and_other_runc_commands_are_left_alone():
    assert all(x["runc_pid"] != 2000001 for x in core.find_stuck_execs(core.parse_ps(PS), 3600))
    assert all(x["runc_pid"] != 2100000 for x in core.find_stuck_execs(core.parse_ps(PS), 3600))
    assert not core.is_runc_exec("runc --root /var/run/docker/runtime-runc/moby state abc")


def test_the_kill_list_is_the_init_then_the_exec_never_the_shim_or_the_container():
    t = core.kill_targets(core.find_stuck_execs(core.parse_ps(PS), 3600))
    assert t == [1823794, 1823779]
    assert 1823591 not in t and 1823612 not in t


def test_growth_is_a_least_squares_slope_in_gib_per_day():
    day = 86400
    pts = [(1_000_000 + i * day / 24, 2.5 + 0.75 * i / 24) for i in range(48)]
    assert abs(core.growth_gib_per_day(pts) - 0.75) < 0.01
    assert core.growth_gib_per_day(pts[:2]) is None                 # too few
    assert core.growth_gib_per_day([(0, 1), (60, 1), (120, 1)]) is None   # under an hour


def test_the_incident_reads_as_errors_and_warnings():
    stuck = core.find_stuck_execs(core.parse_ps(PS), 3600)
    v = core.assess({"active": "activating", "sub": "start", "since_s": 16 * 3600}, 14.0, 0.75, stuck, oom_adj=0, live_restore=False)
    codes = {f["code"]: f["level"] for f in v["findings"]}
    assert v["status"] == "error"
    assert codes == {"daemon_stuck_starting": "ERROR", "stuck_execs": "WARNING", "daemon_memory": "ERROR",
                     "daemon_leak": "WARNING", "daemon_oom_unprotected": "WARNING", "no_live_restore": "WARNING"}


def test_a_healthy_host_is_ok():
    v = core.assess({"active": "active", "sub": "running", "since_s": 600}, 0.4, 0.02, [], oom_adj=-900, live_restore=True)
    assert v == {"status": "ok", "findings": []}


def test_a_short_start_is_not_an_error_but_a_dead_daemon_is():
    assert core.assess({"active": "activating", "sub": "start", "since_s": 30}, None, None, [])["status"] == "ok"
    assert core.assess({"active": "failed", "sub": "failed", "since_s": 5}, None, None, [])["findings"][0]["code"] == "daemon_down"


MOD = open(os.path.join(ROOT, "vera", "workers", "docker_host_health_capabilities.py"), encoding="utf-8").read()
ORCH = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()


def test_the_module_is_loaded_watches_on_the_host_only_and_never_kills_blind():
    assert 'os.path.join(_here, "workers/docker_host_health_capabilities.py"),' in ORCH
    assert 'schedule(_watch_tick, interval=WATCH_S, name="docker_host_watch", skip_in_sandbox=True)' in MOD
    assert 'if os.path.exists("/.dockerenv") or os.getenv("VERA_IS_DEV_SANDBOX"):' in MOD
    # every pid is re-read just before the kill, and only the two runc shapes may be killed
    assert 'if not (cmd == "runc init" or core.is_runc_exec(cmd)):' in MOD
    assert "from Vera.vera.execution.spawn_core import run_argv" in MOD      # never a bare subprocess under uvloop
    # a fix is a dry run unless asked
    assert "async def docker_host_fix_stuck_execs(apply: bool = False" in MOD
