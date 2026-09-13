"""Narrow HTTPS client for a sandbox to join the controller's inference gate.

The client can only acquire, renew, and release opaque leases. It receives no
Redis address, key, command surface, or Loop Lab mutation authority.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx


class BrokerError(RuntimeError):
    pass


class BrokerClient:
    def __init__(self, url: str, sandbox: str, token: str, timeout: float = 125):
        self.url = str(url or "").strip()
        self.sandbox = str(sandbox or "").strip()
        self.token = str(token or "").strip()
        self.timeout = max(1.0, min(float(timeout), 130.0))
        if not (self.url.startswith("https://") and self.sandbox and self.token):
            raise BrokerError("invalid_broker_configuration")

    async def _call(self, name: str, arguments: Dict[str, Any]) -> dict:
        body = {
            "name": name,
            "arguments": {"sandbox": self.sandbox, **arguments},
            "caller_kind": "sandbox_gate",
            "session_id": "",
        }
        try:
            async with httpx.AsyncClient(verify=False, timeout=self.timeout) as client:
                response = await client.post(
                    self.url, json=body,
                    headers={"X-Vera-Sandbox-Gate": self.token})
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise BrokerError("broker_unreachable") from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, dict) or not content.get("ok"):
            reason = content.get("error") if isinstance(content, dict) else "invalid_response"
            raise BrokerError(str(reason or "broker_refused"))
        return content

    async def acquire(self, node: str, wait: float) -> dict:
        result = await self._call("ollama.gate.lease.acquire", {
            "node": node, "wait_s": max(0.0, min(float(wait), 120.0))})
        lease_id = str(result.get("lease_id") or "")
        if not lease_id:
            raise BrokerError("missing_lease")
        return {"broker_lease_id": lease_id, "node": node,
                "waited_s": float(result.get("waited_s") or 0)}

    async def status(self) -> dict:
        return await self._call("ollama.gate.lease.status", {})

    async def renew(self, lease: dict, ttl: int) -> bool:
        result = await self._call("ollama.gate.lease.renew", {
            "lease_id": str((lease or {}).get("broker_lease_id") or "")})
        return bool(result.get("renewed"))

    async def release(self, lease: Optional[dict]) -> bool:
        result = await self._call("ollama.gate.lease.release", {
            "lease_id": str((lease or {}).get("broker_lease_id") or "")})
        return bool(result.get("released"))


def from_environment() -> Optional[BrokerClient]:
    url = os.getenv("VERA_GATE_BROKER_URL", "").strip()
    if not url:
        return None
    return BrokerClient(url, os.getenv("VERA_GATE_BROKER_SANDBOX", ""),
                        os.getenv("VERA_GATE_BROKER_TOKEN", ""))
