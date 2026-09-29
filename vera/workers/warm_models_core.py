"""warm_models_core.py - which models each Ollama node keeps loaded, and when a
workload takes the slots over.

The problem it answers (user, 2026-09-28): time to first token is dominated by
cold loads. A CPU node's first call to a model pays 1-60 s of load (measured on
gpu-250-cpu: the 9b 10.5 s cold, ~1 s warm; the long-horizon 35b MoE 62 s), and
every node let its models lapse after Ollama's five-minute default. So:

  * every node has SLOTS - distinct models held resident: a GPU node 1 (one
    LLM on the card), a CPU node 2 (one runner serves OLLAMA_NUM_PARALLEL=2
    callers, so "2 slots" is two different models, each able to answer two
    callers at once). Embedding models are tiny and ride beside the slots -
    they never take one.
  * the BASELINE fills the slots: an explicit per-node list if one is set,
    else what the routing rules point at this node (a rule's pin/prefer and
    its model), then the node's class default. `@default` is the configured
    default model (cfg.OLLAMA_MODEL), `@naming` the naming route's model.
  * a SCENARIO is a workload that takes the slots over while it runs hot -
    "coding load is high: coders into every slot". It turns on when its job
    types' recent demand crosses a threshold and stays on for `hold_s` after
    the demand falls away (hysteresis, so a burst does not thrash loads).
  * a node's planned set must fit its memory; the lowest-priority model that
    does not fit is dropped and reported, never loaded into a spill.

The runtime (warm_models_capabilities) turns the plan into Ollama calls:
load what is absent with keep_alive=-1 at the planned window, release what it
loaded that the plan dropped. It never re-arms a resident model on every tick
- measured, an empty-prompt call to a resident 9b on CPU took ~33 s and held
up the node's embeds; Vera's own calls keep a planned model resident instead
(they carry keep_alive=-1 and the planned window, see `request_overrides`).

Pure: dicts in, plans out. No app imports, no clock reads (now is passed in).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

REDIS_KEY = "vera:ollama:warm"             # json config
STATE_KEY = "vera:ollama:warm:state"       # json {scenarios:{name:{since,last_hot}}}
TICK_S = 60

#: Ollama's "never expire". A NUMBER: the string "-1" is a 400 (measured).
KEEP_FOREVER = -1
#: An expiry further out than this is one of ours (keep_alive=-1 shows as ~300 years).
FOREVER_AFTER_S = 10 * 365 * 86400
#: Re-arm a planned model only when it is about to lapse - a re-arm of a big
#: CPU model is not free (see the module docstring).
REARM_WITHIN_S = 300

#: Share of a node's memory the planned models may take (weights + KV room).
MEM_SHARE = 0.85
#: KV/runtime overhead on top of the weights, as a fraction of the weights.
KV_OVERHEAD = 0.20

EMBED_HINTS = ("embed", "minilm", "bge-", "bge:", "e5-", "gte-", "snowflake-arctic-embed")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    # planned window on a CPU node: every call to a planned model on that node
    # asks for at least this, so one runner serves them all (a different
    # num_ctx is a new runner - a reload)
    "cpu_num_ctx": 16384,
    "num_ctx": {},                 # model -> window override
    # the class defaults used when neither an explicit list nor a route names
    # enough models for the node's slots
    "class_defaults": {
        "gpu": ["@default"],
        "cpu_sibling": ["@default", "@naming"],
        # the default model for overflow, and the long-horizon MoE so its jobs
        # still start warm when the node they are pinned to is down
        "cpu": ["@default", "@long_horizon"],
    },
    "nodes": {
        # node id -> {models:[...] (explicit, replaces route-derived),
        #             embed:"model"|"" (kept beside the slots), slots:int,
        #             ram_gb:float, reserve:[models a scenario may not take]}
    },
    # embedders kept warm beside the slots, per node class, unless a node says
    "embed_classes": {"cpu_sibling": "@embed", "cpu": "@embed"},
    # CPU spill: when a GPU-preferring call finds the GPU full, a CPU node that
    # already has the model resident may take it - only if it has proved at
    # least this fast for that model (route stats), or a scenario covering the
    # job type is active. Measured 2026-09-28: the 9b does 3.86 tok/s on
    # gpu-250-cpu against 15-30 on the GPU, so the bar is real.
    "spill_min_tps": 3.0,
    # ...and never a prompt bigger than this: a CPU prompt-eval of a big
    # context is slower than waiting for the GPU
    "spill_max_ctx": 8192,
    "scenarios": [
        {
            "name": "coding",
            "label": "Coding load is high - coders into the slots",
            "enabled": False,
            "job_types": ["code", "loop_coder"],
            "min_requests": 6,          # this many calls ...
            "window_s": 600,            # ... within this window, or
            "min_inflight": 2,          # this many in flight right now
            "hold_s": 900,              # stays on this long after it cools
            "fill": "all",              # "all" or a number of slots per node
            "models": {"gpu": [], "cpu": ["qwen3-coder:30b"],
                       "cpu_sibling": ["qwen3-coder:30b"]},
            "spill": True,              # its job types may spill to warm CPU nodes
        },
    ],
}


# ── small helpers ────────────────────────────────────────────────────────────
def is_embed_model(name: Any) -> bool:
    n = str(name or "").lower()
    return any(h in n for h in EMBED_HINTS)


def base_tag(name: Any) -> str:
    """`x` and `x:latest` are one model - the only equivalence Ollama applies."""
    n = str(name or "").strip()
    return n[:-len(":latest")] if n.endswith(":latest") else n


def same_model(a: Any, b: Any) -> bool:
    return bool(a) and base_tag(a) == base_tag(b)


def merge_config(stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Defaults with a stored config laid over them (one level deep for dicts;
    a stored `scenarios` list replaces the default list)."""
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULT_CONFIG.items()}
    for k, v in (stored or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def node_class(iid: str, inst: Dict[str, Any], instances: Dict[str, Dict[str, Any]]) -> str:
    """gpu | cpu_sibling (the CPU Ollama beside a GPU node's own) | cpu."""
    if inst.get("has_gpu"):
        return "gpu"
    if iid.endswith("-cpu") and (instances.get(iid[:-len("-cpu")]) or {}).get("has_gpu"):
        return "cpu_sibling"
    return "cpu"


def slots_for(cls: str, override: Any = None, gpu_slots: int = 1, cpu_slots: int = 2) -> int:
    try:
        if override is not None and int(override) > 0:
            return int(override)
    except (TypeError, ValueError):
        pass
    return gpu_slots if cls == "gpu" else cpu_slots


def resolve_alias(model: str, aliases: Dict[str, str]) -> str:
    m = str(model or "").strip()
    if m.startswith("@"):
        return str(aliases.get(m) or "")
    return m


def _dedupe(models: Iterable[str]) -> List[str]:
    out: List[str] = []
    for m in models:
        if m and not any(same_model(m, x) for x in out):
            out.append(m)
    return out


# ── the baseline ─────────────────────────────────────────────────────────────
def route_models_for(iid: str, rules: Dict[str, Dict[str, Any]], aliases: Dict[str, str]) -> List[str]:
    """Models the routing rules send to this node: a rule that PINS the node
    first, then one that PREFERS it. A rule with no model means the default
    model. Embedding rules are left out (the embedder rides beside the slots)."""
    pinned, preferred = [], []
    for jt, r in sorted((rules or {}).items()):
        if not isinstance(r, dict) or jt == "embedding":
            continue
        model = str(r.get("model") or "") or aliases.get("@default", "")
        if is_embed_model(model):
            continue
        if str(r.get("pin") or "") == iid:
            pinned.append(model)
        elif str(r.get("prefer") or "") == iid:
            preferred.append(model)
    return _dedupe(pinned + preferred)


def baseline_for(iid: str, cls: str, cfg: Dict[str, Any], rules: Dict[str, Dict[str, Any]],
                 aliases: Dict[str, str], slots: int) -> Tuple[List[str], str]:
    """(ordered models, where they came from). An explicit node list wins;
    else the routes' models, topped up from the class default."""
    ncfg = (cfg.get("nodes") or {}).get(iid) or {}
    explicit = [resolve_alias(m, aliases) for m in (ncfg.get("models") or [])]
    explicit = _dedupe(m for m in explicit if m and not is_embed_model(m))
    if explicit:
        return explicit[:slots], "node config"
    routed = route_models_for(iid, rules, aliases)
    fill = [resolve_alias(m, aliases) for m in
            ((cfg.get("class_defaults") or {}).get(cls) or [])]
    models = _dedupe(routed + [m for m in fill if m and not is_embed_model(m)])
    src = "routes + class default" if routed else "class default"
    return models[:slots], src


def embed_for(iid: str, cls: str, cfg: Dict[str, Any], aliases: Dict[str, str]) -> str:
    ncfg = (cfg.get("nodes") or {}).get(iid) or {}
    if "embed" in ncfg:
        return resolve_alias(str(ncfg.get("embed") or ""), aliases)
    return resolve_alias(str((cfg.get("embed_classes") or {}).get(cls) or ""), aliases)


# ── scenarios ────────────────────────────────────────────────────────────────
def demand_by_job(events: Iterable[Tuple[float, str]], now: float, window_s: float) -> Dict[str, int]:
    """Calls per job type among (timestamp, job_type) events inside the window."""
    out: Dict[str, int] = {}
    for ts, jt in events or ():
        if now - float(ts) <= window_s:
            out[str(jt)] = out.get(str(jt), 0) + 1
    return out


def scenario_states(scenarios: List[Dict[str, Any]], events: List[Tuple[float, str]],
                    inflight: Dict[str, int], prev: Dict[str, Dict[str, Any]], now: float,
                    blocked: str = "") -> Dict[str, Dict[str, Any]]:
    """Which scenarios are active now. `prev` is the last state ({name: {since,
    last_hot}}); the result is the next one plus `active`, `hot` and `why`.
    `blocked` (e.g. "a census goal is in flight") keeps every scenario off -
    swapping the models under a measurement would taint it."""
    out: Dict[str, Dict[str, Any]] = {}
    for sc in scenarios or []:
        name = str(sc.get("name") or "")
        if not name:
            continue
        p = dict((prev or {}).get(name) or {})
        jts = [str(j) for j in (sc.get("job_types") or [])]
        window = float(sc.get("window_s") or 600)
        counts = demand_by_job(events, now, window)
        n = sum(counts.get(j, 0) for j in jts)
        live = sum(int((inflight or {}).get(j, 0)) for j in jts)
        need_n = int(sc.get("min_requests") or 0)
        need_live = int(sc.get("min_inflight") or 0)
        hot = bool((need_n and n >= need_n) or (need_live and live >= need_live))
        st = {"requests": n, "inflight": live, "hot": hot, "since": p.get("since"),
              "last_hot": p.get("last_hot")}
        if not sc.get("enabled", True):
            st.update(active=False, why="disabled", since=None)
        elif blocked:
            st.update(active=False, why=blocked, since=None)
        elif hot:
            st.update(active=True, last_hot=now, since=p.get("since") or now,
                      why=f"{n} call(s) in {int(window)} s, {live} in flight")
        elif p.get("last_hot") and now - float(p["last_hot"]) < float(sc.get("hold_s") or 0):
            left = int(float(sc.get("hold_s") or 0) - (now - float(p["last_hot"])))
            st.update(active=True, why=f"cooling - holds {left} s more")
        else:
            st.update(active=False, why=f"{n}/{need_n} call(s), {live}/{need_live} in flight",
                      since=None)
        out[name] = st
    return out


def apply_scenarios(models: List[str], cls: str, slots: int, scenarios: List[Dict[str, Any]],
                    states: Dict[str, Dict[str, Any]], aliases: Dict[str, str],
                    reserve: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """The node's slots after active scenarios take theirs. Returns (models,
    names of the scenarios that changed this node). Reserved models keep their
    slot; a scenario fills the rest ("all") or `fill` of them, first come."""
    reserved = [m for m in models if any(same_model(m, r) for r in reserve or ())]
    free = [m for m in models if m not in reserved]
    took: List[str] = []
    for sc in scenarios or []:
        if not (states.get(str(sc.get("name") or "")) or {}).get("active"):
            continue
        want = _dedupe(resolve_alias(m, aliases) for m in ((sc.get("models") or {}).get(cls) or []))
        want = [m for m in want if m and not any(same_model(m, r) for r in reserved)]
        if not want:
            continue
        room = slots - len(reserved)
        fill = sc.get("fill", "all")
        n = room if fill == "all" else max(0, min(room, int(fill or 0)))
        if n <= 0:
            continue
        take = want[:n]
        keep = [m for m in free if not any(same_model(m, t) for t in take)]
        free = take + keep
        took.append(str(sc["name"]))
    return (reserved + free)[:slots], took


# ── memory ───────────────────────────────────────────────────────────────────
def fit_memory(models: List[str], sizes: Dict[str, int], budget_bytes: float,
               embed: str = "") -> Tuple[List[str], List[Dict[str, Any]]]:
    """Keep models in order while they fit the budget (weights * (1+KV_OVERHEAD)).
    A model whose size is unknown is kept (it cannot be priced, and the node
    reports its own OOM). Returns (kept, dropped[{model, why}])."""
    def size_of(m: str) -> int:
        for k, v in (sizes or {}).items():
            if same_model(k, m):
                return int(v or 0)
        return 0
    used = size_of(embed) * (1 + KV_OVERHEAD) if embed else 0.0
    kept, dropped = [], []
    for m in models:
        s = size_of(m) * (1 + KV_OVERHEAD)
        if budget_bytes and s and used + s > budget_bytes:
            dropped.append({"model": m, "why": "does not fit: %.1f GB over a %.1f GB budget"
                            % ((used + s) / 1e9, budget_bytes / 1e9)})
            continue
        used += s
        kept.append(m)
    return kept, dropped


# ── the plan ─────────────────────────────────────────────────────────────────
def window_for(model: str, cls: str, cfg: Dict[str, Any], gpu_window: int = 0) -> int:
    for k, v in (cfg.get("num_ctx") or {}).items():
        if same_model(k, model):
            return int(v)
    if cls == "gpu":
        return int(gpu_window or 0)          # the router's own stable GPU window
    return int(cfg.get("cpu_num_ctx") or 0)


def plan(instances: Dict[str, Dict[str, Any]], cfg: Dict[str, Any],
         rules: Dict[str, Dict[str, Any]], aliases: Dict[str, str],
         states: Dict[str, Dict[str, Any]], sizes_by_node: Dict[str, Dict[str, int]],
         ram_by_node: Dict[str, float], gpu_windows: Optional[Dict[str, int]] = None,
         gpu_slots: int = 1, cpu_slots: int = 2) -> Dict[str, Dict[str, Any]]:
    """Per online+enabled node: {class, slots, models:[{model, num_ctx}], embed,
    source, scenarios, dropped}."""
    out: Dict[str, Dict[str, Any]] = {}
    for iid, inst in sorted((instances or {}).items()):
        if inst.get("status") not in ("online", None, "unknown") or not inst.get("enabled", True):
            continue
        cls = node_class(iid, inst, instances)
        ncfg = (cfg.get("nodes") or {}).get(iid) or {}
        slots = slots_for(cls, ncfg.get("slots"), gpu_slots, cpu_slots)
        base, src = baseline_for(iid, cls, cfg, rules, aliases, slots)
        reserve = [resolve_alias(m, aliases) for m in (ncfg.get("reserve") or [])]
        models, took = apply_scenarios(base, cls, slots, cfg.get("scenarios") or [], states,
                                       aliases, reserve)
        served = inst.get("models") or []
        missing = [m for m in models if served and not any(same_model(m, s) for s in served)]
        models = [m for m in models if m not in missing]
        embed = "" if cls == "gpu" else embed_for(iid, cls, cfg, aliases)
        if embed and served and not any(same_model(embed, s) for s in served):
            missing.append(embed)
            embed = ""
        ram = float(ncfg.get("ram_gb") or ram_by_node.get(iid) or 0)
        budget = ram * 1e9 * MEM_SHARE if ram else 0
        models, dropped = fit_memory(models, sizes_by_node.get(iid) or {}, budget, embed)
        dropped += [{"model": m, "why": "not on this node"} for m in missing]
        gw = (gpu_windows or {}).get(iid, 0)
        out[iid] = {"class": cls, "slots": slots, "source": src, "scenarios": took,
                    "models": [{"model": m, "num_ctx": window_for(m, cls, cfg, gw)} for m in models],
                    "embed": embed, "dropped": dropped}
    return out


def planned_pairs(p: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """{node: {model: num_ctx}} - what the request path consults."""
    return {iid: {m["model"]: int(m.get("num_ctx") or 0) for m in (n.get("models") or [])}
            for iid, n in (p or {}).items()}


def request_overrides(planned: Dict[str, Dict[str, int]], iid: str, model: str,
                      want_ctx: int, node_cap: int = 0) -> Dict[str, Any]:
    """For one request the router already placed: keep a planned model resident
    and on its planned runner. Returns {} for an unplanned pair, else
    {keep_alive: -1, num_ctx: W} where W is the planned window when the request
    fits in it (so no reload), else the request's own (a bigger prompt has to
    reload - nothing to save). Never above the node-safe cap."""
    row = (planned or {}).get(iid) or {}
    w = 0
    hit = False
    for m, ctx in row.items():
        if same_model(m, model):
            hit, w = True, int(ctx or 0)
            break
    if not hit:
        return {}
    out: Dict[str, Any] = {"keep_alive": KEEP_FOREVER}
    if w and want_ctx <= w and (not node_cap or w <= node_cap):
        out["num_ctx"] = w
    return out


def actions(p: Dict[str, Dict[str, Any]], running: Dict[str, List[Dict[str, Any]]],
            busy: Iterable[str], now: float, embed_windows: Optional[Dict[str, int]] = None
            ) -> List[Dict[str, Any]]:
    """What to do this tick. `running[node]` = /api/ps rows {name, expires_at_s
    (epoch seconds), context_length}; a node absent from it is skipped. At most ONE load per node per tick (loads
    are heavy); nothing on a busy node. A resident model that is not planned is
    released only when it is one of OURS (a forever expiry) - a model someone
    else loaded is left to its own keep_alive."""
    busy = set(busy or ())
    out: List[Dict[str, Any]] = []
    # only nodes whose residency was read: a node missing from `running` is
    # one /api/ps did not answer for, and "unknown" is not "empty"
    for iid in sorted(running or {}):
        node = (p or {}).get(iid)
        rows = (running or {}).get(iid) or []
        if iid in busy:
            continue
        want = [dict(m) for m in (node or {}).get("models") or []]
        if node and node.get("embed"):
            want.append({"model": node["embed"], "num_ctx": int((embed_windows or {}).get(iid, 0)),
                         "embed": True})
        loaded = False
        for w in want:
            r = next((x for x in rows if same_model(x.get("name"), w["model"])), None)
            if r is None:
                if not loaded:
                    out.append({"node": iid, "action": "load", **w})
                    loaded = True
                continue
            left = float(r.get("expires_at_s") or 0) - now
            if 0 < left < REARM_WITHIN_S:
                out.append({"node": iid, "action": "rearm", **w,
                            "num_ctx": int(r.get("context_length") or w.get("num_ctx") or 0)})
        if node is None:
            continue
        keep = [w["model"] for w in want]
        for r in rows:
            name = r.get("name")
            if any(same_model(name, k) for k in keep):
                continue
            if float(r.get("expires_at_s") or 0) - now > FOREVER_AFTER_S:
                out.append({"node": iid, "action": "release", "model": name})
    return out


def spill_ok(tps: float, min_tps: float, scenario_active: bool) -> bool:
    """May a GPU-preferring call take a CPU node that has its model warm?"""
    if scenario_active:
        return True
    return bool(tps) and float(tps) >= float(min_tps or 0)


# ── residency input ──────────────────────────────────────────────────────────
#: A node the router used this recently is left alone. Only the GPU needs it:
#: it holds ONE model, so a load there evicts what a real caller just loaded,
#: and the warmer puts the baseline back only after the card has gone quiet.
#: A CPU node's planned set fits its model slots and memory, so a load evicts
#: nothing - and with embeddings arriving every few seconds a CPU node was
#: never quiet for 2 min, so nothing was ever loaded (prod, 2026-09-28). A CPU
#: node waits only for a generation in flight.
QUIET_S = {"gpu": 600, "cpu": 0, "cpu_sibling": 0}


def parse_expiry(s: Any) -> float:
    """Epoch seconds from an /api/ps `expires_at` ("2319-01-08T18:18:30.1234
    56789Z" or with a +hh:mm offset - Ollama prints nanoseconds, which
    datetime.fromisoformat refuses before 3.11). 0.0 when unparseable."""
    import calendar
    import re
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$",
                 str(s or "").strip())
    if not m:
        return 0.0
    y, mo, d, h, mi, se = (int(x) for x in m.groups()[:6])
    try:
        t = float(calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0)))
    except (OverflowError, ValueError):
        return 0.0
    tz = m.group(7) or "Z"
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        t -= sign * (hh * 3600 + mm * 60)
    return t


