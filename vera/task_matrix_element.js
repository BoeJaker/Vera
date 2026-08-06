/**
 * <vera-task-matrix> — the persistent regression-guard view: benchmark
 * tasks × their own recent runs, one chip per real result, plus a trend
 * sparkline per task.
 *
 * (bun.com source: pattern 3, Compiler Error Workflow Distribution —
 * crates × worktrees grid of status chips. That one tracked a single
 * one-time migration and stopped; this one is live and persistent — it's
 * the actual regression guard, not a retrospective. Component I extends it
 * with a real per-task trend sparkline bun.com has no equivalent for.)
 *
 * Rows = real evolve.tasks. Columns = each task's OWN last N real runs
 * (evolve.runs, grouped client-side by task id, right-aligned so the
 * newest run is always the rightmost column — tasks with fewer runs than
 * N just have empty cells on the left, exactly like a CI matrix where
 * different lanes have different build counts).
 *
 * Truthful animation: a cell pulses ONLY when a real evolve.run.done /
 * evolve.suite.progress(phase=done) WS event lands for that exact task —
 * never on a plain poll tick with no new data (diff-gated via _sig()).
 *
 * Attributes: limit (runs fetched, default 300), cols (columns shown,
 * default 8), tag (filter tasks by tag).
 * Public API: setApiBase(url), refresh()
 * Events dispatched: taskmatrix:opencell {detail:{task, run_id}}
 */
