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
import re
import shlex
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, enum_schema, register_ui, CAPABILITY_REGISTRY,
)

# Pure netboot render + hardening logic lives in an app-free core module so it's
# unit-testable without booting the orchestrator (see foundry_core.py header).
from Vera.vera.foundry.foundry_core import (
    _HARDEN, _pxe_slug, _render_features_script, _render_rpi_config,
    _render_rpi_cmdline, _render_ipxe, _render_autoinstall, _render_boot,
    pct_create_cmd,
    pick_node, cluster_join_script, CLUSTER_KINDS,
    cluster_init_script, parse_init_token,
    pxe_dnsmasq_conf, pxe_ipxe_menu, swarm_service_cmd,
    pxe_ops_apkovl_files, pxe_desktop_apkovl_files, parse_ops_secrets,
)
from Vera.vera.foundry.features_core import feature_script as _feature_script
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


async def _resolve_storage(cluster_id: str, content: str = "images") -> str:
    """Pick an active storage on the node that can hold guest disks — so Foundry
    isn't hardcoded to a storage name that may not exist (e.g. local-lvm vs
    local-zfs). Prefers block pools (zfspool/lvmthin/lvm), else dir/nfs/cifs."""
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command="pvesm status -content %s 2>/dev/null" % content)
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


async def _ct_create_ssh(cluster_id, node, ostemplate, hostname, storage, cores,
                         memory, disk, net0, unprivileged, features):
    """Create a CT the API token can't (privileged, or nesting/keyctl features -- both
    root@pam-only) by running `pct create` as root over SSH (proxmox.node.exec)."""
    nid = await _call("proxmox.nextid", cluster_id=cluster_id)
    vmid = nid.get("vmid")
    if not vmid:
        return {"error": "proxmox.nextid failed: %s" % nid.get("error", ""), "vmid": None}
    cmd = pct_create_cmd(vmid, ostemplate, hostname, storage, cores, memory, disk,
                         net0=net0, unprivileged=unprivileged, features=features)
    res = await _call("proxmox.node.exec", cluster_id=cluster_id, command=cmd, timeout=360)
    if not (bool(res.get("ok")) or res.get("exit_code") == 0):
        return {"error": "pct create (root/ssh) failed: %s"
                % ((res.get("stderr") or res.get("stdout") or res.get("error") or "")[:200]),
                "vmid": None}
    # WireGuard (mesh) needs /dev/net/tun, which a fresh privileged/nesting CT lacks;
    # bind it in via the CT config so it is present when _post_provision starts the CT.
    if features:
        _tun = ("printf '%s\\n%s\\n' 'lxc.cgroup2.devices.allow: c 10:200 rwm' "
                "'lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file' "
                ">> /etc/pve/lxc/" + str(vmid) + ".conf")
        await _call("proxmox.node.exec", cluster_id=cluster_id, command=_tun, timeout=30)
    return {"ok": True, "vmid": int(vmid), "via": "ssh-root",
            "features": features, "unprivileged": bool(unprivileged)}


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
    {"os": "arch", "version": "latest", "type": "lxc-template", "arch": "amd64",
     "source_url": "archlinux-base"},   # pveam appliance name (rolling)
    # Fedora 43
    {"os": "fedora", "version": "43", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2",
     "notes": "verify current build suffix at import time"},
    {"os": "fedora", "version": "43", "type": "lxc-template", "arch": "amd64",
     "source_url": "fedora-43-default"},
    {"os": "fedora", "version": "43", "type": "docker", "arch": "amd64", "source_url": "fedora:43"},
    # Kali (security testing)
    {"os": "kali", "version": "rolling", "type": "docker", "arch": "amd64",
     "source_url": "kalilinux/kali-rolling"},
    {"os": "kali", "version": "rolling", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://kali.download/cloud-images/current/kali-linux-current-cloud-genericcloud-amd64.tar.xz",
     "notes": "Kali cloud image (verify current path at import time)"},
    {"os": "kali", "version": "last-release", "type": "docker", "arch": "amd64",
     "source_url": "kalilinux/kali-last-release"},
    {"os": "kali", "version": "dev", "type": "docker", "arch": "amd64",
     "source_url": "kalilinux/kali-dev"},
    # Rocky Linux 9
    {"os": "rockylinux", "version": "9", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2"},
    {"os": "rockylinux", "version": "9", "type": "lxc-template", "arch": "amd64",
     "source_url": "rockylinux-9-default"},
    {"os": "rockylinux", "version": "9", "type": "docker", "arch": "amd64", "source_url": "rockylinux:9"},
    # CentOS Stream 9
    {"os": "centos", "version": "9-stream", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2"},
    {"os": "centos", "version": "9-stream", "type": "lxc-template", "arch": "amd64",
     "source_url": "centos-9-stream-default"},
    {"os": "centos", "version": "9-stream", "type": "docker", "arch": "amd64", "source_url": "quay.io/centos/centos:stream9"},
    # openSUSE Leap 15.6
    {"os": "opensuse", "version": "15.6", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
     "notes": "verify current filename at import time"},
    {"os": "opensuse", "version": "15.6", "type": "lxc-template", "arch": "amd64",
     "source_url": "opensuse-15.6-default"},
    {"os": "opensuse", "version": "15.6", "type": "docker", "arch": "amd64", "source_url": "opensuse/leap:15.6"},
    # Debian 13 (trixie)
    {"os": "debian", "version": "13", "type": "cloudimg", "arch": "amd64",
     "source_url": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"},
    {"os": "debian", "version": "13", "type": "lxc-template", "arch": "amd64",
     "source_url": "debian-13-standard"},
    {"os": "debian", "version": "13", "type": "docker", "arch": "amd64", "source_url": "debian:13"},
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
                "Ubuntu 24.04, AlmaLinux 9, Rocky 9, CentOS Stream 9, openSUSE 15.6, Alpine, Arch, Fedora 43, Debian 13, Kali, Windows-stub) "
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
    if not storage or storage == "local-lvm":
        storage = await _resolve_storage(cluster_id, "images") or storage
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
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "Host Samba (SMB) + NFS exports (default /srv/foundry)."},
    {"id": "security-monitoring", "label": "Security monitoring", "default": False,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "auditd baseline rules + optional rsyslog shipping (feature ctx log_collector)."},
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
    {"id": "vera-worker", "label": "Vera worker (native compute)", "default": False,
     "targets": ["ct", "vm", "physical"], "status": "ready",
     "desc": "Run a Vera WORKER container joined to the stack (Redis task stream) so "
             "dispatched jobs run here - the distribute-load-across-nodes feature. "
             "Needs the vera image reachable (registry) + docker."},
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


async def _post_provision(cluster_id, node, vmid, kind, feats, fqdn, job_id="", ip="", shares=None):
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
                try:
                    res = await asyncio.wait_for(
                        _call("enroll.guest", cluster_id=cluster_id, vmid=vmid,
                              guest_type="lxc", node=node, fqdn=fqdn or "", via_proxmox=True,
                              skip_mesh=("mesh" in feats)),
                        timeout=90)
                except asyncio.TimeoutError:
                    res = {"error": "enrol timed out (90s) -- continuing to features"}
                steps.append({"enrol": {"ok": not res.get("error"),
                              "identity": (res.get("steps") or {}).get("identity", {}).get("ok"),
                              "mesh": (res.get("steps") or {}).get("mesh"),
                              "error": res.get("error")}})
            if "hardening" in feats:
                h = await _apply_ct_feature(cluster_id, vmid, "lxc", _feature_script("hardening", {}), node)
                steps.append({"hardening": {"ok": bool(h.get("ok"))}})
            # OS-agnostic feature bundles (features_core) -- portable across distros.
            # enrol/mesh/hardening are handled above for CTs; apply the additional
            # portable features here (file-client now; more migrate here as we fan out).
            _fctx = await _features_ctx(shares)
            for _f in ("mesh", "file-client", "file-server", "security-monitoring", "vera-worker"):
                if _f in feats:
                    _sc = _feature_script(_f, _fctx)
                    if _sc:
                        _fr = await _apply_ct_feature(cluster_id, vmid, "lxc", _sc, node)
                        steps.append({_f: {"ok": bool(_fr.get("ok"))}})
        elif running and kind == "qemu":
            if want_enrol and ip:
                ready = await _wait_ssh(cluster_id, ip)
                steps.append({"ssh_wait": {"ip": ip, "reachable": ready}})
                if ready:
                    try:
                        res = await asyncio.wait_for(
                            _call("enroll.guest", cluster_id=cluster_id, vmid=vmid,
                                  guest_type="qemu", node=node, fqdn=fqdn or "", ip=ip,
                                  ssh_user="vera", ssh_key_path=_vera_key_path(),
                                  skip_mesh=("mesh" in feats)),
                            timeout=90)
                    except asyncio.TimeoutError:
                        res = {"error": "enrol timed out (90s) -- continuing to features"}
                    steps.append({"enrol": {"ok": not res.get("error"),
                                  "identity": (res.get("steps") or {}).get("identity", {}).get("ok"),
                                  "mesh": (res.get("steps") or {}).get("mesh"),
                                  "error": res.get("error")}})
            elif want_enrol:
                steps.append({"enrol": {"status": "skipped",
                              "note": "VM enrol needs a static ip (none set)"}})
            # hardening for VMs also rides SSH+sudo — enrol registers the host in the
            # exec store; a follow-up applies _HARDEN over that. Noted for now.
            # OS-agnostic feature bundles over SSH (features_core): hardening + portable features.
            if ip:
                _fctx = await _features_ctx(shares)
                for _f in ("hardening", "mesh", "file-client", "file-server", "security-monitoring", "vera-worker"):
                    if _f in feats:
                        _sc = _feature_script(_f, {} if _f == "hardening" else _fctx)
                        if _sc:
                            _b = base64.b64encode(_sc.encode()).decode()
                            _fr = await _call("exec.ssh.run", host=ip, user="vera",
                                              key_path=_vera_key_path(),
                                              command="echo %s | base64 -d | sudo -n sh" % _b, timeout=600)
                            steps.append({_f: {"ok": bool(_fr.get("ok")), "rc": _fr.get("rc")}})
            else:
                steps.append({"features": {"status": "skipped", "note": "VM features need a static ip"}})
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


async def _post_provision_docker(container, feats, job_id="", shares=None):
    """Apply the CONTAINER-APPLICABLE feature bundles inside a provisioned container via
    docker.exec. Host-level features (hardening / mesh / security-monitoring) do not map to
    a plain container -- they need a CT/VM/physical target -- so they are recorded as
    skipped rather than silently dropped."""
    steps = []
    HOST_ONLY = ("hardening", "mesh", "security-monitoring")
    try:
        _fctx = await _features_ctx(shares)
        for _f in ("file-client", "file-server", "vera-worker", "distributed-compute"):
            if _f in feats:
                _sc = _feature_script(_f, _fctx)
                if _sc:
                    _r = await _call("docker.exec", host_id="", container=container,
                                     command=_sc, timeout=600)
                    steps.append({_f: {"ok": bool(_r.get("ok")) or _r.get("rc") == 0,
                                       "rc": _r.get("rc"), "error": _r.get("error")}})
        for _f in feats:
            if _f in HOST_ONLY:
                steps.append({_f: {"skipped": "host-level feature -- provision a CT/VM/physical target"}})
        for cs in await _resolve_cluster_scripts(feats):
            _r = await _call("docker.exec", host_id="", container=container,
                             command="#!/bin/sh\n" + cs, timeout=600)
            steps.append({"cluster_join": {"ok": bool(_r.get("ok")) or _r.get("rc") == 0}})
    except Exception as e:
        steps.append({"post_error": str(e)})
    r = _redis()
    if r and job_id:
        try:
            rows = await r.lrange(K_JOBS, 0, 199) or []
            for i, raw in enumerate(rows):
                jd = json.loads(raw)
                if jd.get("id") == job_id:
                    jd["steps"].extend(steps); jd["status"] = "ok"
                    await r.lset(K_JOBS, i, json.dumps(jd)); break
        except Exception:
            pass
    await emit_event({"type": "foundry.provision.finished", "job": job_id,
                      "container": container, "steps": steps})


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
                        gateway: str = "", shares="", trace_id=None) -> Dict:
    r = _redis()
    if isinstance(features, str):
        feats = [f.strip() for f in features.replace(",", " ").split() if f.strip()]
    else:
        feats = [str(f) for f in (features or [])]
    if isinstance(shares, str):
        try:
            shares_list = json.loads(shares) if shares.strip() else []
        except Exception:
            shares_list = []
    else:
        shares_list = list(shares or [])
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
        storage = await _resolve_storage(cluster_id,
                        "rootdir" if target == "ct" else "images") or storage
    net0, ipconfig = _netcfg(ip, gateway)   # static IP (no DHCP on this LAN) or dhcp

    want_enrol = "enrol" in feats or "mesh" in feats
    if target == "ct":
        if img.get("type") != "lxc-template":
            return {"error": f"CT target needs an lxc-template image; '{image_id}' is {img.get('type')}"}
        tmpl = img.get("source_url", "")
        if ":" not in tmpl or "vztmpl" not in tmpl:
            return {"error": f"LXC template not downloaded yet for '{image_id}' — build it "
                             "first (foundry.image.import) so there's a real volid"}
        _unpriv = ("mesh" not in feats)
        _nesting = ("nesting=1,keyctl=1" if ("docker-swarm" in feats or "distributed-compute" in feats or "vera-worker" in feats or "mesh" in feats) else "")
        if (not _unpriv) or _nesting:
            # Proxmox forbids API tokens from creating PRIVILEGED CTs or setting the
            # `features` flag (nesting/keyctl) -- root@pam-only. Create as root over SSH.
            res = await _ct_create_ssh(cluster_id, node, tmpl, name or "", storage,
                                       cores, memory, disk, net0, _unpriv, _nesting)
        else:
            res = await _call("proxmox.lxc.create", cluster_id=cluster_id, node=node,
                              ostemplate=tmpl, hostname=name or "",
                              storage=storage, cores=cores, memory=memory, disk=disk,
                              net0=net0, unprivileged=True, features="",
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
            asyncio.create_task(_post_provision(cluster_id, node, vmid, "lxc", feats, fqdn, job_id, shares=shares_list))
    elif target == "docker":
        if img.get("type") != "docker":
            return {"error": f"Docker target needs a docker image; '{image_id}' is {img.get('type')}"}
        # file-* features mount inside the container -> need SYS_ADMIN; host network
        # so the container can reach Vera + the stack. host_id="" = default engine.
        _extra = []
        if any(_x in feats for _x in ("file-client", "file-server")):
            _extra += ["--cap-add", "SYS_ADMIN"]
        # a base-OS image (debian:12 etc.) exits immediately; keep it alive so features
        # can be applied via docker exec and it persists as a lightweight feature-host.
        # A service image provisioned without features keeps its own CMD.
        _keep = "tail -f /dev/null" if any(_x != "enrol" for _x in feats) else ""
        res = await _call("docker.run", host_id="", image=img.get("source_url", ""),
                          name=name or f"foundry-{job_id}", network="host",
                          command=_keep, extra_args=" ".join(_extra))
        step("run", res)
        cname = res.get("name") or name or f"foundry-{job_id}"
        if res.get("error") or not res.get("ok"):
            job["status"] = "error"
        else:
            job["status"] = "ok"
            job["container"] = cname
            step("post", {"status": "applying",
                          "note": "container-applicable features applying via docker exec -- watch events / jobs"})
            asyncio.create_task(_post_provision_docker(cname, feats, job_id, shares_list))
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
                                                    feats, fqdn, job_id, ip, shares=shares_list))
        else:
            # no cloud-init template yet -> auto-build it from the cloudimg (background);
            # the caller re-runs provision once foundry.image.import.status reports ready.
            _imp = await _call("foundry.image.import", image_id=image_id,
                               cluster_id=cluster_id, node=node, storage=storage)
            if _imp.get("ok"):
                step("create", {"status": "building_template",
                                "template_vmid": _imp.get("template_vmid"),
                                "note": ("building the cloud-init template for %s (vmid %s) now -- "
                                         "downloads + converts the image; re-run foundry.provision once "
                                         "foundry.image.import.status reports ready"
                                         % (image_id, _imp.get("template_vmid")))})
            else:
                step("create", {"status": "error",
                                "note": "could not start template build: %s" % _imp.get("error")})
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
    # render the SAME OS-agnostic feature bundles CT/VM use, so a PXE/physical
    # node self-enrols (mesh), becomes a Vera worker, hardens + mounts shares.
    _pf = prof.get("features") or []
    _pfctx = await _features_ctx()
    _fscripts = [_feature_script(_x, {} if _x == "hardening" else _pfctx)
                 for _x in ("hardening", "mesh", "file-client", "vera-worker") if _x in _pf]
    _fscripts = [x for x in _fscripts if x]
    return {"ok": True, **_render_boot(prof, cfg, img, cscripts, _fscripts)}


