/**
 * topology_map_element.js — <vera-topology-map>
 * ============================================================================
 * A live SVG map of the whole Vera stack for the main dashboard: a "Vera" hub
 * in the middle, one category node per subsystem (Nodes / Workers / Ollama /
 * Mesh), and one leaf per actual machine/worker/instance/mesh device, colored
 * by status (ok/warn/err/unknown). Styled after the existing
 * <vera-loop-graph> (loop_graph_element.js) — true SVG via
 * document.createElementNS, pan = drag, zoom = wheel, theming via the
 * standard Vera CSS variables — but with a fixed radial hub→category→leaf
 * layout instead of a growing event-driven tree, since this is fed periodic
 * polled snapshots (topology.snapshot), not a live event stream.
 *
 * Public API:
 *   el.applySnapshot({nodes:[{id,label,kind,status,detail}], edges:[{from,to}]})
 *   el.fit()   — recenter/rescale to fit everything in view
 *
 * Truthful animation, two layers:
 *   1. Status-change pulses: a snapshot poll that changes an id's status from
 *      what it was last time triggers a one-shot pulse-ring (self-removing on
 *      animationend) — a poll with no real change produces zero animation.
 *      SVG shapes don't render box-shadow/background-color, so this is its
 *      own transient-circle technique (same idea <vera-loop-graph> uses for
 *      its pulses) rather than the HTML-oriented window.veraUI.pulseOnce().
 *   2. Real routing activity: a live WS subscription to vera:events (same
 *      stream + subscribe protocol as <vera-ollama-map>/ollama_routing_map_
 *      element.js) drives a "flying chip" from a category to the exact leaf
 *      real work just landed on, fired ONLY by real worker.start/worker.done/
 *      ollama.request(.done/.error) events — never a decorative interval.
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

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function short(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

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
      this._view = { x: 0, y: 0, k: 1 };
      this._drag = null;
      this._ws = null;
      this._base = '';
      this._build();
    }

    connectedCallback() { this.fit(); this._connectWs(); }
    disconnectedCallback() { try { this._ws && this._ws.close(); } catch (_) {} }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }

    // ── live activity: real routing, not a poll-diff guess ─────────────────
    _connectWs() {
      try {
        const wsUrl = this._getBase().replace(/^http/, 'ws') + '/ws';
        this._ws = new WebSocket(wsUrl);
        this._ws.onopen = () => { try { this._ws.send(JSON.stringify({ action: 'subscribe', stream: 'vera:events' })); } catch (_) {} };
        this._ws.onmessage = e => { try { this._onEvent(JSON.parse(e.data)); } catch (_) {} };
        this._ws.onclose = () => { setTimeout(() => this.isConnected && this._connectWs(), 3000); };
        this._ws.onerror = () => { try { this._ws.close(); } catch (_) {} };
      } catch (_) { setTimeout(() => this.isConnected && this._connectWs(), 5000); }
    }

    _parentOf(id) { const e = this._edges.find(x => x.to === id); return e ? e.from : null; }

    _onEvent(ev) {
      const t = (ev && ev.type) || '';
      if (t === 'worker.start' && ev.worker) {
        const nid = 'worker:' + ev.worker;
        this._dispatch(ev.task || nid, nid, 'acc');
      } else if ((t === 'worker.done' || t === 'worker.cancelled') && ev.worker) {
        this._settle(ev.task || ('worker:' + ev.worker), t === 'worker.done' ? 'ok' : 'err');
      } else if (t === 'ollama.request' && ev.phase === 'generating' && ev.instance_id) {
        this._dispatch('oll:' + ev.instance_id, 'ollama:' + ev.instance_id, 'acc');
      } else if ((t === 'ollama.request_done' || t === 'ollama.request_error') && ev.instance_id) {
        this._settle('oll:' + ev.instance_id, t === 'ollama.request_done' ? 'ok' : 'err');
      }
    }

    // Fly a chip from the node's category/parent to the node itself, and mark
    // it "dispatching" until the matching settle() call (or a 20s safety
    // timeout, in case a done/error event is ever dropped).
    _dispatch(key, nodeId, color) {
      const parent = this._parentOf(nodeId) || 'hub';
      this._flyChip(parent, nodeId, STATUS_COL[color] || STATUS_COL.ok);
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
        </style>
        <div class="wrap">
          <svg><g class="cam"><g class="edges"></g><g class="nodes"></g><g class="chips"></g></g></svg>
          <div class="empty" data-part="empty">Waiting for topology.snapshot…</div>
          <div class="tip" data-part="tip"></div>
        </div>`;
      this._svg = this._sr.querySelector('svg');
      this._cam = this._sr.querySelector('.cam');
      this._gEdges = this._sr.querySelector('.edges');
      this._gNodes = this._sr.querySelector('.nodes');
      this._gChips = this._sr.querySelector('.chips');
      this._empty = this._sr.querySelector('[data-part="empty"]');
      this._tip = this._sr.querySelector('[data-part="tip"]');

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
    }

    _applyCam() {
      this._cam.setAttribute('transform', `translate(${this._view.x},${this._view.y}) scale(${this._view.k})`);
    }

    fit() {
      const r = this._svg.getBoundingClientRect();
      this._view = { x: (r.width || 400) / 2, y: (r.height || 260) / 2, k: 1 };
      this._applyCam();
    }

    // ── layout: fixed radial hub → ring1 → ring2, keyed by id so positions
    // stay stable across polls (no force simulation needed for a shallow,
    // slowly-changing graph like this). Ring1 is whatever's directly wired to
    // the hub — usually a "category" (Nodes/Workers/Ollama/Mesh/Fabric) but
    // also standalone single-instance services (Redis) that have no children
    // of their own; ring2 is anything wired to a ring1 node. ─────────────────
    _layout(nodes) {
      const byId = new Map(nodes.map(n => [n.id, n]));
      const hub = byId.get('hub');
      if (hub) { hub.x = 0; hub.y = 0; }
      const edgesByParent = {};
      this._edges.forEach(e => { (edgesByParent[e.from] = edgesByParent[e.from] || []).push(e.to); });
      const ring1 = (edgesByParent['hub'] || []).map(id => byId.get(id)).filter(Boolean);
      const R1 = 110;
      ring1.forEach((c, i) => {
        const a = (i / Math.max(1, ring1.length)) * Math.PI * 2 - Math.PI / 2;
        c.x = Math.cos(a) * R1; c.y = Math.sin(a) * R1;
      });
      ring1.forEach(c => {
        const leafIds = edgesByParent[c.id] || [];
        const R2 = Math.min(70, 26 + leafIds.length * 4);
        leafIds.forEach((lid, j) => {
          const leaf = byId.get(lid); if (!leaf) return;
          const a = (j / Math.max(1, leafIds.length)) * Math.PI * 2;
          leaf.x = c.x + Math.cos(a) * R2;
          leaf.y = c.y + Math.sin(a) * R2;
        });
      });
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
    }

    _render(changed) {
      this._gEdges.innerHTML = '';
      this._gNodes.innerHTML = '';
      this._nodeEls.clear();
      const byId = this._nodes;

      this._edges.forEach(e => {
        const a = byId.get(e.from), b = byId.get(e.to);
        if (!a || !b) return;
        const line = svgEl('line', {
          class: 'edge', x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        });
        this._gEdges.appendChild(line);
      });

      const activeNodeIds = new Set(this._activeDispatch.values());
      byId.forEach(n => {
        const g = svgEl('g', { class: 'node-g', transform: `translate(${n.x},${n.y})` });
        const r = KIND_R[n.kind] || 6;
        g.dataset.r = String(r);
        const col = STATUS_COL[n.status] || STATUS_COL.unknown;
        const dot = svgEl('circle', { class: 'dot', r, fill: col, stroke: col, 'fill-opacity': n.kind === 'hub' ? 0.25 : 0.35 });
        g.appendChild(dot);
        if (changed.has(n.id)) this._pulseEl(g, col);
        if (activeNodeIds.has(n.id)) g.classList.add('dispatching');   // survives the rebuild below
        const label = svgEl('text', { y: r + 11, 'text-anchor': 'middle' });
        label.textContent = short(n.label || n.id, n.kind === 'category' ? 14 : 12);
        g.appendChild(label);
        g.addEventListener('mouseenter', () => this._showTip(n));
        g.addEventListener('mouseleave', () => { this._tip.style.opacity = 0; });
        this._gNodes.appendChild(g);
        this._nodeEls.set(n.id, g);
      });
    }

    _showTip(n) {
      this._tip.textContent = esc(n.label || n.id) + (n.detail ? ' — ' + esc(n.detail) : '') + ' [' + n.status + ']';
      this._tip.style.opacity = 1;
    }
  }

  customElements.define('vera-topology-map', VeraTopologyMap);
})();
