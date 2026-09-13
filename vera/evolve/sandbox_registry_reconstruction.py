"""Pure, fail-closed planning for rebuilding missing sandbox descriptors.

The controller registry is an index, not ownership authority.  Docker, its
compose metadata, the published port, the Vera source mount, and Git's worktree
list must all agree before a missing spawned-sandbox descriptor is eligible to
be restored.  This module performs no I/O and mutates nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePath
from typing import Any

from .sandbox_pool import DB_POOL, PORT_POOL, container_name, slug_for_branch

SCHEMA = "vera.sandbox-registry-reconstruction/v1"
CONTAINER_STATES = {"created", "restarting", "running", "removing", "paused",
                    "exited", "dead"}
def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pool_digest(pool: Mapping[str, Mapping[str, Any]]) -> str:
    """Fingerprint descriptor state without exposing it in the plan."""
    return _canonical_digest({str(key): dict(value or {})
                              for key, value in sorted((pool or {}).items())})


def compose_recovery_labels(branch: str, redis_db: int) -> dict[str, str]:
    """Labels persisted with a compose container for later reconstruction."""
    branch = _text(branch)
    if redis_db not in DB_POOL:
        raise ValueError("sandbox Redis slot is outside the controller pool")
    return {"vera.loop-lab.role": "spawned" if branch else "primary",
            "vera.loop-lab.branch": branch,
            "vera.loop-lab.redis-db": str(redis_db)}


def _compose_basename(value: str) -> str:
    return PurePath(value.replace("\\", "/")).name


def plan_registry_reconstruction(
    observations: Iterable[Mapping[str, Any]], *,
    worktrees: Iterable[Mapping[str, Any]],
    existing_pool: Mapping[str, Mapping[str, Any]],
    repo_root: str,
    existing_compose_files: Iterable[str],
) -> dict[str, Any]:
    """Return a content-addressed reconstruction plan.

    Each observation is already a bounded Docker projection.  Ambiguity blocks
    the affected container and duplicate resource claims block every candidate
    involved.  Existing descriptors are never overwritten.
    """
    root = _text(repo_root).replace("\\", "/").rstrip("/")
    wt_by_path: dict[str, list[str]] = {}
    for item in worktrees:
        path = _text(item.get("path")).replace("\\", "/").rstrip("/")
        branch = _text(item.get("branch"))
        if path and branch:
            wt_by_path.setdefault(path, []).append(branch)
    compose_files = {_compose_basename(_text(value))
                     for value in existing_compose_files if _text(value)}

    existing_resources = {"name": set(), "branch": set(), "worktree": set(),
                          "port": set(), "redis_db": set(), "slug": set()}
    for slug, descriptor in existing_pool.items():
        existing_resources["slug"].add(_text(slug))
        for key in ("name", "branch", "worktree", "port", "redis_db"):
            value = descriptor.get(key)
            if value is not None and _text(value):
                if key == "worktree":
                    value = _text(value).replace("\\", "/").rstrip("/")
                existing_resources[key].add(value)

    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for raw in observations:
        name = _text(raw.get("name"))
        reasons: list[str] = []
        labels = raw.get("labels") if isinstance(raw.get("labels"), Mapping) else {}
        env = raw.get("env") if isinstance(raw.get("env"), Mapping) else {}
        mounts = raw.get("mounts") if isinstance(raw.get("mounts"), Mapping) else {}
        ports = list(raw.get("published_ports") or [])
        if not name.startswith("vera-dev-") or name == "vera-dev":
            reasons.append("not_spawned_sandbox_name")
        if _text(raw.get("status")) not in CONTAINER_STATES:
            reasons.append("container_state_unverified")
        service = _text(labels.get("com.docker.compose.service"))
        if service != name:
            reasons.append("compose_service_mismatch")
        project = _text(labels.get("com.docker.compose.project"))
        if project != name:
            reasons.append("compose_project_mismatch")
        if _text(labels.get("vera.loop-lab.role")) != "spawned":
            reasons.append("sandbox_role_label_missing")
        working_dir = _text(labels.get("com.docker.compose.project.working_dir"))
        if working_dir.replace("\\", "/").rstrip("/") != root:
            reasons.append("compose_working_directory_mismatch")
        config_values = [part.strip() for part in
                         _text(labels.get("com.docker.compose.project.config_files")).split(",")
                         if part.strip()]
        slug_from_name = name[len("vera-dev-"):] if name.startswith("vera-dev-") else ""
        expected_compose = f"docker-compose.dev-{slug_from_name}.yml"
        matching_compose = [value for value in config_values
                            if _compose_basename(value) == expected_compose]
        if len(matching_compose) != 1 or expected_compose not in compose_files:
            reasons.append("compose_file_unverified")
        mount = _text(mounts.get("/app/Vera")).replace("\\", "/").rstrip("/")
        branches = sorted(set(wt_by_path.get(mount, [])))
        if not mount:
            reasons.append("vera_worktree_mount_missing")
        if len(branches) != 1:
            reasons.append("git_worktree_branch_ambiguous")
        branch = branches[0] if len(branches) == 1 else ""
        if branch and _text(labels.get("vera.loop-lab.branch")) != branch:
            reasons.append("branch_label_mismatch")
        slug = slug_for_branch(branch) if branch else slug_from_name
        if branch and container_name(branch) != name:
            reasons.append("branch_container_name_mismatch")
        normalized_ports = sorted({int(value) for value in ports
                                   if isinstance(value, int) or _text(value).isdigit()})
        if len(normalized_ports) != 1 or normalized_ports[0] not in PORT_POOL:
            reasons.append("published_port_unverified")
        redis_value = _text(labels.get("vera.loop-lab.redis-db"))
        redis_db = int(redis_value) if redis_value.isdigit() else None
        if redis_db not in DB_POOL:
            reasons.append("redis_db_unverified")
        if _text(env.get("VERA_IS_DEV_SANDBOX")) != "1":
            reasons.append("sandbox_marker_missing")
        descriptor = {
            "branch": branch,
            "slug": slug,
            "name": name,
            "port": normalized_ports[0] if len(normalized_ports) == 1 else None,
            "redis_db": redis_db,
            "compose": expected_compose,
            "worktree": mount,
            "owner": "unknown",
            "session_id": "",
            "ownership_source": "restored_from_observation",
            "gate_token_sha256": _text(raw.get("gate_token_sha256")),
        }
        if not re.fullmatch(r"[0-9a-f]{64}", descriptor["gate_token_sha256"]):
            reasons.append("gate_token_identity_unverified")
        for key in ("slug", "name", "branch", "worktree", "port", "redis_db"):
            if descriptor[key] in existing_resources[key]:
                reasons.append(f"existing_{key}_claim")
        target = {"name": name, "descriptor": descriptor,
                  "evidence": {"container_status": _text(raw.get("status")),
                               "compose_project": project}}
        if reasons:
            blocked.append({**target, "reasons": sorted(set(reasons))})
        else:
            candidates.append(target)

    # Resource duplication inside the proposed batch is ambiguity, not a
    # tie-break opportunity. Move every conflicting candidate to blocked.
    conflicts: set[int] = set()
    for key in ("slug", "name", "branch", "worktree", "port", "redis_db"):
        owners: dict[Any, list[int]] = {}
        for index, candidate in enumerate(candidates):
            owners.setdefault(candidate["descriptor"][key], []).append(index)
        conflicts.update(index for indexes in owners.values() if len(indexes) > 1
                         for index in indexes)
    eligible = []
    for index, candidate in enumerate(candidates):
        if index in conflicts:
            blocked.append({**candidate, "reasons": ["duplicate_resource_claim"]})
        else:
            eligible.append(candidate)

    core = {"schema": SCHEMA, "repo_root": root,
            "existing_pool_digest": pool_digest(existing_pool),
            "eligible": sorted(eligible, key=lambda item: item["name"]),
            "blocked": sorted(blocked, key=lambda item: item["name"])}
    return {**core, "digest": _canonical_digest(core), "mutated": False,
            "apply_allowed": bool(eligible)}


def descriptors_for_apply(plan: Mapping[str, Any], expected_digest: str) -> dict[str, dict]:
    """Extract exact descriptors only when the caller reviewed this same plan."""
    if plan.get("schema") != SCHEMA or not expected_digest:
        raise ValueError("reconstruction plan and expected digest are required")
    if not _text(plan.get("digest")):
        raise ValueError("reconstruction plan digest is missing")
    if not hmac.compare_digest(_text(plan.get("digest")), expected_digest):
        raise ValueError("reconstruction plan changed; review the new dry run")
    out: dict[str, dict] = {}
    for item in plan.get("eligible") or []:
        descriptor = dict(item.get("descriptor") or {})
        slug = _text(descriptor.get("slug"))
        if not slug or slug in out:
            raise ValueError("reconstruction plan contains an ambiguous slug")
        out[slug] = descriptor
    if not out:
        raise ValueError("reconstruction plan has no eligible descriptors")
    return out