def _vera_host_ip() -> str:
    """This Vera host\'s LAN IP (for the ops-node registry ref + worker backend URLs)."""
    import os as _os, socket as _sock
    h = _os.getenv("VERA_ADVERTISE_HOST", "")
    if h:
        return h
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


def _ops_worker_env() -> str:
    """Backend env served at /ops/vera-worker-env for an ops-node Vera WORKER container.
    Points every store URL at this Vera host\'s LAN IP (a remote worker cannot reach our
    localhost), mirroring provision.worker native mode. Empty values skipped."""
    import os as _os, socket as _sock
    def _lan():
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]; s.close(); return ip
        except Exception:
            return "127.0.0.1"
    bh = _os.getenv("VERA_ADVERTISE_HOST", "") or _lan()
    def _rw(v):
        for h in ("host.docker.internal", "localhost", "127.0.0.1", "::1"):
            v = v.replace(h, bh)
        return v
    out = {"REDIS_URL": _rw(_os.getenv("REDIS_URL", "") or "redis://localhost:6379")}
    for k in ("POSTGRES_URL", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASS", "CHROMA_HOST",
              "CHROMA_PORT", "OLLAMA_BASE_URL", "OLLAMA_GPU_URL", "OLLAMA_CPU_A_URL",
              "OLLAMA_CPU_B_URL", "OLLAMA_EMBED_URL", "OLLAMA_MODEL", "VERA_COORD_REDIS_DB"):
        v = _os.getenv(k)
        if v:
            out[k] = _rw(v)
    out["ORCHESTRATOR_HOST"] = "0.0.0.0"
    out["FOUNDRY_VERA_IMAGE"] = bh + ":5000/vera:latest"
    out["EMBED_CAPS_ON_START"] = "0"
    return "".join("%s=%s\n" % (k, v) for k, v in out.items())


