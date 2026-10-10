"""The switchboard: one registry of switchable behaviours, and how a run resolves them.

Every behaviour the four-minds programme adds (decision students, Sense, rankers,
gates, curator...) ships OFF and is a named switch here, so it can be turned on per
run, per chat session, per census arm or globally, and so every run can say exactly
which behaviours were on. Before this, loop features were individual engine kwargs,
census arms existed for two axes only, and chat features were scattered browser keys.

Resolution, highest wins:
    call (minds= on a run or chat request) > arm (a census schedule's target.minds)
    > session (chat settings) > global (Redis vera:minds:switchboard) > registry default

call and arm are validated and fail closed (unknown ids or modes refuse the run before
it starts). session and global are read leniently: an entry the registry no longer
knows is ignored and reported, so a stale global switch can never stop every run.

Global kill switch: {"*": "default"} in the global layer puts every behaviour back to
its registry default (the conservative mode) whatever the other global entries say.

Pure: no Redis, no registry, no orchestrator. The run's effective set is published
on RUN_MINDS (a ContextVar) for features to read with mode_of().
"""
from __future__ import annotations

import contextvars
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA = "vera.minds/v1"
KILL = "*"


@dataclass(frozen=True)
class Feature:
    id: str
    modes: Tuple[str, ...]                 # ordered; modes[0] is the default
    surface: str                           # chat|loop|canvas|memory|discovery|background|worldview|research|intel
    package: str                           # roadmap package, e.g. FM-0.0
    owner: str = "four-minds"
    description: str = ""
    census: bool = True                    # may a census arm set it
    security: bool = False                 # a feature that could loosen a deterministic gate - refused

    @property
    def default(self) -> str:
        return self.modes[0]


def _f(id, modes, surface, package, description, owner="four-minds", census=True):
    return Feature(id=id, modes=tuple(modes), surface=surface, package=package,
                   owner=owner, description=description, census=census)


# Each entry is added by the package that builds the behaviour - never ahead of it.
REGISTRY: Dict[str, Feature] = {f.id: f for f in (
    _f("fix.worldview_source_gate", ("on", "off"), "research", "FM-0.0",
       "Research cites the JEPA worldview only when worldview.health is 'ok'. "
       "'on' (default) = gated; 'off' = the old ungated behaviour. A safety gate, "
       "so the documented exception to off-by-default."),
    _f("context.needs_web", ("off", "rules"), "chat", "CU-T.3",
       "Decide from the words of a chat turn whether it needs the web before the model answers (fresh facts, an "
       "explicit 'search for', a market figure) and search then - announced in the throbber, waited on at most 3 s, "
       "never instead of a search the user turned on. 'off' (default) = only the user's Web live toggle searches; "
       "'rules' = the rules decide (turn_needs_core).", owner="context-and-ui"),
    _f("context.needs_memory", ("off", "rules"), "chat", "CU-T.5",
       "Decide from the words of a chat turn whether it refers back ('last time', 'what did we decide', 'you said') "
       "and then search memory deeply before the model answers - every session and agent, the vector leg when it "
       "answers in time - announced in the throbber, waited on at most 1.5 s. 'off' (default) = the agent's own "
       "memory only; 'rules' = the rules decide (turn_needs_core).", owner="context-and-ui"),
    _f("decide.student.step_verify", ("off", "shadow", "first"), "loop", "FM-2.5",
       "A pre-trained student (Qwen3-4B, one token over the live verifier's own evidence and rules) on every step "
       "verdict the big model would give. 'off' (default) = the big model only; 'shadow' = the student runs beside "
       "it and is recorded as a shadow; 'first' = the student answers first and its verdict stands when it is an "
       "allowed label (VERA_DECIDE_STUDENT_VERIFY_LABELS, default met) at confidence >= "
       "VERA_DECIDE_STUDENT_TAU (default 0.95), else the big model decides. Deterministic verdicts (parsed "
       "files, test runs, missing files) never reach either."),
)}


def _check_registry(reg: Mapping[str, Feature]) -> None:
    for k, f in reg.items():
        if k != f.id or not f.modes or len(set(f.modes)) != len(f.modes):
            raise ValueError(f"bad switchboard entry: {k}")
        if f.security:
            raise ValueError(f"switchboard refuses security features: {k}")


_check_registry(REGISTRY)


def canonical(minds: Optional[Mapping[str, str]]) -> str:
    return json.dumps({str(k): str(v) for k, v in sorted((minds or {}).items())},
                      separators=(",", ":"), sort_keys=True)


