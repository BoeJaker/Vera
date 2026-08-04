/**
 * <vera-ollama-map> — a live node diagram (gpu-250 / cpu-246 / cpu-247 …,
 * whatever ollama.role_profiles.get actually reports) that pulses the REAL
 * node a real LLM call just dispatched to.
 *
 * No bun.com equivalent — this is the single most Vera-specific, session-
 * earned addition. It exists directly because of this session's own root-
 * cause hunt: the sandbox's Ollama calls were silently landing on a CPU
 * node with the wrong model, and that was invisible for hours because
 * nothing showed WHICH node a call actually hit. This makes that failure
 * class visible at a glance instead of requiring a manual
 * llm.route.resolve trace.
 *
 * Data: `ollama.role_profiles.get` (GET /ollama/role_profiles) for the
 * real node list + `in_use` counts, polled every 15s (diff-gated). Live
 * dispatch comes from the real `ollama.request` (phase:"generating")/
 * `.request_done`/`.request_error` WS events — `instance_id` on those IS
 * the actual node a call landed on, not an inference. Routing-drift
 * highlighting polls `evolve.sandbox.status`'s real `routing_drift` field
 * directly (self-contained — doesn't depend on <vera-branch-pipeline>
 * being present) and marks the mismatched node/role pair.
 *
 * Truthful animation: a node box pulses ONLY on a real `ollama.request`
 * generating-phase event for that instance_id — never speculative, never
 * on an unchanged poll tick.
 *
 * Public API: setApiBase(url), refresh()
 */
