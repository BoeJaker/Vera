/**
 * topology_map_element.js — <vera-topology-map>
 * ============================================================================
 * A live SVG map of the whole Vera stack for the main dashboard: a "Vera" hub
 * in the middle, one category node per subsystem (Nodes / Workers / Ollama /
 * Mesh / Fabric / Docker / Sandboxes), six subsystem-source nodes (Dream /
 * Chat / DAG / User / Perf / Error Monitor — animation endpoints, not
 * machines), and one leaf per actual machine/worker/instance/mesh device/
 * Proxmox guest/container/sandbox, colored by status (ok/warn/err/unknown).
 * A machine leaf that nodes.list's own merge resolved as backing a specific
 * Ollama instance or Docker host gets an extra dashed "serves" edge straight
 * to that service, on top of its plain category edge — real hardware-backs-
 * service structure, not just a flat category fan-out. Click any node to
 * drill into the REAL cap-activity this map has actually routed through it
 * (a rolling per-node log + name-frequency cloud, never a fabricated view);
 * a sandbox leaf's drill-down additionally links to its full session
 * timeline in the Activity tab. Styled after the existing
 * <vera-loop-graph> (loop_graph_element.js) — true SVG via
 * document.createElementNS, pan = drag, zoom = wheel, theming via the
 * standard Vera CSS variables — but with a fixed radial hub→category→leaf
 * layout instead of a growing event-driven tree, since this is fed periodic
 * polled snapshots (topology.snapshot), not a live event stream.
 *
 * Public API:
 *   el.applySnapshot({nodes:[{id,label,kind,status,detail,temp_c}], edges:[{from,to}]})
 *   el.applyEvent(ev)   — feed one live vera:events event (see below)
 *   el.fit()            — recenter/rescale to fit everything in view
 *   el.setColorMode('status'|'temp') — also user-driven via the on-map
 *     status/temp buttons (top-left). "status" is the ok/warn/err/unknown
 *     dot colour already described above; "temp" recolours every node by its
 *     temp_c field (same <70/70-85/>85°C thresholds used on the Host Temps
 *     tile), unknown where no reading exists. Requested as one of several
 *     "layers" (temperature/location/network/data/jobs) — temp is the one
 *     shipped because it's the one backed by real collected data
 *     (obs.node_temps); location/network/data aren't wired up because Vera
 *     doesn't collect rack/physical placement or flow-level network data
 *     today, and this map doesn't fake a layer off nothing.
 *
 * Truthful animation, two layers:
 *   1. Status-change pulses: a snapshot poll that changes an id's status from
 *      what it was last time triggers a one-shot pulse-ring (self-removing on
 *      animationend) — a poll with no real change produces zero animation.
 *      SVG shapes don't render box-shadow/background-color, so this is its
 *      own transient-circle technique (same idea <vera-loop-graph> uses for
 *      its pulses) rather than the HTML-oriented window.veraUI.pulseOnce().
 *   2. Real routing activity: applyEvent(ev), called by the host page's own
 *      handleLiveEvent() for every real-time event on its already-open
 *      /ws/mcp connection (this element always lives directly in
 *      capability_orchestration.html, never in an iframe, so a plain
 *      function call is simpler and more reliable than opening a second,
 *      independent WebSocket of its own — see <vera-ollama-map>/
 *      ollama_routing_map_element.js for that pattern, appropriate there
 *      since it's embedded inside a different document). Drives a "flying
 *      chip" hopping from node to node toward wherever real work just landed,
 *      fired ONLY by real events — never a decorative interval:
 *        - worker.start/.done/.cancelled -> the worker leaf.
 *        - ollama.request(.done/.error) -> the exact instance leaf, routed
 *          through its inferred origin first when the caller is recognized
 *          (Fabric/Dream/Chat/DAG, via CALLER_SOURCE keyed on caller_file)
 *          then the Ollama category, so an embedding call visibly flies
 *          Fabric -> Ollama -> node instead of a flat, misleading
 *          category -> node hop that made everything look Ollama-sourced.
 *        - cap.call/cap.ok/cap.error -> whichever node's GROUP_NODE entry
 *          matches the cap's `group` (its name's dot-prefix), filtered
 *          against NOISE_GROUPS — the same noisy-group convention
 *          _ACT_SKIP_GROUPS/_PANEL_MIRROR_SKIP_GROUPS already use
 *          server-side (obs/health/ui/mcp/session/syslog/panel/…) — so
 *          expanding GROUP_NODE's coverage can never flood the map with
 *          polling chatter. A direct human touchpoint (chat/ide/operator/
 *          accounts/calendar/email, see USER_TOUCHPOINT_GROUPS) gets the
 *          User node prepended as the chip's origin, the same way
 *          CALLER_SOURCE prepends Fabric/Dream/Chat/DAG onto an
 *          ollama.request chip. This is the existing universal per-
 *          capability activity event every non-silent capability already
 *          emits (_mirror_cap_activity et al) — it's what lights up
 *          Dream/Chat/DAG/Docker/Sandboxes/User, not a bespoke event added
 *          just for this map.
 *        - any cap.error / ollama.request_error -> an EXTRA chip flies to
 *          the Error Monitor node regardless of which group raised it (even
 *          an otherwise-noise-filtered one), so failures are visible as
 *          real flow through the system rather than only a recolour of
 *          their origin. perf.stalls (no live push — polled every 20s, same
 *          diff-gated technique <vera-error-radar> already uses) pulses the
 *          Perf node and relays to Error Monitor on a genuinely new
 *          stall/hang only.
 *      This is what makes "what's routed where" visible without waiting for
 *      the next poll, and is the main answer to "static/boring" — the poll
 *      loop alone can't show anything between two snapshots.
 */
