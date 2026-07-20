"""
secrets.py — Shared authenticated encryption for stored credentials
===================================================================

Vera modules store *account-level* secrets in Redis: Google OAuth client
secrets + refresh tokens, CalDAV / IMAP / SMTP app-passwords, the Telegram bot
token. These must never sit in Redis in plaintext (nor under the weak XOR
"obfuscation" used elsewhere).

Two interchangeable backends behind one API:

  • **Fernet** (default) — AES-128-CBC + HMAC-SHA256; the ciphertext lives inline
    in Redis, the key out-of-band (env or 0600 file). Zero-dependency, always
    available; also the bootstrap store for OpenBao's OWN token (the vault can't
    hold the key that opens it).

  • **OpenBao** (opt-in, when stood up) — the store of record: the plaintext is
    written to OpenBao KV v2 and only an opaque *reference* (`bao:v1:<mount>:<path>`)
    is kept in Redis, so credentials never sit in Redis at all and gain audit /
    rotation / central revocation. Activated by environment (below); Vera's
    provisioning/auto-enrol flow exports these after unsealing the vault.

Backend selection (checked at seal time; reads always handle BOTH token forms
so switching backends migrates values lazily — old `fernet:` tokens keep
opening, new secrets seal to `bao:`):

  OpenBao is used when `VERA_SECRET_BACKEND=openbao` OR both an address
  (`BAO_ADDR` | `VAULT_ADDR`) and token (`BAO_TOKEN` | `VAULT_TOKEN`) are set.
  Optional: `BAO_KV_MOUNT` (default "secret"), `BAO_NAMESPACE`, `BAO_VERIFY_TLS`.
  If an OpenBao write ever fails we fall back to Fernet so a secret is never lost.

Master (Fernet) key resolution (never stored in Redis next to ciphertext):
  1. VERA_SECRET_KEY env var — a urlsafe-base64 32-byte Fernet key (preferred).
  2. Fallback: ~/.vera/secret.key — generated on first use, written 0o600.

Public API
──────────
  seal(plaintext, *, force_fernet=False)  -> "fernet:…" | "bao:…"  ("" passes through)
  open_secret(token)                      -> plaintext (tolerates legacy plaintext
                                             + corrupt tokens -> "")
  is_sealed(value)     -> bool
  redact(value)        -> "••••" style hint, never the secret
  backend_status()     -> {backend, openbao_active, addr, mount}  (diagnostics)
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger("vera.security.secrets")

_PREFIX = "fernet:"
_BAO_PREFIX = "bao:v1:"          # bao:v1:<mount>:<path>
_KEY_FILE = Path(os.path.expanduser("~")) / ".vera" / "secret.key"

# Lazily-built singletons so import never fails even if cryptography is missing.
_fernet_obj = None
_init_done = False

# OpenBao availability is probed lazily and cached briefly (an unseal/exports can
# flip it on at runtime).
_bao_cache: dict = {"active": None, "checked": 0.0}
_BAO_TTL = 30.0


def _resolve_key() -> bytes:
    """Return a 32-byte urlsafe-base64 Fernet key from env or key file."""
    from cryptography.fernet import Fernet

    env = os.getenv("VERA_SECRET_KEY", "").strip()
    if env:
        return env.encode("ascii")

    # Fall back to a 0o600 key file kept separate from Redis.
    try:
        if _KEY_FILE.exists():
            return _KEY_FILE.read_text(encoding="ascii").strip().encode("ascii")
    except Exception as e:
        log.warning("security.secrets: could not read key file %s: %s", _KEY_FILE, e)

    key = Fernet.generate_key()
    try:
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(key.decode("ascii"), encoding="ascii")
        try:
            os.chmod(_KEY_FILE, 0o600)
        except Exception:
            pass  # chmod is a no-op / best-effort on Windows
        log.warning(
            "security.secrets: generated a new master key at %s (0600). "
            "Set VERA_SECRET_KEY in the environment for production so the key "
            "is managed out-of-band and survives a home-dir wipe.",
            _KEY_FILE,
        )
    except Exception as e:
        log.error("security.secrets: could not persist key file %s: %s — "
                  "secrets will not survive a restart unless VERA_SECRET_KEY "
                  "is set.", _KEY_FILE, e)
    return key


def _fernet():
    """Build (once) and return the Fernet cipher, or None if unavailable."""
    global _fernet_obj, _init_done
    if _init_done:
        return _fernet_obj
    _init_done = True
    try:
        from cryptography.fernet import Fernet
        _fernet_obj = Fernet(_resolve_key())
    except ImportError:
        log.error("security.secrets: the 'cryptography' package is not "
                  "installed — credentials cannot be encrypted. "
                  "Install it (pip install cryptography) before configuring "
                  "any secret-bearing source.")
        _fernet_obj = None
    except Exception as e:
        log.error("security.secrets: failed to initialise cipher: %s", e)
        _fernet_obj = None
    return _fernet_obj


# ── OpenBao KV v2 backend (sync httpx; env-driven so this stays a leaf module) ─
def _bao_cfg() -> dict:
    """Resolve OpenBao connection from the environment (or {} if not set)."""
    addr = (os.getenv("BAO_ADDR") or os.getenv("VAULT_ADDR") or "").strip().rstrip("/")
    token = (os.getenv("BAO_TOKEN") or os.getenv("VAULT_TOKEN") or "").strip()
    forced = os.getenv("VERA_SECRET_BACKEND", "").strip().lower() == "openbao"
    if not (addr and token):
        return {}
    if not forced and os.getenv("VERA_SECRET_BACKEND", "").strip().lower() not in ("", "openbao", "auto"):
        return {}
    return {
        "addr": addr, "token": token,
        "mount": (os.getenv("BAO_KV_MOUNT") or "secret").strip().strip("/"),
        "namespace": (os.getenv("BAO_NAMESPACE") or "").strip(),
        "verify": os.getenv("BAO_VERIFY_TLS", "").strip().lower() in ("1", "true", "yes"),
    }


def _bao_headers(cfg: dict) -> dict:
    h = {"X-Vault-Token": cfg["token"]}
    if cfg.get("namespace"):
        h["X-Vault-Namespace"] = cfg["namespace"]
    return h


def _bao_active() -> bool:
    """Is an OpenBao backend configured AND healthy (unsealed)? Cached ~30s."""
    now = time.time()
    if _bao_cache["active"] is not None and (now - _bao_cache["checked"]) < _BAO_TTL:
        return _bao_cache["active"]
    _bao_cache["checked"] = now
    cfg = _bao_cfg()
    active = False
    if cfg:
        try:
            import httpx
            r = httpx.get(f"{cfg['addr']}/v1/sys/health",
                          verify=cfg["verify"], timeout=3.0)
            # 200 = unsealed+active. Anything else (sealed/standby) → not usable.
            active = (r.status_code == 200)
        except Exception as e:
            log.debug("security.secrets: OpenBao health probe failed: %s", e)
            active = False
    _bao_cache["active"] = active
    return active


def _bao_put(plaintext: str) -> str:
    """Write a secret to OpenBao KV v2, return its reference token or "" on failure."""
    cfg = _bao_cfg()
    if not cfg:
        return ""
    path = f"vera-secrets/{uuid.uuid4().hex}"
    try:
        import httpx
        r = httpx.post(
            f"{cfg['addr']}/v1/{cfg['mount']}/data/{path}",
            headers=_bao_headers(cfg), verify=cfg["verify"], timeout=6.0,
            json={"data": {"v": plaintext}},
        )
        if r.status_code in (200, 204):
            return f"{_BAO_PREFIX}{cfg['mount']}:{path}"
        log.warning("security.secrets: OpenBao write HTTP %s — falling back to Fernet",
                    r.status_code)
    except Exception as e:
        log.warning("security.secrets: OpenBao write failed (%s) — falling back to Fernet", e)
    return ""


def _bao_get(token: str) -> str:
    """Read a secret back from an OpenBao reference token."""
    cfg = _bao_cfg()
    if not cfg:
        log.warning("security.secrets: got a bao: token but OpenBao is not configured.")
        return ""
    try:
        mount, path = token[len(_BAO_PREFIX):].split(":", 1)
    except ValueError:
        return ""
    try:
        import httpx
        r = httpx.get(f"{cfg['addr']}/v1/{mount}/data/{path}",
                      headers=_bao_headers(cfg), verify=cfg["verify"], timeout=6.0)
        if r.status_code == 200:
            return (((r.json() or {}).get("data") or {}).get("data") or {}).get("v", "") or ""
        log.warning("security.secrets: OpenBao read HTTP %s for %s", r.status_code, path)
    except Exception as e:
        log.warning("security.secrets: OpenBao read failed: %s", e)
    return ""


def is_sealed(value: str) -> bool:
    return isinstance(value, str) and (value.startswith(_PREFIX)
                                       or value.startswith(_BAO_PREFIX))


def _fernet_seal(plaintext: str) -> str:
    f = _fernet()
    if not f:
        # Fail closed: never silently store plaintext for a value the caller
        # asked to seal. Caller treats "" as "no secret stored".
        raise RuntimeError(
            "security.secrets: cannot seal — encryption unavailable "
            "(install 'cryptography' or set VERA_SECRET_KEY)."
        )
    return _PREFIX + f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def seal(plaintext: str, *, force_fernet: bool = False) -> str:
    """Encrypt a secret for storage. Empty input passes through unchanged.

    Uses OpenBao when it's stood up and healthy (only a reference is stored in
    Redis); otherwise Fernet inline. An OpenBao write failure transparently
    falls back to Fernet so a secret is never lost. `force_fernet=True` pins the
    Fernet backend — used for OpenBao's OWN bootstrap secrets (the token that
    opens the vault can't be stored inside the vault).

    Already-sealed values are returned as-is (idempotent), so re-saving a record
    that still carries a sealed secret does not double-encrypt.
    """
    if not plaintext:
        return ""
    if is_sealed(plaintext):
        return plaintext
    if not force_fernet and _bao_active():
        ref = _bao_put(plaintext)
        if ref:
            return ref
        # fall through to Fernet on any OpenBao failure
    return _fernet_seal(plaintext)


def open_secret(value: str) -> str:
    """Decrypt a sealed secret. Tolerates legacy plaintext and corrupt tokens.

    - bao: reference -> plaintext fetched from OpenBao
    - fernet: token  -> plaintext (still works after switching to OpenBao)
    - legacy plain   -> returned as-is (one-time migration tolerance)
    - corrupt/blank  -> "" with a warning
    """
    if not value:
        return ""
    if value.startswith(_BAO_PREFIX):
        return _bao_get(value)
    if not value.startswith(_PREFIX):
        # Legacy/plaintext value written before encryption existed.
        return value
    f = _fernet()
    if not f:
        return ""
    try:
        return f.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as e:
        log.warning("security.secrets: stored credential is corrupt or was "
                    "sealed with a different key — re-enter it. (%s)", e)
        return ""


def backend_status() -> dict:
    """Diagnostics for the Provision/Security panel: which backend is live."""
    cfg = _bao_cfg()
    return {
        "backend": "openbao" if (cfg and _bao_active()) else "fernet",
        "openbao_configured": bool(cfg),
        "openbao_active": bool(cfg) and _bao_active(),
        "addr": cfg.get("addr", "") if cfg else "",
        "mount": cfg.get("mount", "") if cfg else "",
    }


def redact(value: str) -> str:
    """Return a UI-safe hint that a secret is set, never the secret itself."""
    return "••••••••" if value else ""
