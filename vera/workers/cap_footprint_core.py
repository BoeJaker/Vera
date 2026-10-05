"""
cap_footprint_core.py — what a capability touches on the Vera host (no app imports)

Node parity (owner, 2026-10-04: "near enough full parity with the main vera
host", vetted in tiers) needs EVIDENCE per capability that it uses nothing only
the host has. A static audit reads the source; this reads what the host
process actually did while a capability ran. Python's audit hooks report every
file open, SQLite connect, process spawn and socket connect; the tracer
attributes each to the capabilities on RUNNING_CAPS and keeps a small
footprint per capability.

A footprint is host-bound when it shows any of:
  file_write / file_read   a file outside the code, temp and system trees
  sqlite                   the host's SQLite stores (the fabric, markets, ...)
  subprocess               a process on the host (docker, git, ssh, ffmpeg ...)
  socket_local             a host-local service: the host's own API, a unix
                           socket (docker.sock), loopback ports that are not a
                           shared backend
Fine on a node: socket_shared (Redis/Postgres/Neo4j/Chroma - shared, nodes
reach them on the LAN), socket_node (the Ollama/NLP/media nodes),
socket_external (the internet).

A capability is VETTED only when both say so: the static audit finds no host
marker, and it has run often enough with a footprint that is node-fine.

Pure: events in, decisions out. Tested without a host.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, Optional, Set, Tuple

#: The audit events the tracer listens to.
WATCH = frozenset({"open", "sqlite3.connect", "subprocess.Popen", "os.system",
                   "os.posix_spawn", "os.exec", "socket.connect"})

HOST_BOUND = frozenset({"file_write", "file_read", "sqlite", "subprocess", "socket_local"})
NODE_FINE = frozenset({"socket_shared", "socket_node", "socket_external"})

#: Runs a capability needs before a clean footprint counts as evidence.
MIN_RUNS = 3
#: Static markers the footprint can settle: the source opens files, but static
#: analysis cannot tell a code/template read (every node has the code) from a
#: data file. The tracer can - it ignores reads in the code tree - so these
#: need more observed runs instead of disqualifying.
SOFT_MARKERS = frozenset({"localfs"})
MIN_RUNS_SOFT = 5
#: Targets kept per kind (the footprint stays small).
MAX_TARGETS = 6

#: Paths a node has too, or that are nobody's state.
_READ_OK = ("/proc/", "/sys/", "/dev/", "/etc/", "/usr/", "/lib", "/opt/vera-store/",
            "/tmp/", "/var/tmp/")
_WRITE_OK = ("/tmp/", "/var/tmp/", "/dev/null", "/proc/self/")

FOOTPRINT_KEY = "vera:caps:footprint"          # hash cap -> json footprint
VETTED_KEY = "vera:node_workers:vetted"        # hash cap -> json {cls, why, at}
VETTED_CLASS = "extended"


def _is_write(mode: Any, flags: Any) -> bool:
    if isinstance(mode, str) and mode:
        return any(c in mode for c in "wax+")
    try:
        return bool(int(flags or 0) & 0o3)        # O_WRONLY | O_RDWR
    except (TypeError, ValueError):
        return False


def classify_open(path: Any, mode: Any, flags: Any, code_roots: Iterable[str] = (),
                  code_files: Optional[frozenset] = None) -> Optional[Tuple[str, str]]:
    if not isinstance(path, str) or not path:
        return None                                 # an fd, bytes path: nothing to say
    if path.endswith((".py", ".pyc", ".pyi", ".so")) or "/site-packages/" in path \
            or "/dist-packages/" in path or "/__pycache__/" in path:
        return None
    if any(path.startswith(r) for r in code_roots if r):
        # the code tree: reading a TRACKED file (code, templates, static) is
        # fine - every node has the same commit. An untracked file there is
        # runtime data the host wrote (vera/ide/.vera_remote_instances.json,
        # found 2026-10-04): a node has none, so it is host-bound. A write
        # anywhere in the tree is state.
        if not _is_write(mode, flags):
            if code_files is None or path in code_files:
                return None
            return "file_read", path
    write = _is_write(mode, flags)
    if write:
        if path.startswith(_WRITE_OK):
            return None
        return "file_write", path
    if path.startswith(_READ_OK):
        return None
    return "file_read", path


def _ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def classify_socket(address: Any, shared: Set[Tuple[str, int]], shared_ports: Set[int],
                    nodes: Set[str], host_ips: Set[str]) -> Optional[Tuple[str, str]]:
    """`shared`: (host, port) of the shared backends; `shared_ports`: their
    ports (the host reaches them on loopback, a node on the LAN); `nodes`: the
    node IPs; `host_ips`: this host's own addresses."""
    if isinstance(address, (str, bytes)):
        a = address.decode() if isinstance(address, bytes) else address
        return ("socket_local", "unix:" + a) if a else None
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    host, port = str(address[0]), address[1]
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    target = f"{host}:{port}"
    if (host, port) in shared:
        return "socket_shared", target
    ip = _ip(host)
    local = (ip is not None and ip.is_loopback) or host in ("localhost", "") or host in host_ips
    if local:
        return ("socket_shared" if port in shared_ports else "socket_local"), target
    if host in nodes:
        return "socket_node", target
    # anything else - the internet, or another LAN machine (Proxmox, NAS, HA),
    # which a node reaches the same way; the target is kept for a reviewer
    return "socket_external", target


