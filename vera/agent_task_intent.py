"""Deterministic task-level routing for agent authoring steps.

This layer translates a step's explicit deliverable intent into Capability
Contract canonical tasks, then uses the shared resolver to choose a concrete
provider already admitted to the loop catalog.  It never invokes a model or a
candidate capability and fails open when intent is ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .capability_contract_core import project_contract
from .capability_resolver_core import resolve_shadow


SCHEMA = "vera.agent-task-intent/v1"
SOURCE_AUTHOR_TASK = "source_file.author"
DOCUMENT_AUTHOR_TASK = "document.author"
AUTHOR_TASKS = (SOURCE_AUTHOR_TASK, DOCUMENT_AUTHOR_TASK)

_CODE_VERB = re.compile(
    r"\b(writ|creat|generat|build|implement|author|develop|code|scaffold|"
    r"refactor|debug|fix)\w*\b", re.I)
_CODE_NOUN = re.compile(
    r"\b(script|program|module|function|class|parser|code|codebase|"
    r"python|javascript|typescript|bash script|shell script|html|css|js|"
    r"web ?page|front[- ]?end|back[- ]?end|app|application|ui)\b"
    r"|\.(?:py|js|ts|jsx|tsx|sh|html|css|rb|go|rs|java|php|sql)\b", re.I)
_DOCUMENT_NOUN = re.compile(
    r"\b(report|summary|summaries|document|documentation|readme|article|"
    r"write-?up|blog ?post|narrative|synopsis|essay|briefing|guide)\b"
    r"|\.(?:md|markdown|txt|rst|docx|odt)\b", re.I)


def infer_authoring_tasks(text: str) -> dict[str, Any]:
    """Return content-free canonical authoring intent for one step."""
    value = str(text or "")
    tasks = []
    if _CODE_VERB.search(value) and _CODE_NOUN.search(value):
        tasks.append(SOURCE_AUTHOR_TASK)
    if _DOCUMENT_NOUN.search(value):
        tasks.append(DOCUMENT_AUTHOR_TASK)
    mode = "ambiguous" if not tasks else "resolved" if len(tasks) == 1 else "compound"
    return {"schema": SCHEMA, "mode": mode, "canonical_tasks": tasks,
            "authorized": False, "executed": False}


def _manifest(name: str, registry: Mapping[str, Any]) -> dict[str, Any] | None:
    entry = registry.get(name)
    if not isinstance(entry, Mapping):
        return None
    try:
        return project_contract(name, entry)
    except Exception:
        return None


def resolve_authoring_tasks(text: str, catalog: list[str],
                            registry: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve explicit authoring tasks to providers in catalog order.

    Ambiguous text produces no selection and callers preserve their existing
    caps.  A failed contract resolution also preserves compatibility rather
    than manufacturing authority.
    """
    result = infer_authoring_tasks(text)
    if result["mode"] == "ambiguous":
        return {**result, "selections": [], "provider_tasks": {}}

    manifests = [item for name in catalog if (item := _manifest(name, registry))]
    provider_tasks = {item["name"]: item["canonical_task"] for item in manifests
                      if item.get("canonical_task") in AUTHOR_TASKS}
    selections = []
    for task in result["canonical_tasks"]:
        family = [item for item in manifests if item.get("canonical_task") == task]
        if not family:
            selections.append({"canonical_task": task, "selected": None,
                               "alternatives": [], "status": "unavailable"})
            continue
        effects = sorted({effect for item in family
                          for effect in ((item.get("effects") or {}).get("declared") or [])})
        resolution = resolve_shadow(
            family,
            {"canonical_task": task, "allowed_effects": effects,
             "preferred": [name for name in catalog
                           if provider_tasks.get(name) == task]},
        )
        selected = resolution.get("selected")
        selections.append({
            "canonical_task": task,
            "selected": selected,
            "alternatives": [row["name"] for row in resolution.get("eligible") or []
                             if row.get("name") != selected],
            "status": "resolved" if selected else "unresolved",
        })
    return {**result, "selections": selections, "provider_tasks": provider_tasks}


def route_authoring_caps(caps: list[str], resolution: Mapping[str, Any]) -> list[str]:
    """Apply a resolved authoring decision while preserving unrelated caps."""
    if resolution.get("mode") not in {"resolved", "compound"}:
        return list(caps)
    selected = [row.get("selected") for row in resolution.get("selections") or []
                if row.get("selected")]
    if not selected:
        return list(caps)
    requested = set(resolution.get("canonical_tasks") or [])
    provider_tasks = dict(resolution.get("provider_tasks") or {})
    routed = [name for name in caps
              if provider_tasks.get(name) in requested or name not in provider_tasks]
    # A grounded author replaces a raw generation peer for this explicit task.
    routed = [name for name in routed if name != "llm.generate"]
    for name in reversed(selected):
        if name not in routed:
            routed.insert(0, name)
    return routed


def public_task_resolution(resolution: Mapping[str, Any]) -> dict[str, Any]:
    """Content-free event/UI projection; omit registry implementation detail."""
    return {
        "schema": SCHEMA,
        "mode": str(resolution.get("mode") or "ambiguous"),
        "canonical_tasks": list(resolution.get("canonical_tasks") or []),
        "selections": [{"canonical_task": row.get("canonical_task"),
                        "selected": row.get("selected"),
                        "alternatives": list(row.get("alternatives") or []),
                        "status": row.get("status")}
                       for row in resolution.get("selections") or []],
        "authorized": False,
        "executed": False,
    }
