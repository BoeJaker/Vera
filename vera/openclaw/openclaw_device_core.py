"""
openclaw_device_core.py  —  the OpenClaw connect contract, app-free
====================================================================
The pure half of the OpenClaw bridge: the values the gateway's connect schema
actually accepts, and the Ed25519 device proof it verifies. No Vera imports, so
tests reach it as `vera.openclaw.openclaw_device_core` without booting the app.

Ground truth is OpenClaw's own published contract (`@openclaw/gateway-protocol`
`protocol.schema.json` + the `openclaw` package's
`packages/gateway-client/src/device-auth.ts`, read at 2026.9.1):

  • `client.id` and `client.mode` are CLOSED enums. An invented id such as
    "vera-bridge", or "operator" used as a *mode* (it is a role, not a mode),
    is refused by the schema with "must be equal to one of the allowed values".
  • `device.publicKey` / `device.signature` are `minLength: 1` — empty
    placeholders never pass. The gateway wants a real Ed25519 proof bound to
    the `connect.challenge` nonce it just issued.

The signed payload is a pipe-joined string. Three versions exist; a gateway
verifies the ones it knows, so the caller tries newest first and falls back:

    v3 | deviceId | clientId | clientMode | role | scopes | signedAt | token | nonce | platform | deviceFamily
    v2 | deviceId | clientId | clientMode | role | scopes | signedAt | token | nonce
    v1 | deviceId | clientId | clientMode | role | scopes | signedAt | token

`scopes` is the normalised list joined with ","; `platform`/`deviceFamily` are
trimmed and ASCII-lowercased. Public key and signature travel as unpadded
base64url of the RAW 32-byte key / 64-byte signature, and the device id is
`sha256(raw public key)` in hex — that is what the gateway fingerprints.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

# ── the closed enums (protocol.schema.json → ConnectParams.client) ────────────
GATEWAY_CLIENT_IDS: Tuple[str, ...] = (
    "webchat-ui",
    "openclaw-control-ui",
    "openclaw-browser-copilot",
    "openclaw-tui",
    "webchat",
    "cli",
    "gateway-client",
    "openclaw-macos",
    "openclaw-linux",
    "openclaw-ios",
    "openclaw-watchos",
    "openclaw-android",
    "node-host",
    "openclaw-worker",
    "test",
    "fingerprint",
    "openclaw-probe",
)

GATEWAY_CLIENT_MODES: Tuple[str, ...] = (
    "webchat",
    "cli",
    "ui",
    "backend",
    "node",
    "worker",
    "probe",
    "test",
)

# Vera is a headless operator client, so it presents as the generic CLI client.
DEFAULT_CLIENT_ID = "cli"
DEFAULT_CLIENT_MODE = "cli"
DEFAULT_ROLE = "operator"
DEFAULT_SCOPES: Tuple[str, ...] = ("operator.read", "operator.write")

# A trusted local backend (and ONLY on a direct loopback connection) may omit
# the device proof and authenticate with the shared gateway token alone.
LOOPBACK_BACKEND_CLIENT_ID = "gateway-client"
LOOPBACK_BACKEND_CLIENT_MODE = "backend"

MIN_PROTOCOL = 3
MAX_PROTOCOL = 4

# Newest first — the gateway decides which it can verify.
PAYLOAD_VERSIONS: Tuple[str, ...] = ("v3", "v2", "v1")

# `details.code` values that mean "the proof itself was wrong" — worth one
# retry with an older payload shape rather than backing off.
DEVICE_AUTH_RETRY_CODES = frozenset({
    "DEVICE_AUTH_INVALID",
    "DEVICE_AUTH_SIGNATURE_INVALID",
    "DEVICE_AUTH_NONCE_REQUIRED",
    "DEVICE_AUTH_NONCE_MISMATCH",
    "DEVICE_AUTH_DEVICE_ID_MISMATCH",
})

# Codes where retrying anything is pointless until a human acts.
DEVICE_AUTH_TERMINAL_CODES = frozenset({
    "PAIRING_REQUIRED",
    "AUTH_TOKEN_MISSING",
    "AUTH_TOKEN_MISMATCH",
    "AUTH_PASSWORD_MISSING",
    "AUTH_PASSWORD_MISMATCH",
    "AUTH_SCOPE_MISMATCH",
    "PROTOCOL_MISMATCH",
})


# ── normalisation (mirrors shared/device-auth.ts + client-info.ts) ────────────

def normalize_metadata(value: Optional[str]) -> str:
    """Trim, then ASCII-lowercase — the v3 payload's platform/deviceFamily rule."""
    if not isinstance(value, str):
        return ""
    trimmed = value.strip()
    if not trimmed:
        return ""
    return "".join(
        chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch
        for ch in trimmed
    )