async def _features_ctx(shares=None) -> Dict:
    """Context for features_core.feature_script: LAN-reachable Vera URL, registry, worker
    image + backend env, and the mesh enrol token (minted just-in-time)."""
    ip = _vera_host_ip()
    ctx = {"vera_url": "https://%s:8999" % ip, "registry": "%s:5000" % ip,
           "vera_image": "%s:5000/vera:latest" % ip,
           "vera_worker_env": _ops_worker_env(), "shares": list(shares or [])}
    try:
        ctx["mesh_token"] = ((await _call("netsec.mesh.enroll_token")) or {}).get("enroll_token", "")
    except Exception:
        ctx["mesh_token"] = ""
    return ctx


def _load_ops_secrets() -> Dict:
    """Decrypt the off-repo sealed ops-node secrets (~/.vera-ops-secrets/
    ops-secrets.env.enc with ops.key, Fernet) into a dict, so the ops image can bake
    WiFi + Twingate. Returns {} if the sealed file/key are absent or unreadable, so a
    Foundry deploy without secrets still works. Key + ciphertext live OUTSIDE the repo
    and are never committed. Override the folder with VERA_OPS_SECRETS_DIR."""
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return {}
    import os as _os
    from pathlib import Path as _Path
    base = _Path(_os.environ.get("VERA_OPS_SECRETS_DIR")
                 or _os.path.expanduser("~/.vera-ops-secrets"))
    keyf, encf = base / "ops.key", base / "ops-secrets.env.enc"
    if not (keyf.exists() and encf.exists()):
        return {}
    try:
        text = Fernet(keyf.read_bytes().strip()).decrypt(encf.read_bytes()).decode("utf-8")
        return parse_ops_secrets(text)
    except Exception:
        return {}


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


def _pxe_server_setup_script(server_ip, iface, uplink, subnet, conf_b64, menu_b64, apkovl_b64, tui_b64="", sdwrite_b64="", desk_apk_b64="", worker_env_b64="") -> str:
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
echo {worker_env_b64} | base64 -d > /srv/foundry/http/ops/vera-worker-env 2>/dev/null
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
    _secrets = _load_ops_secrets()
    _reg = _vera_host_ip() + ":5000"
    try:
        _mtok = ((await _call("netsec.mesh.enroll_token")) or {}).get("enroll_token", "")
    except Exception:
        _mtok = ""
    _vurl = "https://" + _vera_host_ip() + ":8999"
    ops_files = pxe_ops_apkovl_files(server_ip, secrets=_secrets, registry=_reg, mesh_token=_mtok, vera_url=_vurl)
    apk_b64 = _apkovl_tar_b64(ops_files)
    desk_apk_b64 = _apkovl_tar_b64(pxe_desktop_apkovl_files(server_ip, secrets=_secrets, registry=_reg, mesh_token=_mtok, vera_url=_vurl))
    _b = lambda s: base64.b64encode(s.encode()).decode()
    tui_b64 = _b(ops_files["usr/local/bin/foundry-tui"])
    sdwrite_b64 = _b(ops_files["usr/local/bin/foundry-sdwrite"])
    script = _pxe_server_setup_script(server_ip, iface, uplink, subnet, _b(conf), _b(menu), apk_b64, tui_b64, sdwrite_b64, desk_apk_b64, worker_env_b64=_b(_ops_worker_env()))
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


# ---------------------------------------------------------------------------
# SD-card provisioning — the third target
# ---------------------------------------------------------------------------
# PXE covers machines that can netboot and CT/VM covers Proxmox guests, but a
# Raspberry Pi with a card in a reader was previously unreachable from Foundry.
# These capabilities close that: inspect a card, plan the change, apply it.
#
# The default mode is ADAPT, not flash. Cards are rarely blank — they usually
# hold someone's earlier project — so wiping is opt-in and the plan is always
# reviewable before it runs.

K_NODES = "vera:foundry:nodes"       # nodes that have checked in
K_FRAMES = "vera:foundry:frames"     # what each display node should show


def _sd_script(body: str) -> str:
    """Wrap card-side shell in guards. Every mount is read-only unless the
    caller has explicitly asked to write, and we always unmount on exit so a
    failed run never leaves the card held open."""
    return ("set -u\n"
            "MB=/run/foundry-sd-boot; MR=/run/foundry-sd-root\n"
            "cleanup(){ umount \"$MB\" 2>/dev/null; umount \"$MR\" 2>/dev/null; }\n"
            "trap cleanup EXIT\n"
            "mkdir -p \"$MB\" \"$MR\"\n" + body)


@capability(
    "foundry.sdcard.detect",
    http_method="POST", http_path="/foundry/sdcard/detect", http_tags=["foundry"],
    memory="off", silent=True,
    description="List removable block devices on a Proxmox node that look like a "
                "Raspberry Pi card (a FAT boot partition + a Linux rootfs). Read-only. "
                "Inputs: cluster_id (str!). Output: {cards:[{dev,size,"
                "boot,root,label}]}.",
)
async def cap_sdcard_detect(cluster_id: str = "", trace_id=None) -> Dict:
    cmd = (
        "for d in /sys/block/*; do n=$(basename $d); "
        "case \"$n\" in loop*|zram*|zd*|dm-*|nvme*|sr*) continue;; esac; "
        "[ \"$(cat $d/removable 2>/dev/null)\" = 1 ] || "
        "  { echo \"$n\" | grep -q '^mmcblk' || continue; }; "
        "sz=$(( $(cat $d/size 2>/dev/null || echo 0) / 2097152 )); "
        "b=''; r=''; "
        "for p in /dev/${n}*[0-9]; do [ -b \"$p\" ] || continue; "
        "  t=$(blkid -o value -s TYPE $p 2>/dev/null); "
        "  case \"$t\" in vfat) b=$p;; ext4|ext3|btrfs) r=$p;; esac; done; "
        "[ -n \"$b\" ] && [ -n \"$r\" ] && "
        "  echo \"CARD|/dev/$n|${sz}|$b|$r|$(cat $d/device/model 2>/dev/null | xargs)\"; "
        "done")
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command=cmd, timeout=45)
    if res.get("error"):
        return {"error": res["error"], "cards": []}
    cards = []
    for line in (res.get("stdout", "") or "").splitlines():
        if not line.startswith("CARD|"):
            continue
        p = line.split("|")
        if len(p) >= 6:
            cards.append({"dev": p[1], "size_gb": p[2], "boot": p[3],
                          "root": p[4], "model": p[5]})
    return {"cards": cards, "count": len(cards)}


