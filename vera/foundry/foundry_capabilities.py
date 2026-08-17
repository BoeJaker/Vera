"""
foundry_capabilities.py — Vera OS Provisioning ("Foundry")
==========================================================
Phase 1: a versioned, multi-type **image catalog** + a **feature-bundle**
registry + a **provision orchestrator** that stands up an OS onto a target
(Proxmox CT, Proxmox VM, or Docker) with SSH-cert + FreeIPA + mesh and the
selected hardening / file-server / compute features baked in.

Model — a job is  Target × Base-image × Features[].  The orchestrator composes
existing Vera plumbing (proxmox.lxc.create, docker.run, enroll.guest / lxc.create
auto_enroll, proxmox.guest.exec) rather than reimplementing it.

Catalog (`vera:foundry:images`): one entry per (os, version, type) — types are
  cloudimg | lxc-template | docker | iso | ipxe
so VMs/physical use cloud images or ISOs, CTs use LXC templates, and Docker uses
registry images, all from one index. Big blobs live in Proxmox storage / Garage;
this is the index + (later) the import/serve logic.

See OS-PROVISIONING-ROADMAP.md for the full design + phasing.
Capabilities (group `foundry.*`):
  foundry.catalog.seed   — idempotent seed of the default OS set
  foundry.image.list     — list catalogued images (filter by os/type)
  foundry.image.add      — add/replace a catalogue entry
  foundry.image.delete   — remove an entry
  foundry.image.import   — build a cloud-init template from a cloudimg (VMs)
  foundry.image.import.status — poll a template build
  foundry.features       — the composable feature bundles + target compatibility
  foundry.provision      — stand up target × image × features[]
  foundry.jobs           — recent provision jobs
  foundry.blueprint.*    — versioned, reusable IaC manifests: save/list/get/apply/
                           delete/export(YAML)/import — define a whole estate once,
                           version it, re-apply it, commit it to git/Gitea
  foundry.pxe.*          — PXE/physical + ISO: config, boot profiles (image × the
                           SAME feature bundles), MAC waiting-room, server deploy —
                           so bare metal is provisioned from the same catalogue+features
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, register_ui, CAPABILITY_REGISTRY,
)

# Pure netboot render + hardening logic lives in an app-free core module so it's
# unit-testable without booting the orchestrator (see foundry_core.py header).
from Vera.vera.foundry.foundry_core import (
    _HARDEN, _pxe_slug, _render_features_script, _render_rpi_config,
    _render_rpi_cmdline, _render_ipxe, _render_autoinstall, _render_boot,
    pick_node, cluster_join_script, CLUSTER_KINDS,
    cluster_init_script, parse_init_token,
    pxe_dnsmasq_conf, pxe_ipxe_menu, swarm_service_cmd,
    pxe_ops_apkovl_files, pxe_desktop_apkovl_files,
)
from Vera.vera.security import secrets as vsecrets

_HERE = Path(__file__).parent
K_IMAGES = "vera:foundry:images"
K_JOBS = "vera:foundry:jobs"
K_CLUSTERS = "vera:foundry:clusters"   # registered clusters a host can be provisioned to join


def _redis():
    return getattr(_orch, "REDIS", None)


async def _call(_cap_name: str, **kw) -> Dict:
    """Invoke another capability by name (raw function). The first arg is
    underscore-prefixed so target caps that take a `name` kwarg don't collide."""
    c = CAPABILITY_REGISTRY.get(_cap_name)
    fn = (c.get("raw") or c.get("func")) if c else None
    if not fn:
        return {"error": f"capability '{_cap_name}' unavailable"}
    try:
        return await fn(**kw)
    except Exception as e:
        return {"error": f"{_cap_name}: {type(e).__name__}: {e}"}


async def _resolve_storage(cluster_id: str) -> str:
    """Pick an active storage on the node that can hold guest disks — so Foundry
    isn't hardcoded to a storage name that may not exist (e.g. local-lvm vs
    local-zfs). Prefers block pools (zfspool/lvmthin/lvm), else dir/nfs/cifs."""
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command="pvesm status -content images 2>/dev/null")
    if res.get("error"):
        return ""
    best = ""
    for line in (res.get("stdout", "") or "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 3 and p[2] == "active":
            if p[1] in ("zfspool", "lvmthin", "lvm"):
                return p[0]
            if not best and p[1] in ("dir", "nfs", "cifs"):
                best = p[0]
    return best


async def _resolve_node(cluster_id: str) -> str:
    """Resolve a node name when the caller gave none — clone/create need it, and an
    empty node builds the Proxmox path /nodes//… → HTTP 501 (real-VM E2E finding)."""
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command="pvesh get /nodes --output-format json 2>/dev/null")
    if res.get("error"):
        return ""
    return pick_node(res.get("stdout", "") or "")


def _vera_pubkey() -> str:
    """Vera's SSH public key — baked into VMs via cloud-init so Vera can enrol them."""
    try:
        p = Path.home() / ".vera" / "ssh" / "id_vera.pub"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""


def _netcfg(ip: str, gateway: str):
    """(lxc_net0, vm_ipconfig) for a static IP — the LAN has no DHCP, so guests
    need a static address to be reachable/enrollable. Empty ip → DHCP."""
    if not ip:
        return "", "ip=dhcp"
    cidr = ip if "/" in ip else ip + "/24"
    gw = gateway or "192.168.0.1"
    return f"name=eth0,bridge=vmbr0,ip={cidr},gw={gw}", f"ip={cidr},gw={gw}"


