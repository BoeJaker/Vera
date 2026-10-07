"""docker.host.* - the Docker daemon on the Vera host, watched.

On 2026-09-29 dockerd - which had grown ~0.75 GiB a day for two weeks - was OOM-killed at 14 GiB during the nightly
backup. systemd restarted it, and the new daemon hung for 16 hours stopping two chat sandboxes whose `docker exec`
had deadlocked in `runc init` 11 and 16 days earlier: every container, Vera's Neo4j and Postgres among them, was
down all day and nothing said so. The host now runs with live-restore and OOM protection for dockerd; this module
is the eyes on it.

Capabilities
------------
  docker.host.health            dockerd's state, memory and growth rate, deadlocked exec helpers, live-restore and
                                OOM protection -> {status, findings, ...}
  docker.host.fix_stuck_execs   clear deadlocked `runc exec` helpers (dry run unless apply=true)

The watch (every VERA_DOCKER_WATCH_S, default 300 s, on the host only - never inside a sandbox container) records a
memory sample, writes each NEW or CHANGED finding to the system log (a WARNING or ERROR lands in the errors stream,
which the Observe page leads with), and clears exec helpers stuck longer than VERA_DOCKER_STUCK_EXEC_S (default
3600 s) while VERA_DOCKER_AUTOFIX is on (default on). Every kill is logged with what was killed and why.

The pure logic is docker_host_health_core (tests/test_docker_host_health_core.py).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, now_iso, schedule
from Vera.vera.workers import docker_host_health_core as core

log = logging.getLogger("vera.docker.host")

WATCH_S = int(os.getenv("VERA_DOCKER_WATCH_S", "300"))
STUCK_EXEC_S = int(os.getenv("VERA_DOCKER_STUCK_EXEC_S", str(core.STUCK_EXEC_MIN_AGE_S)))
AUTOFIX = os.getenv("VERA_DOCKER_AUTOFIX", "1").strip().lower() not in ("0", "false", "no", "off")
SAMPLES_KEY = "vera:docker:host:rss"          # newest first: {"t", "gib", "pid"}
SAMPLES_MAX = 3000                            # ~10 days at the default interval
_LAST_SIG: Dict[str, str] = {}                # finding code -> the message last logged (log changes, not repeats)


def _on_host() -> bool:
    """The watch reads the host's systemd and process table: inside a container there is neither."""
    if os.path.exists("/.dockerenv") or os.getenv("VERA_IS_DEV_SANDBOX"):
        return False
    return os.path.isdir("/run/systemd/system") and bool(shutil.which("systemctl"))


async def _run(argv: List[str], timeout: float = 20) -> Dict[str, Any]:
    from Vera.vera.execution.spawn_core import run_argv       # never a bare subprocess under uvloop
    return await run_argv(argv, timeout=timeout)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