def classify(event: str, args: Tuple, *, code_roots=(), shared=frozenset(), shared_ports=frozenset(),
             nodes=frozenset(), host_ips=frozenset(),
             code_files: Optional[frozenset] = None) -> Optional[Tuple[str, str]]:
    try:
        if event == "open":
            return classify_open(args[0], args[1] if len(args) > 1 else None,
                                 args[2] if len(args) > 2 else 0, code_roots, code_files)
        if event == "sqlite3.connect":
            return "sqlite", str(args[0]) if args else "?"
        if event in ("subprocess.Popen", "os.posix_spawn", "os.exec"):
            exe = args[0] if args else "?"
            return "subprocess", str(exe)[:120]
        if event == "os.system":
            return "subprocess", (str(args[0])[:60] if args else "?")
        if event == "socket.connect":
            return classify_socket(args[1] if len(args) > 1 else None, set(shared),
                                   set(shared_ports), set(nodes), set(host_ips))
    except Exception:
        return None
    return None


def merge(old: Optional[Dict[str, Any]], kinds: Dict[str, int], targets: Dict[str, Iterable[str]],
          runs: int, now: float) -> Dict[str, Any]:
    fp = dict(old or {})
    fp["runs"] = int(fp.get("runs") or 0) + int(runs)
    # runs seen by a tracer that tells tracked code files from runtime data in
    # the code tree: only these settle a source that opens files (SOFT_MARKERS)
    fp["runs_tracked"] = int(fp.get("runs_tracked") or 0) + int(runs)
    k = dict(fp.get("kinds") or {})
    for kind, n in kinds.items():
        k[kind] = int(k.get(kind) or 0) + int(n)
    fp["kinds"] = k
    t = {kk: list(v) for kk, v in (fp.get("targets") or {}).items()}
    for kind, vals in targets.items():
        cur = t.setdefault(kind, [])
        for v in vals:
            if v not in cur and len(cur) < MAX_TARGETS:
                cur.append(v)
    fp["targets"] = t
    fp.setdefault("first", now)
    fp["last"] = now
    return fp


def static_markers(rows: Dict[str, Dict[str, Any]], safe: Iterable[str]) -> Dict[str, list]:
    """Each capability's effective static markers (node_cap_audit rows): its
    own, `state` for module-state writes, and `calls_hostbound` when a cap it
    calls in-process is itself host-bound - to a fixed point, since a caller
    runs its callee where it runs."""
    safe = set(safe or ())
    out: Dict[str, list] = {}
    for c, r in (rows or {}).items():
        m = set(r.get("markers") or ())
        if r.get("state"):
            m.add("state")
        out[c] = sorted(m)
    changed = True
    while changed:
        changed = False
        for c, r in (rows or {}).items():
            if out[c]:
                continue
            for x in r.get("calls") or ():
                if x in safe:
                    continue
                if x not in out or out[x]:
                    out[c] = ["calls_hostbound"]
                    changed = True
                    break
    return out


def verdict(fp: Optional[Dict[str, Any]], static_markers: Iterable[str],
            static_known: bool = True) -> Tuple[str, str]:
    """('vetted' | 'host' | 'unproven', why) for one capability."""
    sm = sorted(set(static_markers or ()))
    if not static_known:
        return "unproven", "no static audit for it"
    hard = [m for m in sm if m not in SOFT_MARKERS]
    if hard:
        return "host", "source: " + ", ".join(sm)
    fp = fp or {}
    bad = sorted(k for k, n in (fp.get("kinds") or {}).items() if n and k in HOST_BOUND)
    if bad:
        tg = fp.get("targets") or {}
        return "host", "observed: " + "; ".join(f"{k} {', '.join((tg.get(k) or [])[:2])}" for k in bad)
    # a source that opens files is settled only by runs whose file reads were
    # checked against the tracked files (footprints from before that check
    # counted any read in the code tree as harmless)
    runs = int(fp.get("runs_tracked" if sm else "runs") or 0)
    need = MIN_RUNS_SOFT if sm else MIN_RUNS
    if runs < need:
        return "unproven", (f"{runs} observed run(s), needs {need}"
                            + (" (source reads files: runs decide whether only code files)" if sm else ""))
    seen = sorted(k for k, n in (fp.get("kinds") or {}).items() if n)
    touched = ", ".join(s.replace('socket_', '') for s in seen) or "nothing on the host"
    if sm:
        return "vetted", (f"source reads files, but {runs} runs read only the code tree "
                          f"(which a node has) and touched {touched}")
    return "vetted", f"source clean; {runs} runs touched only {touched}"
