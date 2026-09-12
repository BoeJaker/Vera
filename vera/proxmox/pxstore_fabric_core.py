"""pxstore_fabric_core.py -- the pure decisions behind running the Storage panel
on the Vera File Fabric (VFS-02) instead of on the hypervisor.

Why this exists
---------------
The Storage panel was written when the PVE host itself was the file server:
Samba on the node, an NFS server installed on the node to share the model
store, and model pulls routed through ONE Ollama instance given a writable
mount. Three things made that wrong:

  * VFS-02 (CT160) now serves every share, the estate tree and the model store,
    and the hypervisor Samba it replaces had no sessions at all;
  * all five Ollama nodes mount the store READ-ONLY with OLLAMA_NOPRUNE=1, so a
    "writer instance" cannot write -- and a writable consumer is exactly the
    thing that can prune another node's blobs;
  * installing file servers on the hypervisor widens its attack surface.

So the store has one writer -- an Ollama bound to 127.0.0.1 on VFS-02 -- network
sharing reuses VFS-02's existing read-only export, and the legacy hypervisor
share is retired in stages, never switched off silently.

Everything here is app-free and I/O-free, so it is unit-testable in isolation
(tests/test_pxstore_fabric_core.py). The capability module does the SSH.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shlex
from typing import Dict, List, Optional, Tuple

FABRIC_HOST = "192.168.0.160"
FABRIC_SSH_LABEL = "VFS-02"

WRITER_UNIT = "ollama-store-writer.service"
WRITER_PORT = 11436
WRITER_STORE_MOUNT = "/srv/pools/tank_sdh/vera-store"
WRITER_MODELS_DIR = WRITER_STORE_MOUNT + "/models/ollama"
PULL_DIR = "/var/lib/ollama-store-writer/pulls"
PULL_UNIT_PREFIX = "vera-store-pull-"

# Host path -> the same filesystem inside VFS-02. Mirrors the rbind
# lxc.mount.entry lines in /etc/pve/lxc/160.conf. Longest prefix wins.
FABRIC_POOL_BINDS: Tuple[Tuple[str, str], ...] = (
    ("/rpool/data", "/srv/pools/rpool"),
    ("/mnt/BigDat", "/srv/pools/BigDat"),
    ("/var/lib/vz", "/srv/pools/vz"),
    ("/cpool", "/srv/pools/cpool"),
    ("/bpool", "/srv/pools/bpool"),
    ("/tank_sda", "/srv/pools/tank_sda"),
    ("/tank_sde", "/srv/pools/tank_sde"),
    ("/tank_sdf", "/srv/pools/tank_sdf"),
    ("/tank_sdh", "/srv/pools/tank_sdh"),
)

# Content-addressed blobs: an existing name already holds the same bytes, so
# never rewrite one under a serving node; skip half-written pulls.
CONSOLIDATE_RSYNC_FLAGS = "-a --ignore-existing --exclude '*-partial*' --info=stats2"

STORE_EXPORT_OPTS = "ro,sync,no_subtree_check,root_squash,crossmnt"


# ═════════════════════════════════════════════════════════════════════════════
#  PATHS
# ═════════════════════════════════════════════════════════════════════════════
def _clean_abs(path: str, what: str) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"{what} must be an absolute path: {path!r}")
    if any(c.isspace() for c in path) or ".." in path.split("/"):
        raise ValueError(f"{what} must not contain whitespace or '..': {path!r}")
    return path.rstrip("/") or "/"


def fabric_path(host_path: str) -> str:
    """Where a hypervisor path appears inside VFS-02.

    Raises ValueError for a path on a pool VFS-02 does not bind -- the caller
    must say so rather than hand out a path that does not exist on the server.
    """
    p = _clean_abs(host_path, "host path")
    best = None
    for host_root, fabric_root in FABRIC_POOL_BINDS:
        if p == host_root or p.startswith(host_root + "/"):
            if best is None or len(host_root) > len(best[0]):
                best = (host_root, fabric_root)
    if best is None:
        raise ValueError(f"{p} is not on a pool VFS-02 binds "
                         f"({', '.join(h for h, _ in FABRIC_POOL_BINDS)})")
    return best[1] + p[len(best[0]):]


# ═════════════════════════════════════════════════════════════════════════════
#  THE STORE WRITER  (one Ollama, loopback only, on VFS-02)
# ═════════════════════════════════════════════════════════════════════════════
def writer_unit(store_mount: str = WRITER_STORE_MOUNT,
                models_dir: str = WRITER_MODELS_DIR,
                port: int = WRITER_PORT) -> str:
    """The systemd unit for the store writer. edge/ollama-store-writer.service
    is this function's output for the defaults (a test holds them equal)."""
    store_mount = _clean_abs(store_mount, "store mount")
    models_dir = _clean_abs(models_dir, "models dir")
    port = int(port)
    return f"""[Unit]
Description=Ollama model-store writer (VFS-02, loopback only)
Documentation=https://github.com/ollama/ollama
After=network-online.target
Wants=network-online.target

[Service]
# The store is a nested ZFS mount. If it is absent, ollama would silently
# create an empty models dir on the container rootfs and pull into THAT.
ExecStartPre=/usr/bin/mountpoint -q {store_mount}
ExecStart=/usr/local/bin/ollama serve
User=root
Group=root
UMask=0022
StateDirectory=ollama-store-writer
Environment="HOME=/var/lib/ollama-store-writer"
Environment="OLLAMA_MODELS={models_dir}"
Environment="OLLAMA_HOST=127.0.0.1:{port}"
Environment="OLLAMA_NOPRUNE=1"
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


_MODEL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*){0,3}"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?$")


def valid_model(name: str) -> bool:
    """An Ollama model reference: name[:tag], optionally namespaced
    (user/name, hf.co/user/repo:Q4_K_M). Nothing a shell could reinterpret."""
    return (isinstance(name, str) and 0 < len(name) <= 256
            and ".." not in name and bool(_MODEL_RE.match(name)))


def pull_slug(model: str) -> str:
    """A systemd-unit-safe, collision-free job name for one model reference."""
    if not valid_model(model):
        raise ValueError(f"invalid model reference: {model!r}")
    base = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[:60]
    return f"{base}-{hashlib.sha1(model.encode()).hexdigest()[:8]}"


def pull_paths(model: str, pull_dir: str = PULL_DIR) -> Tuple[str, str]:
    slug = pull_slug(model)
    return PULL_UNIT_PREFIX + slug, f"{_clean_abs(pull_dir, 'pull dir')}/{slug}.jsonl"


def pull_start_script(model: str, port: int = WRITER_PORT,
                      pull_dir: str = PULL_DIR) -> str:
    """Start a pull on the writer as a transient systemd unit.

    Detached on purpose: a big model downloads for an unbounded time, and an
    SSH session or HTTP request must not be what keeps it alive. Progress is
    the writer's own /api/pull JSON stream, appended to a per-model log that
    pull_status_script reads back.

    Prints ALREADY_RUNNING, WRITER_DOWN (exit 4) or STARTED.
    """
    unit, log = pull_paths(model, pull_dir)
    body = json.dumps({"model": model})
    inner = (f"curl -sN http://127.0.0.1:{int(port)}/api/pull -d {shlex.quote(body)} "
             f"> {shlex.quote(log)} 2>&1; "
             f"printf '{{\"exit\":%d}}\\n' \"$?\" >> {shlex.quote(log)}")
    return "\n".join([
        f"if systemctl is-active -q {shlex.quote(unit)}; then echo ALREADY_RUNNING; exit 0; fi",
        f"systemctl is-active -q {WRITER_UNIT} || {{ echo WRITER_DOWN; exit 4; }}",
        f"mkdir -p {shlex.quote(pull_dir)}",
        f"systemctl reset-failed {shlex.quote(unit)} >/dev/null 2>&1",
        f": > {shlex.quote(log)}",
        f"systemd-run --quiet --collect --unit={shlex.quote(unit)} "
        f"/bin/sh -c {shlex.quote(inner)}",
        "echo STARTED",
    ])


def pull_status_script(model: str, pull_dir: str = PULL_DIR) -> str:
    unit, log = pull_paths(model, pull_dir)
    return (f"echo \"state=$(systemctl is-active {shlex.quote(unit)} 2>/dev/null)\"\n"
            f"echo '###LOG'\n"
            f"tail -n 200 {shlex.quote(log)} 2>/dev/null")


def parse_pull_log(lines: List[str], unit_active: bool) -> Dict:
    """Fold the writer's /api/pull stream into one progress record.

    Ollama reports each layer separately ({digest, total, completed}); the
    overall figure is the sum over layers, each at its furthest point.
    """
    layers: Dict[str, Tuple[int, int]] = {}
    status, error, exit_code, noise = "", "", None, []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            noise.append(line)
            continue
        if not isinstance(ev, dict):
            continue
        if "exit" in ev:
            exit_code = ev.get("exit")
            continue
        if ev.get("error"):
            error = str(ev["error"])
        if ev.get("status"):
            status = str(ev["status"])
        dg = ev.get("digest")
        if dg and ev.get("total"):
            t0, c0 = layers.get(dg, (0, 0))
            layers[dg] = (max(t0, int(ev.get("total") or 0)),
                          max(c0, int(ev.get("completed") or 0)))
    total = sum(t for t, _ in layers.values())
    completed = sum(min(c, t) for t, c in layers.values())
    if not error and noise and exit_code not in (None, 0):
        error = noise[-1][:300]
    if unit_active:
        state = "running"
    elif error:
        state = "failed"
    elif status == "success":
        state = "done"
    elif exit_code is None and not status:
        state = "unknown"
    else:
        state = "failed"
        error = (f"pull ended without success (exit {exit_code})"
                 if exit_code is not None else "pull ended without success")
    return {"state": state, "status": status, "error": error,
            "completed": completed, "total": total,
            "percent": round(completed * 100.0 / total, 1) if total else None,
            "exit": exit_code}


def is_read_only_error(err: str) -> bool:
    """A direct pull into an instance that serves the shared, read-only store."""
    e = (err or "").lower()
    return "read-only file system" in e or "read only file system" in e


# ═════════════════════════════════════════════════════════════════════════════
#  NETWORK SHARING  (reuse VFS-02's read-only export)
# ═════════════════════════════════════════════════════════════════════════════
def _network(spec: str):
    try:
        return ipaddress.ip_network(spec.strip(), strict=False)
    except (ValueError, AttributeError):
        return None


def valid_client(spec: str) -> bool:
    return _network(spec or "") is not None


def covers(export_client: str, requested: str) -> bool:
    """Does an export's client spec (IP/CIDR) already admit `requested`?"""
    a, b = _network(export_client), _network(requested)
    if a is None or b is None or a.version != b.version:
        return False
    return b.subnet_of(a)


