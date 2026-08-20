"""
netsec_core.py — pure, app-free builders for the mesh providers
===============================================================
The WireGuard provider's install SHELL SCRIPT is a pure string; extracting the
builder here (no app imports) makes it unit-testable app-free (imported via
lowercase `vera.networking.netsec_core`; see `worktree-testable-cores-pattern`)
and keeps ONE source for the exact command run over SSH.
"""
from __future__ import annotations

# Preamble run before ANY package install on a freshly-provisioned host: a cloud
# image is usually still running cloud-init (which holds the apt/dpkg lock) when we
# enrol, so an immediate `apt-get install` fails with "Could not get lock
# /var/lib/apt/lists/lock" — seen in the 2026-08-10 real-VM E2E, where identity
# enrolment SUCCEEDED but the mesh install lost the apt race to cloud-init's own
# apt (process 462). Wait for cloud-init to finish, THEN wait for the apt/dpkg
# locks to clear, before installing. `$S` (sudo -n or empty) is set by the caller.
_PKG_WAIT = r"""
# don't race a freshly-cloud-init'd host's own apt (dev-lifecycle: real-VM E2E finding)
command -v cloud-init >/dev/null 2>&1 && $S cloud-init status --wait >/dev/null 2>&1 || true
if command -v apt-get >/dev/null 2>&1; then
  for _i in $(seq 1 60); do
    $S fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1 || break
    sleep 5
  done
fi
""".strip()


def wireguard_install_script() -> str:
    """Package-manager-agnostic WireGuard install run over SSH. Waits for cloud-init
    + the apt/dpkg locks first (see _PKG_WAIT), elevates with `sudo -n` when not
    root, logs to /tmp/vera_wg_install.log, and prints VERA_WG_* markers the caller
    parses. apt-get also carries `-o DPkg::Lock::Timeout=300` as belt-and-braces so
    a late-arriving lock waits rather than fails."""
    return r"""
command -v wg >/dev/null 2>&1 && { echo VERA_WG_PRESENT; exit 0; }
S=""; [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1 && S="sudo -n"
__PKG_WAIT__
{
  if command -v apt-get >/dev/null 2>&1; then
    $S apt-get -o DPkg::Lock::Timeout=300 update -y && $S env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y wireguard wireguard-tools
  elif command -v dnf >/dev/null 2>&1; then
    $S dnf install -y wireguard-tools
  elif command -v yum >/dev/null 2>&1; then
    $S yum install -y epel-release 2>/dev/null; $S yum install -y wireguard-tools
  elif command -v zypper >/dev/null 2>&1; then
    $S zypper --non-interactive install wireguard-tools
  elif command -v pacman >/dev/null 2>&1; then
    $S pacman -Sy --noconfirm wireguard-tools
  elif command -v apk >/dev/null 2>&1; then
    $S apk add --no-cache wireguard-tools
  elif command -v opkg >/dev/null 2>&1; then
    $S opkg update && $S opkg install wireguard-tools kmod-wireguard
  else
    echo "VERA_WG_NOPKG: no supported package manager (apt/dnf/yum/zypper/pacman/apk/opkg)"
  fi
} >/tmp/vera_wg_install.log 2>&1
if command -v wg >/dev/null 2>&1; then
  echo VERA_WG_INSTALLED
else
  echo VERA_WG_FAIL
  echo '---VERA_WG_LOG_TAIL---'
  tail -n 15 /tmp/vera_wg_install.log 2>/dev/null
fi
""".strip().replace("__PKG_WAIT__", _PKG_WAIT)


def wg_peer_allowed_ips(peer_ip: str, routes=None) -> str:
    """AllowedIPs value for a mesh peer: its own /32, PLUS any subnets it advertises
    as a gateway — so other members route those subnets through it. Pure →
    unit-testable. (Without this, peers only ever learn each other's /32 host IPs and
    a whole-LAN gateway is impossible.)"""
    parts = [f"{peer_ip}/32"]
    for r in (routes or []):
        r = (str(r) or "").strip()
        if r and r not in parts:
            parts.append(r)
    return ", ".join(parts)


def wg_gateway_postup(mesh_subnet: str, iface: str) -> str:
    """PostUp for a GATEWAY member: enable IPv4 forwarding and masquerade mesh-sourced
    traffic leaving any non-mesh interface, so peers can reach the advertised LAN
    subnet through this host and replies return. Idempotent (-C guard). Pure."""
    return (f"sysctl -w net.ipv4.ip_forward=1; "
            f"iptables -t nat -C POSTROUTING -s {mesh_subnet} ! -o {iface} -j MASQUERADE 2>/dev/null "
            f"|| iptables -t nat -A POSTROUTING -s {mesh_subnet} ! -o {iface} -j MASQUERADE")


def wg_gateway_postdown(mesh_subnet: str, iface: str) -> str:
    """PostDown for a GATEWAY member: remove the masquerade rule PostUp added."""
    return (f"iptables -t nat -D POSTROUTING -s {mesh_subnet} ! -o {iface} -j MASQUERADE "
            f"2>/dev/null || true")
