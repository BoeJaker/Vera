"""What a ZFS pool is doing, from zpool's own words.

A pool card that shows name, size, used and health cannot say what the pool
IS - one disk or a mirror - whether it is being scrubbed, how fast it is
moving, or how well it compresses. zpool says all of that; nothing parsed it.
These rules do: `zpool status` for layout, scrub and errors, `zpool iostat` for
the moment's throughput, `zpool list` and `zfs get` for fragmentation, capacity,
dedup and compression. Then plain findings: a pool with no redundancy, a scrub
that is overdue, a pool that is degraded or filling up.

Pure text in, dicts out (tests/test_pool_core.py).
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

SCRUB_OVERDUE_D = 35          # weekly scrubs here; five weeks is "stopped", not "late"
FULL_PCT = 80
FRAG_PCT = 50
_VDEV_TYPES = ("mirror", "raidz1", "raidz2", "raidz3", "raidz", "draid", "spare", "log", "cache", "special", "dedup")


def parse_zpool_list(text: str) -> Dict[str, Dict[str, Any]]:
    """`zpool list -Hp -o name,size,alloc,free,health,fragmentation,capacity,dedupratio`."""
    out: Dict[str, Dict[str, Any]] = {}
    for line in (text or "").splitlines():
        f = line.split("\t") if "\t" in line else line.split()
        if len(f) < 8:
            continue
        try:
            out[f[0]] = {"frag_pct": _int(f[5]), "cap_pct": _int(f[6]), "dedup": _float(f[7])}
        except ValueError:
            continue
    return out


def parse_compress(text: str) -> Dict[str, float]:
    """`zfs get -Hp -o name,value compressratio <pools>`: 1.10 means 10% saved."""
    out: Dict[str, float] = {}
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) >= 2:
            try:
                out[f[0]] = float(f[1].rstrip("x"))
            except ValueError:
                continue
    return out


def parse_iostat(text: str) -> Dict[str, Dict[str, int]]:
    """`zpool iostat -Hp 1 2`: two blocks, the first the average since boot, the
    second one live second. The last line per pool wins, which is the live one."""
    out: Dict[str, Dict[str, int]] = {}
    for line in (text or "").splitlines():
        f = line.split("\t") if "\t" in line else line.split()
        if len(f) < 7:
            continue
        try:
            out[f[0]] = {"read_ops": _int(f[3]), "write_ops": _int(f[4]),
                         "read_bps": _int(f[5]), "write_bps": _int(f[6])}
        except ValueError:
            continue
    return out


def parse_zpool_status(text: str, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """`zpool status`: per pool its state, scan line (parsed), vdev tree, error line."""
    now = time.time() if now is None else now
    pools: Dict[str, Dict[str, Any]] = {}
    cur: Optional[Dict[str, Any]] = None
    in_config = False
    last_key = ""
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("pool:"):
            cur = {"name": s[5:].strip(), "state": "", "scan": "", "vdevs": [], "errors_line": ""}
            pools[cur["name"]] = cur
            in_config = False
            continue
        if cur is None:
            continue
        if s.startswith("state:"):
            cur["state"] = s[6:].strip()
            last_key = "state"
        elif s.startswith("scan:"):
            cur["scan"] = s[5:].strip()
            last_key = "scan"
        elif s.startswith(("status:", "action:", "see:")):
            last_key = "other"
        elif s.startswith("config:"):
            in_config = True
            last_key = "config"
        elif not in_config and last_key == "scan" and s:
            cur["scan"] += " " + s        # the progress lines that follow a running scan
        elif s.startswith("errors:"):
            cur["errors_line"] = s[7:].strip()
            in_config = False
        elif in_config and s and not s.startswith("NAME"):
            _config_line(cur, raw)
    for p in pools.values():
        p.update(_scan_facts(p["scan"], now))
        p["layout"] = describe_layout(p["vdevs"])
        p["redundancy"] = _redundancy(p["vdevs"])
        p["disks"] = sum(len(v["disks"]) for v in p["vdevs"] if v["role"] == "data")
        p["errors"] = sum(d.get("read", 0) + d.get("write", 0) + d.get("cksum", 0)
                          for v in p["vdevs"] for d in v["disks"])
    return pools


def _config_line(pool: Dict[str, Any], raw: str) -> None:
    """One row of the vdev tree. Indentation says what a row is: the pool
    itself, a vdev group (mirror-0, raidz1-0, logs...) or a disk inside one.
    A disk straight under the pool is its own single-disk vdev."""
    stripped = raw.lstrip("\t")
    indent = len(stripped) - len(stripped.lstrip(" "))
    f = stripped.split()
    if not f:
        return
    name = f[0]
    if name == pool["name"] and indent == 0:
        return
    counts = {}
    if len(f) >= 5:
        try:
            counts = {"read": int(f[2]), "write": int(f[3]), "cksum": int(f[4])}
        except ValueError:
            counts = {}
    state = f[1] if len(f) > 1 else ""
    role = "data"
    if name in ("logs", "cache", "spares", "special", "dedup"):
        pool["_section"] = name
        return
    section = pool.get("_section", "")
    if section:
        role = {"logs": "log", "cache": "cache", "spares": "spare", "special": "special", "dedup": "dedup"}[section]
    kind = next((t for t in _VDEV_TYPES if name.startswith(t + "-") or name == t), "")
    if kind and indent <= 2:
        pool["vdevs"].append({"type": kind, "role": role, "name": name, "state": state, "disks": []})
        return
    disk = {"name": name, "state": state, **counts}
    if indent <= 2 or not pool["vdevs"] or pool["vdevs"][-1]["role"] != role:
        pool["vdevs"].append({"type": "disk", "role": role, "name": name, "state": state, "disks": [disk]})
    else:
        pool["vdevs"][-1]["disks"].append(disk)


def describe_layout(vdevs: Iterable[Mapping[str, Any]]) -> str:
    data = [v for v in vdevs if v.get("role") == "data"]
    if not data:
        return "no data vdevs"
    groups: Dict[str, int] = {}
    for v in data:
        n = len(v.get("disks") or [])
        key = f"{v['type']} of {n}" if v["type"] != "disk" else "1 disk"
        groups[key] = groups.get(key, 0) + 1
    parts = [(f"{c} × " if c > 1 else "") + k for k, c in groups.items()]
    text = ", ".join(parts)
    if all(v["type"] == "disk" for v in data):
        text += " · no redundancy" if len(data) == 1 else " striped · no redundancy"
    extras = [v["role"] for v in vdevs if v.get("role") not in ("data", None)]
    if extras:
        text += " · " + ", ".join(sorted(set(extras)))
    return text


def _redundancy(vdevs: Iterable[Mapping[str, Any]]) -> str:
    kinds = {v["type"] for v in vdevs if v.get("role") == "data"}
    if not kinds or kinds == {"disk"}:
        return "none"
    if kinds <= {"mirror"}:
        return "mirror"
    return ", ".join(sorted(k for k in kinds if k != "disk"))


_SCAN_DONE = re.compile(r"(scrub|resilver)(?:ed)? (?:repaired|completed)?\s*(\S+)?\s*(?:in ([\d:]+ ?(?:days? )?[\d:]*))?\s*with (\d+) errors on (.+)$")
_SCAN_PROG = re.compile(r"(scrub|resilver) in progress since (.+?)(?:\n|$)")


def _scan_facts(scan: str, now: float) -> Dict[str, Any]:
    s = (scan or "").strip()
    if not s or s.startswith("none requested"):
        return {"scan_kind": "", "scan_state": "never", "scan_at": None, "scan_age_d": None, "scan_errors": None}
    if "in progress" in s:
        pct = re.search(r"([\d.]+)% done", s)
        return {"scan_kind": "resilver" if "resilver" in s else "scrub", "scan_state": "running",
                "scan_at": None, "scan_age_d": None, "scan_errors": None,
                "scan_pct": float(pct.group(1)) if pct else None}
    m = re.search(r"with (\d+) errors on (.+)$", s)
    when = _when(m.group(2)) if m else None
    return {"scan_kind": "resilver" if s.startswith("resilver") else "scrub",
            "scan_state": "done" if m else "unknown", "scan_at": when,
            "scan_age_d": (int((now - when) // 86400) if when else None),
            "scan_errors": int(m.group(1)) if m else None}


def _when(text: str) -> Optional[float]:
    t = re.sub(r"\s+", " ", text.strip())
    for fmt in ("%a %b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y"):
        try:
            return float(calendar.timegm(time.strptime(t, fmt)))
        except ValueError:
            continue
    return None


def merge(pools: List[Dict[str, Any]], status: Mapping[str, Mapping[str, Any]],
          listing: Mapping[str, Mapping[str, Any]], compress: Mapping[str, float],
          iostat: Mapping[str, Mapping[str, int]]) -> List[Dict[str, Any]]:
    """The inventory's pool rows, each with what zpool knows about it."""
    out = []
    for p in pools:
        row = dict(p)
        st = status.get(p["name"]) or {}
        row.update({k: st.get(k) for k in ("layout", "redundancy", "disks", "vdevs", "scan", "scan_kind", "scan_state",
                                           "scan_at", "scan_age_d", "scan_errors", "scan_pct", "errors", "errors_line") if k in st})
        row.update(listing.get(p["name"]) or {})
        if p["name"] in compress:
            row["compressratio"] = compress[p["name"]]
        row.update(iostat.get(p["name"]) or {})
        out.append(row)
    return out


