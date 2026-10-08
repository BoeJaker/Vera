"""Sense: one Frame per text (four-minds FM-1.1).

A Frame is what System 1 knows about a piece of text before anything slow runs: patterns found by
regex in microseconds (tickers, paths, URLs, node names, session ids, capability names), the
decision-space vector (one MiniLM-384 `nlp.embed` on a node), the domains that vector is closest
to (one dot product per precomputed domain card), and - when the caller asks and the budget allows -
entities (`nlp.ner`) and the language (`nlp.langid`, only for non-ASCII text).

Measured budget (FM-0.4f, 2026-10-08): the vector is ≈ 10 ms p95 per node, so patterns + vector +
cards fit the ≤ 50 ms chat PRE budget; NER (≈ 0.5 s) does not, so callers on the PRE path leave it off.

The domain cards are data: a short description per domain, embedded once and re-embedded only
when the text changes (keyed by hash). They start from the topics Vera's own reads already gate on
(enrich_core) and its subsystems, and are meant to be edited.

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

FRAME_SCHEMA = "vera.frame/v0"

#: domain -> card text (what the domain covers, in the words people use for it)
DOMAIN_CARDS: Dict[str, str] = {
    "markets": "crypto, stocks and markets: prices, tickers, trading, portfolios, exchanges, charts and market news",
    "estate": "the home lab estate: proxmox hosts and containers, nodes, networking, storage, backups, VPN, certificates",
    "models": "AI models and inference: ollama, vLLM, GPUs, embeddings, model loading, latency and benchmarks",
    "media": "media and entertainment: films, TV, music, Jellyfin and Joeflix libraries, video processing, 360 footage",
    "research": "research and the outside world: news, articles, papers, facts, people, companies, events, the web",
    "build": "writing software: code, scripts, apps, files, tests, bugs, refactoring, repositories and deployments",
    "commerce": "selling and business: eBay and Vinted listings, stock, orders, shipping labels, suppliers, pricing",
    "home": "the home: Home Assistant, lights, sensors, devices, heating, ESP32 boards and displays",
    "schedule": "time and plans: calendar, meetings, reminders, schedules, deadlines and appointments",
    "vera": "Vera herself: her memory, loops, agents, capabilities, dreams, the census, Loop Lab and her own settings",
    "chat": "small talk and conversation: greetings, thanks, opinions, feelings and casual questions",
}

_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_PATH = re.compile(r"(?<![\w:/])(?:~|\.{1,2})?/(?:[\w.@-]+/)*[\w.@-]+|\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]+")
_FILE = re.compile(r"\b[\w-]+\.(?:py|js|ts|html?|css|md|json|ya?ml|sh|txt|csv|log|toml|ini|sql)\b")
_CASHTAG = re.compile(r"(?<![\w$])\$[A-Z]{2,6}\b")
_PAIR = re.compile(r"\b[A-Z]{2,6}[/-](?:USD|USDT|USDC|GBP|EUR|BTC|ETH)\b")
_KNOWN_TICKERS = frozenset("BTC ETH SOL XRP ADA DOGE DOT LTC LINK AVAX MATIC BNB TRX XMR ATOM AAPL TSLA NVDA MSFT".split())
_WORD_UPPER = re.compile(r"\b[A-Z]{2,5}\b")
_NODE = re.compile(r"\b(?:cpu|gpu|nwm|vfs|pbs)-\d{2,3}\b|\bCT\s?\d{3}\b|\b(?:\d{1,3}\.){3}\d{1,3}\b", re.I)
_SESSION = re.compile(r"\b(?:claude|codex|dream|census|chat):[0-9a-z][0-9a-z_-]{5,}\b"
                      r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_CAP = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}\b")


def text_hash(text: str, kind: str = "") -> str:
    return hashlib.sha256((str(kind) + "\x00" + str(text or "")).encode("utf-8")).hexdigest()[:16]


def card_hash(cards: Dict[str, str]) -> str:
    raw = "\x00".join(f"{k}={v}" for k, v in sorted(cards.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _uniq(xs: Iterable[str], n: int = 12) -> List[str]:
    out: List[str] = []
    for x in xs:
        x = str(x).strip().rstrip(".,;:)")
        if x and x not in out:
            out.append(x)
        if len(out) >= n:
            break
    return out


def patterns(text: str, cap_names: Optional[Iterable[str]] = None) -> Dict[str, List[str]]:
    """Regex facts, always available, in microseconds. Capability names count only when they are
    real registered names (`cap_names`), so "e.g." or "v1.2" never pass as one."""
    t = str(text or "")
    urls = _uniq(_URL.findall(t))
    no_urls = _URL.sub(" ", t)
    pairs = _PAIR.findall(t)
    bare = _CASHTAG.sub(" ", _PAIR.sub(" ", t))          # a symbol inside a pair or cashtag is not counted twice
    tick = _CASHTAG.findall(t) + pairs + [w for w in _WORD_UPPER.findall(bare) if w in _KNOWN_TICKERS]
    caps = set(cap_names or ())
    return {
        "urls": urls,
        "paths": _uniq(_PATH.findall(no_urls) + _FILE.findall(no_urls)),
        "tickers": _uniq(x.lstrip("$") for x in tick),
        "nodes": _uniq(_NODE.findall(t)),
        "sessions": _uniq(_SESSION.findall(t)),
        "caps": _uniq(c for c in _CAP.findall(no_urls) if c in caps) if caps else [],
    }


def _norm(v: Sequence[float]) -> List[float]:
    n = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
    return [float(x) / n for x in v]


def domains(vec: Optional[Sequence[float]], cards: Dict[str, Sequence[float]], top: int = 3) -> List[Dict[str, Any]]:
    """The closest domain cards by cosine, best first. Empty without a vector."""
    if not vec or not cards:
        return []
    q = _norm(vec)
    scored = []
    for name, cv in cards.items():
        if not cv or len(cv) != len(q):
            continue
        c = _norm(cv)
        scored.append({"domain": name, "score": round(sum(a * b for a, b in zip(q, c)), 4)})
    scored.sort(key=lambda d: -d["score"])
    return scored[:max(0, int(top))]


def frame(text: str, kind: str, pats: Dict[str, List[str]], *, doms: Optional[List[Dict[str, Any]]] = None,
          entities: Optional[List[Dict[str, Any]]] = None, lang: Optional[str] = None,
          degraded: bool = False, ms: Optional[Dict[str, float]] = None, cards_version: str = "") -> Dict[str, Any]:
    """The Frame v0 record. The vector itself is not in it (callers that need it ask for it)."""
    return {"schema": FRAME_SCHEMA, "text_hash": text_hash(text, kind), "kind": str(kind or ""),
            "chars": len(str(text or "")), "patterns": pats, "domains": list(doms or []),
            "domain": (doms[0]["domain"] if doms else None), "entities": list(entities or []),
            "lang": lang, "degraded": bool(degraded), "cards": cards_version, "ms": dict(ms or {})}


def non_ascii(text: str) -> bool:
    return any(ord(c) > 127 for c in str(text or ""))