@capability(
    "foundry.sdcard.inspect",
    http_method="POST", http_path="/foundry/sdcard/inspect", http_tags=["foundry"],
    memory="off",
    description="Mount a card READ-ONLY and report what is on it: OS, free space, "
                "enabled services, existing config.txt, and which inherited services "
                "would disrupt a live LAN if it booted. Writes nothing. Inputs: "
                "cluster_id (str!), boot (str! e.g. /dev/sdk1), root (str!). "
                "Output: {os, free, config_txt, enabled:[...], hazards:[...]}.",
)
async def cap_sdcard_inspect(cluster_id: str = "", boot: str = "",
                             root: str = "", trace_id=None) -> Dict:
    if not (boot and root):
        return {"error": "boot and root partition devices are required"}
    from Vera.vera.foundry.sdcard_core import hazard_services
    body = (
        f"mount -o ro {boot} \"$MB\" 2>/dev/null || {{ echo 'ERR boot mount'; exit 1; }}\n"
        f"mount -o ro {root} \"$MR\" 2>/dev/null || {{ echo 'ERR root mount'; exit 1; }}\n"
        "echo '---OS---'; grep -h PRETTY_NAME \"$MR/etc/os-release\" 2>/dev/null\n"
        "echo '---FREE---'; df -h \"$MR\" | tail -1\n"
        "echo '---HOSTNAME---'; cat \"$MR/etc/hostname\" 2>/dev/null\n"
        "echo '---ENABLED---'\n"
        "ls \"$MR/etc/systemd/system/multi-user.target.wants/\" 2>/dev/null\n"
        "echo '---CONFIG---'\n"
        "cat \"$MB/config.txt\" 2>/dev/null || cat \"$MB/firmware/config.txt\" 2>/dev/null\n"
        "echo '---SSH---'; [ -e \"$MB/ssh\" ] && echo yes || echo no\n"
        "echo '---END---'\n")
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command=_sd_script(body), timeout=90)
    if res.get("error"):
        return {"error": res["error"]}
    out = res.get("stdout", "") or ""
    if "ERR boot mount" in out or "ERR root mount" in out:
        return {"error": "could not mount the card read-only; is it in use?"}

    def _sec(name: str) -> str:
        try:
            return out.split(f"---{name}---", 1)[1].split("---", 1)[0].strip("\n")
        except Exception:
            return ""

    enabled = [u.strip() for u in _sec("ENABLED").splitlines() if u.strip()]
    return {
        "ok": True,
        "os": _sec("OS").replace("PRETTY_NAME=", "").strip('"'),
        "free": _sec("FREE"),
        "hostname": _sec("HOSTNAME").strip(),
        "ssh_enabled": _sec("SSH").strip() == "yes",
        "config_txt": _sec("CONFIG"),
        "enabled": enabled,
        "hazards": hazard_services(enabled),
    }


@capability(
    "foundry.sdcard.plan",
    http_method="POST", http_path="/foundry/sdcard/plan", http_tags=["foundry"],
    memory="off",
    description="Dry-run: show exactly which files would be written to a card and "
                "which inherited services would be masked, without touching it. "
                "Inputs: config_txt (str — from inspect), enabled (list), display "
                "(xpt2046|none), panel (str), rotate (int), vera_url, node_label. "
                "Output: {boot:{...}, root:[...], mask:[...], notes:[...]}.",
    schema=enum_schema(display=["xpt2046", "none"],
                       panel=["ili9341", "ili9486", "ili9488", "st7735r", "hx8357d"]),
)
async def cap_sdcard_plan(config_txt: str = "", enabled: List[str] = None,
                          display: str = "xpt2046", panel: str = "ili9341",
                          rotate: int = 270, vera_url: str = "",
                          node_label: str = "", role: str = "frame",
                          mask_hazards: bool = True, trace_id=None) -> Dict:
    from Vera.vera.foundry.sdcard_core import plan_adapt
    plan = plan_adapt(config_txt, enabled or [], display=display,
                      display_opts={"panel": panel, "rotate": int(rotate)},
                      vera_url=vera_url or _vera_url(), node_label=node_label,
                      role=role, mask_hazards=mask_hazards)
    # Return the config.txt in full (it is the reviewable part) but only the
    # names of the agent files — they are long and generated.
    return {"mode": plan["mode"], "config_txt": plan["boot"].get("config.txt", ""),
            "boot_files": sorted(plan["boot"]), "root_files": sorted(plan["root"]),
            "mask": plan["mask"], "notes": plan["notes"]}


def _vera_url() -> str:
    """The LAN-reachable Vera base URL a provisioned node should call home on —
    same derivation the feature bundles use (`_features_ctx`)."""
    ip = _vera_host_ip()
    return "https://%s:8999" % ip if ip else ""


@capability(
    "foundry.sdcard.provision",
    http_method="POST", http_path="/foundry/sdcard/provision", http_tags=["foundry"],
    memory="on",
    description="Apply a provisioning plan to a card: merge the TFT overlay into "
                "config.txt, enable SSH, install the first-boot join + display/button "
                "agents, and mask inherited services that would disrupt the LAN. "
                "ADAPTS in place — existing data is preserved. Set confirm=true to "
                "write. Inputs: cluster_id (str!), boot (str!), root (str!), "
                "display, panel, rotate, node_label, wifi_ssid, wifi_psk, confirm "
                "(bool=false). Output: {ok, written:[...], masked:[...]}.",
    schema=enum_schema(display=["xpt2046", "none"],
                       panel=["ili9341", "ili9486", "ili9488", "st7735r", "hx8357d"]),
)
async def cap_sdcard_provision(cluster_id: str = "", boot: str = "",
                               root: str = "", display: str = "xpt2046",
                               panel: str = "ili9341", rotate: int = 270,
                               node_label: str = "", role: str = "frame",
                               wifi_ssid: str = "", wifi_psk: str = "",
                               mask_hazards: bool = True, confirm: bool = False,
                               trace_id=None) -> Dict:
    if not (boot and root):
        return {"error": "boot and root partition devices are required"}
    if not confirm:
        return {"error": "refusing to write without confirm=true",
                "hint": "run foundry.sdcard.plan first to review the change"}
    from Vera.vera.foundry.sdcard_core import plan_adapt

    # Read the card's current state first — the plan must merge into the real
    # config.txt, not a blank one, or we silently drop the settings that make
    # this particular board boot.
    cur = await cap_sdcard_inspect(cluster_id=cluster_id,
                                   boot=boot, root=root)
    if cur.get("error"):
        return cur

    label = node_label or (cur.get("hostname") or "rpi-node")
    token = uuid.uuid4().hex
    wifi = [(wifi_ssid, wifi_psk)] if wifi_ssid else None
    plan = plan_adapt(cur.get("config_txt", ""), cur.get("enabled", []),
                      display=display,
                      display_opts={"panel": panel, "rotate": int(rotate)},
                      vera_url=_vera_url(), enroll_token=token, node_label=label,
                      role=role, wifi=wifi, mask_hazards=mask_hazards)

    # Ship every file as base64 so shell quoting can never corrupt a payload —
    # these include python sources with quotes, braces and newlines.
    writes = []
    for part, files in (("$MB", plan["boot"]), ("$MR", plan["root"])):
        for rel, content in files.items():
            b64 = base64.b64encode(content.encode("utf-8")).decode()
            writes.append(
                f"mkdir -p \"$(dirname {part}/{rel})\" 2>/dev/null; "
                f"printf %s '{b64}' | base64 -d > \"{part}/{rel}\" && "
                f"echo 'WROTE {rel}'")
    chmods = "; ".join(
        f"chmod +x \"$MR/{p}\"" for p in plan["root"] if p.endswith((".sh", ".py")))
    # Enable the first-boot unit the way systemd would have: the wants/ symlink.
    # `systemctl enable` cannot run against an unbooted rootfs.
    enable = ("mkdir -p \"$MR/etc/systemd/system/multi-user.target.wants\"; "
              "ln -sf /etc/systemd/system/foundry-firstboot.service "
              "\"$MR/etc/systemd/system/multi-user.target.wants/"
              "foundry-firstboot.service\" && echo 'ENABLED firstboot'")
    body = (
        f"mount {boot} \"$MB\" 2>/dev/null || {{ echo 'ERR boot mount'; exit 1; }}\n"
        f"mount {root} \"$MR\" 2>/dev/null || {{ echo 'ERR root mount'; exit 1; }}\n"
        # Keep a copy of the file we are about to edit. It is the one file whose
        # loss stops the board booting at all.
        "cp -n \"$MB/config.txt\" \"$MB/config.txt.foundry-backup\" 2>/dev/null || true\n"
        + "\n".join(writes) + "\n" + chmods + "\n" + enable + "\nsync\necho DONE\n")

    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command=_sd_script(body), timeout=180)
    if res.get("error"):
        return {"error": res["error"]}
    out = res.get("stdout", "") or ""
    if "ERR boot mount" in out or "ERR root mount" in out:
        return {"error": "could not mount the card read-write; is it in use?"}
    written = [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("WROTE ")]

    r = _redis()
    if r:
        await r.hset(K_NODES, label, json.dumps(
            {"label": label, "role": role, "token": token, "kind": "rpi",
             "provisioned": time.time(), "panel": panel, "status": "awaiting-first-boot"}))
    await emit_event({"type": "foundry.sdcard.provisioned", "label": label,
                      "files": len(written)})
    return {"ok": "DONE" in out, "label": label, "written": written,
            "masked": [h["name"] for h in plan["mask"]],
            "notes": plan["notes"] + [
                "config.txt backed up on the card as config.txt.foundry-backup",
                "First boot masks the inherited services, then checks in to Vera."]}


