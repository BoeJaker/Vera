"""
providers_capabilities.py — External LLM API providers (Claude / OpenAI)
========================================================================
Adds an "API" page to the Workers & Ollama panel for connecting *external*
hosted LLM providers alongside the local Ollama / vLLM cluster:

  • Store provider API keys (Anthropic, OpenAI, or any OpenAI-compatible base)
    sealed with Fernet in Redis — same vault Vera uses for Proxmox / accounts /
    email creds (`Vera.vera.security.secrets`). Falls back to the provider's
    standard env var (ANTHROPIC_API_KEY / OPENAI_API_KEY) when no key is stored.
  • Test connectivity + list available models via each provider's /models API.
  • A thin chat proxy (`providers.chat`) so chat / agents can route to these
    providers, normalising Anthropic Messages and OpenAI Chat Completions to a
    common {text, input_tokens, output_tokens} shape.
  • Per-request usage + cost tracking (ring buffer + Redis-persisted totals),
    priced from a built-in, UI-overridable per-model price table.

Register in capability_orchestration.py `_module_files`:
    os.path.join(_here, "providers/providers_capabilities.py"),

Routes (auto-created from @capability http_path) + the panel HTML route
(`/providers/panel`) are served below; the Workers & Ollama panel iframes it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, now_iso
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.providers")

# ─────────────────────────────────────────────────────────────────────────────
#  Storage keys + state
# ─────────────────────────────────────────────────────────────────────────────
KEY_PROVIDERS = "vera:providers"            # hash id -> json record
KEY_PRICING   = "vera:providers:pricing"    # hash model -> json {in,out}
KEY_TOTALS    = "vera:providers:totals"     # hash "provider/model" -> json totals

_SECRET_FIELDS = ("api_key",)
_PANEL_PATH = Path(__file__).parent / "providers_panel.html"

# In-process ring buffer of recent calls (newest appended last).
_USAGE_LOG: deque = deque(maxlen=500)

# Env var fallback per built-in provider kind.
_ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

# Built-in provider templates. Stored Redis records override/extend these; a
# custom OpenAI-compatible provider can be added with any id + kind="openai".
_BUILTINS: Dict[str, dict] = {
    "anthropic": {
        "id": "anthropic", "label": "Anthropic (Claude)", "kind": "anthropic",
        "base_url": "https://api.anthropic.com", "enabled": True,
        "default_model": "claude-opus-4-8",
    },
    "openai": {
        "id": "openai", "label": "OpenAI", "kind": "openai",
        "base_url": "https://api.openai.com/v1", "enabled": True,
        "default_model": "gpt-4o",
    },
}

# Built-in price table — USD per 1M tokens {in, out}. Anthropic figures track the
# claude-api reference; OpenAI figures are approximate published rates and are
# fully overridable from the panel (providers.pricing.set).
_PRICING_DEFAULTS: Dict[str, Dict[str, float]] = {
    # Anthropic
    "claude-fable-5":   {"in": 10.0, "out": 50.0},
    "claude-opus-4-8":  {"in": 5.0,  "out": 25.0},
    "claude-opus-4-7":  {"in": 5.0,  "out": 25.0},
    "claude-opus-4-6":  {"in": 5.0,  "out": 25.0},
    "claude-sonnet-4-6":{"in": 3.0,  "out": 15.0},
    "claude-haiku-4-5": {"in": 1.0,  "out": 5.0},
    # OpenAI (approximate — editable in the panel)
    "gpt-4o":           {"in": 2.5,  "out": 10.0},
    "gpt-4o-mini":      {"in": 0.15, "out": 0.6},
    "gpt-4-turbo":      {"in": 10.0, "out": 30.0},
    "o1":               {"in": 15.0, "out": 60.0},
    "o1-mini":          {"in": 1.1,  "out": 4.4},
    "gpt-3.5-turbo":    {"in": 0.5,  "out": 1.5},
}

# Known model lists used when a live /models call isn't available (no key, or
# offline). The live list, when reachable, takes precedence.
_KNOWN_MODELS = {
    "anthropic": [
        "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5",
        "claude-opus-4-7", "claude-fable-5",
    ],
    "openai": [
        "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o1-mini", "gpt-3.5-turbo",
    ],
}


def _redis():
    return getattr(_orch, "REDIS", None)


# ─────────────────────────────────────────────────────────────────────────────
#  Provider records (seal on write, redact on read, env fallback on use)
# ─────────────────────────────────────────────────────────────────────────────
async def _stored() -> Dict[str, dict]:
    r = _redis()
    out: Dict[str, dict] = {}
    if not r:
        return out
    try:
        raw = await r.hgetall(KEY_PROVIDERS)
        for k, v in (raw or {}).items():
            pid = k.decode() if isinstance(k, bytes) else k
            try:
                out[pid] = json.loads(v.decode() if isinstance(v, bytes) else v)
            except Exception:
                continue
    except Exception as e:
        log.debug("providers _stored: %s", e)
    return out


async def _merged() -> Dict[str, dict]:
    """Built-in templates overlaid with stored overrides + custom providers."""
    merged = {pid: dict(rec) for pid, rec in _BUILTINS.items()}
    for pid, rec in (await _stored()).items():
        base = dict(merged.get(pid, {}))
        base.update(rec)
        merged[pid] = base
    return merged


async def _get(pid: str) -> Optional[dict]:
    return (await _merged()).get(pid)


def _open_key(rec: dict) -> str:
    """Stored sealed key (decrypted) or the provider's env-var fallback."""
    stored = vsecrets.open_secret(rec.get("api_key", "")) if rec.get("api_key") else ""
    if stored:
        return stored
    env = _ENV_KEYS.get(rec.get("kind", ""))
    return os.getenv(env, "").strip() if env else ""


