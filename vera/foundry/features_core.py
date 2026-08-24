"""features_core.py -- pure, app-free builders for OS-agnostic Foundry FEATURE scripts.

A *feature* is an idempotent post-install shell script run on a provisioned guest (via
guest.exec for CTs, or cloud-init user-data for VMs). An auto-detecting OS adapter
(pkg_install / svc_enable across apt / apk / pacman / dnf and systemd / OpenRC) lets ONE
feature definition run on Debian/Ubuntu/Kali, Alpine, Arch and AlmaLinux without
per-distro branches in each feature.

Consumers import uppercase (Vera.vera.foundry.features_core); tests import lowercase
(vera.foundry.features_core) so pytest binds to the worktree copy -- see
`worktree-testable-cores-pattern`. This module has NO app imports."""
from __future__ import annotations

# Sourced at the top of every feature script; provides pkg_install + svc_enable.
OS_ADAPTER = '''# --- Foundry OS adapter: pkg_install / svc_enable across distros ---
if command -v apt-get >/dev/null 2>&1; then _PKG=apt
elif command -v apk >/dev/null 2>&1; then _PKG=apk
elif command -v pacman >/dev/null 2>&1; then _PKG=pacman
elif command -v dnf >/dev/null 2>&1; then _PKG=dnf
else _PKG=unknown; fi
pkg_install(){
  case "$_PKG" in
    apt) mkdir -p /etc/apt/apt.conf.d 2>/dev/null; echo 'DPkg::Lock::Timeout "120";' > /etc/apt/apt.conf.d/99foundry-lock 2>/dev/null; dpkg --configure -a >/dev/null 2>&1 || true; DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>/dev/null; DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    apk) apk add "$@" ;;
    pacman) pacman -Sy --noconfirm "$@" ;;
    dnf) dnf install -y "$@" ;;
    *) echo "[foundry] no known package manager"; return 1 ;;
  esac
}
svc_enable(){
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then systemctl enable --now "$1" 2>/dev/null
  else rc-update add "$1" default 2>/dev/null; rc-service "$1" start 2>/dev/null; fi
}
'''

FEATURES = ("mesh", "distributed-compute", "hardening", "file-client")


def _wrap(body: str) -> str:
    return "#!/bin/sh\n" + OS_ADAPTER + "\n" + body


def _mesh_feature(ctx) -> str:
    vera = str(ctx.get("vera_url", "") or "").strip()
    token = str(ctx.get("mesh_token", "") or "").strip()
    body = (
        "# feature: mesh -- self-enrol onto the Vera WireGuard mesh (node keeps its private key)\n"
        "pkg_install wireguard-tools jq curl 2>/dev/null || pkg_install wireguard jq curl\n"
        "mkdir -p /etc/wireguard\n"
        "[ -s /etc/wireguard/vera0.key ] || { umask 077; wg genkey > /etc/wireguard/vera0.key; }\n"
        "PUB=$(wg pubkey < /etc/wireguard/vera0.key)\n"
        "HOST=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')\n"
        "VERA='" + vera + "'\n"
        "TOKEN='" + token + "'\n"
        "BODY=$(printf '{\"pubkey\":\"%s\",\"token\":\"%s\",\"host\":\"%s\",\"label\":\"%s\"}' \"$PUB\" \"$TOKEN\" \"$HOST\" \"$(hostname)\")\n"
        "RESP=$(curl -sk -H 'Content-Type: application/json' -d \"$BODY\" \"$VERA/netsec/mesh/enroll\" 2>/dev/null)\n"
        "printf '%s' \"$RESP\" | grep -q '\"ok\": true' || { echo \"[foundry] mesh enrol failed: $RESP\"; exit 1; }\n"
        "printf '%s' \"$RESP\" | jq -r .conf | sed \"s|__PRIVKEY__|$(cat /etc/wireguard/vera0.key)|\" > /etc/wireguard/vera0.conf\n"
        "chmod 600 /etc/wireguard/vera0.conf\n"
        "wg-quick down vera0 2>/dev/null || true\n"
        "wg-quick up vera0 && echo '[foundry] mesh up'\n"
    )
    return _wrap(body)


