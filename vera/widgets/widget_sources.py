# -*- coding: utf-8 -*-
"""
The widget SOURCE registry (UI redesign, Notes/42 defect 57 - "widgets need a
comprehensive list of sources"): every read-shaped capability the live
registry holds, as a source a widget record can name - with the SHAPE a
widget reads it as, the domain it belongs to, its arguments, a refresh floor
and a one-line description.

Where a shape comes from, in order:
  hand      the well-known reads in widget_catalog.HAND (an override, always wins)
  measured  the MEASURED table below - the envelope every cheap, argument-free
            read answered when the mirror was probed (2026-09-14), reduced to the
            shape and the container field the rows sit in (the read.map the sheet
            suggests); the same reduction runs live when widget.sources is asked
            to probe (probe=True: argument-free reads, a few seconds each, never
            a capability whose name says it writes)
  declared  the capability's own name and description (the tail word, the
            group prefix, the words of the description), the weakest tier

Read-shaped means: the name's tail is a reading word (get, list, status,
history, ... ), or the group is one that only reads (obs, sysmon, perf, ...),
and no word of the name says it writes (write, delete, create, run, add, ack,
...). The registry is derived once and cached for ten minutes; refresh=True
re-derives; a capability registered later is in the next derivation.

The redis.* read family lives here too (read-only: info, keys, one key by its
type, a stream's tail) - the sandboxes' own Redis and prod's are both a source
the same way.

Loaded beside widget_catalog.py (no package): pure functions over a registry
dict, plus the capabilities that read Redis.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability  # noqa: F401

# ── what reads and what writes ────────────────────────────────────────────────
READ_TAIL = ("get", "list", "status", "load", "history", "read", "stats", "metrics", "recent", "tail", "search", "find", "show",
             "info", "summary", "query", "health", "state", "series", "events", "nodes", "jobs", "runs", "snapshot", "top",
             "instances", "sources", "request_log", "inspect", "overview", "catalog", "diag", "diagnostics", "provenance",
             "hosts", "guests", "devices", "containers", "keys", "forms", "templates", "layouts", "resident", "models",
             "temps", "pending", "cluster", "workers", "scheduler", "board", "detail", "activity", "feed", "log", "ps",
             "symbols", "quotes", "bars", "candles", "watchlist", "alerts", "positions", "orders", "topology", "topics",
             "peers", "routes", "tags", "graphs", "datasets", "records", "entities", "profiles", "config", "settings",
             "usage", "capacity", "digest", "news", "calendar", "agenda", "schedule", "count", "size", "total", "rate",
             "throughput", "tok_s", "trend", "timeseries", "all", "map", "tree", "ports", "sessions", "channels")
# the groups that ONLY read (a group with writes in it - mesh, markets, proxmox, nodes - is admitted word by word instead)
READ_GROUPS = ("obs", "sysmon", "perf", "topology", "metrics", "netmon", "capacity", "syslog", "godseye", "netgraph", "redis")
READ_PREFIX = ("docker.ps", "docker.stats", "git.log", "ollama.instances", "ollama.request_log", "ollama.list_models")
NEVER = ("write", "delete", "remove", "create", "run", "exec", "kill", "restart", "stop", "start", "set", "save", "send",
         "post", "push", "upsert", "generate", "author", "edit", "promote", "spawn", "provision", "ota", "cancel", "approve",
         "dismiss", "clear", "ingest", "migrate", "register", "unregister", "pull", "install", "deploy", "apply", "toggle",
         "commit", "reset", "prune", "reap", "pause", "resume", "instantiate", "claim", "release", "seed", "sync", "link",
         "attach", "detach", "enroll", "connect", "disconnect", "login", "logout", "rotate", "ensure", "up", "down", "build",
         "train", "export", "import", "update", "rename", "move", "copy", "patch", "fix", "improve", "plan", "dispatch",
         "trigger", "emit", "notify", "ask", "reply", "chat", "complete", "invoke", "call", "open", "close", "mount",
         "unmount", "scan", "crawl", "fetch", "research", "browse", "navigate", "click", "type", "screenshot", "record",
         "transcribe", "speak", "tts", "stt", "render", "diffuse", "imagine", "draw", "compose", "summarize", "summarise",
         "translate", "rerank", "embed", "index", "reindex", "vacuum", "compact", "flush", "purge", "evict", "expire",
         "lock", "unlock", "grant", "revoke", "ban", "kick", "add", "put", "new", "insert", "submit", "enqueue", "adopt",
         "assign", "ack", "acknowledge", "forget", "drop", "truncate", "wipe", "delete_all", "restore", "rollback",
         # active or costly even when the name reads quietly: a merge, a ping that marks, a probe, a survey, LLM work, trading
         "merge", "ping", "text", "dump", "survey", "mark", "dedupe", "print", "probe", "identify", "detect", "analyze",
         "analyse", "test", "tick", "order", "revert", "reboot", "repair", "refresh", "accept", "sniff", "mark_merged")
NEVER_PREFIX = ("mcp.", "cap.contract", "evolve.sandbox", "evolve.editq", "evolve.unittest", "evolve.suite", "dag.run",
                "dag.plan", "wf.", "workflow.", "ui.script", "exec.", "code.", "ide.exec", "sandbox.session", "widget.template",
                "widget.instance", "widget.layout")
_REFRESH_FLOOR = {"level": "5s", "series": "5s", "values": "10s", "events": "10s", "items": "30s", "graph": "30s",
                  "stages": "5s", "rate": "5s", "parts": "30s", "ohlcv": "1m", "matrix": "30s", "calendar": "1m",
                  "string": "30s", "points": "30s"}
# how a name's tail says what it returns (the widest net; a measured shape beats it)
_NAME_SHAPE = (
    (("history", "series", "timeseries", "trend"), "series"),
    (("events", "log", "tail", "feed", "stream", "activity", "request_log", "alerts", "news", "digest"), "events"),
    (("graph", "snapshot", "topology", "edges", "nodes", "topics", "peers", "routes", "map", "tree"), "graph"),
    (("list", "recent", "search", "find", "instances", "templates", "top", "all", "board", "hosts", "guests", "devices",
      "containers", "keys", "forms", "layouts", "resident", "models", "workers", "scheduler", "sources", "symbols",
      "quotes", "watchlist", "positions", "orders", "records", "datasets", "entities", "profiles", "tags", "graphs",
      "sessions", "channels", "ports", "ps", "modules"), "items"),
    (("stats", "status", "health", "metrics", "summary", "state", "usage", "info", "diag", "diagnostics", "overview",
      "config", "settings", "provenance", "catalog", "temps", "detail", "inspect", "cluster", "get", "show"), "values"),
    (("count", "pending", "depth", "size", "total", "capacity"), "level"),
    (("rate", "throughput", "tok_s"), "rate"),
    (("ohlcv", "bars", "candles"), "ohlcv"),
    (("calendar", "agenda", "schedule"), "calendar"),
)
# the domain a group belongs to, by its first dotted segment
DOMAINS: Dict[str, str] = {
    "obs": "Observability", "sysmon": "Stack monitor", "perf": "Performance", "metrics": "Metrics", "capacity": "Capacity",
    "nodes": "Nodes", "cluster": "Cluster", "estate": "Estate", "proxmox": "Proxmox", "docker": "Docker", "portainer": "Docker",
    "pxstore": "Storage", "backup": "Storage", "vfs": "Files", "fs": "Files", "workspace": "Files", "notes": "Notes",
    "ollama": "Ollama", "vllm": "Inference", "gpu": "GPU", "providers": "Providers", "catalog": "Model catalogue", "models": "Models",
    "mesh": "Mesh", "netmon": "Network", "netgraph": "Network", "netsec": "Network", "conn": "Connections", "ssh": "Connections",
    "markets": "Markets", "business": "Business ops", "acct": "Business ops", "cal": "Calendar", "sched": "Scheduler", "mail": "Mail", "tg": "Telegram",
    "fabric": "Fabric", "memory": "Memory", "context": "Context", "ontologies": "Ontologies", "cap_ontology": "Ontologies", "collector": "Collectors",
    "evolve": "Loop Lab", "loops": "Loops", "workshop": "Loops", "dag": "DAGs", "jobs": "Jobs", "background": "Jobs", "bench": "Benchmarks", "eval": "Evaluation", "census": "Census",
    "dream": "Dream", "agent": "Agents", "agentbridge": "Agents", "autonomous": "Agents", "character": "Agents", "goals": "Goals",
    "ide": "IDE", "sandbox": "Sandboxes", "canvas": "Canvas", "widget": "Widgets", "ui": "UI", "lhm": "UI", "panel": "UI", "pwa": "UI", "gallery": "Media",
    "media": "Media", "images": "Media", "imagery": "Media", "vision": "Media", "cctv": "Media", "spritegen": "Media", "podcast": "Media", "print": "Print",
    "identity": "Identity", "secrets": "Security", "secstore": "Security", "secprov": "Security", "pki": "Security", "certs": "Security", "prov": "Security",
    "ha": "Home", "integration": "Integrations", "n8n": "Integrations", "operator": "Operator", "web": "Web", "syscomms": "Comms", "syslog": "Logs",
    "foundry": "Foundry", "ml": "ML", "nlp": "NLP", "project": "Projects", "platform": "Platform", "registry": "Registry", "caps": "Capabilities", "cap": "Capabilities", "cap_tracking": "Capabilities",
    "redis": "Redis", "stream": "Streams", "board": "Boards", "automations": "Automations", "godseye": "Estate", "topology": "Topology", "system": "System", "sys": "System",
    "app": "System", "babblefish": "Babblefish", "content": "Content", "pydanticai": "Agents", "langgraph": "Agents", "smolagents": "Agents", "eval": "Evaluation", "stream": "Streams",
}
_TTL = 600.0

# ── generated from the mirror's measured envelopes (2026-09-14, _probe_sources.mjs · _measured.mjs): {cap: {shape, map, keys, ms}} ──
MEASURED: Dict[str, Dict[str, Any]] = {
    "acct.get": {"err":"id is required","ms":49,"params":["id"]},
    "acct.list": {"shape":"items","map":{"rows":"presets"},"keys":["accounts","presets"],"ms":52,"note":"dict of things"},
    "agent.get": {"err":"Agent not found: ","ms":84,"params":["id","name","force_refresh"]},
    "agent.history": {"err":"Provide id or name","ms":71,"params":["id","name","limit"]},
    "agent.list": {"shape":"items","map":{"rows":"agents"},"keys":["agents","count"],"ms":129,"note":""},
    "agent.list_fabric": {"shape":"items","map":{"rows":"rows"},"keys":["rows","count"],"ms":146,"note":""},
    "agent.models": {"shape":"items","map":{"rows":"$"},"keys":["instances"],"ms":139,"note":"dict of things"},
    "agentbridge.catalog": {"shape":"items","map":{"rows":"bridges"},"keys":["bridges","count"],"ms":255,"note":""},
    "app.list": {"shape":"items","map":{"rows":"apps"},"keys":["apps","count"],"ms":84,"note":" empty when measured"},
    "automations.overview": {"shape":"items","map":{"rows":"sections"},"keys":["sections","open_actions","unavailable"],"ms":104,"note":""},
    "autonomous.status": {"shape":"values","map":{},"keys":["ok","engaged","mode","reason","since","by"],"ms":61,"note":" key · value"},
    "babblefish.listen": {"err":"host and port required","ms":70,"params":["host","port","protocol","timeout","transport"]},
    "background.status": {"shape":"items","map":{"rows":"jobs"},"keys":["running","busy_reason","quiet_for_s","min_quiet_s","jobs","queue","timeline","eta_total_s","rates"],"ms":83,"note":""},
    "backup.status": {"shape":"items","map":{"rows":"schedules"},"keys":["guests","schedules","storages","snapshots","replication","host","findings","counts","checked_at","elapsed_ms","cached"],"ms":63,"note":""},
    "bench.matrix.get": {"err":"no sweep with id ","ms":52,"params":["id"]},
    "bench.matrix.status": {"err":"unknown or expired sweep: ","ms":54,"params":["run_id"]},
    "bench.node_perf.history": {"shape":"values","map":{},"keys":["series","interval_s","count"],"ms":56,"note":""},
    "bench.result.get": {"err":"id required","ms":48,"params":["id"]},
    "bench.status": {"shape":"level","map":{"value":"active"},"keys":["runs","active"],"ms":50,"note":""},
    "board.item.get": {"err":"no such item: ","ms":94,"params":["id"]},
    "business.account.list": {"shape":"values","map":{},"keys":["accounts","count","total_balance"],"ms":100,"note":""},
    "business.bounty.list": {"shape":"values","map":{},"keys":["bounties","count","reward_pending","reward_paid"],"ms":61,"note":""},
    "business.campaign.list": {"shape":"items","map":{"rows":"campaigns"},"keys":["campaigns","count"],"ms":62,"note":" empty when measured"},
    "business.catalog.products": {"shape":"items","map":{"rows":"products"},"keys":["products","count"],"ms":191,"note":" empty when measured"},
    "business.catalog.resolve": {"shape":"values","map":{},"keys":["ok","product","matched","lookup"],"ms":381,"note":" key · value"},
    "business.content.list": {"shape":"items","map":{"rows":"content"},"keys":["content","count"],"ms":95,"note":" empty when measured"},
    "business.customer.get": {"err":"id required","ms":68,"params":["id"]},
    "business.customer.list": {"shape":"items","map":{"rows":"customers"},"keys":["customers","count"],"ms":56,"note":" empty when measured"},
    "business.gig.list": {"shape":"values","map":{},"keys":["gigs","count","pipeline_value"],"ms":182,"note":""},
    "business.inventory.list": {"shape":"values","map":{},"keys":["items","count","total_value"],"ms":164,"note":""},
    "business.listing.archive": {"err":"listing not found","ms":154,"params":["listing_id","idempotency_key","approval_receipt_ref","retry"]},
    "business.listing.draft": {"err":"product not found","ms":165,"params":["product_id","platform","condition","category","title","description","price","item_specifics","photos","auto_price"]},
    "business.listing.list": {"shape":"items","map":{"rows":"listings"},"keys":["listings","count"],"ms":77,"note":" empty when measured"},
    "business.listing.publish": {"err":"listing not found","ms":74,"params":["listing_id","account_id","price","category_id","idempotency_key","approval_receipt_ref","retry"]},
    "business.market.search": {"err":"query required","ms":64,"params":["query","platform","limit"]},
    "business.material.list": {"shape":"level","map":{"value":"per_item_total"},"keys":["materials","per_item_total"],"ms":294,"note":""},
    "business.order.get": {"err":"id required","ms":52,"params":["id"]},
    "business.order.list": {"shape":"items","map":{"rows":"orders"},"keys":["orders","count"],"ms":152,"note":" empty when measured"},
    "business.price.snapshots": {"shape":"items","map":{"rows":"snapshots"},"keys":["snapshots","count"],"ms":151,"note":" empty when measured"},
    "business.pricehistory.get": {"shape":"values","map":{},"keys":["ok","series","points","currency"],"ms":67,"note":" key · value"},
    "business.product.get": {"err":"id (or sku) required","ms":51,"params":["id"]},
    "business.product.list": {"shape":"items","map":{"rows":"products"},"keys":["products","count"],"ms":438,"note":" empty when measured"},
    "business.service.get": {"err":"id required","ms":45,"params":["id"]},
    "business.service.list": {"shape":"items","map":{"rows":"jobs"},"keys":["jobs","count"],"ms":52,"note":" empty when measured"},
    "business.ship.list": {"shape":"values","map":{},"keys":["shipments","count","to_post","total_postage"],"ms":215,"note":""},
    "business.sim.status": {"shape":"items","map":{"rows":"evals"},"keys":["run","totals","evals"],"ms":129,"note":" empty when measured"},
    "business.social.list": {"shape":"values","map":{},"keys":["socials","count","total_followers"],"ms":75,"note":""},
    "business.sourcing.list": {"shape":"items","map":{"rows":"sourcing"},"keys":["sourcing"],"ms":49,"note":" empty when measured"},
    "business.store.get": {"shape":"items","map":{"rows":"$"},"keys":["store"],"ms":546,"note":"dict of things"},
    "business.store.list": {"shape":"items","map":{"rows":"stores"},"keys":["stores","count"],"ms":395,"note":" empty when measured"},
    "business.stream.list": {"shape":"items","map":{"rows":"streams"},"keys":["streams","count"],"ms":189,"note":" empty when measured"},
    "business.supplier.list": {"shape":"items","map":{"rows":"suppliers"},"keys":["suppliers"],"ms":116,"note":" empty when measured"},
    "business.txn.list": {"shape":"values","map":{},"keys":["txns","count","income","expense","net"],"ms":94,"note":""},
    "business.unit.get": {"err":"unit not found","ms":93,"params":["id"]},
    "business.unit.list": {"shape":"items","map":{"rows":"units"},"keys":["units","count"],"ms":116,"note":" empty when measured"},
    "business.vinted.search": {"err":"query required","ms":91,"params":["query","domain","per_page","order","price_from","price_to","catalog_ids"]},
    "business.watch.list": {"shape":"items","map":{"rows":"watches"},"keys":["watches","count"],"ms":130,"note":" empty when measured"},
    "cal.config.get": {"shape":"values","map":{},"keys":["model","sync_interval_min","fabric_auto_persist","fabric_dataset","tz","default_source"],"ms":52,"note":" key · value"},
    "cal.effects.status": {"shape":"values","map":{},"keys":["schema","local_state","remote_reads","authorization","remote_mutations","claims"],"ms":54,"note":" key · value"},
    "cal.events.list": {"shape":"items","map":{"rows":"events"},"keys":["events","count"],"ms":56,"note":" empty when measured"},
    "cal.notes.list": {"shape":"items","map":{"rows":"notes"},"keys":["notes","count"],"ms":57,"note":""},
    "cal.sources.list": {"shape":"items","map":{"rows":"sources"},"keys":["sources","count"],"ms":47,"note":" empty when measured"},
    "cal.todos.list": {"shape":"items","map":{"rows":"todos"},"keys":["todos","count"],"ms":43,"note":" empty when measured"},
    "canvas.get": {"err":"unknown canvas: ","ms":49,"params":["id"]},
    "canvas.list": {"shape":"items","map":{"rows":"canvases"},"keys":["canvases"],"ms":79,"note":""},
    "canvas.show": {"err":"id is required","ms":55,"params":["id","session_id","title","pinned"]},
    "cap.policy.enforcement.status": {"shape":"series","map":{"series":"supported_families"},"keys":["schema","mode","enabled","config_valid","supported_families","configured_families","unsupported_families","selected_capabilities","rollback"],"ms":103,"note":""},
    "cap_ontology.jobs": {"shape":"items","map":{"rows":"jobs"},"keys":["jobs"],"ms":56,"note":" empty when measured"},
    "cap_ontology.list": {"shape":"items","map":{"rows":"relations"},"keys":["relations","count"],"ms":88,"note":" empty when measured"},
    "cap_ontology.snapshot": {"shape":"values","map":{},"keys":["schema","snapshot_id","content_sha256","relation_count","manual_count","generated_count","relations","restorable","executes"],"ms":55,"note":""},
    "cap_ontology.stats": {"shape":"values","map":{},"keys":["total","auto","manual","covered_caps","total_caps","by_group","auto_generation","generated_relation_consumption"],"ms":56,"note":""},
    "cap_tracking.get_config": {"shape":"series","map":{"series":"hard_skip_groups"},"keys":["config","all_groups","all_caps","hard_skip_groups","always_skip_caps"],"ms":94,"note":""},
    "capacity.status": {"shape":"items","map":{"rows":"seats"},"keys":["ok","seats","ollama","summary"],"ms":154,"note":" empty when measured"},
    "caps.list_categories": {"shape":"items","map":{"rows":"$"},"keys":["categories"],"ms":90,"note":"dict of things"},
    "catalog.autoopt.get": {"shape":"items","map":{"rows":"config"},"keys":["config","log"],"ms":46,"note":"dict of things"},
    "catalog.nodes": {"shape":"items","map":{"rows":"nodes"},"keys":["nodes","cluster"],"ms":71,"note":""},
    "catalog.nodes.detect_all": {"shape":"items","map":{"rows":"results"},"keys":["ok","results"],"ms":54,"note":"dict of things"},
    "catalog.search": {"shape":"items","map":{"rows":"results"},"keys":["results","count","fits","quant"],"ms":258,"note":""},
    "cctv.sources": {"shape":"items","map":{"rows":"sources"},"keys":["sources","count","closed","manifest_url"],"ms":53,"note":""},
    "census.board": {"shape":"values","map":{},"keys":["by_run","found_prefix","fixed_prefix","items_scanned"],"ms":52,"note":" key · value"},
    "census.operator.runs": {"shape":"items","map":{"rows":"runs"},"keys":["runs","count","dir"],"ms":59,"note":" empty when measured"},
    "census.runs": {"shape":"values","map":{},"keys":["runs","count","full_goal_count","complete_count","partial_count","excluded_count","short_count","named_bad_count","trusted_count","trend_complete","trend_trusted","shown_count"],"ms":92,"note":""},
    "certs.list": {"shape":"items","map":{"rows":"certs"},"keys":["certs","counts","findings","soonest","skipped","checked_at","cached"],"ms":51,"note":""},
    "character.get": {"err":"agent_id required","ms":58,"params":["agent_id"]},
    "character.list": {"shape":"items","map":{"rows":"characters"},"keys":["characters","count"],"ms":59,"note":" empty when measured"},
    "cluster.mimic.status": {"shape":"series","map":{"series":"online_nodes"},"keys":["mounted","paused","prefer_gpu","routing","current_target","online_nodes","local_instance","active","queue_depth","queue_per_node","queue_max","max_concurrency"],"ms":66,"note":""},
    "collector.catalog": {"shape":"items","map":{"rows":"sources"},"keys":["sources","count"],"ms":67,"note":""},
    "collector.iot.list_ports": {"shape":"items","map":{"rows":"ports"},"keys":["ports","count"],"ms":64,"note":" empty when measured"},
    "conn.list": {"shape":"items","map":{"rows":"connections"},"keys":["connections","count"],"ms":56,"note":" empty when measured"},
    "content.status": {"shape":"series","map":{"series":"allow"},"keys":["ok","worktree","branch","unpushed_main_commits","allow","deny"],"ms":64,"note":""},
    "context.search_ontologies": {"shape":"items","map":{"rows":"results"},"keys":["results","count","query"],"ms":61,"note":""},
    "dag.workflow.inspect": {"shape":"values","map":{},"keys":["error","executes","mutates"],"ms":61,"note":" key · value"},
    "docker.disk.status": {"shape":"values","map":{},"keys":["level","readable","note"],"ms":53,"note":" key · value"},
    "docker.hosts.list": {"shape":"items","map":{"rows":"hosts"},"keys":["hosts","count"],"ms":49,"note":""},
    "docker.hosts.list.effective": {"shape":"items","map":{"rows":"hosts"},"keys":["hosts","count"],"ms":66,"note":""},
    "docker.ps": {"err":"HTTP 503","ms":75,"params":["host_id","all"]},
    "docker.stack.catalog": {"shape":"items","map":{"rows":"services"},"keys":["services"],"ms":52,"note":""},
    "docker.stack.status": {"shape":"items","map":{"rows":"services"},"keys":["host_id","services"],"ms":63,"note":""},
    "docker.stats.top": {"shape":"items","map":{"rows":"$"},"keys":["hosts"],"ms":53,"note":"dict of things"},
    "docker.worker.list": {"shape":"values","map":{},"keys":["host_id","workers","count","registered"],"ms":61,"note":""},
    "dream.background.status": {"shape":"values","map":{},"keys":["allowed","reason","idle_minutes","min_idle_minutes","human_active","system_busy","enabled","running_background_loops"],"ms":75,"note":""},
    "dream.caps.search": {"shape":"items","map":{"rows":"caps"},"keys":["caps","groups","count"],"ms":64,"note":""},
    "dream.config.get": {"shape":"items","map":{"rows":"$"},"keys":["config"],"ms":52,"note":"dict of things"},
    "dream.cycle.detail": {"err":"cycle_id required","ms":55,"params":["cycle_id"]},
    "dream.director.status": {"shape":"items","map":{"rows":"queue"},"keys":["running","config","cpu_pressure","conversational","conversation","last_thought","queue"],"ms":55,"note":" empty when measured"},
    "dream.history": {"shape":"values","map":{},"keys":["history","count","total","offset","has_more"],"ms":63,"note":""},
    "dream.hitl.pending": {"shape":"items","map":{"rows":"pending"},"keys":["pending","count"],"ms":59,"note":" empty when measured"},
    "dream.journal.list": {"shape":"items","map":{"rows":"journals"},"keys":["ok","journals"],"ms":48,"note":" empty when measured"},
    "dream.loop.settings.get": {"shape":"series","map":{"series":"fields"},"keys":["settings","defaults","fields"],"ms":53,"note":""},
    "dream.pipeline.list": {"shape":"items","map":{"rows":"pipelines"},"keys":["pipelines"],"ms":66,"note":""},
    "dream.review.list": {"shape":"items","map":{"rows":"reports"},"keys":["reports","count"],"ms":58,"note":" empty when measured"},
    "dream.review.runs": {"shape":"items","map":{"rows":"runs"},"keys":["runs","count"],"ms":63,"note":" empty when measured"},
    "dream.review.search": {"shape":"items","map":{"rows":"results"},"keys":["ok","q","results","count"],"ms":50,"note":" empty when measured"},
    "dream.review.snapshots": {"shape":"items","map":{"rows":"snapshots"},"keys":["snapshots","current_source_hash"],"ms":472,"note":""},
    "dream.review.status": {"shape":"values","map":{},"keys":["running"],"ms":51,"note":" key · value"},
    "dream.schedule.events": {"shape":"items","map":{"rows":"events"},"keys":["events","count","days_ahead","generated_at"],"ms":230,"note":""},
    "dream.scheduler.status": {"shape":"values","map":{},"keys":["scheduler_running","enabled","idle_minutes","min_idle_minutes","in_cycle","current_cycle","background_loops","config"],"ms":54,"note":""},
    "dream.sensor.custom.list": {"shape":"items","map":{"rows":"built_in"},"keys":["built_in","custom","total"],"ms":64,"note":""},
    "dream.sensor.topics": {"shape":"items","map":{"rows":"sample"},"keys":["source","count","signal","sample","topics","candidates_total","summary"],"ms":4314,"note":""},
    "dream.sensors.list": {"shape":"items","map":{"rows":"sensors"},"keys":["sensors","count"],"ms":54,"note":""},
    "dream.stage.custom.list": {"shape":"items","map":{"rows":"built_in"},"keys":["built_in","custom","total"],"ms":50,"note":""},
    "dream.stage.load_workspace": {"shape":"items","map":{"rows":"$"},"keys":["workspace"],"ms":53,"note":"dict of things"},
    "dream.stage.snapshot_source": {"shape":"items","map":{"rows":"$"},"keys":["snapshot"],"ms":1346,"note":"dict of things"},
    "dream.stages.list": {"shape":"items","map":{"rows":"stages"},"keys":["stages","count"],"ms":67,"note":""},
    "dream.templates.list": {"shape":"level","map":{"value":"count"},"keys":["templates","count"],"ms":51,"note":""},
    "dream.think.list": {"shape":"items","map":{"rows":"thoughts"},"keys":["thoughts","count"],"ms":74,"note":" empty when measured"},
    "dream.whitelist.list": {"shape":"series","map":{"series":"whitelist"},"keys":["whitelist","count","missing","available"],"ms":79,"note":""},
    "estate.health": {"shape":"items","map":{"rows":"findings"},"keys":["level","counts","sections","findings","checked_at","cached"],"ms":113,"note":""},
    "eval.corpus.inspect": {"shape":"values","map":{},"keys":["schema","ok","issues","case_count","lanes","domains","fingerprint","revision","policy"],"ms":74,"note":""},
    "evolve.activity": {"shape":"items","map":{"rows":"buckets"},"keys":["buckets","hours"],"ms":106,"note":""},
    "evolve.audit.list": {"shape":"items","map":{"rows":"audit"},"keys":["audit","count"],"ms":65,"note":" empty when measured"},
    "evolve.bleeding_edge.list": {"shape":"items","map":{"rows":"edges"},"keys":["edges","default","main"],"ms":167,"note":""},
    "evolve.board": {"shape":"items","map":{"rows":"suites"},"keys":["suites","lanes","count"],"ms":46,"note":" empty when measured"},
    "evolve.census.templates": {"shape":"items","map":{"rows":"templates"},"keys":["templates","count"],"ms":62,"note":" empty when measured"},
    "evolve.config.get": {"shape":"items","map":{"rows":"$"},"keys":["config"],"ms":195,"note":"dict of things"},
    "evolve.errors.list": {"shape":"items","map":{"rows":"items"},"keys":["items","counts","total"],"ms":45,"note":" empty when measured"},
    "evolve.git.status": {"shape":"series","map":{"series":"dirty_files"},"keys":["repo","branch","dirty","dirty_files","branches"],"ms":90,"note":""},
    "evolve.instances": {"shape":"items","map":{"rows":"instances"},"keys":["instances"],"ms":49,"note":" empty when measured"},
    "evolve.mission.events": {"shape":"items","map":{"rows":"activity"},"keys":["events","count","total","summary","families","live","autonomous","counts","any_live","errors_counts","fleet","activity"],"ms":87,"note":""},
    "evolve.overlay.get": {"shape":"values","map":{},"keys":["overlay"],"ms":48,"note":" key · value"},
    "evolve.pipeline.get": {"err":"id required","ms":62,"params":["id"]},
    "evolve.pipeline.list": {"shape":"items","map":{"rows":"pipelines"},"keys":["pipelines","count"],"ms":67,"note":" empty when measured"},
    "evolve.repo.get": {"err":"id required","ms":53,"params":["id"]},
    "evolve.repo.list": {"shape":"items","map":{"rows":"repos"},"keys":["repos"],"ms":100,"note":""},
    "evolve.runs": {"shape":"items","map":{"rows":"runs"},"keys":["runs","count"],"ms":83,"note":" empty when measured"},
    "evolve.target.info": {"shape":"values","map":{},"keys":["target","description","code_file","tunable","profile"],"ms":56,"note":" key · value"},
    "fabric.agents.list": {"shape":"items","map":{"rows":"agents"},"keys":["agents","count"],"ms":59,"note":" empty when measured"},
    "fabric.api.list": {"shape":"items","map":{"rows":"apis"},"keys":["apis","count"],"ms":51,"note":" empty when measured"},
    "fabric.bus.status": {"shape":"items","map":{"rows":"filters"},"keys":["enabled","filters","task_alive","stream","note"],"ms":58,"note":" empty when measured"},
    "fabric.collection.get": {"err":"collection not found","ms":54,"params":["collection_id"]},
    "fabric.collection.list": {"shape":"items","map":{"rows":"collections"},"keys":["collections"],"ms":82,"note":" empty when measured"},
    "fabric.dags.list": {"shape":"items","map":{"rows":"dags"},"keys":["dags","count"],"ms":64,"note":" empty when measured"},
    "fabric.discover.history": {"shape":"items","map":{"rows":"crawls"},"keys":["crawls","count"],"ms":56,"note":" empty when measured"},
    "fabric.discover.query": {"err":"question required","ms":54,"params":["question","dataset_id","crawl_id","max_context_pages"]},
    "fabric.discover.topic": {"err":"topic required","ms":69,"params":["topic","seed_urls","max_sources","content_type","search_angles","sites","include_vera_sources","expansion_rounds","searches_per_round","results_per_search","dataset_id","max_pages","max_depth","topic_dropoff","same_domain","detect_surfaces","extract_subtables","extract_entities","negative_words","negative_urls","required_keyword","required_keyword_mode","auto_promote","auto_pull","llm_search","llm_tagging","min_relevance","extract_entities_llm","no_limit","repetition_dropoff","rolling_description","max_concurrency","entity_workers","auto_synthesize","synth_neighbor_depth","synth_infer_edges","consolidate_entities","drift_guard","llm_drift_gate","swallow_domains","unlimited_depth","topic_brief","brief_rounds","loom","loom_cross","loom_max_datasets","loom_min_score","tags","crawl_id","overwrite","page_text_cap","page_link_cap","max_record_chars","llm_steering","steering_interval","goal","user_agent","ua_rotate"]},
    "fabric.entity_graph.query": {"shape":"items","map":{"rows":"entities"},"keys":["ok","entities","relationships","count"],"ms":72,"note":""},
    "fabric.entity_graph.snapshot": {"shape":"graph","map":{"nodes":"nodes","links":"edges"},"keys":["nodes","edges","node_count","edge_count"],"ms":108,"note":""},
    "fabric.graphs.list": {"shape":"items","map":{"rows":"graphs"},"keys":["graphs"],"ms":58,"note":""},
    "fabric.graphs.query": {"err":"cypher required","ms":57,"params":["graph","cypher"]},
    "fabric.graphs.snapshot": {"shape":"graph","map":{"nodes":"nodes","links":"edges"},"keys":["graph","nodes","edges","node_count","edge_count"],"ms":5583,"note":""},
    "fabric.health": {"shape":"values","map":{},"keys":["db_path","db_size","has_journal_file","has_wal_files","journal_mode","records_count","datasets_count","sources_count","writer_task_alive","write_queue_size","index_migration_done","in_memory_sources"],"ms":53,"note":""},
    "fabric.kb.get": {"err":"knowledgebase not found","ms":76,"params":["kb_id","subject"]},
    "fabric.kb.list": {"shape":"items","map":{"rows":"knowledgebases"},"keys":["knowledgebases"],"ms":61,"note":" empty when measured"},
    "fabric.kb.query": {"err":"query required","ms":80,"params":["query","kb_id","subject","mode","limit"]},
    "fabric.nlp.get": {"shape":"values","map":{},"keys":["enabled"],"ms":63,"note":" key · value"},
    "fabric.objects.list": {"shape":"items","map":{"rows":"objects"},"keys":["objects","count","bucket","prefix"],"ms":50,"note":" empty when measured"},
    "fabric.objects.status": {"shape":"values","map":{},"keys":["enabled","available","mode","endpoint","default_bucket","region","has_boto","last_error"],"ms":59,"note":" key · value"},
    "fabric.pipelines.list": {"shape":"items","map":{"rows":"pipelines"},"keys":["pipelines","count"],"ms":66,"note":" empty when measured"},
    "fabric.query": {"err":"empty query","ms":78,"params":["query","text","vector","dataset_id","top_k","include_data","min_score"]},
    "fabric.schema.get": {"err":"dataset_id required","ms":55,"params":["dataset_id"]},
    "fabric.source_types.list": {"shape":"items","map":{"rows":"types"},"keys":["types"],"ms":56,"note":""},
    "fabric.sources": {"shape":"items","map":{"rows":"sources"},"keys":["sources"],"ms":71,"note":""},
    "fabric.stats": {"shape":"items","map":{"rows":"$"},"keys":["postgres","faiss","chroma","neo4j","sqlite","object_store"],"ms":378,"note":"dict of things"},
    "fabric.subtables.list": {"shape":"items","map":{"rows":"subtables"},"keys":["subtables","count"],"ms":63,"note":" empty when measured"},
    "fabric.surfaces.list": {"shape":"items","map":{"rows":"surfaces"},"keys":["surfaces","count"],"ms":74,"note":" empty when measured"},
    "fabric.synthesize.get": {"err":"model_id required","ms":60,"params":["model_id"]},
    "fabric.synthesize.list": {"shape":"items","map":{"rows":"models"},"keys":["models"],"ms":63,"note":" empty when measured"},
    "fabric.synthesize.topic": {"err":"topic required","ms":61,"params":["topic","dataset_id","allow_discovery","discovery_depth","max_discovery_rounds","max_entries","sites","focus","neighbor_depth","full_source","infer_edges","max_source_chars","persist"]},
    "fabric.tags.list_grouped": {"shape":"items","map":{"rows":"tags"},"keys":["tags","count"],"ms":56,"note":" empty when measured"},
    "fabric.vectors.overview": {"shape":"values","map":{},"keys":["chroma","faiss","configured_dim","chroma_dim","faiss_dim","embed_model","dims_aligned","mismatches"],"ms":64,"note":" key · value"},
    "foundry.blueprint.get": {"err":"id required","ms":58,"params":["id","version"]},
    "foundry.blueprint.list": {"shape":"items","map":{"rows":"blueprints"},"keys":["blueprints"],"ms":53,"note":" empty when measured"},
    "foundry.cluster.init": {"err":"name required","ms":60,"params":["name","kind","advertise_addr","host","ssh_user","ssh_key_path","cluster_id","node","vmid","register"]},
    "foundry.cluster.list": {"shape":"items","map":{"rows":"clusters"},"keys":["clusters"],"ms":54,"note":" empty when measured"},
    "foundry.cluster.ps": {"shape":"values","map":{},"keys":["ok","output"],"ms":58,"note":" key · value"},
    "foundry.cluster.rm": {"err":"name required","ms":63,"params":["cluster_id","node","manager_vmid","name"]},
    "foundry.image.list": {"shape":"items","map":{"rows":"images"},"keys":["images","count"],"ms":58,"note":" empty when measured"},
    "foundry.jobs": {"shape":"items","map":{"rows":"jobs"},"keys":["jobs"],"ms":65,"note":" empty when measured"},
    "foundry.node.list": {"shape":"items","map":{"rows":"nodes"},"keys":["nodes","count"],"ms":65,"note":" empty when measured"},
    "foundry.pxe.profile.list": {"shape":"items","map":{"rows":"profiles"},"keys":["profiles"],"ms":57,"note":" empty when measured"},
    "foundry.pxe.server.status": {"shape":"values","map":{},"keys":["deployed","fenced","iface","output"],"ms":62,"note":" key · value"},
    "foundry.pxe.status": {"shape":"values","map":{},"keys":["deployed","enabled","profiles","macs","config","note"],"ms":56,"note":""},
    "foundry.salvage.inspect": {"err":"device is required","ms":61,"params":["cluster_id","device"]},
    "foundry.salvage.list": {"shape":"items","map":{"rows":"salvages"},"keys":["salvages","count"],"ms":98,"note":" empty when measured"},
    "foundry.sdcard.inspect": {"err":"boot and root partition devices are required","ms":68,"params":["cluster_id","boot","root"]},
    "fs.list": {"shape":"items","map":{"rows":"entries"},"keys":["ok","error","path","entries"],"ms":61,"note":" empty when measured"},
    "fs.read": {"err":"path required","ms":58,"params":["conn_id","kind","docker_host_id","container","ssh_host_id","path","max_bytes"]},
    "gallery.get": {"err":"not found","ms":47,"params":["id"]},
    "gallery.list": {"shape":"items","map":{"rows":"items"},"keys":["items","count","total"],"ms":53,"note":" empty when measured"},
    "goals.detail": {"err":"slug required","ms":57,"params":["slug"]},
    "goals.list": {"shape":"items","map":{"rows":"goals"},"keys":["goals","count"],"ms":55,"note":" empty when measured"},
    "godseye.config.get": {"shape":"series","map":{"series":"known"},"keys":["keys","set","known","note"],"ms":53,"note":""},
    "godseye.status": {"shape":"items","map":{"rows":"config_set"},"keys":["upstream","cloned","commit","dist","build","config_set","app_url","panel_url","vendor_ignored","vendor_ignored_reason","paths"],"ms":171,"note":" empty when measured"},
    "gpu.health": {"shape":"values","map":{},"keys":["status","whisper","stable_diffusion","sd_device","tts","tts_engine","sample_rate","cuda","gpu"],"ms":87,"note":" key · value"},
    "ha.config.get": {"shape":"values","map":{},"keys":["base_url","token","verify_tls","updated","configured"],"ms":85,"note":" key · value"},
    "ha.find": {"err":"query is required","ms":61,"params":["query","domain","limit"]},
    "ha.health": {"err":"Home Assistant is not configured - set base_url and token wi","ms":60,"params":[]},
    "ha.state": {"err":"Home Assistant is not configured - set base_url and token wi","ms":68,"params":["entity"]},
    "ha.states": {"err":"Home Assistant is not configured - set base_url and token wi","ms":61,"params":["domain","available_only","limit"]},
    "ide.agent.list": {"shape":"items","map":{"rows":"agents"},"keys":["agents"],"ms":87,"note":""},
    "ide.claude_sessions.history": {"err":"claude_session_id is required","ms":56,"params":["claude_session_id","scan_limit"]},
    "ide.claude_sessions.list_sessions": {"shape":"values","map":{},"keys":["sessions","returned","max_sessions","total_sessions","truncated"],"ms":64,"note":""},
    "ide.claude_sessions.sources": {"shape":"items","map":{"rows":"sources"},"keys":["sources"],"ms":54,"note":""},
    "ide.claude_sessions.status": {"shape":"items","map":{"rows":"$"},"keys":["sources"],"ms":50,"note":"dict of things"},
    "ide.fs.list": {"shape":"items","map":{"rows":"entries"},"keys":["path","entries"],"ms":86,"note":""},
    "ide.inspect.list_snapshots": {"shape":"items","map":{"rows":"snapshots"},"keys":["snapshots","snapshot_root","current_source_hash","count"],"ms":1010,"note":""},
    "ide.inspect.panel_html": {"shape":"string","map":{},"keys":[],"ms":58,"note":""},
    "ide.inspect.snapshot": {"shape":"values","map":{},"keys":["ok","snapshot_id","path","source_root","file_count","bytes","label","created_at"],"ms":826,"note":""},
    "ide.inspect.source_info": {"shape":"items","map":{"rows":"modules"},"keys":["source_root","snapshot_root","py_file_count","py_total_bytes","modules","panel_files","capabilities_registered","protected"],"ms":73,"note":""},
    "ide.instances": {"shape":"items","map":{"rows":"instances"},"keys":["instances"],"ms":107,"note":""},
    "ide.models": {"shape":"items","map":{"rows":"models"},"keys":["models"],"ms":74,"note":""},
    "ide.remote.bridge.status": {"err":"host-backed instance required","ms":54,"params":["instance_id"]},
    "ide.remote.instances": {"shape":"items","map":{"rows":"instances"},"keys":["instances","count"],"ms":60,"note":" empty when measured"},
    "ide.remote.queue.list": {"shape":"values","map":{},"keys":["items","counts","autopilot","max_concurrency","busy"],"ms":78,"note":" key · value"},
    "ide.remote.status": {"err":"instance not found: ","ms":57,"params":["instance_id"]},
    "ide.sandbox.load": {"shape":"items","map":{"rows":"errors"},"keys":["session_id","loaded","errors"],"ms":61,"note":"dict of things"},
    "ide.vscode.central.status": {"shape":"values","map":{},"keys":["ok","exists","url","proxy","has_token","running","reachable"],"ms":65,"note":" key · value"},
    "ide.vscode.instances": {"shape":"items","map":{"rows":"instances"},"keys":["instances","count"],"ms":62,"note":" empty when measured"},
    "ide.vscode.sandbox.workers": {"shape":"items","map":{"rows":"sandboxes"},"keys":["sandboxes","count"],"ms":90,"note":""},
    "ide.workspace.changes.get": {"err":"unknown proposal: ","ms":52,"params":["id"]},
    "ide.workspace.changes.list": {"shape":"items","map":{"rows":"proposals"},"keys":["proposals","count"],"ms":57,"note":" empty when measured"},
    "ide.workspace.list": {"shape":"items","map":{"rows":"workspaces"},"keys":["workspaces","project_root","snapshot_count"],"ms":786,"note":""},
    "identity.config.get": {"shape":"values","map":{},"keys":["has_ipa_url","ipa_url","has_ipa_domain","ipa_domain","has_ipa_realm","ipa_realm","has_ipa_user","ipa_user","has_dns_zone","dns_zone","has_verify_tls","verify_tls"],"ms":53,"note":" key · value"},
    "identity.group.list": {"err":"ipa_url not configured","ms":59,"params":[]},
    "identity.host.list": {"err":"ipa_url not configured","ms":50,"params":[]},
    "identity.lldap.status": {"shape":"values","map":{},"keys":["deployed","reachable"],"ms":61,"note":" key · value"},
    "identity.resolve.status": {"shape":"values","map":{},"keys":["freeipa","lldap","user_backend","host_backend","backend"],"ms":57,"note":" key · value"},
    "identity.status": {"shape":"values","map":{},"keys":["configured","reachable","version"],"ms":56,"note":" key · value"},
    "identity.user.list": {"err":"ipa_url not configured","ms":48,"params":[]},
    "imagery.search": {"err":"bbox required as [south, west, north, east]","ms":58,"params":["bbox","limit","sources"]},
    "imagery.sources": {"shape":"series","map":{"series":"open"},"keys":["open","keyed","available","note"],"ms":55,"note":""},
    "images.list": {"shape":"items","map":{"rows":"images"},"keys":["images","count"],"ms":61,"note":""},
    "integration.effect.enforcement.readiness": {"shape":"series","map":{"series":"unmet_checks"},"keys":["schema","eligible_for_operator_review","enforcement_enabled","decision","unmet_checks","checks","meaning","executes","changes_policy","retains_payload"],"ms":81,"note":""},
    "integration.effect.replay.status": {"shape":"values","map":{},"keys":["schema","error","already_succeeded","decision","executes","retries","retains_payload"],"ms":194,"note":" key · value"},
    "integration.get": {"err":"not found","ms":55,"params":["id"]},
    "integration.list": {"shape":"items","map":{"rows":"integrations"},"keys":["integrations","count"],"ms":50,"note":" empty when measured"},
    "integration.source.inspect": {"shape":"values","map":{},"keys":["error","kind","accepted","registers","network_io","executes"],"ms":56,"note":" key · value"},
    "jobs.history": {"shape":"values","map":{},"keys":["jobs","total","boot_id","offset","limit"],"ms":47,"note":""},
    "jobs.stats": {"shape":"values","map":{},"keys":["boot_id","running_tracked","stats","history_count","stream"],"ms":55,"note":""},
    "langgraph.status": {"shape":"values","map":{},"keys":["enabled","runtime_id","docker_ok","image","image_present","timeout_s","runtime_adapter"],"ms":203,"note":" key · value"},
    "lhm.menu.list": {"shape":"items","map":{"rows":"menus"},"keys":["ok","menus","count"],"ms":56,"note":" empty when measured"},
    "loops.config.get": {"shape":"values","map":{},"keys":["model","agent","engine","respect_dream_gate","max_concurrent","max_loops_per_program"],"ms":54,"note":""},
    "loops.program.get": {"err":"unknown program: ","ms":99,"params":["id"]},
    "loops.program.list": {"shape":"items","map":{"rows":"programs"},"keys":["programs","count"],"ms":53,"note":" empty when measured"},
    "mail.accounts.list": {"shape":"items","map":{"rows":"accounts"},"keys":["accounts","default"],"ms":47,"note":" empty when measured"},
    "mail.config.get": {"shape":"items","map":{"rows":"accounts"},"keys":["reading_enabled","model","signature","default_account","_migrated_legacy","accounts"],"ms":46,"note":" empty when measured"},
    "mail.events.configure": {"shape":"items","map":{"rows":"events"},"keys":["ok","events"],"ms":48,"note":"dict of things"},
    "mail.events.status": {"shape":"items","map":{"rows":"events"},"keys":["events","bridge_running"],"ms":43,"note":"dict of things"},
    "mail.inbox.list": {"err":"reading disabled — enable it in Email → Settings","ms":70,"params":["account","limit","folder"]},
    "mail.message.get": {"err":"uid is required","ms":45,"params":["uid","account","folder"]},
    "mail.search": {"err":"reading disabled — enable it in Email → Settings","ms":43,"params":["q","account","limit"]},
    "markets.alerts.list": {"shape":"values","map":{},"keys":["alerts","count","unseen"],"ms":95,"note":""},
    "markets.analysis.pivots": {"err":"dataset_id required","ms":52,"params":["dataset_id","method","pct","mult","atr_n","n","detail","limit"]},
    "markets.analysis.trendfit": {"err":"dataset_id required","ms":58,"params":["dataset_id","detail","log_scale","flat_pct_year","limit","start","end"]},
    "markets.backtest.analyze": {"err":"id required","ms":73,"params":["id","replay","limit"]},
    "markets.backtest.autotune": {"err":"dataset_id required","ms":67,"params":["dataset_id","strategy_id","spec","metric","rounds","per_round","axes","update_strategy","oos_split","min_trades","explore","sensitivity","limit","name"]},
    "markets.backtest.autotune_status": {"shape":"items","map":{"rows":"autotunes"},"keys":["autotunes"],"ms":67,"note":" empty when measured"},
    "markets.backtest.batch": {"err":"no strategies — pass strategy_ids (or 'library')","ms":58,"params":["strategy_ids","datasets","assets","tf","all_watchlist","metric","autotune_top","limit"]},
    "markets.backtest.batch_status": {"shape":"items","map":{"rows":"running"},"keys":["last","running"],"ms":58,"note":" empty when measured"},
    "markets.backtest.engines": {"shape":"items","map":{"rows":"engines"},"keys":["engines"],"ms":51,"note":""},
    "markets.backtest.get": {"err":"id required","ms":58,"params":["id"]},
    "markets.backtest.list": {"shape":"items","map":{"rows":"backtests"},"keys":["backtests","count"],"ms":138,"note":" empty when measured"},
    "markets.backtest.signals": {"err":"dataset_id required","ms":58,"params":["dataset_id","strategy_id","spec","start","end","limit","max_markers"]},
    "markets.backtest.sweep": {"err":"dataset_id required","ms":84,"params":["dataset_id","strategy_id","spec","params","metric","limit","name"]},
    "markets.backtest.sweep_status": {"shape":"items","map":{"rows":"sweeps"},"keys":["sweeps"],"ms":49,"note":" empty when measured"},
    "markets.bars": {"err":"dataset_id required","ms":57,"params":["dataset_id","limit","start","end"]},
    "markets.baseline.list": {"shape":"items","map":{"rows":"assets"},"keys":["assets","count"],"ms":130,"note":""},
    "markets.broker.balances": {"err":"id required","ms":74,"params":["id"]},
    "markets.broker.list": {"shape":"items","map":{"rows":"accounts"},"keys":["accounts"],"ms":51,"note":" empty when measured"},
    "markets.broker.order": {"err":"id, symbol and amount required","ms":134,"params":["id","symbol","side","type","amount","price","confirm"]},
    "markets.broker.providers": {"shape":"items","map":{"rows":"providers"},"keys":["providers","ccxt"],"ms":89,"note":""},
    "markets.broker.set_trading": {"err":"id required","ms":55,"params":["id","enabled"]},
    "markets.custom.add_price": {"err":"asset required","ms":64,"params":["asset","price","ts","note"]},
    "markets.custom.list": {"shape":"items","map":{"rows":"assets"},"keys":["assets","count"],"ms":115,"note":" empty when measured"},
    "markets.dynamics.snapshot": {"err":"symbol required","ms":53,"params":["symbol"]},
    "markets.events.detect": {"err":"symbol_key required","ms":53,"params":["symbol_key"]},
    "markets.evolve.history": {"shape":"items","map":{"rows":"history"},"keys":["history"],"ms":47,"note":" empty when measured"},
    "markets.evolve.status": {"shape":"items","map":{"rows":"leaderboard"},"keys":["config","running","tick_running","leaderboard","recent"],"ms":168,"note":" empty when measured"},
    "markets.evolve.tick": {"shape":"items","map":{"rows":"results"},"keys":["ok","results","note"],"ms":56,"note":" empty when measured"},
    "markets.exchanges": {"shape":"items","map":{"rows":"exchanges"},"keys":["ccxt","exchanges","timeframes","default_timeframes"],"ms":83,"note":""},
    "markets.history.audit": {"err":"symbol_key required","ms":56,"params":["symbol_key"]},
    "markets.history.repair": {"err":"symbol_key required","ms":72,"params":["symbol_key","timeframes"]},
    "markets.indicator.custom.list": {"shape":"items","map":{"rows":"indicators"},"keys":["indicators","count"],"ms":143,"note":" empty when measured"},
    "markets.indicator.custom.test": {"err":"dataset_id required","ms":67,"params":["expr","series","dataset_id","limit","full"]},
    "markets.indicator_config.get": {"shape":"items","map":{"rows":"config"},"keys":["symbol_key","config"],"ms":142,"note":"dict of things"},
    "markets.indicators": {"err":"dataset_id required","ms":66,"params":["dataset_id","indicators","limit","start","end"]},
    "markets.infographic.list": {"shape":"items","map":{"rows":"infographics"},"keys":["infographics","count"],"ms":58,"note":" empty when measured"},
    "markets.jobs": {"shape":"items","map":{"rows":"jobs"},"keys":["jobs","inflight"],"ms":72,"note":" empty when measured"},
    "markets.layout.list": {"shape":"items","map":{"rows":"layouts"},"keys":["layouts","count"],"ms":55,"note":" empty when measured"},
    "markets.live.ticks": {"err":"symbol_key required","ms":131,"params":["symbol_key","limit","start"]},
    "markets.macro.catalog": {"shape":"items","map":{"rows":"series"},"keys":["series","count"],"ms":125,"note":""},
    "markets.ml.list": {"shape":"items","map":{"rows":"models"},"keys":["models","count"],"ms":56,"note":" empty when measured"},
    "markets.ml.predict": {"err":"id required","ms":67,"params":["id","dataset_id"]},
    "markets.ml.series": {"err":"id required","ms":89,"params":["id","dataset_id","limit"]},
    "markets.ml.walkforward": {"err":"scikit-learn not installed","ms":62,"params":["dataset_id","model_id","task","model_kind","features","horizon","hyperparams","folds","enter_above","exit_below","short_below","limit","name"]},
    "markets.monitor.status": {"shape":"values","map":{},"keys":["monitors","count","alerts_unseen"],"ms":150,"note":""},
    "markets.news.digest": {"err":"timeout","ms":9056,"params":["symbol_key","per_source","extra_query"]},
    "markets.news.feed": {"shape":"items","map":{"rows":"headlines"},"keys":["ok","query","headlines","cached_at"],"ms":3384,"note":""},
    "markets.news.sources": {"shape":"items","map":{"rows":"sources"},"keys":["sources"],"ms":84,"note":""},
    "markets.news.subscribe": {"err":"label required","ms":60,"params":["label","query","site"]},
    "markets.news.unsubscribe": {"err":"id required","ms":88,"params":["id"]},
    "markets.overview": {"shape":"items","map":{"rows":"groups"},"keys":["groups","count","asof"],"ms":65,"note":" empty when measured"},
    "markets.portfolio.history": {"shape":"items","map":{"rows":"t"},"keys":["t","value","cost","count"],"ms":66,"note":" empty when measured"},
    "markets.portfolio.optimize": {"err":"need ≥2 candidate assets with history","ms":210,"params":["candidates","source","value","objective","max_weight","samples","fee_bps","lookback_days","strategy_map","apply"]},
    "markets.portfolio.positions": {"shape":"items","map":{"rows":"positions"},"keys":["positions","totals","count"],"ms":67,"note":" empty when measured"},
    "markets.portfolio.tx_add": {"err":"symbol_key required","ms":64,"params":["symbol_key","side","qty","price","fees","ts","name","asset_class","note"]},
    "markets.portfolio.tx_list": {"shape":"items","map":{"rows":"transactions"},"keys":["transactions","count"],"ms":79,"note":" empty when measured"},
    "markets.project.portfolio": {"err":"nothing to project — no positions found","ms":112,"params":["source","allocations","horizon_days","strategy_map","inflation_pct","annual_costs_pct"]},
    "markets.quotes": {"shape":"items","map":{"rows":"quotes"},"keys":["quotes","count"],"ms":166,"note":" empty when measured"},
    "markets.sentiment.analyze": {"err":"symbol_key required","ms":100,"params":["symbol_key","name","query"]},
    "markets.sentiment.history": {"err":"symbol_key required","ms":75,"params":["symbol_key","limit"]},
    "markets.sentiment.map": {"shape":"items","map":{"rows":"benchmarks"},"keys":["tracked","benchmarks"],"ms":57,"note":""},
    "markets.sentiment.refresh": {"shape":"series","map":{"series":"queued"},"keys":["ok","queued","count"],"ms":77,"note":""},
    "markets.sentiment.to_series": {"err":"symbol_key required","ms":62,"params":["symbol_key"]},
    "markets.sim.equity": {"err":"account_id required","ms":68,"params":["account_id","limit"]},
    "markets.sim.list": {"shape":"items","map":{"rows":"accounts"},"keys":["accounts","count"],"ms":61,"note":" empty when measured"},
    "markets.sim.order": {"err":"account_id and symbol_key required","ms":76,"params":["account_id","symbol_key","side","qty","notional","pct","price","note","source"]},
    "markets.sim.templates": {"shape":"items","map":{"rows":"templates"},"keys":["templates","count"],"ms":186,"note":""},
    "markets.specialist_context": {"shape":"values","map":{},"keys":["context","asof"],"ms":118,"note":" key · value"},
    "markets.strategy.accept": {"err":"id required","ms":91,"params":["id","dataset_id","interval_min","channels","enabled","sim_account_id","sim_pct"]},
    "markets.strategy.archive": {"err":"id required","ms":71,"params":["id"]},
    "markets.strategy.from_template": {"err":"unknown template ''","ms":95,"params":["template_id","name","overrides"]},
    "markets.strategy.library": {"shape":"items","map":{"rows":"templates"},"keys":["templates","count","categories"],"ms":70,"note":""},
    "markets.strategy.list": {"shape":"items","map":{"rows":"strategies"},"keys":["strategies","count"],"ms":62,"note":" empty when measured"},
    "markets.strategy.revert": {"err":"id required","ms":53,"params":["id","index"]},
    "markets.strategy.versions": {"err":"id required","ms":164,"params":["id"]},
    "markets.symbols": {"shape":"items","map":{"rows":"symbols"},"keys":["exchange","count","symbols"],"ms":3023,"note":""},
    "markets.tax.uk_cgt": {"err":"no transactions in the ledger to compute CGT on","ms":84,"params":["tax_year","annual_exempt","rate_basic","rate_higher","basic_band_left"]},
    "markets.timeframes": {"shape":"series","map":{"series":"timeframes"},"keys":["exchange","timeframes"],"ms":345,"note":""},
    "markets.trader.status": {"shape":"items","map":{"rows":"log"},"keys":["config","grid","log","running"],"ms":262,"note":" empty when measured"},
    "markets.trader.tick": {"shape":"values","map":{},"keys":["ok","mode","grid_cells","trades","signals"],"ms":263,"note":""},
    "markets.watchlist.config": {"err":"symbol required","ms":71,"params":["exchange","symbol","auto_update","update_interval_min","timeframes"]},
    "markets.watchlist.list": {"shape":"items","map":{"rows":"watchlist"},"keys":["watchlist","count"],"ms":757,"note":" empty when measured"},
    "media.image.search": {"shape":"items","map":{"rows":"results"},"keys":["error","results","count"],"ms":62,"note":" empty when measured"},
    "media.nodes": {"shape":"series","map":{"series":"services"},"keys":["nodes","services","fallback_url"],"ms":62,"note":""},
    "memory.stats": {"shape":"series","map":{"series":"active_backends"},"keys":["backends","active_backends"],"ms":494,"note":""},
    "mesh.activity": {"shape":"items","map":{"rows":"events"},"keys":["events","cursor","primed"],"ms":58,"note":" empty when measured"},
    "mesh.alert": {"err":"node_id required","ms":52,"params":["node_id","message","level","sound"]},
    "mesh.app.follow": {"err":"node_id and panel required","ms":60,"params":["node_id","panel","force"]},
    "mesh.app.follow.mode": {"err":"node_id required","ms":58,"params":["node_id","mode"]},
    "mesh.app.launch": {"err":"node_id and app required","ms":61,"params":["node_id","app"]},
    "mesh.app.list": {"shape":"items","map":{"rows":"apps"},"keys":["apps","panels","running"],"ms":57,"note":""},
    "mesh.app.pads": {"shape":"items","map":{"rows":"pads"},"keys":["pads","count"],"ms":75,"note":""},
    "mesh.boards.list": {"shape":"items","map":{"rows":"boards"},"keys":["boards","count"],"ms":51,"note":""},
    "mesh.broadcast": {"err":"type required","ms":54,"params":["type","payload","group","role","module"]},
    "mesh.channel.survey": {"err":"node_id required","ms":51,"params":["node_id"]},
    "mesh.config": {"err":"node_id required","ms":52,"params":["node_id","config","merge"]},
    "mesh.deep_sleep": {"err":"seconds required (>0)","ms":52,"params":["node_id","seconds"]},
    "mesh.display.probe": {"err":"node_id required","ms":52,"params":["node_id"]},
    "mesh.display.test": {"err":"node_id required","ms":55,"params":["node_id","rotation"]},
    "mesh.espnow.ping": {"err":"node_id required","ms":52,"params":["node_id","peer","count"]},
    "mesh.firmware.catalog": {"shape":"items","map":{"rows":"artifacts"},"keys":["artifacts","source_fw","tools"],"ms":109,"note":""},
    "mesh.firmware.probe": {"shape":"values","map":{},"keys":["ok","url","error"],"ms":64,"note":" key · value"},
    "mesh.graph": {"shape":"graph","map":{"nodes":"nodes","links":"edges"},"keys":["nodes","edges","count"],"ms":78,"note":""},
    "mesh.identify": {"err":"node_id required","ms":53,"params":["node_id"]},
    "mesh.io.pins": {"err":"node_id required","ms":51,"params":["node_id","tft","sd","neopixel","rotation"]},
    "mesh.io.read": {"err":"node_id and pin required","ms":58,"params":["node_id","pin","analog"]},
    "mesh.jobs": {"shape":"items","map":{"rows":"jobs"},"keys":["jobs","count"],"ms":90,"note":" empty when measured"},
    "mesh.locate": {"err":"target required","ms":61,"params":["target","max_age_s","rssi_at_1m","path_loss_n"]},
    "mesh.node": {"err":"node_id required","ms":48,"params":["node_id"]},
    "mesh.node.position": {"err":"node_id required","ms":60,"params":["node_id","x","y","z","label"]},
    "mesh.node.position.list": {"shape":"items","map":{"rows":"anchors"},"keys":["anchors","count"],"ms":254,"note":" empty when measured"},
    "mesh.nodes": {"shape":"series","map":{"series":"module_kinds"},"keys":["nodes","count","transports","serial_ports","module_kinds","heartbeat"],"ms":336,"note":""},
    "mesh.pins.map": {"err":"node_id required","ms":50,"params":["node_id"]},
    "mesh.pins.probe": {"err":"node_id required","ms":69,"params":["node_id","pins","results"]},
    "mesh.pins.profiles": {"shape":"level","map":{"value":"count"},"keys":["profiles","count"],"ms":50,"note":""},
    "mesh.pins.touch_hunt": {"err":"node_id required","ms":56,"params":["node_id","hold_s"]},
    "mesh.presence": {"shape":"items","map":{"rows":"nodes"},"keys":["nodes","count"],"ms":56,"note":" empty when measured"},
    "mesh.reboot": {"err":"node_id required","ms":52,"params":["node_id"]},
    "mesh.rf.range": {"err":"target required","ms":51,"params":["node_id","target","kind","samples"]},
    "mesh.rf.targets": {"shape":"items","map":{"rows":"targets"},"keys":["targets","count"],"ms":61,"note":" empty when measured"},
    "mesh.rgb": {"err":"node_id required","ms":58,"params":["node_id","r","g","b","pin","n","effect","brightness"]},
    "mesh.rgb.probe": {"err":"node_id required","ms":51,"params":["node_id","pins","dwell_ms"]},
    "mesh.sd.cat": {"err":"node_id and path required","ms":153,"params":["node_id","path","max"]},
    "mesh.sd.dump": {"err":"node_id required","ms":52,"params":["node_id","paths","max_bytes"]},
    "mesh.sd.identify": {"err":"pass files, or node_id to queue a walk","ms":54,"params":["node_id","files"]},
    "mesh.sd.ls": {"err":"node_id required","ms":89,"params":["node_id","path"]},
    "mesh.sd.store.list": {"shape":"values","map":{},"keys":["files","count","bytes","store"],"ms":47,"note":""},
    "mesh.sd.walk": {"err":"node_id required","ms":74,"params":["node_id","path","max_files","max_depth"]},
    "mesh.settings.get": {"shape":"values","map":{},"keys":["server_url","wifi_ssid","wifi_password","has_wifi_password","token","ota_auto","weather_lat","weather_lon","secrets"],"ms":77,"note":" key · value"},
    "mesh.sniff": {"err":"node_id required","ms":48,"params":["node_id","channel","seconds"]},
    "mesh.sysinfo": {"err":"node_id required","ms":55,"params":["node_id"]},
    "mesh.telemetry": {"err":"node_id required","ms":51,"params":["node_id","metric","limit"]},
    "mesh.topology": {"shape":"graph","map":{"nodes":"nodes","links":"edges"},"keys":["nodes","edges","forwarders"],"ms":3220,"note":""},
    "mesh.touch": {"err":"pin required","ms":55,"params":["node_id","pin"]},
    "mesh.ui.animate": {"err":"node_id required","ms":64,"params":["node_id","url","path","data_b64","frames","cols","rows","w","h","x","y","fps","loop","fit","clear","name"]},
    "mesh.ui.calibrate": {"err":"node_id required","ms":63,"params":["node_id","x0","x1","y0","y1","zmin","zmax","swap","invx","invy"]},
    "mesh.ui.dash.config": {"shape":"items","map":{"rows":"config"},"keys":["ok","config"],"ms":75,"note":"dict of things"},
    "mesh.ui.home": {"err":"node_id required","ms":69,"params":["node_id"]},
    "mesh.ui.image": {"err":"node_id required","ms":72,"params":["node_id","url","path","data_b64","w","h","x","y","fit","clear","name"]},
    "mesh.ui.macropad": {"err":"node_id required","ms":90,"params":["node_id","buttons","cols"]},
    "mesh.ui.screen": {"err":"node_id required","ms":65,"params":["node_id","screen"]},
    "mesh.ui.screens": {"shape":"items","map":{"rows":"screens"},"keys":["screens","count"],"ms":58,"note":" empty when measured"},
    "mesh.ui.sprite": {"err":"node_id and sprite required","ms":56,"params":["node_id","sprite","animation","w","h","x","y","fps","loop"]},
    "mesh.ui.sprites": {"shape":"items","map":{"rows":"sprites"},"keys":["sprites","count","ready","note"],"ms":657,"note":""},
    "mesh.ui.sysmon": {"err":"node_id required","ms":57,"params":["node_id"]},
    "mesh.ui.text": {"err":"node_id required","ms":60,"params":["node_id","title","body","color","bg","size"]},
    "mesh.ui.touch_raw": {"err":"node_id required","ms":60,"params":["node_id","samples"]},
    "mesh.ui.webview": {"err":"node_id and url required","ms":64,"params":["node_id","url","w","h","fit","full_page","wait_ms"]},
    "mesh.ui.widget": {"err":"node_id and a widget object are required","ms":49,"params":["node_id","widget","save_as"]},
    "mesh.ui.widget.kinds": {"shape":"level","map":{"value":"count"},"keys":["kinds","count"],"ms":46,"note":""},
    "metrics.prom.list": {"shape":"items","map":{"rows":"prometheus"},"keys":["prometheus","count"],"ms":50,"note":" empty when measured"},
    "metrics.prom.query": {"err":"query required","ms":59,"params":["query","prom_id","time"]},
    "metrics.prom.query_range": {"err":"query, start and end required","ms":63,"params":["query","start","end","step","prom_id"]},
    "metrics.stack.status": {"shape":"values","map":{},"keys":["host","components","urls"],"ms":57,"note":" key · value"},
    "ml.agent.status": {"shape":"items","map":{"rows":"runs"},"keys":["runs"],"ms":57,"note":" empty when measured"},
    "ml.catalogue": {"shape":"series","map":{"series":"activations"},"keys":["layers","templates","activations","families","backends"],"ms":55,"note":""},
    "ml.data.list": {"shape":"items","map":{"rows":"datasets"},"keys":["datasets","count"],"ms":53,"note":" empty when measured"},
    "ml.examples.load_all": {"err":"timeout","ms":9092,"params":["fetch_real_data"]},
    "ml.list": {"shape":"items","map":{"rows":"modules"},"keys":["modules","count"],"ms":57,"note":" empty when measured"},
    "ml.onnx.list": {"shape":"series","map":{"series":"providers"},"keys":["artifacts","count","dir","onnx","onnxruntime","providers"],"ms":56,"note":""},
    "n8n.config.get": {"shape":"values","map":{},"keys":["base_url","api_key","mcp_path","mcp_token","verify_tls","auto_connect","updated","mcp_url","mcp_test_url","configured","api_configured","mcp_configured"],"ms":67,"note":" key · value"},
    "n8n.workflow.get": {"err":"id required","ms":136,"params":["id","full"]},
    "n8n.workflow.list": {"err":"n8n base_url not configured — call n8n.config.set","ms":66,"params":["active_only","limit"]},
    "netgraph.topology": {"shape":"graph","map":{"nodes":"nodes","links":"edges"},"keys":["nodes","edges","clusters","docker_hosts","cluster_id"],"ms":55,"note":""},
    "netmon.alerts.list": {"shape":"values","map":{},"keys":["detail"],"ms":52,"note":" key · value"},
    "netmon.config.get": {"shape":"items","map":{"rows":"config"},"keys":["config","running","last_tick"],"ms":58,"note":"dict of things"},
    "netmon.snapshot": {"shape":"items","map":{"rows":"nodes"},"keys":["nodes","totals","docker"],"ms":65,"note":" empty when measured"},
    "netmon.target.list": {"shape":"values","map":{},"keys":["detail"],"ms":74,"note":" key · value"},
    "netsec.mesh.status": {"shape":"items","map":{"rows":"members"},"keys":["provider","subnet","enforce","members"],"ms":52,"note":" empty when measured"},
    "nlp.models": {"shape":"series","map":{"series":"providers"},"keys":["rerank_available","rerank_model","classify_available","classify_model","ner_available","ner_model","providers"],"ms":61,"note":""},
    "nodes.backup.get": {"shape":"items","map":{"rows":"config"},"keys":["config","log"],"ms":60,"note":"dict of things"},
    "nodes.components": {"shape":"items","map":{"rows":"components"},"keys":["components"],"ms":60,"note":""},
    "nodes.detect": {"err":"host_id required","ms":57,"params":["host_id"]},
    "nodes.detect_all": {"shape":"items","map":{"rows":"results"},"keys":["ok","results"],"ms":123,"note":"dict of things"},
    "nodes.list": {"shape":"items","map":{"rows":"nodes"},"keys":["nodes"],"ms":58,"note":""},
    "nodes.storage": {"shape":"series","map":{"series":"errors"},"keys":["proxmox","docker","errors"],"ms":75,"note":""},
    "notes.list": {"shape":"items","map":{"rows":"notes"},"keys":["notes","count"],"ms":56,"note":" empty when measured"},
    "obs.cluster": {"shape":"values","map":{},"keys":["workers","ollama","onnx","queues","proxy","local_ollama_id","ts"],"ms":83,"note":" key · value"},
    "obs.diagnostics": {"shape":"series","map":{"series":"worker_ids"},"keys":["host","redis_url","redis_connected","worker_count_local","caps","mode","redis_ping","redis_version","redis_bind","redis_port","workers_in_redis","worker_ids"],"ms":61,"note":""},
    "obs.events": {"shape":"events","map":{},"keys":[],"ms":93,"n":100,"note":""},
    "obs.health": {"shape":"values","map":{},"keys":["redis","postgres","chroma","neo4j","workers","caps","mcp_servers","ollama","mode"],"ms":64,"note":""},
    "obs.modules": {"shape":"items","map":{"rows":"modules"},"keys":["modules","count"],"ms":55,"note":""},
    "obs.neo4j_diag": {"shape":"values","map":{},"keys":["driver_installed","driver_import_error","resolved_uri","resolved_user","password_set","connected","probe"],"ms":65,"note":" key · value"},
    "obs.node_temps": {"shape":"items","map":{"rows":"hosts"},"keys":["hosts","count"],"ms":53,"note":""},
    "obs.pending": {"shape":"items","map":{"rows":"ids"},"keys":["count","ids"],"ms":60,"note":" empty when measured"},
    "obs.provenance": {"shape":"values","map":{},"keys":["git_sha","git_sha_short","branch","dirty","source","instance","pid","started_at"],"ms":336,"note":" key · value"},
    "obs.proxy_log": {"shape":"items","map":{"rows":"log"},"keys":["log","count"],"ms":64,"note":" empty when measured"},
    "obs.redis": {"shape":"values","map":{},"keys":["connected_clients","used_memory_human","uptime_days","keys"],"ms":303,"note":""},
    "obs.scheduler": {"shape":"items","map":{},"keys":["name","interval","runs","last"],"ms":59,"n":64,"note":""},
    "obs.stream_history": {"shape":"items","map":{},"keys":[],"ms":62,"n":0,"note":" empty when measured"},
    "obs.workers": {"shape":"items","map":{"rows":"$"},"keys":["worker-529628a0"],"ms":76,"note":"dict of things"},
    "ollama.cap_routing.get": {"shape":"series","map":{"series":"job_types"},"keys":["user","declared","job_types","nodes"],"ms":54,"note":""},
    "ollama.gate.status": {"shape":"items","map":{"rows":"nodes"},"keys":["enabled","coord_connected","coordination_mode","nodes"],"ms":88,"note":""},
    "ollama.instances": {"shape":"items","map":{"rows":"$"},"keys":["gpu-250","cpu-246","cpu-247"],"ms":75,"note":"dict of things"},
    "ollama.interactive.get": {"shape":"values","map":{},"keys":["enabled","window_s","defer_background","background_always_cpu","human_active","background_on_cpu","seconds_since_interactive"],"ms":56,"note":" key · value"},
    "ollama.list_models": {"shape":"items","map":{"rows":"$"},"keys":["gpu-250","cpu-246","cpu-247"],"ms":137,"note":"dict of things"},
    "ollama.model_tags.get": {"shape":"items","map":{"rows":"$"},"keys":["tags"],"ms":50,"note":"dict of things"},
    "ollama.request_log": {"shape":"events","map":{"events":"entries"},"keys":["entries","total"],"ms":86,"note":""},
    "ollama.role_profiles.get": {"shape":"series","map":{"series":"job_types"},"keys":["declared","user","effective","job_types","nodes"],"ms":55,"note":""},
    "ollama.routing.get": {"shape":"series","map":{"series":"job_types"},"keys":["active_profile","profiles","defaults","job_types","nodes"],"ms":57,"note":""},
    "ontologies.list": {"shape":"items","map":{"rows":"ontologies"},"keys":["ontologies","count"],"ms":63,"note":""},
    "ontologies.list_formats": {"shape":"items","map":{"rows":"formats"},"keys":["rdflib_available","rdflib_error","formats"],"ms":72,"note":""},
    "operator.capture.status": {"shape":"items","map":{"rows":"captures"},"keys":["captures"],"ms":46,"note":" empty when measured"},
    "operator.mission.list": {"shape":"items","map":{"rows":"$"},"keys":["missions"],"ms":46,"note":"dict of things"},
    "operator.read": {"err":"no live session","ms":46,"params":["session_id","selector"]},
    "operator.runs": {"shape":"items","map":{"rows":"runs"},"keys":["runs","count"],"ms":47,"note":" empty when measured"},
    "operator.session.status": {"shape":"items","map":{"rows":"sessions"},"keys":["sessions","count"],"ms":50,"note":" empty when measured"},
    "operator.tour.list": {"shape":"series","map":{"series":"tours"},"keys":["tours"],"ms":53,"note":""},
    "panel.query": {"shape":"values","map":{},"keys":["ok","error","request_id","timeout_secs"],"ms":4074,"note":" key · value"},
    "perf.gate": {"shape":"items","map":{"rows":"top_findings"},"keys":["ok","verdict","blocking","strict","summary","reason","top_findings"],"ms":58,"note":""},
    "perf.log.files": {"shape":"items","map":{"rows":"files"},"keys":["dir","files"],"ms":67,"note":""},
    "perf.log.tail": {"shape":"series","map":{"series":"lines"},"keys":["lines","count","source"],"ms":78,"note":""},
    "perf.note": {"err":"message required","ms":60,"params":["message","level"]},
    "perf.remediate": {"shape":"series","map":{"series":"available"},"keys":["ok","error","available"],"ms":70,"note":""},
    "perf.stalls": {"shape":"events","map":{"events":"events"},"keys":["events","count","hangs","stalls","worst_ms"],"ms":61,"note":""},
    "pki.cert.list": {"shape":"items","map":{"rows":"certs"},"keys":["certs"],"ms":71,"note":" empty when measured"},
    "platform.list": {"shape":"items","map":{"rows":"platforms"},"keys":["platforms","count","store_ok"],"ms":62,"note":""},
    "platform.secrets.list": {"shape":"items","map":{"rows":"secrets"},"keys":["secrets","count","store_ok"],"ms":63,"note":" empty when measured"},
    "platform.values.list": {"shape":"items","map":{"rows":"values"},"keys":["values","count","store_ok"],"ms":55,"note":" empty when measured"},
    "podcast.get": {"err":"unknown episode ","ms":52,"params":["episode_id"]},
    "podcast.list": {"shape":"items","map":{"rows":"episodes"},"keys":["episodes","count"],"ms":64,"note":" empty when measured"},
    "podcast.settings.get": {"shape":"items","map":{"rows":"speakers"},"keys":["speakers","style","minutes","gap_ms","engine","format","show_name","intro","styles"],"ms":55,"note":""},
    "podcast.status": {"err":"unknown job_id ","ms":55,"params":["job_id"]},
    "portainer.containers": {"err":"no Portainer connection configured","ms":63,"params":["portainer_id","endpoint_id","all"]},
    "portainer.list": {"shape":"items","map":{"rows":"portainer"},"keys":["portainer","count"],"ms":76,"note":" empty when measured"},
    "print.config.get": {"shape":"items","map":{"rows":"$"},"keys":["config"],"ms":79,"note":"dict of things"},
    "print.status": {"shape":"series","map":{"series":"transports"},"keys":["pyserial","ports","printers","transports","server_device","pil","pil_version"],"ms":77,"note":""},
    "print.subs.get": {"shape":"items","map":{"rows":"$"},"keys":["subs"],"ms":54,"note":"dict of things"},
    "project.artifact.get": {"err":"slug and id required","ms":72,"params":["slug","id"]},
    "project.artifacts.list": {"err":"slug required","ms":71,"params":["slug","limit","type"]},
    "project.get": {"err":"slug required","ms":49,"params":["slug"]},
    "project.list": {"shape":"items","map":{"rows":"projects"},"keys":["projects","count"],"ms":78,"note":" empty when measured"},
    "project.loops.list": {"err":"slug required","ms":60,"params":["slug","limit"]},
    "project.search_targets": {"shape":"items","map":{"rows":"items"},"keys":["items","type","query"],"ms":67,"note":""},
    "project.thoughts.list": {"shape":"items","map":{"rows":"thoughts"},"keys":["thoughts","count","error"],"ms":56,"note":" empty when measured"},
    "prov.config.get": {"shape":"values","map":{},"keys":["openbao_addr","openbao_namespace","openbao_mount","stepca_url","stepca_fingerprint","stepca_provisioner","stepca_root_pem","base_domain","default_ssh_host_id","verify_tls"],"ms":60,"note":" key · value"},
    "prov.status": {"shape":"items","map":{"rows":"$"},"keys":["openbao","stepca"],"ms":63,"note":"dict of things"},
    "providers.document.status": {"shape":"items","map":{"rows":"provider_profiles"},"keys":["schema","contract","provider_profiles","portable_media_types","frozen_corpus_contract","deterministic_validation","stable_element_ids","citations","render_policy","ocr_execution","parser_execution","optional_provider_imported"],"ms":91,"note":""},
    "providers.list": {"shape":"items","map":{"rows":"providers"},"keys":["providers"],"ms":255,"note":""},
    "providers.models": {"err":"unknown provider: ","ms":64,"params":["provider"]},
    "providers.structured.status": {"shape":"items","map":{"rows":"provider_profiles"},"keys":["schema","portable_keywords","provider_profiles","contract","deterministic_validation","provider_execution","model_execution","optional_providers_imported","network_io","model_called","executes"],"ms":68,"note":""},
    "proxmox.cluster.list": {"shape":"items","map":{"rows":"clusters"},"keys":["clusters"],"ms":51,"note":" empty when measured"},
    "proxmox.console.ticket": {"err":"cluster not found","ms":70,"params":["cluster_id","node","guest_type","vmid","mode"]},
    "proxmox.fw.rules.list": {"err":"cluster not found","ms":62,"params":["cluster_id","scope","node","guest_type","vmid"]},
    "proxmox.guest.action": {"err":"action must be one of ['reboot', 'resume', 'shutdown', 'star","ms":52,"params":["cluster_id","node","guest_type","vmid","action","idempotency_key","approval_receipt_ref","retry"]},
    "proxmox.guest.clone": {"err":"guest_type must be 'qemu' or 'lxc'","ms":85,"params":["cluster_id","node","guest_type","vmid","newid","name","full","storage","target","idempotency_key","approval_receipt_ref","retry"]},
    "proxmox.guest.destroy": {"err":"guest_type must be 'qemu' or 'lxc'","ms":79,"params":["cluster_id","node","guest_type","vmid","purge","idempotency_key","approval_receipt_ref","retry"]},
    "proxmox.guest.ip": {"err":"cluster not found","ms":51,"params":["cluster_id","node","guest_type","vmid"]},
    "proxmox.nextid": {"err":"cluster not found","ms":65,"params":["cluster_id"]},
    "proxmox.node_hosts.merge": {"shape":"values","map":{},"keys":["steps","counts","clusters_changed","dry_run"],"ms":78,"note":" key · value"},
    "proxmox.status": {"shape":"items","map":{"rows":"nodes"},"keys":["error","nodes","guests"],"ms":58,"note":" empty when measured"},
    "proxmox.storage.content": {"err":"cluster not found","ms":59,"params":["cluster_id","node","storage","content"]},
    "pwa.config.get": {"shape":"values","map":{},"keys":["config","version","icons","manifest_url","service_worker_url","offline_url"],"ms":57,"note":" key · value"},
    "pwa.status": {"shape":"series","map":{"series":"install_requirements"},"keys":["enabled","version","assets_present","assets_ok","icons_rendered","config_source","in_pooled_sandbox","disabled_reason","policy","urls","install_requirements","head_tags"],"ms":52,"note":""},
    "pxstore.backend.status": {"err":"cluster not found","ms":51,"params":["cluster_id","node","vmids"]},
    "pxstore.backup.status": {"err":"cluster_id and node required","ms":48,"params":["cluster_id","node"]},
    "pxstore.cpu.topology": {"err":"no SSH login mapped for node '' — set node_hosts on the Prox","ms":81,"params":["cluster_id","node"]},
    "pxstore.fs.status": {"err":"cluster_id and node required","ms":66,"params":["cluster_id","node"]},
    "pxstore.settings.get": {"err":"cluster_id required","ms":59,"params":["cluster_id"]},
    "pxstore.store.status": {"err":"cluster_id and node required","ms":57,"params":["cluster_id","node"]},
    "pydanticai.status": {"shape":"values","map":{},"keys":["enabled","docker_ok","image","image_present","timeout_s"],"ms":197,"note":" key · value"},
    "registry.get": {"shape":"series","map":{"series":"known"},"keys":["ok","error","known"],"ms":56,"note":""},
    "registry.list": {"shape":"items","map":{"rows":"entries"},"keys":["entries","count","kinds"],"ms":57,"note":""},
    "sandbox.config.get": {"shape":"values","map":{},"keys":["docker_host_id","base_image","default_base","auto_sync_interval","archive_on_stop","auto_create","idle_sleep_minutes","idle_archive_days","confine_writes","package_policy","package_allowlist","package_headless_auto"],"ms":66,"note":""},
    "sandbox.packages.catalog": {"shape":"items","map":{"rows":"catalog"},"keys":["ok","policy","catalog","installed","allowlist","blocklist"],"ms":69,"note":""},
    "sandbox.packages.list": {"err":"session_id required","ms":62,"params":["session_id"]},
    "sandbox.packages.pending": {"shape":"items","map":{"rows":"pending"},"keys":["ok","pending","count"],"ms":62,"note":" empty when measured"},
    "sched.config.get": {"shape":"values","map":{},"keys":["enabled","tick_seconds","default_profile","comms_channel","model","max_concurrent_system"],"ms":55,"note":""},
    "secprov.status": {"shape":"items","map":{"rows":"services"},"keys":["host","services"],"ms":75,"note":""},
    "secrets.list": {"err":"OpenBao is not active for Vera; see secrets.status","ms":56,"params":["prefix"]},
    "secrets.status": {"shape":"items","map":{"rows":"findings"},"keys":["backend","openbao","token","keydrop","stored","ssh_store","setup","findings"],"ms":294,"note":""},
    "secstore.kv.get": {"err":"path required","ms":176,"params":["path"]},
    "secstore.kv.list": {"err":"OpenBao address not configured","ms":174,"params":["path"]},
    "smolagents.status": {"shape":"values","map":{},"keys":["enabled","docker_ok","image","image_present","timeout_s"],"ms":128,"note":" key · value"},
    "spritegen.get": {"err":"char_id required","ms":52,"params":["char_id"]},
    "spritegen.list": {"shape":"items","map":{"rows":"characters"},"keys":["characters","count"],"ms":56,"note":" empty when measured"},
    "ssh.cert.status": {"shape":"values","map":{},"keys":["has_key","has_cert","key_path","step_cli"],"ms":53,"note":" key · value"},
    "ssh.host.list": {"shape":"items","map":{"rows":"hosts"},"keys":["hosts"],"ms":59,"note":""},
    "stream.list": {"shape":"values","map":{},"keys":["active","history","active_count","history_count"],"ms":53,"note":""},
    "sys.env.get": {"shape":"items","map":{"rows":"vars"},"keys":["path","vars"],"ms":65,"note":"dict of things"},
    "syscomms.feed": {"shape":"series","map":{"series":"unavailable"},"keys":["feed","summary","unavailable"],"ms":5562,"note":""},
    "syslog.error_summary": {"shape":"events","map":{"events":"entries","t":"ts","kind":"level","text":"message"},"keys":["window_s","errors","warnings","critical","total","by_cap","by_category","series","entries","last_error","source"],"ms":20,"note":"the last hour's warnings and errors, from the errors stream"},
    "syslog.query": {"shape":"events","map":{"events":"entries"},"keys":["entries","count"],"ms":112,"note":""},
    "syslog.status": {"shape":"values","map":{},"keys":["stream","record_count","monitor_enabled","monitor_interval_s","recent_errors"],"ms":95,"note":""},
    "sysmon.history": {"shape":"items","map":{"rows":"samples"},"keys":["samples","count","interval_s","psutil"],"ms":60,"note":""},
    "sysmon.status": {"shape":"values","map":{},"keys":["ts","t","resources","top_processes","proxmox","docker","ollama","temps","pmx_temp_max","cached","age_s"],"ms":63,"note":""},
    "system.narrator.status": {"shape":"series","map":{"series":"probe_kit"},"keys":["enabled","loop_running","cpu_pressure","model","gatherer_model","gap_min","max_probes","deliver_to_chat","probe_kit","intent_enabled","intent","last"],"ms":144,"note":""},
    "tg.bot.status": {"shape":"values","map":{},"keys":["running","bot","last_update","per_chat_agent"],"ms":54,"note":" key · value"},
    "tg.config.get": {"shape":"items","map":{"rows":"$"},"keys":["config"],"ms":62,"note":"dict of things"},
    "tg.events.configure": {"shape":"items","map":{"rows":"events"},"keys":["ok","events"],"ms":58,"note":"dict of things"},
    "tg.events.status": {"shape":"items","map":{"rows":"events"},"keys":["events","running"],"ms":62,"note":"dict of things"},
    "topology.snapshot": {"shape":"graph","map":{"nodes":"nodes","links":"edges"},"keys":["nodes","edges","ts"],"ms":578,"note":""},
    "ui.appearance.get": {"shape":"values","map":{},"keys":["style","density","blocks","styles","densities","defaults"],"ms":414,"note":" key · value"},
    "ui.directive.log": {"shape":"items","map":{"rows":"rows"},"keys":["ok","session_id","rows","asks"],"ms":76,"note":" empty when measured"},
    "ui.loader.get": {"shape":"series","map":{"series":"animations"},"keys":["config","animations"],"ms":81,"note":""},
    "ui.panel.list": {"shape":"items","map":{"rows":"panels"},"keys":["panels","count"],"ms":175,"note":""},
    "ui.policy.get": {"shape":"series","map":{"series":"modes"},"keys":["ok","policy","allowed_always","modes"],"ms":71,"note":""},
    "ui.scale.get": {"shape":"values","map":{},"keys":["scale","min","max","step"],"ms":128,"note":""},
    "ui.theme.get": {"shape":"items","map":{"rows":"vars"},"keys":["theme","vars","type"],"ms":92,"note":"dict of things"},
    "vfs.estate.list": {"err":"asyncssh not installed","ms":52,"params":["refresh"]},
    "vfs.health": {"err":"asyncssh not installed","ms":53,"params":[]},
    "vfs.peer.list": {"err":"the secrets service holds no netctl door token at 'netctl/do","ms":63,"params":[]},
    "vfs.status": {"err":"asyncssh not installed","ms":59,"params":[]},
    "vision.models": {"shape":"items","map":{"rows":"models"},"keys":["models","count","default"],"ms":43,"note":""},
    "vllm.lora.list": {"shape":"items","map":{"rows":"$"},"keys":["by_instance"],"ms":66,"note":"dict of things"},
    "vllm.metrics": {"err":"No instance available","ms":82,"params":["instance_id"]},
    "vllm.models": {"shape":"items","map":{"rows":"by_instance"},"keys":["by_instance","all"],"ms":78,"note":"dict of things"},
    "vllm.status": {"shape":"values","map":{},"keys":["instances","total","online","default_model","global_config"],"ms":65,"note":""},
    "web.api.list": {"shape":"items","map":{"rows":"providers"},"keys":["providers"],"ms":54,"note":" empty when measured"},
    "web.api.search": {"err":"unknown provider: ","ms":56,"params":["id","query","limit"]},
    "widget.forms": {"shape":"items","map":{"rows":"forms"},"keys":["ok","forms","count","shapes","sizes","boards","aliases"],"ms":70,"note":""},
    "widget.instance.list": {"shape":"items","map":{"rows":"instances"},"keys":["ok","instances","count"],"ms":87,"note":""},
    "widget.layouts": {"shape":"items","map":{"rows":"layouts"},"keys":["ok","layouts","count"],"ms":137,"note":""},
    "widget.sources": {"shape":"items","map":{"rows":"sources"},"keys":["ok","sources","count"],"ms":137,"note":""},
    "widget.template.get": {"err":"no template ''","ms":63,"params":["id"]},
    "widget.template.list": {"shape":"items","map":{"rows":"templates"},"keys":["ok","templates","count","total","kinds","wheres","forms","places"],"ms":71,"note":""},
    "workshop.history_to_dag": {"err":"history is required (the agent loop's history list)","ms":64,"params":["history","name","description","tags","category","goal","save"]},
    "workshop.jobs_observatory": {"shape":"items","map":{"rows":"stats"},"keys":["active","history","stats"],"ms":65,"note":"dict of things"},
    "workshop.list_loop_variants": {"shape":"items","map":{"rows":"variants"},"keys":["variants"],"ms":52,"note":""},
    "workspace.get": {"err":"id required","ms":55,"params":["id"]},
    "workspace.list": {"shape":"items","map":{"rows":"workspaces"},"keys":["workspaces","count"],"ms":75,"note":" empty when measured"},
}


# ── the shape of an answer (the python side of the element's formByShape; the container the rows sit in) ────────────
def _ts_key(k: str) -> bool:
    return k in ("t", "ts", "time", "when", "x", "at")


def shape_of(x: Any) -> Tuple[str, Dict[str, str]]:
    """The shape a widget reads an envelope as, and the read.map that digs the rows out of it ('' when there is no
    telling). Mirrors widget_element.js formByShape + applyMap's containers, so a measured source and a live read agree."""
    if x is None:
        return "", {}
    if isinstance(x, list):
        if not x:
            return "items", {}
        f = x[0]
        if isinstance(f, (int, float)) and not isinstance(f, bool):
            return "series", {}
        if isinstance(f, dict):
            ks = set(f)
            if any(_ts_key(k) for k in ks) and ks & {"v", "value", "y"}:
                return "series", {}
            if any(_ts_key(k) for k in ks) and ks & {"text", "msg", "message", "line", "title", "type", "event", "kind", "status"}:
                return "events", {}
            if ks & {"open", "o"} and ks & {"close", "c"}:
                return "ohlcv", {}
            return "items", {}
        return "string" if isinstance(f, str) else "items", {}
    if isinstance(x, dict):
        ks = list(x)
        if isinstance(x.get("nodes"), list) and (isinstance(x.get("edges"), list) or isinstance(x.get("links"), list)):
            return "graph", {"nodes": "nodes", "links": "links" if isinstance(x.get("links"), list) else "edges"}
        if isinstance(x.get("value"), (int, float)) and not isinstance(x.get("value"), bool):
            return "level", {}
        arrays = [k for k in ks if isinstance(x[k], list)]
        obj_arrays = sorted([k for k in arrays if x[k] and isinstance(x[k][0], dict)], key=lambda k: -len(x[k]))
        if obj_arrays:
            k = obj_arrays[0]
            sh, _ = shape_of(x[k])
            cont = {"series": "series", "events": "events", "ohlcv": "bars"}.get(sh, "rows")
            return (sh if sh in ("series", "events", "ohlcv") else "items"), {cont: k}
        num_arrays = [k for k in arrays if x[k] and isinstance(x[k][0], (int, float)) and not isinstance(x[k][0], bool)]
        if num_arrays:
            return "series", {"series": num_arrays[0]}
        objs = [k for k in ks if isinstance(x[k], dict)]
        nums = [k for k in ks if isinstance(x[k], (int, float)) and not isinstance(x[k], bool)]
        if objs and len(objs) == len(ks):
            return "items", {"rows": "$"}          # every key a thing: the entries are the rows, however many
        if len(objs) >= 2 and len(objs) >= len(ks) * 0.6 and all(isinstance(x[k], dict) or k in ("ok", "count", "total", "ts", "mode") for k in ks):
            return "items", {"rows": "$"}          # a dict of things keyed by id: its entries are the rows
        if len(objs) == 1 and len(ks) <= 3 and not nums:
            return "items", {"rows": objs[0]}
        empties = [k for k in arrays if not x[k]]
        if empties and not [k for k in nums if k not in ("count", "total", "n")]:
            return "items", {"rows": empties[0]}
        if len(nums) >= 2:
            return "values", {}
        if len(nums) == 1 and len(ks) <= 3:
            return "level", {"value": nums[0]}
        return "values", {}
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return "level", {}
    return "string", {}


