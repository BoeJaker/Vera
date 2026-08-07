"""
evolve_logs_core.py — pure parsing for the Loop Lab sandbox log/perf collector
==============================================================================

Stdlib-only, no docker/redis/app dependency, so the parsing + classification the
collector relies on is unit-testable without booting Vera or a container (same
pattern as dag/planner_core.py, ide/ws_changes_core.py, evolve/evolve_git_core.py).

The collector shells `docker logs --timestamps` and `docker stats` for each
loop-lab sandbox container; this module turns those raw lines into structured,
classified, de-dupable records.
"""

from __future__ import annotations

import re

# Leading RFC3339 timestamp that `docker logs --timestamps` prepends.
_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s?(?P<rest>.*)$", re.S)
_ERR_RE = re.compile(
    r"(?:\bERROR\b|\bCRITICAL\b|\bFATAL\b|\bException\b|[A-Za-z_]*Error\b|"
    r"Traceback \(most recent call last\))")
_WARN_RE = re.compile(r"\bWARN(?:ING)?\b")


def parse_log_line(line: str):
    """Split a `docker logs --timestamps` line into (ts, text). ts is '' when the
    line carries no leading RFC3339 timestamp (e.g. a wrapped traceback line)."""
    line = (line or "").rstrip("\r\n")
    m = _TS_RE.match(line)
    if m:
        return m.group("ts"), m.group("rest")
    return "", line


def classify_level(text: str) -> str:
    """error | warn | info — cheap keyword classification of a log line."""
    if _ERR_RE.search(text or ""):
        return "error"
    if _WARN_RE.search(text or ""):
        return "warn"
    return "info"


def is_error_line(text: str) -> bool:
    return classify_level(text) == "error"


def error_signature(text: str) -> str:
    """A stable de-dup key for an error line: strip the volatile bits (timestamps,
    hex/uuids, line numbers, addresses, bare integers) so repeats of the SAME
    error collapse to one signature instead of flooding the store/queue."""
    s = text or ""
    s = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", s)
    s = re.sub(r"\b[0-9a-fA-F]{8,}\b", "HEX", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?", "TS", s)
    s = re.sub(r":\d+", ":N", s)               # file:line
    s = re.sub(r"\b\d+\b", "N", s)             # remaining bare integers
    return re.sub(r"\s+", " ", s).strip()[:200]


def _num_pct(v: str):
    try:
        return round(float(str(v).strip().rstrip("%")), 2)
    except Exception:
        return None


def parse_stats_line(line: str):
    """Parse one TAB-separated line of
    `docker stats --no-stream --format '{{.Name}}\\t{{.CPUPerc}}\\t{{.MemPerc}}\\t{{.MemUsage}}'`
    into {name, cpu_pct, mem_pct, mem}, or None if unparseable."""
    parts = [p.strip() for p in (line or "").split("\t")]
    if len(parts) < 3 or not parts[0]:
        return None
    return {
        "name": parts[0],
        "cpu_pct": _num_pct(parts[1]),
        "mem_pct": _num_pct(parts[2]),
        "mem": parts[3] if len(parts) > 3 else "",
    }
