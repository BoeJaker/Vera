"""
netsec_capabilities.py — encrypted cluster mesh (enrolment-gated overlay network)
=================================================================================

Stands up an **encrypted overlay mesh** across the Proxmox nodes, their guests
and the Docker hosts, so every Vera control/data connection can ride a private
network where **only authenticated, enrolled devices** participate. Membership
is gated on enrolment (step-ca cert, via provisioning/enroll_capabilities.py);
everything runs over the canonical exec SSH store, so a host that Vera can
already reach + has enrolled can be pulled onto the mesh with one call.

Design (documentation/32-cluster-encryption.md):
  • **Configurable backend** — the mesh transport is a pluggable *provider*.
    Two ship:
      - `wireguard` (default) — in-kernel, fast, minimal deps. Vera IS the
        coordinator: private keys are generated ON each host over the already
        authenticated SSH channel and never leave it; only public keys travel.
      - `nebula` (experimental) — Slack's cert-based mesh. Vera holds a Nebula
        CA (sealed in Redis), signs a host cert per member and pushes it with a
        config. Its built-in host firewall (security groups) is the reason to
        pick it over raw wg when you want per-edge policy.
  • **Tolerant first** — the overlay is additive. join() works on any reachable
    host; enrolment is checked and recorded but only *enforced* when
    config.enforce is on. Nothing on the LAN breaks the day this goes in.

Capabilities (group `netsec.mesh.*`)
────────────────────────────────────
  netsec.mesh.providers   — list installed providers + which is active
  netsec.mesh.config      — read mesh config (provider, subnet, port, enforce)
  netsec.mesh.config.save — set provider/subnet/port/enforce
  netsec.mesh.candidates  — exec SSH hosts not yet on the mesh (+ enrolment flag)
  netsec.mesh.members     — current members (+ enrolment flag + last status)
  netsec.mesh.join        — install provider on a host, gen identity, allocate
                            overlay IP, add peer, resync everyone
  netsec.mesh.sync        — re-render + push the peer set to members
  netsec.mesh.status      — live per-member handshake/rx/tx (tolerant)
  netsec.mesh.leave       — remove a member everywhere + resync
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, now_iso
# app-free shell-script builder (unit-tested; waits for cloud-init/apt lock)
from Vera.vera.networking.netsec_core import (
    wireguard_install_script as _wireguard_install_script,
    wg_peer_allowed_ips, wg_gateway_postup, wg_gateway_postdown, wg_routes_for_member, wg_client_config,
    door_device_name, door_device_for, wg_door_config,
)

log = logging.getLogger("vera.netsec")
_HERE = Path(__file__).parent

KEY_MESH = "vera:netsec:mesh"        # redis hash, field "main" → JSON config
KEY_ENROLL = "vera:provisioning:ssh_hosts"   # enroll store (dual — cross-ref only)

_DEFAULTS: Dict[str, Any] = {
    # netctl owns the estate's one WireGuard door; Vera's machines join it on
    # the `vera` profile rather than Vera running a second control plane. The
    # self-hosted providers stay available for an estate without netctl.
    "provider": "netctl",
    "subnet": "10.88.0.0/16",   # self-hosted providers only; the door allocates
    "listen_port": 51820,
    "iface": "vera0",
    # Vera acts at the door with a token that can only add, list and revoke
    # vera-profile devices. It is held in the secrets service, never here.
    "door_url": "http://192.168.0.221:8088",
    "door_secret": "netctl/door-vera",
    "enroll_token": "",         # shared self-enrol token (netsec.mesh.enroll_token)
    "enforce": False,           # tolerant to begin — warn, don't block
    "members": {},              # host_id → member record
}


# ─────────────────────────────────────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────────────────────────────────────
def _redis():
    return getattr(_orch, "REDIS", None)


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


def _secrets_service() -> Optional[Dict[str, Any]]:
    fn = (_orch.CAPABILITY_REGISTRY.get("secrets.status") or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


async def _door(cfg: Dict, method: str, path: str,
                body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """netctl's door API, authorised by the vera-profile token in the secrets
    service. That token may add, list and revoke vera-profile devices and
    reach nothing else — not the gateway, not egress, not DNS, and not the
    devices of any other profile."""
    svc = _secrets_service()
    if not svc or "get_named" not in svc:
        return {"error": "the secrets service is not loaded, so the netctl door "
                         "token cannot be read"}
    where = cfg.get("door_secret") or _DEFAULTS["door_secret"]
    token = ((await svc["get_named"](where)) or {}).get("value", "")
    if not token:
        return {"error": f"the secrets service holds no netctl door token at {where!r} "
                         f"— issue one on the door node: door.py issue <name> vera"}
    base = (cfg.get("door_url") or _DEFAULTS["door_url"]).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.request(method, base + path,
                                     headers={"X-Netctl-Door": token}, json=body)
        data = r.json() if r.content else {}
    except Exception as e:
        return {"error": f"netctl's door at {base} did not answer: {type(e).__name__}"}
    if r.status_code == 401:
        return {"error": "netctl refused the door token"}
    if r.status_code == 403:
        return {"error": "netctl refused: this token is not scoped to vera devices"}
    if not isinstance(data, dict):
        return {"error": "netctl gave an unreadable answer"}
    if r.status_code >= 400:
        return {"error": data.get("error") or data.get("message")
                or f"netctl answered HTTP {r.status_code}"}
    return data


async def _ssh(host_id: str, command: str, timeout: int = 120) -> Dict:
    """Run a command on an exec-store host. The single execution primitive the
    providers build on — everything the mesh does happens over this channel."""
    run = _cap("exec.ssh.run")
    if not run:
        return {"ok": False, "error": "exec.ssh.run unavailable", "rc": -1,
                "stdout": "", "stderr": ""}
    return await run(command=command, host_id=host_id, timeout=timeout) or {}


def _root_wrap(script: str) -> str:
    """Wrap a shell script so it runs as ROOT on the remote host.

    WireGuard needs root for /etc/wireguard writes and wg/wg-quick/systemctl —
    a non-root SSH user hits "Permission denied" on the key/conf files. The
    WHOLE script must run under one privileged shell so its redirects, heredocs
    and pipes are all root (elevating individual commands doesn't help a `>`
    redirect evaluated by the caller's non-root shell). If the user is already
    root we run it directly; else we elevate with passwordless sudo (verified
    with `sudo -n true` first). A host with neither prints VERA_WG_NOROOT so the
    caller can surface an actionable message instead of a raw permission error.
    """
    q = shlex.quote(script)
    return (
        'if [ "$(id -u)" = 0 ]; then sh -c ' + q + '; '
        'elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then '
        'sudo -n sh -c ' + q + '; '
        'else echo VERA_WG_NOROOT >&2; exit 97; fi'
    )


_WG_NOROOT_HINT = ("the SSH user is not root and passwordless sudo is not "
                   "available — grant NOPASSWD sudo, enrol the host as root, or "
                   "pre-provision /etc/wireguard as this user")


async def _exec_hosts() -> List[Dict]:
    lst = _cap("exec.ssh.hosts.list")
    if not lst:
        return []
    try:
        return (await lst() or {}).get("hosts", []) or []
    except Exception:
        return []


async def _enrolled_index() -> Dict[str, str]:
    """Map host-ip → auth mode from the enrol store, so we can flag which exec
    hosts are actually enrolled (cert). The two SSH stores haven't converged
    yet (see documentation/32); we cross-reference by IP."""
    r = _redis()
    if not r:
        return {}
    out: Dict[str, str] = {}
    try:
        raw = await r.hgetall(KEY_ENROLL) or {}
        for _id, blob in raw.items():
            try:
                rec = json.loads(blob)
            except Exception:
                continue
            h = rec.get("host") or rec.get("ip")
            if h:
                out[str(h)] = rec.get("auth", "password")
    except Exception as e:
        log.debug("enroll index: %s", e)
    return out


async def _cfg() -> Dict[str, Any]:
    r = _redis()
    cfg = dict(_DEFAULTS)
    cfg["members"] = {}
    if r:
        try:
            raw = await r.hget(KEY_MESH, "main")
            if raw:
                stored = json.loads(raw)
                cfg.update({k: stored.get(k, cfg[k]) for k in _DEFAULTS})
                cfg["members"] = stored.get("members", {}) or {}
        except Exception as e:
            log.debug("mesh cfg read: %s", e)
    return cfg


async def _cfg_put(cfg: Dict[str, Any]) -> None:
    r = _redis()
    if r:
        try:
            await r.hset(KEY_MESH, "main", json.dumps(cfg))
        except Exception as e:
            log.warning("mesh cfg write: %s", e)


def _alloc_ip(cfg: Dict[str, Any]) -> str:
    """Next free host address in the overlay subnet."""
    net = ipaddress.ip_network(cfg["subnet"], strict=False)
    taken = {m.get("ip") for m in cfg["members"].values() if m.get("ip")}
    for host in net.hosts():
        s = str(host)
        if s.endswith(".0") or s.endswith(".255"):
            continue
        if s not in taken:
            return s
    raise RuntimeError("overlay subnet exhausted")


def _host_addr(rec: Dict) -> str:
    return rec.get("host") or rec.get("ip") or ""


# ═════════════════════════════════════════════════════════════════════════════
#  PROVIDER SEAM
#  Each provider turns mesh intent into shell run over SSH. Add a provider by
#  implementing this interface and registering it in _PROVIDERS.
# ═════════════════════════════════════════════════════════════════════════════
class MeshProvider:
    name = "base"
    label = "Base"
    description = ""
    experimental = False
    # True when the provider's own control plane hands out the overlay
    # address, so Vera must not allocate one from `subnet` first.
    assigns_addresses = False

    async def ensure_installed(self, host_id: str, cfg: Dict) -> Dict:
        raise NotImplementedError

    async def gen_identity(self, host_id: str, cfg: Dict, member: Dict) -> Dict:
        """Generate the host's transport identity ON the host; return the public
        part only (`pubkey`). Private material never leaves the host."""
        raise NotImplementedError

    async def apply(self, host_id: str, cfg: Dict, member: Dict,
                    peers: List[Dict]) -> Dict:
        """Render config for `member` with the given peer set and bring the
        interface up (or hot-reload it)."""
        raise NotImplementedError

    async def status(self, host_id: str, cfg: Dict) -> Dict:
        raise NotImplementedError

    async def teardown(self, host_id: str, cfg: Dict) -> Dict:
        raise NotImplementedError


class WireGuardProvider(MeshProvider):
    name = "wireguard"
    label = "WireGuard"
    description = ("In-kernel WireGuard, coordinated by Vera. Keys are generated "
                   "on each host and never transported; only public keys travel. "
                   "Minimal deps — best fit when Vera is the trust root.")

    def _iface(self, cfg) -> str:
        return cfg.get("iface", "vera0")

    async def ensure_installed(self, host_id: str, cfg: Dict) -> Dict:
        # Package-manager-agnostic install. The old command was apt-only, so any
        # host without apt (RHEL/Fedora=dnf/yum, SUSE=zypper, Arch=pacman,
        # Alpine=apk, OpenWrt=opkg) failed with "need apt or manual install".
        # Detect whichever is present and use it, elevate with sudo -n when the
        # SSH user isn't root, and on failure return the install-log tail inline
        # so the operator doesn't have to SSH in to read /tmp/vera_wg_install.log.
        # Script builder lives in app-free netsec_core (unit-tested); it now waits
        # for cloud-init + the apt/dpkg lock before installing, so a fresh cloud
        # image's own apt doesn't make our install lose the lock race (real-VM E2E
        # 2026-08-10: identity enrolled but mesh install hit the apt lock).
        cmd = _wireguard_install_script()
        r = await _ssh(host_id, cmd, timeout=360)
        out = (r.get("stdout") or "")
        if "VERA_WG_PRESENT" in out:
            return {"ok": True, "detail": "already installed"}
        if "VERA_WG_INSTALLED" in out:
            return {"ok": True, "detail": "installed"}
        # Build a self-diagnosing error: distro hint + the install-log tail.
        tail = ""
        if "---VERA_WG_LOG_TAIL---" in out:
            tail = out.split("---VERA_WG_LOG_TAIL---", 1)[1].strip()
        tail = tail or (r.get("stderr") or "").strip()
        if "VERA_WG_NOPKG" in out:
            hint = ("no supported package manager found on the host — install "
                    "wireguard-tools manually, or use the Nebula provider instead")
        elif "permission denied" in (tail.lower() + out.lower()) or "are not allowed" in tail.lower():
            hint = ("the SSH user lacks root — grant passwordless sudo, enrol the "
                    "host as root, or pre-install wireguard-tools")
        else:
            hint = "package install failed"
        err = f"wireguard install failed — {hint}."
        if tail:
            err += f"\ninstall log (tail):\n{tail[:600]}"
        return {"ok": False, "error": err}

    async def gen_identity(self, host_id: str, cfg: Dict, member: Dict) -> Dict:
        ifc = self._iface(cfg)
        # Runs as root (see _root_wrap): /etc/wireguard is root-owned 0700, and
        # the private key is written 0600 — a non-root user can't create either.
        script = (
            f"umask 077; mkdir -p /etc/wireguard; "
            f"[ -f /etc/wireguard/{ifc}.key ] || "
            f"wg genkey | tee /etc/wireguard/{ifc}.key | wg pubkey "
            f"> /etc/wireguard/{ifc}.pub; cat /etc/wireguard/{ifc}.pub"
        )
        r = await _ssh(host_id, _root_wrap(script), timeout=60)
        out = (r.get("stdout") or "")
        err = (r.get("stderr") or "")
        if "VERA_WG_NOROOT" in (out + err):
            return {"ok": False, "error": f"keygen needs root — {_WG_NOROOT_HINT}"}
        pub = out.strip().splitlines()
        pub = pub[-1].strip() if pub else ""
        if not r.get("ok") or len(pub) < 40:
            emsg = err.strip() or "keygen failed"
            if "permission denied" in emsg.lower():
                emsg = f"keygen needs root — {_WG_NOROOT_HINT}"
            return {"ok": False, "error": emsg}
        return {"ok": True, "pubkey": pub}

    def _peer_block(self, cfg: Dict, me: Dict, peers: List[Dict]) -> str:
        out = []
        for p in peers:
            if p.get("host_id") == me.get("host_id"):
                continue
            if not (p.get("pubkey") and p.get("ip")):
                continue
            lines = [
                "[Peer]",
                f"PublicKey = {p['pubkey']}",
                # a gateway member also advertises its routes (e.g. 192.168.0.0/24)
                f"AllowedIPs = {wg_peer_allowed_ips(p['ip'], wg_routes_for_member(p.get('routes'), me.get('host')))}",
            ]
            if p.get("endpoint"):
                lines.append(f"Endpoint = {p['endpoint']}")
            lines.append("PersistentKeepalive = 25")
            out.append("\n".join(lines))
        return "\n\n".join(out)

    async def apply(self, host_id: str, cfg: Dict, member: Dict,
                    peers: List[Dict]) -> Dict:
        ifc = self._iface(cfg)
        port = int(cfg.get("listen_port", 51820))
        peer_block = self._peer_block(cfg, member, peers)
        # a GATEWAY member (has advertised routes) forwards + masquerades mesh traffic
        # to those subnets; PostUp/PostDown make it persist across wg-quick up/down.
        gw = ""
        if member.get("routes"):
            gw = (f"PostUp = {wg_gateway_postup(cfg.get('subnet', ''), ifc)}\n"
                  f"PostDown = {wg_gateway_postdown(cfg.get('subnet', ''), ifc)}\n")
        # Unquoted heredoc so $(cat key) is evaluated ON the host — the private
        # key is inlined locally and never sent over the wire.
        script = (
            "umask 077\n"
            f"cat > /etc/wireguard/{ifc}.conf <<EOF\n"
            "[Interface]\n"
            f"PrivateKey = $(cat /etc/wireguard/{ifc}.key)\n"
            f"Address = {member['ip']}/32\n"
            f"ListenPort = {port}\n"
            f"{gw}"
            f"{peer_block}\n"
            "EOF\n"
            f"if ip link show {ifc} >/dev/null 2>&1; then "
            f"wg-quick strip {ifc} > /tmp/vera_{ifc}.strip 2>/dev/null && "
            f"wg syncconf {ifc} /tmp/vera_{ifc}.strip && echo VERA_WG_SYNCED; "
            "else "
            f"(systemctl enable --now wg-quick@{ifc} >/tmp/vera_wg_up.log 2>&1 "
            f"|| wg-quick up {ifc} >/tmp/vera_wg_up.log 2>&1) && echo VERA_WG_UP "
            "|| echo VERA_WG_UPFAIL; fi"
        )
        # Whole script runs as root (see _root_wrap): writes /etc/wireguard/*.conf
        # and runs wg-quick/wg syncconf/systemctl, all root-only.
        r = await _ssh(host_id, _root_wrap(script), timeout=120)
        out = r.get("stdout") or ""
        err = r.get("stderr") or ""
        if "VERA_WG_SYNCED" in out or "VERA_WG_UP" in out:
            return {"ok": True, "detail": "synced" if "SYNCED" in out else "up"}
        if "VERA_WG_NOROOT" in (out + err):
            return {"ok": False, "error": f"bring-up needs root — {_WG_NOROOT_HINT}"}
        return {"ok": False, "error": (err or out or
                "bring-up failed — see /tmp/vera_wg_up.log on the host")[-400:]}

    async def status(self, host_id: str, cfg: Dict) -> Dict:
        ifc = self._iface(cfg)
        # `wg show` reads the running interface — root-only. Elevate so a non-root
        # SSH user gets true state instead of always seeing "down"; if root is
        # unavailable it degrades to down (tolerant, matching the panel's design).
        r = await _ssh(host_id,
                       _root_wrap(f"wg show {ifc} dump 2>/dev/null || echo VERA_WG_DOWN"),
                       timeout=30)
        out = (r.get("stdout") or "").strip()
        if not out or "VERA_WG_DOWN" in out or "VERA_WG_NOROOT" in (out + (r.get("stderr") or "")):
            return {"ok": True, "up": False, "peers": []}
        lines = out.splitlines()
        peers = []
        import time as _t
        now_epoch = int(_t.time())
        for ln in lines[1:]:                    # first line = interface itself
            f = ln.split("\t")
            if len(f) < 8:
                continue
            hs = int(f[4]) if f[4].isdigit() else 0
            peers.append({
                "pubkey": f[0],
                "endpoint": f[3] if f[3] != "(none)" else "",
                "allowed_ips": f[5] if len(f) > 5 else "",
                "handshake_age_s": (now_epoch - hs) if hs else None,
                "rx": int(f[6]) if len(f) > 6 and f[6].isdigit() else 0,
                "tx": int(f[7]) if len(f) > 7 and f[7].isdigit() else 0,
            })
        return {"ok": True, "up": True, "peers": peers}

    async def teardown(self, host_id: str, cfg: Dict) -> Dict:
        ifc = self._iface(cfg)
        # Root-only: stops the service, downs the interface, removes the conf.
        script = (f"systemctl disable --now wg-quick@{ifc} 2>/dev/null; "
                  f"wg-quick down {ifc} 2>/dev/null; "
                  f"rm -f /etc/wireguard/{ifc}.conf; echo VERA_WG_TORNDOWN")
        r = await _ssh(host_id, _root_wrap(script), timeout=60)
        out = (r.get("stdout") or "") + (r.get("stderr") or "")
        if "VERA_WG_NOROOT" in out:
            return {"ok": False, "error": f"teardown needs root — {_WG_NOROOT_HINT}"}
        return {"ok": "VERA_WG_TORNDOWN" in (r.get("stdout") or "")}


class NebulaProvider(MeshProvider):
    """Slack's Nebula — a cert-based overlay whose built-in host firewall
    (security groups) maps onto per-edge policy. EXPERIMENTAL: needs the
    `nebula`/`nebula-cert` binaries. Vera holds the CA on the Vera host
    (~/.vera/nebula, 0700) and signs one host cert per member; the config
    carries the lighthouse map.

    Cert/config generation is implemented; the on-host binary install is
    best-effort (download from the Nebula release, else a clear error), so this
    provider only fully works where those binaries are reachable.
    """
    name = "nebula"
    label = "Nebula (experimental)"
    description = ("Cert-based mesh with a built-in host firewall. Vera runs the "
                   "CA; each member gets a signed cert. Pick this over WireGuard "
                   "when you want per-edge security groups rather than a flat "
                   "overlay. Requires the nebula binaries on each host.")
    experimental = True

    def _ca_dir(self) -> str:
        d = os.path.join(os.path.expanduser("~"), ".vera", "nebula")
        os.makedirs(d, exist_ok=True)
        return d

    async def _ensure_ca(self, cfg: Dict) -> Dict:
        """Generate the Vera Nebula CA locally once (nebula-cert on the Vera
        host). Returns {ok, ca_crt} or an actionable error."""
        d = self._ca_dir()
        ca_crt, ca_key = os.path.join(d, "ca.crt"), os.path.join(d, "ca.key")
        if os.path.exists(ca_crt) and os.path.exists(ca_key):
            return {"ok": True, "ca_crt": ca_crt, "ca_key": ca_key}
        proc = await asyncio.create_subprocess_exec(
            "nebula-cert", "ca", "-name", "Vera Mesh CA", "-out-crt", ca_crt,
            "-out-key", ca_key,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception as e:
            return {"ok": False, "error": f"nebula-cert not runnable: {e} — "
                    "install step-cli's `nebula-cert` on the Vera host"}
        if proc.returncode:
            return {"ok": False, "error": (err or b"").decode()[-300:]}
        return {"ok": True, "ca_crt": ca_crt, "ca_key": ca_key}

    async def ensure_installed(self, host_id: str, cfg: Dict) -> Dict:
        cmd = (
            "command -v nebula >/dev/null 2>&1 && echo VERA_NEB_PRESENT || echo VERA_NEB_ABSENT"
        )
        r = await _ssh(host_id, cmd, timeout=30)
        if "VERA_NEB_PRESENT" in (r.get("stdout") or ""):
            return {"ok": True, "detail": "already installed"}
        return {"ok": False, "error": "nebula binary not on host — install it "
                "(github.com/slackhq/nebula releases) then re-join. Automatic "
                "download not enabled for this provider yet."}

    async def gen_identity(self, host_id: str, cfg: Dict, member: Dict) -> Dict:
        ca = await self._ensure_ca(cfg)
        if not ca.get("ok"):
            return ca
        d = self._ca_dir()
        name = member.get("label") or member.get("host_id")
        crt = os.path.join(d, f"{member['host_id']}.crt")
        key = os.path.join(d, f"{member['host_id']}.key")
        net = ipaddress.ip_network(cfg["subnet"], strict=False)
        proc = await asyncio.create_subprocess_exec(
            "nebula-cert", "sign", "-ca-crt", ca["ca_crt"], "-ca-key", ca["ca_key"],
            "-name", str(name), "-ip", f"{member['ip']}/{net.prefixlen}",
            "-groups", "vera", "-out-crt", crt, "-out-key", key,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception as e:
            return {"ok": False, "error": f"nebula-cert sign failed: {e}"}
        if proc.returncode:
            return {"ok": False, "error": (err or b"").decode()[-300:]}
        # pubkey identity = the signed cert path (kept Vera-side; pushed in apply)
        return {"ok": True, "pubkey": f"nebula:{member['host_id']}"}

    async def apply(self, host_id: str, cfg: Dict, member: Dict,
                    peers: List[Dict]) -> Dict:
        # Full push of ca.crt + host.crt + host.key + config.yml would go here;
        # returned as not-yet-wired so the operator isn't misled into thinking
        # traffic is encrypted when only the certs were minted.
        return {"ok": False, "error": "nebula apply not wired — certs are minted "
                "locally; on-host config push is the remaining step. Use the "
                "wireguard provider for a working mesh today."}

    async def status(self, host_id: str, cfg: Dict) -> Dict:
        return {"ok": True, "up": False, "peers": []}

    async def teardown(self, host_id: str, cfg: Dict) -> Dict:
        return {"ok": True}


class NetctlDoorProvider(WireGuardProvider):
    """Vera's machines as devices on the estate's one WireGuard door.

    The door (netctl, on NWM-02) already owns the server key, the endpoint,
    the DuckDNS name and the router forward. A second control plane beside it
    would duplicate all four to reach machines that are on the same LAN, so
    Vera joins instead, on a profile of its own: a `vera` device reaches Vera
    and the other machines Vera manages, Vera may open connections back to it,
    and nothing else on the estate answers. Every other profile keeps the
    opposite guarantees — a phone is not reachable and devices cannot see each
    other.

    Install and key generation are inherited: the key pair is still made ON
    the host and only the public half is sent, which is what a door built for
    phones (netctl generates their keys) would otherwise give up.
    """
    name = "netctl"
    label = "netctl door"
    description = ("Join the estate's one WireGuard door (netctl) on the vera "
                   "profile, instead of Vera running a second WireGuard control "
                   "plane. netctl allocates the address and holds the server key; "
                   "each host still generates its own key and sends only the "
                   "public half, over a token scoped to vera devices.")
    assigns_addresses = True

    async def _enrol(self, cfg: Dict, member: Dict) -> Dict:
        """Make sure this member has a device at the door, and return it."""
        name = member.get("door_device") or door_device_name(member.get("label"),
                                                             member.get("host_id"))
        listing = await _door(cfg, "GET", "/api/door/peers")
        if listing.get("error"):
            return listing
        action, dev = door_device_for(listing.get("peers"), name, member.get("pubkey", ""))
        if action == "rekey":
            gone = await _door(cfg, "POST", f"/api/door/peer/delete/{name}")
            if gone.get("error"):
                return {"error": f"the door still holds an older key for {name}: "
                                 f"{gone['error']}"}
            action = "add"
        if action == "add":
            made = await _door(cfg, "POST", "/api/door/peer",
                               {"name": name, "pubkey": member.get("pubkey", "")})
            if made.get("error") or not made.get("ok"):
                return {"error": made.get("error") or made.get("message")
                        or "the door would not enrol this device"}
            dev = made
        else:
            # Already enrolled: the listing has the address, the door status has
            # the server key and endpoint every member needs to dial.
            dev = dict(dev or {})
            dev.setdefault("server_pubkey", listing.get("server_pubkey", ""))
            dev.setdefault("endpoint", listing.get("endpoint", ""))
            dev.setdefault("allowed_ips", listing.get("allowed_ips") or [])
        dev["name"] = name
        return dev

    async def apply(self, host_id: str, cfg: Dict, member: Dict,
                    peers: List[Dict]) -> Dict:
        # `peers` is ignored on purpose: at a door every member has exactly one
        # peer, the door itself, and the door decides who may reach whom.
        dev = await self._enrol(cfg, member)
        if dev.get("error"):
            return {"ok": False, "error": dev["error"]}
        if not dev.get("server_pubkey") or not dev.get("endpoint"):
            return {"ok": False, "error": "the door did not say how to reach it "
                                          "(no server key or endpoint) — is its "
                                          "DuckDNS name configured?"}
        ifc = self._iface(cfg)
        # The door is on the record before the tunnel is tried, so a bring-up
        # that times out still leaves Vera knowing which device is its own.
        member["door_device"] = dev.get("name", "")
        conf = wg_door_config(dev.get("address", ""), dev, f"/etc/wireguard/{ifc}.key",
                              member_host=member.get("host", ""))
        # Unquoted heredoc so $(cat key) runs ON the host: the private key is
        # read there and never crosses the wire.
        script = ("umask 077\n"
                  f"cat > /etc/wireguard/{ifc}.conf <<EOF\n{conf}\nEOF\n"
                  f"if ip link show {ifc} >/dev/null 2>&1; then "
                  f"wg-quick strip {ifc} > /tmp/vera_{ifc}.strip 2>/dev/null && "
                  f"wg syncconf {ifc} /tmp/vera_{ifc}.strip && echo VERA_WG_SYNCED; "
                  "else "
                  f"(systemctl enable --now wg-quick@{ifc} >/tmp/vera_wg_up.log 2>&1 "
                  f"|| wg-quick up {ifc} >/tmp/vera_wg_up.log 2>&1) && echo VERA_WG_UP "
                  "|| echo VERA_WG_UPFAIL; fi")
        # systemctl enable --now on a fresh install has taken over two minutes;
        # if the channel gives up first, the interface is checked directly
        # rather than reporting a tunnel that came up a moment later as down.
        r = await _ssh(host_id, _root_wrap(script), timeout=300)
        out, err = (r.get("stdout") or ""), (r.get("stderr") or "")
        if "VERA_WG_SYNCED" not in out and "VERA_WG_UP" not in out:
            probe = await _ssh(host_id, _root_wrap(f"wg show {ifc} listen-port >/dev/null 2>&1 "
                                                    f"&& echo VERA_WG_PRESENT"), timeout=60)
            if "VERA_WG_PRESENT" not in (probe.get("stdout") or ""):
                return {"ok": False,
                        "error": (err.strip() or out.strip() or "the tunnel would not come up")[:400]}
            out = "VERA_WG_UP"
        # The door allocated the address, so the member record follows it.
        member["ip"] = str(dev.get("address", "")).split("/")[0]
        member["door_device"] = dev.get("name", "")
        member["endpoint"] = dev.get("endpoint", member.get("endpoint", ""))
        return {"ok": True, "detail": ("synced" if "SYNCED" in out else "up")
                + f" as {member['door_device']} ({member['ip']})"}

    async def teardown(self, host_id: str, cfg: Dict) -> Dict:
        res = await super().teardown(host_id, cfg)
        member = (cfg.get("members") or {}).get(host_id) or {}
        name = member.get("door_device")
        if name:
            gone = await _door(cfg, "POST", f"/api/door/peer/delete/{name}")
            if gone.get("error"):
                res = dict(res or {})
                res["warning"] = (f"the tunnel is down, but netctl still lists the "
                                  f"device {name}: {gone['error']}")
        return res


_PROVIDERS: Dict[str, MeshProvider] = {
    p.name: p for p in (NetctlDoorProvider(), WireGuardProvider(), NebulaProvider())
}


def _provider(cfg: Dict) -> MeshProvider:
    return _PROVIDERS.get(cfg.get("provider", "wireguard"), _PROVIDERS["wireguard"])


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "netsec.mesh.providers",
    http_method="GET", http_path="/netsec/mesh/providers", http_tags=["netsec"],
    memory="off", silent=True,
    description="List installed mesh backends and which is active. Output: "
                "{active, providers:[{name,label,description,experimental}]}.",
)
async def cap_mesh_providers(trace_id=None) -> Dict:
    cfg = await _cfg()
    return {"active": cfg.get("provider"),
            "providers": [{"name": p.name, "label": p.label,
                           "description": p.description,
                           "experimental": p.experimental}
                          for p in _PROVIDERS.values()]}


@capability(
    "netsec.mesh.config",
    http_method="GET", http_path="/netsec/mesh/config", http_tags=["netsec"],
    memory="off", silent=True,
    description="Read the mesh config (provider, subnet, listen_port, iface, "
                "enforce). Output: {config}.",
)
async def cap_mesh_config(trace_id=None) -> Dict:
    cfg = await _cfg()
    cfg = dict(cfg)
    cfg["member_count"] = len(cfg.get("members", {}))
    cfg.pop("members", None)
    return {"config": cfg}


@capability(
    "netsec.mesh.config.save",
    http_method="POST", http_path="/netsec/mesh/config/save", http_tags=["netsec"],
    memory="off",
    description="Set mesh config. Changing the provider or subnet after members "
                "exist is refused (leave them first). Inputs: provider (str — "
                "wireguard|nebula), subnet (str CIDR), listen_port (int), iface "
                "(str), enforce (bool — when on, join refuses non-enrolled hosts). "
                "Output: {ok, config}.",
)
async def cap_mesh_config_save(provider: Optional[str] = None, subnet: str = "",
                               listen_port: Optional[int] = None, iface: str = "",
                               enforce: Optional[bool] = None, trace_id=None) -> Dict:
    cfg = await _cfg()
    has_members = bool(cfg.get("members"))
    if provider and provider != cfg["provider"]:
        if provider not in _PROVIDERS:
            return {"error": f"unknown provider: {provider}"}
        if has_members:
            return {"error": "cannot switch provider while members exist — "
                    "leave them first"}
        cfg["provider"] = provider
    if subnet and subnet != cfg["subnet"]:
        try:
            ipaddress.ip_network(subnet, strict=False)
        except Exception:
            return {"error": f"invalid subnet: {subnet}"}
        if has_members:
            return {"error": "cannot change subnet while members exist"}
        cfg["subnet"] = subnet
    if listen_port:
        cfg["listen_port"] = int(listen_port)
    if iface:
        cfg["iface"] = iface
    if enforce is not None:
        cfg["enforce"] = bool(enforce)
    await _cfg_put(cfg)
    out = {k: cfg[k] for k in _DEFAULTS if k != "members"}
    return {"ok": True, "config": out}


@capability(
    "netsec.mesh.candidates",
    http_method="GET", http_path="/netsec/mesh/candidates", http_tags=["netsec"],
    memory="off", silent=True,
    description="Exec-store SSH hosts not yet on the mesh, each flagged with "
                "whether it is enrolled (cert). Output: {candidates:[{host_id,"
                "label,host,user,enrolled,auth}]}.",
)
async def cap_mesh_candidates(trace_id=None) -> Dict:
    cfg = await _cfg()
    members = cfg.get("members", {})
    enrolled = await _enrolled_index()
    out = []
    for h in await _exec_hosts():
        hid = h.get("id")
        if not hid or hid in members:
            continue
        addr = _host_addr(h)
        auth = enrolled.get(addr, "")
        out.append({"host_id": hid, "label": h.get("label") or addr,
                    "host": addr, "user": h.get("user", ""),
                    "enrolled": auth == "cert", "auth": auth or "unknown"})
    return {"candidates": out}


@capability(
    "netsec.mesh.members",
    http_method="GET", http_path="/netsec/mesh/members", http_tags=["netsec"],
    memory="off", silent=True,
    description="Current mesh members with overlay IP, enrolment flag and the "
                "last recorded bring-up state. On the netctl door provider each "
                "member also carries what the door sees live (connected, seconds "
                "since its last handshake, device name) and the answer carries the "
                "door itself (up, endpoint, its address range). Output: {provider, "
                "subnet, enforce, members:[{host_id,label,host,ip,pubkey,endpoint,"
                "enrolled,state,door_device,connected,last_handshake_s}], door:{up,"
                "endpoint,cidr,devices,error}}.",
)
async def cap_mesh_members(trace_id=None) -> Dict:
    cfg = await _cfg()
    enrolled = await _enrolled_index()
    door: Dict[str, Any] = {}
    live: Dict[str, Dict[str, Any]] = {}
    if cfg.get("provider") == "netctl":
        seen = await _door(cfg, "GET", "/api/door/peers")
        if seen.get("error"):
            door = {"error": seen["error"]}
        else:
            live = {d.get("name", ""): d for d in seen.get("peers") or []}
            door = {"up": seen.get("up"), "enabled": seen.get("enabled"),
                    "endpoint": seen.get("endpoint"), "cidr": seen.get("cidr"),
                    "devices": len(live)}
    out = []
    for hid, m in cfg.get("members", {}).items():
        row = {**{k: m.get(k) for k in
                  ("host_id", "label", "host", "ip", "pubkey", "endpoint",
                   "state", "joined", "door_device")},
               "enrolled": enrolled.get(m.get("host", ""), "") == "cert"}
        d = live.get(m.get("door_device") or "")
        if d is not None:
            row["connected"] = bool(d.get("connected"))
            row["last_handshake_s"] = d.get("last_handshake_s")
            if not row.get("ip") and d.get("address"):
                row["ip"] = d["address"]
        elif live or door.get("up") is not None:
            row["connected"] = False
            row["door_missing"] = True     # Vera remembers it; the door does not
        out.append(row)
    subnet = door.get("cidr") or cfg["subnet"]
    return {"provider": cfg["provider"], "subnet": subnet,
            "enforce": cfg["enforce"], "listen_port": cfg["listen_port"],
            "members": out, "door": door}


async def _resolve_exec(host_id: str) -> Optional[Dict]:
    for h in await _exec_hosts():
        if h.get("id") == host_id:
            return h
    return None


@capability(
    "netsec.mesh.join",
    http_method="POST", http_path="/netsec/mesh/join", http_tags=["netsec"],
    memory="off",
    description="Pull a host onto the encrypted mesh: install the active "
                "provider over SSH, generate its transport identity ON the host, "
                "allocate an overlay IP, register the peer and resync every "
                "member so they learn it. Enrolment (step-ca cert) is checked; "
                "in enforce mode a non-enrolled host is refused, otherwise it is "
                "joined with a warning (tolerant). Inputs: host_id (str! — an "
                "exec SSH host), endpoint (str — override host:port peers dial; "
                "default <host>:<listen_port>). Output: {ok, member, sync}.",
)
async def cap_mesh_join(host_id: str = "", endpoint: str = "", trace_id=None) -> Dict:
    if not host_id:
        return {"error": "host_id required"}
    cfg = await _cfg()
    rec = await _resolve_exec(host_id)
    if not rec:
        return {"error": f"unknown exec SSH host: {host_id}"}
    addr = _host_addr(rec)
    enrolled = (await _enrolled_index()).get(addr, "") == "cert"
    if not enrolled and cfg.get("enforce"):
        return {"error": "host is not enrolled (no step-ca cert) and enforce is "
                "on — enrol it first (Provision → Enroll) or turn enforce off"}

    prov = _provider(cfg)
    member = cfg["members"].get(host_id) or {
        "host_id": host_id, "label": rec.get("label") or addr, "host": addr,
        # A door hands out the address itself; apply() fills it in.
        "ip": "" if prov.assigns_addresses else _alloc_ip(cfg),
        "endpoint": endpoint or f"{addr}:{cfg['listen_port']}",
        "joined": now_iso(),
    }
    if endpoint:
        member["endpoint"] = endpoint

    ins = await prov.ensure_installed(host_id, cfg)
    if not ins.get("ok"):
        return {"error": ins.get("error", "install failed")}
    ident = await prov.gen_identity(host_id, cfg, member)
    if not ident.get("ok"):
        return {"error": ident.get("error", "identity failed")}
    member["pubkey"] = ident["pubkey"]
    member["state"] = "identity"
    member["enrolled"] = enrolled
    cfg["members"][host_id] = member
    await _cfg_put(cfg)

    sync = await _sync_all(cfg)
    cfg = await _cfg()   # _sync_all persisted per-member state
    await emit_event({"type": "netsec.mesh.joined", "host_id": host_id,
                      "ip": member["ip"], "provider": cfg["provider"],
                      "enrolled": enrolled})
    return {"ok": True, "member": cfg["members"].get(host_id),
            "enrolled": enrolled,
            "warning": None if enrolled else "joined but NOT enrolled — traffic "
            "is encrypted, but this device has no step-ca identity",
            "sync": sync}


async def _sync_all(cfg: Dict, only: str = "") -> List[Dict]:
    """(Re)render + push the peer set to every member (or one). Persists each
    member's resulting state. Tolerant: one member failing doesn't abort the
    rest."""
    prov = _provider(cfg)
    peers = list(cfg["members"].values())
    results = []
    for hid, m in list(cfg["members"].items()):
        if only and hid != only:
            continue
        try:
            res = await prov.apply(hid, cfg, m, peers)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        m["state"] = "up" if res.get("ok") else "error"
        m["last_error"] = "" if res.get("ok") else res.get("error", "")
        results.append({"host_id": hid, "label": m.get("label"),
                        "ok": res.get("ok"), "detail": res.get("detail"),
                        "error": res.get("error")})
    await _cfg_put(cfg)
    return results


@capability(
    "netsec.mesh.sync",
    http_method="POST", http_path="/netsec/mesh/sync", http_tags=["netsec"],
    memory="off",
    description="Re-render and hot-reload the peer set on mesh members (no "
                "restart — WireGuard syncconf). Run after membership changes or "
                "if a peer drifted. Inputs: host_id (str — one member; default "
                "all). Output: {ok, results:[{host_id,ok,detail,error}]}.",
)
async def cap_mesh_sync(host_id: str = "", trace_id=None) -> Dict:
    cfg = await _cfg()
    if not cfg.get("members"):
        return {"ok": True, "results": [], "note": "no members"}
    results = await _sync_all(cfg, only=host_id)
    # Keep the network graph current: mesh membership/topology just changed, so
    # (best-effort) re-map the overlay into the netmap graph. Never fail the sync
    # if the graph backend is down.
    try:
        ing = _cap("netmap.mesh.ingest")
        if ing:
            await ing(trace_id=trace_id)
    except Exception as e:
        log.debug("mesh sync → netmap ingest skipped: %s", e)
    return {"ok": all(r["ok"] for r in results) if results else True,
            "results": results}


@capability(
    "netsec.mesh.status",
    http_method="GET", http_path="/netsec/mesh/status", http_tags=["netsec"],
    memory="off", silent=True,
    description="Live per-member mesh state: interface up? peers, last-handshake "
                "age, rx/tx. Tolerant — a member that's enrolled but never "
                "handshaken is surfaced as a warning, not a failure. Output: "
                "{provider, members:[{host_id,label,ip,up,peer_count,peers,"
                "error}]}.",
)
async def cap_mesh_status(trace_id=None) -> Dict:
    cfg = await _cfg()
    prov = _provider(cfg)
    out = []
    for hid, m in cfg.get("members", {}).items():
        try:
            st = await prov.status(hid, cfg)
        except Exception as e:
            st = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        peers = st.get("peers", []) if st.get("ok") else []
        handshaken = [p for p in peers if p.get("handshake_age_s") is not None
                      and p["handshake_age_s"] < 180]
        out.append({
            "host_id": hid, "label": m.get("label"), "ip": m.get("ip"),
            "up": bool(st.get("up")), "peer_count": len(peers),
            "handshaken": len(handshaken), "peers": peers,
            "state": m.get("state"), "error": st.get("error") or m.get("last_error"),
        })
    return {"provider": cfg["provider"], "subnet": cfg["subnet"],
            "enforce": cfg["enforce"], "members": out}


@capability(
    "netsec.mesh.leave",
    http_method="POST", http_path="/netsec/mesh/leave", http_tags=["netsec"],
    memory="off",
    description="Remove a host from the mesh: tear the interface down on it and "
                "resync the remaining members so they drop the peer. Inputs: "
                "host_id (str!). Output: {ok, torn_down, sync}.",
)
async def cap_mesh_leave(host_id: str = "", trace_id=None) -> Dict:
    cfg = await _cfg()
    if host_id not in cfg.get("members", {}):
        return {"error": f"not a member: {host_id}"}
    prov = _provider(cfg)
    try:
        td = await prov.teardown(host_id, cfg)
    except Exception as e:
        td = {"ok": False, "error": str(e)}
    cfg["members"].pop(host_id, None)
    await _cfg_put(cfg)
    cfg = await _cfg()
    sync = await _sync_all(cfg)
    await emit_event({"type": "netsec.mesh.left", "host_id": host_id})
    return {"ok": True, "torn_down": td.get("ok"), "sync": sync}


@capability(
    "netsec.mesh.gateway",
    http_method="POST", http_path="/netsec/mesh/gateway", http_tags=["netsec"],
    memory="off",
    description="Designate a mesh MEMBER as a GATEWAY that advertises one or more LAN "
                "subnets (routes) to the rest of the mesh: other members route those "
                "subnets through it, and the gateway host enables ip_forward + masquerade. "
                "This lets mesh nodes reach a whole LAN (e.g. the Vera stack's "
                "192.168.0.0/24) via ONE on-LAN member, without putting every service host "
                "on the mesh. Pass routes=[] to clear the gateway role. Inputs: host_id "
                "(str! — an existing mesh member), routes (list[str] CIDRs). Output: {ok, "
                "member, applied, sync}.",
)
async def cap_mesh_gateway(host_id: str = "", routes=None, trace_id=None) -> Dict:
    cfg = await _cfg()
    m = cfg.get("members", {}).get(host_id)
    if not m:
        return {"error": f"not a mesh member: {host_id} — join it first (netsec.mesh.join)"}
    routes = [str(r).strip() for r in (routes or []) if str(r).strip()]
    m["routes"] = routes
    cfg["members"][host_id] = m
    await _cfg_put(cfg)
    # apply ip_forward + masquerade ON the gateway host now (PostUp also persists it on
    # the next wg-quick up); clearing routes removes the masquerade rule.
    ifc = cfg.get("iface", "vera0")
    sub = cfg.get("subnet", "")
    script = wg_gateway_postup(sub, ifc) if routes else wg_gateway_postdown(sub, ifc)
    applied = ""
    try:
        r = await _ssh(host_id, _root_wrap(script), timeout=30)
        applied = ((r.get("stdout") or "") + (r.get("stderr") or "")).strip()[-200:]
    except Exception as e:
        applied = f"{type(e).__name__}: {e}"
    cfg = await _cfg()
    sync = await _sync_all(cfg)   # re-render peers so they learn (or drop) the advertised route
    await emit_event({"type": "netsec.mesh.gateway", "host_id": host_id, "routes": routes})
    return {"ok": True, "member": (await _cfg())["members"].get(host_id),
            "routes": routes, "applied": applied, "sync": sync}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/netsec/panel", include_in_schema=False)
async def _netsec_panel():
    p = _HERE / "netsec_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>netsec_panel.html not found</p>")


log.info("netsec_capabilities ready — mesh providers: %s", ", ".join(_PROVIDERS))


@capability(
    "netsec.mesh.enroll_token",
    http_method="POST", http_path="/netsec/mesh/enroll_token", http_tags=["netsec"],
    memory="off",
    description="Get or set the shared mesh SELF-ENROL token that gates netsec.mesh.enroll. "
                "generate=true mints a fresh one; token=<str> sets it; no args returns the "
                "current token (minting one if absent). Output: {ok, enroll_token}.",
)
async def cap_mesh_enroll_token(token: str = "", generate: bool = False, trace_id=None) -> Dict:
    cfg = await _cfg()
    cur = cfg.get("enroll_token", "")
    if generate or (not token and not cur):
        import secrets as _secrets
        token = _secrets.token_urlsafe(24)
    if token:
        cfg["enroll_token"] = token
        await _cfg_put(cfg)
    return {"ok": True, "enroll_token": cfg.get("enroll_token", "")}


@capability(
    "netsec.mesh.enroll",
    http_method="POST", http_path="/netsec/mesh/enroll", http_tags=["netsec"],
    memory="off",
    description="SELF-ENROL a node onto the mesh (node-initiated AUTO-JOIN). The node "
                "generates its own WireGuard key and POSTs its PUBLIC key + the shared enrol "
                "token; Vera allocates an overlay IP, registers the peer, resyncs the mesh and "
                "returns the node's wg config to bring up (the node's private key never leaves "
                "it). Token-gated (netsec.mesh.enroll_token). Inputs: pubkey (str!), token "
                "(str!), host (str -- node LAN IP, used to filter gateway routes), label (str), "
                "endpoint (str). Output: {ok, ip, iface, subnet, listen_port, host_id, conf}.",
)
async def cap_mesh_enroll(pubkey: str = "", token: str = "", host: str = "",
                          label: str = "", endpoint: str = "", trace_id=None) -> Dict:
    pubkey = (pubkey or "").strip()
    if not pubkey:
        return {"error": "pubkey required (node runs `wg genkey | wg pubkey`)"}
    cfg = await _cfg()
    want = cfg.get("enroll_token", "")
    if not want:
        return {"error": "self-enrol is disabled -- mint a token first (netsec.mesh.enroll_token)"}
    if (token or "").strip() != want:
        return {"error": "invalid enrol token"}
    hid = None
    for k, m in cfg["members"].items():
        if m.get("pubkey") == pubkey:
            hid = k
            break
    if not hid:
        hid = "self-" + pubkey.replace("/", "").replace("+", "")[:16]
    member = cfg["members"].get(hid) or {
        "host_id": hid, "label": label or host or hid, "host": host or "",
        "ip": _alloc_ip(cfg), "endpoint": endpoint or "", "joined": now_iso(),
    }
    member["pubkey"] = pubkey
    member["state"] = "self-enrolled"
    member["enrolled"] = False
    if host:
        member["host"] = host
    if endpoint:
        member["endpoint"] = endpoint
    cfg["members"][hid] = member
    await _cfg_put(cfg)
    sync = await _sync_all(cfg)
    cfg = await _cfg()
    peers = [m for k, m in cfg["members"].items() if k != hid]
    conf = wg_client_config(member["ip"], cfg.get("listen_port", 51820), peers, client_host=host)
    await emit_event({"type": "netsec.mesh.enrolled", "host_id": hid, "ip": member["ip"]})
    return {"ok": True, "ip": member["ip"], "iface": cfg.get("iface", "vera0"),
            "subnet": cfg["subnet"], "listen_port": cfg.get("listen_port", 51820),
            "host_id": hid, "conf": conf}
