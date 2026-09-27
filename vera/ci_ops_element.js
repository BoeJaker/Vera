/* <vera-ci-ops> - the CI command centre: automated development work, live, whole.

   One place to see every gate run, pipeline, test, board item, sandbox and agentic loop, whoever drove it (claude ·
   codex · vera · a person), and to go from any cell to the run itself. It asks the ci.* / loop.ci.* capabilities
   (ci_view_core computes every picture server side, once) and draws their payload with <vera-widget>'s CI forms - the
   same faces the chat canvas lands - so the page and the canvas never disagree.

     filters   text · agent · outcome · since · group · order - one bar, applied to every tab; kept per viewer
     pulse     pass rate + runs per day (hour, for a day or less) + the agents behind them; the fleet beside it
     tabs      Matrix · Race · Tests · Compare · Board · Loops - every lane whole (a lane that has more runs than the
               tile can draw counts the rest; nothing is sliced away server side)
     drill     click any cell: the run whole (ci.run - its failing tests, its pipeline's every step, its lane, its board
               items, its conversation) in a side sheet; compare it with the run before; read its conversation in full
     compare   "pick two": the next two cells clicked are A and B
     live      the page's event bus (evolve.unittest.*, evolve.pipeline.*, board.*, workshop loop events) refreshes the
               open tab, debounced; a slow poll backs it up while the tab is on screen. A read whose answer did not
               change does not redraw.

   Attributes: base (API base, default ''), repo (default 'vera'), height (main tile px, default 520). */
