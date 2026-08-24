"""Worktree-local, revision-guarded plans for Loop Lab sandboxes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
PLAN_RELATIVE_PATH = Path(".vera-work") / "work-plan.json"
PLAN_STATUSES = {"planned", "in_progress", "blocked", "complete", "abandoned"}
STEP_STATUSES = {"pending", "in_progress", "completed", "blocked", "skipped"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def empty_plan(branch: str = "") -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "branch": branch,
            "title": "", "description": "", "status": "planned",
            "current_step": "", "owner": "", "session_id": "",
            "board_item_ids": [], "notes_refs": [], "steps": [],
            "updates": [], "created_at": "", "updated_at": ""}


def plan_path(worktree: str | Path) -> Path:
    return Path(worktree).resolve() / PLAN_RELATIVE_PATH


def load_plan(worktree: str | Path, branch: str = "") -> dict[str, Any]:
    path = plan_path(worktree)
    if not path.exists():
        return empty_plan(branch)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {**empty_plan(branch), "error": "workplan_unreadable", "detail": str(exc)[:200]}
    return {**empty_plan(branch), **data, "path": str(PLAN_RELATIVE_PATH)}


def _normalise_steps(steps: list | None) -> list[dict[str, Any]]:
    out = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(steps or []):
        if not isinstance(raw, dict):
            raise ValueError("each plan step must be an object")
        status = _text(raw.get("status") or "pending", 20)
        if status not in STEP_STATUSES:
            raise ValueError(f"invalid step status: {status}")
        title = _text(raw.get("title") or raw.get("step"), 240)
        if not title:
            raise ValueError("each plan step requires a title")
        step_id = _text(raw.get("id") or f"step-{index + 1}", 80)
        if step_id in seen_ids:
            raise ValueError(f"duplicate plan step id: {step_id}")
        seen_ids.add(step_id)
        out.append({"id": step_id,
                    "title": title, "status": status,
                    "detail": _text(raw.get("detail"), 2000),
                    "updated_at": _text(raw.get("updated_at") or _now(), 80)})
    if len(out) > 100:
        raise ValueError("plan supports at most 100 steps")
    return out


def update_plan(worktree: str | Path, *, branch: str, expected_revision: int,
                title: str | None = None, description: str | None = None,
                status: str | None = None, current_step: str | None = None,
                owner: str | None = None, session_id: str | None = None,
                board_item_ids: list | None = None, notes_refs: list | None = None,
                steps: list | None = None, update_note: str = "") -> dict[str, Any]:
    current = load_plan(worktree, branch)
    if current.get("error"):
        raise ValueError(current["error"])
    revision = int(current.get("revision", 0))
    if int(expected_revision) != revision:
        raise ValueError(f"revision_conflict: expected {expected_revision}, current {revision}")
    value = dict(current)
    value.pop("path", None)
    for key, raw, limit in (("title", title, 240), ("description", description, 4000),
                            ("current_step", current_step, 80), ("owner", owner, 120),
                            ("session_id", session_id, 160)):
        if raw is not None:
            value[key] = _text(raw, limit)
    if status is not None:
        status = _text(status, 20)
        if status not in PLAN_STATUSES:
            raise ValueError(f"invalid plan status: {status}")
        value["status"] = status
    if board_item_ids is not None:
        value["board_item_ids"] = [_text(item, 160) for item in board_item_ids[:30] if _text(item, 160)]
    if notes_refs is not None:
        value["notes_refs"] = [_text(item, 240) for item in notes_refs[:30] if _text(item, 240)]
    if steps is not None:
        value["steps"] = _normalise_steps(steps)
    step_ids = {step.get("id") for step in value.get("steps") or []}
    if value.get("current_step") and value["current_step"] not in step_ids:
        raise ValueError(f"current_step_not_found: {value['current_step']}")
    if value.get("status") == "complete":
        unfinished = [step.get("id") for step in value.get("steps") or []
                      if step.get("status") not in {"completed", "skipped"}]
        if unfinished:
            raise ValueError("complete_plan_has_unfinished_steps: " + ",".join(unfinished[:10]))
    if update_note:
        value["updates"] = ([{"at": _now(), "by": value.get("owner", ""),
                              "note": _text(update_note, 1000)}] +
                            list(value.get("updates") or []))[:50]
    now = _now()
    value.update({"schema_version": SCHEMA_VERSION, "revision": revision + 1,
                  "branch": branch, "updated_at": now,
                  "created_at": value.get("created_at") or now})
    path = plan_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".tmp-{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return {**value, "path": str(PLAN_RELATIVE_PATH)}


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    steps = plan.get("steps") or []
    done = sum(1 for step in steps if step.get("status") in {"completed", "skipped"})
    return {key: plan.get(key) for key in ("revision", "title", "description", "status",
                                           "current_step", "owner", "updated_at")} | {
        "step_count": len(steps), "completed_steps": done,
        "board_item_ids": plan.get("board_item_ids") or [],
        "notes_refs": plan.get("notes_refs") or [],
        "coordination_linked": bool((plan.get("board_item_ids") or []) and
                                    (plan.get("notes_refs") or [])),
        "error": plan.get("error", "")}
