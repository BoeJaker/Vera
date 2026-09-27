"""Canonical, read-only inventory of Vera's currently loaded system surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from Vera.vera.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    LOADED_MODULES,
    MCP_SERVERS,
    SCHEDULED_TASKS,
    UI_PANELS,
    WORKER_REGISTRY,
    activity_actor,
    capability,
)
from Vera.vera.config import cfg


INVENTORY_SCHEMA_VERSION = "vera.system-inventory/v1"
_DUPLICATION_TERMS = (
    "loop", "pipeline", "workflow", "run", "job", "task", "scheduler",
    "generate", "query", "store",
)
_SECRET_HINTS = ("password", "passwd", "token", "secret", "credential", "api_key", "auth")
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _json_safe(value: Any) -> Any:
    """Return stable JSON data without object reprs or memory addresses."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        result = {}
        for key in sorted(value, key=str):
            label = str(key)
            result[label] = ("[redacted]" if any(hint in label.lower() for hint in _SECRET_HINTS)
                             else _json_safe(value[key]))
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if callable(value):
        return getattr(value, "__qualname__", getattr(value, "__name__", "callable"))
    return type(value).__name__


def _safe_endpoint(value: Any) -> str:
    """Keep endpoint identity without credentials or query-string secrets."""
    text = str(value)
    try:
        parsed = urlsplit(text)
        if not parsed.scheme or not parsed.netloc:
            return text.split("?", 1)[0].split("#", 1)[0]
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "[invalid endpoint]"


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
            "compatibility_alias_for": str(
                entry.get("compatibility_alias_for") or ""),
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


