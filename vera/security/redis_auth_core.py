"""
redis_auth_core.py — who may use the shared Redis, and with what (no app imports)

The Redis at the Vera host serves the whole LLM_Stack, not just Vera: Vera
itself, its node workers, vikunja, searxng and redisinsight. Until 2026-09-27 it
had no password and protected-mode off; only a host firewall rule
(/usr/local/sbin/vera-redis-lan-guard) kept it off the LAN, and node workers
had to be let through that rule to work at all.

It now uses Redis ACL users, one per client, each with its own password:

  * OpenBao is the store of record (KV path below) - it is what provisioning
    hands to a node and what an operator reads to configure redisinsight.
  * The Vera host boots from a LOCAL sealed copy of its own credential
    (Fernet, the key Vera already keeps outside Redis). It cannot read OpenBao
    first: OpenBao's token and unseal keys are themselves stored in Redis, so
    the vault cannot hold the key that opens the store it lives in - the same
    rule security/secrets.py states for OpenBao's own token.
  * Users are persisted by an ACL file on the Redis data volume, so a Redis
    restart does not quietly bring the open default user back.

vikunja can send a password but not a username, so it authenticates as the
`default` user: that user stays enabled, with vikunja's password, rather than
being switched off.

Pure: strings in, strings out. Tested without a Redis.
"""
from __future__ import annotations

import os
import secrets as _secrets
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote

#: ACL rules per user. `~*` keys, `&*` pub/sub channels. The Vera users need
#: everything (they run ACL commands to rotate these very passwords).
USERS: Dict[str, Dict[str, str]] = {
    "vera":      {"rules": "~* &* +@all",
                  "who": "the Vera host - boots from the local sealed copy"},
    "vera-node": {"rules": "~* &* +@all",
                  "who": "node workers - provision.worker puts it in their 0600 env file"},
    "searxng":   {"rules": "~* &* +@all -@admin -@dangerous",
                  "who": "searxng (SEARXNG_REDIS_URL in the LLM_Stack compose)"},
    "inspector": {"rules": "~* &* -@all +@read +@connection -@dangerous",
                  "who": "redisinsight and people browsing - read-only"},
    "default":   {"rules": "~* &* +@all",
                  "who": "vikunja - it sends a password but no username"},
}

#: OpenBao KV v2 path (under the configured mount) for a user's credential.
BAO_PATH = "vera/redis/users/{user}"

#: The Vera host's local sealed copy (a Fernet token of "user:password").
LOCAL_FILE_DEFAULT = "~/.vera/redis.auth"

#: What the ACL file must contain before Redis is restarted with --aclfile:
#: exactly today's behaviour, so switching persistence on changes nothing.
ACL_FILE_SEED = "user default on nopass ~* &* +@all\n"


def local_file(env: Optional[Dict[str, str]] = None) -> str:
    e = os.environ if env is None else env
    return os.path.expanduser(e.get("VERA_REDIS_AUTH_FILE") or LOCAL_FILE_DEFAULT)


def new_password() -> str:
    """256 bits, URL-safe - it travels inside redis:// URLs unescaped."""
    return _secrets.token_urlsafe(32)


def _split(url: str) -> Tuple[str, str, str]:
    """(scheme, userinfo, rest-after-@) - rest is host[:port][/db][?q]."""
    s = str(url or "")
    sep = s.find("://")
    scheme, rest = (s[:sep], s[sep + 3:]) if sep >= 0 else ("redis", s)
    # userinfo ends at the LAST @ before the first '/'
    head = rest.split("/", 1)[0]
    if "@" in head:
        ui, _, _ = head.rpartition("@")
        return scheme, ui, rest[len(ui) + 1:]
    return scheme, "", rest


def has_credentials(url: str) -> bool:
    return bool(_split(url)[1])


def with_credentials(url: str, user: str, password: str) -> str:
    """`url` with user:password, unless it already carries credentials (an
    explicit URL always wins)."""
    scheme, ui, rest = _split(url)
    if ui or not password:
        return url
    return "%s://%s:%s@%s" % (scheme, quote(user or "", safe=""), quote(password, safe=""), rest)


def without_credentials(url: str) -> str:
    """`url` with any user:password removed - so one process's credential is
    never handed on to another client (a node worker gets its OWN user)."""
    scheme, ui, rest = _split(url)
    return "%s://%s" % (scheme, rest) if ui else url


