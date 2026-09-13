"""Deterministic, non-authoritative projection of Vera connection registries."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


SCHEMA = "vera.connection-projection/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _source_id(system: str, raw_id: Any) -> tuple[str, str]:
    raw = str(raw_id or "").strip()
    if not raw:
        raise ValueError(f"{system} record id is required")
    safe = raw if _SAFE_ID.fullmatch(raw) else "sha256-" + hashlib.sha256(
        raw.encode("utf-8")).hexdigest()[:24]
    return f"connection:{system}:{safe}", safe


def _endpoint_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return ""
    default = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    authority = host + (f":{port}" if port and not default else "")
    # Paths, queries, fragments and userinfo can themselves carry credentials
    # (notably private ICS URLs). Connection identity needs only the authority.
    return f"{parsed.scheme}://{authority}"


def _endpoint_host(protocol: str, host: Any, port: Any) -> str:
    if protocol not in {"http", "https", "imap", "smtp"}:
        return ""
    hostname = str(host or "").strip().lower()
    if not hostname or not _HOST.fullmatch(hostname):
        return ""
    try:
        number = int(port or 0)
    except (TypeError, ValueError):
        return ""
    if not 1 <= number <= 65535:
        return ""
    return f"{protocol}://{hostname}:{number}"


def _connection(system: str, record: Mapping[str, Any]) -> tuple[dict, list[dict]]:
    cid, raw_id = _source_id(system, record.get("id"))
    gaps: list[dict] = []
    endpoints: set[str] = set()
    modes: list[str] = []
    configured = False
    storage = "none"
    kind = str(record.get("kind") or system).strip()[:80]
    enabled = True

    if system == "integration":
        endpoint = _endpoint_url(record.get("base_url"))
        if not endpoint and record.get("host"):
            endpoint = _endpoint_host(str(record.get("scheme") or "http"),
                                      record.get("host"), record.get("port"))
        if endpoint:
            endpoints.add(endpoint)
        elif record.get("base_url") or record.get("host"):
            gaps.append({"connection_id": cid, "code": "invalid_endpoint"})
        access = record.get("access") if isinstance(record.get("access"), Mapping) else {}
        modes = sorted(str(mode) for mode, allowed in access.items() if allowed is True)
        configured = bool((record.get("api") or {}).get("has_auth"))
        storage = "sealed_internal" if configured else "none"
        enabled = bool(modes)
    elif system == "account":
        for protocol, host_key, port_key in (
                ("imap", "imap_host", "imap_port"),
                ("smtp", "smtp_host", "smtp_port")):
            if record.get(host_key):
                endpoint = _endpoint_host(protocol, record.get(host_key), record.get(port_key))
                if endpoint:
                    endpoints.add(endpoint)
                else:
                    gaps.append({"connection_id": cid, "code": "invalid_endpoint",
                                 "protocol": protocol})
        for key in ("caldav_url", "ics_url"):
            if record.get(key):
                endpoint = _endpoint_url(record.get(key))
                if endpoint:
                    endpoints.add(endpoint)
                else:
                    gaps.append({"connection_id": cid, "code": "invalid_endpoint",
                                 "protocol": key.removesuffix("_url")})
        modes = sorted(mode for mode, present in {
            "mail": bool(record.get("mail_enabled")),
            "calendar": bool(record.get("caldav_url") or record.get("ics_url")),
            "oauth": bool(record.get("oauth_provider")),
        }.items() if present)
        configured = any(bool(record.get("has_" + field)) for field in
                         ("app_password", "caldav_password", "oauth_client_secret",
                          "oauth_refresh_token"))
        storage = "sealed_internal" if configured else "none"
        kind = str(record.get("oauth_provider") or "account")[:80]
        enabled = bool(modes)
    elif system == "provider":
        endpoint = _endpoint_url(record.get("base_url"))
        if endpoint:
            endpoints.add(endpoint)
        elif record.get("base_url"):
            gaps.append({"connection_id": cid, "code": "invalid_endpoint"})
        modes = ["generation"]
        stored = bool(record.get("has_key"))
        environment = bool(record.get("env_key"))
        configured = stored or environment
        storage = "sealed_internal" if stored else "environment" if environment else "none"
        enabled = record.get("enabled") is not False

    return ({
        "id": cid,
        "source": {"system": system, "record_id": raw_id},
        "category": {"integration": "service", "account": "account",
                     "provider": "model_provider"}[system],
        "kind": kind,
        "label": str(record.get("label") or raw_id)[:240],
        "enabled": enabled,
        "endpoints": sorted(endpoints),
        "modes": modes,
        "credential": {"configured": configured, "storage": storage,
                       "material_exposed": False},
        "authoritative": False,
    }, gaps)


def project_connections(*, integrations: Iterable[Mapping[str, Any]] = (),
                        accounts: Iterable[Mapping[str, Any]] = (),
                        providers: Iterable[Mapping[str, Any]] = (),
                        available_sources: Iterable[str] = (
                            "integration", "account", "provider")) -> dict:
    """Project three registries without merging records or granting authority."""
    source_rows = {"integration": list(integrations), "account": list(accounts),
                   "provider": list(providers)}
    available = set(available_sources)
    if available - set(source_rows):
        raise ValueError("available_sources contains an unknown source")
    if any(len(rows) > 500 for rows in source_rows.values()):
        raise ValueError("each connection source is limited to 500 records")
    connections, gaps = [], []
    raw_index: dict[str, list[str]] = defaultdict(list)
    explicit_refs: list[tuple[str, str]] = []
    for system, rows in source_rows.items():
        for record in rows:
            if not isinstance(record, Mapping):
                raise TypeError(f"{system} records must be objects")
            projected, record_gaps = _connection(system, record)
            connections.append(projected)
            gaps.extend(record_gaps)
            raw_index[str(record.get("id") or "").strip()].append(projected["id"])
            if system == "integration" and record.get("conn_id"):
                explicit_refs.append((projected["id"], str(record["conn_id"])))
    ids = [item["id"] for item in connections]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate connection identity")
    connections.sort(key=lambda item: item["id"])

    links = []
    for source, raw_target in sorted(explicit_refs):
        targets = raw_index.get(raw_target, [])
        state = "resolved" if len(targets) == 1 else "ambiguous" if targets else "unresolved"
        links.append({"source": source, "relation": "references",
                      "target": targets[0] if len(targets) == 1 else "", "state": state,
                      "target_ref_sha256": hashlib.sha256(
                          raw_target.encode("utf-8")).hexdigest()})
        if state != "resolved":
            gaps.append({"connection_id": source, "code": f"explicit_link_{state}"})

    endpoint_index: dict[str, list[str]] = defaultdict(list)
    for item in connections:
        for endpoint in item["endpoints"]:
            endpoint_index[endpoint].append(item["id"])
    collisions = [
        {"endpoint": endpoint, "connection_ids": sorted(connection_ids),
         "merged": False}
        for endpoint, connection_ids in sorted(endpoint_index.items())
        if len(connection_ids) > 1
    ]
    sources = {system: {"available": system in available,
                        "records": len(source_rows[system])}
               for system in sorted(source_rows)}
    body = {"connections": connections, "links": links,
            "collisions": collisions,
            "gaps": sorted(gaps, key=lambda item: _canonical(item)),
            "sources": sources, "complete": all(
                item["available"] for item in sources.values())}
    return {"schema": SCHEMA,
            "projection_id": hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest(),
            **body, "authorizes": False, "activates": False,
            "merges_records": False, "resolves_secrets": False}