def _redact(rec: dict) -> dict:
    """UI-safe copy: secret stripped, replaced with has_key / env_key flags."""
    out = {}
    for k, v in rec.items():
        if k in _SECRET_FIELDS:
            continue
        out[k] = v
    out["has_key"] = bool(rec.get("api_key"))
    env = _ENV_KEYS.get(rec.get("kind", ""))
    out["env_key"] = bool(env and os.getenv(env, "").strip())
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Pricing + usage
# ─────────────────────────────────────────────────────────────────────────────
async def _pricing() -> Dict[str, Dict[str, float]]:
    table = {m: dict(p) for m, p in _PRICING_DEFAULTS.items()}
    r = _redis()
    if r:
        try:
            raw = await r.hgetall(KEY_PRICING)
            for k, v in (raw or {}).items():
                model = k.decode() if isinstance(k, bytes) else k
                try:
                    table[model] = json.loads(v.decode() if isinstance(v, bytes) else v)
                except Exception:
                    continue
        except Exception as e:
            log.debug("providers _pricing: %s", e)
    return table


async def _price_of(model: str) -> Dict[str, float]:
    return (await _pricing()).get(model, {"in": 0.0, "out": 0.0})


def _cost(price: Dict[str, float], in_tok: int, out_tok: int) -> float:
    return round(in_tok / 1e6 * price.get("in", 0.0)
                 + out_tok / 1e6 * price.get("out", 0.0), 6)


async def _record_usage(provider: str, model: str, in_tok: int, out_tok: int,
                        cost: float, caller: str = "", ok: bool = True,
                        error: str = ""):
    entry = {
        "ts": now_iso(), "provider": provider, "model": model,
        "input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": cost,
        "caller": caller, "ok": ok, "error": error,
    }
    _USAGE_LOG.append(entry)
    r = _redis()
    if r:
        try:
            field = f"{provider}/{model}"
            raw = await r.hget(KEY_TOTALS, field)
            tot = json.loads(raw) if raw else {"requests": 0, "input_tokens": 0,
                                               "output_tokens": 0, "cost_usd": 0.0}
            tot["requests"] += 1
            tot["input_tokens"] += in_tok
            tot["output_tokens"] += out_tok
            tot["cost_usd"] = round(tot["cost_usd"] + cost, 6)
            await r.hset(KEY_TOTALS, field, json.dumps(tot))
        except Exception as e:
            log.debug("providers _record_usage: %s", e)
    try:
        await emit_event({"type": "provider.chat", "provider": provider,
                          "model": model, "cost_usd": cost,
                          "input_tokens": in_tok, "output_tokens": out_tok,
                          "ok": ok})
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Provider HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────
def _headers(rec: dict, key: str) -> dict:
    if rec.get("kind") == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"}
    return {"Authorization": f"Bearer {key}", "content-type": "application/json"}


