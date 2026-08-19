# 29 · Security & Secrets

> **Doc status:** concise reference for `security/`. Expand as the surface grows.

`security/secrets.py` provides shared authenticated encryption for the *account-level* secrets that Vera modules store in Redis: Google OAuth client secrets + refresh tokens, CalDAV / IMAP / SMTP app-passwords, and the Telegram bot token. These must never sit in Redis in plaintext.

The module seals such secrets with **Fernet** (AES-128-CBC + HMAC-SHA256) before they are persisted, and only ever opens them server-side at use time. It is a small library (not a capability group) imported by the [Integrations](./23-integrations.md) modules.

---

## Master key resolution

The key is **never** stored in Redis next to the ciphertext. Resolution order:

1. **`VERA_SECRET_KEY`** env var — a urlsafe-base64 32-byte Fernet key. Preferred for production; lets the operator manage the key out-of-band. (Generate with `make secret`.)
2. **Fallback:** `~/.vera/secret.key` — generated on first use and written `0o600`, with a warning logged recommending the env var for production.

> If `VERA_SECRET_KEY` changes, previously sealed secrets become undecryptable — keep it stable. This is the same key referenced throughout [Configuration](./10-configuration.md) and the deployment scaffolding.

---

## Public API

| Function | Returns |
|---|---|
| `seal(plaintext)` | `"fernet:<token>"` (empty string passes through) |
| `open_secret(token)` | plaintext (tolerates legacy plaintext + corrupt tokens → `""`) |
| `is_sealed(value)` | bool |
| `redact(value)` | a `••••`-style hint, never the secret |

`redact()` is what the Calendar / Email / Accounts panels show instead of credentials — the UI only ever sees `has_*` flags and redacted hints, never the plaintext.

**Used by:** Calendar (cloud sources), Telegram (bot token), Email (IMAP/SMTP), Accounts (the registry that holds them all).

---

## See also

- [Integrations](./23-integrations.md) — the Accounts registry and the modules whose secrets this seals
- [Configuration](./10-configuration.md) — `VERA_SECRET_KEY` and key management

## Screenshots

## Security boundaries

Vera's sealed-secret support protects stored credentials at rest; it does not
make arbitrary capability arguments, logs, screenshots, prompts, or generated
files secret. Keep plaintext values within the account/provider configuration
path and pass references elsewhere. Redaction is defense in depth, not a reason
to put secrets into observable channels.

Authorization is enforced at multiple layers: capability policy, agent/tool
allowlists, integration access, Operator host/destructive-action policy, and
explicit confirmation for high-impact operations. UI visibility alone is never
an authorization boundary. External content—including recalled memory and web
pages—must remain data and cannot grant itself additional tools.

Operational checks include secret-key availability, decryptability after
restart, file ownership/permissions, token scope and expiry, audit-event
coverage, and absence of credentials in logs. Rotate a credential if exposure
is suspected; deleting a log entry is not remediation.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