# ─────────────────────────────────────────────────────────────────────────────
# Seed catalogue — the default OS set (indexed, not yet downloaded). Import
# fetches + checksums the blob into Proxmox storage / Garage on demand.
# ─────────────────────────────────────────────────────────────────────────────
SEED: List[Dict[str, Any]] = [
    # Debian 12
    {"os": "debian", "version": "12", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"},
    {"os": "debian", "version": "12", "type": "lxc-template", "arch": "amd64",
     "source_url": "debian-12-standard"},   # pveam appliance name
    {"os": "debian", "version": "12", "type": "docker", "arch": "amd64", "source_url": "debian:12"},
    # Ubuntu 24.04
    {"os": "ubuntu", "version": "24.04", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"},
    {"os": "ubuntu", "version": "24.04", "type": "lxc-template", "arch": "amd64",
     "source_url": "ubuntu-24.04-standard"},
    {"os": "ubuntu", "version": "24.04", "type": "docker", "arch": "amd64", "source_url": "ubuntu:24.04"},
    # AlmaLinux 9
    {"os": "almalinux", "version": "9", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2"},
    {"os": "almalinux", "version": "9", "type": "lxc-template", "arch": "amd64",
     "source_url": "almalinux-9-default"},
    {"os": "almalinux", "version": "9", "type": "docker", "arch": "amd64", "source_url": "almalinux:9"},
    # Alpine
    {"os": "alpine", "version": "3.20", "type": "lxc-template", "arch": "amd64",
     "source_url": "alpine-3.20-default"},
    {"os": "alpine", "version": "3.20", "type": "docker", "arch": "amd64", "source_url": "alpine:3.20"},
    # Arch
    {"os": "arch", "version": "latest", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2"},
    {"os": "arch", "version": "latest", "type": "docker", "arch": "amd64", "source_url": "archlinux:latest"},
    # Kali (security testing)
    {"os": "kali", "version": "rolling", "type": "docker", "arch": "amd64",
     "source_url": "kalilinux/kali-rolling"},
    {"os": "kali", "version": "rolling", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://kali.download/cloud-images/current/kali-linux-current-cloud-genericcloud-amd64.tar.xz",
     "notes": "Kali cloud image (verify current path at import time)"},
    # Windows — stub (bring-your-own ISO)
    {"os": "windows", "version": "server-2022", "type": "iso", "arch": "amd64",
     "source_url": "", "notes": "STUB — supply a Windows Server 2022 ISO volid; autounattend.xml support is a later phase"},
]


def _img_id(e: Dict) -> str:
    return f"{e['os']}-{e['version']}-{e['type']}"


@capability(
    "foundry.catalog.seed",
    http_method="POST", http_path="/foundry/catalog/seed", http_tags=["foundry"],
    memory="on",
    description="Seed the image catalogue with the default OS set (Debian 12, "
                "Ubuntu 24.04, AlmaLinux 9, Alpine, Arch, Kali, Windows-stub) "
                "across cloudimg / lxc-template / docker / iso types. Idempotent — "
                "only adds entries that are missing. Output: {ok, added, total}.",
)
async def cap_seed(overwrite: bool = False, trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"error": "no redis"}
    existing = await r.hgetall(K_IMAGES) or {}
    existing = {(k.decode() if isinstance(k, bytes) else k) for k in existing}
    added = 0
    for e in SEED:
        iid = _img_id(e)
        if iid in existing and not overwrite:
            continue
        rec = {"id": iid, "arch": "amd64", "sha256": "", "size": 0,
               "location": "", "status": "indexed", "notes": "",
               "added": time.time(), "is_latest": True, **e}
        await r.hset(K_IMAGES, iid, json.dumps(rec))
        added += 1
    total = len(await r.hgetall(K_IMAGES) or {})
    await emit_event({"type": "foundry.catalog.seeded", "added": added, "total": total})
    return {"ok": True, "added": added, "total": total}


@capability(
    "foundry.image.list",
    http_method="GET", http_path="/foundry/image/list", http_tags=["foundry"],
    memory="off", silent=True,
    description="List catalogued OS images. Optional filters: os, type "
                "(cloudimg|lxc-template|docker|iso|ipxe). Output: {images:[...]}.",
)
async def cap_image_list(os: str = "", type: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"images": []}
    rows = await r.hgetall(K_IMAGES) or {}
    out = []
    for v in rows.values():
        try:
            rec = json.loads(v)
        except Exception:
            continue
        if os and rec.get("os") != os:
            continue
        if type and rec.get("type") != type:
            continue
        out.append(rec)
    out.sort(key=lambda e: (e.get("os", ""), e.get("version", ""), e.get("type", "")))
    return {"images": out, "count": len(out)}


@capability(
    "foundry.image.add",
    http_method="POST", http_path="/foundry/image/add", http_tags=["foundry"],
    memory="on",
    description="Add or replace a catalogue entry. Inputs: os (str!), version "
                "(str!), type (cloudimg|lxc-template|docker|iso|ipxe), arch "
                "(str='amd64'), source_url (str — URL, pveam name, docker ref or "
                "iso volid), sha256 (str), notes (str). Output: {ok, id}.",
)
async def cap_image_add(os: str = "", version: str = "", type: str = "",
                        arch: str = "amd64", source_url: str = "",
                        sha256: str = "", notes: str = "", template_vmid: int = 0,
                        trace_id=None) -> Dict:
    if not (os and version and type):
        return {"error": "os, version and type are required"}
    r = _redis()
    if not r:
        return {"error": "no redis"}
    e = {"os": os, "version": version, "type": type}
    iid = _img_id(e)
    rec = {"id": iid, "os": os, "version": version, "type": type, "arch": arch,
           "source_url": source_url, "sha256": sha256, "size": 0, "location": "",
           "status": "indexed", "notes": notes, "added": time.time(), "is_latest": True}
    if template_vmid:
        rec["template_vmid"] = int(template_vmid)   # link a cloud-init template (VMs)
    await r.hset(K_IMAGES, iid, json.dumps(rec))
    await emit_event({"type": "foundry.image.added", "id": iid})
    return {"ok": True, "id": iid}


@capability(
    "foundry.image.delete",
    http_method="POST", http_path="/foundry/image/delete", http_tags=["foundry"],
    memory="on", description="Remove a catalogue entry by id. Input: id (str!).",
)
async def cap_image_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    n = await r.hdel(K_IMAGES, id)
    return {"ok": bool(n), "id": id}


async def _vztmpl_storage(cluster_id: str, storage: str) -> str:
    """A storage on the node that holds LXC templates (vztmpl content)."""
    if storage and storage not in ("local-lvm",):
        return storage
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command="pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3==\"active\"{print $1; exit}'")
    return ((res.get("stdout", "") or "").strip()) or "local"


async def _import_lxc_template(image_id, img, cluster_id, storage) -> Dict:
    """Download an LXC appliance template via pveam (background) and, when done,
    the status cap rewrites the catalogue entry's source_url to the real volid."""
    r = _redis()
    appliance = img.get("source_url", "")
    if not appliance or ":" in appliance:
        return {"error": "entry has no pveam appliance name to download"}
    tstore = await _vztmpl_storage(cluster_id, storage)
    log = f"/var/log/foundry-tmpl-{image_id}.log"
    script = (
        "set -e\n"
        f"exec >{log} 2>&1\n"
        "pveam update >/dev/null 2>&1 || true\n"
        f'FULL=$(pveam available 2>/dev/null | awk \'{{print $2}}\' | grep -E "^{appliance}(_|$)" | sort -V | tail -1)\n'
        f'[ -z "$FULL" ] && {{ echo NO_APPLIANCE:{appliance}; exit 1; }}\n'
        f'echo "[foundry] pveam download {tstore} $FULL"; pveam download {tstore} "$FULL"\n'
        f'echo "VERA_TEMPLATE_VOLID:{tstore}:vztmpl/$FULL"\n'
    )
    b64 = base64.b64encode(script.encode()).decode()
    launch = f"nohup bash -c 'echo {b64} | base64 -d | bash' >/dev/null 2>&1 & echo STARTED"
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=launch, timeout=40)
    if res.get("error"):
        return {"error": res["error"]}
    img.update({"status": "importing", "import_log": log, "import_storage": tstore})
    await r.hset(K_IMAGES, image_id, json.dumps(img))
    await emit_event({"type": "foundry.image.import.started", "image": image_id, "kind": "lxc-template"})
    return {"ok": True, "image_id": image_id, "started": True, "log": log,
            "note": "downloading LXC template in the background — poll foundry.image.import.status"}


@capability(
    "foundry.image.import",
    http_method="POST", http_path="/foundry/image/import", http_tags=["foundry"],
    memory="on",
    description="Import a catalogue CLOUD IMAGE onto Proxmox and build a cloud-init "
                "TEMPLATE from it (runs on the node in the background), so VM "
                "provisioning can clone it. Inputs: image_id (str! — a cloudimg entry), "
                "cluster_id (str!), node (str!), storage (str='local-lvm' — where the "
                "VM disk lands, e.g. local-zfs), template_vmid (int — blank=auto). "
                "Output: {ok, image_id, template_vmid, started, log}.",
)
async def cap_image_import(image_id: str = "", cluster_id: str = "", node: str = "",
                           storage: str = "local-lvm", template_vmid: int = 0,
                           trace_id=None) -> Dict:
    r = _redis()
    raw = await r.hget(K_IMAGES, image_id) if (r and image_id) else None
    if not raw:
        return {"error": f"image '{image_id}' not in catalogue"}
    img = json.loads(raw)
    if img.get("type") == "lxc-template":
        return await _import_lxc_template(image_id, img, cluster_id, storage)
    if img.get("type") != "cloudimg":
        return {"error": f"import handles cloudimg (VM template) or lxc-template (CT); "
                         f"'{image_id}' is {img.get('type')}"}
    url = img.get("source_url", "")
    if not url.startswith("http"):
        return {"error": "image has no downloadable http source_url"}
    if not template_vmid:
        nid = await _call("proxmox.nextid", cluster_id=cluster_id)
        template_vmid = int(nid.get("vmid") or 0)
    if not template_vmid:
        return {"error": "could not allocate a template vmid"}
    base = url.split("/")[-1] or f"{image_id}.img"
    tname = f"{img['os']}-{img['version']}-tmpl".replace(".", "-").replace("_", "-")
    log = f"/var/log/foundry-import-{template_vmid}.log"
    script = (
        "set -e\n"
        f"exec >{log} 2>&1\n"
        f"VMID={template_vmid}; STORAGE='{storage}'; URL='{url}'\n"
        "WORK=/var/lib/vz/template/foundry; mkdir -p $WORK\n"
        f'IMG="$WORK/{base}"\n'
        'echo "[foundry] download $URL"; [ -s "$IMG" ] || wget -qO "$IMG" "$URL"\n'
        f"echo '[foundry] create VM'; qm create $VMID --name {tname} --memory 2048 "
        "--cores 2 --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci --ostype l26\n"
        'echo "[foundry] importdisk"; qm importdisk $VMID "$IMG" $STORAGE\n'
        "DISK=$(qm config $VMID | sed -n 's/^unused0: //p')\n"
        '[ -n "$DISK" ] || DISK="$STORAGE:vm-$VMID-disk-0"\n'
        'qm set $VMID --scsi0 "$DISK"\n'
        "qm set $VMID --ide2 $STORAGE:cloudinit\n"
        "qm set $VMID --boot c --bootdisk scsi0 --serial0 socket --vga serial0 --agent enabled=1\n"
        "qm template $VMID\n"
        "echo VERA_TEMPLATE_OK:$VMID\n"
    )
    b64 = base64.b64encode(script.encode()).decode()
    launch = f"nohup bash -c 'echo {b64} | base64 -d | bash' >/dev/null 2>&1 & echo STARTED:$!"
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=launch, timeout=40)
    if res.get("error"):
        return {"error": res["error"]}
    img.update({"status": "importing", "template_vmid": template_vmid,
                "import_log": log, "import_storage": storage})
    await r.hset(K_IMAGES, image_id, json.dumps(img))
    await emit_event({"type": "foundry.image.import.started", "image": image_id,
                      "template_vmid": template_vmid})
    return {"ok": True, "image_id": image_id, "template_vmid": template_vmid,
            "started": True, "log": log,
            "note": "building template in the background — poll foundry.image.import.status"}


@capability(
    "foundry.image.import.status",
    http_method="GET", http_path="/foundry/image/import/status", http_tags=["foundry"],
    memory="off", silent=True,
    description="Check a template-build import: reads the node log + confirms the "
                "template exists, and when ready marks the catalogue entry 'ready' so "
                "VM provisioning can clone it. Inputs: image_id (str!), cluster_id "
                "(str!). Output: {status, template_vmid, ready, tail}.",
)
async def cap_image_import_status(image_id: str = "", cluster_id: str = "",
                                  trace_id=None) -> Dict:
    r = _redis()
    raw = await r.hget(K_IMAGES, image_id) if (r and image_id) else None
    if not raw:
        return {"error": "image not in catalogue"}
    img = json.loads(raw)
    log = img.get("import_log", "")
    # LXC template download: parse the resulting volid from the log → source_url
    if img.get("type") == "lxc-template":
        if not log:
            return {"status": img.get("status", "indexed"), "ready": False, "note": "not imported"}
        res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                          command=f"tail -n 6 {log} 2>/dev/null")
        out = res.get("stdout", "") or ""
        volid = ""
        for line in out.splitlines():
            if line.startswith("VERA_TEMPLATE_VOLID:"):
                volid = line.split("VERA_TEMPLATE_VOLID:", 1)[1].strip()
        if volid:
            img["source_url"] = volid
            img["status"] = "ready"
            await r.hset(K_IMAGES, image_id, json.dumps(img))
            await emit_event({"type": "foundry.image.imported", "image": image_id, "volid": volid})
        return {"status": "ready" if volid else img.get("status", "importing"),
                "ready": bool(volid), "volid": volid, "tail": out[-500:]}
    vmid = img.get("template_vmid")
    if not vmid:
        return {"status": img.get("status", "indexed"), "ready": False, "note": "not imported"}
    cmd = (f"tail -n 5 {log} 2>/dev/null; echo '---'; "
           f"qm config {vmid} 2>/dev/null | grep -q '^template:' && echo IS_TEMPLATE || true")
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=cmd, timeout=30)
    out = res.get("stdout", "") or ""
    ready = ("IS_TEMPLATE" in out) or ("VERA_TEMPLATE_OK" in out)
    if ready and img.get("status") != "ready":
        img["status"] = "ready"
        await r.hset(K_IMAGES, image_id, json.dumps(img))
        await emit_event({"type": "foundry.image.imported", "image": image_id,
                          "template_vmid": vmid})
    return {"status": "ready" if ready else img.get("status", "importing"),
            "template_vmid": vmid, "ready": ready, "tail": out[-500:]}


