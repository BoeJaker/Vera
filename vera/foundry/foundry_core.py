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


# ── PXE NETBOOT SERVER config generation (pure) ─────────────────────────────────
# Codifies the hand-proven netboot stack (dnsmasq + iPXE menu) so foundry.pxe.server.*
# can (re)deploy it deterministically. All values are shell-quoted where interpolated.
_ALPINE_MODULES = "loop,squashfs,sd-mod,usb-storage"


def pxe_dnsmasq_conf(server_ip: str, iface: str, range_lo: str, range_hi: str,
                     tftp_root: str = "/srv/foundry/tftp",
                     except_ifaces: List[str] = None) -> str:
    """Fenced dnsmasq config for a Foundry netboot server: DHCP + DNS + TFTP bound to
    ONE interface only (interface=<iface> + bind-interfaces), so it can NEVER answer on
    the main LAN — dnsmasq logs 'sockets bound exclusively to interface <iface>'. Raw
    PXE clients chainload iPXE (undionly.kpxe / ipxe.efi); iPXE then fetches boot.ipxe."""
    lines = [
        f"# Vera Foundry netboot -- FENCED to {iface} ({server_ip}); never serves other bridges.",
        f"interface={iface}", "bind-interfaces", "except-interface=lo",
    ]
    for i in (except_ifaces or []):
        lines.append(f"except-interface={i}")
    lines += [
        f"dhcp-range={range_lo},{range_hi},255.255.255.0,12h",
        f"dhcp-option=option:router,{server_ip}",
        f"dhcp-option=option:dns-server,{server_ip}",
        "enable-tftp", f"tftp-root={tftp_root}",
        "dhcp-match=set:ipxe,175",
        "dhcp-match=set:efi,option:client-arch,7",
        "dhcp-match=set:efi,option:client-arch,9",
        "dhcp-boot=tag:ipxe,boot.ipxe",
        "dhcp-boot=tag:!ipxe,tag:!efi,undionly.kpxe",
        "dhcp-boot=tag:!ipxe,tag:efi,ipxe.efi",
        "log-dhcp",
    ]
    return "\n".join(lines) + "\n"


def _alpine_boot(server_ip: str, alpine_repo: str, apkovl: str = "") -> str:
    ov = f" apkovl=http://{server_ip}/alpine/{apkovl}" if apkovl else ""
    return ("kernel http://{s}/alpine/vmlinuz-lts initrd=initramfs-lts ip=dhcp "
            "modloop=http://{s}/alpine/modloop-lts alpine_repo={r}{ov} "
            "modules={m} console=tty0\n"
            "initrd http://{s}/alpine/initramfs-lts\n"
            "boot || goto start").format(s=server_ip, r=alpine_repo, ov=ov, m=_ALPINE_MODULES)


