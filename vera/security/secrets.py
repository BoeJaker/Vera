"""
secrets.py — Shared authenticated encryption for stored credentials
===================================================================

Vera modules store *account-level* secrets in Redis: Google OAuth client
secrets + refresh tokens, CalDAV / IMAP / SMTP app-passwords, the Telegram bot
token. These must never sit in Redis in plaintext (nor under the weak XOR
"obfuscation" used elsewhere).

This module seals such secrets with Fernet (AES-128-CBC + HMAC-SHA256) before
they are persisted, and only ever opens them server-side at use time.

Master key resolution (the key is NEVER stored in Redis next to ciphertext):

  1. VERA_SECRET_KEY env var — a urlsafe-base64 32-byte Fernet key. Preferred
     for production; lets the operator manage the key out-of-band.
  2. Fallback: ~/.vera/secret.key — generated on first use and written 0o600.
     A warning is logged recommending the env var for production.

Used by: calendar (cloud sources), telegram (bot token), email (IMAP/SMTP).

Public API
──────────
  seal(plaintext)      -> "fernet:<token>"   (empty string passes through)
  open_secret(token)   -> plaintext          (tolerates legacy plaintext +
                                               corrupt tokens -> "")
  is_sealed(value)     -> bool
  redact(value)        -> "••••" style hint, never the secret
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("vera.security.secrets")

_PREFIX = "fernet:"
_KEY_FILE = Path(os.path.expanduser("~")) / ".vera" / "secret.key"

# Lazily-built singletons so import never fails even if cryptography is missing.
_fernet_obj = None
_init_done = False


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


def is_sealed(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def seal(plaintext: str) -> str:
    """Encrypt a secret for storage. Empty input passes through unchanged.

    Already-sealed values are returned as-is (idempotent), so re-saving a
    record that still carries a sealed secret does not double-encrypt.
    """
    if not plaintext:
        return ""
    if is_sealed(plaintext):
        return plaintext
    f = _fernet()
    if not f:
        # Fail closed: never silently store plaintext for a value the caller
        # asked to seal. Caller treats "" as "no secret stored".
        raise RuntimeError(
            "security.secrets: cannot seal — encryption unavailable "
            "(install 'cryptography' or set VERA_SECRET_KEY)."
        )
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def open_secret(value: str) -> str:
    """Decrypt a sealed secret. Tolerates legacy plaintext and corrupt tokens.

    - sealed token  -> plaintext
    - legacy plain  -> returned as-is (one-time migration tolerance)
    - corrupt/blank -> "" with a warning
    """
    if not value:
        return ""
    if not is_sealed(value):
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


def redact(value: str) -> str:
    """Return a UI-safe hint that a secret is set, never the secret itself."""
    return "••••••••" if value else ""