def credentials_of(url: str) -> Tuple[str, str]:
    """(user, password) carried by a URL - ('', '') when none."""
    ui = _split(url)[1]
    if not ui:
        return "", ""
    user, _, pw = ui.partition(":")
    if not _:            # "redis://:pw@" style has user ''; "redis://pw@" is a password only
        return "", unquote(user)
    return unquote(user), unquote(pw)


def redact_url(url: str) -> str:
    """For logs: the password never appears, the user and host do."""
    scheme, ui, rest = _split(url)
    if not ui:
        return url
    user, sep, _ = ui.partition(":")
    return "%s://%s%s@%s" % (scheme, user, ":***" if sep else "", rest) if sep \
        else "%s://***@%s" % (scheme, rest)


def seal_payload(user: str, password: str) -> str:
    return "%s:%s" % (user, password)


def open_payload(text: str) -> Tuple[str, str]:
    user, sep, pw = str(text or "").partition(":")
    return (user, pw) if sep and user and pw else ("", "")


def setuser_args(user: str, *, add: Iterable[str] = (), remove: Iterable[str] = (),
                 reset: bool = False, rules: Optional[str] = None) -> List[str]:
    """Arguments for ACL SETUSER. Passwords are ADDED so a rotation can overlap:
    both work until the old one is removed."""
    if user not in USERS:
        raise ValueError("unknown redis user %r" % user)
    args = [user, "on"]
    if reset:
        args.append("resetpass")
    args += [">" + p for p in add if p]
    args += ["<" + p for p in remove if p]
    args += (rules or USERS[user]["rules"]).split()
    return args


#: Where redis.auth.export_env may write a client's password (colon-separated
#: VERA_REDIS_EXPORT_ROOTS overrides). The stack whose clients need it.
EXPORT_ROOTS_DEFAULT = "/home/boejaker/LLM_Stack"


def export_roots(env: Optional[Dict[str, str]] = None) -> List[str]:
    e = os.environ if env is None else env
    raw = e.get("VERA_REDIS_EXPORT_ROOTS") or EXPORT_ROOTS_DEFAULT
    return [os.path.realpath(r) for r in raw.split(":") if r.strip()]


def export_path_allowed(path: str, roots: Iterable[str]) -> Tuple[bool, str]:
    """Only an existing `.env` file under an allowed root - resolved, so a
    symlink or `..` cannot walk out of it."""
    real = os.path.realpath(path or "")
    name = os.path.basename(real)
    if not (name == ".env" or name.endswith(".env")):
        return False, "not an env file (must be named .env or *.env): %s" % real
    for root in roots:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True, real
    return False, "outside the allowed roots %s: %s" % (list(roots), real)


def env_var_ok(var: str) -> bool:
    return bool(var) and var.replace("_", "").isalnum() and var[0].isalpha() and var.upper() == var


def env_file_update(text: str, var: str, value: str) -> Tuple[str, str]:
    """(new_text, 'updated' | 'appended'): set VAR=value, keeping the file's
    own line endings (the LLM_Stack files are CRLF) and every other line."""
    if not env_var_ok(var):
        raise ValueError("bad variable name %r" % var)
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(var + "=") or stripped.startswith("export " + var + "="):
            lines[i] = "%s=%s" % (var, value)
            return nl.join(lines), "updated"
    body = text if (not text or text.endswith(nl)) else text + nl
    return body + "%s=%s%s" % (var, value, nl), "appended"


def default_lock_plan(clients: Iterable[Dict[str, str]],
                      allowed_default_addrs: Iterable[str] = ()) -> Dict[str, object]:
    """May the `default` user lose `nopass`? Only when every client still on it
    is one that will send the new password (vikunja). Anything else would be
    locked out - so it is named and the lock refused."""
    allowed = {a.strip() for a in allowed_default_addrs if a and a.strip()}
    unexpected = []
    for c in clients or ():
        if (c.get("user") or "default") != "default":
            continue
        ip = str(c.get("addr") or "").rsplit(":", 1)[0]
        if ip not in allowed:
            unexpected.append({"addr": c.get("addr"), "name": c.get("name") or "",
                               "age": c.get("age"), "cmd": c.get("cmd")})
    return {"ok": not unexpected, "unexpected": unexpected}