# ── the derivation ────────────────────────────────────────────────────────────
def guess_shape(name: str, description: str = "") -> str:
    """The shape a capability most likely returns, from its name and description (the weakest tier); '' when there is
    no telling."""
    n = str(name or "").lower()
    parts = [p for p in n.replace("_", ".").split(".") if p]
    if not parts or any(p in NEVER for p in parts):
        return ""
    tail = parts[-1]
    for words, shape in _NAME_SHAPE:
        if tail in words:
            return shape
    d = str(description or "").lower()
    for words, shape in _NAME_SHAPE:
        if any((" " + w + " ") in (" " + d + " ") for w in words[:4]):
            return shape
    return ""


def is_read(name: str, entry: Optional[Dict[str, Any]] = None) -> bool:
    """A quiet read: the tail word reads, or the group only reads, or the route is a GET; and no word writes."""
    n = str(name or "").lower()
    if not n or n.startswith(NEVER_PREFIX):
        return False
    parts = [p for p in n.replace("_", ".").split(".") if p]
    if any(p in NEVER for p in parts):
        return False
    if any(w in parts for w in NEVER):
        return False
    tail = parts[-1]
    if tail in READ_TAIL or parts[0] in READ_GROUPS or n.startswith(READ_PREFIX):
        return True
    if entry and str(entry.get("http_method") or "").upper() == "GET":
        return True
    return False


