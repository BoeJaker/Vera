"""The surface index: what each part of the estate's UI can show, and which turn it answers (canvas-enrichment slice 3).

A turn about the markets should bring the markets' charts; one about "my account" the account's figures; one about a
node, that node. Nothing said which surfaces belong to which subject, or which of them take the thing the turn named:
canvas.enrich searched capability NAMES and could only run reads that take no arguments. This index is that map,
built from what already exists:

  reads      every registered capability that only reads (no verb in its name changes anything), with the domain its
             namespace puts it in, the kinds of thing its parameters take (a symbol, a url, a node, a path, a dataset)
             and the shape the widget registry measured it answering
  widgets    every widget record in the dashboard layouts (vera/widgets/layouts) - a read already drawn, ready to place
  panels     every registered UI panel, by the domain its id names

find() reads a turn - its words and the things it names (turn_core) - scores the domains, and answers the surfaces
that fit, each with the arguments the turn fills and why it was chosen. The two accounts are kept apart, as the owner
asked ("both but they need to be distinct to the model"): a turn that only says "my account" gets both, named apart.

Pure: the registry, the measured shapes, the layouts and the panels in; an index and its answers out.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: the domains: what the model is told each one is, the namespaces that are it, the words that mean it
DOMAINS: List[Dict[str, Any]] = [
    {"id": "trading_account", "label": "your trading account (broker, portfolio, positions)",
     "prefixes": ("markets.broker.", "markets.portfolio.", "markets.trader."),
     "lead": ("markets.portfolio.positions", "markets.portfolio.history", "markets.trader.status"),
     "words": "portfolio positions position holdings holding broker brokerage pnl p&l trades exposure margin "
              "trading-account"},
    {"id": "business_accounts", "label": "your business accounts (ledger, orders, inventory, tax)",
     "prefixes": ("business.account.", "business.txn.", "business.profit.", "business.tax.", "business.dashboard",
                  "business.store.dashboard", "business.order.", "business.inventory.", "business.customer."),
     "lead": ("business.dashboard", "business.store.dashboard", "business.profit.report", "business.txn.list"),
     "words": "business ledger transactions transaction income expenses expense revenue profit profits tax invoice "
              "invoices orders order sales inventory customers customer shop store bookkeeping"},
    {"id": "markets", "label": "the markets (prices, charts, news, sentiment)",
     "prefixes": ("markets.",),
     "lead": ("markets.overview", "markets.quotes", "markets.news.feed", "markets.sentiment.map"),
     "words": "crypto cryptocurrency bitcoin btc eth ether ethereum sol solana coin coins token tokens market markets "
              "stock stocks share shares price prices trading trade forex fx defi altcoin altcoins ticker tickers "
              "exchange binance nasdaq sp500 index etf commodity gold oil chart charts candles volatility rally crash"},
    {"id": "estate", "label": "the estate (nodes, containers, GPUs, storage)",
     "prefixes": ("nodes.", "docker.", "proxmox.", "estate.", "sysmon.", "ollama.", "pxstore.", "obs."),
     "lead": ("sysmon.status", "estate.health", "nodes.storage"),
     "words": "node nodes server servers docker container containers vm vms proxmox gpu gpus cpu ram memory disk "
              "disks storage cluster estate ollama host hosts uptime load"},
    {"id": "calendar", "label": "the diary",
     "prefixes": ("cal.",),
     "lead": ("cal.events.list",),
     "words": "calendar diary schedule meeting meetings appointment appointments agenda"},
    {"id": "research", "label": "research and the fabric",
     "prefixes": ("research.", "fabric.", "reader."),
     "words": "research sources source article articles paper papers study studies dataset datasets"},
]
DOMAIN = {d["id"]: d for d in DOMAINS}
_ACCOUNTS = ("trading_account", "business_accounts")

#: a parameter's name says what kind of thing it takes
_TAKES = [(re.compile(r"^(symbol|symbols|ticker|pair|symbol_key|asset)$"), "TICKER"),
          (re.compile(r"^(url|urls|page|link)$"), "URL"),
          (re.compile(r"^(host_id|node|node_id|host|node_name)$"), "NODE"),
          (re.compile(r"^(path|file|filepath|paths)$"), "PATH"),
          (re.compile(r"^(dataset_id|dataset)$"), "DATASET"),
          (re.compile(r"^(query|q|text|topic|search)$"), "TEXT")]

#: a capability name with one of these words changes something - never run unasked (the same rule as enrich_core)
try:
    from Vera.vera.canvas.enrich_core import _WRITES, _MACHINERY, _GLANCE, _DRAWABLE   # noqa: F401
except ImportError:                                   # pragma: no cover
    try:
        from vera.canvas.enrich_core import _WRITES, _MACHINERY, _GLANCE, _DRAWABLE   # noqa: F401
    except ImportError:
        _WRITES, _MACHINERY, _GLANCE = set(), set(), set()
        _DRAWABLE = ("items", "values", "series", "level", "events", "graph")

_PANEL_DOMAIN = [(re.compile(r"market|trad|crypto"), "markets"), (re.compile(r"business|commerce|\bshop\b|\bstore\b"), "business_accounts"),
                 (re.compile(r"estate|docker|proxmox|node|system-monitor|perf-monitor|sysmon|ollama|workers"), "estate"),
                 (re.compile(r"calendar|diary"), "calendar"), (re.compile(r"research|fabric|galaxy"), "research")]


def domain_of(cap: str) -> str:
    """The domain a capability's namespace puts it in ('' - none of them). The accounts are tried first: they are
    the narrower namespaces inside markets.* and business.*."""
    c = str(cap or "")
    for d in DOMAINS:
        if any(c == p.rstrip(".") or c.startswith(p) for p in d["prefixes"]):
            return d["id"]
    return ""


#: words the shared list does not have but a turn must never run unasked: upkeep, not a view (seen on the mirror: a
#: ticker filled markets.history.repair)
_UPKEEP = {"repair", "audit", "fix", "migrate", "backfill", "reindex", "validate", "diagnose", "debug", "test", "probe",
           "heal", "rebuild", "compact", "vacuum", "gc"}


def is_read(cap: str) -> bool:
    """A capability safe to run unasked: no word of its name changes anything, or is upkeep."""
    return not any(part in _WRITES or part in _UPKEEP for part in re.split(r"[._]", str(cap or "").lower()))


def takes(schema: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """{param: entity type} for the parameters whose name says what they take."""
    out: Dict[str, str] = {}
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    for p in (props if isinstance(props, dict) else {}):
        for rx, typ in _TAKES:
            if rx.match(str(p).lower()):
                out[p] = typ
                break
    return out


def build(registry: Dict[str, Dict[str, Any]], measured: Optional[Dict[str, Dict[str, Any]]] = None,
          layouts: Sequence[Dict[str, Any]] = (), panels: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """The index: every read, layout widget and panel that belongs to a domain."""
    measured = measured or {}
    surfaces: List[Dict[str, Any]] = []
    for name, entry in (registry or {}).items():
        if not isinstance(entry, dict):          # an alias or a placeholder is not a surface
            continue
        dom = domain_of(name)
        if not dom or not is_read(name):
            continue
        schema = entry.get("schema") if isinstance(entry.get("schema"), dict) else {}
        tk = takes(schema)
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        req = [r for r in (schema.get("required") or []) if isinstance(r, str) and r in props]
        m = measured.get(name) if isinstance(measured.get(name), dict) else {}
        surfaces.append({"kind": "read", "id": name, "cap": name, "domain": dom, "takes": tk, "needs": req,
                         "shape": m.get("shape") or "", "ms": m.get("ms"),
                         "about": str((entry or {}).get("description") or "")[:200]})
    for lay in layouts or ():
        if not isinstance(lay, dict):
            continue
        for w in (lay.get("widgets") or []):
            rec = w.get("record") if isinstance(w, dict) and isinstance(w.get("record"), dict) else {}
            src = str(rec.get("source") or "")
            dom = domain_of(src)
            if not src or not dom or not is_read(src) or rec.get("form") == "section":
                continue
            surfaces.append({"kind": "widget", "id": str(lay.get("key") or "") + ":" + str(rec.get("id") or src),
                             "cap": src, "domain": dom, "takes": {}, "needs": [], "record": rec,
                             "about": str(rec.get("title") or src)})
    for p in panels or ():
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "")
        dom = next((d for rx, d in _PANEL_DOMAIN if rx.search(pid)), "")
        if dom:
            surfaces.append({"kind": "panel", "id": pid, "panel": pid, "domain": dom, "takes": {}, "needs": [],
                             "about": str(p.get("label") or pid)})
    counts: Dict[str, Dict[str, int]] = {}
    for s in surfaces:
        c = counts.setdefault(s["domain"], {"read": 0, "widget": 0, "panel": 0})
        c[s["kind"]] += 1
    return {"surfaces": surfaces, "counts": counts}


_WORD = re.compile(r"[a-z0-9&]+(?:-[a-z0-9]+)?")


def score_domains(text: str, entities: Sequence[Dict[str, Any]] = ()) -> Dict[str, float]:
    """How much the turn is about each domain: its words, and the kinds of thing it names."""
    t = str(text or "").lower()
    words = set(_WORD.findall(t))
    sc: Dict[str, float] = {}
    for d in DOMAINS:
        n = sum(1 for w in d["words"].split() if w in words)
        if n:
            sc[d["id"]] = float(n)
    types = {str(e.get("type") or "").upper() for e in entities or ()}
    if "TICKER" in types:
        sc["markets"] = sc.get("markets", 0) + 2
    # "my account" / "accounts" with nothing narrower: both accounts, named apart
    if re.search(r"\b(my|our)\s+accounts?\b|\baccounts?\b", t) and not any(sc.get(a, 0) >= 1 for a in _ACCOUNTS):
        for a in _ACCOUNTS:
            sc[a] = sc.get(a, 0) + 1.5
    return sc


def _tail(cap: str) -> str:
    return str(cap or "").split(".")[-1]


def find(index: Dict[str, Any], text: str, entities: Sequence[Dict[str, Any]] = (), *, max_items: int = 6,
         max_domains: int = 2) -> Dict[str, Any]:
    """The surfaces that answer a turn: the top domains, and in each the layout widgets, the measured glance reads,
    the reads the turn's named things fill, and the panels - each with its arguments and why."""
    sc = score_domains(text, entities)
    ranked = sorted(sc.items(), key=lambda kv: -kv[1])
    top = ranked[0][1] if ranked else 0
    # a second domain only when the turn is nearly as much about it (a "this week" is not a question for the diary)
    doms = [d for d, v in ranked if v >= 1 and v >= top * 0.6][:max_domains]
    # both accounts travel together when the turn only said "account"
    if any(d in _ACCOUNTS for d in doms):
        if sc.get("trading_account", 0) == sc.get("business_accounts", 0):
            doms = list(dict.fromkeys([d for d in doms if d not in _ACCOUNTS] + list(_ACCOUNTS)))[:max(max_domains, 2)]
    by_type: Dict[str, List[str]] = {}
    for e in entities or ():
        by_type.setdefault(str(e.get("type") or "").upper(), []).append(str(e.get("text") or ""))
    picks: List[Dict[str, Any]] = []
    for dom in doms:
        cands: List[Dict[str, Any]] = []
        lead = DOMAIN[dom].get("lead") or ()
        for s in index.get("surfaces") or []:
            if s["domain"] != dom:
                continue
            tail = _tail(s.get("cap") or "")
            # the domain's headline reads first, in their order - what a person asking about it means first
            if s["kind"] == "read" and s.get("cap") in lead and not s.get("needs"):
                cands.append(dict(s, args={}, score=4.0 - lead.index(s["cap"]) * 0.2, lead=True,
                                  why="%s - the headline of %s" % (s["cap"], DOMAIN[dom]["label"])))
                continue
            if s["kind"] == "widget":
                cands.append(dict(s, args=dict(((s.get("record") or {}).get("read") or {}).get("args") or {}), score=3.0,
                                  why="the %s board's %s" % (dom.replace("_", " "), s["about"])))
            elif s["kind"] == "read":
                if tail in _MACHINERY:
                    continue
                fill = {p: by_type[t][0] for p, t in (s.get("takes") or {}).items() if t not in ("TEXT",) and by_type.get(t)}
                if fill and all(r in fill for r in s.get("needs") or []):
                    # what the turn NAMED, filled in: the most specific thing there is - above the headlines
                    cands.append(dict(s, args=fill, score=4.6 + (0.2 if tail in _GLANCE else 0),
                                      why="%s for %s" % (s["cap"], ", ".join(fill.values()))))
                elif not s.get("needs") and s.get("shape") in _DRAWABLE:
                    cands.append(dict(s, args={}, score=2.0 + (0.5 if tail in _GLANCE else 0),
                                      why="a glance at %s (%s)" % (DOMAIN[dom]["label"], s["cap"])))
            elif s["kind"] == "panel":
                cands.append(dict(s, args={}, score=1.0, why="the %s panel" % s["about"]))
        # a headline that the dashboards already draw as a widget is placed AS that widget (the drawn form wins)
        for c in cands:
            if c["kind"] == "widget" and c.get("cap") in lead:
                c["score"], c["lead"] = 4.05 - lead.index(c["cap"]) * 0.2, True
        seen, mine = set(), []
        for c in sorted(cands, key=lambda c: -c["score"]):
            k = (c.get("cap") or c.get("panel"), tuple(sorted((c.get("args") or {}).items())))
            if k in seen:
                continue
            seen.add(k)
            mine.append(dict(c, domain_label=DOMAIN[dom]["label"]))
        picks.extend(mine[:max(1, max_items // max(1, len(doms)))])
    return {"domains": [{"id": d, "label": DOMAIN[d]["label"], "score": sc.get(d, 0)} for d in doms],
            "surfaces": [{k: v for k, v in p.items() if k not in ("score", "about")} for p in picks][:max_items]}


# ── what the model is told (slice 4): the canvas beside the reply, in a few lines ─────────────────────────────
_NAMEY = ("symbol", "name", "title", "label", "ticker", "pair", "id", "host", "node", "account", "customer", "product")
_FIGURE = re.compile(r"(price|last|value|total|balance|equity|pnl|profit|revenue|change|pct|percent|count|qty|amount|"
                     r"usage|load|cpu|gpu|mem|free|used)", re.I)


def _rows_of(res: Any) -> List[Any]:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k, v in res.items():
            if isinstance(v, list) and v and isinstance(v[0], (dict, str, int, float)):
                return v
    return []


def _fmt(v: Any) -> str:
    """A figure as a person writes it: 84,884 (not 8.488e+04), 1.69, 0.0042, 2.8 bn."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        a = abs(v)
        if a >= 1e9:
            return "%.3g bn" % (v / 1e9)
        if a >= 1000:
            return "{:,.0f}".format(v)
        if isinstance(v, int):
            return str(v)
        return ("%.4g" % v) if a >= 0.001 or a == 0 else ("%.2g" % v)
    return str(v)[:40]


#: a bar's span, said in words
_TF = {"1m": "minute", "5m": "5-minute", "15m": "15-minute", "30m": "30-minute", "1h": "hour", "4h": "4-hour",
       "1d": "day", "1w": "week", "1wk": "week", "1mo": "month"}


def _candles(res: Dict[str, Any], rows: List[Any]) -> str:
    """A chart's bars said plainly: the last close, the latest bar's move, and the move over the whole window with the
    window named - a percentage without its period was read by the model as today's move."""
    closes = [r.get("close") for r in rows if isinstance(r, dict) and isinstance(r.get("close"), (int, float))]
    if len(closes) < 2:
        return ""
    unit = _TF.get(str(res.get("timeframe") or ""), "bar")
    n = len(closes)
    span = "%d %ss" % (n, unit) if unit != "bar" else "%d bars" % n
    head = next((str(res[k]) for k in ("symbol", "name", "title") if res.get(k) not in (None, "")), "")
    last, prev, first = closes[-1], closes[-2], closes[0]
    bits = ["last %s" % _fmt(last)]
    if prev:
        bits.append("latest %s %+.2f%%" % (unit, (last - prev) / prev * 100))
    if first:
        bits.append("over the %s shown %+.1f%%" % (span, (last - first) / first * 100))
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    lows = [r["low"] for r in rows if isinstance(r, dict) and num(r.get("low"))] or closes
    highs = [r["high"] for r in rows if isinstance(r, dict) and num(r.get("high"))] or closes
    bits.append("range %s to %s" % (_fmt(min(lows)), _fmt(max(highs))))
    return ((head + " - ") if head else "") + "; ".join(bits)


def _base(sym: Any) -> str:
    """BTC from "BTC/USD", "BTC-USD", "yahoo:BTC-USD" - the thing a row is about."""
    return re.split(r"[/\-_ ]", str(sym or "").upper().split(":")[-1].strip() or "?")[0]


def _row_hits(r: Any, bases: Sequence[str]) -> bool:
    return isinstance(r, dict) and any(_base(r.get(k)) in bases for k in ("symbol", "key", "ticker", "pair", "asset", "id", "name")
                                       if r.get(k) not in (None, ""))


def summarise(res: Any, max_chars: int = 220, focus: Sequence[str] = ()) -> str:
    """A read's answer in one line a model can talk about: how many rows and the first few by name (with a figure when
    a row has one), or the headline figures of a single record. `focus` - the tickers the turn named: their rows come
    first, with all their figures (a question about BTC is answered by BTC's row, not the first five of thirty-six)."""
    bases = [b for b in (_base(x) for x in focus or ()) if b and b != "?"]
    # a columnar series ({t:[...], equity:[...]}): how many points, and each measure's last value and change
    if isinstance(res, dict) and isinstance(res.get("t"), list) and len(res["t"]) > 1:
        cols = [(k, v) for k, v in res.items() if k != "t" and isinstance(v, list) and len(v) == len(res["t"])
                and all(isinstance(x, (int, float)) for x in v[-3:])]
        if cols:
            bits = []
            for k, v in cols[:3]:
                chg = ("%+.1f%%" % ((v[-1] - v[0]) / v[0] * 100)) if v[0] else ""
                bits.append("%s last %s%s" % (k, _fmt(v[-1]), (" (" + chg + ")") if chg else ""))
            return ("%d points: %s" % (len(res["t"]), "; ".join(bits)))[:max_chars]
    rows = _rows_of(res)
    # rows with nothing to name them by (a chart's candles): the record's own headline figures say more
    if rows and isinstance(res, dict) and all(isinstance(r, dict) and not any(r.get(k) not in (None, "") for k in _NAMEY) for r in rows[:5]):
        said = _candles(res, rows)
        if said:
            return said[:max_chars]
        head = [str(res[k]) for k in ("symbol", "name", "title") if res.get(k) not in (None, "")][:1]
        figs = ["%s %s" % (k, _fmt(v)) for k, v in res.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "count"][:4]
        if head or figs:
            return (" ".join(head + ["-"] if head and figs else head) + " " + "; ".join(figs) + " (%d rows)" % len(rows)).strip()[:max_chars]
    if rows and bases and any(_row_hits(r, bases) for r in rows):
        hits = [r for r in rows if _row_hits(r, bases)][:3]
        bits = []
        for r in hits:
            nm = next((str(r[k]) for k in _NAMEY if r.get(k) not in (None, "")), "")
            figs = ["%s %s" % (k, _fmt(v)) for k, v in r.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
                    and _FIGURE.search(str(k))][:5]
            bits.append(nm + (" (" + ", ".join(figs) + ")" if figs else ""))
        return ("%d row%s; for what was asked: %s" % (len(rows), "" if len(rows) == 1 else "s", "; ".join(bits)))[:max_chars]
    if rows:
        bits = []
        for r in rows[:5]:
            if isinstance(r, dict):
                nm = next((str(r[k]) for k in _NAMEY if r.get(k) not in (None, "")), "")
                fig = next(("%s %s" % (k, _fmt(v)) for k, v in r.items() if _FIGURE.search(str(k)) and isinstance(v, (int, float))), "")
                bits.append((nm + (" (" + fig + ")" if fig else "")).strip() or "?")
            else:
                bits.append(_fmt(r))
        s = "%d row%s: %s" % (len(rows), "" if len(rows) == 1 else "s", "; ".join(b for b in bits if b))
        return s[:max_chars]
    if isinstance(res, dict):
        figs = ["%s %s" % (k, _fmt(v)) for k, v in res.items() if isinstance(v, (int, float)) and not isinstance(v, bool)][:6]
        return ("; ".join(figs) or ", ".join(list(res.keys())[:6]))[:max_chars]
    return _fmt(res)[:max_chars]


def prompt_block(placed: Sequence[Dict[str, Any]], *, max_chars: int = 1200, pending: int = 0) -> str:
    """The note the model gets: what was put on the canvas for this question, by domain, each in a line. The two
    accounts keep their own labels. ~300 tokens at most (owner, 2026-10-03: "Yes, ~300 tokens")."""
    if not placed and not pending:
        return ""
    lines = ["## On the canvas beside this reply",
             "Placed for this question; the user can see these, and the figures below are current. Use them in the answer "
             "where they answer the question, and refer to the items (\"the chart beside this\") rather than reciting whole "
             "tables. Each is named with what it belongs to - keep those apart."]
    for p in placed:
        what = p.get("title") or p.get("cap") or p.get("panel") or "item"
        line = "- [%s] %s" % (p.get("domain_label") or p.get("domain") or "the estate", what)
        if p.get("summary"):
            line += ": " + p["summary"]
        lines.append(line)
    if pending:
        lines.append("- (%d more %s still arriving on the canvas)" % (pending, "item is" if pending == 1 else "items are"))
    out = "\n".join(lines)
    return out if len(out) <= max_chars else out[:max_chars - 24].rsplit("\n", 1)[0] + "\n- (more on the canvas)"