def _worker_feature(ctx) -> str:
    image = str(ctx.get("vera_image", "") or "192.168.0.138:5000/vera:latest").strip()
    reg = str(ctx.get("registry", "") or "192.168.0.138:5000").strip()
    env = str(ctx.get("vera_worker_env", "") or "").rstrip("\n")
    body = (
        "# feature: distributed-compute -- run a Vera WORKER container joined to the stack\n"
        "command -v docker >/dev/null 2>&1 || { pkg_install docker.io 2>/dev/null || pkg_install docker; }\n"
        "cat > /etc/docker/daemon.json <<DJSON\n"
        "{\"insecure-registries\": [\"" + reg + "\"]}\n"
        "DJSON\n"
        "mkdir -p /etc/docker; svc_enable docker\n"
        "(command -v systemctl >/dev/null 2>&1 && systemctl restart docker 2>/dev/null) || rc-service docker restart 2>/dev/null || true\n"
        "cat > /etc/vera-worker.env <<'WENV'\n" + env + "\nWENV\n"
        "docker rm -f vera-worker 2>/dev/null || true\n"
        "docker run -d --name vera-worker --restart unless-stopped --network host --env-file /etc/vera-worker.env '" + image + "' && echo '[foundry] vera-worker started' || echo '[foundry] vera-worker failed (image reachable?)'\n"
    )
    return _wrap(body)


def _hardening_feature(ctx) -> str:
    body = (
        "# feature: hardening -- sshd policy, host firewall, auto-updates, no root SSH login\n"
        "if [ -f /etc/ssh/sshd_config ]; then\n"
        "  sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config\n"
        "  sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config\n"
        "  (command -v systemctl >/dev/null 2>&1 && systemctl reload sshd 2>/dev/null) || rc-service sshd reload 2>/dev/null || true\n"
        "fi\n"
        "if command -v ufw >/dev/null 2>&1 || pkg_install ufw 2>/dev/null; then ufw --force default deny incoming 2>/dev/null; ufw default allow outgoing 2>/dev/null; ufw allow 22/tcp 2>/dev/null; ufw allow 51820/udp 2>/dev/null; ufw --force enable 2>/dev/null; elif command -v firewall-cmd >/dev/null 2>&1; then firewall-cmd --permanent --add-service=ssh 2>/dev/null; firewall-cmd --permanent --add-port=51820/udp 2>/dev/null; firewall-cmd --reload 2>/dev/null; fi\n"
        "case \"$_PKG\" in\n"
        "  apt) pkg_install unattended-upgrades 2>/dev/null; dpkg-reconfigure -f noninteractive unattended-upgrades 2>/dev/null || true ;;\n"
        "  dnf) pkg_install dnf-automatic 2>/dev/null; svc_enable dnf-automatic.timer 2>/dev/null || true ;;\n"
        "esac\n"
        "printf 'net.ipv4.conf.all.rp_filter=1\\nkernel.kptr_restrict=1\\n' > /etc/sysctl.d/90-foundry-hardening.conf\n"
        "sysctl -p /etc/sysctl.d/90-foundry-hardening.conf 2>/dev/null || true\n"
        "echo '[foundry] hardening applied'\n"
    )
    return _wrap(body)


def _file_client_feature(ctx) -> str:
    shares = ctx.get("shares") or []
    lines = [
        "# feature: file-client -- mount SMB/NFS shares (idempotent via fstab)\n",
        "pkg_install cifs-utils nfs-common 2>/dev/null || pkg_install cifs-utils nfs-utils 2>/dev/null || true\n",
    ]
    for sh in shares:
        remote = str(sh.get("remote", "")).strip()
        mnt = str(sh.get("mountpoint", "")).strip()
        typ = str(sh.get("type", "cifs")).strip() or "cifs"
        opts = str(sh.get("options", "")).strip() or ("guest,vers=3.0" if typ == "cifs" else "defaults")
        if not (remote and mnt):
            continue
        lines.append("mkdir -p '" + mnt + "'\n")
        lines.append("grep -q ' " + mnt + " ' /etc/fstab || echo '" + remote + " " + mnt + " " + typ + " " + opts + " 0 0' >> /etc/fstab\n")
        lines.append("mount '" + mnt + "' 2>/dev/null || true\n")
    lines.append("echo '[foundry] file-client configured'\n")
    return _wrap("".join(lines))


def feature_script(feature: str, ctx=None) -> str:
    """Return the idempotent shell script for `feature`, or '' if unknown. `ctx` carries
    per-job values (vera_url, mesh_token, vera_image, vera_worker_env, registry, shares).
    Pure -> unit-testable."""
    ctx = ctx or {}
    feature = (feature or "").strip().lower()
    if feature == "mesh":
        return _mesh_feature(ctx)
    if feature in ("distributed-compute", "vera-worker", "distributed_compute"):
        return _worker_feature(ctx)
    if feature == "hardening":
        return _hardening_feature(ctx)
    if feature == "file-client":
        return _file_client_feature(ctx)
    return ""