@capability(
    "foundry.node.checkin",
    http_method="POST", http_path="/foundry/node/checkin", http_tags=["foundry"],
    memory="off", silent=True,
    description="Called BY a provisioned node on first boot to register itself. "
                "Inputs: label, role, ip, mac, token, kind. Output: {ok}.",
)
async def cap_node_checkin(label: str = "", role: str = "frame", ip: str = "",
                           mac: str = "", token: str = "", kind: str = "rpi",
                           trace_id=None) -> Dict:
    r = _redis()
    if not r or not label:
        return {"error": "label required"}
    raw = await r.hget(K_NODES, label)
    rec = json.loads(raw) if raw else {"label": label}
    # The token proves this is the node we provisioned rather than anything else
    # that found the endpoint. Unknown nodes are recorded, not trusted.
    rec.update({"role": role, "ip": ip, "mac": mac, "kind": kind,
                "last_seen": time.time(),
                "status": "online" if token and token == rec.get("token") else "unverified"})
    await r.hset(K_NODES, label, json.dumps(rec))
    await emit_event({"type": "foundry.node.checkin", "label": label, "ip": ip})
    return {"ok": True, "label": label, "status": rec["status"]}


@capability(
    "foundry.node.list",
    http_method="GET", http_path="/foundry/node/list", http_tags=["foundry"],
    memory="off", silent=True,
    description="List provisioned nodes (photoframes, macro pads, status displays) "
                "and when each last checked in. Output: {nodes:[...]}.",
)
async def cap_node_list(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"nodes": []}
    rows = await r.hgetall(K_NODES) or {}
    nodes = []
    for v in rows.values():
        try:
            rec = json.loads(v)
        except Exception:
            continue
        rec.pop("token", None)          # never hand the enrolment token back out
        seen = rec.get("last_seen", 0)
        rec["stale"] = bool(seen) and (time.time() - seen > 300)
        nodes.append(rec)
    nodes.sort(key=lambda e: e.get("label", ""))
    return {"nodes": nodes, "count": len(nodes)}


@capability(
    "foundry.node.frame",
    http_method="GET", http_path="/foundry/node/frame", http_tags=["foundry"],
    memory="off", silent=True,
    description="Called BY a display node to ask what it should show. Returns "
                "{mode:image|text|blank, url|lines}. Input: label (str!).",
)
async def cap_node_frame(label: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not label:
        return {"mode": "blank"}
    raw = await r.hget(K_FRAMES, label)
    if not raw:
        return {"mode": "text", "lines": [label, "idle", "no content assigned"]}
    try:
        return json.loads(raw)
    except Exception:
        return {"mode": "blank"}


@capability(
    "foundry.node.frame.set",
    http_method="POST", http_path="/foundry/node/frame/set", http_tags=["foundry"],
    memory="on",
    description="Set what a display node shows. Inputs: label (str!), mode "
                "(image|text|blank), url (str — for image), lines (list — for text). "
                "Use label='*' to set every node at once. Output: {ok, applied:[...]}.",
    schema=enum_schema(mode=["image", "text", "blank"]),
)
async def cap_node_frame_set(label: str = "", mode: str = "blank", url: str = "",
                             lines: List[str] = None, trace_id=None) -> Dict:
    r = _redis()
    if not r or not label:
        return {"error": "label required"}
    spec = {"mode": mode}
    if mode == "image":
        if not url:
            return {"error": "url required for mode=image"}
        spec["url"] = url
    elif mode == "text":
        spec["lines"] = lines or []
    targets = [label]
    if label == "*":
        targets = [json.loads(v).get("label") for v in
                   (await r.hgetall(K_NODES) or {}).values()]
        targets = [t for t in targets if t]
    for t in targets:
        await r.hset(K_FRAMES, t, json.dumps(spec))
    return {"ok": True, "applied": targets, "spec": spec}


@capability(
    "foundry.node.action",
    http_method="POST", http_path="/foundry/node/action", http_tags=["foundry"],
    memory="off", silent=True,
    description="Called BY a node when one of its buttons is pressed. Emits a "
                "foundry.node.action event that any Vera automation can react to, "
                "which is what makes these nodes macro pads. Inputs: label, action.",
)
async def cap_node_action(label: str = "", action: str = "", trace_id=None) -> Dict:
    await emit_event({"type": "foundry.node.action", "label": label,
                      "action": action, "ts": time.time()})
    return {"ok": True}


# ---------------------------------------------------------------------------
# VM import / export, and salvaging a machine before it is wiped
# ---------------------------------------------------------------------------
# Two jobs that turned out to be the same shape: point Foundry at a disk that
# lives somewhere else, and decide what to do with what is on it.
#
# The wipe is deliberately gated behind a *verified* backup. A job that exited
# zero is not proof the bytes are readable, and a wipe cannot be undone.

K_SALVAGE = "vera:foundry:salvage"     # per-device salvage records


def _sal_dir(label: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (label or "device"))
    return "/var/lib/vz/dump/salvage-%s" % (safe or "device")


@capability(
    "foundry.vm.import",
    http_method="POST", http_path="/foundry/vm/import", http_tags=["foundry"],
    memory="on",
    description="Import a disk image (vdi/vmdk/vhd/vhdx/qcow2/raw) as a Proxmox "
                "VM: create the shell, importdisk, ATTACH it and set the boot "
                "order — an import that stops after importdisk leaves a VM with "
                "no disk. Inputs: cluster_id (str!), source (str! path on the "
                "node), storage (str!), vmid (int, 0 = next free), name, memory "
                "(int=2048), cores (int=2), bridge (str=vmbr0), os_hint (str — "
                "drives BIOS/bus choice), confirm (bool=false). Output: {ok, "
                "vmid, steps, notes}.",
)
async def cap_vm_import(cluster_id: str = "", source: str = "", storage: str = "",
                        vmid: int = 0, name: str = "", memory: int = 2048,
                        cores: int = 2, bridge: str = "vmbr0",
                        os_hint: str = "", confirm: bool = False,
                        trace_id=None) -> Dict:
    from Vera.vera.foundry.vmport_core import import_plan
    if not (source and storage):
        return {"error": "source and storage are required"}

    # Ask the node about the file before planning: the virtual size is what
    # Proxmox will allocate, and it is routinely 20x the file size.
    probe = await _call("proxmox.node.exec", cluster_id=cluster_id, timeout=60,
                        command="qemu-img info --output=json %s 2>/dev/null; "
                                "stat -c %%s %s 2>/dev/null"
                                % (shlex.quote(source), shlex.quote(source)))
    virtual = actual = 0
    fmt_hint = ""
    out = (probe.get("stdout") or "")
    try:
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
        virtual = int(j.get("virtual-size") or 0)
        actual = int(j.get("actual-size") or 0)
        fmt_hint = j.get("format") or ""
    except Exception:
        pass
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    if tail.isdigit():
        actual = actual or int(tail)
    if not virtual and not actual:
        return {"error": "could not read %s on the node — is the path right?" % source}

    if not vmid:
        nid = await _call("proxmox.nextid", cluster_id=cluster_id)
        vmid = int(nid.get("vmid") or nid.get("nextid") or 0)
        if not vmid:
            return {"error": "could not allocate a vmid"}

    plan = import_plan(vmid, source, storage, name=name, memory=memory,
                       cores=cores, bridge=bridge, os_hint=os_hint,
                       virtual_bytes=virtual, actual_bytes=actual,
                       description="Imported by Vera Foundry from %s" % source)
    if plan.get("error"):
        return plan
    plan["source_format"] = fmt_hint
    if not confirm:
        plan["dry_run"] = True
        plan["hint"] = "re-run with confirm=true to execute these steps"
        return plan

    results = []
    for step in plan["steps"]:
        cmd = " ".join(shlex.quote(c) for c in step["cmd"])
        res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                          command=cmd, timeout=7200 if step.get("slow") else 120)
        ok = not res.get("error") and int(res.get("exit_code", 0) or 0) == 0
        results.append({"stage": step["stage"], "ok": ok,
                        "out": (res.get("stdout") or "")[-300:],
                        "err": (res.get("stderr") or res.get("error") or "")[-300:]})
        if not ok:
            # Stop rather than press on: a failed create makes every later
            # step meaningless and the errors misleading.
            break
    await emit_event({"type": "foundry.vm.imported", "vmid": vmid,
                      "ok": all(r["ok"] for r in results)})
    return {"ok": all(r["ok"] for r in results), "vmid": vmid,
            "results": results, "notes": plan["notes"], "guest": plan["guest"]}


