"""SSH login cleanup: which saved logins are repeats, superseded or stale.

On 13 Sep 2026 the exec store held 28 logins for a much smaller estate: the same
login saved three times for Ollama-D and for foundry-ct-test, an old password login
beside the key login for the same host and user, and logins for test guests that
no longer exist. A login referenced anywhere else (a Docker host, a Proxmox node
map, the mesh, a setting) is never removed.

Pure rules, no app imports (tests/test_ssh_cleanup_core.py).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ACTIONS = ("keep", "merge", "superseded", "stale")


def login_key(rec: Mapping[str, Any]) -> Tuple[str, int, str, str, str]:
    try:
        port = int(rec.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return (str(rec.get("host") or "").strip().lower(), port, str(rec.get("user") or "").strip(),
            str(rec.get("auth") or "password"), str(rec.get("key_path") or ""))


def norm_name(name: Any) -> str:
    s = str(name or "").strip().lower()
    s = re.sub(r"\s*\(.*\)$", "", s)
    s = re.sub(r"\.(vera\.(int|lab)|local|lan)$", "", s)
    return s.strip()


def _tags(rec: Mapping[str, Any]) -> List[str]:
    t = rec.get("tags") or []
    return [x.strip() for x in t.split(",")] if isinstance(t, str) else [str(x) for x in t]


def _enrolled(rec: Mapping[str, Any]) -> bool:
    return any(t.startswith(("enrol:", "guest:")) for t in _tags(rec))


def plan(logins: Mapping[str, Mapping[str, Any]], referenced: Set[str],
         reachable: Mapping[str, Optional[bool]], guests: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """logins: id -> exec-store record. referenced: ids found in any other store.
    reachable: id -> whether its SSH port answered (None = not probed).
    guests: every Proxmox guest, {name, ips}. One step per login:
      keep        in use, or nothing is wrong with it
      merge       an exact repeat of another login (host, port, user, auth, key);
                  its tags move to the login that stays
      superseded  a password login for a host and user that also have a key login
      stale       its port did not answer and no guest has its address or name
    Referenced logins are always kept."""
    guests = list(guests)
    guest_ips = {ip for g in guests for ip in (g.get("ips") or []) if ip} | \
                {g.get("addr") for g in guests if g.get("addr")}
    guest_names = {norm_name(g.get("name") or g.get("label")) for g in guests} - {""}
    steps: Dict[str, Dict[str, Any]] = {}
    add_tags: Dict[str, List[str]] = defaultdict(list)

    def gain(keeper: str, rid: str) -> None:
        have = set(_tags(logins[keeper])) | set(add_tags[keeper])
        for t in _tags(logins[rid]):
            if t and t not in have:
                add_tags[keeper].append(t)
                have.add(t)

    groups: Dict[Tuple, List[str]] = defaultdict(list)
    for rid, rec in logins.items():
        groups[login_key(rec)].append(rid)
    for ids in groups.values():
        if len(ids) < 2:
            continue
        ordered = sorted(ids, key=lambda i: (i in referenced, _enrolled(logins[i]),
                                              str(logins[i].get("updated_at") or ""), i), reverse=True)
        keeper = ordered[0]
        for rid in ordered[1:]:
            if rid in referenced:
                steps[rid] = {"action": "keep", "reason": f"repeats {keeper} but is referenced elsewhere"}
            else:
                steps[rid] = {"action": "merge", "into": keeper, "reason": f"exact repeat of {keeper}"}
                gain(keeper, rid)

    live = [rid for rid in logins if steps.get(rid, {}).get("action") != "merge"]
    key_logins: Dict[Tuple[str, int, str], str] = {}
    for rid in live:
        k = login_key(logins[rid])
        if k[3] == "key":
            key_logins.setdefault(k[:3], rid)

    for rid in live:
        if rid in steps:
            continue
        rec = logins[rid]
        k = login_key(rec)
        if rid in referenced:
            steps[rid] = {"action": "keep", "reason": "referenced elsewhere"}
        elif k[3] == "password" and k[:3] in key_logins:
            keeper = key_logins[k[:3]]
            steps[rid] = {"action": "superseded", "into": keeper,
                          "reason": f"a key login for the same host and user exists ({keeper})"}
            gain(keeper, rid)
        elif (k[0] not in LOCAL_HOSTS and reachable.get(rid) is False
              and k[0] not in guest_ips and norm_name(rec.get("label")) not in guest_names):
            steps[rid] = {"action": "stale", "reason": "does not answer, and no guest has its address or name"}
        else:
            steps[rid] = {"action": "keep", "reason": ""}

    rows = []
    for rid in sorted(logins, key=lambda i: (login_key(logins[i]), i)):
        rec = logins[rid]
        s = steps[rid]
        rows.append({"id": rid, "label": rec.get("label", ""), "host": rec.get("host", ""),
                     "user": rec.get("user", ""), "auth": rec.get("auth", ""),
                     "action": s["action"], "into": s.get("into", ""), "reason": s["reason"],
                     "add_tags": add_tags.get(rid, []) if s["action"] == "keep" else []})
    counts = {a: sum(1 for r in rows if r["action"] == a) for a in ACTIONS}
    return {"steps": rows, "counts": counts,
            "tag_updates": {k: v for k, v in add_tags.items() if v and steps[k]["action"] == "keep"}}
