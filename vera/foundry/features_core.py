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
elif command -v zypper >/dev/null 2>&1; then _PKG=zypper
else _PKG=unknown; fi
pkg_install(){
  case "$_PKG" in
    apt) mkdir -p /etc/apt/apt.conf.d 2>/dev/null; echo 'DPkg::Lock::Timeout "120";' > /etc/apt/apt.conf.d/99foundry-lock 2>/dev/null; dpkg --configure -a >/dev/null 2>&1 || true; DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>/dev/null; DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    apk) apk add "$@" ;;
    pacman) pacman -Sy --noconfirm "$@" ;;
    dnf) dnf install -y "$@" ;;
    zypper) zypper --non-interactive install "$@" ;;
    *) echo "[foundry] no known package manager"; return 1 ;;
  esac
}
svc_enable(){
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then systemctl enable --now "$1" 2>/dev/null
  else rc-update add "$1" default 2>/dev/null; rc-service "$1" start 2>/dev/null; fi
}
'''

FEATURES = ("mesh", "distributed-compute", "hardening", "file-client",
            "file-server", "security-monitoring", "vfs-client")

# Defaults for the vfs-client feature. VFS-02 is the estate's file server; a
# guest that wants the shared drives should not have to be told where they are.
VFS_DEFAULT_HOST = "192.168.0.160"
VFS_SHARE_PATHS = {
    "home": "/srv/pools/tank_sde/vfs/home",
    "cloud": "/srv/pools/tank_sde/vfs/cloud",
    "sync": "/srv/pools/tank_sde/vfs/sync",
    "backup": "/srv/pools/tank_sde/vfs/backup",
    "media": "/srv/pools/BigDat",
    "estate": "/srv/vfs/estate",
}


def _wrap(body: str) -> str:
    return "#!/bin/sh\n" + OS_ADAPTER + "\n" + body


def _q(value) -> str:
    """POSIX single-quote a value for safe interpolation into a feature script.

    Feature ctx is operator-supplied (share names, hosts, an SMB password), and
    these scripts run as root on a freshly provisioned guest, so a value
    carrying a quote or a semicolon must not be able to end the argument and
    start a command. shlex.quote is not used here because this module is
    deliberately import-free and app-free.
    """
    return "'" + str(value).replace("'", "'\\''") + "'"


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
        "printf '%s' \"$RESP\" | jq -e '.ok == true' >/dev/null 2>&1 || { echo \"[foundry] mesh enrol failed: $RESP\"; exit 1; }\n"
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


def _vfs_client_feature(ctx) -> str:
    """Mount the Vera File Fabric (VFS-02) shares on a provisioned guest.

    Distinct from the generic `file-client` feature, which needs every share
    spelled out per job and defaults to `guest,vers=3.0` -- anonymous access
    over a superseded dialect. This one knows where the fabric is, defaults to
    NFSv4.2 (the faster path for Linux, and no credential to leave on disk),
    and where SMB is asked for it insists on SMB3.1.1 with sealing and a
    root-only credentials file.

    ctx keys: vfs_host, shares (list or comma string), transport ('nfs'|'cifs'),
              smb_user, smb_pass, mount_base.
    """
    host = str(ctx.get("vfs_host", "") or VFS_DEFAULT_HOST).strip()
    base = str(ctx.get("mount_base", "") or "/mnt/vfs").strip().rstrip("/")
    transport = str(ctx.get("transport", "") or "nfs").strip().lower()
    if transport not in ("nfs", "cifs"):
        transport = "nfs"
    raw = ctx.get("shares") or ["home", "sync", "media"]
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
    shares = [s for s in raw if s in VFS_SHARE_PATHS]

    lines = ["# feature: vfs-client -- mount the Vera File Fabric (VFS-02)\n"]
    if not shares:
        return _wrap("".join(lines) + "echo '[foundry] vfs-client: no known shares requested'\n")

    if transport == "nfs":
        lines.append("pkg_install nfs-common 2>/dev/null || pkg_install nfs-utils 2>/dev/null || true\n")
    else:
        lines.append("pkg_install cifs-utils 2>/dev/null || true\n")
        user = str(ctx.get("smb_user", "") or "").strip()
        pw = str(ctx.get("smb_pass", "") or "")
        if not (user and pw):
            return _wrap("".join(lines) +
                         "echo '[foundry] vfs-client: transport=cifs needs smb_user and "
                         "smb_pass in the feature ctx'; exit 1\n")
        # 0600 and root-owned: a world-readable credentials file would hand the
        # share to every local account on the guest.
        lines.append("umask 077\n")
        lines.append("printf 'username=%s\\npassword=%s\\n' " +
                     _q(user) + " " + _q(pw) + " > /etc/vfs-credentials\n")
        lines.append("chmod 600 /etc/vfs-credentials; chown root:root /etc/vfs-credentials\n")

    for share in shares:
        mnt = base + "/" + share
        # The estate view is read-only on the server; say so on the client too,
        # so a stray write fails locally instead of confusingly at the server.
        ro = ",ro" if share == "estate" else ""
        if transport == "nfs":
            remote = host + ":" + VFS_SHARE_PATHS[share]
            fstype = "nfs4"
            opts = ("vers=4.2,hard,timeo=600,retrans=2,rsize=1048576,"
                    "wsize=1048576,noatime,_netdev,nofail,x-systemd.automount,"
                    "x-systemd.idle-timeout=600" + ro)
        else:
            remote = "//" + host + "/" + share
            fstype = "cifs"
            opts = ("credentials=/etc/vfs-credentials,vers=3.1.1,seal,"
                    "cache=strict,actimeo=30,iocharset=utf8,_netdev,nofail,"
                    "x-systemd.automount" + ro)
        lines.append("mkdir -p " + _q(mnt) + "\n")
        # Replace any previous line for this mountpoint so re-running the
        # feature with different options converges instead of stacking entries.
        lines.append("sed -i '\\# " + mnt + " #d' /etc/fstab 2>/dev/null || true\n")
        lines.append("printf '%s %s %s %s 0 0\\n' " + _q(remote) + " " + _q(mnt) +
                     " " + _q(fstype) + " " + _q(opts) + " >> /etc/fstab\n")

    lines.append("systemctl daemon-reload 2>/dev/null || true\n")
    # mount -a can hang for a long time on an unreachable server; nofail plus a
    # bounded timeout keeps provisioning moving if the fabric is down.
    lines.append("timeout 60 mount -a 2>/dev/null || "
                 "echo '[foundry] vfs-client: some mounts deferred (server unreachable?)'\n")
    lines.append("echo '[foundry] vfs-client configured: " +
                 " ".join(shares) + " via " + transport + " from " + host + "'\n")
    return _wrap("".join(lines))


def _file_server_feature(ctx) -> str:
    """Host SMB (Samba) + NFS exports. ctx.exports = [{path,name}]; default /srv/foundry."""
    exports = ctx.get("exports") or [{"path": "/srv/foundry", "name": "foundry"}]
    lines = [
        "# feature: file-server -- host SMB (Samba) + NFS exports\n",
        "pkg_install samba samba-common-bin nfs-kernel-server 2>/dev/null || "
        "pkg_install samba nfs-utils 2>/dev/null || pkg_install samba nfs-server 2>/dev/null || true\n",
        "mkdir -p /etc/samba 2>/dev/null; touch /etc/samba/smb.conf 2>/dev/null\n",
        "grep -q '^\\[global\\]' /etc/samba/smb.conf 2>/dev/null || "
        "printf '[global]\\n  workgroup = WORKGROUP\\n  server min protocol = SMB2\\n  map to guest = Bad User\\n' >> /etc/samba/smb.conf\n",
    ]
    for e in exports:
        path = str(e.get("path", "")).strip()
        name = str(e.get("name", "")).strip()
        if not (path and name):
            continue
        lines.append("mkdir -p '" + path + "'; chmod 0777 '" + path + "' 2>/dev/null || true\n")
        lines.append("grep -q '^\\[" + name + "\\]' /etc/samba/smb.conf 2>/dev/null || "
                     "printf '[" + name + "]\\n  path = " + path + "\\n  browseable = yes\\n  read only = no\\n  guest ok = yes\\n' >> /etc/samba/smb.conf\n")
        lines.append("grep -q ' " + path + " ' /etc/exports 2>/dev/null || "
                     "echo '" + path + " *(rw,sync,no_subtree_check)' >> /etc/exports\n")
    lines += [
        "svc_enable smbd 2>/dev/null || svc_enable smb 2>/dev/null || svc_enable samba 2>/dev/null || true\n",
        "svc_enable nmbd 2>/dev/null || svc_enable nmb 2>/dev/null || true\n",
        "exportfs -ra 2>/dev/null || true\n",
        "svc_enable nfs-kernel-server 2>/dev/null || svc_enable nfs-server 2>/dev/null || svc_enable nfs 2>/dev/null || true\n",
        "echo '[foundry] file-server configured'\n",
    ]
    return _wrap("".join(lines))


def _security_monitoring_feature(ctx) -> str:
    """auditd baseline rules + optional remote log shipping (ctx.log_collector = rsyslog host)."""
    collector = str(ctx.get("log_collector", "") or "").strip()
    lines = [
        "# feature: security-monitoring -- auditd baseline rules + optional remote log shipping\n",
        "pkg_install auditd 2>/dev/null || pkg_install audit 2>/dev/null || true\n",
        "mkdir -p /etc/audit/rules.d 2>/dev/null\n",
        "cat > /etc/audit/rules.d/foundry.rules <<'ARULES'\n"
        "-w /etc/passwd -p wa -k identity\n"
        "-w /etc/shadow -p wa -k identity\n"
        "-w /etc/ssh/sshd_config -p wa -k sshd\n"
        "-w /etc/sudoers -p wa -k sudoers\n"
        "-a always,exit -F arch=b64 -S execve -k exec\n"
        "ARULES\n",
        "augenrules --load 2>/dev/null || true\n",
        "svc_enable auditd 2>/dev/null || svc_enable auditd.service 2>/dev/null || true\n",
    ]
    if collector:
        lines += [
            "pkg_install rsyslog 2>/dev/null || true\n",
            "mkdir -p /etc/rsyslog.d 2>/dev/null\n",
            "echo '*.* @@" + collector + ":514' > /etc/rsyslog.d/99-foundry-ship.conf\n",
            "svc_enable rsyslog 2>/dev/null || true\n",
            "(command -v systemctl >/dev/null 2>&1 && systemctl restart rsyslog 2>/dev/null) || rc-service rsyslog restart 2>/dev/null || true\n",
        ]
    lines.append("echo '[foundry] security-monitoring configured'\n")
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
    if feature in ("vfs-client", "vfs_client"):
        return _vfs_client_feature(ctx)
    if feature == "file-server":
        return _file_server_feature(ctx)
    if feature == "security-monitoring":
        return _security_monitoring_feature(ctx)
    return ""
