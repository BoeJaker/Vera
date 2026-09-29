/**
 * <vera-branch-pipeline> — the git branch → worktree → sandbox test → gate →
 * promote pipeline, visualized. Two modes:
 *
 *   mode="detail" branch="loop-lab/fix-a"   — one pipeline's real stage-by-
 *     stage progress as a horizontal rail (bun.com's "adversarial review
 *     flow", extended: 4 real Vera stages instead of 3 narrative ones, gate
 *     numbers shown live, a routing-drift warning chip, role tags on who/
 *     what acted at each stage).
 *
 *   mode="lanes"   — every branch with a real Loop Lab pipeline, one lane
 *     each, run-blocks colored by real outcome, chronological (bun.com's CI
 *     race chart). Blocks are clickable — dispatches `branchpipe:openpipeline`
 *     so a host page can switch a detail-mode instance to that pipeline.
 *
 * Truthful animation: `.stage.active` pulses via window.veraUI.pulseOnce
 * ONLY when a real evolve.pipeline.stage/.gate/.done event lands for the
 * pipeline currently shown (WS-driven), plus a 10s poll fallback that is
 * diff-gated (see _sig()) so a poll tick with no real change never repaints,
 * let alone pulses. Idle = zero motion, exactly like activity_timeline
 * _element.js's existing convention.
 *
 * Data sources (see documentation/36-agentic-loop-v7-evaluation.md for how
 * these were found/built): ide.git.branches, evolve.pipeline.list/.get,
 * evolve.sandbox.status (routing_drift). No backend join exists between
 * them — joined here client-side by branch name.
 *
 * Public API:
 *   el.setMode('detail'|'lanes')
 *   el.setBranch(name)        — detail mode: which branch's latest pipeline to show
 *   el.setPipelineId(id)      — detail mode: pin to one specific pipeline record
 *   el.refresh()
 * Events dispatched: branchpipe:openpipeline {detail:{id,branch}}
 */