# ─────────────────────────────────────────────────────────────────────────────
# Feature bundles — composable, toggleable. `targets` = which target types the
# feature applies to. Scripts (where applicable) are applied post-create via
# proxmox.guest.exec / SSH. enrol+mesh are delivered by lxc.create auto_enroll /
# enroll.guest today; the rest land as bundle scripts (Gitea-backed) next.
# ─────────────────────────────────────────────────────────────────────────────
FEATURES: List[Dict[str, Any]] = [
    {"id": "enrol", "label": "Enrolment (SSH cert + FreeIPA)", "default": True,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "Passwordless SSH-cert trust + FreeIPA host/user join."},
    {"id": "mesh", "label": "Private mesh network", "default": True,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "Join the WireGuard private overlay."},
    {"id": "hardening", "label": "OS hardening", "default": True,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "sshd policy, host firewall, auto-updates, no root SSH login."},
    {"id": "file-client", "label": "File-server client", "default": False,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "Mount the shared SMB/NFS drives."},
    {"id": "file-server", "label": "File server", "default": False,
     "targets": ["ct", "vm", "physical"], "status": "planned",
     "desc": "Host Samba + NFS shares (bundle script next)."},
    {"id": "security-monitoring", "label": "Security monitoring", "default": False,
     "targets": ["ct", "vm", "physical"], "status": "planned",
     "desc": "auditd + log shipping to a collector (bundle script next)."},
    {"id": "docker-swarm", "label": "Docker Swarm member", "default": False,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "Install Docker + join a registered Swarm. Use cluster:<name> to pick a "
             "specific cluster; bare docker-swarm joins the default docker-swarm cluster "
             "if one is registered (foundry.cluster.register), else installs Docker only."},
    {"id": "distributed-compute", "label": "Distributed compute member", "default": False,
     "targets": ["ct", "vm", "physical", "docker"], "status": "ready",
     "desc": "Join a registered distributed-compute cluster (Docker Swarm / k3s / Nomad / "
             "Ray). Use cluster:<name>, or bare = the default registered cluster."},
    {"id": "cluster", "label": "Join a named cluster", "default": False,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "cluster:<name> — join the named cluster from the Foundry registry "
             "(docker-swarm|k3s|nomad|ray|generic). Register clusters with "
             "foundry.cluster.register."},
]


@capability(
    "foundry.features",
    http_method="GET", http_path="/foundry/features", http_tags=["foundry"],
    memory="off", silent=True,
    description="List the composable feature bundles and which target types each "
                "supports. Output: {features:[{id,label,desc,targets,default,status}]}.",
)
async def cap_features(trace_id=None) -> Dict:
    return {"features": FEATURES}