(function () {
  if (customElements.get('vera-ollama-map')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;font-family:var(--sans,system-ui,sans-serif);font-size:11px;
  color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
.wrap{display:flex;gap:10px;flex-wrap:wrap;position:relative;padding-top:22px}
.origin{position:absolute;top:0;left:0;font-size:9.5px;color:var(--dim2,var(--t3,#8a7e70));
  display:flex;align-items:center;gap:4px}
.origin .odot{width:6px;height:6px;border-radius:50%;background:var(--acc,var(--ac,#5a9e8f))}
.chip{position:absolute;top:0;left:0;width:8px;height:8px;border-radius:50%;pointer-events:none;
  transition:transform .55s cubic-bezier(0.4,0,0.3,1),opacity .55s ease;opacity:1;z-index:5}
.node{flex:1;min-width:130px;border:1px solid var(--border,var(--bd2,#3a3530));border-radius:8px;
  padding:8px 10px;background:var(--bg1,var(--s1,#1f1d1a));position:relative}
.node.gpu{border-top:2px solid var(--acc,var(--ac,#5a9e8f))}
.node.down{opacity:.45}
.node.drift{border-color:var(--warn,var(--ac3,#d9a441));box-shadow:0 0 0 1px var(--warn,var(--ac3,#d9a441)) inset}
.node .nh{display:flex;align-items:center;gap:5px;font-weight:600;margin-bottom:4px}
.node .dot{width:6px;height:6px;border-radius:50%;background:var(--dim,var(--t2,#6a6058))}
.node.up .dot{background:var(--ok,var(--ac2,#6db87a))}
.node.down .dot{background:var(--err,var(--ac4,#c96b6b))}
.node .body{font-size:9.5px;color:var(--dim2,var(--t3,#8a7e70));line-height:1.6}
.node .last{margin-top:4px;font-size:9px;font-family:var(--mono,monospace);
  color:var(--text,var(--t1,#ddd5c8));min-height:12px}
.node .inuse{font-family:var(--mono,monospace);color:var(--text,var(--t1,#ddd5c8))}
.node.dispatching{box-shadow:0 0 0 2px var(--acc,var(--ac,#5a9e8f))}
.warn-chip{margin-top:4px;padding:2px 6px;border-radius:4px;font-size:8.5px;
  background:rgba(217,164,65,.14);color:var(--warn,var(--ac3,#d9a441));display:inline-block}
.empty{padding:14px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
</style>
<div id="body"></div>`;

  class VeraOllamaMap extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._ws = null;
      this._pollTimer = null;
      this._lastSig = '';
      this._nodes = {};
      this._lastCall = {};    // iid -> {model, cap_name, ts, ok}
      this._driftNodes = new Set();
      this._firstRender = true;
    }

    connectedCallback() {
      this._connectWs();
      this.refresh();
      this._pollTimer = setInterval(() => this.refresh(), 15000);
    }

    disconnectedCallback() {
      if (this._pollTimer) clearInterval(this._pollTimer);
      try { this._ws && this._ws.close(); } catch (_) {}
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }

    // Endpoint is /ws/mcp (no bare /ws exists), and the event bus subscription
    // is action:'subscribe_events' — action:'subscribe'+stream:'vera:events'
    // silently matched nothing server-side (emit_event() only checks for the
    // special "__events__" sub key), so this never actually received a single
    // live event. Broadcasts arrive wrapped as {type:'event', data:{...}}.
    _connectWs() {
      try {
        const wsUrl = this._getBase().replace(/^http/, 'ws') + '/ws/mcp';
        this._ws = new WebSocket(wsUrl);
        this._ws.onopen = () => { try { this._ws.send(JSON.stringify({ action: 'subscribe_events' })); } catch (_) {} };
        this._ws.onmessage = e => {
          try {
            const msg = JSON.parse(e.data);
            if (msg && msg.type === 'event') this._onEvent(msg.data);
          } catch (_) {}
        };
        this._ws.onclose = () => { setTimeout(() => this._connectWs(), 3000); };
        this._ws.onerror = () => { try { this._ws.close(); } catch (_) {} };
      } catch (_) { setTimeout(() => this._connectWs(), 5000); }
    }

    _onEvent(ev) {
      const t = ev && ev.type || '';
      if (t === 'ollama.request' && ev.phase === 'generating' && ev.instance_id) {
        this._lastCall[ev.instance_id] = { model: ev.model, cap: ev.cap_name, ts: Date.now(), state: 'active' };
        this._paintDispatch(ev.instance_id, ev.model, ev.cap_name, 'active');
      } else if ((t === 'ollama.request_done' || t === 'ollama.request_error') && ev.instance_id) {
        const ok = t === 'ollama.request_done';
        this._lastCall[ev.instance_id] = {
          model: ev.model, cap: ev.caller_func || ev.cap_name, ts: Date.now(),
          state: ok ? 'done' : 'error', elapsed_s: ev.elapsed_s, tok_per_s: ev.tok_per_s,
        };
        this._paintDispatch(ev.instance_id, ev.model, ev.caller_func || '', ok ? 'done' : 'error');
      }
    }

    _paintDispatch(iid, model, cap, state) {
      const el = this.shadowRoot.querySelector('.node[data-iid="' + CSS.escape(iid) + '"]');
      if (!el) return;
      const lastEl = el.querySelector('.last');
      if (lastEl) {
        lastEl.textContent = (state === 'active' ? '▶ ' : state === 'error' ? '✗ ' : '✓ ') +
          (model || '') + (cap ? ' · ' + cap : '');
      }
      if (state === 'active') {
        el.classList.add('dispatching');
        if (window.veraUI && window.veraUI.pulseOnce) window.veraUI.pulseOnce(el, { color: 'var(--acc,var(--ac,#5a9e8f))' });
        this._flyChip(el, 'var(--acc,#5a9e8f)');
      } else {
        el.classList.remove('dispatching');
        if (window.veraUI && window.veraUI.pulseOnce) {
          window.veraUI.pulseOnce(el, { color: state === 'error' ? 'var(--err,var(--ac4,#c96b6b))' : 'var(--ok,var(--ac2,#6db87a))' });
        }
      }
    }

    // bun.com pattern-3 flourish, replicated: a small glowing chip flies
    // from the dispatch origin to the real node a real call just landed on,
    // using the same cubic-bezier motion bun's terminal->cell flourish uses.
    // Fires ONLY on a real ollama.request generating-phase event — never
    // decorative, never looping.
    _flyChip(targetEl, color) {
      if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      const wrap = this.shadowRoot.querySelector('.wrap');
      const origin = this.shadowRoot.getElementById('origin');
      if (!wrap || !origin) return;
      const wrapRect = wrap.getBoundingClientRect();
      const oRect = origin.getBoundingClientRect();
      const tRect = targetEl.getBoundingClientRect();
      const chip = document.createElement('div');
      chip.className = 'chip';
      chip.style.background = color;
      chip.style.boxShadow = '0 0 9px ' + color;
      const startX = oRect.left - wrapRect.left, startY = oRect.top - wrapRect.top + oRect.height / 2;
      chip.style.transform = 'translate(' + startX + 'px,' + startY + 'px)';
      wrap.appendChild(chip);
      requestAnimationFrame(() => {
        const endX = tRect.left - wrapRect.left + tRect.width / 2;
        const endY = tRect.top - wrapRect.top + tRect.height / 2;
        chip.style.transform = 'translate(' + endX + 'px,' + endY + 'px)';
        chip.style.opacity = '0';
      });
      setTimeout(() => chip.remove(), 650);
    }

    async _fetchJson(path) {
      try { const r = await fetch(this._getBase() + path); return await r.json(); }
      catch (_) { return null; }
    }

    async refresh() {
      const [profD, sbD] = await Promise.all([
        this._fetchJson('/ollama/role_profiles'),
        this._fetchJson('/evolve/sandbox/status'),
      ]);
      const nodes = (profD && profD.nodes) || {};
      this._nodes = nodes;
      this._driftNodes = new Set();
      const drift = sbD && sbD.routing_drift;
      if (drift && Array.isArray(drift.mismatches)) {
        drift.mismatches.forEach(m => {
          if (m && m.node) this._driftNodes.add(m.node);
          if (m && m.prod_node) this._driftNodes.add(m.prod_node);
          if (m && m.sandbox_node) this._driftNodes.add(m.sandbox_node);
        });
      }
      const sig = JSON.stringify(Object.keys(nodes).sort().map(k =>
        [k, nodes[k].status, nodes[k].in_use, nodes[k].enabled]));
      const isNew = sig !== this._lastSig;
      this._lastSig = sig;
      this._render(nodes, isNew);
    }

    _render(nodes, isNew) {
      const body = this.shadowRoot.getElementById('body');
      const ids = Object.keys(nodes);
      if (!ids.length) { body.innerHTML = '<div class="empty">No Ollama nodes registered.</div>'; return; }
      body.innerHTML = '<div class="wrap"><div class="origin" id="origin"><span class="odot"></span>active run dispatch</div>' + ids.map(iid => {
        const n = nodes[iid];
        const up = (n.status || '').toLowerCase() !== 'down' && n.enabled !== false;
        const drift = this._driftNodes.has(iid);
        const last = this._lastCall[iid];
        return '<div class="node ' + (n.has_gpu ? 'gpu' : '') + ' ' + (up ? 'up' : 'down') +
          (drift ? ' drift' : '') + '" data-iid="' + this._esc(iid) + '">' +
          '<div class="nh"><span class="dot"></span><span>' + this._esc(n.label || iid) + '</span></div>' +
          '<div class="body">' + (n.has_gpu ? 'GPU' : 'CPU') + ' · in-use: <span class="inuse">' +
          (n.in_use || 0) + '</span></div>' +
          '<div class="last">' + (last ? this._esc((last.state === 'active' ? '▶ ' : last.state === 'error' ? '✗ ' : '✓ ') + (last.model || '')) : 'idle') + '</div>' +
          (drift ? '<div class="warn-chip">⚠ routing drift</div>' : '') +
          '</div>';
      }).join('') + '</div>';
      this._firstRender = false;
    }

    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  }

  customElements.define('vera-ollama-map', VeraOllamaMap);
})();
