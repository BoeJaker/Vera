"""
foundry_core.py — pure, dependency-free Foundry netboot RENDER logic
====================================================================

The PXE/physical netboot render (a profile → its boot artifacts: iPXE +
cloud-init autoinstall for x86; config.txt/cmdline.txt for Raspberry Pi incl. the
XPT2046 3.2" SPI touchscreen overlay) and the hardening bundle were defined
inline in foundry_capabilities.py, which imports the whole orchestrator
(`@capability`, `register_ui`) and therefore can't be imported by a unit test
without booting the app — the exact "in-container full-app import hangs the
serving process" trap (see documentation/specs/dev-lifecycle-and-repo-hygiene.md
§8.3 #9). These functions are pure (base64 + dict/list only), so they're
extracted here VERBATIM. foundry_capabilities.py imports them from here, so there
is ONE implementation, and tests/test_foundry_pxe_render.py imports them via the
lowercase `vera.foundry.foundry_core` path (resolves to the worktree, and pulls
in NO app dependencies).
"""
from __future__ import annotations

import base64
import json
import re
import shlex
from typing import Dict, List

# cluster / distributed-compute systems a Foundry-provisioned host can be made to
# join via a feature bundle (see cluster_join_script).
CLUSTER_KINDS = ("docker-swarm", "k3s", "nomad", "ray", "generic")


def cluster_join_script(kind: str, join_addr: str = "", token: str = "",
                        role: str = "", opts: Dict = None) -> str:
    """Pure POSIX snippet that makes THIS host join an existing cluster / distributed-
    compute system, for a Foundry feature bundle. kind ∈ CLUSTER_KINDS. Dynamic
    values are shell-quoted; the token is a SECRET (the caller unseals it just before
    render and must not log the rendered script). Returns '' for an unknown kind."""
    opts = opts or {}
    kind = (kind or "").lower().strip()
    role = (role or "").lower().strip()
    A = shlex.quote(join_addr or "")
    T = shlex.quote(token or "")

    if kind == "docker-swarm":
        port = int(opts.get("port", 2377))
        return "\n".join([
            "# --- join Docker Swarm ---",
            "command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sh) || true",
            f"docker swarm join --token {T} {A}:{port} || "
            "echo 'VERA_JOIN_WARN: docker swarm join failed (already joined? bad token/addr?)'",
        ])
    if kind == "k3s":
        api = int(opts.get("port", 6443))
        if role in ("server", "manager", "control-plane", "control_plane"):
            return "\n".join([
                "# --- join k3s (HA control-plane) ---",
                f"curl -sfL https://get.k3s.io | K3S_TOKEN={T} sh -s - server --server https://{A}:{api}",
            ])
        return "\n".join([
            "# --- join k3s (agent) ---",
            f"curl -sfL https://get.k3s.io | K3S_URL=https://{A}:{api} K3S_TOKEN={T} sh -",
        ])
    if kind == "nomad":
        srv = int(opts.get("port", 4647))
        return "\n".join([
            "# --- join HashiCorp Nomad (client) ---",
            "if ! command -v nomad >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then",
            "  curl -fsSL https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp.gpg 2>/dev/null || true",
            '  . /etc/os-release; echo "deb [signed-by=/usr/share/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com $VERSION_CODENAME main" >/etc/apt/sources.list.d/hashicorp.list',
            "  apt-get update -y && apt-get install -y nomad || true",
            "fi",
            "mkdir -p /etc/nomad.d /opt/nomad",
            "cat >/etc/nomad.d/client.hcl <<VERA_NOMAD_EOF",
            'data_dir = "/opt/nomad"',
            "client {",
            "  enabled = true",
            f'  servers = ["{join_addr}:{srv}"]',
            "}",
            "VERA_NOMAD_EOF",
            "systemctl enable --now nomad 2>/dev/null || (nomad agent -config=/etc/nomad.d >/var/log/nomad.log 2>&1 &)",
        ])
    if kind == "ray":
        rport = int(opts.get("port", 6379))
        pw = f" --redis-password={T}" if token else ""
        return "\n".join([
            "# --- join Ray cluster (worker) ---",
            "command -v ray >/dev/null 2>&1 || pip install -q 'ray[default]' 2>/dev/null || pip3 install -q 'ray[default]' 2>/dev/null || true",
            f"ray start --address={A}:{rport}{pw} || echo 'VERA_JOIN_WARN: ray start failed'",
        ])
    if kind == "generic":
        return "\n".join(["# --- generic cluster join ---", str(opts.get("command", "true"))])
    return ""


