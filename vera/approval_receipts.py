"""Trusted, scope-bound approval receipt primitives.

Receipt issuance belongs at a trusted Vera boundary.  Callers may carry a
receipt, but they cannot change its capability, effects, scope, expiry, or
nonce without invalidating its signature.  Verification is pure; consumption
is the single operation that records a nonce in a caller-owned replay ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


RECEIPT_SCHEMA = "vera.approval-receipt/v1"
MAX_TTL_SECONDS = 3600
MAX_TEXT_LENGTH = 256
_PAYLOAD_FIELDS = {
    "schema", "capability", "effects", "session_id", "tenant_id",
    "issued_at", "expires_at", "nonce",
}


class ApprovalReceiptError(ValueError):
    """Raised when a receipt cannot be safely issued."""


class ReplayStoreUnavailable(RuntimeError):
    """The atomic replay store could not prove a nonce was unused."""


class NonceReplayLedger:
    """Thread-safe in-process nonce claims for shadow evaluation and tests.

    Enforcement across workers requires a durable implementation with the same
    atomic ``claim`` contract (for example, Redis SET NX plus expiry).
    """

    def __init__(self) -> None:
        self._nonces: set[str] = set()
        self._lock = threading.Lock()

    def contains(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._nonces

    def claim(self, nonce: str) -> bool:
        with self._lock:
            if nonce in self._nonces:
                return False
            self._nonces.add(nonce)
            return True


class RedisNonceReplayLedger:
    """Cross-process replay claims using one atomic Redis ``SET NX EX``."""

    def __init__(self, redis: Any, *, prefix: str = "vera:approval:nonce:") -> None:
        if redis is None or not isinstance(prefix, str) or not prefix:
            raise ApprovalReceiptError("replay_ledger_invalid")
        self._redis = redis
        self._prefix = prefix

    async def claim(self, nonce: str, ttl_seconds: int) -> bool:
        if not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ApprovalReceiptError("replay_ttl_invalid")
        key = self._prefix + hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        try:
            claimed = await self._redis.set(key, "1", nx=True, ex=ttl_seconds)
        except Exception as exc:
            raise ReplayStoreUnavailable("replay_store_unavailable") from exc
        return bool(claimed)


_CONTEXT_SEAL = object()


@dataclass(frozen=True)
class TrustedPolicyContext:
    """Dispatcher-owned authority facts that cannot arrive through JSON args."""

    capability: str
    effects: tuple[str, ...]
    session_id: str
    tenant_id: str
    receipt_nonce_hash: str
    receipt_expires_at: int
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _CONTEXT_SEAL:
            raise ApprovalReceiptError("trusted_context_forgery")

    def projection(self) -> dict[str, Any]:
        return {
            "present": True,
            "capability": self.capability,
            "effects": list(self.effects),
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "receipt_nonce_hash": self.receipt_nonce_hash,
            "receipt_expires_at": self.receipt_expires_at,
        }


TRUSTED_POLICY_CONTEXT: ContextVar[TrustedPolicyContext | None] = ContextVar(
    "vera_trusted_policy_context", default=None)


def current_trusted_policy_context(capability: str,
                                   session_id: str, *,
                                   now: int | None = None) -> TrustedPolicyContext | None:
    """Return context only when it exactly belongs to this dispatch."""
    context = TRUSTED_POLICY_CONTEXT.get()
    checked_at = int(time.time()) if now is None else now
    if (context is None or context.capability != capability
            or context.session_id != session_id
            or not isinstance(checked_at, int) or isinstance(checked_at, bool)
            or checked_at >= context.receipt_expires_at):
        return None
    return context


@contextmanager
def activate_trusted_policy_context(context: TrustedPolicyContext):
    """Propagate dispatcher-owned context through one async call tree."""
    if not isinstance(context, TrustedPolicyContext) or context._seal is not _CONTEXT_SEAL:
        raise ApprovalReceiptError("trusted_context_invalid")
    token = TRUSTED_POLICY_CONTEXT.set(context)
    try:
        yield context
    finally:
        TRUSTED_POLICY_CONTEXT.reset(token)


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ApprovalReceiptError(f"{field}_invalid")
    result = value.strip()
    if (required and not result) or len(result) > MAX_TEXT_LENGTH:
        raise ApprovalReceiptError(f"{field}_invalid")
    return result


def _effects(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ApprovalReceiptError("effects_invalid")
    try:
        result = sorted({_text(value, "effect") for value in values})
    except TypeError as exc:
        raise ApprovalReceiptError("effects_invalid") from exc
    if not result or len(result) > 32:
        raise ApprovalReceiptError("effects_invalid")
    return result


def _key(secret: bytes | bytearray) -> bytes:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
        raise ApprovalReceiptError("signing_key_invalid")
    return bytes(secret)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def issue_approval_receipt(*, signing_key: bytes, capability: str,
                           effects: Iterable[str], session_id: str,
                           tenant_id: str = "", ttl_seconds: int = 300,
                           now: int | None = None,
                           nonce: str | None = None) -> dict[str, Any]:
    """Issue one opaque-signature receipt from trusted, already-approved facts."""
    if (not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool)
            or not 1 <= ttl_seconds <= MAX_TTL_SECONDS):
        raise ApprovalReceiptError("ttl_invalid")
    issued_at = int(time.time()) if now is None else now
    if not isinstance(issued_at, int) or isinstance(issued_at, bool) or issued_at < 0:
        raise ApprovalReceiptError("issued_at_invalid")
    payload = {
        "schema": RECEIPT_SCHEMA,
        "capability": _text(capability, "capability"),
        "effects": _effects(effects),
        "session_id": _text(session_id, "session_id"),
        "tenant_id": _text(tenant_id, "tenant_id", required=False),
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
        "nonce": _text(nonce or secrets.token_urlsafe(24), "nonce"),
    }
    signature = hmac.new(_key(signing_key), _canonical(payload),
                         hashlib.sha256).hexdigest()
    return {"payload": payload, "signature": signature}


def verify_approval_receipt(receipt: Any, *, signing_key: bytes,
                            capability: str, effects: Iterable[str],
                            session_id: str, tenant_id: str = "",
                            now: int | None = None,
                            replay_ledger: NonceReplayLedger | None = None) -> dict[str, Any]:
    """Verify exact scope without consuming the receipt or granting authority."""
    reasons: list[str] = []
    payload = receipt.get("payload") if isinstance(receipt, dict) else None
    signature = receipt.get("signature") if isinstance(receipt, dict) else None
    if (not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS
            or not isinstance(signature, str) or len(signature) != 64):
        return {"valid": False, "consumed": False, "reasons": ["malformed"]}
    try:
        expected_signature = hmac.new(_key(signing_key), _canonical(payload),
                                      hashlib.sha256).hexdigest()
        expected_capability = _text(capability, "capability")
        expected_effects = _effects(effects)
        expected_session = _text(session_id, "session_id")
        expected_tenant = _text(tenant_id, "tenant_id", required=False)
    except (ApprovalReceiptError, TypeError, ValueError):
        return {"valid": False, "consumed": False,
                "reasons": ["verification_context_invalid"]}
    if not hmac.compare_digest(signature, expected_signature):
        reasons.append("signature_invalid")
    if payload.get("schema") != RECEIPT_SCHEMA:
        reasons.append("schema_mismatch")
    if payload.get("capability") != expected_capability:
        reasons.append("capability_mismatch")
    if payload.get("effects") != expected_effects:
        reasons.append("effects_mismatch")
    if payload.get("session_id") != expected_session:
        reasons.append("session_mismatch")
    if payload.get("tenant_id") != expected_tenant:
        reasons.append("tenant_mismatch")
    checked_at = int(time.time()) if now is None else now
    issued_at, expires_at = payload.get("issued_at"), payload.get("expires_at")
    if (not isinstance(checked_at, int) or isinstance(checked_at, bool)
            or not isinstance(issued_at, int) or isinstance(issued_at, bool)
            or not isinstance(expires_at, int) or isinstance(expires_at, bool)
            or expires_at <= issued_at or expires_at - issued_at > MAX_TTL_SECONDS):
        reasons.append("time_invalid")
    elif checked_at < issued_at:
        reasons.append("not_yet_valid")
    elif checked_at >= expires_at:
        reasons.append("expired")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_TEXT_LENGTH:
        reasons.append("nonce_invalid")
    elif replay_ledger is not None and replay_ledger.contains(nonce):
        reasons.append("replayed")
    return {"valid": not reasons, "consumed": False, "reasons": reasons,
            "nonce": nonce if isinstance(nonce, str) else "",
            "expires_at": expires_at if isinstance(expires_at, int) else None}


def consume_approval_receipt(receipt: Any, *, signing_key: bytes,
                             capability: str, effects: Iterable[str],
                             session_id: str, tenant_id: str = "",
                             replay_ledger: NonceReplayLedger,
                             now: int | None = None) -> dict[str, Any]:
    """Verify and atomically claim a nonce in the supplied trusted ledger."""
    if not isinstance(replay_ledger, NonceReplayLedger):
        raise ApprovalReceiptError("replay_ledger_invalid")
    result = verify_approval_receipt(
        receipt, signing_key=signing_key, capability=capability,
        effects=effects, session_id=session_id, tenant_id=tenant_id, now=now,
        replay_ledger=replay_ledger)
    if not result["valid"]:
        return result
    nonce = result["nonce"]
    if not replay_ledger.claim(nonce):
        return {**result, "valid": False, "reasons": ["replayed"]}
    return {**result, "consumed": True}


async def consume_approval_receipt_durable(
        receipt: Any, *, signing_key: bytes, capability: str,
        effects: Iterable[str], session_id: str, tenant_id: str = "",
        replay_ledger: RedisNonceReplayLedger,
        now: int | None = None) -> dict[str, Any]:
    """Verify then atomically consume across workers; fail closed on outage."""
    if not isinstance(replay_ledger, RedisNonceReplayLedger):
        raise ApprovalReceiptError("replay_ledger_invalid")
    result = verify_approval_receipt(
        receipt, signing_key=signing_key, capability=capability,
        effects=effects, session_id=session_id, tenant_id=tenant_id, now=now)
    if not result["valid"]:
        return result
    checked_at = int(time.time()) if now is None else now
    ttl = result["expires_at"] - checked_at
    try:
        claimed = await replay_ledger.claim(result["nonce"], ttl)
    except ReplayStoreUnavailable:
        return {**result, "valid": False, "consumed": False,
                "reasons": ["replay_store_unavailable"]}
    if not claimed:
        return {**result, "valid": False, "consumed": False,
                "reasons": ["replayed"]}
    context = TrustedPolicyContext(
        capability=_text(capability, "capability"),
        effects=tuple(_effects(effects)),
        session_id=_text(session_id, "session_id"),
        tenant_id=_text(tenant_id, "tenant_id", required=False),
        receipt_nonce_hash=hashlib.sha256(result["nonce"].encode("utf-8")).hexdigest(),
        receipt_expires_at=result["expires_at"],
        _seal=_CONTEXT_SEAL,
    )
    return {**result, "consumed": True, "context": context}