(function () {
  'use strict';
  if (window.customElements && window.customElements.get('vera-topology-map')) return;

  const STATUS_COL = {
    ok: '#5a9e8f', warn: '#c9a45a', err: '#c75a5a', unknown: '#7a7468',
  };
  const KIND_R = { hub: 16, category: 11, node: 7, worker: 6, ollama: 7, mesh: 6, service: 10 };

  // ── Where an ollama.request actually came from, so the chip can fly
  // Fabric -> Ollama -> the exact node instead of a flat Ollama-category ->
  // node hop that makes every request look like it originated in Ollama
  // itself. Keyed on caller_file exactly as _ollama_caller_info() in
  // capability_orchestration.py sets it (basename with .py). Best-effort:
  // an unrecognized caller (or one whose whole call stack lives inside
  // capability_orchestration.py, which the caller-walk deliberately skips)
  // just falls back to the old single-hop behaviour below — never guessed. ──
  const CALLER_SOURCE = {
    'memory.py': 'cat:fabric', 'memory_retrieval.py': 'cat:fabric',
    'memory_second_order.py': 'cat:fabric', 'memory_hooks.py': 'cat:fabric',
    'data_fabric.py': 'cat:fabric', 'data_fabric_collectors.py': 'cat:fabric',
    'context.py': 'cat:fabric', 'knowledgebase.py': 'cat:fabric',
    'discovery.py': 'cat:fabric', 'curation_core.py': 'cat:fabric',
    'curation_capabilities.py': 'cat:fabric', 'session_notes.py': 'cat:fabric',
    'fabric_web_acquisition.py': 'cat:fabric',
    'embed_provider_capabilities.py': 'cat:fabric', 'fastembed_provider.py': 'cat:fabric',
    'dream_capabilities.py': 'svc:dream', 'project_capabilities.py': 'svc:dream',
    'chat_panels_capabilities.py': 'svc:chat',
    'dag_workshop_capabilities.py': 'svc:dag', 'dag_store.py': 'svc:dag',
    'loop_orchestrator.py': 'svc:dag', 'loop_profiles.py': 'svc:dag',
  };
  // Every non-silent capability call emits cap.call/cap.ok/cap.error with a
  // `group` (the cap name's dot-prefix — see _mirror_cap_activity's use of
  // the same field server-side). Mapping group -> a node already on this map
  // is what makes Dream/Chat/DAG/Docker/Sandboxes activity visible at all,
  // independent of whether that call ever touches Ollama.
  const GROUP_NODE = {
    dream: 'svc:dream', project: 'svc:dream',
    chat: 'svc:chat', chat_panels: 'svc:chat',
    dag: 'svc:dag', dag_workshop: 'svc:dag', loops: 'svc:dag',
    agent_loop_v5: 'svc:dag', agent_loop_v6: 'svc:dag',
    agent_loop_v7: 'svc:dag', agent_loop_v8: 'svc:dag',
    docker: 'cat:docker',
    sandbox: 'cat:sandboxes', evolve: 'cat:sandboxes',
    mesh: 'cat:mesh', netmon: 'cat:mesh', recon: 'cat:mesh',
    fabric: 'cat:fabric', memory: 'cat:fabric', discover: 'cat:fabric',
    research: 'cat:fabric', web: 'cat:fabric', knowledgebase: 'cat:fabric',
    nodes: 'cat:nodes', provision: 'cat:nodes',
    perf: 'svc:perf',
    // Direct human touchpoints — no subsystem leaf of their own, so the
    // activity IS the User node rather than being forwarded on. See
    // USER_TOUCHPOINT_GROUPS below for the ones that instead relay into an
    // existing subsystem (chat -> svc:chat) with User prepended as origin.
    ide: 'user', operator: 'user', accounts: 'user',
    calendar: 'user', cal: 'user', email: 'user',
  };
  // Groups that represent a real human acting on Vera directly — these get
  // the User node prepended as the chip's ORIGIN (a 2-hop flight,
  // User -> the actual destination) instead of appearing to spawn out of
  // whichever subsystem happened to handle the request, mirroring how
  // CALLER_SOURCE prepends Fabric/Dream/Chat/DAG onto ollama.request chips.
  const USER_TOUCHPOINT_GROUPS = new Set([
    'chat', 'chat_panels', 'ide', 'operator', 'accounts', 'calendar', 'cal', 'email',
  ]);
  // Capability groups that are pure infrastructure/polling chatter — the
  // exact same noisy-group convention _ACT_SKIP_GROUPS (activity-graph
  // recording) and _PANEL_MIRROR_SKIP_GROUPS (panel activity mirror) already
  // apply server-side, reused here so the map can't be flooded by obs.*
  // polling regardless of how many groups GROUP_NODE above learns to route.
  const NOISE_GROUPS = new Set([
    'obs', 'health', 'ui', 'mcp', 'session', 'syslog', 'panel', 'caps', 'db', 'stream', 'cluster',
  ]);

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function short(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  // trace_id is the one field every cap.call/cap.ok/cap.error emission
  // carries and pairs on (set once per call in begin_stream_activity/the
  // @capability wrapper); fall back to name+session for the rare emitter
  // that omits it rather than dropping the pairing entirely.
  function _capKey(ev) { return 'cap:' + (ev.trace_id || (ev.name + ':' + (ev.session_id || ''))); }

  const NS = 'http://www.w3.org/2000/svg';
  function svgEl(tag, attrs) {
    const el = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  class VeraTopologyMap extends HTMLElement {
    constructor() {
      super();
      this._sr = this.attachShadow({ mode: 'open' });
      this._nodes = new Map();     // id -> {id,label,kind,status,detail,x,y}
      this._edges = [];            // [{from,to}]
      this._prevStatus = new Map(); // id -> last-seen status, for diff-only pulsing
      this._nodeEls = new Map();   // id -> its rendered <g class="node-g"> (for live WS-driven effects between polls)
      this._activeDispatch = new Map(); // task_id/instance_id -> node id currently marked "dispatching"
      this._nodeLog = new Map();   // id -> [{ts,name}] rolling recent-activity log, for drill-down (real, not fetched)
      this._view = { x: 0, y: 0, k: 1 };
      this._drag = null;
      this._colorMode = 'status'; // 'status' | 'temp' — see setColorMode()
      this._stallsSeen = new Set(); // dedupe key for perf.stalls poll (see _pollPerfStalls)
      this._stallsPrimed = false;
      this._build();
      this._pollPerfStalls();
      setInterval(() => this._pollPerfStalls(), 20000);
    }

    connectedCallback() { this.fit(); }

    _parentOf(id) { const e = this._edges.find(x => x.to === id); return e ? e.from : null; }

    // ── live activity: real routing, not a poll-diff guess ─────────────────
    // This element always lives directly in capability_orchestration.html
    // (never in an iframe), so it doesn't open its own WebSocket — the page
    // already has one open (main dash's `ws`, subscribed with
    // action:'subscribe_events' onto the /ws/mcp endpoint), and
    // handleLiveEvent() calls this directly as a plain function call for
    // every real-time event. Public API — called from outside this class.
    applyEvent(ev) {
      const t = (ev && ev.type) || '';
      let matched = true;
      if (t === 'worker.start' && ev.worker) {
        const nid = 'worker:' + ev.worker;
        this._dispatch(ev.task || nid, nid, 'acc');
      } else if ((t === 'worker.done' || t === 'worker.cancelled') && ev.worker) {
        this._settle(ev.task || ('worker:' + ev.worker), t === 'worker.done' ? 'ok' : 'err');
      } else if (t === 'ollama.request' && ev.instance_id) {
        // NOTE: do not gate this on ev.phase — the embedding path
        // (capability_orchestration.py's ollama_embed, the single most
        // frequent live caller: every fabric/memory embed call) emits plain
        // {type:'ollama.request', instance_id, ...} with NO phase field at
        // all, so a phase==='generating' check silently excluded almost all
        // real traffic. Only the full-generate path ever sets 'queued' then
        // 'generating' — firing on every ollama.request regardless of phase
        // means a multi-phase generate call just re-triggers _dispatch a
        // couple of times on the SAME key, which is harmless (it only resets
        // the glow + safety timeout, doesn't double-animate the chip flight
        // since the class is already applied).
        const nid = 'ollama:' + ev.instance_id;
        const parent = this._parentOf(nid) || 'hub';
        const src = CALLER_SOURCE[ev.caller_file] || null;
        const path = (src && src !== parent) ? [src, parent, nid] : [parent, nid];
        this._dispatchVia('oll:' + ev.instance_id, path, 'acc');
      } else if ((t === 'ollama.request_done' || t === 'ollama.request_error') && ev.instance_id) {
        const isErr = t === 'ollama.request_error';
        this._settle('oll:' + ev.instance_id, isErr ? 'err' : 'ok');
        if (isErr && this._nodes.has('svc:errors')) {
          this._flyChip('ollama:' + ev.instance_id, 'svc:errors', STATUS_COL.err);
          this._logNode('svc:errors', 'ollama.request_error');
        }
      } else if (t === 'cap.call' && ev.name) {
        const group = ev.group || String(ev.name).split('.')[0];
        if (NOISE_GROUPS.has(group)) {
          matched = false;
        } else {
          const nid = GROUP_NODE[group];
          if (nid && this._nodes.has(nid)) {
            const path = (USER_TOUCHPOINT_GROUPS.has(group) && nid !== 'user' && this._nodes.has('user'))
              ? ['user', nid]
              : null;
            if (path) this._dispatchVia(_capKey(ev), path, 'acc');
            else this._dispatch(_capKey(ev), nid, 'acc');
            this._logNode(nid, ev.name);
          } else {
            matched = false;
          }
        }
      } else if ((t === 'cap.ok' || t === 'cap.error') && ev.name) {
        const group = ev.group || String(ev.name).split('.')[0];
        const isErr = t === 'cap.error';
        if (NOISE_GROUPS.has(group) && !isErr) {
          matched = false;
        } else {
          const nid = GROUP_NODE[group];
          if (nid && this._nodes.has(nid)) {
            this._settle(_capKey(ev), isErr ? 'err' : 'ok');
          } else if (!isErr) {
            matched = false;
          }
          // Errors flow to the Error Monitor regardless of whether their
          // group has a mapped node — an unmapped/noisy group's cap.error is
          // still real signal worth surfacing, unlike its (filtered) cap.call
          // chatter. Origin is the mapped node when known, else the hub.
          if (isErr && this._nodes.has('svc:errors')) {
            this._flyChip(nid && this._nodes.has(nid) ? nid : 'hub', 'svc:errors', STATUS_COL.err);
            this._logNode('svc:errors', ev.name);
          }
        }
      } else {
        matched = false;
      }
      if (matched) {
        this._lastActivityAt = Date.now();
        this._eventCount++;
        if (this._activityDot) {
          this._activityDot.classList.add('hot');
          clearTimeout(this._activityDotTimer);
          this._activityDotTimer = setTimeout(() => this._activityDot.classList.remove('hot'), 400);
        }
      }
    }

    // Runs every second regardless of whether any event has ever arrived —
    // this is the objective, always-on proof the live feed is connected
    // (vs. the chip-flight animation, which is real but easy to miss in a
    // half-second window). Text always reflects reality: says so plainly if
    // nothing has arrived yet, never fakes "connected".
    _tickActivity() {
      if (!this._activityText) return;
      if (!this._lastActivityAt) {
        this._activityText.textContent = 'no live activity yet';
        return;
      }
      const secs = Math.floor((Date.now() - this._lastActivityAt) / 1000);
      this._activityText.textContent = `${this._eventCount} events · last ${secs}s ago`;
    }

    // Record one real routed cap name against a node, for the drill-down
    // panel's "recent activity" list + name-frequency cloud (_showDrill).
    // This is exactly what was just animated — never a separate fetch, so it
    // can never show anything the map itself didn't actually route.
    _logNode(nodeId, name) {
      const log = this._nodeLog.get(nodeId) || [];
      log.unshift({ ts: Date.now(), name: String(name || '') });
      if (log.length > 40) log.length = 40;
      this._nodeLog.set(nodeId, log);
      if (this._drillId === nodeId) this._renderDrill();
    }

    // perf.stalls has no live WS push (see <vera-error-radar>'s own comment
    // to this effect) — poll it on the same cadence/diff-gate technique:
    // seed the baseline silently on the first poll (pre-existing history
    // isn't a new event), then only animate genuinely new stall/hang rows.
    async _pollPerfStalls() {
      try {
        const base = window._veraBase || window.location.origin || '';
        const r = await fetch(base + '/perf/stalls?limit=10', { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        const events = (d && d.events) || [];
        const fresh = [];
        for (const e of events) {
          if (e.kind !== 'stall' && e.kind !== 'hang') continue;
          const k = String(e.ts || '') + '|' + String(e.kind);
          if (this._stallsSeen.has(k)) continue;
          this._stallsSeen.add(k);
          fresh.push(e);
        }
        if (this._stallsSeen.size > 200) {
          const arr = [...this._stallsSeen];
          this._stallsSeen = new Set(arr.slice(-100));
        }
        if (!this._stallsPrimed) { this._stallsPrimed = true; return; }
        if (fresh.length && this._nodes.has('svc:perf')) {
          fresh.forEach(e => {
            this._pulseEl(this._nodeEls.get('svc:perf'), STATUS_COL.warn);
            this._logNode('svc:perf', e.kind === 'hang' ? 'event-loop hang' : 'event-loop stall');
            if (this._nodes.has('svc:errors')) {
              this._flyChip('svc:perf', 'svc:errors', STATUS_COL.warn);
              this._logNode('svc:errors', e.kind === 'hang' ? 'event-loop hang' : 'event-loop stall');
            }
          });
          this._lastActivityAt = Date.now();
          this._eventCount += fresh.length;
        }
      } catch (_) { /* best-effort, same as error-radar's own poll */ }
    }

    // Fly a chip from the node's category/parent to the node itself, and mark
    // it "dispatching" until the matching settle() call (or a 20s safety
    // timeout, in case a done/error event is ever dropped).
    _dispatch(key, nodeId, color) {
      this._dispatchVia(key, [this._parentOf(nodeId) || 'hub', nodeId], color);
    }
    // Same, but the chip visibly hops through every id in `path` in order
    // (e.g. [Fabric, Ollama-category, the-exact-instance]) rather than one
    // flat parent->leaf jump — this is the actual fix for "it always looks
    // like it came from Ollama": a single-hop chip has no way to show who
    // really originated the request.
    _dispatchVia(key, path, color) {
      const col = STATUS_COL[color] || STATUS_COL.ok;
      const nodeId = path[path.length - 1];
      for (let i = 0; i < path.length - 1; i++) {
        setTimeout(() => this._flyChip(path[i], path[i + 1], col), i * 380);
      }
      const el = this._nodeEls.get(nodeId);
      if (el) el.classList.add('dispatching');
      this._activeDispatch.set(key, nodeId);
      clearTimeout(this['_to_' + key]);
      this['_to_' + key] = setTimeout(() => this._settle(key, 'ok'), 20000);
    }
    _settle(key, color) {
      const nodeId = this._activeDispatch.get(key); if (!nodeId) return;
      clearTimeout(this['_to_' + key]);
      this._activeDispatch.delete(key);
      const el = this._nodeEls.get(nodeId);
      if (el) {
        el.classList.remove('dispatching');
        this._pulseEl(el, STATUS_COL[color] || STATUS_COL.ok);
      }
    }

    _flyChip(fromId, toId, color) {
      const a = this._nodes.get(fromId), b = this._nodes.get(toId);
      if (!a || !b) return;
      const chip = svgEl('circle', { class: 'chip', r: 3, cx: a.x, cy: a.y, fill: color });
      this._gChips.appendChild(chip);
      requestAnimationFrame(() => {
        chip.setAttribute('cx', b.x); chip.setAttribute('cy', b.y);
      });
      setTimeout(() => { chip.style.opacity = '0'; setTimeout(() => chip.remove(), 350); }, 550);
    }

    _pulseEl(g, color) {
      const r = parseFloat(g.dataset.r || '6');
      const ring = svgEl('circle', { class: 'pulsering', r, stroke: color, style: `--r0:${r}px` });
      g.appendChild(ring);
      ring.addEventListener('animationend', () => ring.remove());
    }

    _build() {
      this._sr.innerHTML = `
        <style>
          :host{display:block;width:100%;height:100%;min-height:220px;position:relative;
                font-family:var(--mono,monospace);background:var(--bg0,#0b0f17)}
          .wrap{position:absolute;inset:0;overflow:hidden}
          svg{width:100%;height:100%;display:block;cursor:grab;background:
              radial-gradient(circle at 1px 1px, rgba(255,255,255,.03) 1px, transparent 0) 0 0/24px 24px}
          svg.drag{cursor:grabbing}
          .edge{fill:none;stroke:var(--border,#2a3140);stroke-width:1.2;opacity:.55}
          .edge.serves{stroke:var(--acc,#5b8cff);stroke-dasharray:1 4;stroke-linecap:round;opacity:.6}
          .node-g{cursor:pointer}
          .node-g circle.dot{stroke-width:1.6;transition:fill .25s,stroke .25s}
          .node-g text{font-size:9px;fill:var(--ink,#d9e1ed);pointer-events:none}
          .node-g .sub{font-size:7.5px;fill:var(--dim2,#7e8ba0)}
          .pulsering{fill:none;stroke-width:2;transform-box:fill-box;transform-origin:center;
                     animation:tmPulse .9s ease-out 1}
          @keyframes tmPulse{0%{opacity:.8;r:var(--r0,10)}100%{opacity:0;r:calc(var(--r0,10) + 14px)}}
          .node-g.dispatching circle.dot{stroke-width:3;filter:drop-shadow(0 0 5px var(--acc,#5b8cff))}
          .chip{pointer-events:none;transition:cx .55s ease,cy .55s ease,opacity .35s ease}
          .empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
                 color:var(--dim2,#7e8ba0);font-size:10px}
          .tip{position:absolute;pointer-events:none;left:8px;top:8px;font-size:9.5px;
               background:var(--bg0,#0b0f17);border:1px solid var(--border,#2a3140);border-radius:3px;
               padding:3px 7px;color:var(--ink,#d9e1ed);opacity:0;transition:opacity .1s;max-width:70%}
          .activity{position:absolute;right:8px;top:8px;font-size:9px;font-family:var(--mono,monospace);
                    color:var(--dim2,#7e8ba0);display:flex;align-items:center;gap:5px;pointer-events:none}
          .activity .adot{width:6px;height:6px;border-radius:50%;background:var(--dim,#4a5568);
                          transition:background .15s}
          .activity .adot.hot{background:var(--acc,#5b8cff);box-shadow:0 0 5px var(--acc,#5b8cff)}
          .layers{position:absolute;left:8px;top:8px;display:flex;gap:3px;font-family:var(--mono,monospace)}
          .layers button{font:inherit;font-size:9px;background:var(--bg0,#0b0f17);
                         border:1px solid var(--border,#2a3140);color:var(--dim2,#7e8ba0);
                         border-radius:3px;padding:2px 6px;cursor:pointer}
          .layers button.on{color:var(--ink,#d9e1ed);border-color:var(--acc,#5b8cff);
                            background:color-mix(in srgb, var(--acc,#5b8cff) 15%, var(--bg0,#0b0f17))}
          .drill{position:absolute;right:8px;bottom:8px;top:auto;width:min(280px,60%);
                 max-height:min(320px,72%);background:color-mix(in srgb,var(--bg0,#0b0f17) 92%,transparent);
                 border:1px solid var(--border,#2a3140);border-radius:6px;padding:8px 10px;
                 display:none;flex-direction:column;gap:6px;overflow:hidden;font-size:10px;
                 color:var(--ink,#d9e1ed);backdrop-filter:blur(4px)}
          .drill.open{display:flex}
          .drill h4{margin:0;font-size:10.5px;display:flex;justify-content:space-between;align-items:center;gap:6px}
          .drill h4 button{font:inherit;background:none;border:none;color:var(--dim2,#7e8ba0);cursor:pointer;font-size:11px}
          .drill .meta{color:var(--dim2,#7e8ba0);font-size:9px}
          .drill .cloud{display:flex;flex-wrap:wrap;gap:4px 6px}
          .drill .cloud span{color:var(--ink,#d9e1ed);opacity:.55}
          .drill .loglist{overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:2px}
          .drill .logrow{display:flex;gap:6px;color:var(--dim2,#7e8ba0);font-size:9px}
          .drill .logrow .n{color:var(--ink,#d9e1ed);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
          .drill .empty2{color:var(--dim2,#7e8ba0);font-size:9.5px}
          .drill a.opensession{color:var(--acc,#5b8cff);text-decoration:none;font-size:9.5px}
          .drill a.opensession:hover{text-decoration:underline}
        </style>
        <div class="wrap">
          <svg><g class="cam"><g class="edges"></g><g class="nodes"></g><g class="chips"></g></g></svg>
          <div class="empty" data-part="empty">Waiting for topology.snapshot…</div>
          <div class="tip" data-part="tip"></div>
          <div class="layers" data-part="layers" title="Colour nodes by status, or by their hottest known sensor reading">
            <button type="button" data-layer="status" class="on">status</button>
            <button type="button" data-layer="temp">temp</button>
          </div>
          <div class="activity" data-part="activity" title="Live vera:events reaching this map — proof the WS feed is actually connected, independent of whether you catch a chip flight">
            <span class="adot"></span><span data-part="activity-text">no live activity yet</span>
          </div>
          <div class="drill" data-part="drill">
            <h4><span data-part="drill-title"></span><button type="button" data-part="drill-close" title="Close">×</button></h4>
            <div class="meta" data-part="drill-meta"></div>
            <div class="cloud" data-part="drill-cloud"></div>
            <div class="loglist" data-part="drill-log"></div>
          </div>
        </div>`;
      this._svg = this._sr.querySelector('svg');
      this._cam = this._sr.querySelector('.cam');
      this._gEdges = this._sr.querySelector('.edges');
      this._gNodes = this._sr.querySelector('.nodes');
      this._gChips = this._sr.querySelector('.chips');
      this._empty = this._sr.querySelector('[data-part="empty"]');
      this._tip = this._sr.querySelector('[data-part="tip"]');
      this._activityDot = this._sr.querySelector('.adot');
      this._activityText = this._sr.querySelector('[data-part="activity-text"]');
      this._drill = this._sr.querySelector('[data-part="drill"]');
      this._drillTitle = this._sr.querySelector('[data-part="drill-title"]');
      this._drillMeta = this._sr.querySelector('[data-part="drill-meta"]');
      this._drillCloud = this._sr.querySelector('[data-part="drill-cloud"]');
      this._drillLog = this._sr.querySelector('[data-part="drill-log"]');
      this._drillId = null;
      this._eventCount = 0;
      this._lastActivityAt = 0;
      setInterval(() => this._tickActivity(), 1000);

      this._sr.querySelector('[data-part="layers"]').addEventListener('click', e => {
        const btn = e.target.closest('button[data-layer]');
        if (btn) this.setColorMode(btn.dataset.layer);
      });
      this._sr.querySelector('[data-part="drill-close"]').addEventListener('click', () => this._closeDrill());

      this._svg.addEventListener('mousedown', e => {
        if (e.target.closest('.node-g')) return;
        this._drag = { x: e.clientX, y: e.clientY, vx: this._view.x, vy: this._view.y };
        this._svg.classList.add('drag');
      });
      window.addEventListener('mousemove', e => {
        if (!this._drag) return;
        this._view.x = this._drag.vx + (e.clientX - this._drag.x);
        this._view.y = this._drag.vy + (e.clientY - this._drag.y);
        this._applyCam();
      });
      window.addEventListener('mouseup', () => { this._drag = null; this._svg.classList.remove('drag'); });
      this._svg.addEventListener('wheel', e => {
        e.preventDefault();
        const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        const r = this._svg.getBoundingClientRect();
        const mx = e.clientX - r.left, my = e.clientY - r.top;
        this._view.x = mx - (mx - this._view.x) * f;
        this._view.y = my - (my - this._view.y) * f;
        this._view.k = Math.max(0.3, Math.min(3, this._view.k * f));
        this._applyCam();
      }, { passive: false });

      // Dashboard widgets are user-resizable (drag handle, or a solo-float
      // window), and the SVG itself always tracks the host's box via plain
      // CSS width/height:100% — but the CAMERA (_view) was only ever
      // computed once, in fit(), for whatever size the box happened to be
      // at that moment. Growing the widget afterwards left the graph
      // exactly where it was and just revealed more of the surrounding
      // empty dot-grid background around it — "large spaces that don't
      // have the canvas" — since nothing ever told the camera the box had
      // changed. This keeps it centered on every resize.
      new ResizeObserver(() => this._onResize()).observe(this);
    }

    _applyCam() {
      this._cam.setAttribute('transform', `translate(${this._view.x},${this._view.y}) scale(${this._view.k})`);
    }

    fit() {
      const r = this._svg.getBoundingClientRect();
      this._view = { x: (r.width || 400) / 2, y: (r.height || 260) / 2, k: 1 };
      this._lastRect = { w: r.width || 400, h: r.height || 260 };
      this._applyCam();
    }

    // Re-center (keeping current zoom/pan intent) by exactly how much the
    // box grew or shrank, so whatever was centered stays centered instead
    // of resize either leaving dead space (grow) or cropping content
    // (shrink) around a camera position computed for the old box size.
    _onResize() {
      const r = this._svg.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const last = this._lastRect || { w: r.width, h: r.height };
      this._view.x += (r.width - last.w) / 2;
      this._view.y += (r.height - last.h) / 2;
      this._lastRect = { w: r.width, h: r.height };
      this._applyCam();
    }

    // ── layout: radial hub → ring1 → ring2, keyed by id so positions stay
    // stable across polls (no force simulation needed for a shallow, slowly-
    // changing graph like this). Ring1 is whatever's directly wired to the
    // hub — usually a "category" (Nodes/Workers/Ollama/Mesh/Fabric/Docker/
    // Sandboxes) but also standalone single-instance services (Redis) that
    // have no children of their own; ring2 is anything wired to a ring1 node.
    // Both a category's DISTANCE from the hub and its ANGULAR share of the
    // circle scale with how many leaves it has — a fixed radius/equal-angle
    // layout put a 12+-leaf category (Nodes/Docker/Sandboxes) on the exact
    // same ring as a 1-leaf service, so its own leaf-fan routinely swept into
    // its neighbours' territory. Highly-connected categories now sit further
    // out AND claim more of the circle, proportional to their own fan-out. ──
    _layout(nodes) {
      const byId = new Map(nodes.map(n => [n.id, n]));
      const hub = byId.get('hub');
      if (hub) { hub.x = 0; hub.y = 0; }
      const edgesByParent = {};
      this._edges.forEach(e => { (edgesByParent[e.from] = edgesByParent[e.from] || []).push(e.to); });
      const ring1 = (edgesByParent['hub'] || []).map(id => byId.get(id)).filter(Boolean);
      const leafCount = c => (edgesByParent[c.id] || []).length;
      const r2Of = c => Math.min(130, 30 + leafCount(c) * 6);
      const weights = ring1.map(c => 1 + leafCount(c) * 0.2);
      const totalW = weights.reduce((a, b) => a + b, 0) || 1;
      // Hub-ring radius scales with the NUMBER of ring1 categories, not just
      // each one's own leaf count — this graph has grown from ~5-6
      // categories (Nodes/Workers/Ollama/Mesh/Fabric) to 10+ (+ Docker,
      // Sandboxes, the Dream/Chat/DAG source nodes, Redis), and a radius
      // sized for the smaller set packed neighbouring leaf-fans into each
      // other regardless of any one category's own footprint. The +r2Of(c)
      // term on top still pushes an individual big category out further.
      const minR1 = Math.max(140, ring1.length * 32);
      let acc = 0;
      ring1.forEach((c, i) => {
        const slot = weights[i] / totalW;
        const a = (acc + slot / 2) * Math.PI * 2 - Math.PI / 2;
        acc += slot;
        const r1 = minR1 + r2Of(c) * 0.6;
        c.x = Math.cos(a) * r1; c.y = Math.sin(a) * r1;
      });
      ring1.forEach(c => {
        const leafIds = edgesByParent[c.id] || [];
        const R2 = r2Of(c);
        leafIds.forEach((lid, j) => {
          const leaf = byId.get(lid); if (!leaf) return;
          const a = (j / Math.max(1, leafIds.length)) * Math.PI * 2;
          leaf.x = c.x + Math.cos(a) * R2;
          leaf.y = c.y + Math.sin(a) * R2;
        });
      });
    }

    // ── layers: colour-by, not a separate graph. Only real collected data
    // backs a layer — location/network/data/jobs aren't wired up yet because
    // Vera doesn't collect rack/physical location or flow-level network data
    // today, and faking a gradient off nothing would violate the same
    // truthful-animation rule this map already follows for motion. Temp is
    // real (obs.node_temps, already flowing into topology.snapshot's node
    // leaves as temp_c) so it's the one layer implemented so far. ─────────────
    setColorMode(mode) {
      mode = (mode === 'temp') ? 'temp' : 'status';
      if (mode === this._colorMode) return;
      this._colorMode = mode;
      this._sr.querySelectorAll('[data-part="layers"] button').forEach(b => {
        b.classList.toggle('on', b.dataset.layer === mode);
      });
      this._render(new Set()); // re-colour in place, no fake pulses
    }
    _tempColor(n) {
      if (n.temp_c == null) return STATUS_COL.unknown;
      if (n.temp_c < 70) return STATUS_COL.ok;
      if (n.temp_c < 85) return STATUS_COL.warn;
      return STATUS_COL.err;
    }
    _nodeColor(n) {
      return this._colorMode === 'temp' ? this._tempColor(n) : (STATUS_COL[n.status] || STATUS_COL.unknown);
    }

    // ── public API ───────────────────────────────────────────────────────────
    applySnapshot(data) {
      const nodes = (data && data.nodes) || [];
      const edges = (data && data.edges) || [];
      this._empty.style.display = nodes.length ? 'none' : 'flex';
      this._edges = edges;
      this._layout(nodes);

      const changed = new Set();
      nodes.forEach(n => {
        const prev = this._prevStatus.get(n.id);
        if (prev !== undefined && prev !== n.status) changed.add(n.id);
        this._prevStatus.set(n.id, n.status);
      });
      // Ids no longer present just drop out of _prevStatus lazily (a brand new
      // snapshot rebuilds the whole map below) — no separate cleanup needed.

      this._nodes = new Map(nodes.map(n => [n.id, n]));
      this._render(changed);
      // Fit once, on the first real snapshot only — the graph's world-space
      // extent grew a lot as more categories/leaves were added (see
      // _layout's widened radii), and fit()'s original fixed k:1 view
      // rendered it too zoomed-in on a first paint (or too zoomed-out,
      // depending on widget size), rather than actually showing the whole
      // thing. Every poll after that just re-lays-out in place, same as
      // before — a user's manual pan/zoom is never overridden mid-session.
      if (!this._everFitted && nodes.length) { this._everFitted = true; this._fitToContent(); }
    }

    // Compute a camera transform that shows every node with a margin,
    // rather than fit()'s fixed k:1 view centered on an empty box.
    _fitToContent() {
      const pts = Array.from(this._nodes.values());
      if (!pts.length) return;
      const PAD = 40; // leaf radius + label allowance
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      pts.forEach(n => {
        minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
        minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
      });
      const w = Math.max(1, maxX - minX + PAD * 2), h = Math.max(1, maxY - minY + PAD * 2);
      const r = this._svg.getBoundingClientRect();
      const vw = r.width || 400, vh = r.height || 260;
      const k = Math.max(0.25, Math.min(1.4, Math.min(vw / w, vh / h)));
      this._view = { x: vw / 2 - ((minX + maxX) / 2) * k, y: vh / 2 - ((minY + maxY) / 2) * k, k };
      this._lastRect = { w: vw, h: vh };
      this._applyCam();
    }

    _render(changed) {
      this._gEdges.innerHTML = '';
      this._gNodes.innerHTML = '';
      this._nodeEls.clear();
      const byId = this._nodes;

      this._edges.forEach(e => {
        const a = byId.get(e.from), b = byId.get(e.to);
        if (!a || !b) return;
        // 'serves' edges are a real hardware->service link (nodes.list's own
        // merge, e.g. a machine that backs a specific Ollama instance) on top
        // of the generic category tree — dashed so the structural link reads
        // as distinct from the plain hub/category hierarchy.
        const line = svgEl('line', {
          class: e.kind === 'serves' ? 'edge serves' : 'edge',
          x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        });
        this._gEdges.appendChild(line);
      });

      const activeNodeIds = new Set(this._activeDispatch.values());
      byId.forEach(n => {
        const g = svgEl('g', { class: 'node-g', transform: `translate(${n.x},${n.y})` });
        const r = KIND_R[n.kind] || 6;
        g.dataset.r = String(r);
        const col = this._nodeColor(n);
        const dot = svgEl('circle', { class: 'dot', r, fill: col, stroke: col, 'fill-opacity': n.kind === 'hub' ? 0.25 : 0.35 });
        g.appendChild(dot);
        if (changed.has(n.id)) this._pulseEl(g, col);
        if (activeNodeIds.has(n.id)) g.classList.add('dispatching');   // survives the rebuild below
        // Every node gets a permanent label (leaves included) — hiding leaf
        // names behind hover-only made the map read as less informative, not
        // less cluttered; wider ring spacing (_layout() above) is the actual
        // fix for overlap instead.
        const isCat = n.kind === 'category' || n.kind === 'hub' || n.kind === 'service';
        const label = svgEl('text', { y: r + 10, 'text-anchor': 'middle', 'font-size': isCat ? '9px' : '7.5px' });
        label.textContent = short(n.label || n.id, isCat ? 14 : 11);
        g.appendChild(label);
        g.addEventListener('mouseenter', () => this._showTip(n));
        g.addEventListener('mouseleave', () => { this._tip.style.opacity = 0; });
        g.addEventListener('click', e => { e.stopPropagation(); this._showDrill(n.id); });
        this._gNodes.appendChild(g);
        this._nodeEls.set(n.id, g);
      });
      // A stale drill target (id dropped out of this snapshot, or one whose
      // log has since grown) still needs its content refreshed in place.
      if (this._drillId) this._renderDrill();
    }

    // ── drill-down: click any node to see the REAL cap activity this map has
    // actually routed to/through it (_nodeLog, populated by applyEvent/
    // _pollPerfStalls above) — never a separate fetch invented for the
    // panel, so it can never claim more than what was actually animated.
    // Sandbox leaves carry a real session_id (the "sandbox:<id>" node id),
    // so those additionally offer a link into the full Activity tab's own
    // richer per-session timeline (activity.timeline) instead of trying to
    // duplicate that view inline. ──
    _showDrill(nodeId) {
      this._drillId = nodeId;
      this._drill.classList.add('open');
      this._renderDrill();
    }
    _closeDrill() {
      this._drillId = null;
      this._drill.classList.remove('open');
    }
    _renderDrill() {
      const n = this._nodes.get(this._drillId);
      if (!n) { this._closeDrill(); return; }
      this._drillTitle.textContent = n.label || n.id;
      const temp = n.temp_c != null ? ` · ${n.temp_c.toFixed(0)}°C` : '';
      this._drillMeta.textContent = `[${n.status}]${temp}${n.detail ? ' — ' + n.detail : ''}`;

      const log = this._nodeLog.get(this._drillId) || [];
      // Name-frequency cloud: real counts of what actually flowed through
      // this node, sized/lightened by frequency — a "cloud of context" built
      // from observed activity, not a decorative or invented visualization.
      const counts = new Map();
      log.forEach(it => counts.set(it.name, (counts.get(it.name) || 0) + 1));
      const entries = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 16);
      this._drillCloud.innerHTML = entries.length ? entries.map(([name, count]) => {
        const size = Math.min(15, 9 + count * 1.3);
        const op = Math.min(1, 0.45 + count * 0.12);
        return `<span style="font-size:${size}px;opacity:${op}" title="${esc(name)} × ${count}">${esc(short(name, 22))}</span>`;
      }).join('') : '<span class="empty2">no activity observed here yet this session</span>';

      const sid = this._drillId.startsWith('sandbox:') ? this._drillId.slice(8) : '';
      const link = sid ? `<a class="opensession" target="_blank" rel="noopener"
          href="/activity/panel?scope=chat:${encodeURIComponent(sid)}">Open full session activity ↗</a>` : '';
      const rows = log.slice(0, 20).map(it => {
        const secs = Math.max(0, Math.round((Date.now() - it.ts) / 1000));
        const age = secs < 60 ? `${secs}s` : `${Math.round(secs / 60)}m`;
        return `<div class="logrow"><span>${age}</span><span class="n">${esc(it.name)}</span></div>`;
      }).join('');
      this._drillLog.innerHTML = link + (rows || '<span class="empty2">nothing routed here yet — activity appears as it happens</span>');
    }

    _showTip(n) {
      const temp = n.temp_c != null ? ` · ${n.temp_c.toFixed(0)}°C` : '';
      this._tip.textContent = esc(n.label || n.id) + (n.detail ? ' — ' + esc(n.detail) : '') + ' [' + n.status + ']' + temp;
      this._tip.style.opacity = 1;
    }
  }

  customElements.define('vera-topology-map', VeraTopologyMap);
})();