def ps_rows(ps: Dict[str, Any]) -> List[Dict[str, Any]]:
    """/api/ps -> [{name, expires_at_s, context_length, size, digest}]."""
    out = []
    for m in (ps or {}).get("models") or []:
        out.append({"name": m.get("name") or m.get("model") or "",
                    "expires_at_s": parse_expiry(m.get("expires_at")),
                    "context_length": int(m.get("context_length") or 0),
                    "size": int(m.get("size") or 0),
                    "digest": str(m.get("digest") or "")})
    return out


# ── one model, several tags ──────────────────────────────────────────────────
# `jaahas/qwen3.5-uncensored:latest` and `:9b` are the SAME weights (one digest,
# measured on gpu-250 2026-09-28) and Vera uses both names. Compared by name, a
# resident `:9b` read as "the planned model is not loaded" and the warmer would
# load the same weights again under the other tag.
def digest_of(model: str, digests: Dict[str, str]) -> str:
    for name, d in (digests or {}).items():
        if same_model(name, model):
            return str(d or "")
    return ""


def tags_of(model: str, digests: Dict[str, str]) -> List[str]:
    """Every tag on the node carrying the same weights as `model` (itself first)."""
    d = digest_of(model, digests)
    out = [model]
    if d:
        out += [n for n, x in sorted((digests or {}).items()) if x == d and not same_model(n, model)]
    return out


