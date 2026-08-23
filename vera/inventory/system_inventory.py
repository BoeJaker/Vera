"""Canonical, read-only inventory of Vera's currently loaded system surfaces."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from Vera.vera.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    LOADED_MODULES,
    MCP_SERVERS,
    SCHEDULED_TASKS,
    UI_PANELS,
    WORKER_REGISTRY,
    capability,
)


INVENTORY_SCHEMA_VERSION = "vera.system-inventory/v1"
_DUPLICATION_TERMS = (
    "loop", "pipeline", "workflow", "run", "job", "task", "scheduler",
    "generate", "query", "store",
)


def _json_safe(value: Any) -> Any:
    """Return stable JSON data without object reprs or memory addresses."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if callable(value):
        return getattr(value, "__qualname__", getattr(value, "__name__", "callable"))
    return type(value).__name__


def _relative_path(path: Any, repo_root: Path) -> str:
    try:
        return Path(str(path)).resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return Path(str(path)).name


def _inferred_role(name: str, entry: Mapping[str, Any]) -> tuple[str, str]:
    """Conservative role hint; the inventory never treats heuristics as policy."""
    tags = {str(tag).lower() for tag in entry.get("tags", [])}
    desc = str(entry.get("description", "")).lower()
    source = str(entry.get("source", "local"))
    if not entry.get("mcp_expose", True):
        return "internal", "not exposed through MCP"
    if source == "alias" or "alias" in tags:
        return "alias", "registration metadata"
    if "deprecated" in tags or "deprecated" in desc or "legacy" in tags:
        return "deprecated", "registration metadata or description"
    if "experimental" in tags or "experimental" in desc:
        return "experimental", "registration metadata or description"
    if source == "mcp_proxy" or tags.intersection({"provider", "admin", "config"}):
        return "provider_admin", "source or registration tags"
    return "public_task", "default exposed capability classification"


def _capabilities(registry: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for name in sorted(registry):
        entry = registry[name]
        raw = entry.get("raw") or entry.get("func")
        role, role_basis = _inferred_role(name, entry)
        result.append({
            "name": name,
            "role_inferred": role,
            "role_basis": role_basis,
            "mode": entry.get("mode", "local"),
            "source": entry.get("source", "local"),
            "module": getattr(raw, "__module__", ""),
            "callable": getattr(raw, "__qualname__", getattr(raw, "__name__", "")),
            "schema": _json_safe(entry.get("schema", {})),
            "streams": sorted(str(v) for v in entry.get("streams", [])),
            "tags": sorted(str(v) for v in entry.get("tags", [])),
            "mcp_expose": bool(entry.get("mcp_expose", True)),
            "http": {
                "method": entry.get("http_method"),
                "path": entry.get("http_path"),
            },
        })
    return result


def _routes(app: Any) -> list[dict[str, Any]]:
    routes = []
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", "")
        if not path:
            continue
        routes.append({
            "path": path,
            "methods": sorted(getattr(route, "methods", None) or []),
            "name": getattr(route, "name", "") or "",
        })
    return sorted(routes, key=lambda item: (item["path"], item["methods"], item["name"]))


def _modules(modules: Sequence[Mapping[str, Any]], repo_root: Path) -> list[dict[str, Any]]:
    result = []
    for module in modules:
        status = str(module.get("status", "unknown"))
        result.append({
            "name": str(module.get("name", "")),
            "path": _relative_path(module.get("path", ""), repo_root),
            "caps_added": int(module.get("caps_added", 0) or 0),
            "status": "ok" if status == "ok" else "error",
            "error": "" if status == "ok" else status.removeprefix("error: ").strip(),
        })
    return sorted(result, key=lambda item: (item["name"], item["path"]))


def _panels(panels: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "id": panel_id,
        "label": str(panel.get("label", "")),
        "icon": str(panel.get("icon", "")),
        "group": str(panel.get("group", "")),
        "order": panel.get("order"),
    } for panel_id, panel in sorted(panels.items())]


def _named_records(records: Any) -> list[dict[str, Any]]:
    if isinstance(records, Mapping):
        iterable = records.items()
    else:
        iterable = ((str(index), value) for index, value in enumerate(records or []))
    return [{"id": str(key), "metadata": _json_safe(value)} for key, value in sorted(iterable, key=lambda pair: str(pair[0]))]


