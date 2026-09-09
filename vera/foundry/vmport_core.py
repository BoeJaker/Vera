"""vmport_core.py -- pure, app-free planning for VM import and export.

Bringing a machine that lives somewhere else onto Proxmox, and taking one back
off it. Written after doing it by hand for a VirtualBox Kali image, so the
awkward parts are encoded rather than rediscovered.

── Why a plan object rather than a shell one-liner ───────────────────────────

`qm importdisk` is the easy half. The parts that go wrong are around it: the
disk arrives as `unusedN` and does nothing until it is attached; the boot order
still points at nothing; a 32-bit guest needs SeaBIOS while a modern Windows
image needs OVMF and a TPM; and a sparse image reports its *virtual* size, so a
25 GB file lands as a 500 GB disk and the storage estimate is wrong by 20x.
Each of those is a separate, checkable decision, so each is a field here.

── Nothing here runs anything ───────────────────────────────────────────────

The plan is data. The app layer executes it, which keeps this unit-testable
with no hypervisor, and means a plan can be shown to someone before it touches
their disks.

Consumers import uppercase (Vera.vera.foundry.vmport_core); tests import
lowercase (vera.foundry.vmport_core) -- see `worktree-testable-cores-pattern`.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Disk formats qemu-img (and therefore `qm importdisk`) reads directly.
# The value is (label, needs_conversion). Nothing here needs a manual
# qemu-img convert step first -- importdisk handles the read side -- but
# knowing the source format still drives the guest defaults below.
FORMATS: Dict[str, str] = {
    "vdi": "VirtualBox",
    "vmdk": "VMware / VirtualBox export",
    "vhd": "Hyper-V / Virtual PC (fixed or dynamic)",
    "vhdx": "Hyper-V (modern)",
    "qcow2": "QEMU / KVM",
    "qed": "QEMU (obsolete)",
    "raw": "raw block image",
    "img": "raw block image",
    "iso": "optical image -- attach as a CD, do not import as a disk",
}

# Container formats: an archive holding one or more disks plus a descriptor.
ARCHIVE_FORMATS = {"ova": "OVF appliance (tar)", "ovf": "OVF descriptor"}

# Magic bytes, for when the extension lies -- which it does on anything that
# has been renamed, and on `.img` files that are secretly qcow2.
MAGIC = [
    (b"KDMV", "vmdk"),
    (b"QFI\xfb", "qcow2"),
    (b"conectix", "vhd"),
    (b"vhdxfile", "vhdx"),
    (b"<<< Oracle VM VirtualBox Disk Image >>>", "vdi"),
    (b"<<< Sun VirtualBox Disk Image >>>", "vdi"),
    (b"<<< innotek VirtualBox Disk Image >>>", "vdi"),
]

# Proxmox `ostype` values, and what each implies for firmware.
OSTYPES = {
    "l26": "Linux 2.6 - 6.x",
    "l24": "Linux 2.4",
    "win11": "Windows 11 / 2022",
    "win10": "Windows 10 / 2016-2019",
    "win8": "Windows 8 / 2012",
    "win7": "Windows 7 / 2008r2",
    "wxp": "Windows XP",
    "solaris": "Solaris",
    "other": "other",
}

# Guests that will not boot on SeaBIOS and need UEFI plus a TPM.
NEEDS_UEFI = {"win11"}


def detect_format(name: str, header: bytes = b"") -> Dict:
    """Identify a disk image.

    The header wins over the extension. A `.img` that starts with `QFI\\xfb` is
    a qcow2 whatever it is called, and importing it as raw produces a VM that
    boots to a blank screen for reasons nobody enjoys chasing.
    """
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    by_magic = None
    for sig, fmt in MAGIC:
        if header.startswith(sig) or (len(header) >= 8 and header[:8] == sig[:8]
                                      and len(sig) <= 8):
            by_magic = fmt
            break

    if ext in ARCHIVE_FORMATS:
        return {"format": ext, "kind": "archive",
                "label": ARCHIVE_FORMATS[ext],
                "note": "an OVA is a tar of disks plus an OVF descriptor; "
                        "unpack it and import each disk separately"}

    fmt = by_magic or (ext if ext in FORMATS else "")
    out = {
        "format": fmt or "unknown",
        "kind": "disk" if fmt else "unknown",
        "label": FORMATS.get(fmt, "unrecognised"),
        "by_magic": bool(by_magic),
        "by_extension": ext or None,
    }
    # ".img" and ".raw" are generic container names, not format claims, so a
    # qcow2 called disk.img is not a lie -- only a specific-but-wrong extension
    # (a vmdk named .vdi) is worth warning about.
    GENERIC = {"img", "raw"}
    if by_magic and ext and ext in FORMATS and ext not in GENERIC and by_magic != ext:
        # Worth shouting about: the file is not what its name claims.
        out["mismatch"] = (
            "the extension says %r but the file's header says %r; trusting the "
            "header" % (ext, by_magic))
    if fmt == "iso":
        out["kind"] = "optical"
    return out


def guest_defaults(os_hint: str = "", bits: int = 0) -> Dict:
    """Sensible VM settings for a guest, from whatever hint is available.

    `bits` matters: a 32-bit image on a machine type that assumes 64-bit boots
    to nothing, and the imported Kali here was a 32-bit 2020 build.
    """
    hint = (os_hint or "").lower()
    if any(k in hint for k in ("win11", "windows 11")):
        ostype = "win11"
    elif any(k in hint for k in ("win10", "windows 10", "2019", "2016")):
        ostype = "win10"
    elif any(k in hint for k in ("win7", "windows 7")):
        ostype = "win7"
    elif "xp" in hint:
        ostype = "wxp"
    elif "windows" in hint or "win" in hint:
        ostype = "win10"
    elif hint:
        ostype = "l26"
    else:
        ostype = "l26"

    uefi = ostype in NEEDS_UEFI
    return {
        "ostype": ostype,
        "bios": "ovmf" if uefi else "seabios",
        "machine": "q35" if uefi else "pc",
        # virtio-scsi is fastest, but a guest that has never seen it will not
        # find its own disk. Windows images imported from elsewhere have no
        # virtio driver until someone installs one.
        "scsihw": "virtio-scsi-pci",
        "disk_bus": "sata" if ostype.startswith("w") else "scsi",
        "disk_bus_reason": (
            "Windows images from another hypervisor have no virtio driver yet, "
            "so SATA boots and virtio does not"
            if ostype.startswith("w")
            else "virtio-scsi: the guest kernel already has the driver"),
        "vga": "std",
        "needs_uefi": uefi,
        "needs_tpm": uefi,
        "bits": bits or (32 if "32" in hint else 0),
    }


def import_plan(vmid: int, source: str, storage: str, *,
                name: str = "", memory: int = 2048, cores: int = 2,
                bridge: str = "vmbr0", os_hint: str = "",
                virtual_bytes: int = 0, actual_bytes: int = 0,
                description: str = "") -> Dict:
    """The ordered steps to bring a disk image up as a Proxmox VM.

    Split into steps rather than one command because `importdisk` lands the
    disk as `unusedN`: a VM whose disk is never attached and whose boot order
    is never set exists, starts, and boots to nothing.
    """
    if not vmid or not source or not storage:
        return {"error": "vmid, source and storage are all required"}

    fmt = detect_format(source)
    if fmt["kind"] == "optical":
        return {"error": "that is an ISO -- attach it as a CD rather than "
                         "importing it as a disk"}
    if fmt["kind"] == "archive":
        return {"error": fmt.get("note", "unpack the archive first")}

    g = guest_defaults(os_hint)
    bus = g["disk_bus"]
    disk_ref = "%s:vm-%d-disk-0" % (storage, vmid)

    steps: List[Dict] = [
        {"stage": "create",
         "cmd": ["qm", "create", str(vmid), "--name", name or ("vm-%d" % vmid),
                 "--memory", str(memory), "--cores", str(cores),
                 "--net0", "virtio,bridge=%s" % bridge,
                 "--ostype", g["ostype"], "--scsihw", g["scsihw"]]
                + (["--bios", "ovmf", "--machine", "q35"] if g["needs_uefi"] else [])
                + (["--description", description] if description else []),
         "why": "the VM shell, with firmware chosen for the guest"},
        {"stage": "import",
         "cmd": ["qm", "importdisk", str(vmid), source, storage],
         "why": "qm importdisk reads %s natively; no separate conversion needed"
                % fmt["format"],
         "slow": True},
        {"stage": "attach",
         "cmd": ["qm", "set", str(vmid), "--%s0" % bus, disk_ref],
         "why": "the import leaves it as unused0 -- without this the VM has no disk"},
        {"stage": "boot-order",
         "cmd": ["qm", "set", str(vmid), "--boot", "order=%s0" % bus],
         "why": "otherwise it boots from nothing and sits at the BIOS"},
        {"stage": "console",
         "cmd": ["qm", "set", str(vmid), "--vga", g["vga"]],
         "why": "a console you can actually open from the web UI"},
    ]
    if g["needs_tpm"]:
        steps.insert(2, {
            "stage": "tpm",
            "cmd": ["qm", "set", str(vmid), "--tpmstate0",
                    "%s:1,version=v2.0" % storage],
            "why": "Windows 11 refuses to boot without a TPM"})

    notes: List[str] = [g["disk_bus_reason"]]
    if virtual_bytes and actual_bytes and virtual_bytes > actual_bytes * 2:
        notes.append(
            "This image is sparse: %.1f GB of data in a %.1f GB virtual disk. "
            "Proxmox will create a %.0f GB disk -- on a thin-provisioned store "
            "(ZFS, LVM-thin) that costs only the real %.1f GB, but on a "
            "directory store it can allocate the full amount."
            % (actual_bytes / 1e9, virtual_bytes / 1e9,
               virtual_bytes / 1e9, actual_bytes / 1e9))
    if fmt.get("mismatch"):
        notes.append(fmt["mismatch"])

    return {"ok": True, "vmid": vmid, "format": fmt["format"],
            "guest": g, "steps": steps, "notes": notes,
            "estimated_disk_bytes": virtual_bytes or actual_bytes}


def export_plan(vmid: int, disk: str, out_dir: str,
                fmt: str = "qcow2") -> Dict:
    """Take a VM's disk back off Proxmox in a portable format.

    qcow2 by default: it keeps sparseness, so a mostly-empty 500 GB disk stays
    small, which raw does not. vmdk is the one to pick when it has to open in
    VMware or VirtualBox.
    """
    if fmt not in ("qcow2", "vmdk", "vdi", "vhdx", "raw"):
        return {"error": "unsupported export format %r" % fmt}
    if not vmid or not disk:
        return {"error": "vmid and disk are required"}

    target = "%s/vm-%d-disk.%s" % (out_dir.rstrip("/"), vmid, fmt)
    steps = [
        {"stage": "stop-check",
         "cmd": ["qm", "status", str(vmid)],
         "why": "exporting a running VM's disk copies a torn filesystem; it "
                "must be stopped first"},
        {"stage": "resolve",
         "cmd": ["pvesm", "path", disk],
         "why": "turn the storage reference into a real path for qemu-img"},
        {"stage": "convert",
         "cmd": ["qemu-img", "convert", "-p", "-O", fmt, "<resolved-path>", target],
         "why": "qemu-img writes %s directly" % fmt,
         "slow": True},
        {"stage": "verify",
         "cmd": ["qemu-img", "check", target],
         "why": "an export nobody checked is not a backup"},
    ]
    notes = ["The VM must be stopped -- a live disk export is not consistent."]
    if fmt == "raw":
        notes.append("raw loses sparseness: an empty 500 GB disk becomes a "
                     "500 GB file. Prefer qcow2 unless something demands raw.")
    if fmt in ("vmdk", "vdi"):
        notes.append("For VirtualBox/VMware, pair the disk with a new VM "
                     "definition there -- the disk alone is not a machine.")
    return {"ok": True, "vmid": vmid, "format": fmt, "target": target,
            "steps": steps, "notes": notes}
