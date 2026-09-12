"""Rules for estate.health: turn raw facts about the state store, the Vera
host's containers and the Proxmox guests into one findings list.

No app imports, so it tests without booting Vera
(tests/test_estate_health_core.py).

Why it exists: at the 2 Sep 2026 boot the host's own redis-server won port
6379, and Vera ran ten days against a store without its estate settings. Two
containers lost a port race at the same boot, and most infrastructure guests
were not set to start at boot. Nothing on screen said so.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

ERROR, WARN, INFO = "error", "warn", "info"
_RANK = {ERROR: 0, WARN: 1, INFO: 2}

SECTIONS = {
    "state_store": "State store",
    "containers": "Containers on the Vera host",
    "guests": "Proxmox guests",
}
_SECTION_NOUN = {
    "state_store": "the state store",
    "containers": "the Vera host's containers",
    "guests": "the Proxmox guests",
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


def finding(severity: str, section: str, subject: str, message: str,
            detail: str = "") -> Dict[str, str]:
    return {"severity": severity, "section": section, "subject": subject,
            "message": message, "detail": detail}


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
