/**
 * <vera-bench-compare> — two real comparison tables, same aligned-column
 * delta-table family as bun.com's memory/binary-size/HTTP-throughput
 * before/after tables (patterns 5-7), but live and auto-refreshing instead
 * of one-time static snapshots in a blog post.
 *
 * Table 1 — task outcomes, group A vs B: calls the new `evolve.compare`
 * capability (GET /evolve/compare?group_a=&group_b=&task=), which groups
 * real evolve.runs by `variant` id and aggregates avg pass_rate/combined
 * per task, with the A-vs-B delta.
 *
 * Table 2 — model throughput: pulls real production numbers straight from
 * `ollama.route_stats` (GET /ollama/route_stats) — no new capability
 * needed, this data already exists for the router's own tie-break logic.
 *
 * Truthful animation: both tables poll (default 15s) and are diff-gated —
 * only cells whose real value actually changed pulse; a delta crossing a
 * configurable regression threshold (default 1.0 on combined score) pulses
 * red regardless of direction-agnostic diffing, everything else pulses
 * neutral. No motion on an unchanged poll tick.
 *
 * Attributes: group-a, group-b (variant ids to compare; group-b defaults
 * to "" — the baseline/no-variant runs), task (optional single-task
 * filter), poll-ms (default 15000), threshold (default 1.0).
 * Public API: setApiBase(url), setGroups(a,b), refresh()
 */