# ── Cluster registry — clusters a provisioned host can be made to JOIN ───────────
@capability(
    "foundry.cluster.register",
    http_method="POST", http_path="/foundry/cluster/register", http_tags=["foundry"],
    memory="on",
    description="Register a cluster / distributed-compute system a Foundry-provisioned "
                "host can JOIN via the docker-swarm / distributed-compute / cluster:<name> "
                "feature. Inputs: name (str!), kind (docker-swarm|k3s|nomad|ray|generic), "
                "join_addr (str! except generic — manager/server address), token (str — "
                "join token/secret, SEALED at rest), role (str — e.g. k3s server|agent), "
                "port (int — override the default join port), command (str — for "
                "kind=generic). Output: {ok, name, kind}.",
)
async def cap_cluster_register(name: str = "", kind: str = "", join_addr: str = "",
                               token: str = "", role: str = "", port: int = 0,
                               command: str = "", trace_id=None) -> Dict:
    name = (name or "").strip()
    kind = (kind or "").strip().lower()
    if not name:
        return {"error": "name required"}
    if kind not in CLUSTER_KINDS:
        return {"error": f"kind must be one of: {', '.join(CLUSTER_KINDS)}"}
    if kind != "generic" and not (join_addr or "").strip():
        return {"error": "join_addr required (the cluster manager/server address)"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    opts: Dict[str, Any] = {}
    if port:
        opts["port"] = int(port)
    if command:
        opts["command"] = command
    rec = {"name": name, "kind": kind, "join_addr": (join_addr or "").strip(),
           "token": vsecrets.seal(token) if token else "",
           "role": (role or "").strip(), "opts": opts, "updated": time.time()}
    await r.hset(K_CLUSTERS, name, json.dumps(rec))
    await emit_event({"type": "foundry.cluster.registered", "name": name, "kind": kind})
    return {"ok": True, "name": name, "kind": kind}


def _cluster_redacted(rec: Dict) -> Dict:
    out = dict(rec)
    out["token"] = "***" if rec.get("token") else ""
    return out


@capability(
    "foundry.cluster.list",
    http_method="GET", http_path="/foundry/cluster/list", http_tags=["foundry"],
    memory="off", silent=True,
    description="List registered clusters (tokens redacted). Output: {clusters:[...]}.",
)
async def cap_cluster_list(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"clusters": []}
    raw = await r.hgetall(K_CLUSTERS) or {}
    out = []
    for v in raw.values():
        try:
            out.append(_cluster_redacted(json.loads(v)))
        except Exception:
            continue
    out.sort(key=lambda c: c.get("name", ""))
    return {"clusters": out}


@capability(
    "foundry.cluster.delete",
    http_method="POST", http_path="/foundry/cluster/delete", http_tags=["foundry"],
    memory="on",
    description="Remove a registered cluster. Inputs: name (str!). Output: {ok, removed}.",
)
async def cap_cluster_delete(name: str = "", trace_id=None) -> Dict:
    name = (name or "").strip()
    if not name:
        return {"error": "name required"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    n = await r.hdel(K_CLUSTERS, name)
    await emit_event({"type": "foundry.cluster.deleted", "name": name})
    return {"ok": True, "removed": bool(n)}


@capability(
    "foundry.cluster.init",
    http_method="POST", http_path="/foundry/cluster/init", http_tags=["foundry"],
    memory="on",
    description="Bootstrap a NEW cluster on a host and REGISTER it with the join token "
                "captured + sealed — so other hosts can then be provisioned to join it "
                "(feature cluster:<name>). Runs the init on the target over SSH (host + "
                "ssh_user/ssh_key_path, defaults to Vera's key) OR on a Proxmox LXC guest "
                "(cluster_id/node/vmid). Inputs: name (str!), kind (docker-swarm|k3s), "
                "advertise_addr (str — address peers join; defaults to host), host (str — "
                "ssh IP), ssh_user (str=root), ssh_key_path (str), cluster_id (str), node "
                "(str), vmid (int — LXC guest instead of ssh), register (bool=true). "
                "Output: {ok, name, kind, addr, token_captured, registered}.",
)
async def cap_cluster_init(name: str = "", kind: str = "", advertise_addr: str = "",
                           host: str = "", ssh_user: str = "root", ssh_key_path: str = "",
                           cluster_id: str = "", node: str = "", vmid: int = 0,
                           register: bool = True, trace_id=None) -> Dict:
    name = (name or "").strip()
    kind = (kind or "").strip().lower()
    if not name:
        return {"error": "name required"}
    if kind not in ("docker-swarm", "k3s"):
        return {"error": "kind must be docker-swarm or k3s (nomad/ray/generic join an "
                         "existing control plane — use foundry.cluster.register)"}
    adv = (advertise_addr or host or "").strip()
    script = "#!/bin/sh\n" + cluster_init_script(kind, adv)
    out = ""
    if vmid:
        res = await _apply_ct_feature(cluster_id, vmid, "lxc", script, node)
        if res.get("error"):
            return {"error": f"init on guest {vmid} failed: {res.get('error')}"}
        out = res.get("stdout") or res.get("out") or ""
    elif host:
        b = base64.b64encode(script.encode()).decode()
        run = "sudo -n sh" if (ssh_user or "root") != "root" else "sh"
        res = await _call("exec.ssh.run", host=host, user=ssh_user or "root",
                          key_path=ssh_key_path or _vera_key_path(),
                          command=f"echo {b} | base64 -d | {run}", timeout=600)
        out = res.get("stdout", "") or ""
        # the captured token (below) is the real success signal, not the exit code —
        # only bail here if SSH itself never reached the host (no output at all).
        if res.get("error") and not out:
            return {"error": f"init over SSH to {host} failed: {res.get('error')}"}
    else:
        return {"error": "give a target: host (ssh) or cluster_id+vmid (Proxmox LXC)"}
    parsed = parse_init_token(kind, out)
    tok = parsed.get("token", "")
    if not tok:
        return {"error": "init ran but no join token captured — is the docker/k3s daemon "
                         "up on the host?", "output": (out or "")[-800:]}
    addr = adv or parsed.get("addr", "")
    result = {"ok": True, "name": name, "kind": kind, "addr": addr, "token_captured": True}
    if register:
        reg = await cap_cluster_register(name=name, kind=kind, join_addr=addr, token=tok,
                                         role=("worker" if kind == "docker-swarm" else "agent"))
        result["registered"] = bool(reg.get("ok"))
    await emit_event({"type": "foundry.cluster.init", "name": name, "kind": kind})
    return result


@capability(
    "foundry.cluster.run", http_method="POST", http_path="/foundry/cluster/run",
    http_tags=["foundry"], memory="on",
    description="Dispatch COMPUTE to the Docker Swarm (Vera's distributed-compute cluster): "
                "creates a swarm service that runs across the worker nodes (the netbooted ops "
                "nodes). Runs via the swarm manager (a CT/VM) with pct exec, so no direct network "
                "route to the isolated provisioning subnet is needed. Inputs: cluster_id (str), "
                "node (str — auto-resolved), manager_vmid (int=201 — the swarm-manager guest), "
                "name (str), image (str='alpine'), replicas (int=1), command (str — runs in the "
                "container). Output: {ok, service, replicas, output}.",
)
async def cap_cluster_run(cluster_id: str = "", node: str = "", manager_vmid: int = 201,
                          name: str = "vera-job", image: str = "alpine", replicas: int = 1,
                          command: str = "", trace_id=None) -> Dict:
    if not node:
        node = await _resolve_node(cluster_id)
    cmd = swarm_service_cmd(name, image, replicas, command)
    if not cmd:
        return {"error": f"unsafe/empty image reference: {image!r}"}
    res = await _apply_ct_feature(cluster_id, int(manager_vmid), "lxc", cmd, node)
    out = res.get("stdout") or res.get("out") or ""
    if res.get("error"):
        return {"error": f"dispatch failed on manager {manager_vmid}: {res.get('error')}",
                "output": out[-800:]}
    await emit_event({"type": "foundry.cluster.run", "service": name, "image": image,
                      "replicas": replicas})
    return {"ok": True, "service": name, "image": image, "replicas": replicas,
            "output": out[-800:]}


@capability(
    "foundry.cluster.ps", http_method="GET", http_path="/foundry/cluster/ps",
    http_tags=["foundry"], memory="off", silent=True,
    description="What's running on the Docker Swarm + where: nodes, services, and task "
                "placement (across the netbooted worker nodes). Runs on the swarm manager via "
                "pct exec. Inputs: cluster_id, node (auto), manager_vmid (int=201). "
                "Output: {ok, output}.",
)
async def cap_cluster_ps(cluster_id: str = "", node: str = "", manager_vmid: int = 201,
                         trace_id=None) -> Dict:
    if not node:
        node = await _resolve_node(cluster_id)
    cmd = ("echo '== NODES =='; docker node ls 2>&1; echo; echo '== SERVICES =='; "
           "docker service ls 2>&1; echo; echo '== TASKS =='; "
           "for s in $(docker service ls -q 2>/dev/null); do docker service ps "
           "--format '{{.Name}} -> {{.Node}} [{{.CurrentState}}]' $s 2>/dev/null; done | head -40")
    res = await _apply_ct_feature(cluster_id, int(manager_vmid), "lxc", cmd, node)
    return {"ok": not res.get("error"),
            "output": (res.get("stdout") or res.get("out") or res.get("error") or "")[-2500:]}


@capability(
    "foundry.cluster.rm", http_method="POST", http_path="/foundry/cluster/rm",
    http_tags=["foundry"], memory="on",
    description="Remove a swarm service (stop the dispatched compute). Inputs: cluster_id, "
                "node (auto), manager_vmid (int=201), name (str!). Output: {ok, output}.",
)
async def cap_cluster_rm(cluster_id: str = "", node: str = "", manager_vmid: int = 201,
                         name: str = "", trace_id=None) -> Dict:
    name = (name or "").strip()
    if not name:
        return {"error": "name required"}
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    if not node:
        node = await _resolve_node(cluster_id)
    res = await _apply_ct_feature(cluster_id, int(manager_vmid), "lxc",
                                  f"docker service rm {safe}", node)
    await emit_event({"type": "foundry.cluster.rm", "service": safe})
    return {"ok": not res.get("error"),
            "output": (res.get("stdout") or res.get("out") or res.get("error") or "")[-500:]}


async def _cluster_records() -> Dict[str, Dict]:
    r = _redis()
    if not r:
        return {}
    raw = await r.hgetall(K_CLUSTERS) or {}
    byname: Dict[str, Dict] = {}
    for v in raw.values():
        try:
            rec = json.loads(v)
            byname[rec["name"]] = rec
        except Exception:
            pass
    return byname


async def _resolve_cluster_scripts(feats) -> List[str]:
    """Turn cluster features into real join scripts (token unsealed just-in-time):
    'cluster:<name>' joins that cluster; bare 'docker-swarm' joins the default (or
    first) docker-swarm cluster; 'distributed-compute' the first registered cluster."""
    byname = await _cluster_records()
    if not byname:
        return []
    wanted: List[Dict] = []
    for f in feats:
        f = str(f)
        if f.startswith("cluster:"):
            nm = f.split(":", 1)[1].strip()
            if nm in byname:
                wanted.append(byname[nm])
        elif f == "docker-swarm":
            cand = next((c for c in byname.values()
                         if c.get("name") == "default" and c.get("kind") == "docker-swarm"), None)
            cand = cand or next((c for c in byname.values() if c.get("kind") == "docker-swarm"), None)
            if cand:
                wanted.append(cand)
        elif f == "distributed-compute":
            cand = next((c for c in byname.values()), None)
            if cand:
                wanted.append(cand)
    scripts, seen = [], set()
    for rec in wanted:
        if rec["name"] in seen:
            continue
        seen.add(rec["name"])
        tok = vsecrets.open_secret(rec["token"]) if rec.get("token") else ""
        s = cluster_join_script(rec.get("kind", ""), rec.get("join_addr", ""), tok,
                                rec.get("role", ""), rec.get("opts") or {})
        if s:
            scripts.append(s)
    return scripts


# hardening bundle (_HARDEN) is imported from foundry_core (app-free, testable).


async def _apply_ct_feature(cluster_id, vmid, guest_type, script, node="") -> Dict:
    return await _call("proxmox.guest.exec", cluster_id=cluster_id, node=node, vmid=vmid,
                       guest_type=guest_type, command=script, timeout=180)


def _vera_key_path() -> str:
    """Path to Vera's SSH PRIVATE key (baked into VMs as the authorized pubkey)."""
    return str(Path.home() / ".vera" / "ssh" / "id_vera")


async def _wait_ssh(cluster_id, ip, port: int = 22, timeout: int = 180) -> bool:
    """Poll (from the PVE node, via bash /dev/tcp — no nc needed) until the guest's
    SSH port is open. Cloud-init needs ~a minute to boot + install Vera's key."""
    if not ip:
        return False
    cmd = (f"for i in $(seq 1 {max(1, timeout // 5)}); do "
           f"timeout 3 bash -c '</dev/tcp/{ip}/{port}' 2>/dev/null && {{ echo SSH_UP; exit 0; }}; "
           "sleep 5; done; echo TIMEOUT")
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=cmd, timeout=timeout + 20)
    return "SSH_UP" in (res.get("stdout", "") or "")


async def _post_provision(cluster_id, node, vmid, kind, feats, fqdn, job_id="", ip=""):
    """Background: wait for the guest, then enrol + apply features; patches the stored
    job so the sync provision call returns fast.
    LXC → enrol via Proxmox (pct exec, root). QEMU/VM → enrol over SSH to the static
    IP with Vera's baked key (cloud-init user 'vera' + sudo), since cloud images ship
    no guest agent."""
    steps = []
    want_enrol = ("enrol" in feats or "mesh" in feats)
    try:
        running = await _wait_guest_running(cluster_id, vmid, kind)
        steps.append({"boot": {"running": running}})
        if running and kind == "lxc":
            if want_enrol:
                res = await _call("enroll.guest", cluster_id=cluster_id, vmid=vmid,
                                  guest_type="lxc", node=node, fqdn=fqdn or "", via_proxmox=True)
                steps.append({"enrol": {"ok": not res.get("error"),
                              "identity": (res.get("steps") or {}).get("identity", {}).get("ok"),
                              "mesh": (res.get("steps") or {}).get("mesh")}})
            if "hardening" in feats:
                h = await _apply_ct_feature(cluster_id, vmid, "lxc", _HARDEN, node)
                steps.append({"hardening": {"ok": bool(h.get("ok"))}})
        elif running and kind == "qemu":
            if want_enrol and ip:
                ready = await _wait_ssh(cluster_id, ip)
                steps.append({"ssh_wait": {"ip": ip, "reachable": ready}})
                if ready:
                    res = await _call("enroll.guest", cluster_id=cluster_id, vmid=vmid,
                                      guest_type="qemu", node=node, fqdn=fqdn or "", ip=ip,
                                      ssh_user="vera", ssh_key_path=_vera_key_path())
                    steps.append({"enrol": {"ok": not res.get("error"),
                                  "identity": (res.get("steps") or {}).get("identity", {}).get("ok"),
                                  "mesh": (res.get("steps") or {}).get("mesh"),
                                  "error": res.get("error")}})
            elif want_enrol:
                steps.append({"enrol": {"status": "skipped",
                              "note": "VM enrol needs a static ip (none set)"}})
            # hardening for VMs also rides SSH+sudo — enrol registers the host in the
            # exec store; a follow-up applies _HARDEN over that. Noted for now.
            if "hardening" in feats:
                steps.append({"hardening": {"status": "pending",
                              "note": "VM hardening over SSH is the next increment"}})
        # apply cluster / distributed-compute joins (registry-resolved; token unsealed
        # just-in-time) — CT via pct exec (root), VM over SSH as 'vera' with sudo.
        if running:
            for cs in await _resolve_cluster_scripts(feats):
                body = "#!/bin/sh\n" + cs
                if kind == "lxc":
                    cj = await _apply_ct_feature(cluster_id, vmid, "lxc", body, node)
                    steps.append({"cluster_join": {"ok": bool(cj.get("ok"))}})
                elif kind == "qemu" and ip:
                    b = base64.b64encode(body.encode()).decode()
                    cj = await _call("exec.ssh.run", host=ip, user="vera",
                                     key_path=_vera_key_path(),
                                     command=f"echo {b} | base64 -d | sudo -n sh", timeout=600)
                    steps.append({"cluster_join": {"ok": bool(cj.get("ok")),
                                  "rc": cj.get("rc"), "error": cj.get("error")}})
    except Exception as e:
        steps.append({"post_error": str(e)})
    # patch the stored job (find by id in the K_JOBS list)
    r = _redis()
    if r and job_id:
        try:
            rows = await r.lrange(K_JOBS, 0, 199) or []
            for i, raw in enumerate(rows):
                jd = json.loads(raw)
                if jd.get("id") == job_id:
                    jd["steps"].extend(steps)
                    jd["status"] = "ok"
                    await r.lset(K_JOBS, i, json.dumps(jd))
                    break
        except Exception:
            pass
    await emit_event({"type": "foundry.provision.finished", "job": job_id,
                      "vmid": vmid, "steps": steps})


async def _wait_guest_running(cluster_id, vmid, kind="lxc", timeout=90) -> bool:
    # For CTs, `pct create --start 1` often races the config lock and leaves the
    # container stopped — retry `pct start` each poll until it's running.
    tool = "pct" if kind == "lxc" else "qm"
    start = f"{tool} start {vmid} 2>/dev/null || true; " if kind == "lxc" else ""
    cmd = (f"for i in $(seq 1 {max(1, timeout // 3)}); do "
           f"{tool} status {vmid} 2>/dev/null | grep -q running && {{ echo RUNNING; exit 0; }}; "
           f"{start}sleep 3; done; echo TIMEOUT")
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=cmd, timeout=timeout + 15)
    return "RUNNING" in (res.get("stdout", "") or "")


