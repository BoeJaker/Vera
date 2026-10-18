"""Which capabilities a chat agent may be offered, and which of them fit the message (four-minds FM-3.3 'on'; owner,
17 Oct: "capability selection for the chat agents so they can reach a list of all of the safe capabilities via it and
receive a shortlist of the most appropriate ones").

The chat page offers the model a manifest of capabilities per turn; whatever is in it the model may call. Until now
that was the agent's own allow-list scored by words. With the switch on, the server ranks the whole SAFE catalogue
for the message and the page offers that list beside the agent's own.

Safe, conservatively - a capability is offered only when:
- it is exposed to agents (mcp_expose is not False), and
- its declared contract effects stay within read / network / model / none with no approval, or - when it declares
  none - its last name part reads as a lookup (get, list, search, status, fetch, ...) and not as a change (set,
  delete, run, write, install, ...), and its family is not one that acts on the system (shells, secrets, deploys,
  sandboxes, switches, ...). Anything unsure is left out: it stays reachable only through the agent's own list.

Ranking: reciprocal-rank fusion of what similar past steps used (caps.shortlist kNN) and the loop's own keyword
scorer over the safe catalogue; the fused head is then reordered by the cross-encoder when it answers in time.

Pure: no I/O.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

SAFE_EFFECTS = frozenset({"none", "read", "network", "model"})
#: families that act on the system - never offered by the shortlist, whatever their verb
UNSAFE_PREFIXES = ("exec.", "sys.", "secrets.", "keydrop.", "evolve.", "sandbox.", "pve.", "proxmox.", "docker.",
                   "provision.", "autoenroll.", "census.control", "minds.switch", "backup.", "ssh.", "ide.", "git.",
                   "vscode.", "netctl.", "foundry.", "broker.", "markets.trade", "commerce.order", "mesh.", "esp32.",
                   "homeassistant.", "ha.", "operator.", "browser.", "telegram.", "email.", "delivery.", "board.item.",
                   "cron.", "schedule.", "evolve", "decide.purge", "chat.replay.purge", "fabric.chroma_reset",
                   # 18 Oct review of the live catalogue: secret stores, host files, the printer, certificates
                   "secstore.", "vault.", "openbao.", "pki.", "fs.", "print.", "printer.", "jobs.", "auth.", "identity.",
                   "redis.", "acct.", "accounts.", "netscan.")
#: a name with any of these parts touches credentials or private keys - never offered
SENSITIVE_WORDS = frozenset({"secret", "secrets", "token", "tokens", "password", "passwords", "credential",
                             "credentials", "key", "keys", "kv", "apikey", "api_key", "cookie", "cookies", "session_key",
                             "private", "otp", "totp", "mfa", "2fa", "seal", "unseal", "cert", "certs", "pem"})
READ_WORDS = frozenset({
    "get", "list", "search", "status", "report", "read", "query", "stats", "show", "info", "find", "describe",
    "health", "map", "seek", "browse", "explain", "summary", "lookup", "fetch", "preview", "inspect", "history",
    "recent", "top", "compare", "count", "check", "score", "detect", "classify", "extract", "embed", "rerank",
    "translate", "summarize", "summarise", "answer", "ask", "resolve", "validate", "diff", "view", "latest", "quote",
    "price", "prices", "news", "weather", "forecast", "analyze", "analyse", "catalog", "catalogue", "tree", "grep",
    "stat", "overview", "ner", "nli", "zeroshot", "sentiment", "langid", "qa", "frame", "timeline", "graph", "trend",
    "trends", "lookup", "define", "convert", "calc", "calculate", "math", "time", "date", "research", "related",
    "neighbours", "similar", "recall", "select", "pick", "callable", "inventory", "versions", "rows", "last", "doc",
    "docs", "help", "schema", "capabilities"})
CHANGE_WORDS = frozenset({
    "set", "config", "configure", "enable", "disable", "reset", "reload", "register", "unregister", "save", "delete",
    "run", "exec", "execute", "write", "create", "update", "remove", "kill", "stop", "start", "restart", "install",
    "uninstall", "deploy", "send", "post", "move", "purge", "clear", "upsert", "add", "edit", "apply", "promote",
    "release", "adopt", "commit", "push", "pin", "unpin", "approve", "reject", "cancel", "pause", "resume", "rename",
    "import", "export", "upload", "train", "build", "generate", "spawn", "claim", "drop", "archive", "restore",
    "trade", "order", "buy", "sell", "pay", "notify", "enqueue", "schedule", "dispatch", "open", "close", "mount",
    "attach", "detach", "connect", "disconnect", "replace", "patch", "merge", "split", "seed", "ingest", "backfill",
    "reap", "prune", "wipe", "format", "flash", "reboot", "shutdown", "grant", "revoke", "rotate", "seal", "unseal",
    # 18 Oct (first live sample: link_memory, graph_normalize passed): more verbs that change state
    "link", "unlink", "normalize", "normalise", "tag", "untag", "mark", "record", "store", "learn", "sync", "refresh",
    "rebuild", "reindex", "index", "compact", "migrate", "fade", "forget", "label", "rate", "toggle", "assign", "bind",
    "unbind", "retire", "fold", "ack", "acknowledge", "consolidate", "dedupe", "dedup", "repair", "fix", "heal",
    "enrich", "annotate", "crawl", "harvest", "capture", "snapshot", "warm", "preload", "load", "unload", "evict",
    "invalidate", "flush", "kick", "trigger", "fire", "emit", "publish", "subscribe", "unsubscribe", "watch", "enable",
    "arm", "disarm", "lock", "unlock", "queue", "retry", "replay", "rerun", "resolve_conflict", "reconcile",
    "rollback", "rehearse", "promote", "demote", "accept", "decline", "vote", "feedback", "note", "remember",
    "ingest", "observe", "learn", "plan", "act", "do", "make", "author", "draft", "compose", "render", "speak",
    "synthesize", "synthesise", "transcribe", "say", "play", "reply", "respond", "chat", "message", "call", "invoke",
    "set_mode", "switch", "swap", "assess", "judge", "grade", "review", "bench", "benchmark", "probe", "test", "verify",
    "eval", "evaluate", "sweep", "tune", "optimize", "optimise", "compile", "package", "release", "land", "gate",
    "auto", "now", "recover", "reprocess", "process", "summarise_now"})
RRF_K = 60


def _words(name: str) -> List[str]:
    return [w for w in re.split(r"[._\-]+", str(name or "").lower()) if w]


def safe(name: str, entry: Mapping[str, Any]) -> Tuple[bool, str]:
    """(offerable, why)."""
    if not isinstance(entry, Mapping) or entry.get("mcp_expose") is False:
        return False, "not exposed to agents"
    n = str(name or "")
    if n.startswith(UNSAFE_PREFIXES):
        return False, "a family that acts on the system"
    if any(w in SENSITIVE_WORDS for w in _words(n)):
        return False, "touches credentials or keys"
    c = entry.get("contract") or {}
    eff = {str(e) for e in (c.get("effects") or [])}
    if eff:
        bad = eff - SAFE_EFFECTS
        if bad:
            return False, "effects " + ",".join(sorted(bad))
        if str((c.get("approval") or {}).get("status") or "not_required") != "not_required":
            return False, "needs approval"
        return True, "contract: " + ",".join(sorted(eff))
    ws = _words(n)
    last = ws[-1] if ws else ""
    # any part of the name a change (18 Oct: "graph" in the middle let memory.graph_normalize through) ...
    if any(w in CHANGE_WORDS for w in ws):
        return False, "its name reads as a change"
    # ... and the action - the last part - must read as a lookup
    if last in READ_WORDS:
        return True, "its name reads as a lookup"
    return False, "unsure - left out"


def catalogue(registry: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{name: {tags, description, why}} of every safe capability."""
    out = {}
    for name, e in registry.items():
        ok, why = safe(name, e)
        if ok:
            out[name] = {"tags": list(e.get("tags") or []) + list(e.get("http_tags") or []),
                         "description": str(e.get("description") or ""), "why": why}
    return out


def fuse(lists: Mapping[str, Sequence[str]], allowed: Iterable[str], k: int) -> List[Dict[str, Any]]:
    """Reciprocal-rank fusion of ranked name lists, only allowed names: [{name, score, by}] best first."""
    ok = set(allowed)
    score: Dict[str, float] = {}
    by: Dict[str, List[str]] = {}
    for src, names in lists.items():
        for i, n in enumerate(names):
            if n not in ok:
                continue
            score[n] = score.get(n, 0.0) + 1.0 / (RRF_K + i + 1)
            by.setdefault(n, []).append(src)
    ranked = sorted(score, key=lambda n: (-score[n], n))[:max(1, int(k))]
    return [{"name": n, "score": round(score[n], 5), "by": "+".join(by[n])} for n in ranked]


def card(name: str, entry: Mapping[str, Any]) -> str:
    """What the cross-encoder reads for one capability."""
    return f"{name.replace('.', ' ').replace('_', ' ')}: {str(entry.get('description') or '')[:300]}"