@capability(
    "foundry.vm.export",
    http_method="POST", http_path="/foundry/vm/export", http_tags=["foundry"],
    memory="on",
    description="Export a stopped VM's disk to a portable image. qcow2 keeps "
                "sparseness; raw does not. Inputs: cluster_id (str!), vmid "
                "(int!), disk (str — storage ref, default scsi0's), out_dir "
                "(str=/var/lib/vz/dump), format (qcow2|vmdk|vdi|vhdx|raw), "
                "confirm (bool=false). Output: {ok, target, steps}.",
    schema=enum_schema(format=["qcow2", "vmdk", "vdi", "vhdx", "raw"]),
)
async def cap_vm_export(cluster_id: str = "", vmid: int = 0, disk: str = "",
                        out_dir: str = "/var/lib/vz/dump", format: str = "qcow2",
                        confirm: bool = False, trace_id=None) -> Dict:
    from Vera.vera.foundry.vmport_core import export_plan
    if not vmid:
        return {"error": "vmid is required"}

    cfg = await _call("proxmox.node.exec", cluster_id=cluster_id, timeout=60,
                      command="qm config %d" % vmid)
    conf = cfg.get("stdout") or ""
    if not disk:
        m = re.search(r"^scsi0:\s*([^,\s]+)", conf, re.M) or \
            re.search(r"^(?:sata0|virtio0|ide0):\s*([^,\s]+)", conf, re.M)
        disk = m.group(1) if m else ""
    if not disk:
        return {"error": "could not find a disk on VM %d" % vmid, "config": conf[:400]}

    running = await _call("proxmox.node.exec", cluster_id=cluster_id, timeout=60,
                          command="qm status %d" % vmid)
    if "running" in (running.get("stdout") or ""):
        return {"error": "VM %d is running — exporting a live disk copies a "
                         "torn filesystem. Stop it first." % vmid}

    plan = export_plan(vmid, disk, out_dir, fmt=format)
    if plan.get("error") or not confirm:
        plan.setdefault("dry_run", not confirm)
        return plan

    path = await _call("proxmox.node.exec", cluster_id=cluster_id, timeout=60,
                       command="pvesm path %s" % shlex.quote(disk))
    src = (path.get("stdout") or "").strip().splitlines()[-1:] or [""]
    if not src[0].startswith("/"):
        return {"error": "could not resolve %s to a path" % disk}

    cmd = "qemu-img convert -O %s %s %s" % (
        format, shlex.quote(src[0]), shlex.quote(plan["target"]))
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command=cmd, timeout=14400)
    ok = not res.get("error")
    chk = await _call("proxmox.node.exec", cluster_id=cluster_id, timeout=600,
                      command="qemu-img check %s 2>&1 | tail -3"
                              % shlex.quote(plan["target"]))
    await emit_event({"type": "foundry.vm.exported", "vmid": vmid, "ok": ok})
    return {"ok": ok, "target": plan["target"], "notes": plan["notes"],
            "verify": (chk.get("stdout") or "")[-400:],
            "error": res.get("error") or ""}


@capability(
    "foundry.salvage.inspect",
    http_method="POST", http_path="/foundry/salvage/inspect", http_tags=["foundry"],
    memory="off",
    description="Mount a plugged-in disk READ-ONLY and report what is on it: "
                "which OS, which users, how much data each harvest profile "
                "would take. Writes nothing. Inputs: cluster_id (str!), device "
                "(str! e.g. /dev/sdl2). Output: {os, users, sizes, profiles}.",
)
async def cap_salvage_inspect(cluster_id: str = "", device: str = "",
                              trace_id=None) -> Dict:
    from Vera.vera.foundry.salvage_core import detect_os, PROFILE_ORDER
    if not device:
        return {"error": "device is required"}
    mp = "/run/foundry-salvage"
    script = (
        "set -u; mkdir -p %s; umount %s 2>/dev/null; "
        "mount -o ro %s %s 2>/dev/null || { echo ERR_MOUNT; exit 1; }; "
        "echo '---MARKERS---'; "
        "for f in etc/os-release etc/fstab etc/passwd Windows/explorer.exe "
        "Windows/System32/config/SYSTEM pagefile.sys "
        "System/Library/CoreServices/SystemVersion.plist; do "
        "  [ -e %s/$f ] && echo $f; done; "
        "echo '---RELEASE---'; cat %s/etc/os-release 2>/dev/null | head -3; "
        "echo '---USERS---'; ls %s/home 2>/dev/null; ls %s/Users 2>/dev/null; "
        "echo '---SIZES---'; du -sh %s/home/* %s/Users/* 2>/dev/null | head -20; "
        "echo '---FREE---'; df -h %s | tail -1; "
        "umount %s 2>/dev/null; echo '---END---'"
        % (mp, mp, shlex.quote(device), mp, mp, mp, mp, mp, mp, mp, mp, mp))
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command=script, timeout=180)
    out = res.get("stdout") or ""
    if "ERR_MOUNT" in out:
        return {"error": "could not mount %s read-only" % device}

    def sec(name):
        try:
            return out.split("---%s---" % name, 1)[1].split("---", 1)[0].strip("\n")
        except Exception:
            return ""

    markers = [l.strip() for l in sec("MARKERS").splitlines() if l.strip()]
    det = detect_os(markers)
    users = [u.strip() for u in sec("USERS").splitlines() if u.strip()]
    return {"ok": True, "device": device, **det, "users": users,
            "release": sec("RELEASE"), "sizes": sec("SIZES"),
            "free": sec("FREE"), "profiles_available": PROFILE_ORDER}


@capability(
    "foundry.salvage.plan",
    http_method="POST", http_path="/foundry/salvage/plan", http_tags=["foundry"],
    memory="off",
    description="Dry run: exactly which paths would be harvested off a device "
                "and which skipped, without touching it. Inputs: os (linux|"
                "windows|macos), profiles (list — documents/code/editor/"
                "dotfiles/credentials/browser/services/databases). Output: "
                "{includes, excludes, profiles}.",
    schema=enum_schema(os=["linux", "windows", "macos"]),
)
async def cap_salvage_plan(os: str = "linux", profiles: List[str] = None,
                           trace_id=None) -> Dict:
    from Vera.vera.foundry.salvage_core import harvest_plan
    return harvest_plan(os, profiles)


