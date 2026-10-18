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
    _f("decide.site_capture", ("off", "on"), "matrix", "FM-2/FM-12",
       "Record every short-answer LLM call (<= 40 chars, one line) with its call site, prompt and answer in a capped "
       "Redis stream (20k), so each call site that behaves like a decision becomes an application of the candidate "
       "matrix (decide.sites.report: which small model agrees with it). Never chat turns, never the shadow judges' "
       "own calls. 'off' (default) = nothing recorded; 'on' = recorded.", census=False),
    _f("chat.replay_capture", ("off", "on"), "chat", "FM-0.3",
       "Keep the exact request body of each chat turn (message, history, the browser-built system prefix and cap "
       "hint) in a private Postgres store (chat_replay, credentials redacted, 30 days), so real turns can be replayed "
       "faithfully. A case goes into the chat corpus only after it is approved one by one (chat.replay.approve). "
       "'off' (default) = nothing kept; 'on' = every chat turn kept (corpus-run sessions excepted). Owner: yes, "
       "17 Oct (§13 Q3).", census=False),
    _f("decide.student.step_verify", ("off", "shadow", "first", "first_met", "first_not_met"), "loop", "FM-2.5",
       "A pre-trained student (VERA_DECIDE_STUDENT_MODEL, default qwen3:1.7b in a second warm slot on gpu-250; one "
       "token over the live verifier's own evidence and rules) on every step "
       "verdict the big model would give. 'off' (default) = the big model only; 'shadow' = the student runs beside "
       "it and is recorded as a shadow; 'first' = the student answers first and its verdict stands when it is an "
       "allowed label (VERA_DECIDE_STUDENT_VERIFY_LABELS, default met) at confidence >= "
       "VERA_DECIDE_STUDENT_TAU (default 0.95), else the big model decides; 'first_met' / 'first_not_met' = "
       "the same with only that label allowed (the owner, 2026-10-10: run both as census arms and measure the "
       "difference - the plan's FM-2.5 says not_met only; met skips the big call on the common pass). Under "
       "any first mode a random VERA_DECIDE_STUDENT_COSIGN share (default 0.1) of the answers that would stand "
       "go to the big model instead, the student recorded beside it (detail.would_take) - the plan's 'sampled' "
       "co-sign, the precision of the student's takes. Deterministic verdicts (parsed files, test runs, missing "
       "files) never reach either."),
    _f("decide.student.controller_action", ("off", "shadow", "first"), "loop", "FM-2.5",
       "A pre-trained student (VERA_DECIDE_STUDENT_MODEL, default qwen3:1.7b; one letter over the live controller's "
       "own ledger, prompt and rules) on "
       "every next-move decision after a step. 'off' (default) = the big model only; 'shadow' = the student runs "
       "beside it and is recorded as a shadow; 'first' = the student answers first and its move stands when it is "
       "an allowed label (VERA_DECIDE_STUDENT_CONTROLLER_LABELS, default continue) at confidence >= "
       "VERA_DECIDE_STUDENT_TAU (default 0.95), else the big model decides. A student's continue carries no "
       "findings or steer for the next step - turn 'first' on only when shadow shows that costs nothing."),
    # Every other loop question at once (owner, 17 Oct: "set up all the evaluations and training in parallel"):
    # which student answers - the kNN judge, the in-slot LLM, a bound trained head - and from what confidence comes
    # from the cascade plan (decide.cascade.plan: the lowest threshold where its precision on outcomes is at least
    # the live model's accuracy). The loop asks through the same student hook; compute-roles wires each site.
    *[_f(f"decide.student.{q.split('.', 1)[1]}", ("off", "shadow", "first"), "loop", "FM-2.5",
         f"The System 1 student on {q} (four-minds FM-2.5, the cascade plan). 'off' (default) = the big model only; "
         "'shadow' = the plan's student answers beside it, recorded as a shadow; 'first' = the student answers "
         "first and its answer stands at the plan's confidence threshold, else the big model decides. With no "
         "eligible student in the plan, 'first' behaves as 'shadow'.")
      for q in ("loop.step_done", "loop.prestep_needed", "loop.intent", "loop.tier")],
    _f("discovery.recall_index", ("off", "pg"), "discovery", "FM-7.1",
       "Where discover_recall (the fabric's own pages beside every web.search) finds pages for a query. 'off' "
       "(default) = SQLite LIKE per word, newest first; 'pg' = the Postgres page search index (weighted tsvector, "
       "ranked; synced from the SQLite pages every 2 minutes), falling back to LIKE when the index cannot answer. "
       "Owner 2026-10-11: Postgres, not SQLite FTS5."),
    _f("memory.seek_cache", ("off", "on"), "memory", "FM-7.1",
       "memory.seek's result cache: the same seek (query, scope, since, k, size, memory merge) within 10 minutes "
       "and the same fabric generation - a counter every fabric ingest bumps - answers from Redis. 'off' (default) "
       "= every seek searches; 'on' = cached answers carry cached=true; session-memory hits can lag by up to 10 min."),
    _f("plan.goal_frame", ("off", "on", "writer"), "loop", "FM-1.3",
       "Goal constraints (compute-roles B2): dates, versions and numeric limits in the goal text "
       "(\"since March 2024\", \"Python 3.12\", \"under 500 words\", \"top 5\"). 'off' (default) = none; "
       "'on' = they become done_when items and the final gate's must-cover list (measured 12 Oct on dated goals: "
       "HURTS - self-verification and a research loop that never converged); 'writer' (R-B2) = they ride on every "
       "code.author / prose.author task as instructions, and coverage of the delivered answer is recorded as a "
       "shadow signal, done_when and the gate untouched. Census templates carry none - measure on dated goals."),
    _f("plan.tier_lower", ("off", "on"), "loop", "FM-4.3",
       "Tier lowering: the deterministic tier floor no longer calls a goal 'complex' only because it "
       "has three or more conjunctions (no complex hint, under 60 words) - it falls to the length rules "
       "(simple under 40 words). The LLM pre-pass can still raise it. 'off' (default) = the old floor; "
       "'on' = lowered. Measured 11 Oct: 73% of complex-tier census runs executed <= 2 steps."),
    _f("memory.rerank", ("off", "on", "not_chat"), "memory", "FM-7.3",
       "memory.seek's head reranked by the cross-encoder: the top 30 fused candidates re-read in one nlp.rerank call "
       "on a node (bounded at 1.5 s), their order decided by it, still above the rest. 'off' (default) = fused order; "
       "'on' = reranked everywhere; 'not_chat' = reranked everywhere but a live chat turn's own seek (research, "
       "discovery, loops, background - owner 12 Oct). Measured 11 Oct (known-item pg1): nDCG@10 +11.6 pts, p50 +0.8 s."),
    _f("memory.rank", ("off", "on"), "memory", "FM-7.2",
       "How memory.seek ages what it found: 'off' (default) = one mild recency boost for every record (+25% at most, "
       "45-day e-folding); 'on' = rank_core - relevance x a half-life per dataset class (news 3 d, chat 14 d, research "
       "60 d, telemetry 1 d, docs and facts none, others 30 d; floor 0.2) x importance, and the question's words set the "
       "temporal intent ('latest' halves the half-life, 'originally' turns decay off)."),
    _f("sense.chat_pre", ("off", "on"), "chat", "FM-1.2",
       "The chat PRE frame: sense.frame (patterns, the MiniLM decision vector on a node, domain cards) for every chat "
       "message, started when the turn arrives and never awaited before the model - a 'sense / frame' phase records "
       "its domain and when it landed, so the turn record shows how often it is ready before the first token. "
       "'off' (default) = no frame; 'on' = framed. Exit: TTFT p95 unchanged (it waits for nothing)."),
    _f("tools.shortlist.chat", ("legacy", "shadow", "on"), "chat", "FM-3.3",
       "Which tools a chat turn is offered. 'legacy' (default) = the page's own word scoring of the agent's own list "
       "only; 'shadow' = also caps.shortlist on the user's words, started with the turn and never awaited, recorded "
       "after the reply beside the page's list and what the reply used (caps.shortlist.chat_report: recall@16/24/28); "
       "'on' (owner, 17 Oct) = the page asks caps.chat_shortlist before the turn and offers the model the most fitting "
       "SAFE capabilities from the whole catalogue (read / network / model effects, no approval; never shells, "
       "secrets, deploys or system controls) beside the agent's own list - recorded the same way."),
    _f("memory.text_index", ("like", "fts"), "memory", "FM-7.1",
       "memory.seek's keyword leg. 'like' (default) = text ILIKE per word over fabric_records (a sequential scan that "
       "keeps the first rows matching any word, p95 2.2 s on prod 11 Oct); 'fts' = a GIN full-text index "
       "(fabric_records_fts, built once in the background): records with every word ranked first, then any-word "
       "records; falls back to 'like' while the index is missing."),
    _f("canvas.surface_rank", ("off", "shadow"), "chat", "FM-6.2/6.3",
       "The canvas curator's first questions: which domains a turn is about and which surfaces answer it, from the "
       "turn's Frame vector against domain and surface cards (cosine), beside surface.find's word lists. 'off' "
       "(default) = word lists only; 'shadow' = each turn.prepare also records what the cards would have chosen "
       "(background, never awaited; canvas.surface_rank.report). 'on' needs engagement outcomes first."),
    # intel's modes (FM-13.3; mapping agreed with the intelligence unit 11 Oct, intel_switches_core). intel's config
    # stays the truth: a switch overrides its key only when set; at its default (the first mode) it changes nothing.
    # the web map (web-ingress WI-3): every fetch through web_client / http.get leaves a receipt in Postgres
    _f("ingress.recording", ("off", "meta", "on"), "web", "WI-3",
       "Whether a fetch leaves a receipt in the web map. 'off' (default) = nothing written; 'meta' = hosts, URLs "
       "and fetches (status, bytes, caller), never a body; 'on' = plus content hashes and links for external pages "
       "(the owner's 11 Oct decision: external pages in full, internal hosts never, private APIs meta only).",
       owner="web-ingress"),
    _f("ingress.shared_throttle", ("off", "on"), "web", "WI-3",
       "The per-host politeness slot web_client keeps: 'off' (default) = per process, as today; 'on' = one record "
       "per host in the coordination Redis, so prod, the sandboxes and node fetchers together never exceed one "
       "request per interval on a host (two Redis round trips per fetch; Redis down = per process).",
       owner="web-ingress"),
    _f("ingress.dedupe", ("off", "on"), "web", "WI-4",
       "Whether a page ingest asks the web map first: 'off' (default) = every fetch ingests again (11 Oct scan: 40 % "
       "of URL records are repeats, 14,750 with identical text); 'on' = the same record with the same content hash is "
       "skipped, and content already held under another record is linked to it instead of re-embedded.",
       owner="web-ingress"),
    _f("discovery.defer_enrichment", ("off", "on"), "web", "WI-4",
       "The crawl's second-order pass (entities, LLM tags): 'off' (default) = in tandem with the crawl, which waits "
       "for it (8 Wikipedia pages: 299 s vs 28 s); 'on' = queued as one discovery.t3 idle-queue job per crawl and "
       "run when the box is quiet (or in intel's reserved window). Interactive callers pass want_tiers=('t3',) to "
       "keep it inline.", owner="web-ingress"),
    _f("ingress.novelty", ("off", "on"), "web", "WI-5",
       "Novelty on every receipt with content: 'off' (default) = not scored; 'on' = exact (hash) and near-duplicate "
       "(simhash within 3 bits via the LSH bands) against web_content, written to web_urls.novelty and the host's "
       "novel-yield EWMA - two indexed reads per batch, off the request path. The embedding step stays in shadow "
       "(four-minds §12.5).", owner="web-ingress"),
    _f("discovery.read_files", ("off", "on"), "web", "WI-4b",
       "What a crawl does with a file it fetched (PDF, Office, EPUB, archives, catalogues): 'off' (default) = drops it "
       "and queues the parent directory, as before; 'on' = reads it with the ingress readers, ingests it as a 'file' "
       "record (text, tables, archive members, catalogue datasets), indexes it in fabric_pages, and follows the URLs "
       "it points at (catalogue distributions, sitemap and feed entries). Files over the reader's byte cap are "
       "recorded by metadata only.", owner="web-ingress"),
    _f("web.searxng.self_heal", ("off", "on"), "web", "web-ingress §16",
       "SearXNG self-healing: 'off' (default) = a probe every 5 minutes when nothing searches, reported in /health and "
       "web.searxng.heal.status; 'on' = also restart the container on the dead-network signature (every engine times "
       "out, or SearXNG unreachable - not on CAPTCHA / rate limits), at most once per 30 min and 4 a day, and tell the "
       "owner on Telegram when two restarts in a row did not bring it back.", owner="web-ingress"),
    _f("discovery.novelty_frontier", ("off", "on"), "discovery", "WI-8b / FM-7.5",
       "The crawl frontier and novelty: 'off' (default) = links ranked by topic, git paths, domain richness and topic "
       "authority, as before; 'on' = plus each host's novel yield from the web map (hosts whose pages keep coming "
       "back known go down, hosts that keep yielding new content go up, about +-0.15), and the top 2 links queued per "
       "page are recorded as discovery.collect_worth decisions with what their score was made of.",
       owner="web-ingress"),
    _f("web.search.prefetch", ("off", "on"), "web", "WI-13",
       "After a web.search, read its top 3 results into the reader cache in the background, one at a time (a URL "
       "already cached costs nothing): the fetch or read that usually follows a search is then instant. 'off' "
       "(default) = no prefetch.", owner="web-ingress"),
    _f("discovery.media_t3", ("off", "on"), "discovery", "WI-7b",
       "Media files a crawl reads (images, audio): 'off' (default) = recorded as files with no text; 'on' = each "
       "queues a background job that describes the image (vision.describe) or transcribes the audio (stt.transcribe, "
       "25 MB cap) and puts the text on its record. Both models run on the GPU node, in background windows only.",
       owner="web-ingress"),
    _f("ingress.semantic_novelty", ("off", "shadow"), "fabric", "WI-5b",
       "Semantic novelty of crawled pages: 'off' (default) = not measured; 'shadow' = after each page is stored, its own "
       "vector is read back from Chroma and compared with its nearest records (no where filter); 1 - the highest cosine "
       "is recorded on the web map (web_urls.semantic_novelty, the closest record as semantic_twin). Acts on nothing.",
       owner="web-ingress"),
    _f("intel.nlp_triage", ("on", "off"), "intel", "FM-13.3",
       "intel's triage on the NLP tier (config 'nlp'): 'on' = nlp.embed novelty, nlp.rerank relevance, entities; "
       "'off' = keyword/heuristic triage, no nlp.* calls.", owner="intelligence"),
    _f("intel.brief", ("llm", "extractive"), "intel", "FM-13.3",
       "The night brief: 'llm' = the MoE writes the cross-topic top line and follow-ups (build_report "
       "synthesize=True); 'extractive' = per-topic assessments only, no cross-topic pass. Not a no-LLM mode "
       "(snapshot assessments still use the assess role).", owner="intelligence"),
    _f("intel.auto_research", ("off", "on"), "intel", "FM-13.3",
       "Whether a deep topic's new questions start research by themselves (config 'auto_research').",
       owner="intelligence"),
    _f("intel.material_alerts", ("on", "off"), "intel", "FM-13.3",
       "Whether a material change is sent as an alert (config 'alerts'); intel.material_change decisions are "
       "recorded either way.", owner="intelligence"),
    _f("intel.calibrated_gate", ("shadow", "on", "off"), "intel", "FM-13.3",
       "intel's relevance gate: 'shadow' (today) = the score threshold decides, a topic's learned curve records "
       "p_shadow; 'on' = the curve decides once it is usable (ECE <= 0.05, AUC >= 0.6, 30+ ratings); 'off' = no "
       "curve at all.", owner="intelligence"),
    _f("memory.seek_exclude_self", ("off", "on"), "memory", "FM-7.1",
       "Whether an unscoped memory.seek leaves Vera's own telemetry out (chat.messages, ide.*, caps.*, bus.*, ts.*, "
       "streams.*, mesh.*, dream.reports - the worldview corpus' SELF_TELEMETRY list). 'off' (default) = searched like "
       "any data; 'on' = left out unless the caller scopes to one of them. Found 11 Oct: a CVE-id seek put chat logs "
       "and IDE sessions first."),
    _f("plan.precedent", ("off", "on"), "loop", "FM-4.2",
       "Precedent for the planner: up to 2 past runs of similar goals that went well, and 1 warning from one that "
       "did not (plan.precedent, searched on an NLP node), added to what the planner reads as reference with the "
       "objective restated after it; census runs never get census precedents of the same goal. 'off' (default) = "
       "no precedent; 'on' = the block when one is found (bounded at 4 s; never blocks the run)."),
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
