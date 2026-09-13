"""
node_telemetry_core.py — pure parsing of what an Ollama node is doing (no app imports)
====================================================================================

Two questions Vera's own load accounting cannot answer, because the router only
counts the requests VERA sends:

  * What are the node's CPU and memory doing while it serves? A CPU node has no
    GPU for gpu_trace_core to read.
  * Who else is calling the node, and what else is running on it? A request that
    reaches Ollama without going through Vera, or a process that is not Ollama at
    all, takes capacity the router never sees.

The retired edge/ollama_wrapper.sh answered the second question by sitting in
the request path as a proxy. Ollama already writes one access-log line per
request (its [GIN] log) with the client address, status, latency and path, so
the same visibility comes from reading that log — nothing is added to the
request path, and nothing can drop, stall or rewrite a request.

Container facts, measured on the Proxmox LXC Ollama nodes on 2026-09-10:
  * /proc/loadavg is HOST-wide (identical in every container), so it is reported
    as host load and never used for a verdict about the node.
  * The cgroup's cpu.stat usage_usec is the container's own CPU time and is the
    preferred CPU measure; /proc/stat is the fallback where cgroup v2 is absent.
  * `ps %CPU` is a lifetime average, not current use — llama-server showed 452%
    there while the cgroup counter showed the container almost idle — so current
    per-process CPU comes from the second iteration of `top -b`.

Shell scripts use __PLACEHOLDER__ substitution, not str.format, because their
awk programs are full of braces.

Nothing here imports anything beyond the stdlib.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _float(v: Any) -> Optional[float]:
    try:
        return float(str(v).strip().replace(",", "."))
    except Exception:
        return None


def _round(v: Optional[float], nd: int = 1) -> Optional[float]:
    return None if v is None else round(v, nd)


def percentile(values: Iterable[Optional[float]], p: float) -> Optional[float]:
    """Nearest-rank percentile; None when there is no data."""
    ys = sorted(v for v in values if v is not None)
    if not ys:
        return None
    rank = max(1, math.ceil(p / 100.0 * len(ys)))
    return ys[min(rank, len(ys)) - 1]


# ─────────────────────────────────────────────────────────────────────────────
# CPU / memory trace
# ─────────────────────────────────────────────────────────────────────────────

CPU_TRACE_SCRIPT = r"""for i in $(seq 1 __COUNT__); do
echo "T $(date +%s.%N)"
head -1 /proc/stat
cat /proc/loadavg
grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo "CG $(grep -E '^usage_usec' /sys/fs/cgroup/cpu.stat 2>/dev/null) MEMCUR $(cat /sys/fs/cgroup/memory.current 2>/dev/null)"
echo "NCPU $(nproc)"
sleep 1
done"""

# CPU inference is expected to saturate the node (llama.cpp uses every thread it
# is given), so crossing this is reported as CPU-bound, not as a fault.
CPU_BOUND_PCT = 95.0
MEMORY_PRESSURE_PCT = 90.0


def cpu_trace_script(count: int) -> str:
    """Sampler for `count` one-second samples. At least two: CPU use is a delta."""
    return CPU_TRACE_SCRIPT.replace("__COUNT__", str(max(2, int(count))))


def parse_cpu_trace(text: str) -> List[Dict[str, Any]]:
    """Sampler output -> one dict per `T` block. Unknown or malformed lines are
    skipped; a block missing a field simply lacks that key."""
    samples: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for raw in (text or "").splitlines():
        parts = raw.split()
        if not parts:
            continue
        head = parts[0]
        if head == "T":
            t = _float(parts[1]) if len(parts) > 1 else None
            cur = {"t": t} if t is not None else None
            if cur is not None:
                samples.append(cur)
            continue
        if cur is None:
            continue
        if head == "cpu" and len(parts) >= 5:
            # user nice system idle iowait irq softirq steal — guest time is
            # already inside user, so the guest columns are not summed again.
            vals = [(_int(x) or 0) for x in parts[1:9]]
            cur["cpu_total"] = sum(vals)
            cur["cpu_idle"] = vals[3] + (vals[4] if len(vals) > 4 else 0)
        elif head == "MemTotal:" and len(parts) >= 2:
            cur["mem_total_kb"] = _int(parts[1])
        elif head == "MemAvailable:" and len(parts) >= 2:
            cur["mem_avail_kb"] = _int(parts[1])
        elif head == "CG":
            toks = parts[1:]
            for i, tok in enumerate(toks[:-1]):
                if tok == "usage_usec":
                    cur["cg_usage_usec"] = _int(toks[i + 1])
                elif tok == "MEMCUR":
                    cur["mem_current"] = _int(toks[i + 1])
        elif head == "NCPU" and len(parts) >= 2:
            cur["ncpu"] = _int(parts[1])
        elif len(parts) == 5 and "/" in parts[3] and _float(parts[0]) is not None:
            cur["load1"] = _float(parts[0])
    return samples


def cpu_trace_summary(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """CPU and memory over the trace, plus a verdict. {} when there are fewer
    than two samples, so a failed sampler never reads as an idle node."""
    s = [x for x in (samples or []) if isinstance(x, dict) and x.get("t") is not None]
    if len(s) < 2:
        return {}
    ncpu = next((x["ncpu"] for x in reversed(s) if x.get("ncpu")), None)
    pct: List[float] = []
    busy: List[float] = []
    source = None
    for a, b in zip(s, s[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        ua, ub = a.get("cg_usage_usec"), b.get("cg_usage_usec")
        if ua is not None and ub is not None and ub >= ua:
            cores = (ub - ua) / (dt * 1e6)
            busy.append(cores)
            if ncpu:
                pct.append(100.0 * cores / ncpu)
            source = source or "cgroup"
            continue
        ta, tb = a.get("cpu_total"), b.get("cpu_total")
        ia, ib = a.get("cpu_idle"), b.get("cpu_idle")
        if None not in (ta, tb, ia, ib) and tb > ta:
            frac = min(1.0, max(0.0, 1.0 - (ib - ia) / (tb - ta)))
            pct.append(100.0 * frac)
            if ncpu:
                busy.append(frac * ncpu)
            source = source or "procstat"
    if not pct and not busy:
        return {}

    mem_used = [(x["mem_total_kb"] - x["mem_avail_kb"]) / 1048576.0 for x in s
                if x.get("mem_total_kb") and x.get("mem_avail_kb") is not None]
    mem_total = next((x["mem_total_kb"] / 1048576.0 for x in s if x.get("mem_total_kb")),
                     None)
    load1 = [x["load1"] for x in s if x.get("load1") is not None]

    out: Dict[str, Any] = {
        "samples": len(s),
        "source": source,
        "ncpu": ncpu,
        "cpu_pct": ({"mean": _round(sum(pct) / len(pct)), "peak": _round(max(pct))}
                    if pct else None),
        "cores_busy": ({"mean": _round(sum(busy) / len(busy), 2),
                        "peak": _round(max(busy), 2)} if busy else None),
        "mem_used_gb": ({"start": _round(mem_used[0], 2), "peak": _round(max(mem_used), 2)}
                        if mem_used else None),
        "mem_total_gb": _round(mem_total, 2),
        "host_load1": ({"start": load1[0], "end": load1[-1], "peak": max(load1),
                        "note": "host-wide inside an LXC container, not this node's own"}
                       if load1 else None),
    }
    peak = max(pct) if pct else None
    mem_pct = (100.0 * max(mem_used) / mem_total) if (mem_used and mem_total) else None
    out["mem_used_pct_peak"] = _round(mem_pct)
    if mem_pct is not None and mem_pct >= MEMORY_PRESSURE_PCT:
        out["status"] = "memory_pressure"
        out["verdict"] = ("MEMORY PRESSURE: %.1f of %.1f GB in use at peak (%.0f%%)."
                          % (max(mem_used), mem_total, mem_pct))
    elif peak is not None and peak >= CPU_BOUND_PCT:
        out["status"] = "cpu_bound"
        out["verdict"] = ("CPU-bound: the node used %.0f%% of its %s CPUs at peak, so "
                          "throughput here is limited by CPU." % (peak, ncpu or "?"))
    else:
        out["status"] = "ok"
        out["verdict"] = ("CPU %s%% mean / %s%% peak%s."
                          % (out["cpu_pct"]["mean"] if pct else "?",
                             _round(peak) if peak is not None else "?",
                             (" of %s CPUs" % ncpu) if ncpu else ""))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Ollama access log ([GIN] lines)
# ─────────────────────────────────────────────────────────────────────────────

# Endpoints Vera (and dashboards) poll constantly. They are counted per client on
# the node and never shipped line by line — gpu-250 logged ~5,000 of them an hour.
POLL_PATHS = frozenset({"/api/ps", "/api/version", "/api/tags"})
INFERENCE_PATHS = frozenset({
    "/api/generate", "/api/chat", "/api/embed", "/api/embeddings",
    "/v1/chat/completions", "/v1/completions", "/v1/embeddings",
})
LONG_REQUEST_S = 60.0

_GIN_RE = re.compile(
    r'\[GIN\]\s+(\d{4}/\d{2}/\d{2})\s+-\s+(\d{2}:\d{2}:\d{2})\s*\|\s*(\d{3})\s*\|'
    r'\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([A-Z]+)\s+"([^"]*)"')
_DUR_TOK = re.compile(r'(\d+(?:\.\d+)?)(ns|µs|μs|us|ms|h|m|s)')
_DUR_SCALE = {"ns": 1e-9, "µs": 1e-6, "μs": 1e-6, "us": 1e-6, "ms": 1e-3,
              "s": 1.0, "m": 60.0, "h": 3600.0}

REQUEST_LOG_SCRIPT = r"""PID=$(ss -lntpH "sport = :__PORT__" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
UNIT=""
[ -n "$PID" ] && UNIT=$(sed -n 's#.*/\([^/]*\.service\)$#\1#p' /proc/$PID/cgroup 2>/dev/null | head -1)
echo "UNIT $UNIT"
echo "NCPU $(nproc)"
if [ -n "$UNIT" ]; then
  F=$(mktemp)
  journalctl -u "$UNIT" --since "-__MINUTES__min" --no-pager -o cat 2>/dev/null | grep -F '[GIN]' > "$F"
  echo "TOTAL $(wc -l < "$F")"
  grep -E '"/api/(ps|version|tags)"' "$F" | awk -F'|' '{ip=$4; gsub(/ /,"",ip); c[ip]++} END{for(k in c) print "POLL", k, c[k]}'
  echo "WORKTOTAL $(grep -vcE '"/api/(ps|version|tags)"' "$F")"
  echo "WORK"
  grep -vE '"/api/(ps|version|tags)"' "$F" | tail -n __MAXROWS__
  rm -f "$F"
fi
echo "TOP"
top -b -n 2 -d 0.5 -w 256 2>/dev/null | awk '/^ *PID /{n++} n==2' | head -n 16"""


def request_log_script(port: int, minutes: int, max_rows: int) -> str:
    """Node-side reader: finds the systemd unit that owns `port`, aggregates poll
    traffic per client, returns the last `max_rows` non-poll requests, and takes a
    current-use process snapshot."""
    return (REQUEST_LOG_SCRIPT
            .replace("__PORT__", str(int(port)))
            .replace("__MINUTES__", str(max(1, int(minutes))))
            .replace("__MAXROWS__", str(max(1, int(max_rows)))))


def parse_latency_s(text: Any) -> Optional[float]:
    """Go duration text from a [GIN] line -> seconds. '42.075µs', '5.03ms',
    '2.5s', '1m30s', '1h2m3s'. None for anything that is not a whole duration."""
    s = str(text or "").replace(" ", "")
    if not s:
        return None
    toks = _DUR_TOK.findall(s)
    if not toks or "".join(n + u for n, u in toks) != s:
        return None
    return sum(float(n) * _DUR_SCALE[u] for n, u in toks)


def parse_gin_line(line: str) -> Optional[Dict[str, Any]]:
    m = _GIN_RE.search(line or "")
    if not m:
        return None
    date, clock, status, latency, client, method, path = m.groups()
    base = path.split("?", 1)[0]
    return {"ts": "%s %s" % (date.replace("/", "-"), clock), "status": int(status),
            "latency_s": parse_latency_s(latency), "client": client.strip(),
            "method": method, "path": base,
            "inference": base in INFERENCE_PATHS}


def parse_top_snapshot(lines: Any) -> List[Dict[str, Any]]:
    """Process rows from the LAST `PID ... COMMAND` header in `top -b` output.
    The first iteration of top reports averages since boot; only the second is
    current use, and the node-side script already keeps just that one."""
    ls = lines.splitlines() if isinstance(lines, str) else list(lines or [])
    hdr = None
    for i, ln in enumerate(ls):
        if ln.strip().startswith("PID ") and "%CPU" in ln and "COMMAND" in ln:
            hdr = i
    if hdr is None:
        return []
    cols = ls[hdr].split()
    try:
        ci, mi, ui, cmd = (cols.index("%CPU"), cols.index("%MEM"), cols.index("USER"),
                           cols.index("COMMAND"))
    except ValueError:
        return []
    rows: List[Dict[str, Any]] = []
    for ln in ls[hdr + 1:]:
        parts = ln.split(None, cmd)
        if len(parts) <= cmd:
            continue
        pid = _int(parts[0])
        if pid is None:
            continue
        rows.append({"pid": pid, "user": parts[ui], "cpu_pct": _float(parts[ci]),
                     "mem_pct": _float(parts[mi]), "command": parts[cmd].strip()})
    return rows


def parse_request_log(text: str) -> Dict[str, Any]:
    """Output of request_log_script -> {unit, ncpu, total, work_total, polls, rows,
    processes}."""
    out: Dict[str, Any] = {"unit": "", "ncpu": None, "total": None, "work_total": None,
                           "polls": {}, "rows": [], "processes": []}
    section = None
    top_lines: List[str] = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if section == "top":
            top_lines.append(raw)
            continue
        if s == "WORK":
            section = "work"
            continue
        if s == "TOP":
            section = "top"
            continue
        if section == "work":
            row = parse_gin_line(s)
            if row:
                out["rows"].append(row)
            continue
        parts = s.split()
        if not parts:
            continue
        head = parts[0]
        if head == "UNIT":
            out["unit"] = parts[1] if len(parts) > 1 else ""
        elif head == "NCPU" and len(parts) > 1:
            out["ncpu"] = _int(parts[1])
        elif head == "TOTAL" and len(parts) > 1:
            out["total"] = _int(parts[1])
        elif head == "WORKTOTAL" and len(parts) > 1:
            out["work_total"] = _int(parts[1])
        elif head == "POLL" and len(parts) >= 3:
            n = _int(parts[-1])
            ip = " ".join(parts[1:-1])
            if n is not None:
                out["polls"][ip] = out["polls"].get(ip, 0) + n
    out["processes"] = parse_top_snapshot(top_lines)
    return out


def summarise_requests(log: Dict[str, Any], vera_ips: Iterable[str] = (),
                       long_s: float = LONG_REQUEST_S) -> Dict[str, Any]:
    """Who called the node and what it cost them. Clients other than Vera sort
    first, because they are the load Vera's router cannot see."""
    vera = {str(i).strip() for i in (vera_ips or ()) if str(i).strip()}
    rows = list(log.get("rows") or [])
    polls = dict(log.get("polls") or {})
    clients: Dict[str, Dict[str, Any]] = {}
    endpoints: Dict[str, Dict[str, Any]] = {}

    def _client(ip: str) -> Dict[str, Any]:
        return clients.setdefault(ip, {"client": ip, "is_vera": ip in vera, "polls": 0,
                                       "requests": 0, "inference": 0, "errors": 0,
                                       "_lat": []})

    for ip, n in polls.items():
        _client(ip)["polls"] += int(n)
    for r in rows:
        c = _client(r["client"])
        c["requests"] += 1
        # Status codes are kept, not just an error count: a 404 for a model the
        # node does not have and a 500 after two minutes are different problems.
        ep = endpoints.setdefault(r["path"], {"path": r["path"], "count": 0, "errors": 0,
                                              "statuses": {}, "_lat": []})
        ep["count"] += 1
        code = str(r["status"])
        ep["statuses"][code] = ep["statuses"].get(code, 0) + 1
        if r["status"] >= 400:
            c["errors"] += 1
            ep["errors"] += 1
        if r.get("latency_s") is not None:
            ep["_lat"].append(r["latency_s"])
        if r["inference"]:
            c["inference"] += 1
            if r.get("latency_s") is not None:
                c["_lat"].append(r["latency_s"])

    def _fin(d: Dict[str, Any]) -> Dict[str, Any]:
        lat = d.pop("_lat")
        d["p50_s"] = _round(percentile(lat, 50), 3)
        d["p95_s"] = _round(percentile(lat, 95), 3)
        d["max_s"] = _round(max(lat), 3) if lat else None
        return d

    by_client = sorted((_fin(c) for c in clients.values()),
                       key=lambda c: (c["is_vera"], -c["inference"],
                                      -(c["requests"] + c["polls"])))
    by_endpoint = sorted((_fin(e) for e in endpoints.values()), key=lambda e: -e["count"])
    external = [c for c in by_client if not c["is_vera"]]
    ext_inference = sum(c["inference"] for c in external)
    work_total = log.get("work_total")
    long_reqs = [r for r in rows if r.get("latency_s") is not None
                 and r["latency_s"] >= long_s]

    out: Dict[str, Any] = {
        "unit": log.get("unit", ""),
        "total_requests": log.get("total"),
        "polls": sum(polls.values()),
        "work_requests": work_total if work_total is not None else len(rows),
        "rows_returned": len(rows),
        # Inference counts and latencies cover the returned rows only; when this is
        # true the window held more requests than were shipped back.
        "truncated": bool(work_total is not None and work_total > len(rows)),
        "inference_calls": sum(1 for r in rows if r["inference"]),
        "external_clients": len(external),
        "external_inference_calls": ext_inference,
        "by_client": by_client,
        "by_endpoint": by_endpoint,
        "long_requests": list(reversed(long_reqs))[:10],
        "recent": list(reversed(rows))[:50],
    }
    if not log.get("unit"):
        out["verdict"] = ("Could not find the service listening on this node's port, "
                          "so its access log was not read.")
    elif external:
        out["verdict"] = ("%d client(s) other than Vera called this node, including %d "
                          "inference call(s) — load Vera's router does not count."
                          % (len(external), ext_inference))
    else:
        out["verdict"] = "Every request in this window came from Vera."
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Processes
# ─────────────────────────────────────────────────────────────────────────────

OLLAMA_PROCESS_NAMES = frozenset({"ollama", "llama-server", "ollama_llama_se",
                                  "ollama_llama_server", "ollama-runner"})
# The sampler's own `top` shows up in its snapshot; it is not the node's load.
_SAMPLER_PROCESS_NAMES = frozenset({"top"})


def _proc_name(command: Any) -> str:
    first = str(command or "").strip().split(" ", 1)[0]
    return first.rsplit("/", 1)[-1]


def summarise_processes(processes: List[Dict[str, Any]],
                        ncpu: Optional[int] = None) -> Dict[str, Any]:
    """Split current CPU use into Ollama and everything else. top reports %CPU
    per core, so 100 = one full core."""
    rows = [p for p in (processes or []) if p.get("cpu_pct") is not None
            and _proc_name(p.get("command")) not in _SAMPLER_PROCESS_NAMES]
    if not rows:
        return {}
    is_ollama = [_proc_name(p["command"]) in OLLAMA_PROCESS_NAMES
                 or _proc_name(p["command"]).startswith("ollama") for p in rows]
    ollama_pct = sum(p["cpu_pct"] for p, o in zip(rows, is_ollama) if o)
    others = [p for p, o in zip(rows, is_ollama) if not o]
    other_pct = sum(p["cpu_pct"] for p in others)
    out: Dict[str, Any] = {
        "ncpu": ncpu,
        "ollama_cores": round(ollama_pct / 100.0, 2),
        "other_cores": round(other_pct / 100.0, 2),
        "top_other": [{k: p[k] for k in ("pid", "user", "command", "cpu_pct", "mem_pct")}
                      for p in sorted(others, key=lambda p: -p["cpu_pct"])
                      if p["cpu_pct"] > 0][:5],
    }
    if other_pct >= 100.0:
        out["verdict"] = ("Processes other than Ollama are using about %.1f cores on "
                          "this node." % (other_pct / 100.0))
    return out