def cluster_init_script(kind: str, advertise_addr: str = "", opts: Dict = None) -> str:
    """Pure: bootstrap a NEW cluster on THIS host and print its join token behind a
    VERA_*_TOKEN= marker the caller parses. Idempotent (re-running on an existing
    manager just re-reads the token). docker-swarm → `docker swarm init` + the worker
    join-token; k3s → install a k3s server + read its node-token. '' for kinds that
    can't self-init (nomad/ray/generic — point them at an existing control plane)."""
    opts = opts or {}
    kind = (kind or "").lower().strip()
    A = shlex.quote(advertise_addr or "")
    if kind == "docker-swarm":
        adv = f"--advertise-addr {A} " if advertise_addr else ""
        return "\n".join([
            "# --- init Docker Swarm ---",
            "command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sh) || true",
            f"docker swarm init {adv}>/dev/null 2>&1 || true",   # no-op if already a manager
            "echo VERA_SWARM_TOKEN=$(docker swarm join-token -q worker 2>/dev/null)",
            "echo VERA_SWARM_MGR=$(docker info --format '{{.Swarm.NodeAddr}}' 2>/dev/null)",
        ])
    if kind == "k3s":
        return "\n".join([
            "# --- init k3s server ---",
            "command -v k3s >/dev/null 2>&1 || curl -sfL https://get.k3s.io | sh - >/dev/null 2>&1 || true",
            "echo VERA_K3S_TOKEN=$(cat /var/lib/rancher/k3s/server/node-token 2>/dev/null)",
        ])
    return ""


def parse_init_token(kind: str, output: str) -> Dict:
    """Extract the captured join token (+ manager addr for swarm) from cluster_init_script
    output. Returns {token, addr}; token '' when nothing was captured (init failed /
    daemon not up)."""
    text = output or ""
    kind = (kind or "").lower().strip()
    token, addr = "", ""
    if kind == "docker-swarm":
        m = re.search(r"VERA_SWARM_TOKEN=(\S+)", text)
        token = m.group(1) if m else ""
        m2 = re.search(r"VERA_SWARM_MGR=(\S+)", text)
        addr = m2.group(1) if m2 else ""
    elif kind == "k3s":
        m = re.search(r"VERA_K3S_TOKEN=(\S+)", text)
        token = m.group(1) if m else ""
    return {"token": token, "addr": addr}


def pick_node(nodes_json: str) -> str:
    """From `pvesh get /nodes --output-format json`, return the first ONLINE node's
    name (else the first node with a name, else ''). foundry.provision needs a node
    for clone/create — an empty node builds the API path /nodes//… → HTTP 501 (the
    real-VM E2E 2026-08-10 clone failure)."""
    try:
        nodes = json.loads(nodes_json or "[]")
    except Exception:
        return ""
    if not isinstance(nodes, list):
        return ""
    for n in nodes:
        if isinstance(n, dict) and n.get("status") == "online" and n.get("node"):
            return str(n["node"])
    for n in nodes:
        if isinstance(n, dict) and n.get("node"):
            return str(n["node"])
    return ""


# hardening bundle — small, idempotent, POSIX-ish (Debian/EL both handled).
_HARDEN = r"""
set -e
# no root SSH password login; keep key/cert auth
if [ -f /etc/ssh/sshd_config ]; then
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config || true
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config || true
  (systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null || true)
fi
# host firewall — allow ssh + mesh, default deny (best-effort per available tool)
if command -v ufw >/dev/null 2>&1; then
  ufw --force reset >/dev/null 2>&1 || true
  ufw default deny incoming >/dev/null 2>&1; ufw default allow outgoing >/dev/null 2>&1
  ufw allow 22/tcp >/dev/null 2>&1; ufw allow 51820/udp >/dev/null 2>&1
  ufw --force enable >/dev/null 2>&1 || true
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-service=ssh >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-port=51820/udp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
fi
# unattended security updates
if command -v apt-get >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y dnf-automatic >/dev/null 2>&1 && systemctl enable --now dnf-automatic.timer >/dev/null 2>&1 || true
fi
echo VERA_HARDEN_DONE
""".strip()


