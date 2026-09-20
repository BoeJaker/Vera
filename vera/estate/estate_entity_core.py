"""One record for one thing, joined from every reader that already knows it.

Twelve entity kinds appear on six or more Estate pages each, and every page
renders its own half of the same machine. This module takes what the existing
readers return - estate.machines, backup.status, certs.list, netsec.mesh.members,
identity.host.list, exec.ssh.hosts.list, integration.list, docker.hosts.list,
pxstore.inventory - and answers "what does the estate know about <kind>:<id>":
a title, its facts, its standing in each registration plane, the other
entities it touches, and where a reader can go next. Nothing new is collected.

Pure joins over plain dicts (tests/test_estate_entity_core.py).
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

# Relative, so the worktree's own vocabulary is used under tests (Vera.vera.*
# resolves to the main checkout) and the package's under the running app.
from .estate_nav_core import ENTITY_KINDS, entity_ref, parse_entity

# The registration planes every machine-like entity is measured against.
PLANES = ("ssh", "directory", "mesh", "certificate", "backup")


def _s(v: Any) -> str:
    return str(v or "").strip()


def _lower(v: Any) -> str:
    return _s(v).lower()


def _fact(label: str, value: Any) -> Dict[str, Any]:
    return {"label": label, "value": value}


def _plane(state: str, detail: str = "", ref: str = "") -> Dict[str, str]:
    """state: yes | no | n/a | unknown."""
    out = {"state": state, "detail": detail}
    if ref:
        out["ref"] = ref
    return out


def _link(label: str, ref: str) -> Dict[str, str]:
    return {"label": label, "ref": ref}


def _host_of_url(url: str) -> str:
    u = _s(url)
    u = u.split("://", 1)[1] if "://" in u else u
    return u.split("/", 1)[0].split(":", 1)[0].strip("[]").lower()


def _age(epoch: Any, now: float) -> str:
    try:
        secs = now - float(epoch)
    except (TypeError, ValueError):
        return ""
    if secs < 0:
        return "in the future"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{int(secs // size)} {unit} ago"
    return "just now"


# ── the readers, indexed once ─────────────────────────────────────────────────

class Sources:
    """The readers' outputs, as given; each index is built lazily."""

    def __init__(self, machines=None, backups=None, certs=None, mesh=None, identity=None,
                 ssh_hosts=None, integrations=None, docker_hosts=None, inventory=None,
                 containers=None, errors: Optional[Mapping[str, str]] = None, now: Optional[float] = None):
        self.machines = list(machines or [])
        self.containers = list(containers or [])      # docker.ps rows (Engine shape), local host
        self.backups = dict(backups or {})            # backup.status: guests, schedules
        self.certs = list(certs or [])
        self.mesh = list(mesh or [])
        self.identity = list(identity or [])          # [{fqdn, enrolled}]
        self.ssh_hosts = list(ssh_hosts or [])
        self.integrations = list(integrations or [])
        self.docker_hosts = list(docker_hosts or [])
        self.inventory = dict(inventory or {})        # pools, datasets, guests, storages
        self.errors = dict(errors or {})
        self.now = time.time() if now is None else now

    # machines
    def machine_by_vmid(self, vmid: Any) -> Dict[str, Any]:
        v = _s(vmid).split(":")[-1]
        return next((m for m in self.machines if m.get("kind") == "guest" and _s(m.get("vmid")) == v), {})

    def machine_by_host_id(self, hid: str) -> Dict[str, Any]:
        return next((m for m in self.machines if m.get("ssh_host_id") == hid or m.get("id") == hid), {})

    def machine_by_addr(self, addr: str) -> Dict[str, Any]:
        a = _lower(addr)
        if not a:
            return {}
        return next((m for m in self.machines if _lower(m.get("addr")) == a or a in [_lower(x) for x in m.get("ips") or []]), {})

    def machine_by_name(self, name: str) -> Dict[str, Any]:
        n = _lower(name).split(".")[0]
        return next((m for m in self.machines if _lower(m.get("label")).split(".")[0] == n), {}) if n else {}

    # joins by address / name
    def addrs_of(self, m: Mapping[str, Any]) -> List[str]:
        out = [_lower(m.get("addr"))] + [_lower(x) for x in m.get("ips") or []]
        return [a for a in out if a]

    def ssh_for(self, m: Mapping[str, Any]) -> Dict[str, Any]:
        if m.get("ssh_host_id"):
            rec = next((h for h in self.ssh_hosts if h.get("id") == m["ssh_host_id"]), None)
            if rec:
                return rec
        addrs = self.addrs_of(m)
        return next((h for h in self.ssh_hosts if _lower(h.get("host")) in addrs), {})

    def identity_for(self, m: Mapping[str, Any]) -> Dict[str, Any]:
        n = _lower(m.get("label")).split(".")[0]
        return next((h for h in self.identity if _lower(h.get("fqdn")).split(".")[0] == n), {}) if n else {}

    def mesh_for(self, m: Mapping[str, Any]) -> Dict[str, Any]:
        addrs = self.addrs_of(m)
        hid = m.get("ssh_host_id")
        return next((x for x in self.mesh if (hid and x.get("host_id") == hid) or _lower(x.get("host")) in addrs), {})

    def certs_for(self, m: Mapping[str, Any]) -> List[Dict[str, Any]]:
        addrs = set(self.addrs_of(m))
        n = _lower(m.get("label")).split(".")[0]
        out = []
        for c in self.certs:
            where = _lower(c.get("where")).split(":")[0]
            names = [_lower(x) for x in c.get("names") or []] + [_lower(c.get("name"))]
            if (where and where in addrs) or (n and any(x.split(".")[0] == n for x in names if x)):
                out.append(c)
        return out

    def backup_for(self, vmid: Any) -> Dict[str, Any]:
        v = _s(vmid).split(":")[-1]
        return next((g for g in self.backups.get("guests") or [] if _s(g.get("vmid")) == v), {})

    def integrations_for(self, m: Mapping[str, Any]) -> List[Dict[str, Any]]:
        addrs = set(self.addrs_of(m))
        return [i for i in self.integrations if _host_of_url(i.get("base_url", "")) in addrs]

    def docker_host_for(self, m: Mapping[str, Any]) -> Dict[str, Any]:
        if m.get("docker_host_id"):
            rec = next((d for d in self.docker_hosts if d.get("id") == m["docker_host_id"]), None)
            if rec:
                return rec
        addrs = set(self.addrs_of(m))
        return next((d for d in self.docker_hosts if _host_of_url(d.get("url", "")) in addrs
                     or (d.get("ssh_host_id") and d.get("ssh_host_id") == m.get("ssh_host_id"))), {})

    def inventory_guest(self, vmid: Any) -> Dict[str, Any]:
        v = _s(vmid).split(":")[-1]
        return next((g for g in self.inventory.get("guests") or [] if _s(g.get("vmid")) == v), {})