def pool_findings(pools: Iterable[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """Plain language, worst first."""
    out = []
    for p in pools:
        name = p.get("name", "?")
        health = str(p.get("health") or p.get("state") or "").upper()
        if health and health != "ONLINE":
            out.append({"severity": "error", "pool": name, "message": f"Pool {name} is {health.lower()}.",
                        "detail": p.get("errors_line", "")})
        if (p.get("errors") or 0) > 0:
            out.append({"severity": "error", "pool": name,
                        "message": f"Pool {name} has counted {p['errors']} read, write or checksum errors.",
                        "detail": "run zpool status -v and check the disk"})
        if p.get("cap_pct") is not None and p["cap_pct"] >= FULL_PCT:
            out.append({"severity": "warn", "pool": name, "message": f"Pool {name} is {p['cap_pct']}% full.",
                        "detail": "ZFS slows down and fragments badly past 80%"})
        if p.get("scan_state") == "never":
            out.append({"severity": "warn", "pool": name, "message": f"Pool {name} has never been scrubbed.",
                        "detail": "a scrub reads every block and repairs what it can"})
        elif p.get("scan_state") == "done" and (p.get("scan_age_d") or 0) > SCRUB_OVERDUE_D:
            out.append({"severity": "warn", "pool": name,
                        "message": f"Pool {name} was last scrubbed {p['scan_age_d']} days ago.",
                        "detail": "the weekly scrub has stopped running"})
        if p.get("redundancy") == "none" and (p.get("disks") or 0) >= 1:
            out.append({"severity": "info", "pool": name,
                        "message": f"Pool {name} has no redundancy: one failing disk loses the pool.",
                        "detail": "back it up or mirror it; ZFS can attach a mirror to a single disk in place"})
        if p.get("frag_pct") is not None and p["frag_pct"] >= FRAG_PCT:
            out.append({"severity": "info", "pool": name, "message": f"Pool {name} is {p['frag_pct']}% fragmented.",
                        "detail": "free space is scattered; writes get slower"})
    rank = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: rank[f["severity"]])
    return out


def _int(v: str) -> int:
    return int(float(str(v).rstrip("%x")))


def _float(v: str) -> float:
    return float(str(v).rstrip("x"))
