"""Which running containers are no longer running the image they were built from.

A container pins the image ID it was created with. Rebuilding or re-pulling
`vera:latest` moves the TAG; every container already running keeps the old
layers until it is RECREATED. Restarting is not enough - `docker restart` reuses
the same container, and therefore the same image.

That is how an instance ends up weeks behind while looking healthy, which is
exactly what happened on 2026-08-31: a foundry-provisioned worker ran code from
before 2026-08-30 for days, sweeping the shared estate with pre-fix logic, and
nothing surfaced it. It was found by noticing that its audit lines were in a
summary format the current source cannot emit.

LOOP LAB SANDBOXES ARE EXCLUDED ON PURPOSE. They are short-lived and version
SENSITIVE: a per-branch sandbox exists precisely to run one branch's code, which
is normally NOT the latest image, and rolling them forward would destroy the
thing they were spawned to test. Their own lifecycle (spawn, pin, idle-pause,
down) already handles them.

This module only DECIDES. It does not restart anything: recreating a container
needs its full run configuration (ports, env, volumes, network), which docker
does not hand back in a form that can be replayed safely, so the actual roll
forward belongs with whatever created the container - compose for the stack,
docker.worker.spawn for workers.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

#: Containers whose version is deliberately not "latest".
EXCLUDED_PREFIXES = (
    "vera-dev-",    # Loop Lab per-branch sandboxes - pinned to a branch on purpose
    "vera-sbx-",    # ephemeral session sandboxes
)

#: Infrastructure that is not a Vera build at all (its own release cadence).
NOT_VERA = ("vera-registry", "vera-opa", "vera-lldap", "vera-step-ca",
            "vera-openbao", "vera-garage")


def _name(container: Mapping[str, Any]) -> str:
    names = container.get("Names") or container.get("names") or []
    if isinstance(names, str):
        names = [names]
    return str((names or [""])[0]).lstrip("/")


def is_excluded(name: str, extra: Iterable[str] = ()) -> bool:
    n = str(name or "").lstrip("/")
    if not n:
        return True
    if n in NOT_VERA:
        return True
    prefixes = tuple(EXCLUDED_PREFIXES) + tuple(extra or ())
    return any(n.startswith(p) for p in prefixes)


def classify(containers: Optional[Iterable[Mapping[str, Any]]],
             image_ids_by_tag: Optional[Mapping[str, str]],
             extra_excluded: Iterable[str] = ()) -> Dict[str, Any]:
    """Which running containers are behind the image their tag now points at.

    `image_ids_by_tag` maps 'vera:latest' -> the CURRENT image id. A container
    whose recorded ImageID differs from that is running superseded layers.
    Unknown tags decide nothing: a tag we cannot resolve is not evidence of
    drift, and acting on it would recreate a container for no reason.
    """
    tags = {str(k): str(v) for k, v in (image_ids_by_tag or {}).items()}
    drifted: List[Dict[str, Any]] = []
    current: List[str] = []
    skipped: List[Dict[str, str]] = []
    unknown: List[Dict[str, str]] = []
    for c in (containers or []):
        if not isinstance(c, Mapping):
            continue
        name = _name(c)
        if not name:
            continue
        if is_excluded(name, extra_excluded):
            skipped.append({"name": name, "why": "version-pinned or not a Vera build"})
            continue
        tag = str(c.get("Image") or c.get("image") or "")
        running_id = str(c.get("ImageID") or c.get("image_id") or "")
        latest_id = tags.get(tag, "")
        if not tag or not running_id or not latest_id:
            unknown.append({"name": name, "why": "image or tag could not be resolved"})
            continue
        if running_id != latest_id:
            drifted.append({"name": name, "image": tag,
                            "running": running_id[:19], "latest": latest_id[:19]})
        else:
            current.append(name)
    return {"drifted": sorted(drifted, key=lambda r: r["name"]),
            "current": sorted(current), "skipped": skipped, "unknown": unknown}


def describe(plan: Mapping[str, Any]) -> str:
    d = list((plan or {}).get("drifted") or [])
    if not d:
        n = len(list((plan or {}).get("current") or []))
        return f"{n} container(s) on the current image, none drifted"
    names = ", ".join(r["name"] for r in d)
    return (f"{len(d)} container(s) running a superseded image: {names}. "
            "A restart will NOT pick the new image up - they must be RECREATED "
            "by whatever brought them up (compose, or docker.worker.spawn).")