def parse_exports(text: str) -> Dict[str, List[Tuple[str, str]]]:
    """/etc/exports -> {path: [(client, opts), ...]}."""
    out: Dict[str, List[Tuple[str, str]]] = {}
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        path, rest = parts[0].rstrip("/") or "/", parts[1:]
        for tok in rest:
            m = re.match(r"^([^()]+)\(([^)]*)\)$", tok)
            if m:
                out.setdefault(path, []).append((m.group(1), m.group(2)))
    return out


def export_for(exports: Dict[str, List[Tuple[str, str]]], path: str,
               client: str) -> Optional[Dict]:
    for spec, opts in exports.get(path.rstrip("/"), []):
        if covers(spec, client):
            return {"client": spec, "opts": opts,
                    "read_only": "ro" in opts.split(",")}
    return None


def add_export_client_script(path: str, client: str,
                             opts: str = STORE_EXPORT_OPTS,
                             exports_file: str = "/etc/exports") -> str:
    """Admit one more client to an existing export line (or add the line),
    keeping a backup, then re-export. Read-only options by default."""
    path = _clean_abs(path, "export path")
    net = _network(client or "")
    if net is None:
        raise ValueError(f"client must be an IP or CIDR: {client!r}")
    if not re.match(r"^[a-z0-9_,=]+$", opts):
        raise ValueError(f"invalid export options: {opts!r}")
    ef = shlex.quote(exports_file)
    entry = shlex.quote(f"{net.with_prefixlen}({opts})")
    p = shlex.quote(path)
    return "\n".join([
        "set -e",
        f"cp -p {ef} {ef}.vera-bak",
        f"if awk -v p={p} '$1==p{{f=1}} END{{exit !f}}' {ef}; then",
        f"  awk -v p={p} -v c={entry} '$1==p{{$0=$0\" \"c}} {{print}}' {ef} > {ef}.vera-new",
        f"  cat {ef}.vera-new > {ef} && rm -f {ef}.vera-new",
        "else",
        f"  printf '%s %s\\n' {p} {entry} >> {ef}",
        "fi",
        "exportfs -ra",
        "echo EXPORT_OK",
    ])