(function () {

  /* ONE event socket per page. Every element used to open its own /ws
     (seven on the Loop Lab panel, 2026-09-10) and reconnect on its own clock.
     Now: the page's own bus if it has one (window._veraSubscribe, which the
     panel exposes), else the parent frame's, else one shared socket on
     window.__veraEventsBus that every element subscribes to. Returns an
     unsubscribe function. */
  function veraSharedEvents(base, fn) {
    try {
      if (typeof window._veraSubscribe === 'function') { const off = window._veraSubscribe(fn); return typeof off === 'function' ? off : () => {}; }
      const par = window.parent;
      if (par && par !== window && typeof par._veraSubscribe === 'function') { par._veraSubscribe(fn); return () => {}; }
    } catch (_) {}
    const w = window;
    if (!w.__veraEventsBus) {
      const bus = { subs: new Set(), ws: null, base: base };
      const connect = () => {
        try {
          const ws = new WebSocket(bus.base.replace(/^http/, 'ws') + '/ws');
          ws.onopen = () => { try { ws.send(JSON.stringify({ action: 'subscribe', stream: 'vera:events' })); } catch (_) {} };
          ws.onmessage = e => { let ev; try { ev = JSON.parse(e.data); } catch (_) { return; } bus.subs.forEach(s => { try { s(ev); } catch (_) {} }); };
          ws.onclose = () => { bus.ws = null; setTimeout(connect, 3000); };
          ws.onerror = () => { try { ws.close(); } catch (_) {} };
          bus.ws = ws;
        } catch (_) { setTimeout(connect, 5000); }
      };
      bus.subscribe = s => { bus.subs.add(s); if (!bus.ws) connect(); return () => bus.subs.delete(s); };
      w.__veraEventsBus = bus;
    }
    return w.__veraEventsBus.subscribe(fn);
  }
  if (customElements.get('vera-branch-pipeline')) return;

  const STAGES = [
    { id: 'implement', label: 'Implement', icon: '🛠' },
    { id: 'sandbox_test', label: 'Sandbox test', icon: '▣' },
    { id: 'gate', label: 'Gate', icon: '🔍' },
    { id: 'promote', label: 'Promote', icon: '⇪' },
  ];

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;font-family:var(--sans,system-ui,sans-serif);font-size:11px;
  color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
.hdr{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.hdr .branch{font-family:var(--mono,monospace);color:var(--acc,var(--ac,#5a9e8f))}
.hdr .decision{font-size:9px;padding:2px 7px;border-radius:9px;border:1px solid var(--border,var(--bd2,#3a3530))}
.hdr .decision.promoted{border-color:var(--ok,var(--ac2,#6db87a));color:var(--ok,var(--ac2,#6db87a))}
.hdr .decision.held{border-color:var(--warn,var(--ac3,#d9a441));color:var(--warn,var(--ac3,#d9a441))}
.hdr .decision.pending{border-color:var(--acc,var(--ac,#5a9e8f));color:var(--acc,var(--ac,#5a9e8f))}
/* ── detail: adversarial-review chat-bubble flow (bun.com pattern 2,
   replicated visually: avatar circle + bordered message bubble per stage,
   staggered one-shot reveal, real gate-fail flash) — applied to Vera's
   real 4-stage pipeline instead of bun's 3 narrative bug-report examples,
   and live (WS-driven) instead of a browsable static history. */
.flow{display:flex;flex-direction:column;gap:10px}
.msg{display:flex;gap:9px;align-items:flex-start}
.msg.enter{animation:bpEnter .4s ease-out both}
@keyframes bpEnter{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
.msg .av{width:26px;height:26px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;
  justify-content:center;font-size:13px;border:2px solid var(--dim,var(--t2,#6a6058));
  background:var(--bg2,var(--s2,#272421));color:var(--dim,var(--t2,#6a6058))}
.msg .av.implement{background:rgba(251,240,223,.12);border-color:#fbf0df;color:#fbf0df}
.msg .av.reviewer{background:rgba(217,119,87,.16);border-color:#d97757;color:#d97757}
.msg .av.done{border-color:var(--ok,var(--ac2,#6db87a));color:var(--ok,var(--ac2,#6db87a))}
.bubble{flex:1;min-width:0;border-radius:9px;padding:8px 11px;
  border:1px solid var(--border,var(--bd2,#3a3530));background:var(--bg1,var(--s1,#1f1d1a));opacity:.5}
.bubble.active,.bubble.done,.bubble.failed{opacity:1}
.bubble.done{border-color:rgba(74,222,128,.5);background:rgba(74,222,128,.06)}
.bubble.active{border-color:var(--acc,var(--ac,#5a9e8f));box-shadow:0 0 0 1px var(--acc,var(--ac,#5a9e8f)) inset}
.bubble.failed{border-color:rgba(239,68,68,.5);background:rgba(239,68,68,.07)}
.bubble.flash{animation:bpFlash 1.1s ease-out}
@keyframes bpFlash{0%{background:rgba(239,68,68,.4)}100%{background:rgba(239,68,68,.07)}}
.bubble .bh{display:flex;align-items:center;gap:6px;font-weight:600;margin-bottom:3px;flex-wrap:wrap}
.bubble .bh .dot{width:6px;height:6px;border-radius:50%;background:var(--dim,var(--t2,#6a6058))}
.bubble.active .bh .dot{background:var(--acc,var(--ac,#5a9e8f))}
.bubble.done .bh .dot{background:var(--ok,var(--ac2,#6db87a))}
.bubble.failed .bh .dot{background:var(--err,var(--ac4,#c96b6b))}
.bubble .bmsg{font-size:10.5px;line-height:1.55;color:var(--dim2,var(--t3,#8a7e70))}
.bubble .num{color:var(--text,var(--t1,#ddd5c8));font-family:var(--mono,monospace)}
.bubble .commit{margin-top:5px;font-size:9px;font-family:var(--mono,monospace);color:var(--dim,var(--t2,#6a6058))}
.warn-chip{margin-top:5px;padding:2px 6px;border-radius:4px;font-size:8.5px;
  background:rgba(217,164,65,.14);color:var(--warn,var(--ac3,#d9a441));display:inline-block}
.empty{padding:14px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
/* ── lanes: CI race chart (bun.com pattern 4), real SVG lanes with a real
   sweeping scrub-line replay, exactly like <vera-test-activity-timeline>'s
   sweep mechanics, applied to real pipeline history per branch. ── */
.lanewrap{display:flex;flex-direction:column;gap:3px}
.lanerow{display:flex;align-items:center;gap:8px}
.lanerow .name{width:150px;flex-shrink:0;font-family:var(--mono,monospace);font-size:9.5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--dim2,var(--t3,#8a7e70))}
.lanerow svg{flex:1;height:20px;display:block;cursor:pointer}
.blk{transition:opacity .25s ease}
.blk.dim{opacity:.12}
/* a pipeline genuinely still running is a real ongoing state — same
   truthful-ongoing pulse convention as <vera-test-activity-timeline>'s
   current-hour marker. Everything else stays still until real data changes. */
@keyframes bpLive{0%,100%{opacity:1}50%{opacity:.4}}
.livering{animation:bpLive 1.4s ease-in-out infinite}
.lanerow .streak{font-size:8.5px;color:var(--dim2,var(--t3,#8a7e70));flex-shrink:0;width:52px;text-align:right}
.raceclock{font-family:var(--mono,monospace);font-size:9.5px;color:var(--acc,var(--ac,#5a9e8f));margin-left:auto}
</style>
<div class="hdr" id="hdr"></div>
<div id="body"></div>`;

  class VeraBranchPipeline extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._mode = 'lanes';
      this._branch = '';
      this._pipelineId = '';
      this._base = '';
      this._ws = null;
      this._pollTimer = null;
      this._lastSig = '';
      this._rec = null;         // detail mode's current pipeline record
      this._lanes = [];         // lanes mode's joined data
      this._firstRender = true; // suppress pulse on initial history load, not just idle polls
      this._prevStates = null;  // detail mode: per-stage state on the last real render (stagger/flash gating)
      this._playing = false;    // lanes mode: race-chart scrub replay
      this._playRaf = null;
    }

    connectedCallback() {
      this._mode = this.getAttribute('mode') || 'lanes';
      this._branch = this.getAttribute('branch') || '';
      this._pipelineId = this.getAttribute('pipeline-id') || '';
      this._connectWs();
      if (this._onScreen()) this.refresh();
      this._pollTimer = setInterval(() => { if (this._onScreen() && !document.hidden) this.refresh(); }, 30000);   // was 10000 ms, and ran in a background tab
    }

    /* Poll only while on screen. offsetParent is null inside a hidden
       section (display:none), so an element on a page that is not showing
       costs nothing; the panel's nav() refreshes it when its page opens. */
    // offsetParent alone misses a closed <details>: Chromium keeps its contents
    // laid out (content-visibility: hidden) for find-in-page, so an element
    // in a folded card kept polling (found 2026-09-10 on Mission control).
    // checkVisibility() sees content-visibility; older browsers fall back.
    _onScreen() { return this.offsetParent !== null && (typeof this.checkVisibility !== 'function' || this.checkVisibility()); }
    disconnectedCallback() {
      if (this._pollTimer) clearInterval(this._pollTimer);
      if (this._playRaf) cancelAnimationFrame(this._playRaf);
      try { this._unsubEvents && this._unsubEvents(); this._unsubEvents = null; } catch (_) {}
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }

    setMode(m) { this._mode = m; this.refresh(); }
    // Called every 4 s by the Loop Lab's active-run refresh with the same
    // branch; an unchanged branch must not start another fetch (2026-09-10).
    setBranch(name) {
      if ((name || '') === (this._branch || '') && !this._pipelineId) return;
      this._branch = name; this._pipelineId = ''; this.refresh();
    }
    setPipelineId(id) { if (id === this._pipelineId) return; this._pipelineId = id; this.refresh(); }

    _connectWs() {
      if (this._unsubEvents) return;
      this._unsubEvents = veraSharedEvents(this._getBase(), ev => { try { this._onEvent(ev); } catch (_) {} });
    }

    _onEvent(ev) {
      const t = ev && ev.type || '';
      if (!t.startsWith('evolve.pipeline.') && !t.startsWith('evolve.sandbox.')) return;
      // Only re-fetch (not re-render blind) — the fetch itself is diff-gated
      // by _sig(), so an event about a DIFFERENT pipeline than what's shown
      // still won't cause a spurious pulse here.
      this.refresh(true);
    }

    async _fetchJson(path) {
      try { const r = await fetch(this._getBase() + path); return await r.json(); }
      catch (_) { return null; }
    }

    async refresh(fromLiveEvent) {
      if (this._inflight) return;            // never stack a refresh on a slower one
      this._inflight = true;
      try {
        if (this._mode === 'detail') await this._refreshDetail(fromLiveEvent);
        else await this._refreshLanes(fromLiveEvent);
      } finally { this._inflight = false; }
    }

    // ── detail mode ──────────────────────────────────────────────────────
    async _refreshDetail() {
      let rec = null;
      if (this._pipelineId) {
        const d = await this._fetchJson('/evolve/pipeline/get?id=' + encodeURIComponent(this._pipelineId));
        rec = d && d.pipeline;
      } else if (this._branch) {
        const d = await this._fetchJson('/evolve/pipeline/list?limit=50');
        const all = (d && d.pipelines) || [];
        rec = all.find(p => p.branch === this._branch) || null;
      }
      let drift = null;
      if (rec && rec.status && ['branching', 'testing'].includes(rec.status)) {
        const sb = await this._fetchJson('/evolve/sandbox/status');
        if (sb && sb.sandbox && sb.sandbox.branch === rec.branch) drift = sb.routing_drift;
      }
      const sig = JSON.stringify([rec && rec.id, rec && rec.status, rec && rec.decision,
        rec && rec.candidate_score, rec && rec.gate_delta, drift && drift.mismatches && drift.mismatches.length]);
      const isNew = sig !== this._lastSig;
      this._lastSig = sig;
      this._rec = rec;
      this._renderDetail(rec, drift, isNew);
    }

    _stageStates(rec) {
      // Map the pipeline's real status/decision onto the 4 visual stages.
      // 'starting'/'baseline'/'branching'/'awaiting_edit' = still implementing;
      // 'testing' = sandbox test running; 'tested' = gate computed;
      // decision 'promoted'/'held' = the promote stage's real outcome.
      if (!rec) return STAGES.map(() => 'pending');
      const st = rec.status || '';
      const order = ['starting', 'baseline', 'branching', 'awaiting_edit', 'testing', 'tested', 'done'];
      const idx = order.indexOf(st);
      const states = ['pending', 'pending', 'pending', 'pending'];
      if (st === 'error') {
        // fail at whichever stage was in flight — best-effort from `current`.
        const cur = (rec.current || '').toLowerCase();
        const failIdx = cur.includes('gate') ? 2 : cur.includes('test') || cur.includes('sandbox') ? 1 : 0;
        for (let i = 0; i < failIdx; i++) states[i] = 'done';
        states[failIdx] = 'failed';
        return states;
      }
      if (idx <= 3) { states[0] = 'active'; return states; }              // implementing
      if (st === 'testing') { states[0] = 'done'; states[1] = 'active'; return states; }
      if (st === 'tested' || st === 'done') {
        states[0] = 'done'; states[1] = 'done';
        states[2] = rec.decision === 'pending' || rec.decision === 'held' || rec.decision === 'promoted' ? 'done' : 'active';
        if (rec.decision === 'promoted') states[3] = 'done';
        else if (rec.decision === 'held') states[3] = 'failed';
        else states[3] = 'active';   // gate passed, awaiting manual promote
        return states;
      }
      return states;
    }

    _renderDetail(rec, drift, isNew) {
      const $ = id => this.shadowRoot.getElementById(id);
      const hdr = $('hdr'), body = $('body');
      if (!rec) {
        hdr.innerHTML = '';
        body.innerHTML = '<div class="empty">No pipeline found' + (this._branch ? ' for branch ' + this._esc(this._branch) : '') + '.</div>';
        return;
      }
      const dec = rec.decision || 'pending';
      hdr.innerHTML = '<span class="branch">' + this._esc(rec.branch || rec.id) + '</span>' +
        '<span class="decision ' + dec + '">' + dec + '</span>' +
        (rec.kind ? '<span style="color:var(--dim2,var(--t3,#8a7e70))">' + this._esc(rec.kind) + '</span>' : '');
      const states = this._stageStates(rec);
      const editCount = (rec.edits || []).length;
      const prev = this._prevStates;
      const AVATAR = { implement: 'implement', sandbox_test: '', gate: 'reviewer', promote: '' };
      const GLYPH = { implement: '✻', sandbox_test: '▣', gate: '✻', promote: dec === 'promoted' ? '✓' : dec === 'held' ? '✕' : '⇪' };
      let staggerN = 0;
      body.innerHTML = '<div class="flow">' + STAGES.map((s, i) => {
        const state = states[i];
        const changed = !this._firstRender && (!prev || prev[i] !== state);
        const avCls = AVATAR[s.id] + (state === 'done' && !AVATAR[s.id] ? ' done' : '');
        let extra = '';
        if (s.id === 'implement') {
          extra = editCount + ' edit(s) proposed' +
            (rec.worktree ? ' on <span class="commit">' + this._esc(rec.worktree.split('/').slice(-1)[0]) + '</span>' : '');
        } else if (s.id === 'sandbox_test') {
          extra = rec.candidate_suite ? this._esc(JSON.stringify(rec.candidate_suite).slice(0, 70)) : (state === 'active' ? 'running…' : 'not yet run');
          if (drift && drift.mismatches && drift.mismatches.length) {
            extra += '<div class="warn-chip">⚠ sandbox routing differs from prod</div>';
          }
        } else if (s.id === 'gate') {
          extra = 'baseline <span class="num">' + this._fmt(rec.baseline_score) +
            '</span> vs candidate <span class="num">' + this._fmt(rec.candidate_score) +
            '</span> — Δ <span class="num">' + this._fmt(rec.gate_delta) + '</span>';
          const reviews = rec.reviews || [];
          if (reviews.length) {
            const last = reviews[reviews.length - 1];
            const icon = { claude_code: '🧑‍💻', autonomous: '⚙', user: '👤' }[last.reviewer] || '';
            extra += '<div class="commit">' + icon + ' adversarial review by ' + this._esc(last.reviewer) +
              ' — <b>' + this._esc(last.verdict) + '</b>: ' + this._esc((last.findings || '').slice(0, 140)) +
              (reviews.length > 1 ? ' (' + reviews.length + ' reviews)' : '') + '</div>';
          } else if (rec.review_requested) {
            extra += '<div class="warn-chip">⏳ awaiting external adversarial review' +
              (rec.review_request_reason ? ' — ' + this._esc(rec.review_request_reason.slice(0, 80)) : '') + '</div>';
          }
        } else if (s.id === 'promote') {
          extra = dec === 'held' ? 'held — ' + this._esc((rec.error || 'gate not passed').slice(0, 70))
            : dec === 'promoted' ? 'promoted to main' : state === 'active' ? 'awaiting manual promote' : 'waiting';
        }
        const isFlash = changed && state === 'failed';
        const html = '<div class="msg' + (changed ? ' enter' : '') + '" style="animation-delay:' +
          (changed ? (staggerN++ * 180) : 0) + 'ms">' +
          '<div class="av ' + avCls + '">' + GLYPH[s.id] + '</div>' +
          '<div class="bubble ' + state + (isFlash ? ' flash' : '') + '" data-stage="' + s.id + '">' +
          '<div class="bh"><span class="dot"></span><span>' + s.label + '</span>' +
          '<span style="margin-left:auto;font-size:9px;color:var(--dim,var(--t2,#6a6058))">' +
          this._roleLabel(s.id, rec) + '</span></div>' +
          '<div class="bmsg">' + extra + '</div>' +
          '<div class="commit">' + this._esc((rec.id || '').slice(0, 10)) + (rec.created_at ? ' · ' + this._esc(rec.created_at) : '') + '</div>' +
          '</div></div>';
        return html;
      }).join('') + '</div>';
      this._prevStates = states;
      if (isNew && !this._firstRender) {
        const activeEl = body.querySelector('.bubble.active .dot');
        if (activeEl && window.veraUI && window.veraUI.pulseOnce) window.veraUI.pulseOnce(activeEl);
      }
      if (isNew) this._firstRender = false;
    }

    // ── lanes mode ───────────────────────────────────────────────────────
    async _refreshLanes() {
      // ide.git.branches is a POST capability: ask it that way the first time.
      // (A GET first, "falling back" to POST, was a 405 in the console and a
      // wasted request on every 10s tick.)
      const [branchesD, pipesD] = await Promise.all([
        fetch(this._getBase() + '/ide/git/branches', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
        }).then(r => r.json()).catch(() => null),
        this._fetchJson('/evolve/pipeline/list?limit=100'),
      ]);
      let branches = (branchesD && branchesD.branches) || [];
      const pipelines = (pipesD && pipesD.pipelines) || [];
      const byBranch = {};
      pipelines.forEach(p => { (byBranch[p.branch] = byBranch[p.branch] || []).unshift(p); }); // chronological
      const lanes = Object.keys(byBranch).map(name => {
        const runs = byBranch[name];
        let streak = 0;
        for (let i = runs.length - 1; i >= 0; i--) {
          if (runs[i].decision === 'held' || runs[i].error) break;
          streak++;
        }
        return { name, runs, streak, meta: branches.find(b => b.name === name) };
      });
      const sig = JSON.stringify(lanes.map(l => [l.name, l.runs.length, l.runs[l.runs.length - 1] && l.runs[l.runs.length - 1].decision]));
      const isNew = sig !== this._lastSig;
      this._lastSig = sig;
      this._lanes = lanes;
      this._renderLanes(lanes, isNew);
    }

    _blockColor(rec) {
      if (rec.error) return '#ef4444';
      if (rec.decision === 'held') return '#d9a441';
      if (rec.decision === 'promoted') return '#22c55e';
      if (rec.live) return 'var(--acc,#5a9e8f)';
      return 'rgba(255,255,255,.14)';
    }

    _renderLanes(lanes, isNew) {
      const $ = id => this.shadowRoot.getElementById(id);
      $('hdr').innerHTML =
        '<span style="color:var(--dim2,var(--t3,#8a7e70))">Loop Lab pipeline history — ' + lanes.length + ' branch(es)</span>' +
        '<button class="btn-race" id="raceplay" style="margin-left:8px;background:var(--bg2,var(--s2,#272421));' +
        'border:1px solid var(--border,var(--bd2,#3a3530));color:var(--text,var(--t1,#ddd5c8));' +
        'border-radius:5px;padding:3px 8px;cursor:pointer;font-size:9.5px">▶ replay the race</button>' +
        '<span class="raceclock" id="raceclock"></span>';
      const body = $('body');
      if (!lanes.length) { body.innerHTML = '<div class="empty">No Loop Lab pipelines yet.</div>'; return; }
      const allTs = [];
      lanes.forEach(l => l.runs.forEach(r => { const t = Date.parse(r.created_at || r.ended_at || ''); if (t) allTs.push(t); }));
      const minTs = allTs.length ? Math.min(...allTs) : Date.now() - 3600000;
      const maxTs = allTs.length ? Math.max(...allTs) : Date.now();
      const span = Math.max(maxTs - minTs, 1000);
      const vbW = 700;
      body.innerHTML = '<div class="lanewrap">' + lanes.map(l => {
        const blocks = l.runs.map(r => {
          const t = Date.parse(r.created_at || r.ended_at || '') || maxTs;
          const x = ((t - minTs) / span) * (vbW - 16) + 2;
          const color = this._blockColor(r);
          const isLive = !!r.live;
          return '<rect class="blk' + (isLive ? ' livering' : '') + '" data-id="' + this._esc(r.id) +
            '" data-branch="' + this._esc(l.name) + '" data-ts="' + t + '" x="' + x.toFixed(1) +
            '" y="3" width="14" height="14" rx="3" fill="' + color + '">' +
            '<title>' + this._esc(r.id) + ' · ' + this._esc(r.decision || r.status || '') + '</title></rect>';
        }).join('');
        const repo = (l.runs[l.runs.length - 1] || {}).repo || 'vera';
        const repoTag = repo !== 'vera' ? '<span style="color:var(--acc,var(--ac,#5a9e8f))">' +
          this._esc(repo) + ' · </span>' : '';
        return '<div class="lanerow"><div class="name">' + repoTag + this._esc(l.name) + '</div>' +
          '<svg viewBox="0 0 ' + vbW + ' 20" preserveAspectRatio="none">' + blocks + '</svg>' +
          '<div class="streak">' + l.streak + ' clean</div></div>';
      }).join('') + '</div>';
      body.querySelectorAll('.blk').forEach(b => {
        b.addEventListener('click', () => this.dispatchEvent(new CustomEvent('branchpipe:openpipeline',
          { detail: { id: b.dataset.id, branch: b.dataset.branch } })));
      });
      const playBtn = this.shadowRoot.getElementById('raceplay');
      if (playBtn) playBtn.onclick = () => this._startRaceReplay(minTs, maxTs);
      if (isNew && !this._firstRender) {
        const live = body.querySelector('rect.livering');
        if (live && window.veraUI && window.veraUI.pulseOnce) window.veraUI.pulseOnce(live);
      }
      if (isNew) this._firstRender = false;
    }

    // Real sweeping scrub-line across ALL lanes together, chronological —
    // bun.com's race-chart mechanic, over real pipeline creation timestamps.
    _startRaceReplay(minTs, maxTs) {
      if (this._playing) return;
      const body = this.shadowRoot.getElementById('body');
      const blocks = body.querySelectorAll('.blk');
      if (!blocks.length) return;
      this._playing = true;
      const span = Math.max(maxTs - minTs, 1000);
      const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const durationMs = reduced ? 1 : 8000;
      const clock = this.shadowRoot.getElementById('raceclock');
      blocks.forEach(b => b.classList.add('dim'));
      const t0 = performance.now();
      const frame = now => {
        const frac = Math.min(1, (now - t0) / durationMs);
        const playheadTs = minTs + frac * span;
        if (clock) clock.textContent = new Date(playheadTs).toLocaleString();
        blocks.forEach(b => {
          const t = parseInt(b.dataset.ts, 10);
          if (t <= playheadTs) b.classList.remove('dim');
        });
        if (frac < 1) this._playRaf = requestAnimationFrame(frame);
        else { this._playing = false; if (clock) clock.textContent = ''; blocks.forEach(b => b.classList.remove('dim')); }
      };
      this._playRaf = requestAnimationFrame(frame);
    }

    _roleLabel(stageId, rec) {
      if (stageId === 'implement') {
        const who = { claude_code: 'Claude Code', autonomous: 'Vera autonomous', user: 'user' }
          [rec.controller] || rec.controller || 'coder';
        return 'controller — ' + who;
      }
      if (stageId === 'promote') return 'gate decision';
      if (stageId === 'sandbox_test') return 'sandbox';
      if (stageId === 'gate') {
        const reviews = rec.reviews || [];
        if (reviews.length) {
          const last = reviews[reviews.length - 1];
          const who = { claude_code: 'Claude Code', autonomous: 'Vera autonomous', user: 'user' }[last.reviewer] || last.reviewer;
          return 'adversarial reviewer — ' + who;
        }
        return rec.review_requested ? 'adversarial reviewer — pending' : 'adversarial reviewer';
      }
      return '';
    }

    _fmt(n) { return (n == null) ? '—' : (Math.round(n * 100) / 100); }
    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  }

  customElements.define('vera-branch-pipeline', VeraBranchPipeline);
})();
