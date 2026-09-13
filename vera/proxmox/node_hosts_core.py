"""Which SSH login reaches a Proxmox node: one map, on the cluster record.

Three places answered that question on their own. The storage fabric kept a
node -> SSH host map in its own settings (vera:pxstore:cfg node_hosts),
proxmox.node.exec took whichever exec-store login had the API's hostname, and
Foundry went through proxmox.node.exec. On a single-node estate they agreed; on
a second node the hostname match would silently pick the wrong machine.

The map now lives on the Proxmox cluster record (node_hosts). Everything
resolves a node the same way: the cluster record's map, then the storage
fabric's older map, then an exec login named after the node, then the exec
login at the API host's address. A mapped login that no longer exists in the
exec store is skipped rather than used.

Pure rules, no app imports (tests/test_node_hosts_core.py).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse


def api_hostname(api_url: Any) -> str:
    return (urlparse(str(api_url or "")).hostname or "").lower()


def _norm(name: Any) -> str:
    s = str(name or "").strip().lower()
    s = re.sub(r"\s*\(.*\)$", "", s)
    return re.sub(r"\.(vera\.(int|lab)|local|lan)$", "", s).strip()


def _maps(cluster: Mapping[str, Any], pxstore_cfg: Optional[Mapping[str, Any]]):
    return [("cluster", dict((cluster or {}).get("node_hosts") or {})),
            ("pxstore", dict((pxstore_cfg or {}).get("node_hosts") or {}))]


def resolve_node_host(node: str, cluster: Mapping[str, Any],
                      pxstore_cfg: Optional[Mapping[str, Any]],
                      exec_hosts: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    """{host_id, source} for a node ("" = the cluster's node when there is only
    one). source: cluster | pxstore | label | api-host, or "" when nothing fits."""
    node = str(node or "").strip()
    hosts = list(exec_hosts)
    known = {str(h.get("id")) for h in hosts if h.get("id")}

    def usable(hid: str) -> bool:
        return bool(hid) and (not known or hid in known)

    for source, mapping in _maps(cluster, pxstore_cfg):
        if node and usable(str(mapping.get(node) or "")):
            return {"host_id": str(mapping[node]), "source": source}
        if not node and len(mapping) == 1:
            hid = str(next(iter(mapping.values())) or "")
            if usable(hid):
                return {"host_id": hid, "source": source}
    if node:
        for h in hosts:
            if _norm(h.get("label")) == _norm(node):
                return {"host_id": str(h["id"]), "source": "label"}
    api = api_hostname((cluster or {}).get("api_url"))
    if api:
        for h in hosts:
            if str(h.get("host") or "").lower() == api:
                return {"host_id": str(h["id"]), "source": "api-host"}
    return {"host_id": "", "source": ""}


def plan_merge(clusters: Iterable[Mapping[str, Any]],
               pxstore_cfgs: Mapping[str, Mapping[str, Any]],
               exec_hosts: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Copy the storage fabric's node map onto each cluster record. Per record:
    add (nodes to map), conflicts (both maps name different logins; the cluster
    record's is kept), stale (the fabric maps a login the exec store no longer
    has), and action update | conflict | current."""
    known = {str(h.get("id")) for h in exec_hosts if h.get("id")}
    steps: List[Dict[str, Any]] = []
    for c in clusters:
        have = {str(k): str(v) for k, v in dict(c.get("node_hosts") or {}).items()}
        legacy = dict((pxstore_cfgs.get(str(c.get("id"))) or {}).get("node_hosts") or {})
        add: Dict[str, str] = {}
        conflicts: List[Dict[str, str]] = []
        stale: List[Dict[str, str]] = []
        for node, hid in legacy.items():
            node, hid = str(node), str(hid)
            if known and hid not in known:
                stale.append({"node": node, "host_id": hid})
            elif node not in have:
                add[node] = hid
            elif have[node] != hid:
                conflicts.append({"node": node, "cluster": have[node], "pxstore": hid})
        action = "update" if add else "conflict" if conflicts else "current"
        steps.append({"cluster_id": c.get("id"), "label": c.get("label"),
                      "api_host": api_hostname(c.get("api_url")), "node_hosts": have,
                      "add": add, "conflicts": conflicts, "stale": stale, "action": action})
    counts: Dict[str, int] = {}
    for s in steps:
        counts[s["action"]] = counts.get(s["action"], 0) + 1
    return {"steps": steps, "counts": counts,
            "clusters_changed": sum(1 for s in steps if s["add"])}
