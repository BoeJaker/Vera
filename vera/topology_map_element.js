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
 * Truthful animation: nodes/edges only pulse when a snapshot changes an id's
 * status from what it was on the PREVIOUS applySnapshot() call — a poll with
 * no real change produces zero animation. SVG shapes don't render
 * box-shadow/background-color, so this uses the same "transient pulse-ring
 * circle, self-removing on animationend" technique <vera-loop-graph> already
 * uses for its own SVG pulses, rather than the HTML-oriented
 * window.veraUI.pulseOnce() (built for box-shadow/background-color flashes,
 * which don't apply to raw SVG geometry).
 */
(function () {
  'use strict';
  if (window.customElements && window.customElements.get('vera-topology-map')) return;

  const STATUS_COL = {
    ok: '#5a9e8f', warn: '#c9a45a', err: '#c75a5a', unknown: '#7a7468',
  };
  const KIND_R = { hub: 16, category: 11, node: 7, worker: 6, ollama: 7, mesh: 6 };

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
      this._view = { x: 0, y: 0, k: 1 };
      this._drag = null;
      this._build();
    }

    connectedCallback() { this.fit(); }

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
          .empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
                 color:var(--dim2,#7e8ba0);font-size:10px}
          .tip{position:absolute;pointer-events:none;left:8px;top:8px;font-size:9.5px;
               background:var(--bg0,#0b0f17);border:1px solid var(--border,#2a3140);border-radius:3px;
               padding:3px 7px;color:var(--ink,#d9e1ed);opacity:0;transition:opacity .1s;max-width:70%}
        </style>
        <div class="wrap">
          <svg><g class="cam"><g class="edges"></g><g class="nodes"></g></g></svg>
          <div class="empty" data-part="empty">Waiting for topology.snapshot…</div>
          <div class="tip" data-part="tip"></div>
        </div>`;
      this._svg = this._sr.querySelector('svg');
      this._cam = this._sr.querySelector('.cam');
      this._gEdges = this._sr.querySelector('.edges');
      this._gNodes = this._sr.querySelector('.nodes');
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

    // ── layout: fixed radial hub → category → leaf, keyed by id so positions
    // stay stable across polls (no force simulation needed for a shallow,
    // slowly-changing graph like this). ──────────────────────────────────────
    _layout(nodes) {
      const byId = new Map(nodes.map(n => [n.id, n]));
      const hub = byId.get('hub');
      if (hub) { hub.x = 0; hub.y = 0; }
      const cats = nodes.filter(n => n.kind === 'category');
      const R1 = 110;
      cats.forEach((c, i) => {
        const a = (i / Math.max(1, cats.length)) * Math.PI * 2 - Math.PI / 2;
        c.x = Math.cos(a) * R1; c.y = Math.sin(a) * R1;
      });
      const edgesByCat = {};
      this._edges.forEach(e => { (edgesByCat[e.from] = edgesByCat[e.from] || []).push(e.to); });
      cats.forEach(c => {
        const leafIds = edgesByCat[c.id] || [];
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
      const byId = this._nodes;

      this._edges.forEach(e => {
        const a = byId.get(e.from), b = byId.get(e.to);
        if (!a || !b) return;
        const line = svgEl('line', {
          class: 'edge', x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        });
        this._gEdges.appendChild(line);
      });

      byId.forEach(n => {
        const g = svgEl('g', { class: 'node-g', transform: `translate(${n.x},${n.y})` });
        const r = KIND_R[n.kind] || 6;
        const col = STATUS_COL[n.status] || STATUS_COL.unknown;
        const dot = svgEl('circle', { class: 'dot', r, fill: col, stroke: col, 'fill-opacity': n.kind === 'hub' ? 0.25 : 0.35 });
        g.appendChild(dot);
        if (changed.has(n.id)) {
          const ring = svgEl('circle', { class: 'pulsering', r, stroke: col, style: `--r0:${r}px` });
          g.appendChild(ring);
          ring.addEventListener('animationend', () => ring.remove());
        }
        const label = svgEl('text', { y: r + 11, 'text-anchor': 'middle' });
        label.textContent = short(n.label || n.id, n.kind === 'category' ? 14 : 12);
        g.appendChild(label);
        g.addEventListener('mouseenter', () => this._showTip(n));
        g.addEventListener('mouseleave', () => { this._tip.style.opacity = 0; });
        this._gNodes.appendChild(g);
      });
    }

    _showTip(n) {
      this._tip.textContent = esc(n.label || n.id) + (n.detail ? ' — ' + esc(n.detail) : '') + ' [' + n.status + ']';
      this._tip.style.opacity = 1;
    }
  }

  customElements.define('vera-topology-map', VeraTopologyMap);
})();