def _models_url(rec: dict) -> str:
    base = (rec.get("base_url") or "").rstrip("/")
    return f"{base}/v1/models" if rec.get("kind") == "anthropic" else f"{base}/models"


def _chat_url(rec: dict) -> str:
    base = (rec.get("base_url") or "").rstrip("/")
    return f"{base}/v1/messages" if rec.get("kind") == "anthropic" else f"{base}/chat/completions"


async def _live_models(rec: dict, key: str) -> List[str]:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(_models_url(rec), headers=_headers(rec, key))
            r.raise_for_status()
            data = r.json().get("data", [])
            return [m.get("id") for m in data if m.get("id")]
    except Exception as e:
        log.debug("providers _live_models(%s): %s", rec.get("id"), e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Capabilities
# ─────────────────────────────────────────────────────────────────────────────
@capability("providers.list", memory="off", silent=True,
            http_method="GET", http_path="/providers/list", http_tags=["providers"],
            description="List configured external LLM providers (Anthropic, "
                        "OpenAI, custom). Secrets are redacted to has_key/env_key "
                        "flags. Output: {providers:[...]}.")
async def cap_providers_list(trace_id=None) -> Dict:
    merged = await _merged()
    return {"providers": [_redact(rec) for rec in merged.values()]}


@capability("providers.save", memory="off",
            http_method="POST", http_path="/providers/save", http_tags=["providers"],
            description="Create/update a provider. Inputs: id (str!), label, kind "
                        "('anthropic'|'openai'), base_url, api_key (sealed; blank "
                        "keeps the existing key), enabled (bool), default_model. "
                        "Output: {ok, provider}.")
async def cap_providers_save(id: str = "", label: str = "", kind: str = "openai",
                             base_url: str = "", api_key: str = "",
                             enabled: bool = True, default_model: str = "",
                             trace_id=None) -> Dict:
    if not id:
        return {"error": "id required"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    existing = (await _stored()).get(id, {})
    rec = dict(_BUILTINS.get(id, {}))
    rec.update(existing)
    rec["id"] = id
    if label:        rec["label"] = label
    if kind:         rec["kind"] = kind
    if base_url:     rec["base_url"] = base_url
    rec["enabled"] = bool(enabled)
    if default_model: rec["default_model"] = default_model
    if api_key:
        # Blank input keeps the stored key (don't clobber on a metadata-only save).
        try:
            rec["api_key"] = vsecrets.seal(api_key)
        except Exception as e:
            return {"error": f"could not seal key: {e}"}
    try:
        await r.hset(KEY_PROVIDERS, id, json.dumps(rec))
    except Exception as e:
        return {"error": str(e)}
    await emit_event({"type": "provider.saved", "provider": id})
    return {"ok": True, "provider": _redact({**_BUILTINS.get(id, {}), **rec})}


@capability("providers.delete", memory="off",
            http_method="POST", http_path="/providers/delete", http_tags=["providers"],
            description="Delete a stored provider record. Built-ins revert to "
                        "their template defaults. Input: id (str!). Output: {ok}.")
async def cap_providers_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    try:
        await r.hdel(KEY_PROVIDERS, id)
    except Exception as e:
        return {"error": str(e)}
    await emit_event({"type": "provider.deleted", "provider": id})
    return {"ok": True}


@capability("providers.test", memory="off",
            http_method="POST", http_path="/providers/test", http_tags=["providers"],
            description="Test a provider's API key by listing its models. Input: "
                        "id (str!). Output: {ok, latency_ms, models:[...], error}.")
async def cap_providers_test(id: str = "", trace_id=None) -> Dict:
    rec = await _get(id)
    if not rec:
        return {"ok": False, "error": f"unknown provider: {id}"}
    key = _open_key(rec)
    if not key:
        return {"ok": False, "error": "no API key (set one or export the env var)"}
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(_models_url(rec), headers=_headers(rec, key))
        latency = round((time.time() - t0) * 1000, 1)
        if r.status_code >= 400:
            return {"ok": False, "latency_ms": latency,
                    "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        return {"ok": True, "latency_ms": latency, "models": models}
    except Exception as e:
        return {"ok": False, "latency_ms": round((time.time() - t0) * 1000, 1),
                "error": str(e)}


@capability("providers.models", memory="off", silent=True,
            http_method="GET", http_path="/providers/models", http_tags=["providers"],
            description="List models for a provider (live /models when a key is "
                        "available, else the known fallback list). Input: provider "
                        "(str!). Output: {provider, models:[...], live (bool)}.")
async def cap_providers_models(provider: str = "", trace_id=None) -> Dict:
    rec = await _get(provider)
    if not rec:
        return {"error": f"unknown provider: {provider}"}
    key = _open_key(rec)
    live = await _live_models(rec, key) if key else []
    if live:
        return {"provider": provider, "models": live, "live": True}
    return {"provider": provider,
            "models": _KNOWN_MODELS.get(rec.get("kind"), []), "live": False}


@capability("providers.chat", memory="off",
            http_method="POST", http_path="/providers/chat", http_tags=["providers"],
            description="Send a chat completion to an external provider and record "
                        "usage + cost. Inputs: provider (str!), model (blank=default), "
                        "prompt OR messages:[{role,content}], system, max_tokens, "
                        "caller. Output: {text, model, input_tokens, output_tokens, "
                        "cost_usd, error}.")
async def cap_providers_chat(provider: str = "", model: str = "", prompt: str = "",
                             messages: Optional[List[dict]] = None, system: str = "",
                             max_tokens: int = 1024, caller: str = "",
                             trace_id=None) -> Dict:
    rec = await _get(provider)
    if not rec:
        return {"error": f"unknown provider: {provider}"}
    if not rec.get("enabled", True):
        return {"error": f"provider '{provider}' is disabled"}
    key = _open_key(rec)
    if not key:
        return {"error": "no API key for this provider"}
    model = model or rec.get("default_model", "")
    if not model:
        return {"error": "no model specified"}
    msgs = messages or ([{"role": "user", "content": prompt}] if prompt else [])
    if not msgs:
        return {"error": "prompt or messages required"}

    try:
        if rec.get("kind") == "anthropic":
            body = {"model": model, "max_tokens": int(max_tokens), "messages": msgs}
            if system:
                body["system"] = system
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(_chat_url(rec), headers=_headers(rec, key), json=body)
            r.raise_for_status()
            d = r.json()
            text = "".join(b.get("text", "") for b in d.get("content", [])
                           if b.get("type") == "text")
            usage = d.get("usage", {})
            in_tok = int(usage.get("input_tokens", 0))
            out_tok = int(usage.get("output_tokens", 0))
        else:
            full_msgs = ([{"role": "system", "content": system}] if system else []) + msgs
            body = {"model": model, "max_tokens": int(max_tokens), "messages": full_msgs}
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(_chat_url(rec), headers=_headers(rec, key), json=body)
            r.raise_for_status()
            d = r.json()
            choices = d.get("choices", [])
            text = choices[0].get("message", {}).get("content", "") if choices else ""
            usage = d.get("usage", {})
            in_tok = int(usage.get("prompt_tokens", 0))
            out_tok = int(usage.get("completion_tokens", 0))
    except httpx.HTTPStatusError as e:
        err = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        await _record_usage(provider, model, 0, 0, 0.0, caller, ok=False, error=err)
        return {"error": err, "model": model}
    except Exception as e:
        await _record_usage(provider, model, 0, 0, 0.0, caller, ok=False, error=str(e))
        return {"error": str(e), "model": model}

    cost = _cost(await _price_of(model), in_tok, out_tok)
    await _record_usage(provider, model, in_tok, out_tok, cost, caller, ok=True)
    return {"text": text, "model": model, "input_tokens": in_tok,
            "output_tokens": out_tok, "cost_usd": cost}


@capability("providers.usage", memory="off", silent=True,
            http_method="GET", http_path="/providers/usage", http_tags=["providers"],
            description="Recent provider calls + cumulative totals for the usage/"
                        "cost dashboard. Query: limit (int, default 100). Output: "
                        "{recent:[...], totals:[...], summary:{...}}.")
async def cap_providers_usage(limit: int = 100, trace_id=None) -> Dict:
    recent = list(_USAGE_LOG)[-min(limit, _USAGE_LOG.maxlen):][::-1]
    totals: List[dict] = []
    r = _redis()
    if r:
        try:
            raw = await r.hgetall(KEY_TOTALS)
            for k, v in (raw or {}).items():
                field = k.decode() if isinstance(k, bytes) else k
                try:
                    t = json.loads(v.decode() if isinstance(v, bytes) else v)
                except Exception:
                    continue
                prov, _, mdl = field.partition("/")
                totals.append({"provider": prov, "model": mdl, **t})
        except Exception as e:
            log.debug("providers usage totals: %s", e)
    summary = {
        "total_cost_usd": round(sum(t.get("cost_usd", 0.0) for t in totals), 4),
        "total_requests": sum(t.get("requests", 0) for t in totals),
        "total_input_tokens": sum(t.get("input_tokens", 0) for t in totals),
        "total_output_tokens": sum(t.get("output_tokens", 0) for t in totals),
    }
    return {"recent": recent, "totals": totals, "summary": summary}


@capability("providers.usage.clear", memory="off",
            http_method="POST", http_path="/providers/usage/clear", http_tags=["providers"],
            description="Clear the in-memory usage log and persisted totals. "
                        "Output: {ok}.")
async def cap_providers_usage_clear(trace_id=None) -> Dict:
    _USAGE_LOG.clear()
    r = _redis()
    if r:
        try:
            await r.delete(KEY_TOTALS)
        except Exception:
            pass
    return {"ok": True}


@capability("providers.pricing", memory="off", silent=True,
            http_method="GET", http_path="/providers/pricing", http_tags=["providers"],
            description="Return the per-model price table (USD per 1M tokens). "
                        "Output: {pricing:{model:{in,out}}}.")
async def cap_providers_pricing(trace_id=None) -> Dict:
    return {"pricing": await _pricing()}


@capability("providers.pricing.set", memory="off",
            http_method="POST", http_path="/providers/pricing/set", http_tags=["providers"],
            description="Override the price for a model. Inputs: model (str!), "
                        "in_per_m (float), out_per_m (float). Output: {ok}.")
async def cap_providers_pricing_set(model: str = "", in_per_m: float = 0.0,
                                    out_per_m: float = 0.0, trace_id=None) -> Dict:
    r = _redis()
    if not r or not model:
        return {"error": "model required"}
    try:
        await r.hset(KEY_PRICING, model,
                     json.dumps({"in": float(in_per_m), "out": float(out_per_m)}))
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
#  Panel route (standalone page; iframed by the Workers & Ollama "API" pane)
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/providers/panel", include_in_schema=False)
async def _providers_panel_html():
    from fastapi.responses import HTMLResponse as _HTMLResp
    if _PANEL_PATH.exists():
        return _HTMLResp(_PANEL_PATH.read_text(encoding="utf-8"))
    return _HTMLResp("<p style='color:#c96b6b'>providers_panel.html not found</p>",
                     status_code=404)


log.info("providers: module loaded (%d built-in providers)", len(_BUILTINS))
