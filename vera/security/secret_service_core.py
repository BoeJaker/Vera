"""One secrets service: the rules behind the secrets.* capabilities.

Vera's secrets had three homes that did not know about each other:
  vera/security/secrets.py  seal/open: Fernet inline in Redis, or OpenBao only when
                            the environment named one, and none was ever named
  OpenBao                   ran in dev mode on in-memory storage, unused
  keydrop                   the append-only drop only the user's private key can
                            read, filled by hand
Now OpenBao is the store of record for everything Vera seals and for named
secrets, Vera holds a token limited to its own paths, and anything a person needs
is also sealed into keydrop. These rules choose paths, find values still sealed
with the file key, write Vera's policy, and turn readings into findings.

Pure rules, no app imports (tests/test_secret_service_core.py).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple

FERNET = "fernet:"
BAO = "bao:v1:"
NAMED = "vera/named"
POLICY = "vera"
TOKEN_PERIOD = "768h"
RENEW_BELOW_S = 7 * 24 * 3600

# OpenBao's own bootstrap secrets (its unseal key and the token that opens it)
# live in this key and must stay sealed with the file key.
PINNED_KEYS = frozenset({"vera:provisioning:state"})
# Capability result caches and this service's own backups are not stores of record.
SKIP_PREFIXES = ("vera:cap:result:", "vera:secrets:migrate:backup:")

_SEGMENT = r"[a-z0-9][a-z0-9._-]{0,63}"
_PATH = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,6}}$")


# ── named secrets ────────────────────────────────────────────────────────────

def check_path(path: Any) -> str:
    p = str(path or "").strip().strip("/").lower()
    if not _PATH.match(p) or ".." in p:
        raise ValueError("path: lowercase letters, digits, '.', '_' and '-', "
                         "in up to seven '/'-separated parts")
    return p


def named_data(mount: str, path: str) -> str:
    return f"{mount.strip('/')}/data/{NAMED}/{check_path(path)}"


def named_metadata(mount: str, path: str) -> str:
    return f"{mount.strip('/')}/metadata/{NAMED}/{check_path(path)}"


def named_folder(mount: str, prefix: str = "") -> str:
    base = f"{mount.strip('/')}/metadata/{NAMED}/"
    return base + (check_path(prefix) + "/" if prefix else "")


def policy_hcl(mount: str = "secret") -> str:
    """Vera's OpenBao policy: its own KV paths and its own token, nothing else."""
    m = mount.strip("/")
    rules = [(f"{m}/data/vera/*", '"create", "read", "update", "delete"'),
             (f"{m}/metadata/vera/*", '"list", "read", "delete"'),
             (f"{m}/data/vera-secrets/*", '"create", "read", "update", "delete"'),
             (f"{m}/metadata/vera-secrets/*", '"list", "read", "delete"'),
             ("auth/token/renew-self", '"update"'),
             ("auth/token/lookup-self", '"read"')]
    return "\n".join(f'path "{p}" {{\n  capabilities = [{caps}]\n}}' for p, caps in rules) + "\n"


# ── finding sealed values ────────────────────────────────────────────────────

def sealed_kind(value: Any) -> str:
    if isinstance(value, str):
        if value.startswith(FERNET):
            return "fernet"
        if value.startswith(BAO):
            return "bao"
    return ""


def _walk(node: Any, path: Tuple, out: List[Tuple[Tuple, str]]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, path + (k,), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, path + (i,), out)
    elif sealed_kind(node):
        out.append((path, sealed_kind(node)))


def sealed_in(value: Any) -> List[Tuple[Tuple, str]]:
    """Every sealed string in a stored value: the value itself, or strings nested
    in a JSON document. [(path, kind)], with path () for the whole value."""
    if not isinstance(value, str):
        return []
    kind = sealed_kind(value)
    if kind:
        return [((), kind)]
    s = value.lstrip()
    if not s or s[0] not in "{[":
        return []
    try:
        doc = json.loads(value)
    except ValueError:
        return []
    out: List[Tuple[Tuple, str]] = []
    _walk(doc, (), out)
    return out


def sealed_strings(value: Any, kind: str = "fernet") -> List[str]:
    """The sealed strings themselves (of one kind) inside a stored value."""
    if not isinstance(value, str):
        return []
    if sealed_kind(value) == kind:
        return [value]
    found: List[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)
        elif sealed_kind(node) == kind:
            found.append(node)

    if any(k == kind for _p, k in sealed_in(value)):
        collect(json.loads(value))
    return found