# ── planes for a machine-like row ────────────────────────────────────────────

def machine_planes(src: Sources, m: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
    planes: Dict[str, Dict[str, str]] = {}
    ssh = src.ssh_for(m)
    planes["ssh"] = (_plane("yes", f"{ssh.get('user') or 'root'}@{ssh.get('host')} ({ssh.get('auth') or '?'})",
                            entity_ref("host", ssh.get("id")))
                     if ssh else _plane("no", "no SSH login in the store"))
    if ssh:
        planes["ssh"].update(auth=ssh.get("auth") or "", host_id=ssh.get("id") or "", user=ssh.get("user") or "")
    ident = src.identity_for(m)
    planes["directory"] = (_plane("yes", ident.get("fqdn", ""), entity_ref("identity", ident.get("fqdn")))
                           if ident else _plane("no", "not in FreeIPA"))
    if ident:
        planes["directory"]["fqdn"] = ident.get("fqdn", "")
    mesh = src.mesh_for(m)
    if mesh:
        state = "yes" if mesh.get("connected") or mesh.get("state") == "up" else "unknown"
        planes["mesh"] = _plane(state, f"{mesh.get('ip') or ''} {('connected' if mesh.get('connected') else mesh.get('state') or '')}".strip(),
                                entity_ref("mesh", mesh.get("host_id")))
        planes["mesh"].update(ip=mesh.get("ip") or "", connected=bool(mesh.get("connected")), host_id=mesh.get("host_id") or "")
    else:
        planes["mesh"] = _plane("no", "not on the door")
    certs = src.certs_for(m)
    live = [c for c in certs if c.get("state") not in ("superseded", "revoked")]
    if live:
        worst = min(live, key=lambda c: (c.get("days_left") if c.get("days_left") is not None else 10 ** 6))
        state = "yes" if worst.get("state") in ("ok", "renew soon") else ("unknown" if worst.get("state") in ("unreachable", "recorded") else "no")
        planes["certificate"] = _plane(state, f"{worst.get('name')} · {worst.get('state')}"
                                       + (f" · {worst.get('days_left')} d" if worst.get("days_left") is not None else ""),
                                       entity_ref("cert", worst.get("name")))
    else:
        planes["certificate"] = _plane("no", "no certificate seen")
    if m.get("kind") == "guest" and m.get("vmid") is not None:
        b = src.backup_for(m.get("vmid"))
        if not b:
            planes["backup"] = _plane("unknown", "not in the backup view")
        elif b.get("covered_by"):
            planes["backup"] = _plane("yes" if b.get("backups") else "no",
                                      (f"{b.get('backups')} copies, latest {_age(b.get('latest_at'), src.now)}"
                                       if b.get("backups") else "covered but never backed up")
                                      + f" · {', '.join(b.get('covered_by') or [])}",
                                      entity_ref("backup-job", (b.get("covered_by") or [""])[0]))
        elif b.get("excluded_by"):
            planes["backup"] = _plane("no", "excluded by " + ", ".join(b.get("excluded_by") or []),
                                      entity_ref("backup-job", (b.get("excluded_by") or [""])[0]))
        else:
            planes["backup"] = _plane("no", "no job covers it")
    else:
        planes["backup"] = _plane("n/a", "not a Proxmox guest")
    return planes


def _machine_record(src: Sources, kind: str, ident: str, m: Mapping[str, Any]) -> Dict[str, Any]:
    facts = [_fact("State", m.get("status") or "—"), _fact("Address", m.get("addr") or "—")]
    if m.get("kind") == "guest":
        facts.append(_fact("Guest", f"{'VM' if m.get('type') == 'qemu' else 'CT'} {m.get('vmid')} on {m.get('node')}"))
    if m.get("ips") and len(m.get("ips") or []) > 1:
        facts.append(_fact("All addresses", ", ".join(m.get("ips") or [])))
    if m.get("hardware"):
        facts.append(_fact("Hardware", ", ".join(m.get("hardware") or [])))
    if m.get("runs"):
        facts.append(_fact("Runs", ", ".join(m.get("runs") or [])))
    if m.get("note"):
        facts.append(_fact("Note", m.get("note")))
    inv = src.inventory_guest(m.get("vmid")) if m.get("kind") == "guest" else {}
    related: List[Dict[str, str]] = []
    for d in inv.get("datasets") or []:
        related.append({"ref": entity_ref("dataset", d.get("name")), "label": d.get("name", ""), "noun": "dataset",
                        "detail": _fmt_bytes(d.get("used")) + (f" of {_fmt_bytes(d.get('quota'))}" if d.get("quota") else "")})
    dh = src.docker_host_for(m)
    if dh:
        related.append({"ref": entity_ref("docker-host", dh.get("id")), "label": dh.get("label") or "Docker", "noun": "Docker host"})
    for i in src.integrations_for(m):
        related.append({"ref": entity_ref("integration", i.get("id")), "label": i.get("label") or i.get("id", ""),
                        "noun": "service", "detail": i.get("base_url", "")})
    for c in src.certs_for(m):
        if c.get("state") not in ("superseded",):
            related.append({"ref": entity_ref("cert", c.get("name")), "label": c.get("name", ""), "noun": "certificate",
                            "detail": c.get("state", "")})
    links = [_link("Open in Machines", entity_ref(kind, ident))]
    if m.get("kind") == "guest":
        links.append({"label": "Storage for this guest", "pane": "storage", "sub": "", "ref": entity_ref("guest", ident)})
        links.append({"label": "Backups", "pane": "storage", "sub": "estate"})
    title = m.get("label") or ident
    sub = ("VM " if m.get("type") == "qemu" else "CT " if m.get("kind") == "guest" else "") \
        + (f"{m.get('vmid')} · " if m.get("kind") == "guest" else "") + (m.get("status") or "")
    return {"found": True, "title": title, "subtitle": sub.strip(" ·"), "facts": facts,
            "planes": machine_planes(src, m), "related": related, "links": links}


def _fmt_bytes(n: Any) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── the other kinds ──────────────────────────────────────────────────────────

def _pool_record(src: Sources, name: str) -> Dict[str, Any]:
    pool = next((p for p in src.inventory.get("pools") or [] if p.get("name") == name), None)
    if not pool:
        return {"found": False}
    datasets = [d for d in src.inventory.get("datasets") or [] if _s(d.get("name")) == name or _s(d.get("name")).startswith(name + "/")]
    guests = [g for g in src.inventory.get("guests") or []
              if any(_s(d.get("name")).startswith(name + "/") for d in g.get("datasets") or [])]
    storages = [s for s in src.inventory.get("storages") or [] if _s(s.get("pool")) == name]
    used = pool.get("alloc") or 0
    facts = [_fact("Health", pool.get("health") or "?"),
             _fact("Used", f"{_fmt_bytes(used)} of {_fmt_bytes(pool.get('size'))}"
                   + (f" ({int(100 * used / pool['size'])}%)" if pool.get("size") else "")),
             _fact("Free", _fmt_bytes(pool.get("free"))),
             _fact("Datasets", len(datasets)), _fact("Guests on it", len(guests)),
             _fact("Proxmox storages", ", ".join(_s(s.get("storage")) for s in storages) or "none")]
    related = [{"ref": entity_ref("guest", g.get("vmid")), "label": g.get("name", ""), "noun": "machine",
                "detail": _fmt_bytes(sum((d.get("used") or 0) for d in g.get("datasets") or [] if _s(d.get("name")).startswith(name + "/")))}
               for g in guests]
    related += [{"ref": entity_ref("dataset", d.get("name")), "label": d.get("name", ""), "noun": "dataset",
                 "detail": _fmt_bytes(d.get("used"))} for d in sorted(datasets, key=lambda d: -(d.get("used") or 0))[:12]
                if _s(d.get("name")) != name]
    return {"found": True, "title": name, "subtitle": f"ZFS pool · {pool.get('health') or '?'}", "facts": facts,
            "planes": {}, "related": related, "links": [_link("Open in Storage", entity_ref("pool", name))]}


def _dataset_record(src: Sources, name: str) -> Dict[str, Any]:
    d = next((x for x in src.inventory.get("datasets") or [] if _s(x.get("name")) == name), None)
    if not d:
        return {"found": False}
    pool = name.split("/", 1)[0]
    owner = next((g for g in src.inventory.get("guests") or []
                  if any(_s(x.get("name")) == name for x in g.get("datasets") or [])), None)
    facts = [_fact("Used", _fmt_bytes(d.get("used"))), _fact("Available", _fmt_bytes(d.get("avail"))),
             _fact("Quota", _fmt_bytes(d.get("quota")) if d.get("quota") else "none"),
             _fact("Mounted at", d.get("mountpoint") or "—"), _fact("Type", d.get("type") or "filesystem")]
    related = [{"ref": entity_ref("pool", pool), "label": pool, "noun": "pool"}]
    if owner:
        related.append({"ref": entity_ref("guest", owner.get("vmid")), "label": owner.get("name", ""), "noun": "machine"})
    return {"found": True, "title": name, "subtitle": "ZFS dataset" + (f" · {owner.get('name')}" if owner else ""),
            "facts": facts, "planes": {}, "related": related, "links": [_link("Open in Storage", entity_ref("dataset", name))]}


def _integration_record(src: Sources, ident: str) -> Dict[str, Any]:
    i = next((x for x in src.integrations if x.get("id") == ident), None)
    if not i:
        return {"found": False}
    host = _host_of_url(i.get("base_url", ""))
    m = src.machine_by_addr(host)
    acc = i.get("access") or {}
    facts = [_fact("Kind", i.get("kind") or "generic"), _fact("Address", i.get("base_url") or "—"),
             _fact("Access", ", ".join(k for k, v in acc.items() if v) or "none"),
             _fact("Sensitive", "yes" if i.get("sensitive") else "no")]
    related = []
    if m:
        related.append({"ref": entity_ref("guest" if m.get("kind") == "guest" else "host", m.get("vmid") if m.get("kind") == "guest" else m.get("ssh_host_id") or m.get("id")),
                        "label": m.get("label", ""), "noun": "machine", "detail": m.get("status", "")})
    for c in src.certs:
        if _lower(c.get("where")).split(":")[0] == host and c.get("source") == "service":
            related.append({"ref": entity_ref("cert", c.get("name")), "label": c.get("name", ""), "noun": "certificate", "detail": c.get("state", "")})
    return {"found": True, "title": i.get("label") or ident, "subtitle": i.get("kind") or "service", "facts": facts,
            "planes": {}, "related": related, "links": [_link("Open in Integrations", entity_ref("integration", ident))]}


def _cert_record(src: Sources, name: str) -> Dict[str, Any]:
    rows = [c for c in src.certs if _lower(c.get("name")) == _lower(name)]
    if not rows:
        return {"found": False}
    cur = next((c for c in rows if c.get("state") not in ("superseded",)), rows[0])
    facts = [_fact("State", cur.get("state")), _fact("Issuer", cur.get("issuer_kind") or cur.get("issuer") or "—"),
             _fact("Seen at", cur.get("where") or "—"),
             _fact("Days left", cur.get("days_left") if cur.get("days_left") is not None else "—"),
             _fact("Covers", ", ".join(cur.get("names") or []) or "—")]
    if len(rows) > 1:
        facts.append(_fact("Older copies", len(rows) - 1))
    m = src.machine_by_addr(_lower(cur.get("where")).split(":")[0]) or src.machine_by_name(name)
    related = []
    if m:
        related.append({"ref": entity_ref("guest" if m.get("kind") == "guest" else "host", m.get("vmid") if m.get("kind") == "guest" else m.get("ssh_host_id") or m.get("id")),
                        "label": m.get("label", ""), "noun": "machine"})
    return {"found": True, "title": name, "subtitle": f"certificate · {cur.get('source')}", "facts": facts,
            "planes": {}, "related": related, "links": [_link("Open in Certificates", entity_ref("cert", name))]}


def _backup_job_record(src: Sources, ident: str) -> Dict[str, Any]:
    job = next((s for s in src.backups.get("schedules") or [] if s.get("id") == ident), None)
    if not job:
        return {"found": False}
    covered = [g for g in src.backups.get("guests") or [] if ident in (g.get("covered_by") or [])]
    excluded = [g for g in src.backups.get("guests") or [] if ident in (g.get("excluded_by") or [])]
    facts = [_fact("Does", job.get("what") or "—"), _fact("Schedule", job.get("schedule") or "—"),
             _fact("Target", job.get("storage") or "—"), _fact("Enabled", "yes" if job.get("enabled") else "no"),
             _fact("Next run", _age(job.get("next_run"), src.now).replace(" ago", "") if job.get("next_run") else "—"),
             _fact("Last run", _age(job.get("last_run"), src.now) if job.get("last_run") else "never"),
             _fact("Guests covered", len(covered)), _fact("Guests excluded", len(excluded))]
    related = [{"ref": entity_ref("guest", g.get("vmid")), "label": g.get("name", ""), "noun": "machine",
                "detail": (f"{g.get('backups')} copies" if g.get("backups") else "no copy yet")} for g in covered[:40]]
    return {"found": True, "title": ident, "subtitle": f"backup job · {job.get('owner') or ''}", "facts": facts,
            "planes": {}, "related": related, "links": [{"label": "Open in Backups", "pane": "storage", "sub": "estate"}]}


def _mesh_record(src: Sources, hid: str) -> Dict[str, Any]:
    x = next((y for y in src.mesh if y.get("host_id") == hid), None)
    if not x:
        return {"found": False}
    m = src.machine_by_host_id(hid) or src.machine_by_addr(x.get("host", ""))
    facts = [_fact("Overlay address", x.get("ip") or "—"), _fact("State", x.get("state") or "—"),
             _fact("Connected", "yes" if x.get("connected") else "no"), _fact("Device at the door", x.get("door_device") or "—")]
    related = [{"ref": entity_ref("guest" if m.get("kind") == "guest" else "host", m.get("vmid") if m.get("kind") == "guest" else m.get("ssh_host_id") or m.get("id")),
                "label": m.get("label", ""), "noun": "machine"}] if m else []
    return {"found": True, "title": x.get("label") or hid, "subtitle": "mesh member", "facts": facts,
            "planes": {}, "related": related, "links": [_link("Open in Trust › mesh", entity_ref("mesh", hid))]}


def _identity_record(src: Sources, fqdn: str) -> Dict[str, Any]:
    h = next((y for y in src.identity if _lower(y.get("fqdn")) == _lower(fqdn)), None)
    if not h:
        return {"found": False}
    m = src.machine_by_name(fqdn)
    facts = [_fact("Enrolled", "yes" if h.get("enrolled") else "no")]
    related = [{"ref": entity_ref("guest" if m.get("kind") == "guest" else "host", m.get("vmid") if m.get("kind") == "guest" else m.get("ssh_host_id") or m.get("id")),
                "label": m.get("label", ""), "noun": "machine", "detail": m.get("status", "")}] if m else []
    if not m:
        facts.append(_fact("Machine", "no machine in the estate answers to this name - a stale entry?"))
    return {"found": True, "title": fqdn, "subtitle": "directory host (FreeIPA)", "facts": facts,
            "planes": {}, "related": related, "links": [_link("Open in Trust › Identity", entity_ref("identity", fqdn))]}


def _container_name(c: Mapping[str, Any]) -> str:
    names = c.get("Names") or []
    return str(names[0] if names else (c.get("Name") or c.get("Id") or "")).lstrip("/")


def _container_record(src: Sources, ident: str) -> Dict[str, Any]:
    """ident is host/name (host is the Docker host id, 'local' for Vera's own)."""
    host, _, name = ident.partition("/")
    if not name:
        host, name = "local", host
    c = next((x for x in src.containers if _container_name(x) == name or str(x.get("Id", "")).startswith(name)), None)
    if not c:
        return {"found": False}
    labels = c.get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    ports = sorted({f"{p.get('PublicPort')}->{p.get('PrivatePort')}/{p.get('Type')}" for p in (c.get("Ports") or []) if p.get("PublicPort")})
    restart = ((c.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or ""
    facts = [_fact("State", c.get("State") or "—"), _fact("Status", c.get("Status") or "—"),
             _fact("Image", c.get("Image") or "—"), _fact("Compose project", project or "not managed by compose"),
             _fact("Restart policy", restart or "none"), _fact("Ports", ", ".join(ports) or "none published")]
    mounts = [m.get("Source") or m.get("Name") for m in (c.get("Mounts") or []) if m.get("Source") or m.get("Name")]
    if mounts:
        facts.append(_fact("Mounts", ", ".join(str(m) for m in mounts[:6])))
    dh = next((d for d in src.docker_hosts if d.get("id") == host), None)
    related = []
    if dh:
        related.append({"ref": entity_ref("docker-host", host), "label": dh.get("label") or host, "noun": "Docker host"})
    m = src.machine_by_host_id((dh or {}).get("ssh_host_id") or "") if dh else {}
    if m:
        related.append({"ref": entity_ref("guest" if m.get("kind") == "guest" else "host", m.get("vmid") if m.get("kind") == "guest" else m.get("ssh_host_id") or m.get("id")),
                        "label": m.get("label", ""), "noun": "machine"})
    return {"found": True, "title": name, "subtitle": f"container · {c.get('State') or ''}".strip(" ·"), "facts": facts,
            "planes": {}, "related": related, "links": [_link("Open in Docker", entity_ref("container", ident))]}


def _docker_host_record(src: Sources, ident: str) -> Dict[str, Any]:
    d = next((y for y in src.docker_hosts if y.get("id") == ident), None)
    if not d:
        return {"found": False}
    m = src.machine_by_host_id(d.get("ssh_host_id") or "") or src.machine_by_addr(_host_of_url(d.get("url", "")))
    facts = [_fact("Kind", d.get("kind") or "—"), _fact("Reached at", d.get("url") or d.get("socket") or "—"),
             _fact("Default", "yes" if d.get("default") else "no")]
    related = [{"ref": entity_ref("guest" if m.get("kind") == "guest" else "host", m.get("vmid") if m.get("kind") == "guest" else m.get("ssh_host_id") or m.get("id")),
                "label": m.get("label", ""), "noun": "machine"}] if m else []
    return {"found": True, "title": d.get("label") or ident, "subtitle": "Docker host", "facts": facts,
            "planes": {}, "related": related, "links": [_link("Open in Docker", entity_ref("docker-host", ident))]}


# ── entry point ──────────────────────────────────────────────────────────────

def resolve(ref: Any, src: Sources) -> Dict[str, Any]:
    ent = parse_entity(ref)
    base = {"ref": _s(ref), "kind": ent.get("kind", ""), "id": ent.get("id", ""),
            "noun": ENTITY_KINDS.get(ent.get("kind", ""), {}).get("noun", ""),
            "errors": dict(src.errors)}
    if not ent:
        return {**base, "found": False, "error": "not an entity reference (want <kind>:<id>)"}
    kind, ident = ent["kind"], ent["id"]
    if kind == "guest":
        m = src.machine_by_vmid(ident)
        rec = _machine_record(src, kind, ident, m) if m else {"found": False}
    elif kind == "host":
        m = src.machine_by_host_id(ident)
        if not m:
            h = next((x for x in src.ssh_hosts if x.get("id") == ident), None)
            m = {"id": ident, "label": h.get("label"), "kind": "host", "addr": h.get("host"), "ips": [h.get("host")],
                 "ssh_host_id": ident, "status": ""} if h else {}
        rec = _machine_record(src, kind, ident, m) if m else {"found": False}
    elif kind == "pool":
        rec = _pool_record(src, ident.split("/")[-1])
    elif kind == "dataset":
        rec = _dataset_record(src, ident)
    elif kind == "integration":
        rec = _integration_record(src, ident)
    elif kind == "cert":
        rec = _cert_record(src, ident)
    elif kind == "backup-job":
        rec = _backup_job_record(src, ident)
    elif kind == "mesh":
        rec = _mesh_record(src, ident)
    elif kind == "identity":
        rec = _identity_record(src, ident)
    elif kind == "docker-host":
        rec = _docker_host_record(src, ident)
    elif kind == "container":
        rec = _container_record(src, ident)
    else:
        rec = {"found": False, "error": f"{kind} is known but has no reader joined yet"}
    out = {**base, **rec}
    if not out.get("found") and not out.get("error"):
        out["error"] = f"no {base['noun'] or kind} matches {ident!r}"
        if src.errors:
            out["error"] += " (some readers did not answer: " + ", ".join(src.errors) + ")"
    return out