@capability(
    "foundry.provision",
    http_method="POST", http_path="/foundry/provision", http_tags=["foundry"],
    memory="on",
    description="Stand up an OS: target (ct|vm|docker) × image_id (from the "
                "catalogue) × features[]. All three targets are live (CT/VM need the "
                "image imported first — foundry.image.import). Inputs: target, "
                "image_id, name (hostname), features (csv/list), cluster_id, node, "
                "cores (int=1), memory (int=1024 MB), disk (int=8 GB), storage (str — "
                "blank auto-resolves), fqdn (str — FreeIPA name), ip (str — static "
                "address, this LAN has no DHCP; blank=dhcp), gateway (str). CT enrol+"
                "mesh via auto-enrol + hardening script; VM bakes Vera's SSH key + "
                "static IP (post-boot enrol next); other features recorded pending. "
                "Output: {ok, job_id, target, steps}.",
)
async def cap_provision(target: str = "", image_id: str = "", name: str = "",
                        features="", cluster_id: str = "", node: str = "",
                        cores: int = 1, memory: int = 1024, disk: int = 8,
                        storage: str = "", fqdn: str = "", ip: str = "",
                        gateway: str = "", trace_id=None) -> Dict:
    r = _redis()
    if isinstance(features, str):
        feats = [f.strip() for f in features.replace(",", " ").split() if f.strip()]
    else:
        feats = [str(f) for f in (features or [])]
    if "enrol" not in feats:
        feats.insert(0, "enrol")            # baseline
    img = None
    if r and image_id:
        raw = await r.hget(K_IMAGES, image_id)
        if raw:
            try:
                img = json.loads(raw)
            except Exception:
                img = None
    if not img:
        return {"error": f"image '{image_id}' not in catalogue (foundry.image.list)"}
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "target": target, "image": image_id, "name": name,
           "features": feats, "created": time.time(), "steps": [], "status": "running"}

    def step(k, v):
        job["steps"].append({k: v}); return v

    # resolve a node when none given — clone/create need it; empty → /nodes//… 501
    if target in ("ct", "vm") and not node:
        node = await _resolve_node(cluster_id) or node
    # resolve a real node storage if none/invalid given (local-lvm may not exist)
    if target in ("ct", "vm") and (not storage or storage == "local-lvm"):
        storage = await _resolve_storage(cluster_id) or storage
    net0, ipconfig = _netcfg(ip, gateway)   # static IP (no DHCP on this LAN) or dhcp

    want_enrol = "enrol" in feats or "mesh" in feats
    if target == "ct":
        if img.get("type") != "lxc-template":
            return {"error": f"CT target needs an lxc-template image; '{image_id}' is {img.get('type')}"}
        tmpl = img.get("source_url", "")
        if ":" not in tmpl or "vztmpl" not in tmpl:
            return {"error": f"LXC template not downloaded yet for '{image_id}' — build it "
                             "first (foundry.image.import) so there's a real volid"}
        res = await _call("proxmox.lxc.create", cluster_id=cluster_id, node=node,
                          ostemplate=tmpl, hostname=name or "",
                          storage=storage, cores=cores, memory=memory, disk=disk,
                          net0=net0,
                          features="nesting=1,keyctl=1" if "docker-swarm" in feats else "",
                          auto_enroll=False)   # enrol AFTER it's running (avoid create-task race)
        step("create", res)
        vmid = res.get("vmid")
        if res.get("error") or not vmid:
            job["status"] = "error"
        else:
            job["vmid"] = vmid
            job["status"] = "ok"
            for f in feats:
                if f in ("file-server", "security-monitoring", "docker-swarm",
                         "distributed-compute", "file-client"):
                    step(f, {"status": "pending", "note": "bundle script lands next"})
            step("post", {"status": "applying",
                          "note": "start + enrol + hardening running in background — watch events / jobs"})
            asyncio.create_task(_post_provision(cluster_id, node, vmid, "lxc", feats, fqdn, job_id))
    elif target == "docker":
        if img.get("type") != "docker":
            return {"error": f"Docker target needs a docker image; '{image_id}' is {img.get('type')}"}
        res = await _call("docker.run", image=img.get("source_url", ""),
                          name=name or f"foundry-{job_id}", detach=True)
        step("run", res)
        job["status"] = "error" if res.get("error") else "ok"
        for f in feats:
            if f not in ("enrol",):
                step(f, {"status": "pending",
                         "note": "container features (mesh/compute) land next"})
    elif target == "vm":
        if img.get("type") not in ("cloudimg", "iso"):
            return {"error": f"VM target needs a cloudimg/iso image; '{image_id}' is {img.get('type')}"}
        tmpl = img.get("template_vmid")
        if tmpl:
            res = await _call("proxmox.vm.create", cluster_id=cluster_id, node=node,
                              template_vmid=int(tmpl), name=name, cores=cores,
                              memory=memory, disk=disk, storage=storage,
                              ipconfig=ipconfig, sshkeys=_vera_pubkey())
            step("create", res)
            vmid = res.get("vmid")
            if res.get("error") or not vmid:
                job["status"] = "error"
            else:
                job["status"] = "ok"
                job["vmid"] = vmid
                # VM boots with Vera's key + static IP; enrol it in the background
                # over SSH to that IP (cloud image ships no guest agent).
                step("post", {"status": "applying",
                              "note": "VM boot + SSH enrol running in background — watch events / jobs"})
                asyncio.create_task(_post_provision(cluster_id, node, vmid, "qemu",
                                                    feats, fqdn, job_id, ip))
        else:
            step("create", {"status": "pending",
                            "note": "no cloud-init template linked to this cloudimg yet — "
                                    "build one from the image, or set template_vmid via "
                                    "foundry.image.add (image-import pipeline lands next)"})
            job["status"] = "pending"
    else:
        return {"error": "target must be one of: ct | vm | docker"}

    if r:
        await r.lpush(K_JOBS, json.dumps(job))
        await r.ltrim(K_JOBS, 0, 199)
    await emit_event({"type": "foundry.provision", "job": job_id, "target": target,
                      "image": image_id, "status": job["status"]})
    return {"ok": job["status"] in ("ok", "pending"), "job_id": job_id,
            "target": target, "status": job["status"], "steps": job["steps"],
            "vmid": job.get("vmid")}


@capability(
    "foundry.jobs",
    http_method="GET", http_path="/foundry/jobs", http_tags=["foundry"],
    memory="off", silent=True,
    description="Recent provision jobs (newest first). Output: {jobs:[...]}.",
)
async def cap_jobs(limit: int = 30, trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"jobs": []}
    rows = await r.lrange(K_JOBS, 0, max(1, min(limit, 200)) - 1) or []
    jobs = []
    for v in rows:
        try:
            jobs.append(json.loads(v))
        except Exception:
            pass
    return {"jobs": jobs}


# ─────────────────────────────────────────────────────────────────────────────
# Blueprints — declarative, versioned, reusable Infrastructure-as-Code. A
# blueprint describes a whole estate (N nodes, each = target × image × features ×
# resources); it is versioned on every save (prior versions snapshotted),
# re-applied idempotently by fanning out to foundry.provision, and exported as a
# portable YAML manifest you can commit to Gitea/git. This is what makes Foundry
# "a versioned, reusable infra setup tool like Docker/Terraform".
# ─────────────────────────────────────────────────────────────────────────────
K_BP = "vera:foundry:blueprints"
K_BP_RUNS = "vera:foundry:bp_runs"


def _bp_versions_key(bid: str) -> str:
    return f"vera:foundry:bp:{bid}:versions"


@capability(
    "foundry.blueprint.save",
    http_method="POST", http_path="/foundry/blueprint/save", http_tags=["foundry"],
    memory="on",
    description="Create or update a versioned, reusable provisioning blueprint "
                "(Infrastructure-as-Code, like a docker-compose for OS estates). "
                "Inputs: id (blank = new), name (str!), description (str), nodes "
                "(list/JSON of {name,target,image_id,count,features,cluster_id,node,"
                "cores,memory,disk,fqdn}). Updating an existing id snapshots the "
                "prior version and bumps the version. Output: {ok, id, version}.",
)
async def cap_bp_save(id: str = "", name: str = "", description: str = "",
                      nodes=None, trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"error": "no redis"}
    if isinstance(nodes, str):
        try:
            nodes = json.loads(nodes)
        except Exception:
            return {"error": "nodes must be a JSON list"}
    nodes = nodes or []
    now = time.time()
    if id:
        raw = await r.hget(K_BP, id)
        if not raw:
            return {"error": f"blueprint {id} not found"}
        cur = json.loads(raw)
        await r.lpush(_bp_versions_key(id), raw)      # snapshot the prior version
        await r.ltrim(_bp_versions_key(id), 0, 49)
        doc = {**cur, "name": name or cur.get("name"),
               "description": description if description else cur.get("description", ""),
               "nodes": nodes if nodes else cur.get("nodes", []),
               "version": int(cur.get("version", 1)) + 1, "updated": now}
    else:
        if not name:
            return {"error": "name required"}
        id = uuid.uuid4().hex[:12]
        doc = {"id": id, "name": name, "description": description, "nodes": nodes,
               "version": 1, "created": now, "updated": now}
    await r.hset(K_BP, id, json.dumps(doc))
    await emit_event({"type": "foundry.blueprint.saved", "id": id, "version": doc["version"]})
    return {"ok": True, "id": id, "version": doc["version"]}


@capability(
    "foundry.blueprint.list",
    http_method="GET", http_path="/foundry/blueprint/list", http_tags=["foundry"],
    memory="off", silent=True,
    description="List provisioning blueprints (latest version each). "
                "Output: {blueprints:[{id,name,version,nodes,description,updated}]}.",
)
async def cap_bp_list(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"blueprints": []}
    rows = await r.hgetall(K_BP) or {}
    out = []
    for v in rows.values():
        try:
            d = json.loads(v)
            out.append({"id": d["id"], "name": d.get("name"), "version": d.get("version"),
                        "nodes": len(d.get("nodes", [])), "description": d.get("description", ""),
                        "updated": d.get("updated")})
        except Exception:
            pass
    out.sort(key=lambda b: (b.get("name") or "").lower())
    return {"blueprints": out}


