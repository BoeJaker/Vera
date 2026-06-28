"""
enroll_capabilities.py — Vera Security Suite · SSH creds + agentless enrolment
==============================================================================

Two related concerns for onboarding an *already-established* Proxmox estate:

  1. Per-host SSH credentials (sealed in Redis). Auth modes, preferred order:
     cert (step-ca SSH user cert) → key (sealed private key) → password (sealed).
  2. Agentless enrolment: Vera SSHes into a guest with bootstrap creds and runs a
     script that (a) trusts the step-ca roots, (b) installs the step-ca SSH *user*
     CA so the box accepts cert logins, (c) requests a TLS cert, then Vera
     registers the host in FreeIPA/DNS. Works on existing and brand-new guests.

Everything is decoupled through the capability registry: Proxmox discovery via
`proxmox.status`, SSH execution via `exec.ssh.run`, identity via
`identity.host.register`. step-ca / OpenBao config is read from the provisioning
module's state (`vera:provisioning:state`).

Capabilities
────────────
  ssh.host.save / .list / .delete / .test   — per-host SSH credential store
  enroll.discover                           — guests + their enrolment state
  enroll.guest                              — the agentless SSH-push enrolment
  enroll.script                             — preview the enrolment script only

Redis layout
────────────
  vera:provisioning:ssh_hosts   hash  id -> JSON   (password / private_key sealed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, now_iso
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.enroll")
_HERE = Path(__file__).parent

KEY_HOSTS = "vera:provisioning:ssh_hosts"
KEY_PROV_STATE = "vera:provisioning:state"      # owned by provisioning_capabilities
_SECRET_FIELDS = ("password", "private_key")


def _redis():
    return getattr(_orch, "REDIS", None)


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


async def _prov_state() -> Dict[str, Any]:
    """Read the provisioning suite's step-ca / OpenBao config (secrets stay sealed
    here; open individually with vsecrets.open_secret when needed)."""
    r = _redis()
    if not r:
        return {}
    raw = await r.hget(KEY_PROV_STATE, "main")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


# ── Vera's own SSH identity (step-ca user cert) ───────────────────────────────
def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _vera_ssh_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), ".vera", "ssh")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _vera_key_path() -> str:
    # asyncssh auto-loads the matching "<key>-cert.pub" when this key is used.
    return os.path.join(_vera_ssh_dir(), "id_vera")


async def _sh(args: List[str], timeout: int = 60) -> Tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except Exception as ex:
        return 1, "", f"{type(ex).__name__}: {ex}"


async def _auth_for(rec: Dict) -> Tuple[str, str, List[str]]:
    """(password, key_path, tmpfiles) for an OPENED host record, honouring its
    auth mode: cert → Vera's user cert key; key → sealed key to a temp file;
    password → sealed password."""
    auth = rec.get("auth", "password")
    if auth == "cert" and os.path.exists(_vera_key_path()):
        return "", _vera_key_path(), []
    if auth == "key" and rec.get("private_key"):
        tf = tempfile.NamedTemporaryFile("w", delete=False, suffix=".key")
        tf.write(rec["private_key"]); tf.close()
        try:
            os.chmod(tf.name, 0o600)
        except Exception:
            pass
        return "", tf.name, [tf.name]
    return rec.get("password", ""), "", []


# ═════════════════════════════════════════════════════════════════════════════
#  PER-HOST SSH CREDENTIAL STORE
# ═════════════════════════════════════════════════════════════════════════════
async def _hosts_raw() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    items = await r.hgetall(KEY_HOSTS)
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    return out


def _redact(rec: Dict) -> Dict:
    out = {}
    for k, v in rec.items():
        if k in _SECRET_FIELDS:
            out[f"has_{k}"] = bool(v)
        else:
            out[k] = v
    return out


def _open(rec: Dict) -> Dict:
    out = dict(rec)
    for f in _SECRET_FIELDS:
        out[f] = vsecrets.open_secret(rec.get(f, ""))
    return out


async def _get_host(host_id: str, opened: bool = False) -> Optional[Dict]:
    r = _redis()
    if not r or not host_id:
        return None
    raw = await r.hget(KEY_HOSTS, host_id)
    if not raw:
        return None
    rec = json.loads(raw)
    return _open(rec) if opened else rec


@capability(
    "ssh.host.save",
    http_method="POST", http_path="/enroll/ssh/host/save", http_tags=["enroll"],
    memory="off",
    description="Create/update per-host SSH credentials. Secrets (password, "
                "private_key) are SEALED (Fernet); blank keeps existing. Inputs: "
                "id (str — omit to create), label, host (str!), port (int 22), "
                "user (str!), auth ('cert'|'key'|'password'), password, "
                "private_key, public_key, guest_ref (str — 'cluster:vmid' link). "
                "Output: {ok, host(redacted)}.",
)
async def cap_host_save(
    id: str = "", label: str = "", host: str = "", port: int = 22, user: str = "",
    auth: str = "password", password: str = "", private_key: str = "",
    public_key: str = "", guest_ref: str = "", trace_id=None,
) -> Dict:
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    existing = await _get_host(id) if id else None
    rec = dict(existing) if existing else {"id": str(uuid.uuid4()), "created": now_iso()}
    for k, v in (("label", label), ("host", host), ("port", int(port or 22)),
                 ("user", user), ("auth", auth), ("public_key", public_key),
                 ("guest_ref", guest_ref)):
        if v != "" or k not in rec:
            rec[k] = v
    if not rec.get("label"):
        rec["label"] = host or user or "host"
    try:
        if password:
            rec["password"] = vsecrets.seal(password)
        if private_key:
            rec["private_key"] = vsecrets.seal(private_key)
    except RuntimeError as e:
        return {"error": str(e)}
    rec["updated"] = now_iso()
    await r.hset(KEY_HOSTS, rec["id"], json.dumps(rec))
    return {"ok": True, "host": _redact(rec)}


@capability(
    "ssh.host.list",
    http_method="GET", http_path="/enroll/ssh/host/list", http_tags=["enroll"],
    memory="off", silent=True,
    description="List per-host SSH credentials (REDACTED — has_password / "
                "has_private_key). Output: {hosts:[...]}.",
)
async def cap_host_list(trace_id=None) -> Dict:
    return {"hosts": [_redact(h) for h in await _hosts_raw()]}


@capability(
    "ssh.host.delete",
    http_method="POST", http_path="/enroll/ssh/host/delete", http_tags=["enroll"],
    memory="off", description="Delete per-host SSH creds. Input: id (str!).",
)
async def cap_host_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    await r.hdel(KEY_HOSTS, id)
    return {"ok": True}


async def _run_ssh(host: str, user: str, *, password: str = "", key_path: str = "",
                   port: int = 22, command: str, timeout: int = 120) -> Dict:
    ssh = _cap("exec.ssh.run")
    if not ssh:
        return {"ok": False, "error": "exec.ssh.run unavailable"}
    return await ssh(command=command, host=host, user=user, password=password,
                     key_path=key_path, port=int(port or 22), timeout=timeout) or {}


@capability(
    "ssh.host.test",
    http_method="POST", http_path="/enroll/ssh/host/test", http_tags=["enroll"],
    memory="off",
    description="Test SSH connectivity. Inputs: id (str — stored host) OR inline "
                "host/user/password/key_path/port. Output: {ok, stdout, error}.",
)
async def cap_host_test(id: str = "", host: str = "", user: str = "",
                        password: str = "", key_path: str = "", port: int = 22,
                        trace_id=None) -> Dict:
    tmp: List[str] = []
    if id:
        rec = await _get_host(id, opened=True)
        if not rec:
            return {"ok": False, "error": "host not found"}
        host, user, port = rec.get("host", ""), rec.get("user", ""), rec.get("port", 22)
        password, key_path, tmp = await _auth_for(rec)
    if not host or not user:
        return {"ok": False, "error": "host and user required"}
    try:
        res = await _run_ssh(host, user, password=password, key_path=key_path,
                             port=port, command="echo vera-ok && hostname", timeout=20)
    finally:
        for f in tmp:
            try:
                os.remove(f)
            except Exception:
                pass
    return {"ok": bool(res.get("ok")), "via": "cert" if (key_path == _vera_key_path()) else None,
            "stdout": res.get("stdout", ""),
            "error": res.get("error") or res.get("stderr", "")}


@capability(
    "ssh.cert.mint",
    http_method="POST", http_path="/enroll/ssh/cert/mint", http_tags=["enroll"],
    memory="off",
    description="Mint Vera's own SSH *user* certificate from step-ca's SSH user CA "
                "so Vera logs into enrolled hosts cert-only (no stored password). "
                "Requires the `step` CLI on the Vera host + step-ca configured "
                "(Security tab). Inputs: principal (str='vera'), force (bool). "
                "Output: {ok, key_path, cert_path} or {need_step_cli, public_key} "
                "so you can sign it externally.",
)
async def cap_ssh_cert_mint(principal: str = "vera", force: bool = False,
                            trace_id=None) -> Dict:
    state = await _prov_state()
    url = state.get("stepca_url", "")
    fp = state.get("stepca_fingerprint", "")
    prov = state.get("stepca_provisioner", "")
    pw = vsecrets.open_secret(state.get("stepca_provisioner_password", ""))
    key = _vera_key_path(); pub = key + ".pub"; cert = key + "-cert.pub"

    if not _have("step"):
        if _have("ssh-keygen") and (force or not os.path.exists(key)):
            for p in (key, pub, cert):
                try:
                    os.path.exists(p) and os.remove(p)
                except Exception:
                    pass
            await _sh(["ssh-keygen", "-t", "ed25519", "-f", key, "-N", "", "-q", "-C", "vera"])
        pk = ""
        try:
            pk = open(pub).read().strip()
        except Exception:
            pass
        return {"ok": False, "need_step_cli": True, "public_key": pk, "key_path": key,
                "hint": "Install the step CLI on the Vera host, or sign this public "
                        "key with step-ca's SSH user CA and drop the cert at " + cert}

    if not url:
        return {"error": "step-ca not configured — set stepca_url in the Security tab"}
    if force:
        for p in (key, pub, cert):
            try:
                os.path.exists(p) and os.remove(p)
            except Exception:
                pass
    if fp:
        await _sh(["step", "ca", "bootstrap", "--ca-url", url,
                   "--fingerprint", fp, "--force"], timeout=30)
    args = ["step", "ssh", "certificate", principal, key,
            "--force", "--no-password", "--insecure"]
    if prov:
        args += ["--provisioner", prov]
    pwfile = None
    if pw:
        tf = tempfile.NamedTemporaryFile("w", delete=False, suffix=".pw")
        tf.write(pw); tf.close(); pwfile = tf.name
        args += ["--provisioner-password-file", pwfile]
    rc, out, err = await _sh(args, timeout=90)
    if pwfile:
        try:
            os.remove(pwfile)
        except Exception:
            pass
    if rc != 0:
        return {"error": "step ssh certificate failed: " + (err or out)[:300]}
    try:
        os.chmod(key, 0o600)
    except Exception:
        pass
    await emit_event({"type": "ssh.cert.minted", "principal": principal})
    return {"ok": True, "key_path": key, "cert_path": cert, "principal": principal}


@capability(
    "ssh.cert.status",
    http_method="GET", http_path="/enroll/ssh/cert/status", http_tags=["enroll"],
    memory="off", silent=True,
    description="Report Vera's SSH user-cert state. Output: {has_key, has_cert, "
                "key_path, detail (ssh-keygen -L)}.",
)
async def cap_ssh_cert_status(trace_id=None) -> Dict:
    key = _vera_key_path(); cert = key + "-cert.pub"
    out = {"has_key": os.path.exists(key), "has_cert": os.path.exists(cert),
           "key_path": key, "step_cli": _have("step")}
    if out["has_cert"] and _have("ssh-keygen"):
        rc, o, e = await _sh(["ssh-keygen", "-L", "-f", cert])
        out["detail"] = (o or e or "")[:700]
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  ENROLMENT
# ═════════════════════════════════════════════════════════════════════════════
async def _ssh_user_ca(state: Dict) -> str:
    """Fetch step-ca's SSH *user* CA public key (so the guest accepts cert logins).
    Best-effort: returns '' when step-ca has no SSH CA / is unreachable."""
    url = (state.get("stepca_url") or "").rstrip("/")
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10, verify=bool(state.get("verify_tls"))) as c:
            r = await c.get(url + "/ssh/roots")
            if r.status_code >= 400:
                return ""
            d = r.json() or {}
            keys = d.get("userKey") or d.get("UserKey") or []
            if isinstance(keys, str):
                keys = [keys]
            return keys[0] if keys else ""
    except Exception:
        return ""


def _enroll_script(state: Dict, fqdn: str, ssh_user_ca: str) -> str:
    """The agentless enrolment payload run on the guest (Debian/RHEL-ish)."""
    url = state.get("stepca_url", "")
    fp = state.get("stepca_fingerprint", "")
    prov = state.get("stepca_provisioner", "")
    lines = [
        "set -e",
        "mkdir -p /etc/vera/certs",
        # 1. Trust step-ca roots (TLS)
        f"(curl -sSk {url}/roots.pem -o /usr/local/share/ca-certificates/step-ca.crt "
        "&& update-ca-certificates) 2>/dev/null || true",
        # 2. step CLI bootstrap (no-op if step absent)
        f"command -v step >/dev/null && step ca bootstrap --ca-url {url} "
        f"--fingerprint {fp} --force --install 2>/dev/null || true",
    ]
    # 3. SSH user-CA trust → enable certificate logins
    if ssh_user_ca:
        ca = ssh_user_ca.replace("'", "")
        lines += [
            "mkdir -p /etc/ssh",
            f"echo '{ca}' > /etc/ssh/vera_user_ca.pub",
            "grep -q vera_user_ca /etc/ssh/sshd_config || "
            "echo 'TrustedUserCAKeys /etc/ssh/vera_user_ca.pub' >> /etc/ssh/sshd_config",
            "(systemctl reload sshd || systemctl reload ssh || service ssh reload) 2>/dev/null || true",
        ]
    # 4. TLS certificate
    cert_cmd = (f"command -v step >/dev/null && step ca certificate {fqdn} "
                f"/etc/vera/certs/{fqdn}.crt /etc/vera/certs/{fqdn}.key --force")
    if prov:
        cert_cmd += f" --provisioner {prov}"
    lines.append(cert_cmd + " 2>/dev/null || true")
    lines.append("echo VERA_ENROLL_DONE")
    return " && ".join(lines)


@capability(
    "enroll.script",
    http_method="POST", http_path="/enroll/script", http_tags=["enroll"],
    memory="off", silent=True,
    description="Preview the agentless enrolment script for an FQDN (no exec). "
                "Input: fqdn (str!). Output: {script, ssh_user_ca_present}.",
)
async def cap_enroll_script(fqdn: str = "", trace_id=None) -> Dict:
    state = await _prov_state()
    ca = await _ssh_user_ca(state)
    return {"script": _enroll_script(state, fqdn or "host.local", ca),
            "ssh_user_ca_present": bool(ca)}


@capability(
    "enroll.discover",
    http_method="POST", http_path="/enroll/discover", http_tags=["enroll"],
    memory="off", silent=True,
    description="List a cluster's guests annotated with enrolment state (whether "
                "Vera holds SSH creds / a cert for them). Input: cluster_id (str). "
                "Output: {guests:[{vmid,name,type,node,status,enrolled,host_id}]}.",
)
async def cap_discover(cluster_id: str = "", trace_id=None) -> Dict:
    status = _cap("proxmox.status")
    if not status:
        return {"error": "proxmox module not loaded", "guests": []}
    if not cluster_id:
        clist = _cap("proxmox.cluster.list")
        cs = (await clist()).get("clusters", []) if clist else []
        cluster_id = cs[0]["id"] if cs else ""
    if not cluster_id:
        return {"error": "no cluster configured", "guests": []}
    snap = await status(cluster_id=cluster_id)
    hosts = await _hosts_raw()
    by_ref = {h.get("guest_ref"): h for h in hosts if h.get("guest_ref")}
    guests = []
    for g in snap.get("guests", []):
        if g.get("template"):
            continue
        ref = f"{cluster_id}:{g['vmid']}"
        h = by_ref.get(ref)
        guests.append({
            "vmid": g["vmid"], "name": g.get("name", ""), "type": g["type"],
            "node": g["node"], "status": g["status"],
            "enrolled": bool(h), "host_id": h.get("id", "") if h else "",
            "auth": h.get("auth", "") if h else "",
        })
    return {"cluster_id": cluster_id, "guests": guests}


@capability(
    "enroll.guest",
    http_method="POST", http_path="/enroll/guest", http_tags=["enroll"],
    memory="off",
    description="Agentless SSH-push enrolment of one guest: SSH in with bootstrap "
                "creds, install step-ca trust + SSH user-CA (cert logins) + TLS "
                "cert, register in FreeIPA/DNS, and save per-host SSH creds. "
                "Inputs: cluster_id, vmid (int), guest_type ('qemu'|'lxc'), node, "
                "fqdn (str! — or name+base_domain), ip (str!), ssh_user (str!), "
                "ssh_password (str) OR ssh_key_path (str), ssh_port (int 22). "
                "Output: {ok, fqdn, steps:{ssh,script,identity}, host_id}.",
)
async def cap_enroll_guest(
    cluster_id: str = "", vmid: int = 0, guest_type: str = "", node: str = "",
    fqdn: str = "", ip: str = "", ssh_user: str = "root", ssh_password: str = "",
    ssh_key_path: str = "", ssh_port: int = 22, trace_id=None,
) -> Dict:
    if not ip or not ssh_user:
        return {"error": "ip and ssh_user required"}
    if not fqdn:
        return {"error": "fqdn required"}
    state = await _prov_state()
    ca = await _ssh_user_ca(state)
    script = _enroll_script(state, fqdn, ca)
    steps: Dict[str, Any] = {}

    # 1. Push + run the enrolment script over SSH (bootstrap creds).
    res = await _run_ssh(ip, ssh_user, password=ssh_password, key_path=ssh_key_path,
                         port=ssh_port, command=script, timeout=180)
    steps["ssh"] = {"ok": bool(res.get("ok")), "error": res.get("error", ""),
                    "stderr": (res.get("stderr", "") or "")[:400]}
    out = res.get("stdout", "") or ""
    steps["script"] = {"reached_end": "VERA_ENROLL_DONE" in out,
                       "ssh_user_ca_installed": bool(ca),
                       "tail": out[-600:]}

    # 2. Persist per-host SSH creds for future cert/key/password logins.
    save = await cap_host_save(
        label=fqdn, host=ip, port=ssh_port, user=ssh_user,
        auth="cert" if ca else ("key" if ssh_key_path else "password"),
        password=ssh_password if not ca and not ssh_key_path else "",
        guest_ref=f"{cluster_id}:{vmid}" if cluster_id and vmid else "",
    )
    host_id = (save.get("host") or {}).get("id", "")

    # 3. Register in FreeIPA + DNS (best-effort, decoupled).
    reg = _cap("identity.host.register")
    if reg:
        try:
            steps["identity"] = await reg(fqdn=fqdn, ip=ip)
        except Exception as e:
            steps["identity"] = {"error": str(e)}
    else:
        steps["identity"] = {"skipped": "identity module not loaded"}

    await emit_event({"type": "enroll.guest.done", "fqdn": fqdn, "ip": ip,
                      "ok": steps["ssh"]["ok"]})
    return {"ok": steps["ssh"]["ok"], "fqdn": fqdn, "host_id": host_id, "steps": steps}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL  (embedded as the Enroll sub-tab of the workers Proxmox pane — no tab)
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/enroll/panel", include_in_schema=False)
async def _enroll_panel():
    p = _HERE / "enroll_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>enroll_panel.html not found</p>")


log.info("enroll_capabilities ready")