async def _daemon_state() -> Dict[str, Any]:
    r = await _run(["systemctl", "show", "docker", "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                    "-p", "StateChangeTimestampMonotonic"])
    kv = dict(line.split("=", 1) for line in (r.get("stdout") or "").splitlines() if "=" in line)
    since = None
    try:
        up = float(_read("/proc/uptime").split()[0])
        mono = int(kv.get("StateChangeTimestampMonotonic", "0") or 0)
        if mono:
            since = max(0.0, up - mono / 1e6)
    except (ValueError, IndexError):
        pass
    try:
        pid = int(kv.get("MainPID", "0") or 0)
    except ValueError:
        pid = 0
    return {"active": kv.get("ActiveState", ""), "sub": kv.get("SubState", ""), "pid": pid, "since_s": since}


def _rss_gib(pid: int) -> Optional[float]:
    for line in _read(f"/proc/{pid}/status").splitlines() if pid else []:
        if line.startswith("VmRSS:"):
            try:
                return round(int(line.split()[1]) / 2 ** 20, 3)
            except (IndexError, ValueError):
                return None
    return None


def _oom_adj(pid: int) -> Optional[int]:
    v = _read(f"/proc/{pid}/oom_score_adj").strip() if pid else ""
    return int(v) if v.lstrip("-").isdigit() else None


async def _live_restore() -> Optional[bool]:
    r = await _run(["docker", "info", "--format", "{{.LiveRestoreEnabled}}"], timeout=20)
    v = (r.get("stdout") or "").strip().lower()
    return True if v == "true" else (False if v == "false" else None)


async def _procs() -> List[Dict[str, Any]]:
    r = await _run(["ps", "-eo", "pid=,ppid=,etimes=,stat=,args="], timeout=20)
    return core.parse_ps(r.get("stdout") or "")


async def _samples(pid: int, rss: Optional[float], record: bool) -> List[tuple]:
    R = getattr(_orch, "REDIS", None)
    if not R:
        return []
    try:
        if record and rss is not None and pid:
            await R.lpush(SAMPLES_KEY, json.dumps({"t": time.time(), "gib": rss, "pid": pid}))
            await R.ltrim(SAMPLES_KEY, 0, SAMPLES_MAX - 1)
        raw = await R.lrange(SAMPLES_KEY, 0, SAMPLES_MAX - 1)
    except Exception:
        return []
    out = []
    for x in raw:
        try:
            s = json.loads(x)
        except (TypeError, ValueError):
            continue
        if s.get("pid") == pid:                 # a restart resets the memory: only this daemon's samples count
            out.append((s["t"], s["gib"]))
    return sorted(out)


async def _health(record: bool = False) -> Dict[str, Any]:
    if not _on_host():
        return {"available": False, "reason": "not on the Docker host (inside a container, or no systemd)"}
    st = await _daemon_state()
    rss = _rss_gib(st["pid"])
    samples = await _samples(st["pid"], rss, record)
    growth = core.growth_gib_per_day(samples)
    stuck = core.find_stuck_execs(await _procs(), STUCK_EXEC_S)
    live = await _live_restore() if st["active"] == "active" else None
    oom = _oom_adj(st["pid"])
    verdict = core.assess(st, rss, growth, stuck, oom_adj=oom, live_restore=live)
    return {"available": True, "ts": now_iso(), "daemon": st, "rss_gib": rss, "growth_gib_per_day": growth,
            "samples": len(samples), "oom_score_adj": oom, "live_restore": live, "stuck_execs": stuck,
            "autofix": AUTOFIX, "stuck_exec_after_s": STUCK_EXEC_S, **verdict}


async def _syslog(level: str, code: str, message: str, extra: Dict[str, Any]) -> None:
    try:
        from Vera.vera.workers.syslog import SYSLOG, SyslogRecord
        await SYSLOG.write(SyslogRecord(level=level, category="system", event_type=f"docker.host.{code}",
                                        message=message, cap_name="docker.host.health", cap_group="docker",
                                        extra=extra))
    except Exception as e:                      # the log is the report: say so in the app log if it failed
        log.warning("docker.host: could not write to the system log: %s (%s)", e, message)


async def _fix(apply: bool, min_age_s: int) -> Dict[str, Any]:
    if not _on_host():
        return {"available": False, "reason": "not on the Docker host"}
    stuck = core.find_stuck_execs(await _procs(), min_age_s)
    out = {"apply": apply, "min_age_s": min_age_s, "stuck_execs": stuck, "killed": [], "skipped": []}
    if not apply or not stuck:
        out["note"] = "dry run - pass apply=true to clear them" if stuck and not apply else "nothing stuck"
        return out
    for pid in core.kill_targets(stuck):
        # the process must still be exactly what was found: a pid reused since the scan is left alone
        cmd = _read(f"/proc/{pid}/cmdline").replace("\x00", " ").strip()
        if not (cmd == "runc init" or core.is_runc_exec(cmd)):
            out["skipped"].append({"pid": pid, "now": cmd[:80]})
            continue
        r = await _run(["sudo", "-n", "kill", "-9", str(pid)], timeout=10)
        (out["killed"] if r.get("ok") else out["skipped"]).append({"pid": pid, "cmd": cmd[:80], "err": (r.get("stderr") or "")[:120]})
    for s in stuck:
        await _syslog("WARNING", "stuck_exec_cleared",
                      f"cleared a `docker exec` deadlocked in runc for {s['age_h']} h (container {s['container_id'][:12]}): "
                      f"runc {s['runc_pid']} and init {s['init_pids']}", {"stuck": s, "killed": out["killed"]})
    return out


@capability(
    "docker.host.health", memory="off", silent=True,
    http_method="GET", http_path="/docker/host/health", http_tags=["docker"],
    description="The Docker daemon on the Vera host: its state (and how long it has been in it), resident memory and "
                "growth per day, `docker exec` helpers deadlocked in runc, whether live-restore is on and dockerd is "
                "protected from the OOM killer. Output: {status: ok|warn|error, findings:[{level, code, message}], "
                "daemon, rss_gib, growth_gib_per_day, stuck_execs, live_restore, oom_score_adj}.",
)
async def docker_host_health(trace_id=None):
    return await _health(record=False)


@capability(
    "docker.host.fix_stuck_execs", memory="off",
    http_method="POST", http_path="/docker/host/fix_stuck_execs", http_tags=["docker"],
    description="Clear `docker exec` helpers deadlocked in runc (a `runc exec` and its `runc init`, older than "
                "min_age_s) - the thing that blocked dockerd's restart for 16 hours on 2026-09-29. Only those two "
                "processes are killed, never the container or its shim; each pid is re-checked just before. Dry run "
                "unless apply=true; every clear is written to the system log.",
)
async def docker_host_fix_stuck_execs(apply: bool = False, min_age_s: int = STUCK_EXEC_S, trace_id=None):
    return await _fix(bool(apply), max(300, int(min_age_s or STUCK_EXEC_S)))


async def _watch_tick() -> None:
    if not _on_host():
        return
    try:
        h = await _health(record=True)
    except Exception as e:
        log.warning("docker.host watch: %s", e)
        return
    if AUTOFIX and h.get("stuck_execs"):
        try:
            await _fix(True, STUCK_EXEC_S)
        except Exception as e:
            log.warning("docker.host autofix: %s", e)
    seen = set()
    for f in h.get("findings", []):
        seen.add(f["code"])
        if _LAST_SIG.get(f["code"]) != f["message"]:
            _LAST_SIG[f["code"]] = f["message"]
            await _syslog(f["level"], f["code"], f["message"],
                          {"rss_gib": h.get("rss_gib"), "growth_gib_per_day": h.get("growth_gib_per_day"), "daemon": h.get("daemon")})
    for code in [c for c in _LAST_SIG if c not in seen]:     # a finding that cleared says so, once
        _LAST_SIG.pop(code, None)
        await _syslog("INFO", f"{code}_cleared", f"docker host: {code.replace('_', ' ')} no longer seen", {})


schedule(_watch_tick, interval=WATCH_S, name="docker_host_watch", skip_in_sandbox=True)