_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")


def fstab_line(server: str, remote_path: str, local_path: str,
               ro: bool = True) -> str:
    """An NFS client fstab entry that cannot hang a boot when the server is
    down (_netdev,nofail) and matches the 4.2 export VFS-02 serves."""
    if not isinstance(server, str) or not (_HOST_RE.match(server) or valid_client(server)):
        raise ValueError(f"invalid NFS server: {server!r}")
    remote_path = _clean_abs(remote_path, "remote path")
    local_path = _clean_abs(local_path, "local path")
    opts = ("ro" if ro else "rw") + ",hard,vers=4.2,_netdev,nofail"
    return f"{server}:{remote_path} {local_path} nfs {opts} 0 0"


# ═════════════════════════════════════════════════════════════════════════════
#  CONSUMERS  (which containers mount the store)
# ═════════════════════════════════════════════════════════════════════════════
_CONF_MP_RE = re.compile(
    r"^/etc/pve/lxc/(?P<vmid>\d+)\.conf:(?P<key>mp\d+):\s*(?P<val>.+)$")


def parse_store_consumers(grep_output: str, store_mount: str) -> List[Dict]:
    """`grep -H <store> /etc/pve/lxc/*.conf` -> one record per bind mount."""
    store_mount = store_mount.rstrip("/")
    out = []
    for raw in (grep_output or "").splitlines():
        m = _CONF_MP_RE.match(raw.strip())
        if not m:
            continue
        fields = m.group("val").split(",")
        host = fields[0].strip()
        if not (host == store_mount or host.startswith(store_mount + "/")):
            continue
        opts = dict(f.split("=", 1) for f in fields[1:] if "=" in f)
        out.append({"vmid": int(m.group("vmid")), "mp_key": m.group("key"),
                    "host_path": host, "ct_path": opts.get("mp", ""),
                    "ro": opts.get("ro", "0") in ("1", "true", "yes")})
    return sorted(out, key=lambda r: (r["vmid"], r["mp_key"]))