def pxe_ipxe_menu(server_ip: str, install_images: List[Dict] = None,
                  alpine_repo: str = "http://dl-cdn.alpinelinux.org/alpine/v3.21/main") -> str:
    """Generate the iPXE boot menu. Built-ins: LOCAL DISK (default, protects the
    installed OS), RAM ops node (terminal UI + Docker worker), Desktop+VNC, bare
    Alpine, and netboot.xyz (all OSes incl. Kali live). `install_images` (from the
    Foundry image catalogue: [{id, os, version}] with linux+initrd hosted at
    http://<server>/<id>/) become the 'Install to disk' entries — so 'pick what to
    install' is driven by the catalogue, not a hand-written list."""
    S = server_ip
    imgs = [i for i in (install_images or []) if i.get("id")]
    out = ["#!ipxe", ":start", "menu Vera Foundry -- network boot",
           "item --gap -- --- Run in RAM (nothing written to local disk) ---",
           "item local    Boot from LOCAL DISK  (keep the installed OS)",
           "item ops      Ops + Compute node -- Alpine RAM + terminal UI (Docker worker, joins swarm)",
           "item desktop  Desktop + VNC (heavy) -- Alpine RAM + XFCE + Remmina (VNC/RDP/SSH)",
           "item alpine   Alpine -- minimal RAM live (bare console)",
           "item --gap -- --- Full OS catalogue: live + install (Ubuntu/Debian/Fedora/Alma/Arch/Kali/Mint...) ---",
           "item nbxyz    Browse ALL OSes -- netboot.xyz  (incl. Kali live desktop w/ VNC)"]
    if imgs:
        out.append("item --gap -- --- Install to disk (OVERWRITES the target) ---")
        for im in imgs:
            label = f"Install {im.get('os','?')} {im.get('version','')}".strip()
            out.append(f"item img_{_pxe_slug(im['id'])}    {label}")
    out += ["item --gap -- ---", "item shell    iPXE shell",
            "choose --default local --timeout 20000 target && goto ${target}", "",
            ":local", "echo Booting local disk (installed OS untouched)...",
            "sanboot --no-describe --drive 0x80 || goto start", "",
            ":ops", _alpine_boot(S, alpine_repo, "node.apkovl.tar.gz"), "",
            ":desktop", _alpine_boot(S, alpine_repo, "desktop.apkovl.tar.gz"), "",
            ":alpine", _alpine_boot(S, alpine_repo), "",
            ":nbxyz", "echo Loading netboot.xyz (all OSes)...",
            f"iseq ${{platform}} efi && chain http://{S}/netboot.xyz.efi || kernel http://{S}/netboot.xyz.lkrn",
            "boot || goto start", ""]
    for im in imgs:
        slug = _pxe_slug(im["id"])
        out += [f":img_{slug}",
                f"kernel http://{S}/{im['id']}/linux",
                f"initrd http://{S}/{im['id']}/initrd.gz",
                "boot || goto start", ""]
    out += [":shell", "shell", ""]
    return "\n".join(out)