# ── netboot artifact rendering (pure) — a PXE profile → its boot files ──────────
def _pxe_slug(s: str) -> str:
    s = (s or "node").lower()
    return ("".join(c if (c.isalnum() or c == "-") else "-" for c in s).strip("-")) or "node"


def _render_features_script(feats: List[str], cluster_scripts: List[str] = None) -> str:
    """First-boot script applying the SAME feature bundles as CT/VM (target-agnostic).
    `cluster_scripts` are pre-rendered cluster-join snippets (from cluster_join_script,
    resolved against the Foundry cluster registry in the app layer and passed in so
    this stays pure) — e.g. join a Docker Swarm / k3s / Nomad / Ray cluster."""
    out = ["#!/bin/sh", "set -e", "# Foundry feature bundles — applied on first boot"]
    if "hardening" in feats:
        out += ["# --- hardening ---", _HARDEN]
    if "file-client" in feats:
        out += ["# --- file-client ---",
                "command -v apt-get >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive "
                "apt-get install -y cifs-utils nfs-common autofs >/dev/null 2>&1 || true"]
    # real cluster / distributed-compute joins resolved from the registry
    for cs in (cluster_scripts or []):
        if cs and cs.strip():
            out += ["", cs]
    # legacy fallback: a swarm/compute feature with no cluster registered → install
    # the runtime only (so the host is ready to be joined manually).
    if (("docker-swarm" in feats or "distributed-compute" in feats)
            and not (cluster_scripts or [])):
        out += ["# --- docker (no cluster registered — runtime install only) ---",
                "command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sh) || true"]
    out += ["# enrol/mesh: host registers with Foundry on check-in (SSH-cert + FreeIPA + mesh)",
            "echo foundry-features-done"]
    return "\n".join(out) + "\n"


def _render_rpi_config(display: str, opts: Dict = None) -> str:
    """Raspberry Pi config.txt. display=xpt2046 → 3.2\" SPI TFT + XPT2046 touch.
    Pins/panel are overridable via the profile's display_opts (defaults suit a
    common generic 3.2\" ILI9341 board — adjust to your wiring)."""
    opts = opts or {}
    lines = ["# Foundry Raspberry Pi config.txt", "arm_64bit=1", "enable_uart=1"]
    if display == "xpt2046":
        panel = opts.get("panel", "ili9341")
        dc, rst = opts.get("dc", 24), opts.get("reset", 25)
        penirq, rotate = opts.get("penirq", 17), opts.get("rotate", 270)
        speed = opts.get("speed", 32000000)
        lines += [
            "# --- XPT2046 3.2\" SPI touchscreen: TFT + ADS7846-compatible touch ---",
            "# (panel/pins overridable via the profile's display_opts)",
            "dtparam=spi=on",
            f"dtoverlay=fbtft,spi0-0,{panel},dc_pin={dc},reset_pin={rst},rotate={rotate},"
            f"speed={speed},fps=30,bgr=1",
            f"dtoverlay=ads7846,cs=1,penirq={penirq},penirq_pull=2,speed=1000000,keep_vref_on=1,"
            "swapxy=0,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900",
            "hdmi_force_hotplug=0",
        ]
    else:
        lines += ["# --- HDMI monitor ---", "hdmi_force_hotplug=1", "hdmi_drive=2"]
    return "\n".join(lines) + "\n"


def _render_rpi_cmdline(profile: Dict, cfg: Dict) -> str:
    boot_type = profile.get("boot_type", "rpi-netboot")
    ip = profile.get("ip", "")
    gw = cfg.get("gateway", "")
    net = f"ip={ip}::{gw}:255.255.255.0::eth0:off" if ip else "ip=dhcp"
    if boot_type == "rpi-netboot":
        root = f"root=/dev/nfs nfsroot={gw}:/srv/foundry/rpi/{_pxe_slug(profile.get('name',''))},vers=3 rw"
    else:
        root = "root=/dev/mmcblk0p2 rootwait"
    con = " fbcon=map:1 console=tty1" if profile.get("display") == "xpt2046" else ""
    return f"console=serial0,115200 {root} {net}{con} elevator=deadline\n"


