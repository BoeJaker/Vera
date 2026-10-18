"""Who called a capability, from where, and why (pure, no I/O).

B0.11 R0, slice 1 (backbone roadmap; desk package DO-0.4). The dispatcher already knows several facts about each
call (its context variables): which capability's own route is serving, whether it came through /mcp/call, what
caller_kind the body claimed, the background driver, the capabilities it runs inside. The desk's hosts add two more
in a header: the surface that asked and whether a person or an auto-refresh did:

    X-Vera-Surface: <desk|dashboard|panel:<id>|chat|lhm>; trigger=<auto|person>

From these this module derives one actor kind per call and the field the usage counter stores, so "order by usage"
can rank by what people ask for (trigger=person) apart from what screens re-read on their own.

Observation only: nothing here allows or refuses a call. The policy side of B0.11 rides the Capability Contract v2
enforcement (roadmap-activation), not this module.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SURFACES = ("desk", "dashboard", "panel", "chat", "lhm")
TRIGGERS = ("auto", "person")
BRIDGE_KINDS = ("claude", "codex", "mcp")
RETENTION_DAYS = 91
RETENTION_S = RETENTION_DAYS * 86400
KEY_PREFIX = "vera:caps:usage:"
FLUSH_INTERVAL_S = 30.0
MAX_FIELDS = 20000            # distinct fields held between flushes; beyond it new fields are counted as overflow

_TOKEN = re.compile(r"[^a-z0-9:._-]")
UNKNOWN = "-"


def _clean(value: str, limit: int = 64) -> str:
    return _TOKEN.sub("", str(value or "").strip().lower())[:limit]


def parse_surface(header: Optional[str]) -> Tuple[str, str]:
    """'panel:markets; trigger=person' -> ('panel:markets', 'person'). Unknown surfaces read 'other'; a missing or
    unknown trigger reads ''."""
    if not header:
        return "", ""
    parts = [p.strip() for p in str(header).split(";")]
    surface = _clean(parts[0])
    base = surface.split(":", 1)[0]
    if surface and base not in SURFACES:
        surface = "other"
    trigger = ""
    for p in parts[1:]:
        k, _, v = p.partition("=")
        if k.strip().lower() == "trigger":
            v = v.strip().lower()
            trigger = v if v in TRIGGERS else ""
    return surface, trigger


def actor_kind(*, name: str, running_caps: Sequence[str] = (), http_cap: str = "", mcp_call: bool = False,
               caller_kind: str = "", background: str = "") -> str:
    """One word for who drove this call.

    nested            inside another capability (a loop, an agent turn, a DAG step, a panel's composite)
    background:<x>    a background driver (dream, v8, fabric, idle ...), from BACKGROUND_LLM's label
    bridge:<kind>     an MCP bridge (caller_kind claude / codex / mcp)
    caller:<kind>     another self-declared caller_kind
    ui:mcp            the browser's /mcp/call: a person, or the chat's cap-in-chat and copilots acting for one
    ui:route          the capability's own HTTP route (a panel button, a desk refresh)
    internal          none of the above: schedules, triggers, startup loops
    """
    if running_caps:
        return "nested"
    if background:
        return "background:" + (_clean(str(background).split(":", 1)[0], 24) or "other")
    if caller_kind:
        ck = _clean(caller_kind, 24) or "other"
        return ("bridge:" if ck in BRIDGE_KINDS else "caller:") + ck
    if mcp_call:
        return "ui:mcp"
    if http_cap and http_cap == name:
        return "ui:route"
    return "internal"


def usage_field(cap: str, actor: str, surface: str = "", trigger: str = "") -> str:
    """The counter's hash field: cap|actor|surface|trigger ('-' for an unknown part; '|' never inside a part)."""
    def part(v: str) -> str:
        v = str(v or "").replace("|", "")[:128]
        return v or UNKNOWN
    return "|".join((part(cap), part(actor), part(surface), part(trigger)))


def split_field(field: str) -> Dict[str, str]:
    cap, actor, surface, trigger = (str(field).split("|") + [UNKNOWN] * 4)[:4]
    return {"cap": cap, "actor": actor, "surface": surface, "trigger": trigger}


def day_key(ts: Optional[float] = None) -> str:
    """The Redis hash for one UTC day: vera:caps:usage:YYYYMMDD."""
    return KEY_PREFIX + time.strftime("%Y%m%d", time.gmtime(time.time() if ts is None else ts))


def day_keys(days: int, now: Optional[float] = None) -> List[str]:
    """The last `days` day keys, newest first (1..RETENTION_DAYS)."""
    days = max(1, min(int(days or 1), RETENTION_DAYS))
    t = time.time() if now is None else now
    return [day_key(t - i * 86400) for i in range(days)]


class UsageBuffer:
    """Counts held in process between flushes, so the hot path costs a dict increment, not a Redis call."""

    def __init__(self, interval: float = FLUSH_INTERVAL_S, max_fields: int = MAX_FIELDS):
        self.interval = float(interval)
        self.max_fields = int(max_fields)
        self.counts: Counter = Counter()
        self.overflow = 0
        self.last_flush = 0.0
        self.flushing = False

    def add(self, field: str, n: int = 1) -> None:
        if field in self.counts or len(self.counts) < self.max_fields:
            self.counts[field] += n
        else:
            self.overflow += n

    def due(self, now: float) -> bool:
        return bool(self.counts) and not self.flushing and now - self.last_flush >= self.interval

    def drain(self, now: Optional[float] = None) -> Dict[str, int]:
        out = dict(self.counts)
        if self.overflow:
            out[usage_field("(overflow)", UNKNOWN)] = out.get(usage_field("(overflow)", UNKNOWN), 0) + self.overflow
        self.counts = Counter()
        self.overflow = 0
        self.last_flush = time.monotonic() if now is None else now
        return out

    def restore(self, batch: Mapping[str, int]) -> None:
        """Put a batch back after a failed write (bounded like add)."""
        for f, n in batch.items():
            self.add(f, int(n))

    def snapshot(self) -> Dict[str, int]:
        return dict(self.counts)


def _decode(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


def aggregate(hashes: Iterable[Mapping[Any, Any]], *, cap: str = "", surface: str = "", trigger: str = "",
              actor: str = "", limit: int = 100) -> Dict[str, Any]:
    """Sum day hashes into rows (cap, actor, surface, trigger, count) and a per-capability view that ranks by what
    people asked for: person first, then all calls. Filters match a prefix ('panel' matches 'panel:markets')."""
    total: Counter = Counter()
    for h in hashes:
        for f, n in (h or {}).items():
            try:
                total[_decode(f)] += int(_decode(n))
            except ValueError:
                continue
    rows = []
    by_cap: Dict[str, Dict[str, Any]] = {}
    for f, n in total.items():
        r = split_field(f)
        if cap and not r["cap"].startswith(cap):
            continue
        if surface and not r["surface"].startswith(surface):
            continue
        if trigger and r["trigger"] != trigger:
            continue
        if actor and not r["actor"].startswith(actor):
            continue
        r["count"] = n
        rows.append(r)
        b = by_cap.setdefault(r["cap"], {"cap": r["cap"], "person": 0, "auto": 0, "unmarked": 0, "total": 0,
                                         "actors": Counter(), "surfaces": Counter()})
        b[r["trigger"] if r["trigger"] in TRIGGERS else "unmarked"] += n
        b["total"] += n
        b["actors"][r["actor"]] += n
        b["surfaces"][r["surface"]] += n
    rows.sort(key=lambda r: (-r["count"], r["cap"]))
    caps = sorted(by_cap.values(), key=lambda b: (-b["person"], -b["total"], b["cap"]))
    for b in caps:
        b["actors"] = dict(b["actors"].most_common(8))
        b["surfaces"] = dict(b["surfaces"].most_common(8))
    lim = max(1, int(limit or 100))
    return {"rows": rows[:lim], "by_cap": caps[:lim], "calls": sum(r["count"] for r in rows), "distinct": len(rows)}