def _configuration_records(records: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Expose declared key presence only; values never cross this boundary."""
    return [{"key": str(key), "explicit": bool(records[key])}
            for key in sorted(records or {}, key=str)]


def _stored_workflow_records(records: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Keep workflow identity/shape, never definitions, prompts, or state."""
    result = []
    for source, values in sorted((records or {}).items()):
        for index, value in enumerate(values or []):
            item = value if isinstance(value, Mapping) else {}
            definition = item.get("dag") or item.get("definition") or []
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
            result.append({
                "source": str(source),
                "id": str(item.get("id") or index),
                "name": str(item.get("name") or ""),
                "category": str(item.get("category") or ""),
                "tags": sorted(str(tag) for tag in tags),
                "node_count": len(definition) if isinstance(definition, list)
                              else int(item.get("node_count") or 0),
                "capabilities": sorted({
                    str(node[0]) for node in definition
                    if isinstance(node, list) and node and isinstance(node[0], str)
                }) if isinstance(definition, list) else [],
            })
    return sorted(result, key=lambda item: (item["source"], item["id"], item["name"]))


def _database_schema_records(records: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    return sorted(({"table": str(item.get("table", "")),
                    "source": str(item.get("source", ""))}
                   for item in (records or []) if item.get("table")),
                  key=lambda item: (item["table"], item["source"]))


def _artifact_provider_records(capabilities: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Inventory artifact interfaces, not sensitive project artifact instances."""
    records = []
    for cap in capabilities:
        name = str(cap.get("name", ""))
        if not any(part.startswith("artifact")
                   for part in name.lower().replace("-", ".").split(".")):
            continue
        records.append({
            "capability": name,
            "module": str(cap.get("module", "")),
            "http": _json_safe(cap.get("http", {})),
            "mcp_expose": bool(cap.get("mcp_expose", True)),
            "scope": "provider_surface_not_stored_content",
        })
    return records


def _connection_records(records: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Expose saved connection identity/type while excluding endpoints and credentials."""
    result = []
    for index, item in enumerate(records or []):
        if not isinstance(item, Mapping):
            continue
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        result.append({
            "id": str(item.get("id") or index),
            "label": str(item.get("label") or ""),
            "kind": str(item.get("kind") or ""),
            "tags": sorted(str(tag) for tag in tags),
            "has_credential_reference": bool(item.get("ssh_host_id") or
                                             item.get("docker_host_id")),
        })
    return sorted(result, key=lambda item: (item["kind"], item["id"]))


def _caller_graph(capabilities: Sequence[Mapping[str, Any]], schedules: Any,
                  workflows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Describe known interface callers; never claim a speculative runtime call graph."""
    edges = set()
    for cap in capabilities:
        target = str(cap.get("name", ""))
        module = str(cap.get("module", ""))
        if module:
            edges.add((f"python:{module}", target, "registration"))
        http = cap.get("http") or {}
        if http.get("path"):
            edges.add((f"http:{http.get('method') or '*'} {http['path']}", target, "route"))
        if cap.get("mcp_expose", True):
            edges.add(("mcp:capability", target, "exposure"))
    for index, schedule in enumerate(schedules or []):
        if not isinstance(schedule, Mapping):
            continue
        target = schedule.get("capability") or schedule.get("cap")
        if target:
            edges.add((f"schedule:{schedule.get('name') or index}", str(target), "schedule"))
    for workflow in workflows:
        caller = f"stored-definition:{workflow['source']}:{workflow['id']}"
        for target in workflow.get("capabilities", []):
            edges.add((caller, str(target), "workflow_node"))
    return [{"caller": caller, "capability": target, "basis": basis}
            for caller, target, basis in sorted(edges)]


def _observed_caller_graph(
    events: Sequence[Mapping[str, Any]] | None,
    known_capabilities: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate bounded runtime caller evidence without retaining payloads."""
    known = {str(item.get("name") or "") for item in known_capabilities}
    counts: dict[tuple[str, str, str, str], int] = {}
    for raw in events or []:
        if not isinstance(raw, Mapping):
            continue
        event_type = str(raw.get("type") or "")
        if event_type not in {"cap.ok", "cap.error"}:
            continue
        target = str(raw.get("name") or "")
        if target not in known:
            continue
        actor = activity_actor(dict(raw))
        if actor == "unknown":
            continue
        if actor == "you" or actor in {"agent:user", "agent:ui", "agent:browser"}:
            caller, caller_class = "ui:browser", "ui"
        elif actor.startswith("agent:"):
            caller, caller_class = actor, "agent"
        elif actor.startswith("system:"):
            caller, caller_class = actor, "system"
        else:
            continue
        outcome = "ok" if event_type == "cap.ok" else "error"
        key = (caller, caller_class, target, outcome)
        counts[key] = counts.get(key, 0) + 1
    return [{
        "caller": caller,
        "caller_class": caller_class,
        "capability": target,
        "outcome": outcome,
        "observations": count,
        "basis": "bounded_runtime_telemetry",
    } for (caller, caller_class, target, outcome), count in sorted(counts.items())]


def _declared_database_schema(loaded_modules: Sequence[Mapping[str, Any]],
                              repo_root: Path) -> list[dict[str, str]]:
    """Scan loaded local source for declared tables without opening a database."""
    found = set()
    for module in loaded_modules:
        path = Path(str(module.get("path") or ""))
        if not path.is_absolute():
            path = repo_root / path
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        rel = _relative_path(path, repo_root)
        for table in _CREATE_TABLE_RE.findall(source):
            found.add((table, rel))
    return [{"table": table, "source": source} for table, source in sorted(found)]


async def _inventory_capability_records(name: str, key: str) -> list[dict[str, Any]]:
    """Call a read-only raw capability and normalize unavailable stores to empty."""
    entry = CAPABILITY_REGISTRY.get(name) or {}
    raw = entry.get("raw")
    if not callable(raw):
        return []
    try:
        result = await raw()
    except Exception:
        return []
    values = result.get(key, []) if isinstance(result, Mapping) else []
    return list(values) if isinstance(values, list) else []


async def _inventory_capability_events(name: str, *, limit: int) -> list[dict[str, Any]]:
    """Read a bounded event list from a raw observation capability."""
    entry = CAPABILITY_REGISTRY.get(name) or {}
    raw = entry.get("raw")
    if not callable(raw):
        return []
    try:
        result = await raw(limit=max(1, min(int(limit), 500)))
    except Exception:
        return []
    return ([dict(item) for item in result if isinstance(item, Mapping)]
            if isinstance(result, list) else [])


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
    configuration_keys: Mapping[str, Any] | None = None,
    stored_workflows: Mapping[str, Any] | None = None,
    database_schema: Sequence[Mapping[str, Any]] | None = None,
    connections: Sequence[Mapping[str, Any]] | None = None,
    runtime_events: Sequence[Mapping[str, Any]] | None = None,
    runtime_event_limit: int = 500,
) -> dict[str, Any]:
    """Build a canonical snapshot whose fingerprint ignores capture time/load order."""
    cap_items = _capabilities(capabilities)
    workflow_items = _stored_workflow_records(stored_workflows)
    body = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "capabilities": cap_items,
        "modules": _modules(loaded_modules, repo_root),
        "panels": _panels(panels),
        "http_routes": _routes(app),
        "schedules": _schedule_records(schedules),
        "workers": _named_records(workers),
        "mcp_servers": [{"name": name, "url": _safe_endpoint(mcp_servers[name])}
                        for name in sorted(mcp_servers)],
        "configuration_keys": _configuration_records(configuration_keys),
        "stored_workflows": workflow_items,
        "database_schema": _database_schema_records(database_schema),
        "artifacts": _artifact_provider_records(cap_items),
        "connections": _connection_records(connections),
        "caller_graph": _caller_graph(cap_items, schedules, workflow_items),
        "observed_caller_graph": _observed_caller_graph(runtime_events, cap_items),
        "observation_coverage": {
            "runtime_callers": "partial",
            "source": "bounded_recent_capability_events",
            "event_limit": max(1, min(int(runtime_event_limit), 500)),
            "privacy": "aggregate_without_content_or_identifiers",
        },
        "duplication_signals": {
            term: [cap["name"] for cap in cap_items
                   if term in cap["name"].lower().replace("-", ".").split(".")]
            for term in _DUPLICATION_TERMS
        },
        "coverage": {
            "included": ["capabilities", "loaded_modules", "panels", "http_routes",
                         "schedules", "workers", "mcp_servers", "configuration_keys",
                         "stored_workflows", "database_schema", "artifacts",
                         "connections", "caller_graph", "observed_caller_graph"],
            "not_yet_included": [],
        },
    }
    # Workers contain heartbeat state, counters, PIDs, and random process IDs.
    # They belong in each captured snapshot but not in the architecture
    # fingerprint.  Schedule definitions are canonicalized above so their
    # run counters and timestamps cannot create false architectural drift.
    structural = {key: value for key, value in body.items()
                  if key not in {"workers", "observed_caller_graph",
                                 "observation_coverage"}}
    encoded = json.dumps(structural, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body["fingerprint_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    body["captured_at"] = captured_at or datetime.now(timezone.utc).isoformat()
    body["counts"] = {
        key: len(body[key]) for key in (
            "capabilities", "modules", "panels", "http_routes", "schedules",
            "workers", "mcp_servers",
            "configuration_keys", "stored_workflows", "database_schema",
            "artifacts", "connections", "caller_graph", "observed_caller_graph",
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
        "observation_coverage": snapshot.get("observation_coverage", {}),
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
    dag_store, fabric_dags, connections, runtime_events = await asyncio.gather(
        _inventory_capability_records("dag.store_list", "dags"),
        _inventory_capability_records("fabric.dags.list", "dags"),
        _inventory_capability_records("conn.list", "connections"),
        _inventory_capability_events("obs.events", limit=500),
    )
    snapshot = build_system_inventory(
        capabilities=CAPABILITY_REGISTRY,
        loaded_modules=LOADED_MODULES,
        panels=UI_PANELS,
        app=APP,
        schedules=SCHEDULED_TASKS,
        workers=WORKER_REGISTRY,
        mcp_servers=MCP_SERVERS,
        repo_root=repo_root,
        configuration_keys={name: name in os.environ for name in vars(type(cfg))
                            if name.isupper() and not name.startswith("_")},
        stored_workflows={"dag_store": dag_store, "fabric": fabric_dags},
        database_schema=_declared_database_schema(LOADED_MODULES, repo_root),
        connections=connections,
        runtime_events=runtime_events,
        runtime_event_limit=500,
    )
    return snapshot if detail else summarize_system_inventory(snapshot)