@capability(
    "foundry.blueprint.get",
    http_method="GET", http_path="/foundry/blueprint/get", http_tags=["foundry"],
    memory="off", silent=True,
    description="Get a blueprint, optionally a past version. Inputs: id (str!), "
                "version (int — blank/0 = latest). Output: the doc + {versions:[...]}.",
)
async def cap_bp_get(id: str = "", version: int = 0, trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    raw = await r.hget(K_BP, id)
    if not raw:
        return {"error": "not found"}
    latest = json.loads(raw)
    hist = await r.lrange(_bp_versions_key(id), 0, -1) or []
    old_versions = []
    for h in hist:
        try:
            old_versions.append(json.loads(h).get("version"))
        except Exception:
            pass
    all_versions = [latest.get("version")] + old_versions
    if version and version != latest.get("version"):
        for h in hist:
            try:
                d = json.loads(h)
                if d.get("version") == version:
                    return {**d, "versions": all_versions, "is_latest": False}
            except Exception:
                pass
        return {"error": f"version {version} not found"}
    return {**latest, "versions": all_versions, "is_latest": True}


@capability(
    "foundry.blueprint.delete",
    http_method="POST", http_path="/foundry/blueprint/delete", http_tags=["foundry"],
    memory="on", description="Delete a blueprint + its version history. Input: id (str!).",
)
async def cap_bp_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    await r.hdel(K_BP, id)
    await r.delete(_bp_versions_key(id))
    return {"ok": True, "id": id}


@capability(
    "foundry.blueprint.apply",
    http_method="POST", http_path="/foundry/blueprint/apply", http_tags=["foundry"],
    memory="on",
    description="Apply a blueprint — provision every node (× count) it declares by "
                "fanning out to foundry.provision. Inputs: id (str!), version (int — "
                "blank = latest), dry_run (bool — plan only, no changes). "
                "Output: {ok, run_id, results:[{node,target,status,job_id}]}.",
)
async def cap_bp_apply(id: str = "", version: int = 0, dry_run: bool = False,
                       trace_id=None) -> Dict:
    got = await cap_bp_get(id=id, version=version)
    if got.get("error"):
        return got
    results = []
    for n in got.get("nodes", []):
        count = int(n.get("count", 1) or 1)
        for i in range(max(1, count)):
            nm = n.get("name", "node")
            if count > 1:
                nm = f"{nm}-{i + 1}"
            if dry_run:
                results.append({"node": nm, "target": n.get("target"),
                                "image": n.get("image_id"), "features": n.get("features", []),
                                "status": "dry-run"})
                continue
            res = await _call("foundry.provision", target=n.get("target", "ct"),
                              image_id=n.get("image_id", ""), name=nm,
                              features=n.get("features", []), cluster_id=n.get("cluster_id", ""),
                              node=n.get("node", ""), cores=int(n.get("cores", 1) or 1),
                              memory=int(n.get("memory", 1024) or 1024),
                              disk=int(n.get("disk", 8) or 8), fqdn=n.get("fqdn", ""),
                              ip=n.get("ip", ""), gateway=n.get("gateway", ""))
            results.append({"node": nm, "target": n.get("target"),
                            "status": res.get("status") or ("error" if res.get("error") else "?"),
                            "job_id": res.get("job_id"), "error": res.get("error")})
    run_id = uuid.uuid4().hex[:12]
    r = _redis()
    if r:
        await r.lpush(K_BP_RUNS, json.dumps({"run_id": run_id, "blueprint": id,
                      "version": got.get("version"), "dry_run": dry_run,
                      "results": results, "ts": time.time()}))
        await r.ltrim(K_BP_RUNS, 0, 99)
    await emit_event({"type": "foundry.blueprint.applied", "id": id, "run": run_id,
                      "nodes": len(results), "dry_run": dry_run})
    return {"ok": True, "run_id": run_id, "results": results}


@capability(
    "foundry.blueprint.export",
    http_method="GET", http_path="/foundry/blueprint/export", http_tags=["foundry"],
    memory="off", silent=True,
    description="Export a blueprint as a portable IaC manifest (YAML if available, "
                "else JSON) to commit to git/Gitea for versioned, reusable infra. "
                "Inputs: id (str!), format (yaml|json). Output: {ok, format, text}.",
)
async def cap_bp_export(id: str = "", format: str = "yaml", trace_id=None) -> Dict:
    got = await cap_bp_get(id=id)
    if got.get("error"):
        return got
    doc = {"foundry_blueprint": {"apiVersion": "foundry/v1",
           "name": got.get("name"), "description": got.get("description", ""),
           "nodes": got.get("nodes", [])}}
    if format == "yaml":
        try:
            import yaml
            return {"ok": True, "format": "yaml",
                    "text": yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)}
        except Exception:
            pass
    return {"ok": True, "format": "json", "text": json.dumps(doc, indent=2)}


@capability(
    "foundry.blueprint.import",
    http_method="POST", http_path="/foundry/blueprint/import", http_tags=["foundry"],
    memory="on",
    description="Import a blueprint from a YAML or JSON IaC manifest (round-trips "
                "foundry.blueprint.export) and save it as a new blueprint. "
                "Input: text (str!). Output: {ok, id, version}.",
)
async def cap_bp_import(text: str = "", trace_id=None) -> Dict:
    if not text.strip():
        return {"error": "text (YAML/JSON manifest) required"}
    doc = None
    try:
        import yaml
        doc = yaml.safe_load(text)
    except Exception:
        try:
            doc = json.loads(text)
        except Exception:
            return {"error": "not valid YAML or JSON"}
    bp = (doc or {}).get("foundry_blueprint") or doc or {}
    return await cap_bp_save(name=bp.get("name", ""), description=bp.get("description", ""),
                             nodes=bp.get("nodes", []))


# ─────────────────────────────────────────────────────────────────────────────
# PXE / physical + ISO — the SAME image catalogue + feature bundles, delivered to
# bare metal via netboot. A PXE *profile* is the physical/ISO analogue of a
# provision request / blueprint node: image × features[] × autoinstall. This is
# the management layer (config, profiles, MAC waiting-room) driveable from the UI;
# the netboot server itself is stood up on a dedicated provisioning bridge/VLAN
# (foundry.pxe.server.deploy → the vera-foundry host) — see the roadmap.
# ─────────────────────────────────────────────────────────────────────────────
K_PXE_CFG = "vera:foundry:pxe:config"
K_PXE_PROFILES = "vera:foundry:pxe:profiles"
K_PXE_MACS = "vera:foundry:pxe:macs"
PXE_DEFAULTS = {
    "enabled": False, "deployed": False, "host": "",
    "bridge": "vmbr1", "subnet": "10.42.0.0/24",
    "dhcp_from": "10.42.0.50", "dhcp_to": "10.42.0.200", "gateway": "10.42.0.1",
    "default_action": "local", "note": "",
}


async def _pxe_cfg() -> Dict:
    r = _redis()
    cfg = dict(PXE_DEFAULTS)
    if r:
        raw = await r.hget(K_PXE_CFG, "main")
        if raw:
            try:
                cfg.update(json.loads(raw))
            except Exception:
                pass
    return cfg


@capability(
    "foundry.pxe.config", http_method="GET", http_path="/foundry/pxe/config",
    http_tags=["foundry"], memory="off", silent=True,
    description="PXE/netboot server config (bridge/VLAN, DHCP range, gateway, "
                "deploy host, enabled/deployed). Output: the config.",
)
async def cap_pxe_config(trace_id=None) -> Dict:
    return await _pxe_cfg()


@capability(
    "foundry.pxe.config.save", http_method="POST", http_path="/foundry/pxe/config/save",
    http_tags=["foundry"], memory="on",
    description="Update PXE server config. Inputs (any of): enabled (bool), host "
                "(str — the provisioning host, e.g. the vera-foundry CT), bridge, "
                "subnet, dhcp_from, dhcp_to, gateway, default_action (local|menu). "
                "Runs on a dedicated provisioning bridge/VLAN, isolated from the main "
                "LAN. Output: the saved config.",
)
async def cap_pxe_config_save(enabled=None, host=None, bridge=None, subnet=None,
                              dhcp_from=None, dhcp_to=None, gateway=None,
                              default_action=None, trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"error": "no redis"}
    cfg = await _pxe_cfg()
    for k, v in (("enabled", enabled), ("host", host), ("bridge", bridge),
                 ("subnet", subnet), ("dhcp_from", dhcp_from), ("dhcp_to", dhcp_to),
                 ("gateway", gateway), ("default_action", default_action)):
        if v is not None:
            cfg[k] = v
    await r.hset(K_PXE_CFG, "main", json.dumps(cfg))
    await emit_event({"type": "foundry.pxe.config.saved"})
    return cfg


@capability(
    "foundry.pxe.profile.save", http_method="POST", http_path="/foundry/pxe/profile/save",
    http_tags=["foundry"], memory="on",
    description="Create/update a PXE boot profile — the physical/ISO analogue of a "
                "provision node: an image × feature bundles × autoinstall. Inputs: id "
                "(blank=new), name (str!), image_id (str! — a cloudimg/iso catalogue "
                "entry), features (csv/list — same bundles as CT/VM), disk (int GB), "
                "autoinstall (str — extra preseed/kickstart/cloud-init), arch "
                "(amd64|arm64), boot_type (uefi|bios|rpi-netboot|rpi-flash — blank "
                "auto-picks by arch), display (hdmi|xpt2046 — xpt2046 = 3.2\" SPI "
                "touchscreen on a Raspberry Pi), ip (static, no DHCP on the main LAN). "
                "arch: amd64|arm64. boot_type: uefi|bios|rpi-netboot|rpi-flash. "
                "display: hdmi|xpt2046. Output: {ok,id}.",
)
async def cap_pxe_profile_save(id="", name="", image_id="", features=None, disk: int = 20,
                               autoinstall="", arch="amd64", boot_type="", display="hdmi",
                               ip="", display_opts=None, trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"error": "no redis"}
    if isinstance(features, str):
        features = [f.strip() for f in features.replace(",", " ").split() if f.strip()]
    features = features or ["enrol", "hardening"]
    if isinstance(display_opts, str):
        try:
            display_opts = json.loads(display_opts) if display_opts.strip() else {}
        except Exception:
            return {"error": "display_opts must be a JSON object"}
    arch = (arch or "amd64").lower()
    boot_type = (boot_type or ("rpi-netboot" if arch in ("arm64", "armhf") else "uefi")).lower()
    display = (display or "hdmi").lower()
    if not id:
        if not name:
            return {"error": "name required"}
        id = uuid.uuid4().hex[:12]
    prof = {"id": id, "name": name, "image_id": image_id, "features": features,
            "disk": int(disk), "autoinstall": autoinstall, "arch": arch,
            "boot_type": boot_type, "display": display, "ip": ip.strip(),
            "display_opts": display_opts or {}, "updated": time.time()}
    await r.hset(K_PXE_PROFILES, id, json.dumps(prof))
    await emit_event({"type": "foundry.pxe.profile.saved", "id": id})
    return {"ok": True, "id": id}