(function () {
  if (customElements.get('vera-task-matrix')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;font-family:var(--sans,system-ui,sans-serif);font-size:11px;
  color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
table{border-collapse:collapse;width:100%}
th,td{padding:3px 4px;text-align:center}
th{font-size:9px;color:var(--dim2,var(--t3,#8a7e70));font-weight:500}
th.hcol{text-align:left;font-family:var(--mono,monospace)}
td.rowhead{text-align:left;font-family:var(--mono,monospace);font-size:9.5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:140px}
.chip{width:16px;height:16px;border-radius:3px;display:inline-block;cursor:pointer;
  background:var(--bg2,var(--s2,#272421));border:1px solid var(--border,var(--bd2,#3a3530))}
.chip.pass{background:var(--ok,var(--ac2,#6db87a));border-color:var(--ok,var(--ac2,#6db87a))}
.chip.partial{background:var(--warn,var(--ac3,#d9a441));border-color:var(--warn,var(--ac3,#d9a441))}
.chip.fail{background:var(--err,var(--ac4,#c96b6b));border-color:var(--err,var(--ac4,#c96b6b))}
.chip.running{background:var(--acc,var(--ac,#5a9e8f));border-color:var(--acc,var(--ac,#5a9e8f))}
.chip.empty{opacity:.25}
.spark{width:60px;height:16px;display:block}
.foot{margin-top:5px;font-size:9.5px;color:var(--dim2,var(--t3,#8a7e70));text-align:right}
.empty-msg{padding:14px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
</style>
<div id="body"></div>
<div class="foot" id="foot"></div>`;

  class VeraTaskMatrix extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._ws = null;
      this._pollTimer = null;
      this._lastSig = '';
      this._liveTask = '';
      this._knownRunIds = new Set();
      this._knownLive = '';
      this._firstRender = true;
    }

    connectedCallback() {
      this._limit = parseInt(this.getAttribute('limit') || '300', 10);
      this._cols = parseInt(this.getAttribute('cols') || '8', 10);
      this._tag = this.getAttribute('tag') || '';
      this._connectWs();
      this.refresh();
      this._pollTimer = setInterval(() => this.refresh(), 10000);
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

    _connectWs() {
      try {
        const wsUrl = this._getBase().replace(/^http/, 'ws') + '/ws';
        this._ws = new WebSocket(wsUrl);
        this._ws.onopen = () => { try { this._ws.send(JSON.stringify({ action: 'subscribe', stream: 'vera:events' })); } catch (_) {} };
        this._ws.onmessage = e => { try { this._onEvent(JSON.parse(e.data)); } catch (_) {} };
        this._ws.onclose = () => { setTimeout(() => this._connectWs(), 3000); };
        this._ws.onerror = () => { try { this._ws.close(); } catch (_) {} };
      } catch (_) { setTimeout(() => this._connectWs(), 5000); }
    }

    _onEvent(ev) {
      const t = ev && ev.type || '';
      if (t === 'evolve.run.done' ||
          (t === 'evolve.suite.progress' && ev.phase === 'done') ||
          t === 'evolve.suite.done') {
        this.refresh();
      } else if (t === 'evolve.workflow' && ev.state === 'done') {
        this.refresh();
      }
    }

    async _fetchJson(path) {
      try { const r = await fetch(this._getBase() + path); return await r.json(); }
      catch (_) { return null; }
    }

    async refresh() {
      const [tasksD, runsD, statusD] = await Promise.all([
        this._fetchJson('/evolve/tasks' + (this._tag ? '?tag=' + encodeURIComponent(this._tag) : '')),
        this._fetchJson('/evolve/runs?limit=' + this._limit),
        this._fetchJson('/evolve/run/status'),
      ]);
      const tasks = (tasksD && tasksD.tasks) || [];
      const runs = (runsD && runsD.runs) || [];
      this._liveTask = (statusD && statusD.live && statusD.current && statusD.current.task) || '';

      const byTask = {};
      runs.forEach(r => { (byTask[r.task] = byTask[r.task] || []).push(r); });
      Object.keys(byTask).forEach(k => byTask[k].reverse()); // oldest→newest within each task

      const rows = tasks.map(t => {
        const all = byTask[t.id] || [];
        const shown = all.slice(-this._cols);
        const pad = this._cols - shown.length;
        return { task: t, all, shown, pad };
      });

      const sig = JSON.stringify(rows.map(r => [r.task.id, r.shown.map(x => x.run_id), this._liveTask]));
      const isNew = sig !== this._lastSig;
      this._lastSig = sig;
      this._render(rows, isNew);
    }

    _cellClass(rec) {
      if (rec == null) return 'empty';
      const pr = rec.pass_rate;
      if (pr == null) return 'empty';
      if (pr >= 1) return 'pass';
      if (pr <= 0) return 'fail';
      return 'partial';
    }

    _sparkline(all) {
      const pts = all.slice(-12).map(r => (r.combined != null ? r.combined : (r.pass_rate || 0) * 10));
      if (pts.length < 2) return '';
      const max = Math.max(...pts, 1), min = Math.min(...pts, 0);
      const range = (max - min) || 1;
      const w = 60, h = 16, step = w / (pts.length - 1);
      const path = pts.map((v, i) => {
        const x = Math.round(i * step);
        const y = Math.round(h - ((v - min) / range) * h);
        return (i === 0 ? 'M' : 'L') + x + ',' + Math.max(1, Math.min(h - 1, y));
      }).join(' ');
      const last = pts[pts.length - 1], prev = pts[pts.length - 2];
      const stroke = last >= prev ? 'var(--ok,var(--ac2,#6db87a))' : 'var(--err,var(--ac4,#c96b6b))';
      return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
        '<path d="' + path + '" fill="none" stroke="' + stroke + '" stroke-width="1.5"/></svg>';
    }

    _render(rows, isNew) {
      const $ = id => this.shadowRoot.getElementById(id);
      const body = $('body'), foot = $('foot');
      if (!rows.length) { body.innerHTML = '<div class="empty-msg">No benchmark tasks defined.</div>'; foot.textContent = ''; return; }
      let head = '<tr><th class="hcol">task</th>';
      for (let i = 0; i < this._cols; i++) head += '<th>' + (i === this._cols - 1 ? 'latest' : '') + '</th>';
      head += '<th>trend</th></tr>';
      const bodyRows = rows.map(r => {
        const cells = [];
        for (let i = 0; i < r.pad; i++) cells.push('<td><span class="chip empty"></span></td>');
        r.shown.forEach(rec => {
          const isLiveRunning = false; // completed runs only appear here; live cell appended below if applicable
          const cls = this._cellClass(rec);
          const trigLabel = { claude_code: 'triggered by Claude Code', autonomous: 'triggered autonomously', user: 'triggered by user' }[rec.triggered_by] || '';
          cells.push('<td><span class="chip ' + cls + '" data-task="' + this._esc(r.task.id) +
            '" data-run="' + this._esc(rec.run_id) + '" title="' + this._esc(rec.run_id) +
            ' · pass_rate ' + (rec.pass_rate == null ? '—' : rec.pass_rate) +
            (trigLabel ? ' · ' + trigLabel : '') + '"></span></td>');
        });
        if (this._liveTask === r.task.id && r.shown.length + r.pad >= this._cols) {
          // running now — replace the notion of a fixed column; show a running chip appended
          cells.push('<td><span class="chip running" title="running now"></span></td>');
        } else if (this._cols - r.pad - r.shown.length <= 0 && this._liveTask === r.task.id) {
          cells.push('<td><span class="chip running" title="running now"></span></td>');
        }
        while (cells.length < this._cols) cells.push('<td><span class="chip empty"></span></td>');
        return '<tr><td class="rowhead" title="' + this._esc(r.task.label || r.task.id) + '">' +
          this._esc(r.task.label || r.task.id) + '</td>' + cells.slice(0, this._cols).join('') +
          '<td>' + this._sparkline(r.all) + '</td></tr>';
      }).join('');
      body.innerHTML = '<table>' + head + bodyRows + '</table>';
      body.querySelectorAll('.chip[data-run]').forEach(c => {
        c.onclick = () => this.dispatchEvent(new CustomEvent('taskmatrix:opencell',
          { detail: { task: c.dataset.task, run_id: c.dataset.run } }));
      });
      const totalRuns = rows.reduce((n, r) => n + r.shown.length, 0);
      const failing = rows.filter(r => r.shown.length && r.shown[r.shown.length - 1].pass_rate < 1).length;
      foot.textContent = totalRuns + ' run(s) shown · ' + failing + ' task(s) failing latest';
      if (isNew) {
        // Pulse only cells whose run_id is genuinely new since the last render
        // (never the whole row/table on an unrelated change elsewhere), and
        // never on the very first render — that's history loading, not a
        // real-time event.
        const curIds = new Set();
        const skipPulse = this._firstRender;
        body.querySelectorAll('.chip[data-run]').forEach(c => {
          const id = c.dataset.run;
          curIds.add(id);
          if (!skipPulse && id && !this._knownRunIds.has(id) && window.veraUI && window.veraUI.pulseOnce) {
            window.veraUI.pulseOnce(c);
          }
        });
        this._knownRunIds = curIds;
        if (!skipPulse && this._liveTask && this._liveTask !== this._knownLive) {
          const runningChip = body.querySelector('.chip.running');
          if (runningChip && window.veraUI && window.veraUI.pulseOnce) window.veraUI.pulseOnce(runningChip);
        }
        this._knownLive = this._liveTask;
        this._firstRender = false;
      }
    }

    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  }

  customElements.define('vera-task-matrix', VeraTaskMatrix);
})();