def pxe_ops_apkovl_files(server_ip: str, alpine_ver: str = "3.21") -> Dict:
    """Files for the ops-node Alpine diskless overlay (apkovl), as {relpath: content}.
    The node boots to RAM, installs Docker + SSH + tools, joins the swarm as a WORKER
    only (never self-promotes to manager — managers are persistent VMs/CTs), and
    auto-launches a whiptail terminal UI on the console. Pure → unit-testable."""
    repos = (f"http://dl-cdn.alpinelinux.org/alpine/v{alpine_ver}/main\n"
             f"http://dl-cdn.alpinelinux.org/alpine/v{alpine_ver}/community\n")
    start = (
        "#!/bin/sh\n"
        "exec >/var/log/foundry-node.log 2>&1\n"
        'echo "[foundry] ops node boot $(date); kernel=$(uname -r)"\n'
        # modloop (the kernel-module squashfs) must be mounted before Docker's
        # overlay/bridge/netfilter drivers can load, or dockerd cannot start.
        "rc-service modloop start 2>/dev/null\n"
        "KREL=$(uname -r)\n"
        'for i in $(seq 1 12); do [ -d "/lib/modules/$KREL/kernel" ] && break; '
        "rc-service modloop start 2>/dev/null; sleep 2; done\n"
        "depmod -a 2>/dev/null\n"
        "modprobe -a overlay br_netfilter bridge veth nf_nat nf_conntrack ip_tables "
        "iptable_nat iptable_filter ip6_tables ip6table_nat xt_conntrack tun 2>/dev/null\n"
        "mkdir -p /etc/modules-load.d && printf '%s\\n' overlay br_netfilter bridge veth "
        "nf_nat ip_tables iptable_nat tun > /etc/modules-load.d/foundry.conf\n"
        "apk update; apk add newt eudev\n"
        "apk add docker docker-cli openssh openssh-client curl jq bash rsync ca-certificates\n"
        # On RAM/diskless boot the OpenRC boot runlevel does not complete (fsck/sysfs
        # report 'would not start'), which both prevents the udev-trigger coldplug and
        # makes `service docker start` fail its dependency check. Start udevd directly,
        # coldplug by hand, and start cgroups+docker with --nodeps to bypass the phantom
        # dependency failure. Verified live on a diskless ThinkPad ops node.
        "pgrep -x udevd >/dev/null 2>&1 || /sbin/udevd --daemon 2>/dev/null\n"
        "udevadm trigger --action=add 2>/dev/null; udevadm settle --timeout=20 2>/dev/null\n"
        "rc-update add cgroups boot 2>/dev/null; rc-service --nodeps cgroups start 2>/dev/null\n"
        "rc-update add docker default; rc-service --nodeps docker start 2>/dev/null\n"
        "for i in $(seq 1 12); do docker info >/dev/null 2>&1 && break; sleep 2; done\n"
        "docker info 2>&1 | grep -iE 'Server Version|Storage Driver' | "
        "sed 's/^/[foundry] docker: /'\n"
        "rc-update add sshd default; rc-service --nodeps sshd start 2>/dev/null || "
        "service sshd start\n"
        "mkdir -p /root/.ssh; chmod 700 /root/.ssh\n"
        f"wget -qO- http://{server_ip}/ops/authorized_keys 2>/dev/null > /root/.ssh/authorized_keys; "
        "chmod 600 /root/.ssh/authorized_keys 2>/dev/null\n"
        "sleep 3\n"
        f"TOKEN=$(wget -qO- http://{server_ip}/swarm/worker-token 2>/dev/null | tr -d '\\r\\n')\n"
        f"MGR=$(wget -qO- http://{server_ip}/swarm/manager 2>/dev/null | tr -d '\\r\\n')\n"
        'if [ -n "$TOKEN" ] && [ -n "$MGR" ]; then '
        'docker swarm join --token "$TOKEN" "${MGR}:2377"; '
        'else echo "[foundry] no manager published -> standalone (worker-only, never a manager)"; fi\n'
    )
    tui = (
        "#!/bin/sh\n"
        'command -v whiptail >/dev/null 2>&1 || { echo "Ops node still installing..."; '
        "while ! command -v whiptail >/dev/null 2>&1; do sleep 3; done; }\n"
        "T=/tmp/ftui.out\n"
        "PVE=$(cat /etc/foundry/pve 2>/dev/null || echo 192.168.0.200)\n"
        "sshkey(){ [ -s /root/.ssh/id_estate ] && echo '-i /root/.ssh/id_estate' || echo ''; }\n"
        "while true; do\n"
        '  CH=$(whiptail --title "Vera Foundry - Ops Node ($(hostname))" --menu "Diskless compute worker" 21 76 11 '
        'status "Node + Docker + swarm status" vms "Proxmox VMs / CTs -- list + console in" '
        'dps "Running containers" nodes "Swarm nodes" ssh "SSH into an estate host" '
        'join "Re-run swarm join" pve "Set Proxmox host" log "Boot/join log" '
        'shell "Shell" reboot "Reboot" 3>&1 1>&2 2>&3) || { clear; exec sh; }\n'
        "  K=$(sshkey)\n"
        "  case \"$CH\" in\n"
        '    status) { echo HOST: $(hostname); ip -4 addr show eth0 2>/dev/null|grep inet; docker node ls 2>/dev/null||echo "(not in a swarm)"; } >$T 2>&1; whiptail --scrolltext --textbox $T 24 78;;\n'
        '    vms) ssh $K -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 root@$PVE "echo == CONTAINERS ==; pct list 2>/dev/null; echo; echo == VMs ==; qm list 2>/dev/null" >$T 2>&1; '
        'whiptail --title "Proxmox $PVE" --scrolltext --textbox $T 26 100; '
        'ID=$(whiptail --inputbox "Console INTO which CT/VM id? (CT=pct enter, VM=serial console; blank cancels)" 9 68 "" 3>&1 1>&2 2>&3); '
        '[ -n "$ID" ] && { clear; ssh $K -t root@$PVE "if pct status $ID >/dev/null 2>&1; then pct enter $ID; else qm terminal $ID; fi"; };;\n'
        "    dps) docker ps >$T 2>&1; whiptail --scrolltext --textbox $T 24 100;;\n"
        "    nodes) docker node ls >$T 2>&1; whiptail --scrolltext --textbox $T 24 100;;\n"
        '    ssh) H=$(whiptail --inputbox "SSH target (user@host):" 8 60 "root@$PVE" 3>&1 1>&2 2>&3) && { clear; ssh $K -o StrictHostKeyChecking=accept-new $H; };;\n'
        f'    join) {{ TK=$(wget -qO- http://{server_ip}/swarm/worker-token|tr -d "\\r\\n"); M=$(wget -qO- http://{server_ip}/swarm/manager|tr -d "\\r\\n"); docker swarm join --token "$TK" "${{M}}:2377"; }} >$T 2>&1; whiptail --scrolltext --textbox $T 20 90;;\n'
        '    pve) NP=$(whiptail --inputbox "Proxmox host IP/name:" 8 60 "$PVE" 3>&1 1>&2 2>&3) && { echo "$NP" > /etc/foundry/pve; PVE="$NP"; };;\n'
        "    log) whiptail --scrolltext --textbox /var/log/foundry-node.log 24 100;;\n"
        '    shell) clear; echo "type exit to return to the menu"; sh;;\n'
        "    reboot) reboot;;\n"
        "  esac\ndone\n"
    )
    # busybox `login -f root` (present in the Alpine base) autologins tty1 — agetty is
    # NOT in the base and isn't installed until local.d runs, which panicked init.
    inittab = ("::sysinit:/sbin/openrc sysinit\n::sysinit:/sbin/openrc boot\n::wait:/sbin/openrc default\n"
               "tty1::respawn:/bin/login -f root\n"
               "tty2::respawn:/sbin/getty 38400 tty2\n"
               "::ctrlaltdel:/sbin/reboot\n::shutdown:/sbin/openrc shutdown\n")
    profile = ('if [ "$(tty)" = "/dev/tty1" ] && [ -z "$FTUI_RUN" ]; then '
               "export FTUI_RUN=1; /usr/local/bin/foundry-tui; fi\n")
    return {
        "etc/apk/repositories": repos,
        "etc/local.d/foundry.start": start,
        "usr/local/bin/foundry-tui": tui,
        "etc/inittab": inittab,
        "root/.profile": profile,
        "etc/foundry/pve": "192.168.0.200\n",   # default Proxmox host for the VMs/CTs menu
    }


import re as _re


def swarm_service_cmd(name: str, image: str, replicas: int = 1, command: str = "",
                      detach: bool = True) -> str:
    """Build a `docker service create` command to dispatch work onto the Docker Swarm
    (Vera's distributed-compute cluster). Name is slugified; the image ref is validated
    (returns '' if unsafe); the optional command runs inside the container. Pure →
    unit-testable; the cap runs it on the swarm manager via `pct exec`."""
    name = _re.sub(r"[^A-Za-z0-9_.-]", "-", (name or "vera-job")).strip("-") or "vera-job"
    image = (image or "").strip()
    if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_./:@-]*$", image):
        return ""   # reject an unsafe/empty image reference
    reps = max(1, min(int(replicas or 1), 100))
    parts = ["docker", "service", "create", "--name", shlex.quote(name),
             "--replicas", str(reps)]
    if detach:
        parts.append("--detach")
    parts.append(shlex.quote(image))
    if command and command.strip():
        parts += ["sh", "-c", shlex.quote(command)]
    return " ".join(parts)