(function () {
  if (customElements.get('vera-bench-compare')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;font-family:var(--sans,system-ui,sans-serif);font-size:11px;
  color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
h4{margin:0 0 4px;font-size:10px;font-weight:600;color:var(--dim2,var(--t3,#8a7e70));
  text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%;margin-bottom:14px}
th,td{padding:4px 6px;text-align:right;border-bottom:1px solid var(--border,var(--bd2,#3a3530))}
th:first-child,td:first-child{text-align:left;font-family:var(--mono,monospace)}
th{font-size:9px;color:var(--dim2,var(--t3,#8a7e70));font-weight:500}
td.num{font-family:var(--mono,monospace)}
td.delta.pos{color:var(--ok,var(--ac2,#6db87a))}
td.delta.neg{color:var(--err,var(--ac4,#c96b6b))}
td.delta.warn{color:var(--warn,var(--ac3,#d9a441));font-weight:600}
.empty{padding:10px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
.cellwrap{display:inline-block}
</style>
<h4 id="h1">Task outcomes</h4>
<div id="t1"></div>
<h4 id="h2">Model throughput</h4>
<div id="t2"></div>`;

  class VeraBenchCompare extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._pollTimer = null;
      this._lastSig1 = '';
      this._lastSig2 = '';
      this._prevVals1 = {};
      this._prevVals2 = {};
      this._firstRender1 = true;
      this._firstRender2 = true;
    }

    connectedCallback() {
      this._groupA = this.getAttribute('group-a') || '';
      this._groupB = this.getAttribute('group-b') || '';
      this._task = this.getAttribute('task') || '';
      this._threshold = parseFloat(this.getAttribute('threshold') || '1.0');
      const pollMs = parseInt(this.getAttribute('poll-ms') || '15000', 10);
      this.refresh();
      this._pollTimer = setInterval(() => this.refresh(), pollMs);
    }

    disconnectedCallback() { if (this._pollTimer) clearInterval(this._pollTimer); }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }

    setGroups(a, b) { this._groupA = a || ''; this._groupB = b || ''; this.refresh(); }

    async _fetchJson(path) {
      try { const r = await fetch(this._getBase() + path); return await r.json(); }
      catch (_) { return null; }
    }

    async refresh() {
      await Promise.all([this._refreshTaskCompare(), this._refreshThroughput()]);
    }

    // ── table 1: task outcomes ──────────────────────────────────────────
    async _refreshTaskCompare() {
      const $ = id => this.shadowRoot.getElementById(id);
      $('h1').textContent = 'Task outcomes — ' + (this._groupA || 'A') + ' vs ' + (this._groupB || 'baseline');
      const qs = new URLSearchParams({ group_a: this._groupA, group_b: this._groupB });
      if (this._task) qs.set('task', this._task);
      const d = await this._fetchJson('/evolve/compare?' + qs.toString());
      const rows = (d && d.rows) || [];
      const sig = JSON.stringify(rows.map(r => [r.task, r.a && r.a.avg_combined, r.b && r.b.avg_combined]));
      const isNew = sig !== this._lastSig1;
      this._lastSig1 = sig;
      this._renderTaskCompare(rows, isNew);
    }

    _renderTaskCompare(rows, isNew) {
      const t1 = this.shadowRoot.getElementById('t1');
      if (!rows.length) { t1.innerHTML = '<div class="empty">No runs for these groups yet.</div>'; return; }
      const fmtCell = c => c ? this._fmt(c.avg_combined) + ' <span style="color:var(--dim2,var(--t3,#8a7e70))">(' + c.n + ' runs)</span>' : '—';
      const newVals = {};
      const body = rows.map(r => {
        newVals[r.task] = r.delta;
        const changed = !this._firstRender1 && this._prevVals1[r.task] !== undefined && this._prevVals1[r.task] !== r.delta;
        const dCls = r.delta == null ? '' : (Math.abs(r.delta) >= this._threshold ? 'warn' : (r.delta >= 0 ? 'pos' : 'neg'));
        return '<tr><td>' + this._esc(r.label) + '</td>' +
          '<td class="num">' + fmtCell(r.a) + '</td>' +
          '<td class="num">' + fmtCell(r.b) + '</td>' +
          '<td class="num delta ' + dCls + '" data-task="' + this._esc(r.task) + '" data-changed="' + changed + '">' +
          (r.delta == null ? '—' : (r.delta > 0 ? '+' : '') + r.delta) + '</td></tr>';
      }).join('');
      t1.innerHTML = '<table><tr><th>task</th><th>A</th><th>B</th><th>Δ</th></tr>' + body + '</table>';
      this._prevVals1 = newVals;
      if (isNew && !this._firstRender1) {
        t1.querySelectorAll('td[data-changed="true"]').forEach(td => {
          if (window.veraUI && window.veraUI.pulseOnce) window.veraUI.pulseOnce(td);
        });
      }
      this._firstRender1 = false;
    }

    // ── table 2: model throughput ───────────────────────────────────────
    async _refreshThroughput() {
      const d = await this._fetchJson('/ollama/route_stats?estimate_prompt_chars=0');
      const stats = (d && d.stats) || [];
      const sig = JSON.stringify(stats.map(s => [s.model, s.instance, s.job_type, s.ema_tps, s.n]));
      const isNew = sig !== this._lastSig2;
      this._lastSig2 = sig;
      this._renderThroughput(stats, isNew);
    }

    _renderThroughput(stats, isNew) {
      const t2 = this.shadowRoot.getElementById('t2');
      if (!stats.length) { t2.innerHTML = '<div class="empty">No routed requests observed yet.</div>'; return; }
      const key = s => s.model + '|' + s.instance + '|' + s.job_type;
      const newVals = {};
      const body = stats.slice(0, 25).map(s => {
        const k = key(s);
        newVals[k] = s.n;
        const changed = !this._firstRender2 && this._prevVals2[k] !== undefined && this._prevVals2[k] !== s.n;
        return '<tr><td>' + this._esc(s.model) + '</td><td>' + this._esc(s.instance) + '</td>' +
          '<td class="num">' + (s.ema_tps ? s.ema_tps.toFixed(1) : '—') + '</td>' +
          '<td class="num">' + (s.ema_elapsed_s ? s.ema_elapsed_s.toFixed(1) + 's' : '—') + '</td>' +
          '<td class="num" data-key="' + this._esc(k) + '" data-changed="' + changed + '">' + s.n + '</td></tr>';
      }).join('');
      t2.innerHTML = '<table><tr><th>model</th><th>node</th><th>tok/s</th><th>avg latency</th><th>calls</th></tr>' + body + '</table>';
      this._prevVals2 = newVals;
      if (isNew && !this._firstRender2) {
        t2.querySelectorAll('td[data-changed="true"]').forEach(td => {
          if (window.veraUI && window.veraUI.pulseOnce) window.veraUI.pulseOnce(td);
        });
      }
      this._firstRender2 = false;
    }

    _fmt(n) { return n == null ? '—' : n; }
    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  }

  customElements.define('vera-bench-compare', VeraBenchCompare);
})();