@capability(
    "foundry.salvage.run",
    http_method="POST", http_path="/foundry/salvage/run", http_tags=["foundry"],
    memory="on",
    description="Back a device up before it is wiped. Takes a file-level "
                "HARVEST (the part you will actually browse) and optionally a "
                "full IMAGE (the safety net), then VERIFIES both by reading "
                "them back. Inputs: cluster_id (str!), device (str! partition "
                "for the harvest), label (str!), os (linux|windows|macos), "
                "profiles (list), image_device (str — whole disk, e.g. /dev/sdl, "
                "for the full image), confirm (bool=false). Output: {ok, "
                "artifacts:[{artifact,exists,verified,bytes}]}.",
    schema=enum_schema(os=["linux", "windows", "macos"]),
)
async def cap_salvage_run(cluster_id: str = "", device: str = "", label: str = "",
                          os: str = "linux", profiles: List[str] = None,
                          image_device: str = "", confirm: bool = False,
                          trace_id=None) -> Dict:
    from Vera.vera.foundry.salvage_core import harvest_plan
    if not (device and label):
        return {"error": "device and label are required"}
    plan = harvest_plan(os, profiles)
    if plan.get("error"):
        return plan
    dest = _sal_dir(label)
    if not confirm:
        return {"dry_run": True, "dest": dest, "includes": plan["includes"],
                "excludes": plan["excludes"],
                "hint": "re-run with confirm=true to actually copy"}

    mp = "/run/foundry-salvage"
    inc = " ".join("--include=%s" % shlex.quote(p) for p in plan["includes"])
    exc = " ".join("--exclude=%s" % shlex.quote(p) for p in plan["excludes"])
    # tar over a find selection rather than rsync: no assumption that rsync is
    # installed on a hypervisor, and the exclusion list is long.
    harvest = "%s/harvest.tar.gz" % dest
    script = (
        "set -u; mkdir -p %s %s; umount %s 2>/dev/null; "
        "mount -o ro %s %s || { echo ERR_MOUNT; exit 1; }; "
        "cd %s && tar -czf %s %s %s . 2>/dev/null; echo TAR_RC=$?; "
        "cd /; umount %s 2>/dev/null; "
        "ls -l %s 2>/dev/null; "
        "echo '---VERIFY---'; tar -tzf %s >/dev/null 2>&1 && echo HARVEST_OK || echo HARVEST_BAD"
        % (dest, mp, mp, shlex.quote(device), mp, mp,
           shlex.quote(harvest), exc, inc, mp, shlex.quote(harvest),
           shlex.quote(harvest)))
    res = await _call("proxmox.node.exec", cluster_id=cluster_id,
                      command=script, timeout=14400)
    out = res.get("stdout") or ""
    artifacts = [{
        "artifact": harvest, "kind": "harvest",
        "exists": "harvest.tar.gz" in out,
        "verified": "HARVEST_OK" in out,
    }]

    if image_device:
        img = "%s/disk.img.gz" % dest
        iscript = (
            "set -u; sfdisk -d %s > %s/partition-table.sfdisk 2>/dev/null; "
            "dd if=%s bs=4M 2>/dev/null | gzip -1 > %s; echo DD_RC=$?; "
            "echo '---VERIFY---'; gzip -t %s && echo IMAGE_OK || echo IMAGE_BAD"
            % (shlex.quote(image_device), dest, shlex.quote(image_device),
               shlex.quote(img), shlex.quote(img)))
        ires = await _call("proxmox.node.exec", cluster_id=cluster_id,
                           command=iscript, timeout=28800)
        iout = ires.get("stdout") or ""
        artifacts.append({"artifact": img, "kind": "image",
                          "exists": "DD_RC=0" in iout,
                          "verified": "IMAGE_OK" in iout})

    r = _redis()
    if r:
        await r.hset(K_SALVAGE, label, json.dumps(
            {"label": label, "device": device, "dest": dest,
             "artifacts": artifacts, "at": time.time()}))
    await emit_event({"type": "foundry.salvage.done", "label": label,
                      "verified": all(a["verified"] for a in artifacts)})
    return {"ok": all(a["verified"] for a in artifacts), "dest": dest,
            "artifacts": artifacts,
            "note": "a wipe is only authorised once every artifact is verified"}


@capability(
    "foundry.salvage.list",
    http_method="GET", http_path="/foundry/salvage/list", http_tags=["foundry"],
    memory="off", silent=True,
    description="Devices salvaged so far and whether each backup was verified. "
                "Output: {salvages:[...]}.",
)
async def cap_salvage_list(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"salvages": []}
    rows = await r.hgetall(K_SALVAGE) or {}
    out = []
    for v in rows.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    out.sort(key=lambda e: -(e.get("at") or 0))
    return {"salvages": out, "count": len(out)}


@capability(
    "foundry.salvage.wipe_check",
    http_method="POST", http_path="/foundry/salvage/wipe_check", http_tags=["foundry"],
    memory="off",
    description="Is it safe to wipe this device yet? Answers only from what has "
                "actually been backed up AND read back — a finished job is not a "
                "verified backup. Inputs: label (str!), force (bool=false). "
                "Output: {allowed, reason}.",
)
async def cap_salvage_wipe_check(label: str = "", force: bool = False,
                                 trace_id=None) -> Dict:
    from Vera.vera.foundry.salvage_core import wipe_authorisation
    r = _redis()
    rec = None
    if r and label:
        raw = await r.hget(K_SALVAGE, label)
        rec = json.loads(raw) if raw else None
    if not rec:
        return {"allowed": False,
                "reason": "no salvage record for %r — nothing has been backed "
                          "up from it" % label}
    verdict = wipe_authorisation(rec.get("artifacts") or [], force=force)
    verdict["label"] = label
    verdict["dest"] = rec.get("dest")
    return verdict


# ---------------------------------------------------------------------------
# Security baselines: apply, verify, and watch for drift
# ---------------------------------------------------------------------------
# Hardening used to be a script that ran and said "done". Nothing recorded
# which hosts it had touched, nothing re-checked whether the settings survived,
# and the definition of "hardened" existed only inside the script.
#
# The registry below is the missing half: every apply and every verify is
# recorded per host, so "is this estate actually hardened" is a question with
# an answer rather than an assumption.

K_SECHOSTS = "vera:foundry:security:hosts"    # per-host state + history
K_SECFIM = "vera:foundry:security:fim"        # per-host file manifests


async def _sec_exec(cluster_id: str, target: str, script: str,
                    timeout: int = 300) -> Dict:
    """Run a generated script on a host.

    `target` is "node" for the Proxmox host itself, or "ct:<vmid>" / "vm:<vmid>"
    for a guest, so one code path covers the hypervisor and the things running
    on it.
    """
    b64 = base64.b64encode(script.encode("utf-8")).decode()
    runner = "printf %s '" + b64 + "' | base64 -d | sh"
    if target.startswith(("ct:", "lxc:")):
        vmid = target.split(":", 1)[1]
        cmd = "pct exec %s -- sh -c %s" % (vmid, shlex.quote(runner))
    elif target.startswith(("vm:", "qemu:")):
        vmid = target.split(":", 1)[1]
        return await _call("proxmox.guest.exec", cluster_id=cluster_id,
                           vmid=int(vmid), guest_type="qemu",
                           command=runner, timeout=timeout)
    else:
        cmd = runner
    return await _call("proxmox.node.exec", cluster_id=cluster_id,
                       command=cmd, timeout=timeout)


@capability(
    "foundry.security.standards",
    http_method="GET", http_path="/foundry/security/standards", http_tags=["foundry"],
    memory="off", silent=True,
    description="The security standard Foundry applies, in full: every control, "
                "why it matters, which CIS section it maps to, its severity, and "
                "how to remediate it — plus the named profiles (baseline, "
                "exposed, minimal). This is the answer to 'what does hardened "
                "mean here'. Output: {profiles, controls, severities}.",
)
async def cap_security_standards(trace_id=None) -> Dict:
    from Vera.vera.foundry.security_core import catalogue
    return catalogue()


@capability(
    "foundry.security.apply",
    http_method="POST", http_path="/foundry/security/apply", http_tags=["foundry"],
    memory="on",
    description="Apply a security profile to a host and record it in the "
                "registry. Guard controls run first: a host with no working SSH "
                "key will NOT have password auth disabled — it aborts instead of "
                "stranding the host. Inputs: cluster_id (str!), target (str! "
                "'node' | 'ct:<vmid>' | 'vm:<vmid>'), profile (baseline|exposed|"
                "minimal), dry_run (bool=false). Output: {ok, results, aborted}.",
    schema=enum_schema(profile=["baseline", "exposed", "minimal"]),
)
async def cap_security_apply(cluster_id: str = "", target: str = "",
                             profile: str = "baseline", dry_run: bool = False,
                             trace_id=None) -> Dict:
    from Vera.vera.foundry.security_core import (
        profile as sec_profile, render_apply, parse_results)
    if not target:
        return {"error": "target is required (node | ct:<vmid> | vm:<vmid>)"}
    p = sec_profile(profile)
    if p.get("error"):
        return p

    script = render_apply(p["controls"], dry_run=dry_run)
    if dry_run:
        return {"dry_run": True, "profile": profile, "target": target,
                "controls": [{"id": c["id"], "title": c["title"],
                              "severity": c["severity"], "standard": c["standard"]}
                             for c in p["controls"]],
                "script": script}

    res = await _sec_exec(cluster_id, target, script, timeout=900)
    if res.get("error"):
        return {"error": res["error"], "target": target}
    out = res.get("stdout") or ""
    parsed = parse_results(out, p["controls"])

    if parsed["aborted"]:
        # Deliberately not a silent partial success: the host is untouched and
        # the reason is the guard.
        await emit_event({"type": "foundry.security.aborted", "target": target})
        return {"ok": False, "aborted": True, "target": target,
                "profile": profile, **parsed,
                "reason": "a guard control failed; nothing was changed"}

    r = _redis()
    if r:
        raw = await r.hget(K_SECHOSTS, target)
        rec = json.loads(raw) if raw else {"target": target, "history": []}
        rec.update({"profile": profile, "last_applied": time.time(),
                    "last_result": parsed, "last_verified": time.time()})
        rec["history"] = (rec.get("history") or [])[-19:] + [
            {"at": time.time(), "action": "apply", "profile": profile,
             "passed": parsed["passed"], "failed": parsed["failed"]}]
        await r.hset(K_SECHOSTS, target, json.dumps(rec))

    await emit_event({"type": "foundry.security.applied", "target": target,
                      "profile": profile, "failed": parsed["failed"]})
    return {"ok": parsed["failed"] == 0, "target": target, "profile": profile,
            **parsed}


