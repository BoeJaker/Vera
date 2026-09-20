"""Where a Docker host's disk has gone, from the Engine's own accounting.

docker.disk.status gave one line: the data root's size and how full it is.
VFS-02 gets a whole pane. The Engine's /system/df already splits the same
disk into images, containers, volumes and build cache, with what each item
holds and what could be reclaimed; this turns that into an answer a person
can act on: totals per category, what is reclaimable and why, the containers
whose writable layers have grown, dangling images, unused volumes, and the
session sandboxes the reaper would take.

Pure dicts in, dicts out (tests/test_docker_disk_core.py). /system/df itself is
slow on a busy host - minutes with 250 containers - which is the caller's
problem, not this module's.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

SANDBOX_PREFIXES = ("vera-sbx-", "sbxw-")
TOP_N = 12


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _name(c: Mapping[str, Any]) -> str:
    names = c.get("Names") or []
    n = names[0] if names else (c.get("Name") or c.get("Id") or "")
    return str(n).lstrip("/")


def summarize(df: Mapping[str, Any], reap: Optional[Mapping[str, Any]] = None,
              mount: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """df: the Engine's /system/df. reap: docker.disk.status's reap block (the
    session-sandbox reaper's view). mount: docker.disk.status's disk line."""
    images = list(df.get("Images") or [])
    containers = list(df.get("Containers") or [])
    volumes = list(df.get("Volumes") or [])
    build = list(df.get("BuildCache") or [])

    img_total = sum(_int(i.get("Size")) for i in images)
    img_shared = sum(_int(i.get("SharedSize")) for i in images)
    dangling = [i for i in images if not (i.get("RepoTags") or []) or i.get("RepoTags") == ["<none>:<none>"]]
    unused_images = [i for i in images if _int(i.get("Containers")) <= 0]
    img_reclaim = sum(_int(i.get("Size")) - _int(i.get("SharedSize")) for i in unused_images)

    ctr_rw = sum(_int(c.get("SizeRw")) for c in containers)
    stopped = [c for c in containers if str(c.get("State") or "").lower() not in ("running", "restarting", "paused")]
    ctr_reclaim = sum(_int(c.get("SizeRw")) for c in stopped)
    sandboxes = [c for c in containers if _name(c).startswith(SANDBOX_PREFIXES)]
    sbx_rw = sum(_int(c.get("SizeRw")) for c in sandboxes)

    vol_total = sum(_int((v.get("UsageData") or {}).get("Size")) for v in volumes)
    unused_vols = [v for v in volumes if _int((v.get("UsageData") or {}).get("RefCount")) <= 0]
    vol_reclaim = sum(_int((v.get("UsageData") or {}).get("Size")) for v in unused_vols)

    build_total = sum(_int(b.get("Size")) for b in build)
    build_reclaim = sum(_int(b.get("Size")) for b in build if not b.get("InUse"))

    top_containers = sorted(containers, key=lambda c: -_int(c.get("SizeRw")))[:TOP_N]
    top_images = sorted(images, key=lambda i: -_int(i.get("Size")))[:TOP_N]
    top_volumes = sorted(volumes, key=lambda v: -_int((v.get("UsageData") or {}).get("Size")))[:TOP_N]

    categories = [
        {"name": "images", "label": "Images", "size": img_total - img_shared, "count": len(images),
         "reclaimable": max(img_reclaim, 0),
         "why": f"{len(unused_images)} not used by any container" + (f", {len(dangling)} untagged" if dangling else "")},
        {"name": "containers", "label": "Container writable layers", "size": ctr_rw, "count": len(containers),
         "reclaimable": ctr_reclaim, "why": f"{len(stopped)} stopped"},
        {"name": "volumes", "label": "Volumes", "size": vol_total, "count": len(volumes),
         "reclaimable": vol_reclaim, "why": f"{len(unused_vols)} attached to nothing"},
        {"name": "build", "label": "Build cache", "size": build_total, "count": len(build),
         "reclaimable": build_reclaim, "why": "not in use by a build"},
    ]
    findings: List[Dict[str, str]] = []
    if mount and mount.get("pct_used") is not None and float(mount["pct_used"]) >= 85:
        findings.append({"severity": "warn", "message": f"The Docker data disk is {mount['pct_used']}% full.",
                         "detail": mount.get("note", "")})
    if reap and _int(reap.get("reapable")):
        findings.append({"severity": "info",
                         "message": f"{reap['reapable']} finished session sandboxes are older than "
                                    f"{reap.get('retain_hours', 24)} h and can be reaped.",
                         "detail": "docker.disk.reap removes them; running ones are kept"})
    if len(unused_vols) and vol_reclaim:
        findings.append({"severity": "info", "message": f"{len(unused_vols)} volumes are attached to nothing "
                                                        f"({_fmt(vol_reclaim)}).",
                         "detail": "a volume outlives the container that made it; check before pruning"})
    if dangling:
        findings.append({"severity": "info", "message": f"{len(dangling)} untagged images ({_fmt(sum(_int(i.get('Size')) - _int(i.get('SharedSize')) for i in dangling))}).",
                         "detail": "left behind by rebuilds; docker image prune removes them"})
    big = [c for c in top_containers if _int(c.get("SizeRw")) >= 1 << 30]
    if big:
        findings.append({"severity": "info", "message": f"{len(big)} containers have grown past 1 GB in their writable layer.",
                         "detail": "logs or data written inside the container instead of a volume: " + ", ".join(_name(c) for c in big[:5])})
    return {
        "layers_size": _int(df.get("LayersSize")),
        "categories": categories,
        "reclaimable_total": sum(c["reclaimable"] for c in categories),
        "sandboxes": {"count": len(sandboxes), "writable": sbx_rw,
                      "reapable": _int((reap or {}).get("reapable")), "retain_hours": (reap or {}).get("retain_hours")},
        "top_containers": [{"name": _name(c), "image": c.get("Image", ""), "state": c.get("State", ""),
                            "writable": _int(c.get("SizeRw")), "rootfs": _int(c.get("SizeRootFs"))} for c in top_containers],
        "top_images": [{"tags": i.get("RepoTags") or [], "size": _int(i.get("Size")), "shared": _int(i.get("SharedSize")),
                        "containers": _int(i.get("Containers"))} for i in top_images],
        "top_volumes": [{"name": v.get("Name", ""), "size": _int((v.get("UsageData") or {}).get("Size")),
                         "used_by": _int((v.get("UsageData") or {}).get("RefCount"))} for v in top_volumes],
        "findings": findings,
    }


def _fmt(n: Any) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
