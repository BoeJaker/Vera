"""The OpenClaw connect handshake must satisfy the gateway's own schema.

The bridge shipped with an invented `client.id` ("vera-bridge"), a *role* in the
`client.mode` slot ("operator"), and empty `device.publicKey`/`device.signature`
placeholders — four schema violations, so every connect was refused before it
began:

    invalid connect params: at /client/id: must be equal to one of the allowed
    values; at /client/mode: …; at /device/publicKey: must NOT have fewer than 1
    characters; at /device/signature: …

These tests pin the wire contract against OpenClaw's published schema
(`@openclaw/gateway-protocol` protocol.schema.json) and its device-auth payload
builder, because a drift here is invisible in Vera and only shows up as a
gateway that will not talk to us.
"""
import base64
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.openclaw import openclaw_device_core as dev  # noqa: E402

# ConnectParams.properties, which is `additionalProperties: false` — anything
# else Vera invents is refused wholesale.
ALLOWED_PARAM_KEYS = {
    "minProtocol", "maxProtocol", "client", "caps", "commands", "computerUse",
    "workerRuns", "permissions", "pathEnv", "role", "scopes", "device", "auth",
    "locale", "userAgent",
}
ALLOWED_CLIENT_KEYS = {
    "id", "displayName", "version", "buildId", "platform", "deviceFamily",
    "modelIdentifier", "timeZone", "mode", "instanceId",
}
ALLOWED_DEVICE_KEYS = {"id", "publicKey", "signature", "signedAt", "nonce"}

def _has_cryptography() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


requires_crypto = pytest.mark.skipif(
    not _has_cryptography(), reason="cryptography not installed in this env"
)


# ── the enums, which are closed ───────────────────────────────────────────────

def test_the_shipped_identity_is_rejected_before_dialling():
    ok, why = dev.validate_client_identity("vera-bridge", "operator")
    assert not ok
    assert "vera-bridge" in why


def test_operator_is_a_role_not_a_mode():
    ok, why = dev.validate_client_identity("cli", "operator")
    assert not ok
    assert "ROLE" in why or "role" in why


def test_the_generic_cli_operator_identity_is_accepted():
    assert dev.validate_client_identity(dev.DEFAULT_CLIENT_ID,
                                        dev.DEFAULT_CLIENT_MODE) == (True, "")


def test_defaults_are_members_of_the_enums():
    assert dev.DEFAULT_CLIENT_ID in dev.GATEWAY_CLIENT_IDS
    assert dev.DEFAULT_CLIENT_MODE in dev.GATEWAY_CLIENT_MODES
    assert dev.LOOPBACK_BACKEND_CLIENT_ID in dev.GATEWAY_CLIENT_IDS
    assert dev.LOOPBACK_BACKEND_CLIENT_MODE in dev.GATEWAY_CLIENT_MODES


# ── normalisation the gateway repeats on its side ─────────────────────────────

def test_write_implies_read_and_scopes_are_sorted():
    assert dev.normalize_scopes(["operator.write"]) == ["operator.read",
                                                        "operator.write"]


def test_admin_implies_both_and_duplicates_collapse():
    assert dev.normalize_scopes(["operator.admin", "operator.admin", " "]) == [
        "operator.admin", "operator.read", "operator.write"
    ]


def test_metadata_is_trimmed_and_ascii_lowercased():
    assert dev.normalize_metadata("  Linux  ") == "linux"
    assert dev.normalize_metadata(None) == ""


# ── the signed payload, byte for byte ─────────────────────────────────────────

_PAYLOAD_ARGS = dict(
    device_id="d1", client_id="cli", client_mode="cli", role="operator",
    scopes=["operator.read", "operator.write"], signed_at_ms=1737264000000,
    token="tok", nonce="n1", platform="Linux",
)


def test_v3_payload_matches_the_gateway_builder():
    assert dev.build_device_auth_payload("v3", **_PAYLOAD_ARGS) == (
        "v3|d1|cli|cli|operator|operator.read,operator.write"
        "|1737264000000|tok|n1|linux|"
    )


def test_v2_payload_ends_at_the_nonce():
    assert dev.build_device_auth_payload("v2", **_PAYLOAD_ARGS) == (
        "v2|d1|cli|cli|operator|operator.read,operator.write"
        "|1737264000000|tok|n1"
    )


def test_v1_payload_carries_no_nonce():
    assert dev.build_device_auth_payload("v1", **_PAYLOAD_ARGS) == (
        "v1|d1|cli|cli|operator|operator.read,operator.write|1737264000000|tok"
    )


def test_an_absent_token_signs_as_an_empty_field_not_none():
    args = dict(_PAYLOAD_ARGS, token="")
    assert dev.build_device_auth_payload("v2", **args).endswith("|1737264000000||n1")


def test_an_unknown_payload_version_is_refused():
    with pytest.raises(ValueError):
        dev.build_device_auth_payload("v9", **_PAYLOAD_ARGS)


def test_the_fallback_ladder_runs_newest_first():
    assert dev.PAYLOAD_VERSIONS == ("v3", "v2", "v1")


# ── the device identity itself ────────────────────────────────────────────────