def _schedule_records(records: Any) -> list[dict[str, Any]]:
    """Keep schedule definitions while excluding counters/timestamps."""
    result = []
    for index, value in enumerate(records or []):
        item = dict(value) if isinstance(value, Mapping) else {"value": value}
        for volatile in ("last", "runs", "running", "next_run", "last_error"):
            item.pop(volatile, None)
        result.append({
            "id": str(item.get("name") or index),
            "metadata": _json_safe(item),
        })
    return sorted(result, key=lambda item: item["id"])


def build_system_inventory(
    *,
    capabilities: Mapping[str, Mapping[str, Any]],
    loaded_modules: Sequence[Mapping[str, Any]],
    panels: Mapping[str, Mapping[str, Any]],
    app: Any,
    schedules: Any,
    workers: Any,
    mcp_servers: Mapping[str, str],
    repo_root: Path,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Build a canonical snapshot whose fingerprint ignores capture time/load order."""
    cap_items = _capabilities(capabilities)
    body = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "capabilities": cap_items,
        "modules": _modules(loaded_modules, repo_root),
        "panels": _panels(panels),
        "http_routes": _routes(app),
        "schedules": _schedule_records(schedules),
        "workers": _named_records(workers),
        "mcp_servers": [{"name": name, "url": str(mcp_servers[name])}
                        for name in sorted(mcp_servers)],
        "duplication_signals": {
            term: [cap["name"] for cap in cap_items
                   if term in cap["name"].lower().replace("-", ".").split(".")]
            for term in _DUPLICATION_TERMS
        },
        "coverage": {
            "included": ["capabilities", "loaded_modules", "panels", "http_routes",
                         "schedules", "workers", "mcp_servers"],
            "not_yet_included": ["stored_workflows", "database_schema", "artifacts",
                                 "connections", "configuration_keys", "caller_graph"],
        },
    }
    # Workers contain heartbeat state, counters, PIDs, and random process IDs.
    # They belong in each captured snapshot but not in the architecture
    # fingerprint.  Schedule definitions are canonicalized above so their
    # run counters and timestamps cannot create false architectural drift.
    structural = {key: value for key, value in body.items() if key != "workers"}
    encoded = json.dumps(structural, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body["fingerprint_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    body["captured_at"] = captured_at or datetime.now(timezone.utc).isoformat()
    body["counts"] = {
        key: len(body[key]) for key in (
            "capabilities", "modules", "panels", "http_routes", "schedules",
            "workers", "mcp_servers",
        )
    }
    body["counts"]["module_errors"] = sum(
        module["status"] == "error" for module in body["modules"]
    )
    return body


def summarize_system_inventory(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Compact default view suitable for agents, dashboards, and drift checks."""
    roles: dict[str, int] = {}
    for item in snapshot.get("capabilities", []):
        role = str(item.get("role_inferred", "unknown"))
        roles[role] = roles.get(role, 0) + 1
    return {
        "schema_version": snapshot.get("schema_version"),
        "captured_at": snapshot.get("captured_at"),
        "fingerprint_sha256": snapshot.get("fingerprint_sha256"),
        "counts": snapshot.get("counts", {}),
        "role_counts_inferred": dict(sorted(roles.items())),
        "module_errors": [item for item in snapshot.get("modules", [])
                          if item.get("status") == "error"],
        "duplication_signal_counts": {
            term: len(names) for term, names in
            snapshot.get("duplication_signals", {}).items()
        },
        "coverage": snapshot.get("coverage", {}),
        "detail": False,
        "detail_hint": "Call system.inventory with detail=true for canonical records.",
    }


@capability(
    "system.inventory",
    memory="off",
    silent=True,
    tags=["system", "inventory", "experimental"],
    description=(
        "Canonical read-only snapshot of Vera's loaded capabilities, modules, panels, "
        "HTTP routes, schedules, workers, and MCP servers. Stable fingerprint excludes "
        "capture time, load order, and volatile worker state; optional-module failures "
        "remain explicit. Compact by default; pass detail=true for all records."
    ),
)
async def system_inventory(detail: bool = False, trace_id=None):
    raw_file = inspect.getsourcefile(build_system_inventory) or __file__
    repo_root = Path(raw_file).resolve().parents[2]
    snapshot = build_system_inventory(
        capabilities=CAPABILITY_REGISTRY,
        loaded_modules=LOADED_MODULES,
        panels=UI_PANELS,
        app=APP,
        schedules=SCHEDULED_TASKS,
        workers=WORKER_REGISTRY,
        mcp_servers=MCP_SERVERS,
        repo_root=repo_root,
    )
    return snapshot if detail else summarize_system_inventory(snapshot)
