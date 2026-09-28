"""Specialist (non-LLM) models across the estate: NLP on the nodes, STT/TTS and
diffusion on the GPU media server, entity NER on the host - pure shaping for
`specialist.status`, testable without booting Vera.

Consumers import uppercase (Vera.vera.catalog.specialist_core); tests import
lowercase so pytest binds to the worktree copy. Nothing here imports Vera: the
version comparison is passed in (components_core.compare_versions).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List

#: The families the Specialist view shows, in display order.
FAMILIES = ("nlp", "media", "host_ner")

#: The media node detail the heartbeat keeps (capability_orchestration's
#: _ping_media_instance); `services` carries which of stt/tts/imagegen it serves.
MEDIA_DETAIL_KEYS = ("tts_engine", "gpu", "cuda", "device", "sample_rate")


def nlp_node_row(node: Dict[str, Any], host_version: Dict[str, Any],
                 compare: Callable[[Dict, Dict], Dict]) -> Dict[str, Any]:
    """One NLP node as the view shows it: its deployed nlp_server version against
    the host's, and each task's model with whether it is in the store and loaded.

    `modified` beats every other state: a node whose files no longer match its
    own version record is running code nobody deployed."""
    comp = node.get("component") or {}
    cmp = compare(host_version, comp)
    state = "modified" if comp.get("intact") is False else cmp["state"]
    tasks = {}
    for task, row in sorted((node.get("tasks") or {}).items()):
        tasks[task] = {"model": row.get("model", ""), "present": bool(row.get("present")),
                       "loaded": bool(row.get("loaded")),
                       "package": ((row.get("model_package") or {}).get("version") or "")}
    return {"node_id": str(node.get("node_id") or ""), "url": node.get("nlp_url", ""),
            "version": comp.get("version", ""), "state": state,
            "changed": list(comp.get("changed") or cmp.get("changed") or []),
            "threads": node.get("threads"), "tasks": tasks,
            "missing": [t for t, r in tasks.items() if not r["present"]]}


def media_node_row(iid: str, inst: Dict[str, Any]) -> Dict[str, Any]:
    detail = inst.get("detail") or {}
    return {"instance_id": iid, "label": inst.get("label", iid),
            "url": inst.get("url", ""), "status": inst.get("status", "unknown"),
            "has_gpu": bool(inst.get("has_gpu")), "enabled": inst.get("enabled", True),
            "services": list(inst.get("services") or []),
            "in_use": int(inst.get("in_use") or 0),
            "serves": {k: detail.get(k) for k in MEDIA_DETAIL_KEYS if k in detail}}


def registry_rows(default_models: Dict[str, str], task_kind: Dict[str, str]) -> List[Dict]:
    """The NLP model registry every node serves from (nlp_dispatch_core)."""
    return [{"task": t, "model": m, "kind": task_kind.get(t, "")}
            for t, m in default_models.items()]


def summarize(nlp_rows: Iterable[Dict], media_rows: Iterable[Dict]) -> Dict[str, int]:
    nlp_rows, media_rows = list(nlp_rows), list(media_rows)
    states: Dict[str, int] = {}
    for r in nlp_rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
    return {"nlp_nodes": len(nlp_rows),
            "nlp_current": states.get("current", 0),
            "nlp_not_current": len(nlp_rows) - states.get("current", 0),
            "nlp_missing_models": sum(1 for r in nlp_rows if r["missing"]),
            "media_nodes": len(media_rows),
            "media_online": sum(1 for r in media_rows if r["status"] == "online")}