def domain_of(name: str) -> str:
    g = str(name or "").split(".")[0].lower()
    return DOMAINS.get(g, g.capitalize() or "Other")


def _params(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The capability's arguments from its declared schema, else its signature: [{name, type, required, default}]."""
    out: List[Dict[str, Any]] = []
    schema = (entry or {}).get("schema") if isinstance((entry or {}).get("schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    req = set(schema.get("required") or [])
    if props:
        for k, v in list(props.items())[:12]:
            if k in ("trace_id", "self"):
                continue
            out.append({"name": k, "type": str((v or {}).get("type") or "") if isinstance(v, dict) else "", "required": k in req,
                        "default": (v or {}).get("default") if isinstance(v, dict) else None,
                        "desc": str((v or {}).get("description") or "")[:80] if isinstance(v, dict) else ""})
        return out
    fn = (entry or {}).get("raw") or (entry or {}).get("func")
    try:
        for p in list(inspect.signature(fn).parameters.values())[:12]:
            if p.name in ("trace_id", "self") or p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            out.append({"name": p.name, "type": "", "required": p.default is inspect.Parameter.empty, "default": None if p.default is inspect.Parameter.empty else p.default, "desc": ""})
    except Exception:
        pass
    return out


def _desc(entry: Dict[str, Any]) -> str:
    d = re.sub(r"\s+", " ", str((entry or {}).get("description") or "")).strip()
    m = re.match(r"(.{20,160}?[.;])(\s|$)", d)
    return (m.group(1) if m else (d[:157].rstrip() + "…" if len(d) > 160 else d)).strip()


_CONTAINER = {"series": "series", "events": "events", "ohlcv": "bars", "graph": "nodes"}


def _container_for(shape: str, m: Dict[str, str]) -> Dict[str, str]:
    """A measured map re-keyed to the shape that won (the hand list says series where the probe read items): the rows
    container becomes the shape's own — rows ← samples becomes series ← samples."""
    if not m or shape in ("graph",):
        return m
    want = _CONTAINER.get(shape, "rows")
    have = [k for k in ("rows", "series", "events", "bars") if k in m]
    if len(have) == 1 and have[0] != want and want != "nodes":
        m = dict(m); m[want] = m.pop(have[0])
    return m


_CACHE: Dict[str, Any] = {"at": 0.0, "items": [], "live": {}}


def derive(hand: Dict[str, Dict[str, Any]], streams: List[Dict[str, Any]], registry: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Every source: the registry's quiet reads with their shape (hand > measured > declared), the hand list's reads not
    (yet) registered, the streams. Pure over the registry handed in."""
    reg = registry if registry is not None else (getattr(_orch, "CAPABILITY_REGISTRY", {}) or {})
    out: List[Dict[str, Any]] = []
    seen = set()
    live = _CACHE.get("live") or {}
    for name, entry in list(reg.items()):
        entry = entry or {}
        if entry.get("source") == "alias" or entry.get("compatibility_alias_for"):
            continue
        h = hand.get(name)
        if not h and not is_read(name, entry):
            continue
        m = live.get(name) or MEASURED.get(name) or {}
        sh = (h or {}).get("shape") or m.get("shape") or guess_shape(name, str(entry.get("description") or ""))
        if not sh:
            continue
        tier = "hand" if h else ("measured" if m.get("shape") else "declared")
        params = _params(entry)
        mp = _container_for(sh, dict(m.get("map") or {}))
        out.append({"id": name, "cap": name, "shape": sh, "domain": domain_of(name), "args": [p["name"] for p in params], "params": params,
                    "required": [p["name"] for p in params if p.get("required")], "refresh_min": (h or {}).get("refresh_min") or _REFRESH_FLOOR.get(sh, "30s"),
                    "unit": (h or {}).get("unit") or "", "desc": _desc(entry), "map": mp, "keys": list(m.get("keys") or [])[:12],
                    "tier": tier, "note": "" if h else ("measured" + (" · " + m["note"].strip() if m.get("note", "").strip() else "") if tier == "measured" else "shape from the name"),
                    "ms": m.get("ms"), "http": str(entry.get("http_method") or "")})
        seen.add(name)
    for name, h in hand.items():
        if name not in seen:
            out.append({"id": name, "cap": name, "shape": h["shape"], "domain": domain_of(name), "args": [], "params": [], "required": [],
                        "refresh_min": h.get("refresh_min") or "30s", "unit": h.get("unit") or "", "desc": "", "map": {}, "keys": [], "tier": "hand",
                        "note": "not registered here", "ms": None, "http": ""})
    for s in streams:
        x = dict(s)
        x.setdefault("domain", "Streams"); x.setdefault("params", []); x.setdefault("required", []); x.setdefault("desc", x.get("note", "")); x.setdefault("map", {}); x.setdefault("keys", []); x.setdefault("tier", "hand")
        out.append(x)
    out.sort(key=lambda x: (x["note"] == "not registered here", x["domain"], x["id"]))
    return out


def catalogue(hand, streams, refresh: bool = False) -> List[Dict[str, Any]]:
    """The derived registry, cached for _TTL seconds; refresh re-derives now."""
    now = time.time()
    if refresh or not _CACHE["items"] or now - _CACHE["at"] > _TTL:
        _CACHE["items"] = derive(hand, streams)
        _CACHE["at"] = now
    return list(_CACHE["items"])


def cached_at() -> float:
    return float(_CACHE.get("at") or 0.0)


async def probe(limit: int = 40, per_call: float = 3.0, registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Measure the unmeasured: call up to `limit` argument-free quiet reads with a short timeout and keep the shape
    each answered as the live measurement (it beats the table; the next derivation reads it). Never a capability whose
    name says it writes, never one with a required argument."""
    reg = registry if registry is not None else (getattr(_orch, "CAPABILITY_REGISTRY", {}) or {})
    done = 0
    out: Dict[str, Any] = {"probed": [], "errors": [], "skipped": 0}
    for name, entry in list(reg.items()):
        if done >= max(1, int(limit)):
            break
        entry = entry or {}
        if not is_read(name, entry) or name in MEASURED or name in (_CACHE.get("live") or {}):
            continue
        if any(p.get("required") for p in _params(entry)):
            out["skipped"] += 1
            continue
        fn = entry.get("func")
        if not fn:
            continue
        done += 1
        try:
            res = await asyncio.wait_for(fn(), timeout=per_call)
            if isinstance(res, dict) and res.get("error") and len(res) <= 2:
                out["errors"].append({"cap": name, "error": str(res["error"])[:80]})
                continue
            sh, m = shape_of(res)
            if sh:
                _CACHE.setdefault("live", {})[name] = {"shape": sh, "map": m, "keys": list(res)[:12] if isinstance(res, dict) else [], "note": "probed live"}
                out["probed"].append({"cap": name, "shape": sh, "map": m})
        except Exception as e:  # noqa: BLE001 — a probe never raises out
            out["errors"].append({"cap": name, "error": str(e)[:80]})
    if out["probed"]:
        _CACHE["at"] = 0.0     # the next catalogue() re-derives with the live measurements
    return out


# ── the redis.* read family: read-only, the same Redis the orchestrator holds (prod's, or a sandbox's sidecar) ───────
def _redis():
    return getattr(_orch, "REDIS", None)


def _s(v: Any) -> Any:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.hex()
    return v


@capability("redis.info", memory="off", silent=True, http_method="GET", http_path="/ui/widgets/redis/info", http_tags=["redis", "widgets"],
            description="Redis server info as a widget source (values): clients, memory, keys per db, ops/s, uptime. Read-only. "
                        "Output: {ok, connected_clients, used_memory, used_memory_human, total_keys, ops_per_sec, uptime_s, version, dbs:{db: keys}}.")
async def redis_info(trace_id=None):
    r = _redis()
    if not r:
        return {"error": "Redis not connected"}
    try:
        info = await r.info()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    dbs = {k: (v.get("keys") if isinstance(v, dict) else v) for k, v in info.items() if str(k).startswith("db")}
    return {"ok": True, "connected_clients": info.get("connected_clients"), "used_memory": info.get("used_memory"),
            "used_memory_human": info.get("used_memory_human"), "total_keys": sum(int(v or 0) for v in dbs.values() if str(v).isdigit()),
            "ops_per_sec": info.get("instantaneous_ops_per_sec"), "uptime_s": info.get("uptime_in_seconds"), "version": info.get("redis_version"), "dbs": dbs}


@capability("redis.keys", memory="off", silent=True, http_method="GET", http_path="/ui/widgets/redis/keys", http_tags=["redis", "widgets"],
            description="Keys matching a pattern with their type and size, as a widget source (items). Inputs: pattern (str, default "
                        "'vera:*'), limit (int, 200). Read-only, SCAN-based. Output: {ok, keys:[{key, type, len, ttl}], count, pattern}.")
async def redis_keys(pattern: str = "vera:*", limit: int = 200, trace_id=None):
    r = _redis()
    if not r:
        return {"error": "Redis not connected"}
    pat = str(pattern or "vera:*")[:200]
    lim = max(1, min(int(limit or 200), 2000))
    keys: List[Dict[str, Any]] = []
    try:
        async for k in r.scan_iter(match=pat, count=200):
            ks = _s(k)
            t = _s(await r.type(k))
            ln = None
            try:
                if t == "stream":
                    ln = await r.xlen(k)
                elif t == "list":
                    ln = await r.llen(k)
                elif t == "hash":
                    ln = await r.hlen(k)
                elif t == "set":
                    ln = await r.scard(k)
                elif t == "zset":
                    ln = await r.zcard(k)
                elif t == "string":
                    ln = await r.strlen(k)
            except Exception:  # noqa: BLE001
                ln = None
            ttl = await r.ttl(k)
            keys.append({"key": ks, "type": t, "len": ln, "ttl": ttl if ttl is not None and ttl >= 0 else None})
            if len(keys) >= lim:
                break
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    keys.sort(key=lambda x: x["key"])
    return {"ok": True, "keys": keys, "count": len(keys), "pattern": pat}


@capability("redis.get", memory="off", silent=True, http_method="GET", http_path="/ui/widgets/redis/get", http_tags=["redis", "widgets"],
            schema={"type": "object", "properties": {"key": {"type": "string", "description": "the key to read"}}, "required": ["key"]},
            description="One key read by its type, as a widget source: a string (parsed as JSON when it is), a hash (values), a list or "
                        "set (items), a sorted set (items with a score), a stream (its last entries as events). Inputs: key (str!), "
                        "limit (int, 100). Read-only. Output: {ok, key, type, value|values|items|events, count}.")
async def redis_get(key: str = "", limit: int = 100, trace_id=None):
    r = _redis()
    if not r:
        return {"error": "Redis not connected"}
    k = str(key or "").strip()
    if not k:
        return {"error": "key required"}
    lim = max(1, min(int(limit or 100), 1000))
    try:
        t = _s(await r.type(k))
        if t == "none":
            return {"error": "no such key: " + k[:120]}
        if t == "string":
            raw = _s(await r.get(k))
            val: Any = raw
            if isinstance(raw, str) and raw[:1] in "{[":
                try:
                    import json
                    val = json.loads(raw)
                except Exception:  # noqa: BLE001
                    val = raw
            return {"ok": True, "key": k, "type": t, "value": val if not isinstance(val, str) else val[:4000]}
        if t == "hash":
            h = await r.hgetall(k)
            vals = {_s(a): _s(b) for a, b in list(h.items())[:lim]}
            return {"ok": True, "key": k, "type": t, "values": vals, "count": len(h)}
        if t == "list":
            items = [_s(x) for x in await r.lrange(k, -lim, -1)]
            return {"ok": True, "key": k, "type": t, "items": items, "count": await r.llen(k)}
        if t == "set":
            items = [_s(x) for x in list(await r.smembers(k))[:lim]]
            return {"ok": True, "key": k, "type": t, "items": items, "count": await r.scard(k)}
        if t == "zset":
            items = [{"member": _s(m), "score": s} for m, s in await r.zrevrange(k, 0, lim - 1, withscores=True)]
            return {"ok": True, "key": k, "type": t, "items": items, "count": await r.zcard(k)}
        if t == "stream":
            return await redis_stream_tail(key=k, count=lim)
        return {"ok": True, "key": k, "type": t}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


@capability("redis.stream.tail", memory="off", silent=True, http_method="GET", http_path="/ui/widgets/redis/stream", http_tags=["redis", "widgets"],
            schema={"type": "object", "properties": {"key": {"type": "string", "description": "the stream key"}}, "required": ["key"]},
            description="The last entries of a Redis stream as widget events (vera:stream:* and any other). Inputs: key (str!), count "
                        "(int, 50). Read-only. Output: {ok, key, events:[{id, t, ...fields}], count, length}.")
async def redis_stream_tail(key: str = "", count: int = 50, trace_id=None):
    r = _redis()
    if not r:
        return {"error": "Redis not connected"}
    k = str(key or "").strip()
    if not k:
        return {"error": "key required"}
    n = max(1, min(int(count or 50), 500))
    try:
        if _s(await r.type(k)) != "stream":
            return {"error": "not a stream: " + k[:120]}
        rows = await r.xrevrange(k, count=n)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    events = []
    for sid, fields in rows:
        sid = _s(sid)
        ev: Dict[str, Any] = {"id": sid}
        try:
            ev["t"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(int(str(sid).split("-")[0]) / 1000.0))
        except Exception:  # noqa: BLE001
            ev["t"] = sid
        for a, b in fields.items():
            a = _s(a); b = _s(b)
            if a == "data" and isinstance(b, str) and b[:1] in "{[":
                try:
                    import json
                    d = json.loads(b)
                    if isinstance(d, dict):
                        ev.update({kk: (vv if isinstance(vv, (int, float, str, bool)) or vv is None else str(vv)[:200]) for kk, vv in list(d.items())[:12]})
                        continue
                except Exception:  # noqa: BLE001
                    pass
            ev[a] = b if not isinstance(b, str) else b[:400]
        ev.setdefault("text", str(ev.get("type") or ev.get("kind") or ev.get("data") or "")[:200])
        events.append(ev)
    events.reverse()
    try:
        length = await r.xlen(k)
    except Exception:  # noqa: BLE001
        length = None
    return {"ok": True, "key": k, "events": events, "count": len(events), "length": length}
