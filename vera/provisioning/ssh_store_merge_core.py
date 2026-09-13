"""One SSH host store: rules for folding the enrolment store into the exec store.

Vera kept SSH logins in two places. The exec store (exec.ssh.hosts.*, Neo4j +
file cache) is what every remote command, Docker SSH host, terminal and mesh
join resolves. The enrolment store (ssh.host.*, Redis vera:provisioning:ssh_hosts)
is written by the enrolment flow, which also wrote a second copy into the exec
store on every run. A host saved in one was invisible to the other.

On 12 Sep 2026 all 18 enrolment records already had an exec twin (same host,
port and user); six of them were repeats of the same login. What the enrolment
store adds is the link to a Proxmox guest (guest_ref) and its own record id, so
merging means copying those onto the exec twin as tags. Nothing is deleted:
ssh.host.* keeps its own records and reads the exec store through until a later
step retires them.

Pure rules, no app imports (tests/test_ssh_store_merge_core.py).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

GUEST_TAG = "guest:"     # guest:<cluster_id>:<vmid>
ENROL_TAG = "enrol:"     # enrol:<enrolment record id>


def login_key(rec: Mapping[str, Any]) -> Tuple[str, int, str]:
    try:
        port = int(rec.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return (str(rec.get("host") or "").strip().lower(), port, str(rec.get("user") or "").strip())


def _newest(recs: List[Mapping[str, Any]]) -> Mapping[str, Any]:
    return sorted(recs, key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""))[-1]


def find_twin(enrol: Mapping[str, Any], exec_records: Iterable[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The exec record for the same login: same host, port and user; among
    several, the one with the same label, else the most recently updated."""
    same = [r for r in exec_records if login_key(r) == login_key(enrol)]
    if not same:
        return None
    labelled = [r for r in same if (r.get("label") or "") == (enrol.get("label") or "")]
    return _newest(labelled or same)


def wanted_tags(enrol: Mapping[str, Any]) -> List[str]:
    tags = []
    ref = str(enrol.get("guest_ref") or "").strip()
    if ref and ":" in ref:
        tags.append(GUEST_TAG + ref)
    if enrol.get("id"):
        tags.append(ENROL_TAG + str(enrol["id"]))
    return tags


def plan_merge(enrol_records: Iterable[Mapping[str, Any]],
               exec_records: Iterable[Mapping[str, Any]],
               vera_key_path: str) -> Dict[str, Any]:
    """What merging would do, record by record. Actions:
      link       add the enrolment record's tags to its exec twin
      linked     the twin already carries them; nothing to do
      copy       no twin: create an exec record (cert and password logins only)
      attention  no twin and the login cannot be carried over automatically
    """
    exec_records = list(exec_records)
    steps: List[Dict[str, Any]] = []
    pending: Dict[str, List[str]] = {}          # exec id -> tags this plan adds
    for enrol in enrol_records:
        twin = find_twin(enrol, exec_records)
        want = wanted_tags(enrol)
        base = {"enrol_id": enrol.get("id"), "label": enrol.get("label"), "host": enrol.get("host"),
                "port": login_key(enrol)[1], "user": enrol.get("user"), "auth": enrol.get("auth")}
        if twin is not None:
            have = set(twin.get("tags") or []) | set(pending.get(twin["id"], []))
            add = [t for t in want if t not in have]
            if add:
                pending.setdefault(twin["id"], []).extend(add)
            steps.append(dict(base, action="link" if add else "linked", exec_id=twin["id"],
                              exec_label=twin.get("label"), add_tags=add))
            continue
        auth = str(enrol.get("auth") or "password")
        if auth == "cert":
            steps.append(dict(base, action="copy", exec_auth="key", key_path=vera_key_path, add_tags=want))
        elif auth == "password" and enrol.get("has_password", True):
            steps.append(dict(base, action="copy", exec_auth="password", key_path="", add_tags=want))
        else:
            steps.append(dict(base, action="attention", add_tags=want,
                              reason="its private key is sealed in the enrolment store; the exec store "
                                     "only takes a key file path"))
    counts: Dict[str, int] = {}
    for s in steps:
        counts[s["action"]] = counts.get(s["action"], 0) + 1
    return {"steps": steps, "counts": counts,
            "exec_records_changed": len({s["exec_id"] for s in steps if s["action"] == "link"}),
            "exec_records_created": counts.get("copy", 0)}


def guest_ref_of(tags: Iterable[str]) -> str:
    for t in tags or []:
        if str(t).startswith(GUEST_TAG):
            return str(t)[len(GUEST_TAG):]
    return ""


def guest_refs(tags: Any) -> List[str]:
    """Every guest a login is tagged with; a reused address can carry several."""
    items = tags.split(",") if isinstance(tags, str) else list(tags or [])
    return [str(t).strip()[len(GUEST_TAG):] for t in items if str(t).strip().startswith(GUEST_TAG)]


def login_for_guest(logins: Iterable[Mapping[str, Any]], cluster_id: str,
                    vmid: Any) -> Optional[Mapping[str, Any]]:
    """The exec login for a Proxmox guest: the label pve:<vmid>@<node> that
    proxmox.guest.enroll writes, which the storage fabric has always matched, else
    a login tagged guest:<cluster>:<vmid> (enrolment and the store merge tag them).
    The label wins so a stale tag left on a reused address cannot displace it."""
    logins = list(logins)
    prefix = f"pve:{vmid}@"
    for h in logins:
        if str(h.get("label") or "").startswith(prefix):
            return h
    if cluster_id:
        ref = f"{cluster_id}:{vmid}"
        for h in logins:
            if ref in guest_refs(h.get("tags")):
                return h
    return None


def read_through(enrol_rows: Iterable[Mapping[str, Any]],
                 exec_rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """ssh.host.list during the migration: the enrolment rows as before (each
    marked with its exec twin), then every exec login that has no enrolment
    row, shaped like one so existing callers keep working."""
    exec_rows = list(exec_rows)
    out: List[Dict[str, Any]] = []
    twinned = set()
    for row in enrol_rows:
        twin = find_twin(row, exec_rows)
        if twin is not None:
            twinned.add(twin["id"])
        out.append(dict(row, source="enrol", exec_id=twin["id"] if twin is not None else ""))
    for r in exec_rows:
        if r["id"] in twinned:
            continue
        out.append({"id": r["id"], "label": r.get("label"), "host": r.get("host"),
                    "port": login_key(r)[1], "user": r.get("user"),
                    "auth": r.get("auth") or "password",
                    "has_password": bool(r.get("has_password")), "has_private_key": False,
                    "key_path": r.get("key_path") or "", "public_key": "",
                    "guest_ref": guest_ref_of(r.get("tags")), "tags": list(r.get("tags") or []),
                    "source": "exec", "exec_id": r["id"]})
    return out