@requires_crypto
def test_device_id_is_the_sha256_of_the_raw_public_key():
    identity = dev.generate_identity()
    raw = dev.b64url_decode(identity.public_key)
    assert len(raw) == 32
    assert identity.device_id == hashlib.sha256(raw).hexdigest()
    assert len(identity.device_id) == 64


@requires_crypto
def test_the_public_key_travels_as_unpadded_base64url():
    identity = dev.generate_identity()
    assert "=" not in identity.public_key
    assert "+" not in identity.public_key and "/" not in identity.public_key


@requires_crypto
def test_an_identity_reloaded_from_its_pem_keeps_its_fingerprint():
    identity = dev.generate_identity()
    again = dev.identity_from_private_pem(identity.private_key_pem)
    assert again.device_id == identity.device_id
    assert again.public_key == identity.public_key


@requires_crypto
def test_the_signature_verifies_against_the_advertised_public_key():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    identity = dev.generate_identity()
    payload = dev.build_device_auth_payload(
        "v3", **dict(_PAYLOAD_ARGS, device_id=identity.device_id))
    signature = dev.sign_payload(identity.private_key_pem, payload)

    public = ed25519.Ed25519PublicKey.from_public_bytes(
        dev.b64url_decode(identity.public_key))
    public.verify(dev.b64url_decode(signature), payload.encode("utf-8"))


def test_base64url_round_trips_without_padding():
    raw = bytes(range(32))
    encoded = dev.b64url_encode(raw)
    assert not encoded.endswith("=")
    assert dev.b64url_decode(encoded) == raw
    assert base64.urlsafe_b64decode(encoded + "==") == raw


# ── the assembled connect params ──────────────────────────────────────────────

def _params(**overrides):
    """Assemble params with a fresh identity; crypto-gated callers only."""
    identity = overrides.pop("identity", None) or dev.generate_identity()
    base = dict(identity=identity, nonce="n1", signed_at_ms=1737264000000,
                token="tok")
    base.update(overrides)
    return identity, dev.build_connect_params(**base)


@requires_crypto
def test_the_device_block_is_never_empty_again():
    _, params = _params()
    device = params["device"]
    assert device["publicKey"] and device["signature"]          # minLength: 1
    assert set(device) == ALLOWED_DEVICE_KEYS
    assert device["signedAt"] == 1737264000000                  # the challenge ts
    assert device["nonce"] == "n1"


@requires_crypto
def test_params_stay_inside_the_schema():
    _, params = _params()
    assert set(params) <= ALLOWED_PARAM_KEYS
    assert set(params["client"]) <= ALLOWED_CLIENT_KEYS
    assert params["client"]["id"] in dev.GATEWAY_CLIENT_IDS
    assert params["client"]["mode"] in dev.GATEWAY_CLIENT_MODES
    assert params["minProtocol"] <= params["maxProtocol"]


@requires_crypto
def test_the_signed_scopes_are_the_scopes_that_are_sent():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    identity, params = _params(scopes=["operator.write", "operator.write"])
    assert params["scopes"] == ["operator.read", "operator.write"]

    payload = dev.build_device_auth_payload(
        "v3", device_id=identity.device_id, client_id=params["client"]["id"],
        client_mode=params["client"]["mode"], role=params["role"],
        scopes=params["scopes"], signed_at_ms=params["device"]["signedAt"],
        token="tok", nonce="n1", platform=params["client"]["platform"])
    public = ed25519.Ed25519PublicKey.from_public_bytes(
        dev.b64url_decode(identity.public_key))
    public.verify(dev.b64url_decode(params["device"]["signature"]),
                  payload.encode("utf-8"))


@requires_crypto
def test_an_invalid_client_identity_cannot_be_assembled_at_all():
    identity = dev.generate_identity()
    with pytest.raises(ValueError):
        dev.build_connect_params(identity=identity, nonce="n", signed_at_ms=1,
                                 client_id="vera-bridge", client_mode="operator")


def test_only_the_loopback_backend_may_omit_the_device_proof():
    params = dev.build_connect_params(
        identity=None, nonce="n", signed_at_ms=1, token="tok",
        client_id=dev.LOOPBACK_BACKEND_CLIENT_ID,
        client_mode=dev.LOOPBACK_BACKEND_CLIENT_MODE)
    assert "device" not in params

    with pytest.raises(ValueError):
        dev.build_connect_params(identity=None, nonce="n", signed_at_ms=1)


# ── reading the refusal ───────────────────────────────────────────────────────

def test_the_structured_code_wins_over_the_frame_code():
    assert dev.connect_error_code(
        {"code": "BAD_REQUEST", "details": {"code": "PAIRING_REQUIRED"}}
    ) == "PAIRING_REQUIRED"
    assert dev.connect_error_code({"code": "BAD_REQUEST"}) == "BAD_REQUEST"
    assert dev.connect_error_code(None) == ""


def test_a_pairing_refusal_names_the_device_to_approve():
    described = dev.describe_connect_error("PAIRING_REQUIRED", "not paired",
                                           device_id="abc123")
    assert "abc123" in described and "approve" in described.lower()


def test_an_unknown_code_still_reports_the_message():
    assert "boom" in dev.describe_connect_error("WAT", "boom")
    assert "handshake failed" in dev.describe_connect_error("", "handshake failed")