@capability(
    "foundry.pxe.profile.list", http_method="GET", http_path="/foundry/pxe/profile/list",
    http_tags=["foundry"], memory="off", silent=True,
    description="List PXE boot profiles. Output: {profiles:[...]}.",
)
async def cap_pxe_profile_list(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"profiles": []}
    rows = await r.hgetall(K_PXE_PROFILES) or {}
    out = []
    for v in rows.values():
        try:
            out.append(json.loads(v))
        except Exception:
            pass
    out.sort(key=lambda p: (p.get("name") or "").lower())
    return {"profiles": out}


@capability(
    "foundry.pxe.profile.delete", http_method="POST", http_path="/foundry/pxe/profile/delete",
    http_tags=["foundry"], memory="on", description="Delete a PXE profile. Input: id (str!).",
)
async def cap_pxe_profile_delete(id="", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    await r.hdel(K_PXE_PROFILES, id)
    return {"ok": True, "id": id}


@capability(
    "foundry.pxe.mac.add", http_method="POST", http_path="/foundry/pxe/mac/add",
    http_tags=["foundry"], memory="on",
    description="Register/assign a physical machine by MAC to a boot profile "
                "(the waiting-room: an unknown MAC that PXE-boots gets its assigned "
                "profile, else the default action). Inputs: mac (str!), profile_id "
                "(str), hostname (str), ip (str — static, no DHCP on the main LAN). "
                "Output: {ok, mac}.",
)
async def cap_pxe_mac_add(mac="", profile_id="", hostname="", ip="", trace_id=None) -> Dict:
    r = _redis()
    if not r or not mac:
        return {"error": "mac required"}
    mac = mac.strip().lower()
    rec = {"mac": mac, "profile_id": profile_id, "hostname": hostname, "ip": ip,
           "status": "assigned" if profile_id else "waiting", "updated": time.time()}
    await r.hset(K_PXE_MACS, mac, json.dumps(rec))
    await emit_event({"type": "foundry.pxe.mac.assigned", "mac": mac, "profile": profile_id})
    return {"ok": True, "mac": mac}


@capability(
    "foundry.pxe.macs", http_method="GET", http_path="/foundry/pxe/macs",
    http_tags=["foundry"], memory="off", silent=True,
    description="The PXE waiting-room — physical machines seen/registered by MAC + "
                "their assigned profile. Output: {macs:[...]}.",
)
async def cap_pxe_macs(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"macs": []}
    rows = await r.hgetall(K_PXE_MACS) or {}
    out = []
    for v in rows.values():
        try:
            out.append(json.loads(v))
        except Exception:
            pass
    return {"macs": out}


@capability(
    "foundry.pxe.status", http_method="GET", http_path="/foundry/pxe/status",
    http_tags=["foundry"], memory="off", silent=True,
    description="PXE subsystem status: config + counts + whether the netboot server "
                "is deployed. Output: {deployed, enabled, profiles, macs, config, note}.",
)
async def cap_pxe_status(trace_id=None) -> Dict:
    cfg = await _pxe_cfg()
    profs = (await cap_pxe_profile_list()).get("profiles", [])
    macs = (await cap_pxe_macs()).get("macs", [])
    note = ("netboot server not deployed — set a provisioning host (the vera-foundry "
            "CT) + a dedicated bridge/VLAN, then foundry.pxe.server.deploy"
            if not cfg.get("deployed") else "netboot server deployed")
    return {"deployed": cfg.get("deployed"), "enabled": cfg.get("enabled"),
            "profiles": len(profs), "macs": len(macs), "config": cfg, "note": note}


# netboot artifact rendering (_pxe_slug, _render_features_script, _render_rpi_config,
# _render_rpi_cmdline, _render_ipxe, _render_autoinstall, _render_boot) is imported
# from foundry_core — pure logic, unit-tested without booting the app.


@capability(
    "foundry.pxe.render", http_method="GET", http_path="/foundry/pxe/render",
    http_tags=["foundry"], memory="off", silent=True,
    description="Render a PXE boot profile into its netboot artifacts: iPXE + "
                "cloud-init autoinstall for x86; config.txt/cmdline.txt for Raspberry "
                "Pi (incl. the XPT2046 3.2\" SPI touchscreen overlay), plus the feature "
                "first-boot script (same bundles as CT/VM). Input: profile_id (str!). "
                "Output: {ok, boot_type, arch, display, features, artifacts:{name:content}}.",
)
async def cap_pxe_render(profile_id: str = "", trace_id=None) -> Dict:
    r = _redis()
    raw = await r.hget(K_PXE_PROFILES, profile_id) if (r and profile_id) else None
    if not raw:
        return {"error": f"profile '{profile_id}' not found"}
    prof = json.loads(raw)
    cfg = await _pxe_cfg()
    img: Dict = {}
    if prof.get("image_id") and r:
        iraw = await r.hget(K_IMAGES, prof["image_id"])
        if iraw:
            try:
                img = json.loads(iraw)
            except Exception:
                img = {}
    cscripts = await _resolve_cluster_scripts(prof.get("features") or [])
    return {"ok": True, **_render_boot(prof, cfg, img, cscripts)}


def _apkovl_tar_b64(files: Dict) -> str:
    """Build an Alpine apkovl (a gzip tar of an overlay rooted at /) from {relpath:
    content} in memory and base64-encode it — no fragile shell tar-building."""
    import io as _io, tarfile as _tf
    buf = _io.BytesIO()
    with _tf.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in files.items():
            data = content.encode()
            ti = _tf.TarInfo(path)
            ti.size = len(data)
            ti.mode = 0o755 if (path.startswith("usr/local/bin") or path.endswith(".start")) else 0o644
            tar.addfile(ti, _io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _pxe_server_setup_script(server_ip, iface, uplink, subnet, conf_b64, menu_b64, apkovl_b64, tui_b64="", sdwrite_b64="", desk_apk_b64="") -> str:
    """The node-side setup shell — reproduces the hand-proven netboot server: install
    dnsmasq+iPXE, write the (core-generated) fenced dnsmasq conf + iPXE menu + ops
    apkovl, fetch iPXE/Alpine/netboot.xyz/Debian-d-i assets, enable scoped NAT, then
    start dnsmasq (with a fencing gate that aborts if it binds anything but `iface`)
    and an HTTP server bound to `server_ip`."""
    return f"""set +e
mkdir -p /srv/foundry/tftp /srv/foundry/http/alpine /srv/foundry/http/swarm /srv/foundry/http/ops /srv/foundry/http/debian12
rm -f /usr/sbin/policy-rc.d
DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq ipxe curl >/tmp/foundry_pxe.log 2>&1
systemctl stop dnsmasq 2>/dev/null
echo {conf_b64} | base64 -d > /etc/dnsmasq.d/vera-foundry.conf
echo {menu_b64} | base64 -d > /srv/foundry/tftp/boot.ipxe
echo {apkovl_b64} | base64 -d > /srv/foundry/http/alpine/node.apkovl.tar.gz
echo {desk_apk_b64} | base64 -d > /srv/foundry/http/alpine/desktop.apkovl.tar.gz
echo {tui_b64} | base64 -d > /srv/foundry/http/ops/foundry-tui 2>/dev/null; chmod +x /srv/foundry/http/ops/foundry-tui 2>/dev/null
echo {sdwrite_b64} | base64 -d > /srv/foundry/http/ops/foundry-sdwrite 2>/dev/null; chmod +x /srv/foundry/http/ops/foundry-sdwrite 2>/dev/null
printf 'proxmox {server_ip}\\n' > /srv/foundry/http/ops/pve_hosts
printf 'raspios-lite-arm64 https://downloads.raspberrypi.com/raspios_lite_arm64_latest\\nraspios-desktop-arm64 https://downloads.raspberrypi.com/raspios_arm64_latest\\nraspios-full-arm64 https://downloads.raspberrypi.com/raspios_full_arm64_latest\\nraspios-lite-armhf https://downloads.raspberrypi.com/raspios_lite_armhf_latest\\n' > /srv/foundry/http/ops/pi_images
[ -f /srv/foundry/http/ops/authorized_keys ] || : > /srv/foundry/http/ops/authorized_keys
[ -f /srv/foundry/http/ops/id_estate ] || : > /srv/foundry/http/ops/id_estate
for f in undionly.kpxe ipxe.efi snponly.efi; do cp -f /usr/lib/ipxe/$f /srv/foundry/tftp/ 2>/dev/null; done
NB=https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/netboot
for f in vmlinuz-lts initramfs-lts modloop-lts; do [ -s /srv/foundry/http/alpine/$f ] || curl -fsS --max-time 220 -o /srv/foundry/http/alpine/$f "$NB/$f"; done
for f in netboot.xyz.lkrn netboot.xyz.efi; do [ -s /srv/foundry/http/$f ] || curl -fsSL --max-time 90 -o /srv/foundry/http/$f "https://github.com/netbootxyz/netboot.xyz/releases/latest/download/$f"; done
DI=http://deb.debian.org/debian/dists/bookworm/main/installer-amd64/current/images/netboot/debian-installer/amd64
[ -s /srv/foundry/http/debian12/linux ] || curl -fsS --max-time 150 -o /srv/foundry/http/debian12/linux "$DI/linux"
[ -s /srv/foundry/http/debian12/initrd.gz ] || curl -fsS --max-time 180 -o /srv/foundry/http/debian12/initrd.gz "$DI/initrd.gz"
# persist ip_forward + NAT as a boot-durable oneshot (ordered before dnsmasq)
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-foundry-netboot.conf
sysctl -w net.ipv4.ip_forward=1 >/dev/null
cat > /etc/systemd/system/foundry-nat.service <<'NATUNIT'
[Unit]
Description=Foundry netboot NAT (masquerade the provisioning subnet out the uplink)
After=network-online.target
Wants=network-online.target
Before=dnsmasq.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'sysctl -w net.ipv4.ip_forward=1; iptables -t nat -C POSTROUTING -s {subnet} -o {uplink} -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s {subnet} -o {uplink} -j MASQUERADE'
[Install]
WantedBy=multi-user.target
NATUNIT
# dnsmasq boot-safety drop-in: refuse to start unless {iface} exists (never fall back to the LAN)
mkdir -p /etc/systemd/system/dnsmasq.service.d
cat > /etc/systemd/system/dnsmasq.service.d/foundry-order.conf <<'DNSD'
[Unit]
After=network-online.target foundry-nat.service
Wants=network-online.target
[Service]
ExecStartPre=/bin/sh -c 'ip link show {iface} >/dev/null 2>&1 || {{ echo "{iface} absent - refusing to start dnsmasq"; exit 1; }}'
DNSD
systemctl daemon-reload
systemctl enable --now foundry-nat >/dev/null 2>&1
dnsmasq --test 2>&1
systemctl restart dnsmasq; sleep 2
L=$(ss -ulnp 2>/dev/null | grep -E ":67 ")
if echo "$L" | grep -q "{iface}:67" && ! echo "$L" | grep -qE "0\\.0\\.0\\.0:67[^%]|192\\.168\\.0\\."; then echo FENCE_OK; else echo FENCE_FAIL; systemctl stop dnsmasq; exit 2; fi
# fence passed -> make dnsmasq durable across reboots (safe: config binds {iface} only)
systemctl enable dnsmasq >/dev/null 2>&1
# persistent HTTP unit replaces the old transient systemd-run (which died on reboot)
systemctl stop foundry-http 2>/dev/null; systemctl reset-failed foundry-http 2>/dev/null; rm -f /run/systemd/transient/foundry-http.service 2>/dev/null
cat > /etc/systemd/system/foundry-http.service <<'HTTPUNIT'
[Unit]
Description=Foundry netboot HTTP server (PXE assets)
After=network-online.target foundry-nat.service
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 -m http.server 80 --bind {server_ip} --directory /srv/foundry/http
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
HTTPUNIT
systemctl daemon-reload
systemctl enable --now foundry-http >/dev/null 2>&1
sleep 1
echo "DEPLOY_OK dnsmasq=$(systemctl is-active dnsmasq) http=$(systemctl is-active foundry-http) nat=$(systemctl is-active foundry-nat)"
"""


@capability(
    "foundry.pxe.server.deploy", http_method="POST", http_path="/foundry/pxe/server/deploy",
    http_tags=["foundry"], memory="on",
    description="Stand up (idempotently) the netboot stack on a Proxmox node, bound to a "
                "DEDICATED bridge — dnsmasq DHCP+DNS+TFTP fenced to that interface (never the "
                "main LAN, verified by a fencing gate that aborts on a bad bind), iPXE + an HTTP "
                "boot menu generated from the Foundry image catalogue (local-disk default, "
                "Alpine RAM ops/desktop nodes that join the swarm as workers, netboot.xyz for all "
                "OSes incl. Kali, and catalogue-driven installers), plus scoped NAT so provisioned "
                "hosts reach package mirrors. Inputs: cluster_id (str), node (str — auto-resolved), "
                "iface (str='vmbr2'), server_ip (str='10.22.22.25'), range_lo/range_hi, uplink "
                "(str='vmbr0'), subnet (str='10.22.22.0/24'). Output: {ok, deployed, fenced, node}.",
)
async def cap_pxe_server_deploy(cluster_id: str = "", node: str = "", iface: str = "vmbr2",
                                server_ip: str = "10.22.22.25", range_lo: str = "10.22.22.100",
                                range_hi: str = "10.22.22.150", uplink: str = "vmbr0",
                                subnet: str = "10.22.22.0/24", trace_id=None) -> Dict:
    if not node:
        node = await _resolve_node(cluster_id)
    if not node:
        return {"error": "no node — pass node= or register a Proxmox cluster (proxmox.cluster.save)"}
    # install entries come from the catalogue (only the Debian d-i is hosted here; every
    # other OS install/live is covered by the netboot.xyz entry).
    install_images = [{"id": "debian12", "os": "Debian", "version": "12"}]
    conf = pxe_dnsmasq_conf(server_ip, iface, range_lo, range_hi, except_ifaces=[uplink])
    menu = pxe_ipxe_menu(server_ip, install_images=install_images)
    ops_files = pxe_ops_apkovl_files(server_ip)
    apk_b64 = _apkovl_tar_b64(ops_files)
    desk_apk_b64 = _apkovl_tar_b64(pxe_desktop_apkovl_files(server_ip))
    _b = lambda s: base64.b64encode(s.encode()).decode()
    tui_b64 = _b(ops_files["usr/local/bin/foundry-tui"])
    sdwrite_b64 = _b(ops_files["usr/local/bin/foundry-sdwrite"])
    script = _pxe_server_setup_script(server_ip, iface, uplink, subnet, _b(conf), _b(menu), apk_b64, tui_b64, sdwrite_b64, desk_apk_b64)
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=script, timeout=520)
    out = (res.get("stdout") or "") + (res.get("error") or "")
    fenced = "FENCE_OK" in out
    ok = "DEPLOY_OK" in out and fenced
    await emit_event({"type": "foundry.pxe.server.deployed", "node": node, "iface": iface, "ok": ok})
    return {"ok": ok, "deployed": ok, "fenced": fenced, "node": node, "iface": iface,
            "server_ip": server_ip, "subnet": subnet,
            "note": ("netboot server up + LAN-fenced; PXE-boot a client on the dedicated bridge"
                     if ok else "deploy did not fully succeed — see output"),
            "output": out[-1500:]}


@capability(
    "foundry.pxe.server.status", http_method="GET", http_path="/foundry/pxe/server/status",
    http_tags=["foundry"], memory="off", silent=True,
    description="Live status of the netboot server on a Proxmox node: dnsmasq/HTTP active, the "
                "exact listen bindings (to confirm DHCP/DNS/TFTP are fenced to the bridge), NAT "
                "rule present, and which boot assets are hosted. Inputs: cluster_id, node "
                "(auto-resolved), iface (str='vmbr2'). Output: {deployed, fenced, listeners, assets}.",
)
async def cap_pxe_server_status(cluster_id: str = "", node: str = "", iface: str = "vmbr2",
                                subnet: str = "10.22.22.0/24", trace_id=None) -> Dict:
    check = (f"echo DNSMASQ=$(systemctl is-active dnsmasq 2>/dev/null); "
             f"echo HTTP=$(systemctl is-active foundry-http 2>/dev/null); "
             f"echo LISTEN:; ss -ulnp 2>/dev/null | grep -E ':53 |:67 |:69 ' | sed -E 's/ +users.*//'; "
             f"echo NAT:; iptables -t nat -S POSTROUTING 2>/dev/null | grep '{subnet}' || echo none; "
             f"echo ASSETS:; ls /srv/foundry/tftp /srv/foundry/http /srv/foundry/http/alpine 2>/dev/null | tr '\\n' ' '")
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=check, timeout=40)
    out = res.get("stdout", "") or res.get("error", "")
    deployed = "DNSMASQ=active" in out
    fenced = (f"{iface}:67" in out) and ("0.0.0.0:67 " not in out) and ("192.168.0." not in out.split("NAT:")[0])
    return {"deployed": deployed, "fenced": fenced, "iface": iface, "output": out[-1500:]}