def _render_ipxe(profile: Dict, cfg: Dict, image: Dict, http: str) -> str:
    nm = _pxe_slug(profile.get("name", "node"))
    return "\n".join([
        "#!ipxe",
        f"# Foundry profile: {profile.get('name')} ({profile.get('arch','amd64')})",
        "dhcp || echo using-next-server",
        f"set base {http}/{nm}",
        f"kernel ${{base}}/vmlinuz initrd=initrd.img autoinstall "
        f"ds=nocloud-net;s=${{base}}/autoinstall/ ---",
        f"initrd ${{base}}/initrd.img",
        "boot || shell",
    ]) + "\n"


def _render_autoinstall(profile: Dict, cfg: Dict, feats: List[str],
                        cluster_scripts: List[str] = None) -> str:
    """cloud-init NoCloud user-data: static net + run the feature bundle on first boot."""
    ip = profile.get("ip", "")
    gw = cfg.get("gateway", "")
    net = ""
    if ip:
        net = ("network:\n  version: 2\n  ethernets:\n    eth0:\n"
               f"      addresses: [{ip}/24]\n      routes: [{{to: default, via: {gw}}}]\n")
    # embed the feature script base64 (encoding: b64) so arbitrary shell content
    # can never break the YAML block-scalar indentation.
    fb64 = base64.b64encode(_render_features_script(feats, cluster_scripts).encode()).decode()
    return ("#cloud-config\n"
            f"hostname: {_pxe_slug(profile.get('name','node'))}\n"
            "ssh_pwauth: false\n"
            + net
            + "write_files:\n"
              "  - path: /var/lib/foundry/features.sh\n    permissions: '0755'\n"
              f"    encoding: b64\n    content: {fb64}\n"
              "runcmd:\n  - [ sh, /var/lib/foundry/features.sh ]\n")


def _render_boot(profile: Dict, cfg: Dict, image: Dict,
                 cluster_scripts: List[str] = None) -> Dict:
    """Turn a PXE profile into its netboot artifacts — unified across x86 + RPi.
    `cluster_scripts` (from the app, resolved against the cluster registry) bake
    Docker Swarm / k3s / Nomad / Ray joins into the first-boot feature script."""
    arch = (profile.get("arch") or "amd64").lower()
    boot_type = (profile.get("boot_type")
                 or ("rpi-netboot" if arch in ("arm64", "armhf") else "uefi")).lower()
    display = (profile.get("display") or "hdmi").lower()
    feats = profile.get("features") or []
    http = cfg.get("http_base") or f"http://{cfg.get('gateway','10.42.0.1')}/foundry"
    artifacts: Dict[str, str] = {"features.sh": _render_features_script(feats, cluster_scripts)}
    if boot_type in ("rpi-netboot", "rpi-flash"):
        artifacts["config.txt"] = _render_rpi_config(display, profile.get("display_opts"))
        artifacts["cmdline.txt"] = _render_rpi_cmdline(profile, cfg)
        if boot_type == "rpi-flash":
            artifacts["flash.plan"] = (
                f"# rpi-imager/dd flash plan for {profile.get('name')}\n"
                f"IMAGE={image.get('source_url','<image>')}\n"
                "# 1) rpi-imager --cli $IMAGE /dev/sdX   (write base image to SD)\n"
                "# 2) copy config.txt + cmdline.txt onto the boot partition\n"
                "# 3) copy features.sh + a firstrun hook into the rootfs\n")
    else:
        artifacts["boot.ipxe"] = _render_ipxe(profile, cfg, image, http)
        artifacts["autoinstall/user-data"] = _render_autoinstall(profile, cfg, feats, cluster_scripts)
        artifacts["autoinstall/meta-data"] = \
            f"instance-id: {_pxe_slug(profile.get('name','node'))}\n"
    return {"boot_type": boot_type, "arch": arch, "display": display,
            "features": feats, "artifacts": artifacts}