def canonical_rows(rows: List[Dict[str, Any]], planned: Iterable[str],
                   digests: Dict[str, str]) -> List[Dict[str, Any]]:
    """Resident rows with a planned model's weights renamed to the planned
    name, so residency and actions compare the same thing."""
    by_digest = {digest_of(m, digests): m for m in planned if digest_of(m, digests)}
    out = []
    for r in rows or []:
        d = str(r.get("digest") or "") or digest_of(r.get("name", ""), digests)
        if d in by_digest and not same_model(r.get("name"), by_digest[d]):
            r = dict(r, name=by_digest[d], tag=r.get("name"))
        out.append(r)
    return out


def planned_pairs_with_tags(p: Dict[str, Dict[str, Any]],
                            digests_by_node: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, int]]:
    """planned_pairs, with every other tag of a planned model's weights too -
    a call naming `:9b` keeps the runner a plan for `:latest` spawned."""
    out: Dict[str, Dict[str, int]] = {}
    for iid, row in planned_pairs(p).items():
        dg = (digests_by_node or {}).get(iid) or {}
        out[iid] = {}
        for m, ctx in row.items():
            for t in tags_of(m, dg):
                out[iid].setdefault(t, ctx)
    return out


def busy_nodes(instances: Dict[str, Dict[str, Any]], last_picked: Dict[str, float],
               now: float, quiet_s: Optional[Dict[str, int]] = None) -> Dict[str, str]:
    """{node: why} for nodes the warmer must not touch this tick."""
    q = quiet_s or QUIET_S
    out: Dict[str, str] = {}
    for iid, inst in (instances or {}).items():
        if int(inst.get("in_use") or 0) > 0:
            out[iid] = "a call is in flight"
            continue
        cls = node_class(iid, inst, instances)
        ago = now - float((last_picked or {}).get(iid) or 0)
        if ago < float(q.get(cls, 120)):
            out[iid] = "used %d s ago (waits %d s of quiet)" % (int(ago), int(q.get(cls, 120)))
    return out