def normalize_role(role: Optional[str]) -> str:
    """Roles keep their case and namespace; only surrounding space is dropped."""
    return role.strip() if isinstance(role, str) else ""


def normalize_scopes(scopes: Optional[Iterable[str]]) -> List[str]:
    """Dedupe, add the implied operator scopes, and sort — the gateway signs the
    normalised list, so a client that signs the raw one fails verification."""
    if not isinstance(scopes, (list, tuple, set, frozenset)):
        return []
    out = set()
    for scope in scopes:
        if not isinstance(scope, str):
            continue
        trimmed = scope.strip()
        if trimmed:
            out.add(trimmed)
    if "operator.admin" in out:
        out.update({"operator.read", "operator.write"})
    elif "operator.write" in out:
        out.add("operator.read")
    return sorted(out)


def validate_client_identity(client_id: str, client_mode: str) -> Tuple[bool, str]:
    """Check the pair against the closed enums BEFORE dialling, so a bad config
    reads as a config error instead of a schema rejection mid-handshake."""
    if client_id not in GATEWAY_CLIENT_IDS:
        return False, (
            f"client.id {client_id!r} is not accepted by the OpenClaw gateway — "
            f"use one of: {', '.join(GATEWAY_CLIENT_IDS)}"
        )
    if client_mode not in GATEWAY_CLIENT_MODES:
        return False, (
            f"client.mode {client_mode!r} is not accepted by the OpenClaw gateway "
            f"('operator' is a ROLE, not a mode) — use one of: "
            f"{', '.join(GATEWAY_CLIENT_MODES)}"
        )
    return True, ""


def is_loopback_backend(client_id: str, client_mode: str) -> bool:
    """True for the one identity allowed to connect without a device proof."""
    return (client_id == LOOPBACK_BACKEND_CLIENT_ID
            and client_mode == LOOPBACK_BACKEND_CLIENT_MODE)


# ── base64url, exactly as Node's Buffer.toString("base64url") ────────────────

def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


# ── device identity ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceIdentity:
    """A persisted Ed25519 identity. `device_id` is what an operator approves."""
    device_id: str            # sha256(raw public key), hex
    public_key: str           # unpadded base64url of the raw 32-byte public key
    private_key_pem: str      # PKCS#8 PEM — sealed before it touches disk

    def public(self) -> dict:
        """The non-secret half, safe to return from a status endpoint."""
        return {"device_id": self.device_id, "public_key": self.public_key}