def digest(minds: Optional[Mapping[str, str]]) -> str:
    """12 hex of sha256 over the canonical requested set; 'default' when nothing is requested."""
    if not minds:
        return "default"
    return hashlib.sha256(canonical(minds).encode("utf-8")).hexdigest()[:12]


def validate(minds: Optional[Mapping[str, Any]], registry: Mapping[str, Feature] = REGISTRY,
             *, census: bool = False) -> Dict[str, Any]:
    """Strict check of a requested set (a run's minds= or a census arm's target.minds)."""
    unknown: List[str] = []
    bad: List[Dict[str, Any]] = []
    not_census: List[str] = []
    if minds is not None and not isinstance(minds, Mapping):
        return {"ok": False, "unknown_ids": [], "bad_modes": [], "not_census": [],
                "error": "minds must be an object of {feature_id: mode}", "digest": "default"}
    for k, v in (minds or {}).items():
        f = registry.get(str(k))
        if f is None:
            unknown.append(str(k))
        elif str(v) not in f.modes:
            bad.append({"id": f.id, "mode": str(v), "allowed": list(f.modes)})
        elif census and not f.census:
            not_census.append(f.id)
    ok = not unknown and not bad and not not_census
    return {"ok": ok, "unknown_ids": unknown, "bad_modes": bad, "not_census": not_census,
            "digest": digest(minds) if ok else "invalid"}


def explain(v: Mapping[str, Any]) -> str:
    parts = []
    if v.get("error"):
        parts.append(str(v["error"]))
    if v.get("unknown_ids"):
        parts.append("unknown ids: " + ", ".join(v["unknown_ids"]))
    for b in v.get("bad_modes") or []:
        parts.append(f"{b['id']}={b['mode']} (allowed: {'|'.join(b['allowed'])})")
    if v.get("not_census"):
        parts.append("not settable by a census arm: " + ", ".join(v["not_census"]))
    return "; ".join(parts) or "ok"


def _lenient(layer: Optional[Mapping[str, Any]], registry: Mapping[str, Feature],
             ignored: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (layer or {}).items():
        if k == KILL:
            continue
        mode = v.get("mode") if isinstance(v, Mapping) else v
        f = registry.get(str(k))
        if f is None or str(mode) not in f.modes:
            ignored.append(f"{k}={mode}")
            continue
        out[f.id] = str(mode)
    return out


def resolve(call: Optional[Mapping[str, str]] = None, arm: Optional[Mapping[str, str]] = None,
            session: Optional[Mapping[str, Any]] = None, global_: Optional[Mapping[str, Any]] = None,
            registry: Mapping[str, Feature] = REGISTRY) -> Dict[str, Any]:
    """Effective mode for every registered feature, plus where each came from.

    call/arm must already have passed validate(); session/global_ are read leniently.
    Returns {"modes": {id: mode}, "source": {id: layer}, "ignored": [...], "killed": bool}.
    """
    ignored: List[str] = []
    killed = bool(global_) and (
        (global_.get(KILL) == "default")
        or (isinstance(global_.get(KILL), Mapping) and global_[KILL].get("mode") == "default"))
    layers = [("call", dict(call or {})), ("arm", dict(arm or {})),
              ("session", _lenient(session, registry, ignored)),
              ("global", {} if killed else _lenient(global_, registry, ignored))]
    modes: Dict[str, str] = {}
    source: Dict[str, str] = {}
    for fid, f in registry.items():
        modes[fid], source[fid] = f.default, "default"
        for name, layer in layers:
            if fid in layer:
                modes[fid], source[fid] = layer[fid], name
                break
    return {"modes": modes, "source": source, "ignored": ignored, "killed": killed}


# The current run's effective modes. Set once at the start of every run (so a value
# can never leak from one run into the next in the same task); read by features.
RUN_MINDS: contextvars.ContextVar = contextvars.ContextVar("vera_run_minds", default=None)


def mode_of(feature_id: str, effective: Optional[Mapping[str, Any]] = None,
            registry: Mapping[str, Feature] = REGISTRY) -> str:
    """The mode a feature should use now: the given effective set, else this run's, else the default."""
    f = registry.get(feature_id)
    if f is None:
        raise KeyError(f"unregistered switch: {feature_id}")
    eff = effective if effective is not None else RUN_MINDS.get()
    modes = (eff or {}).get("modes", eff or {}) if isinstance(eff, Mapping) else {}
    m = modes.get(feature_id)
    return m if m in f.modes else f.default


def listing(registry: Mapping[str, Feature] = REGISTRY) -> List[Dict[str, Any]]:
    return [{"id": f.id, "modes": list(f.modes), "default": f.default, "surface": f.surface,
             "package": f.package, "owner": f.owner, "census": f.census,
             "description": f.description} for f in registry.values()]