def replace_sealed(value: str, mapping: Mapping[str, str]) -> str:
    """Swap sealed strings for their replacements, keeping a JSON document's shape."""
    if value in mapping:
        return mapping[value]

    def sub(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: sub(v) for k, v in node.items()}
        if isinstance(node, list):
            return [sub(v) for v in node]
        if isinstance(node, str) and node in mapping:
            return mapping[node]
        return node

    return json.dumps(sub(json.loads(value)))


def skip_key(key: str) -> bool:
    return str(key).startswith(SKIP_PREFIXES)


def migration_plan(entries: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """entries: {key, field ("" for a plain string), value}. The stored values that
    still hold file-key seals, leaving OpenBao's own bootstrap secrets alone."""
    items: List[Dict[str, Any]] = []
    pinned = bao = 0
    for e in entries:
        key = str(e.get("key") or "")
        if skip_key(key):
            continue
        found = sealed_in(e.get("value"))
        fernet = sum(1 for _p, k in found if k == "fernet")
        bao += sum(1 for _p, k in found if k == "bao")
        if not fernet:
            continue
        if key in PINNED_KEYS:
            pinned += fernet
            continue
        items.append({"key": key, "field": str(e.get("field") or ""), "count": fernet})
    return {"items": items, "fernet": sum(i["count"] for i in items), "pinned": pinned,
            "bao": bao, "keys": len({i["key"] for i in items})}


def ssh_store_plan(records: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Exec-store logins whose password or key passphrase is not yet held in OpenBao."""
    items: List[Dict[str, Any]] = []
    counts = {"fernet": 0, "bao": 0, "legacy": 0}
    for rid, rec in records.items():
        for field in ("password_obf", "passphrase_obf"):
            value = rec.get(field)
            if not value:
                continue
            kind = sealed_kind(value) or "legacy"
            counts[kind] += 1
            if kind != "bao":
                items.append({"id": rid, "label": rec.get("label", ""), "field": field, "kind": kind})
    return {"items": items, "counts": counts}


# ── keydrop ──────────────────────────────────────────────────────────────────

def keydrop_payload(title: str, username: str = "", password: str = "", url: str = "",
                    notes: str = "", tags: Iterable[str] = (), extra: Mapping[str, Any] = None) -> Dict[str, Any]:
    """The entry keydrop_pick.py turns into a KeePass row."""
    payload: Dict[str, Any] = {"title": title, "username": username or "", "password": password or "",
                               "url": url or "", "notes": notes or "", "tags": list(tags or [])}
    if extra:
        payload["extra"] = {str(k): str(v) for k, v in extra.items()}
    return payload


# ── findings ─────────────────────────────────────────────────────────────────

def findings(openbao: Mapping[str, Any], backend: Mapping[str, Any], token: Mapping[str, Any],
             keydrop: Mapping[str, Any], remaining: int) -> List[Dict[str, str]]:
    """The service's state in plain language, most serious first."""
    out: List[Dict[str, str]] = []

    def add(severity: str, message: str, detail: str = "") -> None:
        out.append({"severity": severity, "message": message, "detail": detail})

    if not openbao.get("reachable"):
        add("error", "OpenBao is not reachable, so secrets stored in it cannot be read.",
            str(openbao.get("error") or ""))
    else:
        if openbao.get("storage") == "inmem":
            add("error", "OpenBao keeps its data in memory; everything in it is lost when it restarts.",
                "run it with file or raft storage")
        if not openbao.get("initialized"):
            add("error", "OpenBao is not initialized.")
        elif openbao.get("sealed"):
            add("error", "OpenBao is sealed, so Vera cannot read secrets stored in it.",
                "secstore.unseal opens it with the stored key")
    if not backend.get("openbao_configured"):
        add("warn", "Vera is not configured to use OpenBao; new secrets are sealed with the file key.")
    elif not backend.get("openbao_active"):
        add("warn", "Vera is configured for OpenBao but cannot use it right now; new secrets fall back "
                    "to the file key.")
    if token.get("root"):
        add("warn", "Vera is using OpenBao's root token.",
            "secrets.setup gives it a token limited to its own paths")
    elif token.get("ttl_s") is not None and token["ttl_s"] < RENEW_BELOW_S and not token.get("renewable"):
        add("warn", "Vera's OpenBao token expires soon and cannot be renewed.")
    if remaining:
        add("info", f"{remaining} stored secret(s) are still sealed with the file key.",
            "secrets.migrate moves them into OpenBao")
    if not keydrop.get("available"):
        add("info", "Keydrop is not available here, so nothing can be handed to you.",
            str(keydrop.get("error") or ""))
    elif not keydrop.get("chain_ok", True):
        add("error", "Keydrop's hash chain is broken, so it refuses new entries.",
            str(keydrop.get("message") or ""))
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["severity"]])