@capability(
    "foundry.pxe.server.teardown", http_method="POST", http_path="/foundry/pxe/server/teardown",
    http_tags=["foundry"], memory="on",
    description="Tear down the netboot server on a Proxmox node: stop dnsmasq + the HTTP server, "
                "remove the Foundry dnsmasq config + the scoped NAT rule. Leaves the bridge and "
                "the main LAN untouched. Inputs: cluster_id, node, subnet, uplink. Output: {ok}.",
)
async def cap_pxe_server_teardown(cluster_id: str = "", node: str = "", subnet: str = "10.22.22.0/24",
                                  uplink: str = "vmbr0", trace_id=None) -> Dict:
    cmd = (f"systemctl stop dnsmasq foundry-http 2>/dev/null; systemctl reset-failed foundry-http 2>/dev/null; "
           f"rm -f /etc/dnsmasq.d/vera-foundry.conf; "
           f"iptables -t nat -D POSTROUTING -s {subnet} -o {uplink} -j MASQUERADE 2>/dev/null; "
           f"echo TORNDOWN")
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=cmd, timeout=40)
    ok = "TORNDOWN" in (res.get("stdout", "") or "")
    await emit_event({"type": "foundry.pxe.server.teardown", "ok": ok})
    return {"ok": ok, "note": "dnsmasq/HTTP stopped, config + NAT removed; TFTP/HTTP files kept"}


@APP.get("/foundry/panel", include_in_schema=False)
async def _foundry_panel():
    p = _HERE / "foundry_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>foundry_panel.html not found</p>")


register_ui(
    "foundry",
    "Foundry",
    "🏭",
    """<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/foundry/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          title="Foundry — OS provisioning"></iframe>
</div>""",
    "",
    ui_caps=["foundry.image.list", "foundry.features", "foundry.provision",
             "foundry.jobs", "proxmox.cluster.list",
             "foundry.image.import", "foundry.image.import.status",
             "foundry.blueprint.list", "foundry.blueprint.save",
             "foundry.blueprint.apply", "foundry.blueprint.export",
             "foundry.pxe.status", "foundry.pxe.config", "foundry.pxe.config.save",
             "foundry.pxe.profile.list", "foundry.pxe.profile.save",
             "foundry.pxe.mac.add", "foundry.pxe.macs"],
    mode="element",     # embedded as a Workers & Ollama sub-tab
)
