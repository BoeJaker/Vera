"""One enrolment pipeline: the rules auto-enrol applies to every asset.

Enrolment had several entry points, each doing part of the job:
  enroll.guest            push trust, Vera's key and a TLS cert into a guest, save
                          its login, register it in the directory, join the mesh
  proxmox.guest.enroll    save a login the caller already has, nothing else
  proxmox.lxc.create      auto_enroll called enroll.guest
  Foundry post-provision  called enroll.guest
  nodes.provision         called proxmox.guest.enroll
  autoenroll.run          asked for a password even for guests Proxmox can reach
                          itself, then repeated the directory and mesh steps

Auto-enrol is now the one pipeline, and those entry points are its login step.
This module decides which steps an asset gets, in order, and why the others do
not run; autoenroll_capabilities carries them out.

Pure rules, no app imports (tests/test_enrol_pipeline_core.py).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

# Pipeline order. enroll_guest is the login step, ldap the directory record.
STEPS = ("enroll_guest", "cert", "ldap", "mesh", "apps")

# Config switch per step; the login step is always on when it applies.
SWITCHES = {"cert": "do_certs", "ldap": "do_ldap", "mesh": "do_mesh", "apps": "do_apps"}


def _items(steps: Any) -> List[str]:
    if isinstance(steps, str):
        return [s.strip() for s in steps.split(",") if s.strip()]
    return [str(s).strip() for s in (steps or []) if str(s).strip()]


def parse_steps(steps: Any) -> List[str]:
    """csv or list -> known step names in pipeline order."""
    wanted = set(_items(steps))
    return [s for s in STEPS if s in wanted]


def unknown_steps(steps: Any) -> List[str]:
    return [s for s in _items(steps) if s not in STEPS]


def guest_key(cluster_id: str, vmid: Any) -> str:
    return f"pve:{cluster_id}:{vmid}"


def is_proxmox_guest(asset: Mapping[str, Any]) -> bool:
    return asset.get("source") == "proxmox" or bool(asset.get("cluster_id") and asset.get("vmid"))


def login_method(asset: Mapping[str, Any], provided: Optional[Mapping[str, Any]] = None,
                 register_only: bool = False) -> Dict[str, Any]:
    """How the login step reaches a Proxmox guest. {method, needs}:
      register  save the login the caller supplied (proxmox.guest.enroll)
      key       SSH in with a key file
      password  SSH in with a password
      proxmox   run the enrolment inside the guest through Proxmox, which needs no
                guest credentials: always for a container, and for a VM whose
                guest agent answered (discovery found its address through it)
    `needs` names what is missing when none of these can work."""
    provided = provided or {}
    if register_only:
        return {"method": "register", "needs": []}
    if provided.get("via_proxmox"):
        return {"method": "proxmox", "needs": []}
    ip = str(asset.get("ip") or "").strip()
    container = asset.get("guest_type") == "lxc" or asset.get("kind") == "lxc"
    if provided.get("ssh_key_path") or provided.get("ssh_password"):
        method = "key" if provided.get("ssh_key_path") else "password"
        # a container's address comes from its config; a VM's needs its agent
        return {"method": method, "needs": [] if (ip or container) else ["ip"]}
    if container or ip:
        return {"method": "proxmox", "needs": []}
    return {"method": "", "needs": ["ssh_password"]}


def plan(asset: Mapping[str, Any], cfg: Mapping[str, Any], *,
         provided: Optional[Mapping[str, Any]] = None, only: Any = None,
         register_only: bool = False, skip_mesh: bool = False) -> Dict[str, Any]:
    """The pipeline for one asset: {steps:[{step, run, covered, reason}], needs, method}.

    Without `only`, the config's switches decide, and an asset that already has
    a login skips the login step. Naming steps (a caller that has just built the
    guest) runs exactly those, the login step even when a login exists, because a
    reused VMID can carry an earlier guest's login.
    `covered` marks a step the login step does itself (enroll.guest registers the
    directory record and joins the mesh), so it is not repeated."""
    explicit = only is not None and bool(_items(only))
    if explicit:
        wanted = set(parse_steps(only))
    else:
        wanted = {"enroll_guest"} | {s for s, sw in SWITCHES.items() if cfg.get(sw)}
    guest = is_proxmox_guest(asset)
    kind = asset.get("kind") or ""
    steps: List[Dict[str, Any]] = []
    needs: List[str] = []
    method = ""
    login_runs = False

    def add(step: str, run: bool, reason: str = "", covered: bool = False) -> None:
        steps.append({"step": step, "run": run, "covered": covered, "reason": reason})

    for step in STEPS:
        if step not in wanted:
            continue
        if needs:
            add(step, False, "waiting for the login step")
            continue
        if step == "enroll_guest":
            if not guest and not register_only:
                if not explicit:
                    continue                      # plain hosts arrive with their login
                add(step, False, "only Proxmox guests are enrolled this way; save an SSH login instead")
            elif asset.get("enrolled_ssh") and not explicit:
                add(step, False, "already has a saved login")
            else:
                lm = login_method(asset, provided, register_only)
                method = lm["method"]
                if lm["needs"]:
                    needs.extend(lm["needs"])
                    add(step, False, "missing " + ", ".join(lm["needs"]))
                else:
                    login_runs = True
                    add(step, True, f"via {method}")
        elif step == "cert":
            if kind == "docker":
                add(step, False, "not for Docker hosts")
            else:
                add(step, True)
        elif step == "ldap":
            if login_runs and method != "register":
                add(step, False, "done by the login step", covered=True)
            else:
                add(step, True)
        elif step == "mesh":
            if asset.get("in_mesh"):
                add(step, False, "already in the mesh")
            elif skip_mesh:
                add(step, False, "handled elsewhere")
            elif login_runs and method != "register":
                add(step, False, "done by the login step", covered=True)
            elif not asset.get("host_id") and not login_runs:
                add(step, False, "no SSH login yet")
            else:
                add(step, True)
        elif step == "apps":
            if kind == "docker":
                add(step, True)
            else:
                add(step, False, "only for Docker hosts")
    return {"steps": steps, "needs": needs, "method": method}


def scan_row(asset: Mapping[str, Any], cfg: Mapping[str, Any],
             provided: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """autoenroll.scan's view: the steps that will happen (run here or covered by
    the login step) and the credentials still needed."""
    p = plan(asset, cfg, provided=provided)
    return {"actions": [s["step"] for s in p["steps"] if s["run"] or s["covered"]],
            "needs": p["needs"], "login": p["method"]}


def step_ok(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return True
    if result.get("error"):
        return False
    return bool(result.get("ok", True))


def status_of(results: Mapping[str, Any], needs: Iterable[str] = ()) -> str:
    if list(needs):
        return "needs_cred"
    return "ok" if all(step_ok(v) for v in results.values()) else "partial"