@capability(
    "foundry.security.verify",
    http_method="POST", http_path="/foundry/security/verify", http_tags=["foundry"],
    memory="on",
    description="Re-check a host against its profile WITHOUT changing anything, "
                "and report drift against the last result. A control that used "
                "to pass and now fails is reported separately from one that "
                "never passed. Inputs: cluster_id (str!), target (str!), profile "
                "(str — defaults to whatever was applied). Output: {results, "
                "drift, passed, failed, unknown}.",
)
async def cap_security_verify(cluster_id: str = "", target: str = "",
                              profile: str = "", trace_id=None) -> Dict:
    from Vera.vera.foundry.security_core import (
        profile as sec_profile, render_verify, parse_results, drift)
    if not target:
        return {"error": "target is required"}

    r = _redis()
    rec = None
    if r:
        raw = await r.hget(K_SECHOSTS, target)
        rec = json.loads(raw) if raw else None
    prof = profile or (rec or {}).get("profile") or "baseline"
    p = sec_profile(prof)
    if p.get("error"):
        return p

    res = await _sec_exec(cluster_id, target, render_verify(p["controls"]),
                          timeout=300)
    if res.get("error"):
        return {"error": res["error"], "target": target}
    parsed = parse_results(res.get("stdout") or "", p["controls"])
    d = drift((rec or {}).get("last_result") or {}, parsed)

    if r:
        rec = rec or {"target": target, "history": []}
        rec.update({"profile": prof, "last_verified": time.time(),
                    "last_result": parsed, "last_drift": d})
        rec["history"] = (rec.get("history") or [])[-19:] + [
            {"at": time.time(), "action": "verify", "profile": prof,
             "passed": parsed["passed"], "failed": parsed["failed"],
             "regressed": len(d["regressed"])}]
        await r.hset(K_SECHOSTS, target, json.dumps(rec))

    if d["drifted"]:
        await emit_event({"type": "foundry.security.drift", "target": target,
                          "regressed": d["regressed"],
                          "severity": d["worst_regression"]})
    return {"ok": parsed["failed"] == 0 and not d["drifted"],
            "target": target, "profile": prof, **parsed, "drift": d}


@capability(
    "foundry.security.registry",
    http_method="GET", http_path="/foundry/security/registry", http_tags=["foundry"],
    memory="off", silent=True,
    description="Every host Foundry has hardened: which profile, when it was "
                "applied, when it was last verified, how many controls pass now, "
                "and whether it has drifted. Hosts not verified recently are "
                "flagged as stale — an old pass is not evidence about today. "
                "Output: {hosts:[...], summary}.",
)
async def cap_security_registry(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"hosts": [], "summary": {}}
    rows = await r.hgetall(K_SECHOSTS) or {}
    now = time.time()
    hosts = []
    for v in rows.values():
        try:
            rec = json.loads(v)
        except Exception:
            continue
        last = rec.get("last_result") or {}
        seen = rec.get("last_verified") or 0
        hosts.append({
            "target": rec.get("target"),
            "profile": rec.get("profile"),
            "applied": rec.get("last_applied"),
            "verified": seen,
            "passed": last.get("passed", 0),
            "failed": last.get("failed", 0),
            "unknown": last.get("unknown", 0),
            "worst": last.get("worst"),
            "drifted": bool((rec.get("last_drift") or {}).get("drifted")),
            # A verification from a fortnight ago says nothing about now.
            "stale": bool(seen) and (now - seen > 7 * 86400),
            "history": (rec.get("history") or [])[-5:],
        })
    hosts.sort(key=lambda h: (not h["drifted"], h["failed"] == 0,
                              str(h["target"])))
    return {"hosts": hosts, "count": len(hosts),
            "summary": {
                "total": len(hosts),
                "clean": sum(1 for h in hosts if not h["failed"] and not h["drifted"]),
                "failing": sum(1 for h in hosts if h["failed"]),
                "drifted": sum(1 for h in hosts if h["drifted"]),
                "stale": sum(1 for h in hosts if h["stale"]),
            }}


@capability(
    "foundry.security.fim.baseline",
    http_method="POST", http_path="/foundry/security/fim/baseline", http_tags=["foundry"],
    memory="on",
    description="Record a file-integrity baseline for a host: SHA-256 of the "
                "files where interference shows up (authorized_keys, sudoers, "
                "systemd units, cron, resolv.conf). Hashes only — never file "
                "contents. Inputs: cluster_id (str!), target (str!), areas (list "
                "— access/persistence/network/binaries). Output: {ok, files}.",
)
async def cap_security_fim_baseline(cluster_id: str = "", target: str = "",
                                    areas: List[str] = None, trace_id=None) -> Dict:
    from Vera.vera.foundry.security_core import fim_scan_script, fim_parse
    if not target:
        return {"error": "target is required"}
    res = await _sec_exec(cluster_id, target, fim_scan_script(areas), timeout=600)
    if res.get("error"):
        return {"error": res["error"]}
    out = res.get("stdout") or ""
    manifest = fim_parse(out)
    if not manifest:
        return {"error": "no files hashed — is the target reachable?",
                "raw": out[:300]}
    r = _redis()
    if r:
        await r.hset(K_SECFIM, target, json.dumps(
            {"target": target, "at": time.time(),
             "areas": areas or None, "manifest": manifest}))
    return {"ok": True, "target": target, "files": len(manifest),
            "complete": "FIM_COMPLETE" in out,
            "areas": areas or "default"}


@capability(
    "foundry.security.fim.check",
    http_method="POST", http_path="/foundry/security/fim/check", http_tags=["foundry"],
    memory="on",
    description="Compare a host's watched files against its baseline. Reports "
                "added, modified AND removed — deleting an audit rule is as much "
                "a signal as adding a key — grouped by what each change would "
                "mean. A new authorized_keys entry is flagged urgent. Inputs: "
                "cluster_id (str!), target (str!), update (bool=false — adopt "
                "the current state as the new baseline). Output: {clean, events, "
                "urgent, summary}.",
)
async def cap_security_fim_check(cluster_id: str = "", target: str = "",
                                 update: bool = False, trace_id=None) -> Dict:
    from Vera.vera.foundry.security_core import (
        fim_scan_script, fim_parse, fim_diff)
    if not target:
        return {"error": "target is required"}
    r = _redis()
    raw = await r.hget(K_SECFIM, target) if r else None
    if not raw:
        return {"error": "no baseline for %r — run foundry.security.fim.baseline "
                         "first" % target}
    prev = json.loads(raw)

    res = await _sec_exec(cluster_id, target,
                          fim_scan_script(prev.get("areas")), timeout=600)
    if res.get("error"):
        return {"error": res["error"]}
    cur = fim_parse(res.get("stdout") or "")
    if not cur:
        return {"error": "scan returned nothing; not treating that as 'no "
                         "changes' — the host may be unreachable"}

    d = fim_diff(prev.get("manifest") or {}, cur)
    if d["urgent"]:
        await emit_event({"type": "foundry.security.fim.urgent",
                          "target": target,
                          "paths": [e["path"] for e in d["urgent"]]})
    elif not d["clean"]:
        await emit_event({"type": "foundry.security.fim.changed",
                          "target": target, "count": len(d["events"])})

    if update and r:
        await r.hset(K_SECFIM, target, json.dumps(
            {"target": target, "at": time.time(),
             "areas": prev.get("areas"), "manifest": cur}))
    return {"target": target, "baseline_at": prev.get("at"),
            "files": len(cur), "baseline_updated": bool(update), **d}


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
             "foundry.pxe.mac.add", "foundry.pxe.macs",
             "foundry.sdcard.detect", "foundry.sdcard.inspect",
             "foundry.sdcard.plan", "foundry.sdcard.provision",
             "foundry.node.list", "foundry.node.frame.set",
             "foundry.vm.import", "foundry.vm.export",
             "foundry.salvage.inspect", "foundry.salvage.plan",
             "foundry.salvage.run", "foundry.salvage.list",
             "foundry.salvage.wipe_check",
             "foundry.security.standards", "foundry.security.apply",
             "foundry.security.verify", "foundry.security.registry",
             "foundry.security.fim.baseline",
             "foundry.security.fim.check"],
    mode="element",     # embedded as a Workers & Ollama sub-tab
)
