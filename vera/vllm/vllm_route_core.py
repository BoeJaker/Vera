"""
vllm_route_core.py - how a routing-table rule reaches a vLLM server, with the
same semantics an Ollama node gets from the picker.

Pure: plain dicts in, decisions out (no I/O, no app imports).

A rule (or profile rule) pins vLLM with `pin: "vllm:<id>"` or `pin: "vllm:*"`.
Every routed path - generate, chat, embeddings - resolves the target here, so
they cannot drift apart:

  * pin to one id  -> that instance if it is up, else no target (the caller
                      falls back to Ollama routing, as an offline Ollama pin does)
  * pin to *       -> candidates are the instances that are up; the rule's
                      deny_gpu drops GPU servers, prefer_gpu narrows to GPU ones
                      when any is up; an instance that serves the model wins;
                      then lowest load, then priority, then id.
  * served model   -> the requested name when the server answers to it (an alias
                      such as the Ollama tag it stands in for), else the server's
                      own model - reported back, never silently.
  * gate           -> `gate_as` when the server shares hardware with an Ollama
                      node (vLLM on gpu-250's V100 gates as gpu-250), else its own id.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

UP = ("online", "degraded")


def pin_target(rule: Optional[Dict[str, Any]]) -> Optional[str]:
    """'*' or an instance id when the rule pins vLLM; None otherwise."""
    pin = str((rule or {}).get("pin") or "")
    if not pin.startswith("vllm:"):
        return None
    tail = pin.split(":", 1)[1].strip()
    return tail or "*"


def _f(inst: Any, key: str, default: Any = None) -> Any:
    if isinstance(inst, dict):
        return inst.get(key, default)
    return getattr(inst, key, default)


def serves(inst: Any, model: Optional[str]) -> bool:
    if not model:
        return False
    names = list(_f(inst, "models", []) or []) + list(_f(inst, "aliases", []) or [])
    return model in names


def choose(instances: Dict[str, Any], pin: str, *, model: Optional[str] = None,
           prefer_gpu: bool = False, deny_gpu: bool = False) -> Optional[str]:
    """The instance id a vLLM pin routes to, or None (caller falls back)."""
    if not pin:
        return None
    if pin != "*":
        inst = instances.get(pin)
        return pin if inst is not None and _f(inst, "status") in UP else None
    cands = {i: v for i, v in instances.items() if _f(v, "status") in UP}
    if deny_gpu:
        cands = {i: v for i, v in cands.items() if not _f(v, "has_gpu", False)}
    if not cands:
        return None
    if prefer_gpu and not deny_gpu:
        gpu = {i: v for i, v in cands.items() if _f(v, "has_gpu", False)}
        if gpu:
            cands = gpu
    with_model = {i: v for i, v in cands.items() if serves(v, model)}
    if with_model:
        cands = with_model

    def key(item):
        iid, v = item
        load = int(_f(v, "in_use", 0) or 0) * 2 + int(_f(v, "waiting_requests", 0) or 0)
        degraded = 0 if _f(v, "status") == "online" else 1
        return (degraded, load, int(_f(v, "priority", 0) or 0), iid)

    return min(cands.items(), key=key)[0]


def served_model(inst: Any, wanted: Optional[str]) -> Optional[str]:
    """The model name to send: the wanted one if the server answers to it, else
    the server's first model (None when the server reports none yet)."""
    if serves(inst, wanted):
        return wanted
    models = list(_f(inst, "models", []) or [])
    return models[0] if models else (wanted or None)


def gate_id(inst: Any, own_id: str) -> str:
    """Which node's semaphore + cross-process gate this server takes."""
    return str(_f(inst, "gate_as", "") or own_id)


def ollama_ids_shadowed(instances: Dict[str, Any]) -> Iterable[str]:
    """Ollama node ids that some vLLM server gates as (shares hardware with)."""
    return sorted({str(_f(v, "gate_as", "")) for v in instances.values()
                   if _f(v, "gate_as", "")})


# ── chat: Ollama-shaped request in, Ollama-shaped stream out ──────────────────
# Vera's chat loop (agents.run / run_stream) builds an Ollama /api/chat body and
# reads Ollama NDJSON lines - stall detection, thinking split, TTS and the done
# accounting all hang off that shape. A vLLM-pinned chat translates at the edge
# so all of that runs unchanged.

def chat_body(ollama_body: Dict[str, Any], model: Optional[str]) -> Dict[str, Any]:
    """An OpenAI /v1/chat/completions body from an Ollama /api/chat body."""
    opts = dict(ollama_body.get("options") or {})
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in (ollama_body.get("messages") or [])]
    out: Dict[str, Any] = {"model": model or ollama_body.get("model") or "",
                           "messages": msgs,
                           "stream": bool(ollama_body.get("stream", False))}
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    n = int(opts.get("num_predict") or 0)
    if n > 0:
        out["max_tokens"] = n
    for k in ("temperature", "top_p", "top_k", "seed", "stop"):
        if opts.get(k) is not None:
            out[k] = opts[k]
    if "think" in ollama_body:
        out["chat_template_kwargs"] = {"enable_thinking": bool(ollama_body["think"])}
    if ollama_body.get("format") == "json":
        out["response_format"] = {"type": "json_object"}
    return out


def _reasoning(d: Dict[str, Any]) -> str:
    return str(d.get("reasoning_content") or d.get("reasoning") or "")


def chat_reply(openai_json: Dict[str, Any]) -> Dict[str, Any]:
    """Ollama /api/chat (non-stream) reply shape from an OpenAI completion."""
    ch = ((openai_json or {}).get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    usage = (openai_json or {}).get("usage") or {}
    return {"message": {"role": "assistant", "content": msg.get("content") or "",
                        "thinking": _reasoning(msg)},
            "done": True, "done_reason": ch.get("finish_reason") or "stop",
            "eval_count": int(usage.get("completion_tokens") or 0),
            "prompt_eval_count": int(usage.get("prompt_tokens") or 0)}


def sse_to_ollama_line(line: str) -> Optional[Dict[str, Any]]:
    """One vLLM SSE line -> one Ollama NDJSON chunk (as a dict), or None to skip.

    'data: [DONE]' -> a bare done chunk; a usage-only chunk (include_usage) -> the
    done chunk carrying eval counts; a delta -> content / thinking tokens.
    """
    line = (line or "").strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return {"message": {"role": "assistant", "content": ""}, "done": True}
    import json as _json
    try:
        ev = _json.loads(payload)
    except Exception:
        return None
    choices = ev.get("choices") or []
    usage = ev.get("usage") or {}
    if not choices and usage:
        return {"message": {"role": "assistant", "content": ""}, "done": True,
                "eval_count": int(usage.get("completion_tokens") or 0),
                "prompt_eval_count": int(usage.get("prompt_tokens") or 0)}
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    content = delta.get("content") or ""
    thinking = _reasoning(delta)
    if not content and not thinking:
        return None
    return {"message": {"role": "assistant", "content": content, "thinking": thinking},
            "done": False}
