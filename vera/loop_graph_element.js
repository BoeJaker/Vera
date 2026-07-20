/**
 * loop_graph_element.js — <vera-loop-graph>
 * ============================================================================
 * A LIVE activity graph for the Vera agentic loop (v5 and friends). Feed it the
 * same SSE events that drive <vera-agent-loop-output> via `appendEvent(ev)` and
 * it streams nodes/edges in as the loop runs:
 *
 *   run ─▶ step ─▶ (phase sub-agent | sub-plan step) ─▶ cap call ─▶ result
 *          └─▶ skill (satellite)   └─▶ saved file (satellite)
 *
 * Nodes are coloured by status (running / ok / failed). Layout is a stable
 * left→right layered tree (depth from the run root) so nothing jitters as new
 * nodes arrive. Pan = drag, zoom = wheel, click a node for detail.
 *
 * Public API:
 *   el.appendEvent(ev)   — feed one parsed SSE event (same shape as the loop UI)
 *   el.appendEvents(arr) — feed many
 *   el.reset()           — clear the graph
 *   el.fit()             — fit all nodes in view
 *
 * Zero backend dependency — it derives everything from the event stream.
 * Theming uses the standard Vera CSS variables.
 */
(function () {
  'use strict';
  if (window.customElements && window.customElements.get('vera-loop-graph')) return;

  const NS = 'http://www.w3.org/2000/svg';
  const COL_W = 200;       // horizontal spacing per depth
  const ROW_H = 46;        // vertical spacing within a depth
  const MARGIN = 40;

  const KIND_STYLE = {
    run:    { fill: '#1f1d2e', stroke: '#a78bfa', r: 9 },
    step:   { fill: '#1d2630', stroke: '#6b9bd2', r: 8 },
    phase:  { fill: '#1d2620', stroke: '#a8c87a', r: 7 },
    cap:    { fill: '#241d1a', stroke: '#c9955a', r: 6 },
    result: { fill: '#16201c', stroke: '#5a9e8f', r: 4 },
    skill:  { fill: '#1a2420', stroke: '#5a9e8f', r: 4 },
    file:   { fill: '#201c24', stroke: '#c97ad0', r: 5 },
    recon:  { fill: '#1d2230', stroke: '#5aa0c9', r: 6 },
    master: { fill: '#241f2e', stroke: '#c084fc', r: 7 },
    context:{ fill: '#221c2e', stroke: '#a07ec1', r: 6 },
  };
  const STATUS_COL = { running: '#c9a45a', ok: '#5a9e8f', fail: '#c75a5a', idle: '#7a7468' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function short(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

  class VeraLoopGraph extends HTMLElement {
    constructor() {
      super();
      this._sr = this.attachShadow({ mode: 'open' });
      this._nodes = new Map();          // id → node
      this._edges = [];                 // {from,to}
      this._ctxEdges = [];              // {from,to} prior-step → step's context node
      this._showContext = (this.getAttribute('show-context') !== 'false'); // context layer ON by default
      this._order = {};                 // depth → next slot index
      this._raf = 0;
      this._view = { x: 0, y: 0, k: 1 };
      this._drag = null;
      this._orient = (this.getAttribute('orient') === 'horizontal') ? 'horizontal' : 'vertical';
      this._follow = true;          // auto-keep the latest activity in view
      this._lastNodeId = null;      // node touched by the most recent event
      this._build();
    }

    // ── DOM ────────────────────────────────────────────────────────────────
    _build() {
      this._sr.innerHTML = `
        <style>
          :host{display:block;width:100%;height:100%;min-height:160px;position:relative;
                font-family:var(--mono,monospace);background:var(--bg0,#15140f)}
          .wrap{position:absolute;inset:0;overflow:hidden}
          svg{width:100%;height:100%;display:block;cursor:grab;background:
              radial-gradient(circle at 1px 1px, rgba(255,255,255,.03) 1px, transparent 0) 0 0/22px 22px}
          svg.drag{cursor:grabbing}
          .edge{fill:none;stroke:var(--border,#3a3530);stroke-width:1.2;opacity:.55}
          .node{cursor:pointer}
          .node text{font-size:9px;fill:var(--text2,#bfb6a8);pointer-events:none}
          .node .ic{font-size:8px;fill:var(--dim,#a89f92);pointer-events:none}
          .node.sel circle,.node.sel rect{stroke-width:2.5px}
          /* context layer: dashed link from a step to the exact context it got,
             plus cross-links from the prior steps that fed that context. */
          .ctxedge{fill:none;stroke:#a07ec1;stroke-width:1.2;stroke-dasharray:3 3;opacity:.5}
          .pulsering{transform-box:fill-box;transform-origin:center;animation:lgPulse 1.15s ease-in-out infinite}
          .ctxedge.pulse{animation:lgEdgePulse 1.15s ease-in-out infinite}
          @keyframes lgPulse{0%,100%{opacity:.15;r:9}50%{opacity:.6;r:15}}
          @keyframes lgEdgePulse{0%,100%{opacity:.25}50%{opacity:.9}}
          .bar{position:absolute;top:0;left:0;right:0;display:flex;gap:6px;align-items:center;
               padding:4px 8px;font-size:9.5px;color:var(--dim,#a89f92);
               background:linear-gradient(var(--bg0,#15140f),transparent);z-index:2;pointer-events:none}
          .bar b{color:var(--acc2,#a8c87a)}
          .bar .btns{margin-left:auto;display:flex;gap:4px;pointer-events:auto}
          button{font:inherit;font-size:9px;background:var(--bg1,#1f1d1a);color:var(--text2,#bfb6a8);
                  border:1px solid var(--border,#3a3530);border-radius:3px;padding:1px 6px;cursor:pointer}
          button:hover{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}
          button.on{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);background:rgba(90,158,143,.12)}
          .legend{position:absolute;bottom:4px;left:8px;display:flex;flex-wrap:wrap;gap:6px;
                  font-size:8px;color:var(--dim,#a89f92);z-index:2;pointer-events:none}
          .legend i{display:inline-block;width:7px;height:7px;border-radius:2px;margin-right:2px;vertical-align:middle}
          .detail{position:absolute;top:26px;right:6px;width:230px;max-height:70%;overflow:auto;
                  background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:4px;
                  padding:6px 8px;font-size:9px;color:var(--text2,#bfb6a8);display:none;z-index:3}
          .detail.show{display:block}
          .detail h4{margin:0 0 4px;font-size:9.5px;color:var(--acc2,#a8c87a);word-break:break-all}
          .detail pre{margin:4px 0 0;white-space:pre-wrap;word-break:break-word;font-size:8.5px;
                      color:var(--dim,#a89f92);max-height:200px;overflow:auto}
          .detail .x{float:right;cursor:pointer;color:var(--dim2,#777)}
          .empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
                 color:var(--dim,#a89f92);font-size:10px;text-align:center}
        </style>
        <div class="wrap">
          <svg part="svg"><g class="cam"><g class="edges"></g><g class="ctxedges"></g><g class="nodes"></g></g></svg>
          <div class="bar"><b data-part="title">Activity graph</b>
            <span data-part="stat"></span>
            <span class="btns"><button data-act="orient" title="Toggle vertical / horizontal layout">Vertical</button><button data-act="follow" class="on" title="Keep the newest activity in view">Follow</button><button data-act="fit">Fit</button><button data-act="reset">Clear</button></span>
          </div>
          <div class="legend">
            <span><i style="background:#a78bfa"></i>run</span>
            <span><i style="background:#6b9bd2"></i>step</span>
            <span><i style="background:#a8c87a"></i>phase</span>
            <span><i style="background:#c9955a"></i>cap</span>
            <span><i style="background:#5a9e8f"></i>result/skill</span>
            <span><i style="background:#c97ad0"></i>file</span>
            <span><i style="background:#a07ec1"></i>context</span>
          </div>
          <div class="empty" data-part="empty">Waiting for an agentic run…</div>
          <div class="detail" data-part="detail"></div>
        </div>`;
      this._svg = this._sr.querySelector('svg');
      this._cam = this._sr.querySelector('.cam');
      this._gEdges = this._sr.querySelector('.edges');
      this._gCtxEdges = this._sr.querySelector('.ctxedges');
      this._gNodes = this._sr.querySelector('.nodes');
      this._detail = this._sr.querySelector('[data-part="detail"]');

      // Context layer is ON by default now (no toggle button) — each step's exact
      // context shows as a linked, pulsing layer. Host can opt out with show-context="false".
      this._btnOrient = this._sr.querySelector('[data-act="orient"]');
      this._btnFollow = this._sr.querySelector('[data-act="follow"]');
      this._btnOrient.textContent = (this._orient === 'vertical') ? 'Vertical' : 'Horizontal';
      this._btnOrient.onclick = () => {
        this._orient = (this._orient === 'vertical') ? 'horizontal' : 'vertical';
        this._btnOrient.textContent = (this._orient === 'vertical') ? 'Vertical' : 'Horizontal';
        this._render(); this.fit();
      };
      this._btnFollow.onclick = () => {
        this._follow = !this._follow;
        this._btnFollow.classList.toggle('on', this._follow);
        if (this._follow) this._focusLatest(true);
      };
      this._sr.querySelector('[data-act="fit"]').onclick = () => this.fit();
      this._sr.querySelector('[data-act="reset"]').onclick = () => this.reset();

      // pan (manual interaction turns Follow off so the view doesn't fight the user)
      this._svg.addEventListener('mousedown', e => {
        if (e.target.closest('.node')) return;
        this._setFollow(false);
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
      // zoom
      this._svg.addEventListener('wheel', e => {
        e.preventDefault();
        this._setFollow(false);
        const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        const r = this._svg.getBoundingClientRect();
        const mx = e.clientX - r.left, my = e.clientY - r.top;
        this._view.x = mx - (mx - this._view.x) * f;
        this._view.y = my - (my - this._view.y) * f;
        this._view.k = Math.max(0.2, Math.min(3, this._view.k * f));
        this._applyCam();
      }, { passive: false });
    }

    _applyCam() {
      this._cam.setAttribute('transform',
        `translate(${this._view.x},${this._view.y}) scale(${this._view.k})`);
    }

    _setFollow(on) {
      if (this._follow === on) return;
      this._follow = on;
      if (this._btnFollow) this._btnFollow.classList.toggle('on', on);
    }

    // Keep the latest-touched node comfortably in view. Only pans when it has
    // drifted near/outside the edge, so a visible node doesn't cause jitter.
    _focusLatest(force) {
      if ((!this._follow && !force) || !this._lastNodeId) return;
      const n = this._nodes.get(this._lastNodeId);
      if (!n) return;
      const r = this._svg.getBoundingClientRect();
      if (!r.width) return;
      const k = this._view.k;
      const sx = n.x * k + this._view.x, sy = n.y * k + this._view.y;
      const padX = Math.min(180, r.width * 0.32), padY = Math.min(120, r.height * 0.32);
      let moved = false;
      if (force || sx < padX || sx > r.width - padX) { this._view.x = r.width * 0.55 - n.x * k; moved = true; }
      if (force || sy < padY + 24 || sy > r.height - padY) { this._view.y = r.height * 0.5 - n.y * k; moved = true; }
      if (moved) this._applyCam();
    }

    // Context nodes only participate (layout + render) when the optional context
    // layer is toggled on; everything else is always visible.
    _visible(n) { return this._showContext || !n || n.kind !== 'context'; }

    // ── model ────────────────────────────────────────────────────────────
    reset() {
      this._nodes.clear(); this._edges = []; this._ctxEdges = []; this._order = {};
      this._view = { x: 0, y: 0, k: 1 }; this._sel = null; this._lastNodeId = null;
      this._setFollow(true);
      this._detail.classList.remove('show');
      this._render();
    }

    _node(id, kind, label, extra) {
      let n = this._nodes.get(id);
      if (!n) {
        const parent = (extra && extra.parent) || null;
        const depth = parent && this._nodes.get(parent) ? this._nodes.get(parent).depth + 1 : 0;
        const slot = (this._order[depth] = (this._order[depth] || 0)); this._order[depth]++;
        n = { id, kind, label: label || id, status: (extra && extra.status) || 'idle',
              detail: (extra && extra.detail) || '', parent, depth, x: 0, y: 0, _slot: slot };
        this._nodes.set(id, n);
        if (parent && this._nodes.get(parent)) this._edges.push({ from: parent, to: id });
      } else {
        if (label) n.label = label;
        if (extra && extra.status) n.status = extra.status;
        if (extra && extra.detail) n.detail = extra.detail;
      }
      return n;
    }

    // step_id → graph node id. Phase/sub-plan ids (>=100) carry their parent as
    // floor(id/100); top-level step ids (<100) hang off the run root.
    _stepNode(stepId, ev) {
      const id = 's' + stepId;
      const parent = (stepId >= 100) ? ('s' + Math.floor(stepId / 100)) : 'run';
      const kind = (ev && ev.phase) ? 'phase' : 'step';
      const label = (ev && ev.phase) ? (ev.phase) : short((ev && ev.title) || ('step ' + stepId), 26);
      const n = this._node(id, kind, label, { parent, status: 'running',
        detail: (ev && (ev.goal || ev.title)) || '' });
      if (ev && ev.phase) n.kind = 'phase';
      return n;
    }

    appendEvents(arr) { (arr || []).forEach(e => this.appendEvent(e)); }

    appendEvent(ev) {
      if (!ev || typeof ev !== 'object') return;
      const t = ev.type || '';
      try { this._consume(t, ev); } catch (_) { /* never break the host */ }
      this._schedule();
    }

    _consume(t, ev) {
      // The chat wrapper emits BOTH `start` and `*.triage_start` per run — reset
      // on `start` only; treat triage_start as a goal-change check so re-runs
      // (which may have no `start`) still clear.
      if (t === 'start') {
        this.reset();
        this._node('run', 'run', short(ev.goal || 'run', 30), { status: 'running', detail: ev.goal || '' });
        return;
      }
      if (t.endsWith('.triage_start')) {
        const r = this._nodes.get('run');
        if (!r) this._node('run', 'run', short(ev.goal || 'run', 30), { status: 'running', detail: ev.goal || '' });
        else if (ev.goal && r.detail && ev.goal !== r.detail) {
          this.reset();
          this._node('run', 'run', short(ev.goal, 30), { status: 'running', detail: ev.goal });
        }
        return;
      }
      if (!this._nodes.has('run')) this._node('run', 'run', short(ev.goal || 'run', 30), { status: 'running' });

      // NOTE: match by SUFFIX (.master_plan / .plan / …) not exact 'agent_loop_v5.*'
      // so the v6/v7 engines — which emit agent_loop_v6.* and are what long-term /
      // strategic planning runs on — render too (this was the "graph not functioning
      // in long-term planning" bug). '.plan' does NOT match '.master_plan' (that ends
      // with '_plan', no dot) or '.subplan', so the ordered checks stay correct.
      if (t.endsWith('.master_plan')) {
        this._node('master', 'master', 'master plan', { parent: 'run', status: 'ok',
          detail: (ev.persona ? '[' + ev.persona + ']\n\n' : '') + (ev.long_form || '') });
        return;
      }
      if (t.endsWith('.recon')) {
        const nid = 'recon:' + (ev.cap || 'x');
        if (ev.phase === 'done') {
          const n = this._nodes.get(nid); if (n) { n.status = ev.ok ? 'ok' : 'fail'; n.detail = ev.preview || n.detail; }
        } else {
          this._node(nid, 'recon', short(ev.cap || 'recon', 22), { parent: 'run', status: 'running',
            detail: ev.why || '' });
        }
        this._lastNodeId = nid;
        return;
      }
      // Plan (v5/v6/v7) AND each piecewise master-plan piece → pre-create step nodes
      // so long-term/strategic runs show their structure as pieces are planned.
      if (t.endsWith('.plan') || t.endsWith('.master_plan_piece_planned')) {
        (ev.steps || []).forEach(s => {
          const n = this._stepNode(s.id, { title: s.title });
          if (n.status !== 'running' && n.status !== 'ok' && n.status !== 'fail') n.status = 'idle';
          if (s.complex && !String(n.label).startsWith('⧉')) n.label = '⧉ ' + n.label;
          (s.skills || []).forEach(sk => this._node('sk:' + s.id + ':' + sk, 'skill', short(sk, 14),
            { parent: 's' + s.id, status: 'ok' }));
        });
        return;
      }
      if (t.endsWith('.subplan')) {
        (ev.steps || []).forEach(s => this._stepNode(s.id, { title: s.title }));
        return;
      }
      if (t === 'agent_loop_v5.step_start' || t.endsWith('.step_start')) {
        this._chainState = null;   // any prior chain ends at a step boundary
        const n = this._stepNode(ev.step_id, ev);
        n.status = 'running';
        this._lastNodeId = 's' + ev.step_id;
        (ev.skills || []).forEach(sk => this._node('sk:' + ev.step_id + ':' + sk, 'skill', short(sk, 14),
          { parent: 's' + ev.step_id, status: 'ok' }));
        return;
      }
      // The EXACT context a step's specialist was given → its own linked layer.
      if (t.endsWith('.step_context')) {
        const sid = 's' + ev.step_id, cid = 'ctx:' + ev.step_id;
        const p = ev.parts || {};
        const bits = [];
        if (p.caps && p.caps.length) bits.push('caps: ' + p.caps.join(', '));
        if (p.skills && p.skills.length) bits.push('skills: ' + p.skills.join(', '));
        if (p.context_sources && p.context_sources.length)
          bits.push('context from steps: ' + p.context_sources.map(s => s.step_id).join(', '));
        const detail = (bits.length ? '── layers ──\n' + bits.join('\n') + '\n\n' : '')
          + '── exact system prompt ──\n' + (ev.system_prompt || '');
        const sn = this._nodes.get(sid);
        const n = this._node(cid, 'context', 'context', { parent: sid, status: 'ok', detail });
        n.pulse = !!(sn && sn.status === 'running');   // pulse while the step is using it
        // Cross-link: the prior steps whose output became this step's context.
        (p.context_sources || []).forEach(s => {
          const src = 's' + s.step_id;
          if (this._nodes.has(src)) this._ctxEdges.push({ from: src, to: cid });
        });
        return;
      }
      if (t === 'agent_loop_v5.step_done' || t.endsWith('.step_done')) {
        const n = this._nodes.get('s' + ev.step_id);
        if (n) { n.status = ev.ok ? 'ok' : 'fail'; if (ev.summary) n.detail = ev.summary; }
        const cn = this._nodes.get('ctx:' + ev.step_id);
        if (cn) cn.pulse = false;   // step finished — stop pulsing its context
        this._lastNodeId = 's' + ev.step_id;
        return;
      }
      if (t.endsWith('.scope_widened')) {
        const n = this._nodes.get('s' + ev.step_id);
        if (n) n.detail = (n.detail || '') + '\n[scope+: ' + (ev.added || []).join(', ') + ']';
        return;
      }
      if (t.endsWith('.code_saved')) {
        (ev.files || []).forEach(f => this._node('file:' + f.path + ':' + f.version, 'file',
          short(f.path, 18) + ' v' + f.version, { parent: 's' + ev.step_id, status: 'ok',
            detail: f.path + ' v' + f.version + ' (' + (f.bytes || 0) + 'b)' + (f.gitea ? '\n' + f.gitea : '') }));
        if (this._nodes.has('s' + ev.step_id)) this._lastNodeId = 's' + ev.step_id;
        return;
      }
      // Agent-authored cap CHAIN (a DAG run in one turn): the next N cap nodes are
      // the pipeline hops. We nest them (hop1 under hop0 …) so output→input flow
      // reads as a linked sub-graph under the step, reusing the parent-edge render.
      if (t.endsWith('.chain')) {
        this._chainState = { left: (ev.hops || []).length || 8, prev: null };
        return;
      }
      // A conditionally-skipped chain hop emits no tool_call — keep the nesting
      // counter aligned so the hop AFTER it still chains correctly.
      if (t.endsWith('.chain_skip')) {
        if (this._chainState && this._chainState.left > 0) {
          if (--this._chainState.left <= 0) this._chainState = null;
        }
        return;
      }
      if (t.endsWith('.tool_call')) {
        let parent = this._nodes.has('s' + ev.step_id) ? 's' + ev.step_id : 'run';
        let inChain = false;
        if (this._chainState && this._chainState.left > 0) {
          inChain = true;
          if (this._chainState.prev) parent = this._chainState.prev;   // nest under the previous hop
          this._chainState.prev = 'cap:' + ev.cycle;
          if (--this._chainState.left <= 0) this._chainState = null;
        }
        this._node('cap:' + ev.cycle, 'cap', (inChain ? '🔗 ' : '') + short(ev.tool || 'cap', 20), { parent, status: 'running',
          detail: (ev.thought ? ev.thought + '\n\n' : '') + 'args: ' + JSON.stringify(ev.args || {}, null, 1) });
        this._lastNodeId = 'cap:' + ev.cycle;
        return;
      }
      if (t.endsWith('.tool_done')) {
        this._lastNodeId = 'cap:' + ev.cycle;
        const n = this._nodes.get('cap:' + ev.cycle);
        if (n) {
          n.status = ev.ok ? 'ok' : 'fail';
          if (ev.tool && n.label === 'cap') n.label = short(ev.tool, 22);
          n.detail = (n.detail || '') + '\n\nresult: ' + short(ev.preview || ev.error || '', 600);
          // a small result leaf so "results from caps" are nodes too — labelled
          // with the actual output snippet, not just ok/err (status colours it).
          const snip = String(ev.preview || ev.error || '').replace(/\s+/g, ' ').trim();
          this._node('res:' + ev.cycle, 'result', short(snip, 26) || (ev.ok ? 'ok' : 'err'),
            { parent: 'cap:' + ev.cycle, status: ev.ok ? 'ok' : 'fail',
              detail: short(ev.preview || ev.error || '', 800) });
        }
        return;
      }
      if (t.endsWith('.done')) {
        const r = this._nodes.get('run'); if (r) r.status = ev.error ? 'fail' : 'ok';
        return;
      }
    }

    // ── layout + render ──────────────────────────────────────────────────
    _schedule() { if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = 0; this._render(); }); }

    _layout() {
      // Tidy layered tree: one axis = depth (the layer), the other = a subtree-
      // packed slot so a node's CHILDREN nest under IT rather than every node at a
      // depth sharing one global line (which made deep branches "fan out" on a
      // single layer). VERTICAL flows top→bottom; HORIZONTAL flows left→right.
      const vert = this._orient === 'vertical';
      const layerGap = vert ? 78 : COL_W;     // distance between layers (depth)
      const slotGap = vert ? 172 : ROW_H;     // distance between sibling subtrees

      // Build parent→children from the LIVE parent links so depth + ordering are
      // derived here, not frozen when the node was first seen (robust to a child
      // event arriving before its parent's).
      const kids = new Map();
      const roots = [];
      for (const n of this._nodes.values()) {
        if (!this._visible(n)) continue;   // hidden context layer: don't reserve slots
        if (n.parent && this._nodes.has(n.parent)) {
          let a = kids.get(n.parent); if (!a) kids.set(n.parent, a = []); a.push(n);
        } else {
          roots.push(n);
        }
      }
      const bySlot = (a, b) => a._slot - b._slot;
      roots.sort(bySlot);
      for (const a of kids.values()) a.sort(bySlot);

      // True depth from the root chain, so a deep branch actually descends instead
      // of collapsing onto an early layer.
      for (const r of roots) {
        const stack = [[r, 0]];
        while (stack.length) {
          const [n, d] = stack.pop();
          n.depth = d;
          const cs = kids.get(n.id);
          if (cs) for (const c of cs) stack.push([c, d + 1]);
        }
      }

      // Cross-axis position by leaf packing: each leaf takes the next slot; each
      // parent is centred over its children → subtrees stay together and nest.
      let leaf = 0;
      const place = (n) => {
        const cs = kids.get(n.id);
        if (!cs || !cs.length) { n._cross = leaf++; return n._cross; }
        let sum = 0; for (const c of cs) sum += place(c);
        n._cross = sum / cs.length; return n._cross;
      };
      for (const r of roots) place(r);

      for (const n of this._nodes.values()) {
        const along = MARGIN + n.depth * layerGap;
        const cross = MARGIN + (n._cross || 0) * slotGap;
        if (vert) { n.y = along; n.x = cross; } else { n.x = along; n.y = cross; }
      }
    }

    _render() {
      const empty = this._sr.querySelector('[data-part="empty"]');
      if (empty) empty.style.display = this._nodes.size ? 'none' : 'flex';
      const stat = this._sr.querySelector('[data-part="stat"]');
      if (stat) {
        let caps = 0, ok = 0, fail = 0;
        for (const n of this._nodes.values()) { if (n.kind === 'cap') { caps++; if (n.status === 'ok') ok++; else if (n.status === 'fail') fail++; } }
        stat.textContent = `${this._nodes.size} nodes · ${caps} caps · ${ok}✓ ${fail}✗`;
      }
      this._layout();

      // edges (curve bias follows the flow axis) — derived from each node's LIVE
      // parent link so a child that arrived before its parent still gets wired.
      const vert = this._orient === 'vertical';
      let ep = '';
      for (const b of this._nodes.values()) {
        if (!b.parent || !this._visible(b)) continue;
        const a = this._nodes.get(b.parent);
        if (!a || !this._visible(a)) continue;
        let d;
        if (vert) { const my = (a.y + b.y) / 2; d = `M ${a.x} ${a.y} C ${a.x} ${my}, ${b.x} ${my}, ${b.x} ${b.y}`; }
        else { const mx = (a.x + b.x) / 2; d = `M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`; }
        ep += `<path class="edge" d="${d}"/>`;
      }
      this._gEdges.innerHTML = ep;

      // context cross-edges: prior-step → this step's context node. Pulse the
      // edge while the context node is actively pulsing (its step is running).
      let cep = '';
      if (this._showContext) {
        for (const e of this._ctxEdges) {
          const a = this._nodes.get(e.from), b = this._nodes.get(e.to);
          if (!a || !b) continue;
          let d;
          if (vert) { const my = (a.y + b.y) / 2; d = `M ${a.x} ${a.y} C ${a.x} ${my}, ${b.x} ${my}, ${b.x} ${b.y}`; }
          else { const mx = (a.x + b.x) / 2; d = `M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`; }
          cep += `<path class="ctxedge${b.pulse ? ' pulse' : ''}" d="${d}"/>`;
        }
      }
      this._gCtxEdges.innerHTML = cep;

      // nodes
      let np = '';
      for (const n of this._nodes.values()) {
        if (!this._visible(n)) continue;
        const st = KIND_STYLE[n.kind] || KIND_STYLE.cap;
        const ring = STATUS_COL[n.status] || st.stroke;
        const r = st.r;
        const sel = (this._sel === n.id) ? ' sel' : '';
        const spin = n.status === 'running'
          ? `<circle cx="${n.x}" cy="${n.y}" r="${r + 3}" fill="none" stroke="${ring}" stroke-width="1" opacity=".5"/>` : '';
        // pulse ring — the ACTIVE step, the ACTIVE cap, and the context layer a
        // running step is consuming all pulse (n.pulse is set on context nodes;
        // any node still 'running' pulses too).
        const pulse = (n.pulse || n.status === 'running')
          ? `<circle class="pulsering" cx="${n.x}" cy="${n.y}" r="9" fill="none" stroke="${st.stroke}" stroke-width="1.4"/>` : '';
        np += `<g class="node${sel}" data-id="${esc(n.id)}">${spin}${pulse}` +
          `<circle cx="${n.x}" cy="${n.y}" r="${r}" fill="${st.fill}" stroke="${ring}" stroke-width="1.6"/>` +
          `<text x="${n.x + r + 4}" y="${n.y + 3}">${esc(short(n.label, 28))}</text></g>`;
      }
      this._gNodes.innerHTML = np;
      this._gNodes.querySelectorAll('.node').forEach(g => {
        g.addEventListener('click', e => { e.stopPropagation(); this._select(g.getAttribute('data-id')); });
      });
      this._applyCam();
      this._focusLatest(false);
    }

    _select(id) {
      this._sel = id;
      const n = this._nodes.get(id);
      if (!n) { this._detail.classList.remove('show'); this._render(); return; }
      this._detail.innerHTML = `<span class="x" data-x="1">✕</span>` +
        `<h4>${esc(n.kind)} · ${esc(n.label)}</h4>` +
        `<div>status: <b style="color:${STATUS_COL[n.status] || '#999'}">${esc(n.status)}</b></div>` +
        (n.detail ? `<pre>${esc(n.detail)}</pre>` : '');
      this._detail.classList.add('show');
      this._detail.querySelector('[data-x]').onclick = () => { this._sel = null; this._detail.classList.remove('show'); this._render(); };
      this._render();
    }

    fit() {
      if (!this._nodes.size) return;
      let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
      for (const n of this._nodes.values()) {
        minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x + 140);
        minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
      }
      const r = this._svg.getBoundingClientRect();
      const w = Math.max(50, maxX - minX), h = Math.max(50, maxY - minY);
      const k = Math.max(0.2, Math.min(2, Math.min((r.width - 40) / w, (r.height - 60) / h)));
      this._view.k = k;
      this._view.x = 20 - minX * k;
      this._view.y = 36 - minY * k;
      this._applyCam();
    }
  }

  customElements.define('vera-loop-graph', VeraLoopGraph);
})();