def _ed25519():
    """Imported lazily so the payload/enum logic stays usable (and testable)
    without `cryptography` installed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    return serialization, ed25519


def device_id_for_public_key(raw_public_key: bytes) -> str:
    return hashlib.sha256(raw_public_key).hexdigest()


def identity_from_private_pem(private_key_pem: str) -> DeviceIdentity:
    """Rebuild the identity (id + public key) from stored private key material."""
    serialization, ed25519 = _ed25519()
    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError("OpenClaw device identity must be an Ed25519 key")
    raw_public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return DeviceIdentity(
        device_id=device_id_for_public_key(raw_public),
        public_key=b64url_encode(raw_public),
        private_key_pem=private_key_pem,
    )


def generate_identity() -> DeviceIdentity:
    """Mint a fresh Ed25519 device identity. The caller persists it — a new key
    on every connect would ask the operator to re-approve a new device forever."""
    serialization, ed25519 = _ed25519()
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return identity_from_private_pem(pem)


def sign_payload(private_key_pem: str, payload: str) -> str:
    """Ed25519-sign the pipe-joined payload; unpadded base64url on the wire."""
    serialization, ed25519 = _ed25519()
    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError("OpenClaw device identity must be an Ed25519 key")
    return b64url_encode(key.sign(payload.encode("utf-8")))


# ── the signed payload ───────────────────────────────────────────────────────

def build_device_auth_payload(
    version: str,
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: Sequence[str],
    signed_at_ms: int,
    token: str = "",
    nonce: str = "",
    platform: str = "",
    device_family: str = "",
) -> str:
    """Reproduce buildDeviceAuthPayload{,V3} byte for byte."""
    joined_scopes = ",".join(scopes)
    token = token or ""
    if version == "v3":
        parts = [
            "v3", device_id, client_id, client_mode, role, joined_scopes,
            str(signed_at_ms), token, nonce,
            normalize_metadata(platform), normalize_metadata(device_family),
        ]
    elif version == "v2":
        parts = [
            "v2", device_id, client_id, client_mode, role, joined_scopes,
            str(signed_at_ms), token, nonce,
        ]
    elif version == "v1":
        parts = [
            "v1", device_id, client_id, client_mode, role, joined_scopes,
            str(signed_at_ms), token,
        ]
    else:
        raise ValueError(
            f"unknown device-auth payload version {version!r} — "
            f"expected one of {', '.join(PAYLOAD_VERSIONS)}"
        )
    return "|".join(parts)


def build_connect_params(
    *,
    identity: Optional[DeviceIdentity],
    nonce: str,
    signed_at_ms: int,
    client_id: str = DEFAULT_CLIENT_ID,
    client_mode: str = DEFAULT_CLIENT_MODE,
    client_version: str = "1.0.0",
    display_name: str = "",
    platform: str = "linux",
    role: str = DEFAULT_ROLE,
    scopes: Sequence[str] = DEFAULT_SCOPES,
    token: str = "",
    caps: Sequence[str] = (),
    locale: str = "en-US",
    user_agent: str = "",
    payload_version: str = "v3",
    min_protocol: int = MIN_PROTOCOL,
    max_protocol: int = MAX_PROTOCOL,
) -> dict:
    """Build `connect.params`, device proof included.

    `identity` may be None only for the loopback-backend identity, which the
    gateway lets through on the shared token alone.
    """
    ok, err = validate_client_identity(client_id, client_mode)
    if not ok:
        raise ValueError(err)

    norm_role = normalize_role(role) or DEFAULT_ROLE
    norm_scopes = normalize_scopes(scopes)

    client: dict = {
        "id": client_id,
        "version": client_version,
        "platform": platform,
        "mode": client_mode,
    }
    if display_name:
        client["displayName"] = display_name

    params: dict = {
        "minProtocol": min_protocol,
        "maxProtocol": max_protocol,
        "client": client,
        "role": norm_role,
        "scopes": norm_scopes,
        "caps": list(caps),
        "auth": {"token": token or ""},
        "locale": locale,
        "userAgent": user_agent or f"vera-openclaw-bridge/{client_version}",
    }

    if identity is None:
        if not is_loopback_backend(client_id, client_mode):
            raise ValueError(
                "a device identity is required unless connecting as the "
                f"loopback backend ({LOOPBACK_BACKEND_CLIENT_ID}/"
                f"{LOOPBACK_BACKEND_CLIENT_MODE})"
            )
        return params

    payload = build_device_auth_payload(
        payload_version,
        device_id=identity.device_id,
        client_id=client_id,
        client_mode=client_mode,
        role=norm_role,
        scopes=norm_scopes,
        signed_at_ms=signed_at_ms,
        token=token,
        nonce=nonce,
        platform=platform,
    )
    params["device"] = {
        "id": identity.device_id,
        "publicKey": identity.public_key,
        "signature": sign_payload(identity.private_key_pem, payload),
        "signedAt": signed_at_ms,
        "nonce": nonce,
    }
    return params


# ── reading the gateway's refusal ────────────────────────────────────────────

def connect_error_code(error: Optional[dict]) -> str:
    """The structured code, which lives in `details.code` and falls back to the
    frame's own `code`."""
    if not isinstance(error, dict):
        return ""
    details = error.get("details")
    if isinstance(details, dict):
        code = details.get("code")
        if isinstance(code, str) and code:
            return code
    code = error.get("code")
    return code if isinstance(code, str) else ""


def describe_connect_error(code: str, message: str, device_id: str = "") -> str:
    """Turn a connect refusal into the next action, not just a restatement."""
    device = device_id or "(no device identity)"
    hints = {
        "PAIRING_REQUIRED": (
            f"gateway wants this device approved — approve device {device} in "
            "the OpenClaw Control UI (Devices) or with `openclaw devices`, then "
            "it reconnects on its own"
        ),
        "AUTH_TOKEN_MISSING": (
            "gateway requires its shared token — set OPENCLAW_TOKEN (or "
            "openclaw.config.set token=…) to the gateway's own "
            "`gateway.auth.token` (openclaw.json / OPENCLAW_GATEWAY_TOKEN)"
        ),
        "AUTH_TOKEN_MISMATCH": (
            "the configured token does not match the gateway's "
            "`gateway.auth.token`"
        ),
        "AUTH_SCOPE_MISMATCH": (
            "the gateway refused the requested scopes — approve the upgrade on "
            f"device {device}, or ask for fewer scopes"
        ),
        "PROTOCOL_MISMATCH": (
            f"no shared protocol version — Vera offers {MIN_PROTOCOL}..{MAX_PROTOCOL}"
        ),
        "DEVICE_IDENTITY_REQUIRED": (
            "the gateway requires a device proof on this connection (the "
            "no-device path is loopback-backend only)"
        ),
    }
    hint = hints.get(code, "")
    base = f"{code}: {message}" if code else (message or "handshake failed")
    return f"{base} — {hint}" if hint else base