# ═════════════════════════════════════════════════════════════════════════════
#  BACKUP TARGET  (vzdump into the fabric's backup share)
# ═════════════════════════════════════════════════════════════════════════════
_STORAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,30}$")


def backup_target_script(storage_id: str, dataset_mount: str,
                         subdir: str = "pve", keep_last: int = 3) -> str:
    """Register a PVE dir storage for backups inside the fabric's backup
    dataset. `is_mountpoint` makes PVE treat it as offline -- instead of
    writing dumps onto the root disk -- if that dataset is ever not mounted.

    Prints ALREADY_EXISTS, NOT_MOUNTED (exit 3) or TARGET_OK."""
    if not _STORAGE_ID_RE.match(storage_id or ""):
        raise ValueError(f"invalid PVE storage id: {storage_id!r}")
    mount = _clean_abs(dataset_mount, "backup dataset mount")
    if not re.match(r"^[A-Za-z0-9_.-]+$", subdir or ""):
        raise ValueError(f"invalid subdir: {subdir!r}")
    keep = max(1, int(keep_last))
    path = f"{mount}/{subdir}"
    sid, m, p = shlex.quote(storage_id), shlex.quote(mount), shlex.quote(path)
    return "\n".join([
        "set -e",
        f"if pvesm status --storage {sid} >/dev/null 2>&1; then echo ALREADY_EXISTS; exit 0; fi",
        f"mountpoint -q {m} || {{ echo NOT_MOUNTED; exit 3; }}",
        f"mkdir -p {p}",
        f"pvesm add dir {sid} --path {p} --content backup "
        f"--is_mountpoint {m} --prune-backups keep-last={keep}",
        "echo TARGET_OK",
    ])


# ═════════════════════════════════════════════════════════════════════════════
#  LEGACY HYPERVISOR SHARE  (retire in stages, never silently)
# ═════════════════════════════════════════════════════════════════════════════
_SESSIONS = "$(smbstatus -b 2>/dev/null | sed '1,/^----/d' | grep -c . || true)"

LEGACY_PROBE_SCRIPT = "\n".join([
    "echo \"active=$(systemctl is-active smbd 2>/dev/null)\"",
    "echo \"enabled=$(systemctl is-enabled smbd 2>/dev/null)\"",
    f"echo \"sessions={_SESSIONS}\"",
    "echo \"listening=$(ss -Hltn 'sport = :445' 2>/dev/null | wc -l)\"",
])


def parse_legacy_probe(stdout: str) -> Dict:
    kv = {}
    for line in (stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {"active": kv.get("active") == "active",
            "enabled": kv.get("enabled") == "enabled",
            "sessions": _int(kv.get("sessions")),
            "listening": (_int(kv.get("listening")) or 0) > 0}


def retire_legacy_script(force: bool = False) -> str:
    """Stop and disable the hypervisor Samba. Config and share tree are kept,
    so restore_legacy_script() brings it straight back. Refuses while anyone
    is connected unless forced. Prints IN_USE <n> (exit 5) or RETIRED."""
    guard = [] if force else [
        f"n={_SESSIONS}",
        "if [ \"${n:-0}\" -gt 0 ]; then echo \"IN_USE $n\"; exit 5; fi",
    ]
    return "\n".join(guard + [
        "systemctl disable --now smbd nmbd >/dev/null 2>&1 || true",
        "systemctl is-active -q smbd && { echo STILL_ACTIVE; exit 6; }",
        "echo RETIRED",
    ])


def restore_legacy_script() -> str:
    return "\n".join([
        "systemctl enable --now smbd nmbd >/dev/null 2>&1 || true",
        "systemctl is-active -q smbd && echo RESTORED || { echo RESTORE_FAILED; exit 6; }",
    ])