(function () {
  'use strict';
  if (window.customElements && customElements.get('vera-ci-ops')) return;
  const KEY = 'vera:ci-ops:v1';
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (_) { return {}; } };
  const save = (s) => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (_) {} };
  const ago = (t) => { const x = Date.parse(t); if (!isFinite(x)) return ''; const s = (Date.now() - x) / 1000; return s < 90 ? Math.round(s) + 's ago' : s < 5400 ? Math.round(s / 60) + 'm ago' : s < 172800 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago'; };
  const SINCE = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000, all: 0 };
  const TABS = [['matrix', 'Matrix', 'status matrix: a lane per branch, a cell per gate run'], ['race', 'Race to green', 'every lane racing to green: red laps, attempts, time to green'], ['tests', 'Tests', 'every test that failed in any run, against every run: broken · flaky · fixed'], ['compare', 'Compare', 'two runs: fixed · broken · still failing'], ['board', 'Board', 'the work board, each card with its pipeline gate'], ['loops', 'Loops', 'the agentic loop: a lane per goal, a cell per run - click one for its steps']];
  const AGENTS = ['claude', 'codex', 'vera', 'user'];
  const AGC = { claude: '#d97757', codex: '#6ea8d8', vera: '#8fb87a', user: '#b39ddb' };
  function sharedEvents(fn) {
    try { if (typeof window._veraSubscribe === 'function') { const off = window._veraSubscribe(fn); return typeof off === 'function' ? off : () => {}; }
      const par = window.parent; if (par && par !== window && typeof par._veraSubscribe === 'function') { par._veraSubscribe(fn); return () => {}; } } catch (_) {}
    return () => {};
  }
  const CSS = `
  vera-ci-ops{display:flex;flex-direction:column;gap:10px;min-width:0;--co-bd:var(--border,#3a3530);--co-bg:var(--bg1,#1f1d1a);--co-bg2:var(--bg2,#272421);--co-t:var(--text,#ddd5c8);--co-t3:var(--dim2,#8a7e70);--co-ac:var(--acc,#5a9e8f);--co-ok:var(--ok,#6db87a);--co-err:var(--err,#c96b6b);--co-warn:var(--warn,#c9a35a)}
  .co-bar{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .co-bar input[type=search]{min-width:220px;flex:1;max-width:380px}
  .co-seg{display:inline-flex;border:1px solid var(--co-bd);border-radius:7px;overflow:hidden}
  .co-seg button{background:transparent;border:0;color:var(--co-t3);padding:4px 9px;font:500 11px var(--sans,system-ui);cursor:pointer;display:inline-flex;align-items:center;gap:5px}
  .co-seg button+button{border-left:1px solid var(--co-bd)}.co-seg button:hover{color:var(--co-t)}
  .co-seg button.on{background:var(--co-bg2);color:var(--co-t);box-shadow:inset 0 -2px 0 var(--co-ac)}
  .co-seg button i{width:7px;height:7px;border-radius:50%;display:inline-block}.co-seg button em{font-style:normal;font-size:10px;opacity:.65}
  .co-meta{margin-left:auto;font:10px var(--mono,monospace);color:var(--co-t3);display:flex;align-items:center;gap:8px;white-space:nowrap}
  .co-live{display:inline-flex;align-items:center;gap:5px;cursor:pointer;user-select:none}.co-live i{width:8px;height:8px;border-radius:50%;background:var(--co-t3)}.co-live.on i{background:var(--co-ok);box-shadow:0 0 0 0 var(--co-ok);animation:coPulse 1.6s infinite}
  @keyframes coPulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--co-ok) 70%,transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
  .co-top{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:10px}
  @media (max-width:900px){.co-top{grid-template-columns:1fr}}
  .co-tile{background:var(--co-bg);border:1px solid var(--co-bd);border-radius:10px;padding:10px 12px;min-width:0;position:relative}
  .co-tile>vera-widget{display:block;width:100%}
  .co-tile .co-h{display:flex;align-items:center;gap:8px;margin-bottom:6px;font:700 10.5px var(--mono,monospace);letter-spacing:.08em;text-transform:uppercase;color:var(--co-t3)}
  .co-tabs{display:flex;gap:2px;border-bottom:1px solid var(--co-bd);margin-bottom:8px;flex-wrap:wrap}
  .co-tabs button{background:transparent;border:0;color:var(--co-t3);padding:6px 12px;font:600 11.5px var(--sans,system-ui);cursor:pointer;border-radius:7px 7px 0 0}
  .co-tabs button.on{color:var(--co-t);background:var(--co-bg2);box-shadow:inset 0 2px 0 var(--co-ac)}
  .co-tabs .sp{flex:1}.co-tabs .co-pick{font-weight:500}.co-tabs .co-pick.on{color:var(--co-warn);box-shadow:inset 0 2px 0 var(--co-warn)}
  .co-empty{padding:30px;text-align:center;color:var(--co-t3)}
  .co-err{padding:10px;color:var(--co-err);font:11px var(--mono,monospace);white-space:pre-wrap}
  .co-loading{position:absolute;top:8px;right:10px;width:14px;height:14px;border:2px solid var(--co-bd);border-top-color:var(--co-ac);border-radius:50%;animation:coSpin .8s linear infinite}
  @keyframes coSpin{to{transform:rotate(360deg)}}
  .co-sheet{position:fixed;top:0;right:0;bottom:0;width:min(760px,96vw);background:var(--bg0,#181614);border-left:1px solid var(--co-bd);box-shadow:-18px 0 50px rgba(0,0,0,.45);z-index:9000;display:flex;flex-direction:column;transform:translateX(102%);transition:transform .18s ease}
  .co-sheet.open{transform:none}
  .co-sheet .sh-h{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--co-bd)}
  .co-sheet .sh-h b{font:600 13px var(--mono,monospace);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .co-sheet .sh-b{flex:1;overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:12px}
  .co-sheet .sh-b vera-widget{display:block}
  .co-sheet .sec{font:700 10px var(--mono,monospace);letter-spacing:.08em;text-transform:uppercase;color:var(--co-t3);margin-bottom:4px}
  .co-sheet .turn{margin:5px 0;padding:6px 9px;border-radius:7px;background:var(--co-bg2);white-space:pre-wrap;word-break:break-word;font-size:12px}
  .co-sheet .turn.user{border-left:2px solid var(--co-warn)}.co-sheet .turn .who{display:block;font-size:10px;opacity:.6;text-transform:uppercase;letter-spacing:.06em}
  .co-sheet .acts{display:flex;gap:6px;flex-wrap:wrap}
  .co-sel{position:absolute;top:8px;right:34px;font:600 10px var(--mono,monospace);color:var(--co-warn)}
  `;
  class VeraCiOps extends HTMLElement {
    connectedCallback() {
      if (this._up) return; this._up = true;
      if (!document.getElementById('vera-ci-ops-css')) { const st = document.createElement('style'); st.id = 'vera-ci-ops-css'; st.textContent = CSS; document.head.appendChild(st); }
      const s = load();
      this.st = Object.assign({ q: '', agent: '', status: '', since: '7d', group: 'branch', order: 'red', tab: 'matrix', live: true, a: '', b: '' }, s);
      this.base = this.getAttribute('base') || '';
      this.repo = this.getAttribute('repo') || '';
      this._sig = {}; this._ctl = {}; this._pick = null;
      this.innerHTML = this.shell();
      this.bind();
      this._off = sharedEvents((ev) => this.onEvent(ev));
      this._poll = setInterval(() => { if (this.st.live && this.offsetParent) this.refresh(false); }, 20000);
      this.ensureWidgets().then(() => this.refresh(true));
    }
    disconnectedCallback() { this._up = false; try { this._off && this._off(); } catch (_) {} clearInterval(this._poll); clearTimeout(this._deb); if (this._key) document.removeEventListener('keydown', this._key); }
    ensureWidgets() {
      if (window.customElements && customElements.get('vera-widget')) return Promise.resolve();
      return new Promise((ok) => { let sc = document.getElementById('vera-widget-js'); if (!sc) { sc = document.createElement('script'); sc.id = 'vera-widget-js'; sc.src = this.base + '/ui/widgets/widget_element.js'; document.head.appendChild(sc); }
        sc.addEventListener('load', () => ok(), { once: true }); sc.addEventListener('error', () => ok(), { once: true }); setTimeout(ok, 4000); });
    }
    shell() {
      const st = this.st, H = +(this.getAttribute('height') || 520);
      const seg = (k, opts) => '<span class="co-seg" data-k="' + k + '">' + opts.map((o) => '<button data-v="' + esc(o[0]) + '" class="' + (String(st[k]) === String(o[0]) ? 'on' : '') + '" title="' + esc(o[2] || '') + '">' + o[1] + '</button>').join('') + '</span>';
      return '<div class="co-bar">'
        + '<input type="search" data-k="q" placeholder="search branch, pipeline, test id, summary…  ( / )" value="' + esc(st.q) + '">'
        + seg('agent', [['', 'all agents']].concat(AGENTS.map((a) => [a, '<i style="background:' + AGC[a] + '"></i>' + a + '<em data-n="' + a + '"></em>'])))
        + seg('status', [['', 'any'], ['red', 'red'], ['running', 'running'], ['pass', 'green']])
        + seg('since', Object.keys(SINCE).map((k) => [k, k]))
        + '<select data-k="group" title="a lane per…">' + [['branch', 'lane per branch'], ['controller', 'lane per agent'], ['day', 'lane per day'], ['markers', 'lane per test tier']].map((o) => '<option value="' + o[0] + '"' + (st.group === o[0] ? ' selected' : '') + '>' + o[1] + '</option>').join('') + '</select>'
        + '<select data-k="order" title="lane order">' + [['red', 'red first'], ['recent', 'most recent'], ['name', 'by name']].map((o) => '<option value="' + o[0] + '"' + (st.order === o[0] ? ' selected' : '') + '>' + o[1] + '</option>').join('') + '</select>'
        + '<span class="co-meta"><span class="co-live' + (st.live ? ' on' : '') + '" title="refresh on every gate, pipeline, board and loop event"><i></i>live</span><span data-meta></span><button class="btn sm" data-act="refresh" title="read again">↻</button></span>'
        + '</div>'
        + '<div class="co-top"><div class="co-tile"><div class="co-h">pulse</div><vera-widget data-w="pulse" bare style="height:168px"></vera-widget></div><div class="co-tile"><div class="co-h">fleet · who is working where</div><vera-widget data-w="fleet" bare style="height:168px"></vera-widget></div></div>'
        + '<div class="co-tile"><div class="co-tabs">' + TABS.map((t) => '<button data-tab="' + t[0] + '" title="' + esc(t[2]) + '" class="' + (st.tab === t[0] ? 'on' : '') + '">' + t[1] + '</button>').join('') + '<span class="sp"></span><button class="co-pick" data-act="pick" title="the next two cells you click are compared">⇄ pick two</button></div>'
        + '<span class="co-sel" data-sel></span><div data-main style="min-height:' + H + 'px"><vera-widget data-w="main" bare style="height:' + H + 'px"></vera-widget></div></div>'
        + '<div class="co-sheet" data-sheet><div class="sh-h"><b data-sh-title>run</b><button class="btn sm" data-act="close">✕</button></div><div class="sh-b" data-sh-body></div></div>';
    }
    bind() {
      const q = this.querySelector('input[data-k=q]');
      q.addEventListener('input', () => { this.st.q = q.value.trim(); this.persist(); clearTimeout(this._qd); this._qd = setTimeout(() => this.refresh(true), 280); });
      this.addEventListener('click', (e) => {
        const b = e.target.closest('button,.co-live'); if (!b || !this.contains(b)) return;
        const segEl = b.closest('.co-seg');
        if (segEl && b.dataset.v != null) { this.st[segEl.dataset.k] = b.dataset.v; segEl.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b)); this.persist(); this.refresh(true); return; }
        if (b.dataset.tab) { this.st.tab = b.dataset.tab; this.querySelectorAll('.co-tabs [data-tab]').forEach((x) => x.classList.toggle('on', x === b)); this.persist(); this.refresh(true); return; }
        if (b.classList.contains('co-live')) { this.st.live = !this.st.live; b.classList.toggle('on', this.st.live); this.persist(); return; }
        const act = b.dataset.act;
        if (act === 'refresh') this.refresh(true);
        else if (act === 'close') this.closeSheet();
        else if (act === 'pick') { this._pick = this._pick ? null : []; b.classList.toggle('on', !!this._pick); this.querySelector('[data-sel]').textContent = this._pick ? 'click run A' : ''; }
        else if (act === 'cmp-prev' && this._run) this.comparePrev(this._run);
        else if (act === 'only-branch' && this._run) { this.st.q = ''; this.querySelector('input[data-k=q]').value = this._run.lane || ''; this.st.q = this._run.lane || ''; this.persist(); this.closeSheet(); this.refresh(true); }
        else if (act === 'conv' && b.dataset.sid) this.conversation(b.dataset.sid, b.closest('[data-conv-host]'));
        else if (act === 'open-pipe' && b.dataset.pid) { try { if (typeof window.openPipe === 'function') window.openPipe(b.dataset.pid); } catch (_) {} }
      });
      this.querySelectorAll('select[data-k]').forEach((s) => s.addEventListener('change', () => { this.st[s.dataset.k] = s.value; this.persist(); this.refresh(true); }));
      // a cell / lane / test / card clicked in any drawing: handled here (the generic drawer stays shut)
      this.addEventListener('widget:item', (e) => { const d = e.detail || {}; e.preventDefault(); this.onItem(d.item || {}, d.path || ''); });
      this._key = (e) => { if (!this.offsetParent) return; if (e.key === 'Escape') this.closeSheet(); if (e.key === '/' && !/input|textarea|select/i.test((document.activeElement || {}).tagName || '')) { e.preventDefault(); this.querySelector('input[data-k=q]').focus(); } };
      document.addEventListener('keydown', this._key);
    }
    persist() { save(this.st); }
    qs(extra) {
      const st = this.st, p = new URLSearchParams();
      if (st.q) p.set('q', st.q); if (st.agent) p.set('controller', st.agent); if (st.status) p.set('status', st.status);
      const sec = SINCE[st.since]; if (sec) p.set('since', new Date(Date.now() - sec * 1000).toISOString());
      if (this.repo && this.repo !== 'vera') p.set('repo', this.repo);
      Object.keys(extra || {}).forEach((k) => { if (extra[k] !== '' && extra[k] != null) p.set(k, extra[k]); });
      const s = p.toString(); return s ? '?' + s : '';
    }
    async get(key, path) {
      try { this._ctl[key] && this._ctl[key].abort(); } catch (_) {}
      const ctl = this._ctl[key] = new AbortController();
      const r = await fetch(this.base + path, { signal: ctl.signal }); if (!r.ok) throw new Error(r.status + ' ' + path);
      return r.json();
    }
    draw(which, form, data, size) {
      const w = this.querySelector('vera-widget[data-w="' + which + '"]'); if (!w) return;
      let sig = ''; try { sig = form + '|' + JSON.stringify(data); } catch (_) { sig = String(Math.random()); }
      if (this._sig[which] === sig) return;           // an answer that did not change does not redraw
      this._sig[which] = sig;
      try { w.record = { form, title: '', data, frame: { size: size || 'xl' } }; } catch (_) {}
    }
    mainTile() { return this.querySelector('[data-main]'); }
    spin(on) { const t = this.mainTile().parentElement; let s = t.querySelector('.co-loading'); if (on && !s) { s = document.createElement('i'); s.className = 'co-loading'; t.appendChild(s); } else if (!on && s) s.remove(); }
    async refresh(force) {
      if (!this._up) return; const t0 = performance.now(); this.spin(true);
      const st = this.st, hourly = SINCE[st.since] && SINCE[st.since] <= 86400;
      const jobs = [
        this.get('pulse', '/ci/pulse' + this.qs({ buckets: hourly ? 'hour' : 'day' })).then((p) => { this.draw('pulse', 'ci-pulse', p, 'l'); const c = (p.summary && p.summary.controllers) || {}; AGENTS.forEach((a) => { const n = this.querySelector('[data-n="' + a + '"]'); if (n) n.textContent = c[a] ? ' ' + c[a].runs : ''; }); }),
        this.get('fleet', '/ci/fleet').then((f) => this.draw('fleet', 'ci-fleet', f, 'l')),
        this.loadTab(),
      ];
      const res = await Promise.allSettled(jobs); this.spin(false);
      const bad = res.filter((r) => r.status === 'rejected' && !(r.reason && r.reason.name === 'AbortError'));
      const meta = this.querySelector('[data-meta]'); if (meta) meta.textContent = (bad.length ? bad.length + ' read failed · ' : '') + 'read ' + Math.round(performance.now() - t0) + ' ms · ' + new Date().toLocaleTimeString();
      if (bad.length && this.st.tab && !this._mainOk) this.mainTile().innerHTML = '<div class="co-err">' + esc(bad.map((r) => String(r.reason && r.reason.message || r.reason)).join('\n')) + '\n\nThe ci.* capabilities arrive with this branch; an instance without them cannot answer yet.</div><vera-widget data-w="main" bare style="display:none"></vera-widget>';
    }
    async loadTab() {
      const st = this.st; this._mainOk = false;
      const ensure = () => { if (!this.querySelector('vera-widget[data-w="main"]') || this.querySelector('vera-widget[data-w="main"]').style.display === 'none') { this.mainTile().innerHTML = '<vera-widget data-w="main" bare style="height:' + (+(this.getAttribute('height') || 520)) + 'px"></vera-widget>'; this._sig.main = ''; } };
      let form, data;
      if (st.tab === 'matrix') { form = 'status-matrix'; data = await this.get('main', '/ci/matrix' + this.qs({ group: st.group, order: st.order })); }
      else if (st.tab === 'race') { form = 'race-green'; data = await this.get('main', '/ci/race' + this.qs({ group: st.group })); }
      else if (st.tab === 'tests') { form = 'test-grid'; data = await this.get('main', '/ci/tests' + this.qs()); }
      else if (st.tab === 'board') { form = 'ci-board'; data = await this.get('main', '/ci/board?include_done=true'); }
      else if (st.tab === 'loops') { form = 'status-matrix'; data = await this.get('main', '/loop/ci/matrix'); }
      else if (st.tab === 'compare') {
        if (!(st.a && st.b)) { this.mainTile().innerHTML = '<div class="co-empty">Pick two runs to compare: <b>⇄ pick two</b>, then click run A and run B in the matrix, race or tests - or open a run and <b>compare with the run before</b>.</div>'; this._mainOk = true; return; }
        form = 'run-compare'; data = await this.get('main', '/ci/compare?a=' + encodeURIComponent(st.a) + '&b=' + encodeURIComponent(st.b));
      }
      ensure(); this._lastMain = data; this.draw('main', form, data, 'xl'); this._mainOk = true;
    }
    onEvent(ev) {
      if (!this.st.live || !ev) return; const t = String(ev.type || ev.event || '');
      if (!/^(evolve\.(unittest|pipeline|sandbox|branch)|board\.|workshop\.agent_loop|loop\.)/.test(t)) return;
      clearTimeout(this._deb); this._deb = setTimeout(() => { if (this.offsetParent) this.refresh(false); }, 1500);
    }
    onItem(it, path) {
      if (this._pick) {                                // picking two runs to compare
        const ref = it.pipeline_id || it.ts || it.id; if (!ref || /^lane /.test(path)) return;
        this._pick.push(String(it.ts || ref)); const sel = this.querySelector('[data-sel]');
        if (this._pick.length === 1) { sel.textContent = 'A = ' + (it.ts ? ago(it.ts) : ref) + ' · click run B'; return; }
        this.st.a = this._pick[0]; this.st.b = this._pick[1]; this._pick = null; sel.textContent = ''; this.querySelector('[data-act=pick]').classList.remove('on');
        this.st.tab = 'compare'; this.querySelectorAll('.co-tabs [data-tab]').forEach((x) => x.classList.toggle('on', x.dataset.tab === 'compare')); this.persist(); this.refresh(true); return;
      }
      if (/^lane /.test(path) || (it.lane && it.race)) { this.st.q = it.lane || ''; this.querySelector('input[data-k=q]').value = this.st.q; this.persist(); this.refresh(true); return; }
      if (/^test /.test(path) || it.class) { this.openTest(it); return; }
      if (/^item /.test(path)) { this.openBoardItem(it); return; }
      if (/^sandbox /.test(path)) { this.st.q = it.branch || ''; this.querySelector('input[data-k=q]').value = this.st.q; this.persist(); this.refresh(true); return; }
      if (this.st.tab === 'loops' && it.id && !it.pipeline_id) { this.openLoop(it.session_id || it.id); return; }
      this.openRun(it);
    }
    sheet(title) { const s = this.querySelector('[data-sheet]'); s.classList.add('open'); this.querySelector('[data-sh-title]').textContent = title; const b = this.querySelector('[data-sh-body]'); b.innerHTML = '<div class="co-empty">reading…</div>'; return b; }
    closeSheet() { const s = this.querySelector('[data-sheet]'); if (s) s.classList.remove('open'); this._run = null; }
    widgetIn(host, form, data, size, h) { const w = document.createElement('vera-widget'); w.setAttribute('bare', ''); if (h) w.style.height = h + 'px'; host.appendChild(w); try { w.record = { form, title: '', data, frame: { size: size || 'xl' } }; } catch (_) {} return w; }
    async openRun(cell) {
      const ref = cell.pipeline_id || cell.ts || cell.id; const body = this.sheet((cell.label || cell.o || 'run') + ' · ' + (cell.ts ? new Date(cell.ts).toLocaleString() : ref));
      this._run = Object.assign({ lane: '' }, cell);
      let r; try { r = await this.get('sheet', '/ci/run?ref=' + encodeURIComponent(cell.ts || ref)); } catch (e) { body.innerHTML = '<div class="co-err">' + esc(e.message) + '</div>'; return; }
      if (r.error) { body.innerHTML = '<div class="co-err">' + esc(r.error) + '</div>'; return; }
      this._run.lane = r.title || ''; this._run.ts = (r.cell && r.cell.ts) || cell.ts; this._run.laneData = r.lane;
      body.innerHTML = '<div class="acts"><button class="btn sm" data-act="cmp-prev">⇄ compare with the run before</button><button class="btn sm" data-act="only-branch">only this branch</button>' + (r.cell && r.cell.pipeline_id ? '<button class="btn sm" data-act="open-pipe" data-pid="' + esc(r.cell.pipeline_id) + '">open pipeline ' + esc(r.cell.pipeline_id) + '</button>' : '') + (r.conversation ? '<button class="btn sm" data-act="conv" data-sid="' + esc(r.conversation.session_id) + '">💬 read the conversation (' + esc(r.conversation.controller || 'agent') + ')</button>' : '') + '</div>';
      this.widgetIn(body, 'ci-run', r, 'xl', 460);
      if (r.track) { const h = document.createElement('div'); h.innerHTML = '<div class="sec">the pipeline, every step in full</div>'; body.appendChild(h); this.widgetIn(body, 'run-track', r.track, 'xl', 360); }
      const ch = document.createElement('div'); ch.setAttribute('data-conv-host', ''); body.appendChild(ch);
    }
    async comparePrev(run) {
      const cells = (run.laneData && run.laneData.cells) || []; const i = cells.findIndex((c) => c.ts === run.ts);
      if (i <= 0) { const b = this.querySelector('[data-sh-body]'); const n = document.createElement('div'); n.className = 'co-empty'; n.textContent = 'this is the first run on its lane'; b.prepend(n); return; }
      this.st.a = cells[i - 1].ts; this.st.b = run.ts; this.st.tab = 'compare'; this.querySelectorAll('.co-tabs [data-tab]').forEach((x) => x.classList.toggle('on', x.dataset.tab === 'compare')); this.persist(); this.closeSheet(); this.refresh(true);
    }
    async conversation(sid, host) {
      host = host || this.querySelector('[data-sh-body]'); host.innerHTML = '<div class="sec">conversation ' + esc(sid) + '</div><div class="co-empty">reading…</div>';
      try { const h = await this.get('conv', '/ide/claude_sessions/history?claude_session_id=' + encodeURIComponent(sid)); const ts = h.turns || [];
        host.innerHTML = '<div class="sec">conversation ' + esc(sid) + ' · ' + ts.length + ' turns, in full</div>' + (ts.length ? ts.map((t) => '<div class="turn ' + esc(t.role) + '"><span class="who">' + esc(t.role) + ' · ' + esc(ago(t.ts)) + '</span>' + esc(t.text) + '</div>').join('') : '<div class="co-empty">' + esc(h.error || 'no turns ingested for this session') + '</div>');
        host.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } catch (e) { host.innerHTML = '<div class="co-err">' + esc(e.message) + '</div>'; }
    }
    openTest(t) {
      const body = this.sheet(t.id || t.name || 'test');
      body.innerHTML = '<div class="sec">' + esc(t.class || '') + ' · failed in ' + esc(t.fails) + ' runs · flipped ' + esc(t.flips) + ' times · first seen ' + esc(ago(t.first_seen)) + '</div>' + (t.description ? '<pre style="white-space:pre-wrap">' + esc(t.description) + '</pre>' : '') + '<div class="acts"><button class="btn sm" data-act="only-test">only runs where it failed</button></div>';
      body.querySelector('[data-act=only-test]').onclick = () => { this.st.q = t.id; this.querySelector('input[data-k=q]').value = t.id; this.st.tab = 'matrix'; this.querySelectorAll('.co-tabs [data-tab]').forEach((x) => x.classList.toggle('on', x.dataset.tab === 'matrix')); this.persist(); this.closeSheet(); this.refresh(true); };
    }
    async openBoardItem(it) {
      const body = this.sheet(it.title || it.id || 'item');
      try { const r = await this.get('sheet', '/board/item?id=' + encodeURIComponent(it.id)); const x = r.item || it;
        body.innerHTML = '<div class="sec">' + esc(String(x.lane || '').replace(/_/g, ' ')) + ' · ' + esc(x.agent || '') + (x.branch ? ' · ' + esc(x.branch) : '') + (x.pipeline ? ' · pipeline ' + esc(x.pipeline) : '') + '</div>' + (x.body ? '<pre style="white-space:pre-wrap">' + esc(x.body) + '</pre>' : '') + ((x.comments || []).length ? '<div class="sec">thread</div>' + x.comments.map((c) => '<div class="turn"><span class="who">' + esc(c.kind || '') + ' · ' + esc(c.from || '') + (c.to ? ' → ' + esc(c.to) : '') + '</span>' + esc(c.body || '') + '</div>').join('') : '')
          + (x.session ? '<div class="acts" data-conv-host><button class="btn sm" data-act="conv" data-sid="' + esc(x.session) + '">💬 read the conversation</button></div>' : '');
      } catch (e) { body.innerHTML = '<div class="co-err">' + esc(e.message) + '</div>'; }
    }
    async openLoop(sid) {
      const body = this.sheet('loop ' + sid);
      try { const [race, board] = await Promise.all([this.get('sheet', '/loop/ci/race?session_id=' + encodeURIComponent(sid)), fetch(this.base + '/loop/ci/board?session_id=' + encodeURIComponent(sid)).then((r) => r.json())]);
        body.innerHTML = '<div class="sec">' + esc((race.run && race.run.goal) || '') + '</div>'; this.widgetIn(body, 'race-green', race, 'xl', 300); this.widgetIn(body, 'ci-board', board, 'xl', 300);
        if ((race.warnings || []).length) { const w = document.createElement('div'); w.innerHTML = '<div class="sec">warnings</div>' + race.warnings.map((x) => '<div class="turn">' + esc(x) + '</div>').join(''); body.appendChild(w); }
      } catch (e) { body.innerHTML = '<div class="co-err">' + esc(e.message) + '</div>'; }
    }
  }
  customElements.define('vera-ci-ops', VeraCiOps);
})();
