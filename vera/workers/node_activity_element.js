/**
 * <vera-node-activity> - everything the compute nodes are doing, in one pane.
 *
 * Per node: what is running NOW (model, caller - prod Vera, a Loop Lab sandbox,
 * or an external client - job type, elapsed, prompt preview), the node's own
 * load (node agent: load, memory, GPU, runners), who holds the GPU gate, and
 * the window's calls / busy % / tokens / tok/s / errors by caller. Below, every
 * call in a filterable table; a click opens the call's FULL prompt, response
 * and stats (fetched from the node's tap).
 *
 * Data:   GET /nodes/activity         (nodes.activity)
 *         GET /nodes/activity/record  (nodes.activity.record)
 * Source: the node-side taps (nodes.ollama.tap) - they see every request a
 *         node's Ollama serves, whoever sent it.
 *
 * Attributes: api-base (default ''), refresh (seconds, default 5)
 */
(function () {
  if (customElements.get('vera-node-activity')) return;

  const CSS = `
  :host{display:block;font:11px/1.45 var(--sans,-apple-system,system-ui,sans-serif);color:var(--fg,var(--text,#d8dde3))}
  .wrap{display:flex;flex-direction:column;gap:10px;padding:10px}
  .hdr{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .t{font:600 10px/1 var(--mono,ui-monospace,monospace);letter-spacing:.12em;text-transform:uppercase;color:var(--acc,#4a9eff)}
  .muted{color:var(--dim,#5f6975)} .mono{font-family:var(--mono,ui-monospace,monospace)} .sp{flex:1}
  select,input,button{font:inherit;font-size:10.5px;background:var(--bg2,#1a1f26);color:inherit;border:1px solid var(--border2,#2e3742);border-radius:4px;padding:3px 7px}
  button{cursor:pointer} button:hover{border-color:var(--acc,#4a9eff)} button.on{border-color:var(--acc,#4a9eff);color:var(--acc,#4a9eff)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
  .card{background:var(--bg1,#14181d);border:1px solid var(--border,#232a33);border-radius:7px;padding:9px 11px;display:flex;flex-direction:column;gap:5px}
  .card.busy{border-color:var(--warn,#f5b341)}
  .row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .name{font-weight:600;font-size:12px}
  .badge{font:600 9px/1 var(--mono,ui-monospace,monospace);padding:3px 5px;border-radius:3px;border:1px solid currentColor;white-space:nowrap}
  .gpu{color:var(--warn,#f5b341)} .cpu{color:var(--acc,#4a9eff)} .ok{color:var(--acc2,var(--ok,#28c28a))} .bad{color:var(--err,#ef5b5b)} .warn{color:var(--warn,#f5b341)}
  .c-prod{color:var(--acc2,#28c28a)} .c-sandbox{color:#b48cff} .c-external{color:var(--warn,#f5b341)} .c-other{color:var(--dim,#5f6975)}
  .run{border-left:2px solid var(--warn,#f5b341);padding:2px 0 2px 6px;font-size:10.5px}
  .bar{height:4px;background:var(--bg2,#1a1f26);border-radius:2px;overflow:hidden} .bar i{display:block;height:100%;background:var(--acc,#4a9eff)}
  table{border-collapse:collapse;width:100%;font-size:10.5px}
  th{text-align:left;color:var(--dim,#5f6975);font-weight:600;padding:3px 6px 3px 0;border-bottom:1px solid var(--border2,#2e3742);position:sticky;top:0;background:var(--bg0,var(--bg,#0d0f12))}
  td{padding:3px 6px 3px 0;border-top:1px solid var(--border,#232a33);vertical-align:top}
  tr.r{cursor:pointer} tr.r:hover td{background:var(--bg1,#14181d)}
  .tbl{max-height:52vh;overflow:auto}
  .pv{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:9}
  .dlg{background:var(--bg1,#14181d);border:1px solid var(--border2,#2e3742);border-radius:8px;width:min(1100px,94vw);max-height:90vh;display:flex;flex-direction:column}
  .dlg .body{overflow:auto;padding:10px 12px;display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .dlg pre{margin:0;white-space:pre-wrap;word-break:break-word;font:10.5px/1.4 var(--mono,ui-monospace,monospace);background:var(--bg0,#0d0f12);border:1px solid var(--border,#232a33);border-radius:5px;padding:8px;max-height:60vh;overflow:auto}
  .sec{font:600 9.5px/1 var(--mono,ui-monospace,monospace);letter-spacing:.1em;text-transform:uppercase;color:var(--dim,#5f6975);margin:4px 0}
  `;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ago = (t) => { if (!t) return '-'; const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? Math.round(s) + 's' : s < 3600 ? Math.round(s / 60) + 'm' : Math.round(s / 3600) + 'h'; };
  const hms = (t) => t ? new Date(t * 1000).toLocaleTimeString() : '-';
  const cls = (c) => '<span class="badge c-' + esc(c || 'other') + '">' + esc(c || '?') + '</span>';
  const mb = (v) => v == null ? '?' : (v >= 1024 ? (v / 1024).toFixed(1) + 'G' : v + 'M');

  class VeraNodeActivity extends HTMLElement {
    constructor() {
      super();
      this._root = this.attachShadow({ mode: 'open' });
      this._d = null; this._f = { node: '', service: '', caller: '', kind: '', text: '', since_s: '3600' };
      this._paused = false; this._msg = ''; this._rec = null; this._timer = null;
    }
    connectedCallback() {
      this._render(); this.refresh();
      const s = Math.max(2, parseInt(this.getAttribute('refresh') || '5', 10) || 5);
      this._timer = setInterval(() => { if (!this._paused && !this._rec) this.refresh(); }, s * 1000);
    }
    disconnectedCallback() { clearInterval(this._timer); }
    get _base() { return this.getAttribute('api-base') || ''; }
    async _get(path) {
      const r = await fetch(this._base + path, { credentials: 'same-origin' });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((j && (j.detail || j.error)) || ('HTTP ' + r.status));
      return j;
    }
    async refresh() {
      const q = Object.entries(this._f).filter(([, v]) => v).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
      try { this._d = await this._get('/nodes/activity?limit=300&' + q); this._msg = ''; }
      catch (e) { this._msg = 'could not read node activity: ' + e.message; }
      this._render();
    }
    async _open(r) {
      this._rec = { loading: true, row: r }; this._render();
      try { const full = await this._get('/nodes/activity/record?node=' + encodeURIComponent(r.node) + '&id=' + encodeURIComponent(r.id) + '&port=' + (r.port || 0));
            this._rec = full.error ? { row: r, error: full.error } : full; }
      catch (e) { this._rec = { row: r, error: e.message }; }
      this._render();
    }

    _nodeCard(n, s, gate) {
      const inst = s.instance || {}, ag = s.agent || {}, run = s.running || [];
      const g = (gate || []).find((x) => x.node === n) || {};
      const memUsed = ag.mem_total_mb && ag.mem_available_mb != null ? ag.mem_total_mb - ag.mem_available_mb : null;
      const gpu = ag.gpu || {};
      let h = '<div class="card' + (run.length ? ' busy' : '') + '"><div class="row"><span class="name">' + esc(n) + '</span>'
        + '<span class="badge ' + (inst.has_gpu ? 'gpu">GPU' : 'cpu">CPU') + '</span>'
        + (s.tapped ? '<span class="badge ok" title="the node-side tap reports every request">tapped</span>' : '<span class="badge warn" title="no tap on this node yet: only Vera-side stats">untapped</span>')
        + '<span class="sp"></span>' + (inst.status === 'online' ? '<b class="ok">online</b>' : '<b class="bad">' + esc(inst.status || '?') + '</b>') + '</div>';
      // LXC load average is the HOST's, not this container's - shown for scale only
      h += '<div class="muted">' + (ag.cores ? ag.cores + ' cores · ' : '') + 'host load ' + esc((ag.load || [])[0] ?? '?') + ' · mem ' + mb(memUsed) + '/' + mb(ag.mem_total_mb)
        + (gpu.util_pct != null ? ' · GPU ' + gpu.util_pct + '% ' + mb(gpu.used_mb) + '/' + mb(gpu.total_mb) : '')
        + (ag.runners != null ? ' · runners ' + (Array.isArray(ag.runners) ? ag.runners.length : ag.runners) : '') + '</div>';
      if (g.gated) h += '<div class="muted">gate ' + (g.held || 0) + '/' + (g.capacity || 0) + (g.owners && g.owners.length ? ' · ' + esc(g.owners.join(', ')) : '') + '</div>';
      h += '<div class="sec">Now</div>' + (run.length ? run.map((r) => '<div class="run">' + cls(r.caller_class) + ' <b class="mono">' + esc(r.model) + '</b> <span class="muted">' + esc(r.kind) + ' · ' + esc((r.origin || '').split('|').slice(0, 2).join(' · ') || r.caller) + ' · ' + esc(r.running_s) + 's</span><div class="muted pv">' + esc(r.prompt_preview) + '</div></div>').join('') : '<div class="muted">idle</div>');
      h += '<div class="sec">Last ' + Math.round(parseInt(this._f.since_s, 10) / 60 > 15 ? 15 : parseInt(this._f.since_s, 10) / 60) + ' min</div>'
        + '<div class="bar" title="busy ' + (s.busy_pct || 0) + '%"><i style="width:' + Math.min(100, s.busy_pct || 0) + '%"></i></div>'
        + '<div class="muted">' + (s.calls || 0) + ' calls · busy ' + (s.busy_pct || 0) + '% · ' + (s.tokens_out || 0) + ' tok out'
        + (s.mean_tps ? ' · ' + s.mean_tps + ' tok/s' : '') + (s.errors ? ' · <span class="bad">' + s.errors + ' errors</span>' : '') + '</div>'
        + '<div class="row">' + Object.entries(s.by_caller || {}).map(([c, v]) => cls(c) + '<span class="muted">' + v + '</span>').join(' ')
        + ' ' + Object.entries(s.by_kind || {}).map(([k, v]) => '<span class="muted">' + esc(k) + ' ' + v + '</span>').join(' · ') + '</div>';
      return h + '</div>';
    }

    _renderModal() {
      const r = this._rec; if (!r) return '';
      if (r.loading) return '<div class="modal"><div class="dlg"><div class="body">Loading the full call from the node…</div></div></div>';
      const st = ['model', 'node', 'port', 'kind', 'path', 'caller', 'origin', 'status', 'duration_s', 'prompt_eval_count', 'eval_count', 'tps', 'done_reason', 'vectors', 'dims', 'thinking_chars', 'error']
        .filter((k) => r[k] != null && r[k] !== '').map((k) => '<tr><td class="muted">' + esc(k) + '</td><td class="mono">' + esc(r[k]) + '</td></tr>').join('');
      return '<div class="modal" data-a="close-bg"><div class="dlg"><div class="row" style="padding:8px 12px;border-bottom:1px solid var(--border,#232a33)"><span class="name">'
        + esc(r.model || (r.row || {}).model || 'call') + '</span><span class="muted">' + esc(hms(r.start)) + ' · ' + esc(r.node || '') + '</span><span class="sp"></span><button data-a="close">Close</button></div>'
        + (r.error ? '<div class="body"><div class="bad">' + esc(r.error) + '</div></div>' :
          '<div class="body"><div><div class="sec">Prompt</div><pre>' + esc(r.prompt || '') + '</pre></div><div><div class="sec">Response</div><pre>' + esc(r.response || '') + '</pre></div>'
          + '<div><div class="sec">Stats</div><table>' + st + '</table></div><div><div class="sec">Options</div><pre>' + esc(JSON.stringify(r.options || {}, null, 1)) + '</pre></div></div>')
        + '</div></div>';
    }

    _render() {
      const d = this._d || {}, f = this._f;
      const nodes = Object.keys(d.nodes || {}).sort();
      let h = '<style>' + CSS + '</style><div class="wrap"><div class="hdr"><span class="t">Node activity</span>'
        + '<span class="muted">' + (d.taps ? d.taps.length + ' tapped · ' : '') + (d.records_seen || 0) + ' calls recorded</span><span class="sp"></span>'
        + '<select data-f="node"><option value="">all nodes</option>' + nodes.map((n) => '<option' + (f.node === n ? ' selected' : '') + '>' + esc(n) + '</option>').join('') + '</select>'
        + '<select data-f="service">' + [['', 'all services'], ['ollama', 'LLM (ollama)'], ['nlp', 'NLP'], ['media', 'media (STT/TTS/image)'], ['worker', 'worker tasks']].map(([v, l]) => '<option value="' + v + '"' + (f.service === v ? ' selected' : '') + '>' + l + '</option>').join('') + '</select>'
        + '<select data-f="caller">' + [['', 'all callers'], ['prod', 'prod'], ['sandbox', 'sandboxes'], ['external', 'external']].map(([v, l]) => '<option value="' + v + '"' + (f.caller === v ? ' selected' : '') + '>' + l + '</option>').join('') + '</select>'
        + '<select data-f="kind">' + [['', 'all kinds'], ['generate', 'generate'], ['chat', 'chat'], ['embed', 'embed']].map(([v, l]) => '<option value="' + v + '"' + (f.kind === v ? ' selected' : '') + '>' + l + '</option>').join('') + '</select>'
        + '<select data-f="since_s">' + [['900', '15 min'], ['3600', '1 h'], ['21600', '6 h'], ['86400', '24 h']].map(([v, l]) => '<option value="' + v + '"' + (f.since_s === v ? ' selected' : '') + '>' + l + '</option>').join('') + '</select>'
        + '<input data-f="text" placeholder="search prompt / response / model" value="' + esc(f.text) + '" style="width:210px">'
        + '<button data-a="pause" class="' + (this._paused ? 'on' : '') + '">' + (this._paused ? 'Paused' : 'Live') + '</button></div>';
      if (this._msg) h += '<div class="bad">' + esc(this._msg) + '</div>';
      h += '<div class="grid">' + nodes.filter((n) => !f.node || n === f.node).map((n) => this._nodeCard(n, d.nodes[n], d.gate)).join('') + '</div>';
      const rows = d.rows || [];
      h += '<div class="sec">Calls (' + rows.length + ')</div><div class="tbl"><table><tr><th>time</th><th>node</th><th>caller</th><th>service</th><th>kind</th><th>model</th><th>dur</th><th>tok</th><th>tok/s</th><th>status</th><th>prompt</th></tr>'
        + rows.map((r, i) => '<tr class="r" data-i="' + i + '"><td class="mono">' + esc(hms(r.start)) + '<div class="muted">' + ago(r.end) + ' ago</div></td><td>' + esc(r.node) + '</td>'
          + '<td>' + cls(r.caller_class) + '<div class="muted">' + esc(r.who || r.caller) + (r.job_type ? ' · ' + esc(r.job_type) : '') + '</div></td><td>' + esc(r.service || 'ollama') + '</td><td>' + esc(r.kind) + (r.service === 'worker' ? '<div class="muted mono">' + esc(r.cap) + '</div>' : '') + '</td><td class="mono">' + esc(r.model) + '</td>'
          + '<td>' + esc(r.duration_s != null ? r.duration_s.toFixed ? r.duration_s.toFixed(1) + 's' : r.duration_s : '-') + '</td><td>' + esc(r.eval_count ?? (r.vectors != null ? r.vectors + ' vec' : '')) + '</td>'
          + '<td>' + esc(r.tps ?? '') + '</td><td class="' + (r.error || (r.status || 200) >= 400 ? 'bad' : 'ok') + '">' + esc(r.error ? 'err' : r.status) + '</td><td class="pv muted">' + esc(r.prompt_preview) + '</td></tr>').join('')
        + (rows.length ? '' : '<tr><td colspan="11" class="muted">No calls recorded' + (d.taps && d.taps.length ? ' for these filters.' : ' - no node has its tap yet (nodes.ollama.tap).') + '</td></tr>') + '</table></div>';
      h += this._renderModal() + '</div>';
      this._root.innerHTML = h;
      this._root.querySelectorAll('[data-f]').forEach((el) => {
        const ev = el.tagName === 'INPUT' ? 'change' : 'change';
        el.addEventListener(ev, () => { this._f[el.dataset.f] = el.value; this.refresh(); });
      });
      this._root.querySelectorAll('tr.r').forEach((el) => el.addEventListener('click', () => this._open(rows[+el.dataset.i])));
      this._root.querySelectorAll('[data-a]').forEach((el) => el.addEventListener('click', (e) => {
        const a = el.dataset.a;
        if (a === 'pause') { this._paused = !this._paused; this._render(); }
        else if (a === 'close' || (a === 'close-bg' && e.target === el)) { this._rec = null; this._render(); }
      }));
    }
  }
  customElements.define('vera-node-activity', VeraNodeActivity);
})();
