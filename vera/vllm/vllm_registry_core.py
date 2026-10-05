"""
vllm_registry_core.py - what persists of the vLLM instance registry, and how the
stored copy merges with the VLLM_INSTANCES env list at boot.

Pure: no I/O, no app imports (testable app-free).

The runtime registry (vllm.instances.add / .remove) used to live only in the
process, so every restart silently dropped it. The stored document is

    {"instances": {<id>: {url, label, has_gpu, priority}}, "removed": [<id>, ...]}

* Runtime-added instances are restored; a stored entry overrides an env entry of
  the same id (the later, explicit act wins).
* Removing an env-defined instance records a tombstone so it stays removed.
* API keys are NOT persisted: they come from the env list / VLLM_API_KEY (and,
  later, the secrets service) - never a plain Redis string.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

PERSISTED_FIELDS = ("url", "label", "has_gpu", "priority", "gate_as")


def record(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The persisted subset of one instance's config."""
    return {
        "url": str(spec.get("url") or "").rstrip("/"),
        "label": str(spec.get("label") or ""),
        "has_gpu": bool(spec.get("has_gpu", True)),
        "priority": int(spec.get("priority") or 0),
        # the Ollama node id whose gate this server takes (shared hardware)
        "gate_as": str(spec.get("gate_as") or ""),
    }


def empty_doc() -> Dict[str, Any]:
    return {"instances": {}, "removed": []}


def normalise_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        return empty_doc()
    inst = doc.get("instances") if isinstance(doc.get("instances"), dict) else {}
    removed = [str(r) for r in (doc.get("removed") or []) if r]
    return {"instances": {str(k): record(v) for k, v in inst.items()
                          if isinstance(v, dict) and v.get("url")},
            "removed": sorted(set(removed))}


def note_added(doc: Optional[Dict[str, Any]], iid: str,
               spec: Dict[str, Any]) -> Dict[str, Any]:
    d = normalise_doc(doc)
    d["instances"][iid] = record(spec)
    d["removed"] = [r for r in d["removed"] if r != iid]
    return d


def note_removed(doc: Optional[Dict[str, Any]], iid: str,
                 env_ids: Iterable[str] = ()) -> Dict[str, Any]:
    d = normalise_doc(doc)
    d["instances"].pop(iid, None)
    if iid in set(env_ids) and iid not in d["removed"]:
        d["removed"] = sorted(d["removed"] + [iid])
    return d


def merge(env_specs: List[Dict[str, Any]],
          doc: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The instance specs to register at boot: env list, minus tombstones,
    overlaid and extended by the stored runtime registrations. Env-only fields
    (api_key) survive an overlay of the same id."""
    d = normalise_doc(doc)
    out: Dict[str, Dict[str, Any]] = {}
    for i, spec in enumerate(env_specs or []):
        iid = str(spec.get("id") or f"vllm-{i}")
        if iid in d["removed"]:
            continue
        out[iid] = dict(spec, id=iid)
    for iid, rec in d["instances"].items():
        base = out.get(iid, {})
        out[iid] = dict(base, **rec, id=iid)
    return list(out.values())
