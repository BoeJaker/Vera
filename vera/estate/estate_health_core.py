"""Rules for estate.health: turn raw facts about the state store, the Vera
host's containers, the Proxmox guests, backups, disks and the core services
into one findings list.

No app imports, so it tests without booting Vera
(tests/test_estate_health_core.py, tests/test_estate_overview_warnings.py).

Why it exists: at the 2 Sep 2026 boot the host's own redis-server won port
6379, and Vera ran ten days against a store without its estate settings. Two
containers lost a port race at the same boot, and most infrastructure guests
were not set to start at boot. Nothing on screen said so.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

ERROR, WARN, INFO = "error", "warn", "info"
_RANK = {ERROR: 0, WARN: 1, INFO: 2}

SECTIONS = {
    "state_store": "State store",
    "containers": "Containers on the Vera host",
    "guests": "Proxmox guests",
    "backups": "Backups",
    "storage": "Disks and storage",
    "services": "File fabric and directory",
    "certificates": "Certificates",
}
_SECTION_NOUN = {
    "state_store": "the state store",
    "containers": "the Vera host's containers",
    "guests": "the Proxmox guests",
    "backups": "the backup system",
    "storage": "the disks",
    "services": "the file fabric and directory",
    "certificates": "the certificates",
}

# Keys only the estate store holds: each is written the first time its feature
# is configured, so a store without them is not the one Vera was set up against.
# vera:autoenroll:config is left out on purpose - it exists only once auto-enrol
# settings are saved, and prod never saved any.
EXPECTED_STATE_KEYS: Dict[str, str] = {
    "vera:proxmox:clusters": "Proxmox cluster records",
    "vera:provisioning:identity": "directory settings",
    "vera:provisioning:ssh_hosts": "SSH enrolment hosts",
    "vera:netsec:mesh": "host mesh",
}

# The Debian package's binary. The compose container reports /data/redis-server.
HOST_PACKAGE_REDIS = ("/usr/bin/redis-server", "/usr/sbin/redis-server")

RESTARTING_POLICIES = ("always", "unless-stopped", "on-failure")
_NOT_RUNNING = ("exited", "created", "dead")
_PORT_CLASH = ("port is already allocated", "address already in use", "bind for")

# A nightly job that has not produced a backup in a day and a half has stopped.
BACKUP_STALE_H = 36


def finding(severity: str, section: str, subject: str, message: str,
            detail: str = "") -> Dict[str, str]:
    return {"severity": severity, "section": section, "subject": subject,
            "message": message, "detail": detail}


def _sentence(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


# ── state store ──────────────────────────────────────────────────────────────

def state_store_section(info: Mapping[str, Any], present: Mapping[str, bool],
                        url: str = "", keys: Optional[int] = None) -> Dict[str, Any]:
    findings = []
    missing = [k for k in EXPECTED_STATE_KEYS if not present.get(k)]
    if missing:
        what = ", ".join(EXPECTED_STATE_KEYS[k] for k in missing)
        findings.append(finding(
            ERROR, "state_store", url or "redis",
            f"Vera's state store has no {what}. It is probably connected to the wrong Redis.",
            "Missing keys: " + ", ".join(missing)))
    executable = str(info.get("executable") or "")
    if executable in HOST_PACKAGE_REDIS:
        findings.append(finding(
            WARN, "state_store", url or "redis",
            "Vera is talking to the host's own redis-server, not the Redis container "
            "that holds its state.",
            f"redis-server binary: {executable}"))
    facts = {"url": url, "version": str(info.get("redis_version") or ""),
             "executable": executable, "keys": keys,
             "expected_present": len(EXPECTED_STATE_KEYS) - len(missing),
             "expected_total": len(EXPECTED_STATE_KEYS)}
    return {"facts": facts, "findings": findings}


def startup_lines(section: Mapping[str, Any]) -> List[str]:
    """What the boot log should say about the state store: one line per
    finding, with its detail, or the reason the check could not run."""
    if section.get("error"):
        return [f"could not check the state store: {section['error']}"]
    return [f"{f['message']} {f['detail']}".strip() if f.get("detail") else f["message"]
            for f in section.get("findings") or []]


# ── containers ───────────────────────────────────────────────────────────────

def is_sandbox(name: str, labels: Optional[Mapping[str, str]]) -> bool:
    """Session sandboxes and Loop Lab dev sandboxes are stopped and paused on
    purpose, and they belong to whoever is using them."""
    name = (name or "").lstrip("/")
    return ("vera.sandbox" in (labels or {}) or name.startswith("vera-sbx-")
            or "vera-dev" in name)


def needs_inspection(name: str, labels: Optional[Mapping[str, str]], state: str) -> bool:
    """Only stopped infrastructure containers need their restart policy and
    start error read; the host runs hundreds of containers."""
    return not is_sandbox(name, labels) and (state or "") in _NOT_RUNNING


def _stopped(finished_at: Any) -> str:
    ts = str(finished_at or "")
    return "never ran" if not ts or ts.startswith("0001") else f"stopped {ts[:10]}"


def container_section(containers: Iterable[Mapping[str, Any]], listed: int = 0,
                      sandboxes: int = 0) -> Dict[str, Any]:
    findings = []
    down = 0
    for c in containers:
        name = str(c.get("name") or "").lstrip("/")
        labels = c.get("labels") or {}
        state = str(c.get("state") or "")
        if is_sandbox(name, labels) or state not in _NOT_RUNNING:
            continue
        down += 1
        restart = str(c.get("restart") or "")
        error = str(c.get("error") or "")
        project = labels.get("com.docker.compose.project", "")
        where = f"compose project {project}" if project else "not managed by compose"
        if any(p in error.lower() for p in _PORT_CLASH):
            findings.append(finding(
                ERROR, "containers", name,
                f"{name} could not start: another service already holds its port.",
                f"{error[:240]} ({where})"))
        elif state == "created":
            findings.append(finding(
                WARN if restart in RESTARTING_POLICIES else INFO, "containers", name,
                f"{name} was created but never started.",
                f"restart policy {restart or 'none'}; {where}"))
        elif restart in RESTARTING_POLICIES:
            findings.append(finding(
                WARN, "containers", name,
                f"{name} is stopped although it is set to restart.",
                f"exit code {c.get('exit_code')}, {_stopped(c.get('finished_at'))}; "
                f"restart policy {restart}; {where}"))
        else:
            findings.append(finding(
                INFO, "containers", name,
                f"{name} is stopped and has no restart policy.",
                f"exit code {c.get('exit_code')}, {_stopped(c.get('finished_at'))}; {where}"))
    facts = {"listed": listed, "sandboxes_ignored": sandboxes, "not_running": down}
    return {"facts": facts, "findings": findings}


# ── Proxmox guests ───────────────────────────────────────────────────────────

def cluster_host(record: Mapping[str, Any]) -> str:
    p = urlparse(str(record.get("api_url") or ""))
    return f"{p.hostname or ''}:{p.port or 8006}"


def group_clusters(records: Iterable[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    """Cluster records keyed by the Proxmox API they reach."""
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for r in records:
        groups.setdefault(cluster_host(r), []).append(r)
    return groups


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def guest_section(guests: Iterable[Mapping[str, Any]],
                  records: Iterable[Mapping[str, Any]] = (),
                  errors: Iterable[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    records = list(records)
    groups = group_clusters(records)
    findings = []
    for host, recs in groups.items():
        if len(recs) > 1:
            labels = ", ".join(f"\"{r.get('label') or r.get('id') or '?'}\"" for r in recs)
            findings.append(finding(
                WARN, "guests", host,
                f"{len(recs)} Proxmox cluster records point at the same host ({host}).",
                f"Records: {labels}. Anything tied to one record does not see the other."))
    running = 0
    for g in guests:
        if g.get("template") or g.get("status") != "running":
            continue
        running += 1
        kind = "VM" if g.get("type") == "qemu" else "CT"
        label = f"{g.get('name') or 'guest'} ({kind} {g.get('vmid')})"
        if g.get("config_error"):
            findings.append(finding(
                INFO, "guests", str(g.get("vmid")),
                f"Could not read the configuration of {label}, so its autostart is unknown.",
                str(g.get("config_error"))))
        elif not _truthy(g.get("onboot")):
            findings.append(finding(
                WARN, "guests", str(g.get("vmid")),
                f"{label} is running but will not start after a host reboot.",
                f"node {g.get('node')}; turn on Start at boot in its Proxmox options"))
    for e in errors:
        findings.append(finding(
            WARN, "guests", str(e.get("host") or "proxmox"),
            f"Could not read guests from {e.get('host') or 'Proxmox'}.",
            str(e.get("error") or "")))
    facts = {"clusters": len(groups), "records": len(records), "running_checked": running}
    return {"facts": facts, "findings": findings}


# ── backups ──────────────────────────────────────────────────────────────────

def backups_section(reports: Iterable[Mapping[str, Any]],
                    now: Optional[float] = None) -> Dict[str, Any]:
    """reports: one per Proxmox node, {node, status} where status is
    pxstore.backup.status's output or {error}."""
    now = time.time() if now is None else now
    findings = []
    nodes = enabled = ok = failed = 0
    latest_overall = 0
    for rep in reports:
        node = str(rep.get("node") or "Proxmox")
        st = rep.get("status") or {}
        nodes += 1
        if st.get("error"):
            findings.append(finding(WARN, "backups", node,
                                    f"Could not read the backup system on {node}.",
                                    str(st["error"])[:240]))
            continue
        for w in st.get("warnings") or []:
            findings.append(finding(WARN, "backups", node, _sentence(w), f"Proxmox node {node}"))
        enabled += sum(1 for j in st.get("jobs") or [] if j.get("enabled"))
        runs = st.get("runs") or []
        latest = 0
        for run in runs:
            latest = max(latest, int(run.get("at") or 0))
            if run.get("result") == "error":
                failed += 1
                findings.append(finding(
                    ERROR, "backups", str(run.get("guest") or "?"),
                    f"The last backup of {run.get('guest')} on {node} failed.",
                    str(run.get("line") or "")[:240]))
            elif run.get("result") == "ok":
                ok += 1
        latest_overall = max(latest_overall, latest)
        if not runs:
            findings.append(finding(INFO, "backups", node,
                                    f"No backup runs are recorded on {node}."))
        elif now - latest > BACKUP_STALE_H * 3600:
            hours = int((now - latest) // 3600)
            findings.append(finding(
                WARN, "backups", node,
                f"No guest on {node} has been backed up in {hours} hours.",
                "the nightly job may have stopped"))
    facts = {"nodes": nodes, "enabled_jobs": enabled, "guests_ok": ok,
             "guests_failed": failed, "latest_run_at": latest_overall or None}
    return {"facts": facts, "findings": findings}


# ── disks and storage ────────────────────────────────────────────────────────

_DISK_STATES = {
    "damaged": (WARN, "holds a damaged pool{pool}"),
    "importable": (INFO, "holds pool{pool}, which is not imported"),
    "labelled": (INFO, "carries a label, but nothing uses it"),
    "free": (INFO, "is unused"),
    "not mounted": (INFO, "has a filesystem that is not mounted"),
}


def _size(n: Any) -> str:
    try:
        return f"{round(int(n) / 1e9)} GB"
    except (TypeError, ValueError):
        return "unknown size"


def storage_section(disk_reports: Iterable[Mapping[str, Any]],
                    docker_disk: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """disk_reports: one per Proxmox node, {node, status} where status is
    pxstore.disks's output or {error}. docker_disk: docker.disk.status."""
    findings = []
    disks = in_use = 0
    for rep in disk_reports:
        node = str(rep.get("node") or "Proxmox")
        st = rep.get("status") or {}
        if st.get("error"):
            findings.append(finding(WARN, "storage", node, f"Could not read the disks on {node}.",
                                    str(st["error"])[:240]))
            continue
        for d in st.get("disks") or []:
            disks += 1
            state = str(d.get("state") or "")
            if state == "in use":
                in_use += 1
                continue
            rule = _DISK_STATES.get(state)
            if not rule:
                continue
            severity, text = rule
            pool = f" {d.get('pool')}" if d.get("pool") else ""
            usb = ", USB" if d.get("usb") else ""
            what = f"{d.get('name')} ({_size(d.get('size'))}, {d.get('model') or 'unknown model'}{usb})"
            findings.append(finding(severity, "storage", f"{node}/{d.get('name')}",
                                    f"Disk {what} on {node} {text.format(pool=pool)}.",
                                    str(d.get("detail") or "")))
    used_pct = None
    if docker_disk is not None:
        if docker_disk.get("error"):
            findings.append(finding(WARN, "storage", "docker",
                                    "Could not read the Docker data disk on the Vera host.",
                                    str(docker_disk["error"])[:240]))
        else:
            used_pct = docker_disk.get("pct_used")
            level = docker_disk.get("level")
            if level in ("warn", "critical"):
                findings.append(finding(
                    ERROR if level == "critical" else WARN, "storage",
                    str(docker_disk.get("mount") or "docker"),
                    f"The Docker data disk on the Vera host is {used_pct}% full.",
                    str(docker_disk.get("note") or "")))
    facts = {"disks": disks, "in_use": in_use, "docker_disk_used_pct": used_pct}
    return {"facts": facts, "findings": findings}


# ── file fabric and directory ────────────────────────────────────────────────

def services_section(vfs: Optional[Mapping[str, Any]] = None,
                     identity: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """vfs: vfs.health's output or {error}; identity: identity.status's."""
    findings = []
    fabric = "unknown"
    if vfs is not None:
        if vfs.get("error"):
            findings.append(finding(WARN, "services", "VFS-02",
                                    "Could not check the file server (VFS-02).",
                                    str(vfs["error"])[:240]))
        else:
            down = sorted(name for name, up in (vfs.get("services") or {}).items() if not up)
            for name in down:
                findings.append(finding(ERROR, "services", f"VFS-02/{name}",
                                        f"{name} is down on the file server (VFS-02)."))
            if not vfs.get("estate_mounts"):
                findings.append(finding(WARN, "services", "VFS-02/estate",
                                        "VFS-02 has no estate mounts, so the estate share shows nothing.",
                                        "vfs.estate.sync rebuilds the tree"))
            fabric = "down" if down else "up"
    directory = "unknown"
    if identity is not None:
        if "configured" not in identity and identity.get("error"):
            findings.append(finding(WARN, "services", "FreeIPA",
                                    "Could not check the directory (FreeIPA).",
                                    str(identity["error"])[:240]))
        elif not identity.get("configured"):
            directory = "not configured"
            findings.append(finding(WARN, "services", "FreeIPA",
                                    "No directory (FreeIPA) is configured in Vera.",
                                    "set it in Estate > Trust > Identity"))
        elif not identity.get("reachable"):
            directory = "unreachable"
            findings.append(finding(ERROR, "services", "FreeIPA",
                                    "The directory (FreeIPA) is not reachable.",
                                    str(identity.get("error") or "")[:240]))
        else:
            directory = f"FreeIPA {identity.get('version') or ''}".strip()
    facts = {"file_fabric": fabric, "estate_mounts": (vfs or {}).get("estate_mounts"),
             "directory": directory}
    return {"facts": facts, "findings": findings}


# ── certificates ─────────────────────────────────────────────────────────────

def certificates_section(certs: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """certs: certs.list's output or {error}. Its findings already say what has
    expired or is about to; only expiry and unreadable sources reach the Overview,
    the informational rows (self-signed, superseded) stay on the Certificates view."""
    if certs is None or certs.get("error"):
        return {"error": str((certs or {}).get("error") or "certs.list did not answer")}
    findings = [finding(f["severity"], "certificates", f.get("subject", ""), f["message"], f.get("detail", ""))
                for f in certs.get("findings") or [] if f.get("severity") in (ERROR, WARN)]
    counts = certs.get("counts") or {}
    facts = {"certificates": len(certs.get("certs") or []),
             "expired": counts.get("expired", 0), "expiring": counts.get("expiring", 0),
             "soonest": certs.get("soonest")}
    return {"facts": facts, "findings": findings}


# ── all together ─────────────────────────────────────────────────────────────

def _order(f: Mapping[str, Any]):
    return (_RANK.get(f.get("severity"), 3), f.get("section", ""), f.get("subject", ""))


def summarize(results: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """One result: overall level, counts, every finding (errors first) and the
    per-section facts. A source that failed is itself a finding, never a blank."""
    sections: Dict[str, Any] = {}
    everything: List[Dict[str, Any]] = []
    for key, label in SECTIONS.items():
        res = results.get(key) or {}
        items = list(res.get("findings") or [])
        if res.get("error"):
            items.append(finding(WARN, key, label,
                                 f"Could not check {_SECTION_NOUN[key]}: {res['error']}"))
        items.sort(key=_order)
        sections[key] = {"label": label, "facts": res.get("facts") or {},
                         "elapsed_ms": res.get("elapsed_ms"),
                         "error": res.get("error") or "", "findings": items}
        everything.extend(items)
    counts = {s: sum(1 for f in everything if f["severity"] == s) for s in (ERROR, WARN, INFO)}
    level = ERROR if counts[ERROR] else WARN if counts[WARN] else "ok"
    return {"level": level, "counts": counts, "sections": sections,
            "findings": sorted(everything, key=_order)}
