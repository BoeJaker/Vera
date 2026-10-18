/**
 * <vera-node-models section="warm|settings|nlp|training"> - the models the nodes keep
 * ready, how each node is tuned, and where NLP runs. One section per page (the
 * Models tab lists them separately: Warm models, Node settings, NLP).
 *
 * warm      per node: its slots (GPU 1, CPU 2 - the embedder beside them), the
 *           planned models and where the plan came from, what is resident now
 *           (pinned or expiring), what was dropped and why; the workloads that
 *           take the slots over while a job type runs hot; the routing knobs.
 * settings  what is tuned on each Ollama node (nodes.ollama.settings): num_thread,
 *           environment flags, drop-ins, custom flags, live runners and their -t.
 *           Read over SSH, so it loads when opened and on "reload" only.
 * nlp       where nlp.* runs, each node's NLP server (version, models loaded),
 *           and the three switches.
 * callable  every model Vera can call and what each is for (model.callable):
 *           specialist models from the capabilities that serve them, the LLMs
 *           the routing system can pick, where each is loaded and what calls
 *           to it took; and "which model for this task?" (model.pick).
 *           Loaded when opened and on reload only (it reads node activity).
 *
 * Data: ollama.warm.status|set|apply, nodes.ollama.settings|set, nlp.nodes,
 *       nlp.config.get|set, fabric.nlp.get|set.
 * Attributes: section (default warm), api-base (default ''), refresh (s, 15)
 */
(function () {
  if (customElements.get('vera-node-models')) return;

  const CSS = `
  :host{display:block;font:11px/1.45 var(--sans,-apple-system,system-ui,sans-serif);color:var(--fg,var(--text,#d8dde3))}
  .wrap{display:flex;flex-direction:column;gap:12px;padding:10px}
  .hdr{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .t{font:600 10px/1 var(--mono,ui-monospace,monospace);letter-spacing:.12em;text-transform:uppercase;color:var(--acc,#4a9eff)}
  .muted{color:var(--dim,#5f6975)} .mono{font-family:var(--mono,ui-monospace,monospace)} .sp{flex:1}
  select,input,button,textarea{font:inherit;font-size:10.5px;background:var(--bg2,#1a1f26);color:inherit;border:1px solid var(--border2,#2e3742);border-radius:4px;padding:3px 7px}
  textarea{width:100%;box-sizing:border-box;font-family:var(--mono,ui-monospace,monospace);min-height:150px}
  button{cursor:pointer} button:hover{border-color:var(--acc,#4a9eff)} button.on{border-color:var(--acc,#4a9eff);color:var(--acc,#4a9eff)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:10px}
  .card{background:var(--bg1,#14181d);border:1px solid var(--border,#232a33);border-radius:7px;padding:9px 11px;display:flex;flex-direction:column;gap:5px}
  .card.act{border-color:var(--acc,#4a9eff)}
  .row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .name{font-weight:600;font-size:12px}
  .badge{font:600 9px/1 var(--mono,ui-monospace,monospace);padding:3px 5px;border-radius:3px;border:1px solid currentColor;white-space:nowrap}
  .gpu{color:var(--warn,#f5b341)} .cpu{color:var(--acc,#4a9eff)} .ok{color:var(--acc2,var(--ok,#28c28a))} .bad{color:var(--err,#ef5b5b)} .warn{color:var(--warn,#f5b341)}
  .slot{display:flex;gap:6px;align-items:baseline;border-left:2px solid var(--border2,#2e3742);padding:1px 0 1px 6px}
  .slot.warm{border-left-color:var(--acc2,#28c28a)} .slot.cold{border-left-color:var(--warn,#f5b341)}
  .slot .mono{flex:1;min-width:0;overflow-wrap:anywhere} .slot>span:not(.mono){white-space:nowrap}
  .sec{font:600 9.5px/1 var(--mono,ui-monospace,monospace);letter-spacing:.1em;text-transform:uppercase;color:var(--dim,#5f6975);margin:2px 0}
  .panel{background:var(--bg1,#14181d);border:1px solid var(--border,#232a33);border-radius:7px;padding:9px 11px;display:flex;flex-direction:column;gap:6px}
  table{border-collapse:collapse;width:100%;font-size:10.5px}
  th{text-align:left;color:var(--dim,#5f6975);font-weight:600;padding:3px 6px 3px 0;border-bottom:1px solid var(--border2,#2e3742)}
  td{padding:3px 6px 3px 0;border-top:1px solid var(--border,#232a33);vertical-align:top}
  .kv{display:grid;grid-template-columns:max-content 1fr;gap:3px 10px;align-items:center}
  .msg{min-height:14px}
  `;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const dur = (s) => s == null ? '' : s < 90 ? Math.round(s) + 's' : s < 5400 ? Math.round(s / 60) + 'm' : Math.round(s / 3600) + 'h';

  class VeraNodeModels extends HTMLElement {
    constructor() {
      super();
      this._root = this.attachShadow({ mode: 'open' });
      this._warm = null; this._nlp = null; this._ncfg = null; this._llmnlp = null;
      this._edit = null;      // {kind:'node'|'scenario', key}
      this._settings = null;  // nodes.ollama.settings, loaded on demand
      this._setBusy = '';
      this._msg = '';
      this._root.innerHTML = '<style>' + CSS + '</style><div class="wrap" id="w"></div>';
      this._last = ''; this._auto = false;
      this._root.addEventListener('click', (e) => this._click(e));
      this._root.addEventListener('change', (e) => this._change(e));
    }
    get base() { return this.getAttribute('api-base') || ''; }
    get section() { const s = this.getAttribute('section') || 'warm'; return ['warm', 'settings', 'nlp', 'training', 'callable'].indexOf(s) >= 0 ? s : 'warm'; }
    connectedCallback() {
      this.load();
      const n = Math.max(5, parseInt(this.getAttribute('refresh') || '15', 10));
      // node settings are an SSH read per node, callable models read node
      // activity and every NLP node: neither on a timer
      this._t = setInterval(() => { if (!this._edit && !document.hidden && this.section !== 'settings' && this.section !== 'callable') this.load(true); }, n * 1000);
    }
    disconnectedCallback() { clearInterval(this._t); }

    async _get(path) {
      try { const r = await fetch(this.base + path); return r.ok ? await r.json() : null; } catch (e) { return null; }
    }
    async _post(path, body) {
      try {
        const r = await fetch(this.base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
        return await r.json();
      } catch (e) { return { ok: false, error: String(e) }; }
    }
    async load(auto) {
      const sec = this.section;
      if (sec === 'warm') {
        const [w, bo] = await Promise.all([this._get('/ollama/warm/status'), this._get('/nodes/backend/overview')]);
        this._warm = w; this._onVllm = ((bo && bo.nodes) || []).filter((n) => n.backend === 'vllm');
      }
      else if (sec === 'nlp') {
        const [n, c, l] = await Promise.all([this._get('/nlp/nodes'), this._get('/nlp/config'), this._get('/fabric/nlp/config')]);
        this._nlp = n; this._ncfg = c; this._llmnlp = l;
      } else if (sec === 'training') {
        const [c, t, pp] = await Promise.all([this._get('/compute/status'), this._get('/evolve/test_pool/status'), this._get('/nodes/parity/status')]);
        this._comp = c; this._pool = t; this._parity = pp;
      } else if (sec === 'callable') {
        this._cmBusy = 'load'; this.render();
        this._calls = await this._get('/models/callable'); this._cmBusy = '';
      } else if (sec === 'settings' && !this._settings) {
        this._setBusy = 'load'; this.render();
        this._settings = await this._get('/nodes/ollama/settings'); this._setBusy = '';
      }
      this._auto = !!auto; try { this.render(); } finally { this._auto = false; }
    }

    /* Repaint without the flicker (2026-10-01): the stylesheet is set once,
       identical output is not repainted, the page and inner scroll positions
       are kept, and an automatic refresh waits while a field has focus. The
       old pane also re-attached an embedded catalog on every refresh, which
       restarted it from "Loading..." and threw the page back to the top. */
    _paint(h) {
      const host = this._root.getElementById('w');
      if (!host || h === this._last) return false;
      const a = this._root.activeElement;
      if (this._auto && a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return false;
      const se = document.scrollingElement, py = se ? se.scrollTop : 0;
      const sel = 'pre, textarea, table, [data-keep-scroll]';
      const keep = Array.prototype.map.call(host.querySelectorAll(sel), (e) => e.scrollTop);
      host.innerHTML = h;
      this._last = h;
      Array.prototype.forEach.call(host.querySelectorAll(sel), (e, i) => { if (keep[i]) e.scrollTop = keep[i]; });
      if (se && se.scrollTop !== py) se.scrollTop = py;
      return true;
    }
    _say(m) { this._msg = m; const el = this._root.getElementById('msg'); if (el) el.textContent = m; }

    render() {
      const sec = this.section;
      const msg = '<span class="sp"></span><span class="muted msg" id="msg">' + esc(this._msg) + '</span>';
      let h;
      if (sec === 'warm') {
        const w = this._warm || {};
        h = '<div class="hdr"><span class="t">Warm models</span>'
          + '<button data-a="warm-toggle" class="' + (w.enabled ? 'on' : '') + '" title="Keep each node\'s planned models loaded">' + (w.enabled ? 'on' : 'off') + '</button>'
          + (w.census_busy ? '<span class="badge warn" title="workloads stay off and the GPU is left alone while a census goal runs">census running</span>' : '')
          + msg + '<button data-a="preview">preview</button><button data-a="apply" title="Load what is missing now (one model per node)">apply now</button></div>'
          + (this._warm ? this._nodesHtml(w) + this._scenariosHtml(w) + this._routingHtml(w) : '<div class="muted">loading...</div>');
      } else if (sec === 'settings') {
        h = '<div class="hdr"><span class="t">Node settings</span>' + msg + '</div>' + this._settingsHtml();
      } else if (sec === 'training') {
        h = '<div class="hdr"><span class="t">Node jobs</span>' + msg + '</div>' + this._trainHtml();
      } else if (sec === 'callable') {
        h = '<div class="hdr"><span class="t">Callable models</span>' + msg
          + '<button data-a="cm-reload"' + (this._cmBusy ? ' disabled' : '') + '>' + (this._cmBusy === 'load' ? 'loading...' : 'reload') + '</button></div>'
          + this._callableHtml();
      } else {
        h = '<div class="hdr"><span class="t">NLP</span>' + msg + '</div>' + this._nlpHtml();
      }
      this._paint(h);
    }

    _nodesHtml(w) {
      /* nodes serving vLLM: their Ollama is stopped, so the warm plan leaves them out */
      const vl = (this._onVllm || []).map((n) => esc(n.instance) + (n.served_by ? ' (' + esc(n.served_by) + ')' : '')).join(', ');
      const vnote = vl ? '<div class="muted" style="margin:2px 0 6px"><span class="badge ok">vLLM</span> ' + vl + ' - on vLLM now; warm slots belong to Ollama, so the plan leaves ' + ((this._onVllm || []).length > 1 ? 'them' : 'it') + ' out.</div>' : '';
      const nodes = (w.nodes || []).filter((n) => !(this._onVllm || []).some((v) => v.instance === n.node));
      if (!nodes.length) return vnote + '<div class="muted">no nodes</div>';
      const cards = nodes.map((n) => {
        const res = n.resident || [];
        const isRes = (m) => res.find((r) => String(r.model).replace(/:latest$/, '') === String(m).replace(/:latest$/, ''));
        const clsBadge = n.class === 'gpu' ? '<span class="badge gpu">GPU</span>' : n.class === 'cpu_sibling' ? '<span class="badge cpu" title="the CPU-only Ollama beside a GPU node">CPU sibling</span>' : '<span class="badge cpu">CPU</span>';
        const slots = (n.planned || []).map((p) => {
          const r = isRes(p.model);
          const state = r ? (r.pinned ? '<span class="ok" title="kept loaded until the plan drops it">pinned</span>' : '<span class="warn">lapses in ' + dur(r.expires_in_s) + '</span>') : '<span class="warn">not loaded</span>';
          return '<div class="slot ' + (r ? 'warm' : 'cold') + '"><span class="mono">' + esc(p.model) + '</span><span class="muted">' + (p.num_ctx ? esc(p.num_ctx) + ' ctx' : '') + '</span>' + state + '</div>';
        }).join('') || '<div class="muted">no planned models</div>';
        const embed = n.embed ? '<div class="slot ' + (isRes(n.embed) ? 'warm' : 'cold') + '"><span class="mono">' + esc(n.embed) + '</span><span class="muted">embedder</span></div>' : '';
        const extra = res.filter((r) => !(n.planned || []).some((p) => String(p.model).replace(/:latest$/, '') === String(r.model).replace(/:latest$/, '')) && String(r.model).replace(/:latest$/, '') !== String(n.embed || '').replace(/:latest$/, ''));
        const others = extra.length ? '<div class="muted">also loaded: ' + extra.map((r) => esc(r.model) + (r.pinned ? ' (pinned)' : ' (' + dur(r.expires_in_s) + ')')).join(', ') + '</div>' : '';
        const dropped = (n.dropped || []).length ? '<div class="warn">' + n.dropped.map((d) => esc(d.model) + ': ' + esc(d.why)).join('<br>') + '</div>' : '';
        const editing = this._edit && this._edit.kind === 'node' && this._edit.key === n.node;
        const ed = editing ? '<div class="sec">models for this node (comma separated; @default @naming @embed @long_horizon; empty = follow the routes)</div>'
          + '<input id="ed-models" value="' + esc((((w.config || {}).nodes || {})[n.node] || {}).models ? ((w.config.nodes[n.node].models) || []).join(', ') : '') + '">'
          + '<div class="row"><label>slots <input id="ed-slots" type="number" min="1" max="4" style="width:48px" value="' + esc(n.slots) + '"></label>'
          + '<label title="models a workload scenario may not take">reserve <input id="ed-reserve" style="width:150px" value="' + esc(((((w.config || {}).nodes || {})[n.node] || {}).reserve || []).join(', ')) + '"></label></div>'
          + '<div class="row"><button data-a="node-save" data-k="' + esc(n.node) + '">save</button><button data-a="node-reset" data-k="' + esc(n.node) + '" title="drop this node\'s override: follow the routes">reset</button><button data-a="cancel">cancel</button></div>' : '';
        return '<div class="card' + (n.acting ? ' act' : '') + '"><div class="row"><span class="name">' + esc(n.node) + '</span>' + clsBadge
          + '<span class="badge muted">' + esc(n.slots) + ' slot' + (n.slots === 1 ? '' : 's') + '</span>'
          + ((n.scenarios || []).length ? '<span class="badge ok">' + esc(n.scenarios.join(', ')) + '</span>' : '')
          + '<span class="sp"></span><button data-a="node-edit" data-k="' + esc(n.node) + '">edit</button></div>'
          + '<div class="muted">' + esc(n.source || '') + (n.busy ? ' &middot; <span class="warn">' + esc(n.busy) + '</span>' : '') + (n.acting ? ' &middot; <span class="cpu">' + esc(n.acting) + '</span>' : '') + (n.resident == null ? ' &middot; <span class="bad">no answer from /api/ps</span>' : '') + '</div>'
          + slots + embed + others + dropped + ed + '</div>';
      }).join('');
      const acts = (w.actions || []).length ? '<div class="muted">next: ' + w.actions.map((a) => esc(a.action + ' ' + a.model + ' on ' + a.node)).join(' &middot; ') + '</div>' : '';
      const last = ((w.last || {}).results || []).length ? '<div class="muted">last: ' + w.last.results.map((r) => '<span class="' + (r.ok ? 'ok' : 'bad') + '">' + esc(r.action + ' ' + r.model + ' on ' + r.node + ' ' + (r.ok ? r.s + 's' : (r.error || 'failed'))) + '</span>').join(' &middot; ') + '</div>' : '';
      return '<div class="sec">Warm slots</div>' + vnote + '<div class="grid">' + cards + '</div>' + acts + last;
    }

    _scenariosHtml(w) {
      const scs = w.scenarios || [];
      const rows = scs.map((s) => {
        const st = s.state || {};
        const on = st.active ? '<span class="badge ok">active</span>' : '<span class="badge muted">idle</span>';
        const models = Object.entries(s.models || {}).filter(([k, v]) => (v || []).length).map(([k, v]) => esc(k) + ': ' + esc(v.join(', '))).join('<br>');
        return '<tr><td><input type="checkbox" data-a="sc-enable" data-k="' + esc(s.name) + '"' + (s.enabled ? ' checked' : '') + '></td>'
          + '<td><b>' + esc(s.name) + '</b><div class="muted">' + esc(s.label || '') + '</div></td>'
          + '<td class="mono">' + esc((s.job_types || []).join(', ')) + '</td>'
          + '<td>' + esc(s.min_requests) + ' in ' + dur(s.window_s) + ' or ' + esc(s.min_inflight) + ' live<div class="muted">holds ' + dur(s.hold_s) + '</div></td>'
          + '<td class="mono">' + (models || '<span class="muted">-</span>') + '<div class="muted">fill ' + esc(s.fill) + (s.spill ? ' &middot; spill' : '') + '</div></td>'
          + '<td>' + on + '<div class="muted">' + esc(st.why || '') + '</div></td>'
          + '<td><button data-a="sc-edit" data-k="' + esc(s.name) + '">edit</button></td></tr>';
      }).join('');
      const editing = this._edit && this._edit.kind === 'scenario';
      let ed = '';
      if (editing) {
        const cur = scs.find((s) => s.name === this._edit.key) || { name: '', label: '', enabled: false, job_types: [], min_requests: 6, window_s: 600, min_inflight: 2, hold_s: 900, fill: 'all', models: { gpu: [], cpu: [], cpu_sibling: [] }, spill: false };
        const clean = Object.assign({}, cur); delete clean.state;
        ed = '<div class="sec">scenario (JSON) - models per node class: gpu, cpu, cpu_sibling; fill "all" or a number of slots</div>'
          + '<textarea id="sc-json">' + esc(JSON.stringify(clean, null, 2)) + '</textarea>'
          + '<div class="row"><button data-a="sc-save">save</button>' + (cur.name ? '<button data-a="sc-remove" data-k="' + esc(cur.name) + '">remove</button>' : '') + '<button data-a="cancel">cancel</button></div>';
      }
      return '<div class="panel"><div class="row"><span class="sec">Workloads - hotload models into the slots when a job type runs hot</span><span class="sp"></span><button data-a="sc-new">add</button></div>'
        + '<table><tr><th></th><th>scenario</th><th>job types</th><th>turns on at</th><th>models</th><th>state</th><th></th></tr>' + (rows || '<tr><td colspan="7" class="muted">none</td></tr>') + '</table>'
        + ((w.spill_job_types || []).length ? '<div class="muted">spilling to warm CPU nodes now: ' + esc(w.spill_job_types.join(', ')) + '</div>' : '') + ed + '</div>';
    }

    _routingHtml(w) {
      const c = w.config || {};
      return '<div class="panel"><div class="sec">Routing</div><div class="row">'
        + '<label title="the window every call to a planned model on a CPU node shares - one runner, no reloads">CPU window <input id="r-ctx" type="number" step="4096" min="4096" style="width:80px" value="' + esc(c.cpu_num_ctx) + '"></label>'
        + '<label title="a GPU-preferring call may take a CPU node that has its model warm only if that node has proved this fast (or a scenario is active)">spill min tok/s <input id="r-tps" type="number" step="0.5" min="0" style="width:60px" value="' + esc(c.spill_min_tps) + '"></label>'
        + '<label title="never spill a prompt bigger than this onto a CPU node">spill max ctx <input id="r-maxctx" type="number" step="1024" min="0" style="width:72px" value="' + esc(c.spill_max_ctx) + '"></label>'
        + '<button data-a="r-save">save</button></div>'
        + '<div class="muted">Also: a context larger than the GPU window goes to a CPU node that has the model loaded; a node with every slot taken loses to one with a free slot; a loaded model wins a tie.</div></div>';
    }

    _settingsHtml() {
      const s = this._settings;
      let h = '<div class="panel"><div class="row"><span class="sec">Each Ollama node - threads, flags, drop-ins and the runners loaded now</span><span class="sp"></span>'
        + '<button data-a="set-load">' + (this._setBusy === 'load' ? 'reading nodes...' : (s ? 'reload' : 'load')) + '</button></div>';
      if (!s) return h + '<div class="muted">' + (this._setBusy === 'load' ? 'Reading each node over SSH...' : 'Reads each node over SSH: runner threads, environment flags, drop-ins, loaded runners.') + '</div></div>';
      for (const n of s.nodes || []) {
        const cpu = !n.has_gpu;
        const bad = (n.runners || []).filter((r) => r.default_threads);
        h += '<div class="card" style="margin-top:6px"><div class="row"><span class="name">' + esc(n.instance) + '</span>'
          + '<span class="badge ' + (cpu ? 'cpu">CPU' : 'gpu">GPU') + '</span><span class="muted mono">' + esc(n.unit || '') + '</span>'
          + (bad.length ? '<span class="badge bad" title="runners started without -t run llama.cpp\'s default - 24 threads on 12 CPUs">' + bad.length + ' runner(s) on the 24-thread default</span>' : '')
          + (n.switched ? '<span class="badge ok" title="Switched to vLLM - see LLM backend above">serving vLLM</span>' : '')
          + (n.error ? '<span class="bad">' + esc(n.error) + '</span>' : '') + '</div>';
        if (n.error) { h += '</div>'; continue; }
        if (n.switched) {
          /* its Ollama unit is stopped on purpose: applying a setting would restart
             it beside the vLLM server, so these are read-only until it switches back */
          h += '<div class="muted">This node serves vLLM now (LLM backend, above). Its Ollama settings below are kept and apply again when it switches back to Ollama.</div>'
            + (cpu ? '<div class="muted">num_thread: <b>' + esc(n.num_thread) + '</b></div>' : '')
            + '<div class="muted">custom flags: ' + (Object.entries(n.custom || {}).map(([k, v]) => '<span class="mono">' + esc(k) + '=' + esc(v) + '</span>').join(' &middot; ') || 'none') + '</div></div>';
          continue;
        }
        if (cpu) {
          h += '<div class="row"><label title="sent with every routed call, and written as the unit\'s runner default so every caller gets it">num_thread '
            + '<input id="nt-' + esc(n.instance) + '" type="number" min="1" max="64" style="width:56px" value="' + esc(n.num_thread) + '"></label>'
            + '<span class="muted">runner default on the node: <b class="' + (String(n.threads_default) === String(n.num_thread) ? 'ok' : 'warn') + '">' + esc(n.threads_default || 'not set (llama.cpp default)') + '</b></span>'
            + '<button data-a="nt-save" data-k="' + esc(n.instance) + '">' + (this._setBusy === 'nt:' + n.instance ? 'applying...' : 'apply') + '</button></div>';
        }
        h += '<table><tr><th>runner</th><th>ctx</th><th>parallel</th><th>-t</th><th>OS threads</th></tr>'
          + (n.runners || []).map((r) => '<tr><td class="mono">' + esc(r.model) + (r.embedding ? ' <span class="muted">(embed)</span>' : '') + '</td><td>' + esc(r.ctx) + '</td><td>' + esc(r.parallel) + '</td>'
            + '<td class="' + (r.default_threads ? 'bad' : '') + '">' + (r.threads ? esc(r.threads) : (cpu ? 'default (24)' : '-')) + '</td><td>' + esc(r.os_threads) + '</td></tr>').join('')
          + ((n.runners || []).length ? '' : '<tr><td colspan="5" class="muted">no runner loaded</td></tr>') + '</table>';
        h += '<div class="muted" style="margin-top:4px">environment: ' + Object.entries(n.env || {}).map(([k, v]) => '<span class="mono">' + esc(k) + '=' + esc(v) + '</span>').join(' &middot; ') + '</div>'
          + '<div class="muted">drop-ins: ' + (n.dropins || []).map((d) => '<span class="mono" title="' + esc(d.text) + '">' + esc(d.name) + '</span>').join(', ') + '</div>';
        const cf = Object.entries(n.custom || {}).map(([k, v]) => k + '=' + v).join('\n');
        h += '<div class="sec">custom flags (OLLAMA_* / LLAMA_ARG_*, one KEY=VALUE per line; remove a line to drop it)</div>'
          + '<textarea id="cf-' + esc(n.instance) + '" style="min-height:48px">' + esc(cf) + '</textarea>'
          + '<div class="row"><button data-a="cf-preview" data-k="' + esc(n.instance) + '">preview</button>'
          + '<button data-a="cf-apply" data-k="' + esc(n.instance) + '" title="restarts the unit; its models reload; rolls back if Ollama does not answer">'
          + (this._setBusy === 'cf:' + n.instance ? 'applying...' : 'apply') + '</button></div></div>';
      }
      return h + '</div>';
    }

    _flagsDiff(inst) {
      const n = ((this._settings || {}).nodes || []).find((x) => x.instance === inst) || {};
      const want = {};
      for (const line of (this._root.getElementById('cf-' + inst).value || '').split('\n')) {
        const t = line.trim(); if (!t || t.startsWith('#')) continue;
        const i = t.indexOf('='); if (i < 1) continue;
        want[t.slice(0, i).trim()] = t.slice(i + 1).trim();
      }
      const flags = Object.assign({}, want);
      for (const k of Object.keys(n.custom || {})) if (!(k in want)) flags[k] = null;
      return flags;
    }

    _nlpHtml() {
      const n = this._nlp, c = (this._ncfg || {}).config || {}, l = this._llmnlp || {};
      if (!n) return '<div class="muted">loading...</div>';
      const remote = n.where === 'remote';
      let h = '<div class="panel"><div class="row"><span class="badge ' + (remote ? 'ok' : 'warn') + '">' + (remote ? 'on the nodes' : esc(n.where || '?')) + '</span>'
        + '<span>' + esc(n.reason || '') + '</span><span class="sp"></span><span class="muted">next call: <b>' + esc(n.node || '-') + '</b></span></div>'
        + '<table><tr><th>node</th><th>models loaded</th><th>NLP server</th><th>memory free</th></tr>'
        + (n.nodes || []).map((x) => {
            const p = x.preload || {};
            const loaded = (p.loaded || []).length, failed = Object.keys(p.failed || {}).length;
            return '<tr><td><b>' + esc(x.node_id) + '</b></td>'
              + '<td>' + (p.state === 'done' ? '<span class="ok">' + loaded + '</span>' : p.state === 'loading' ? '<span class="warn">loading ' + loaded + '</span>' : '<span class="muted">' + esc(p.state || 'lazy') + '</span>')
              + (failed ? ' <span class="bad" title="' + esc(JSON.stringify(p.failed)) + '">' + failed + ' failed</span>' : '')
              + ' <span class="muted" title="' + esc((p.loaded || []).join(', ')) + '">' + esc((p.loaded || []).join(' · ')) + '</span></td>'
              + '<td class="mono">' + esc((x.component || {}).version || '') + '</td>'
              + '<td>' + (x.mem_available_mb != null ? esc(Math.round(x.mem_available_mb / 1024)) + ' GB' : '') + '</td></tr>';
          }).join('')
        + ((n.nodes || []).length ? '' : '<tr><td colspan="4" class="muted">no node answers</td></tr>') + '</table></div>';
      const pins = ['<option value="">best node</option>'].concat((n.candidates || []).map((id) => '<option value="' + esc(id) + '"' + (c.node === id ? ' selected' : '') + '>' + esc(id) + '</option>')).join('');
      h += '<div class="panel"><div class="sec">Switches</div><div class="kv">'
        + '<span>Run on</span><span><select data-a="nlp-pin">' + pins + '</select></span>'
        + '<span>Host fallback</span><span><label><input type="checkbox" data-a="nlp-local"' + (c.nlp_local ? ' checked' : '') + '> let the Vera host run NLP when no node can</label></span>'
        + '<span>LLM NLP</span><span><label><input type="checkbox" data-a="llm-nlp"' + (l.enabled ? ' checked' : '') + '> use the LLMs for NLP in automatic pipelines</label></span>'
        + '</div></div>';
      return h;
    }

    /* Model training on the nodes (compute.status): which workers take it and
       with what, where each trainer would run now, the switches, recent jobs. */
    _callableHtml() {
      const c = this._calls;
      const pr = this._pickRes;
      const ms = (v) => v == null ? '' : v < 1 ? Math.round(v * 1000) + ' ms' : (Math.round(v * 10) / 10) + ' s';
      const call = (r) => {
        const sel = r.selector || {}; const k = Object.keys(sel);
        return '<span class="mono">' + esc(r.cap) + (k.length ? ' ' + k.map((x) => esc(x) + '=' + esc(sel[x])).join(' ') : '') + '</span>';
      };
      const meas = (r) => {
        const m = r.measured || {};
        if (!m.calls) return '<span class="muted" title="no calls in the last 24 h">-</span>';
        return esc(ms(m.p50_s)) + ' / ' + esc(ms(m.p95_s)) + ' <span class="muted">(' + esc(m.calls) + ' calls'
          + (m.error_rate ? ', <span class="bad">' + Math.round(m.error_rate * 100) + '% errors</span>' : '')
          + (m.scope && m.scope !== 'model' ? ', ' + esc(m.scope) : '') + ')</span>';
      };
      const where = (r) => (r.loaded_on && r.loaded_on.length) ? '<span class="badge ok">' + esc(r.loaded_on.join(', ')) + '</span>'
        : (r.present_on && r.present_on.length) ? '<span class="badge warn" title="present, not loaded">' + esc(r.present_on.join(', ')) + '</span>'
        : '<span class="muted">' + esc(r.placement || '') + '</span>';
      let h = '<div class="panel"><div class="sec">Which model for a task?</div>'
        + '<div class="row"><input id="cm-need" data-a="cm-need" placeholder="describe the task, e.g. find dates and money in an English article" style="flex:1;min-width:240px" value="' + esc(this._cmNeed || '') + '">'
        + '<button data-a="cm-pick"' + (this._cmBusy ? ' disabled' : '') + '>' + (this._cmBusy === 'pick' ? 'picking...' : 'pick') + '</button></div>';
      if (pr && pr.error) h += '<div class="bad">' + esc(pr.error) + '</div>';
      if (pr && pr.picks) {
        h += '<table><tr><th>model</th><th>call</th><th>why</th><th>score</th></tr>' + pr.picks.map((r, i) =>
          '<tr><td><b>' + (i + 1) + '. ' + esc(r.id) + '</b><div class="muted">' + esc(r.uses || '') + '</div></td>'
          + '<td>' + call(r) + '</td><td class="muted">' + esc(r.why || '') + '</td><td class="mono">' + esc(r.score) + '</td></tr>').join('')
          + '</table><div class="muted">ranked by ' + esc(pr.ranked_by || '') + '</div>';
      }
      h += '</div>';
      if (!c) return h + '<div class="muted">' + (this._cmBusy === 'load' ? 'loading...' : 'not loaded') + '</div>';
      if (c.ok === false || c.error) return h + '<div class="bad">' + esc(c.error || 'model.callable failed') + '</div>';
      const rows = c.models || [];
      const fams = Array.from(new Set(rows.map((r) => r.family))).sort();
      const q = String(this._cmQ || '').toLowerCase(), fam = this._cmFam || '';
      const keep = (r) => (!fam || r.family === fam) && (!q || ((r.id || '') + ' ' + (r.uses || '') + ' ' + (r.model || '')).toLowerCase().indexOf(q) >= 0);
      const spec = rows.filter((r) => r.cap !== 'llm.generate' && keep(r));
      const llms = rows.filter((r) => r.cap === 'llm.generate' && keep(r));
      const src = c.sources || {};
      h += '<div class="row"><select data-a="cm-fam"><option value="">every family</option>'
        + fams.map((f) => '<option value="' + esc(f) + '"' + (f === fam ? ' selected' : '') + '>' + esc(f) + '</option>').join('') + '</select>'
        + '<input data-a="cm-q" placeholder="filter" value="' + esc(this._cmQ || '') + '" style="width:180px">'
        + '<span class="sp"></span><span class="muted">' + esc(rows.length) + ' models · NLP nodes: ' + esc(src.nlp_nodes || '?')
        + (src.measured ? ' · ' + esc(src.measured) : '') + '</span></div>';
      if ((c.errors || []).length) h += '<div class="panel"><div class="sec bad">Binding errors</div>' + c.errors.map((e) => '<div class="mono bad">' + esc(e) + '</div>').join('') + '</div>';
      h += '<div class="panel"><div class="sec">Specialist models (' + spec.length + ')</div><div data-keep-scroll style="overflow:auto">'
        + '<table><tr><th>model</th><th>for</th><th>runs</th><th>loaded</th><th style="white-space:nowrap">p50 / p95</th></tr>'
        + spec.map((r) => '<tr><td><b>' + esc(r.id) + '</b>' + (Object.keys(r.selector || {}).length ? '<div>' + call(r) + '</div>' : '') + '<div class="muted">' + esc(r.family) + (r.tier ? ' · tier ' + esc(r.tier) : '') + (r.questions && r.questions.length ? ' · ' + esc(r.questions.length) + ' questions' : '') + '</div></td>'
          + '<td>' + esc(r.uses || '') + (r.limits ? '<div class="muted">Not for: ' + esc(r.limits) + '</div>' : '') + '</td>'
          + '<td class="mono" style="overflow-wrap:anywhere">' + esc(r.model || (r.note || '')) + (r.installable ? '<div class="muted">catalog: ' + esc(r.installable) + '</div>' : '') + '</td>'
          + '<td>' + where(r) + '</td><td>' + meas(r) + '</td></tr>').join('')
        + (spec.length ? '' : '<tr><td colspan="5" class="muted">none match</td></tr>') + '</table></div></div>';
      const lf = src.llm_facts || {};
      h += '<div class="panel"><div class="sec">LLMs - routed by model= (' + llms.length + ')</div>'
        + '<div class="muted">Described from each model\'s facts (Ollama), measured speed and your purpose tags (Model Routing). '
        + (lf.still_to_fetch ? esc(lf.still_to_fetch) + ' more models get their facts on the next reloads.' : '') + '</div>'
        + '<div data-keep-scroll style="overflow:auto"><table><tr><th>model</th><th>for</th><th>not for</th><th>loaded</th><th>tok/s</th><th style="white-space:nowrap">p50 / p95</th></tr>'
        + llms.map((r) => { const t = r.tps || {}; const best = Object.keys(t).sort((a, b) => t[b] - t[a])[0];
            return '<tr><td class="mono" style="overflow-wrap:anywhere"><b>' + esc(r.model) + '</b></td><td>' + esc(r.uses || '') + '</td>'
              + '<td class="muted">' + esc(r.limits || '') + '</td><td>' + where(r) + '</td>'
              + '<td>' + (best ? esc(Math.round(t[best])) + ' <span class="muted">' + esc(best) + '</span>' : '') + '</td><td>' + meas(r) + '</td></tr>'; }).join('')
        + (llms.length ? '' : '<tr><td colspan="6" class="muted">none match</td></tr>') + '</table></div></div>';
      return h;
    }

    _trainHtml() {
      const c = this._comp;
      if (!c) return '<div class="muted">loading...</div>';
      const cfg = c.config || {}, ws = c.workers || [], fns = c.functions || {}, jobs = c.jobs || [];
      const tp = this._pool || {}, tenv = ((tp.testenv || {}).nodes) || {}, pcfg = tp.config || {};
      const lib = (l, k) => '<span class="badge ' + (l[k] ? 'ok' : 'bad') + '">' + k + '</span>';
      const n1 = (v) => (v == null || isNaN(v)) ? '-' : (Math.round(v * 10) / 10);
      let h = '<div class="panel"><div class="sec">Nodes</div><table><tr><th>node</th><th>free CPU</th><th>free memory</th><th>sent, not yet seen</th><th>libraries</th><th>test venv</th><th>now</th></tr>'
        + ws.map((w) => { const l = w.libs || {}, cp = w.capacity || {}, rs = w.reserved || {}, te = tenv[w.host] || {};
            return '<tr><td><b>' + esc(w.host || w.id) + '</b></td>'
              + '<td>' + n1(cp.cpu_free) + ' / ' + n1(cp.cores) + ' cores</td>'
              + '<td>' + n1(cp.mem_free_gb) + ' / ' + n1(cp.mem_total_gb) + ' GB</td>'
              + '<td class="muted">' + ((rs.cpu || rs.mem_gb) ? n1(rs.cpu) + ' cores, ' + n1(rs.mem_gb) + ' GB' : '') + '</td>'
              + '<td>' + ['numpy', 'torch', 'sklearn'].map((k) => lib(l, k)).join(' ') + '</td>'
              + '<td>' + (l.testenv ? '<span class="badge ok">current</span>' : te.sha ? '<span class="badge warn">stale</span>' : '<span class="badge bad">none</span>')
              + (te.failed && te.failed.length ? ' <span class="warn" title="' + esc(te.failed.join(', ')) + '">' + te.failed.length + ' pkg failed</span>' : '') + '</td>'
              + '<td class="muted">' + esc((w.status || '').replace('running:', 'running ')) + '</td></tr>'; }).join('')
        + (ws.length ? '' : '<tr><td colspan="7" class="muted">no node worker has the CPU compute class</td></tr>')
        + '</table><div class="muted" style="margin-top:4px">A job goes to the node with most room, and only where the free CPU and memory cover it beyond each node\'s reserve for its own LLM and NLP services.</div></div>';
      const label = { 'worldview.train': 'Worldview training', 'ml.train': 'ML Lab training', 'markets.ml.train': 'Markets models', 'pytest.shard': 'Gate test shards' };
      h += '<div class="panel"><div class="sec">Where each job runs</div><table><tr><th>job</th><th>takes</th><th>runs on</th><th>next</th><th></th></tr>'
        + Object.keys(fns).map((k) => { const f = fns[k], rs = f.resources || {}; const cls = f.would_run === 'node' ? (f.room_now === false ? 'warn' : 'ok') : f.would_run === 'host' ? 'warn' : 'bad';
            return '<tr><td><b>' + esc(label[k] || k) + '</b></td><td class="muted">' + esc((rs.cpu || 1) + ' cores, ' + (rs.mem_gb || 1) + ' GB') + '</td>'
              + '<td><span class="badge ' + cls + '">' + esc(f.would_run === 'refuse' ? 'not run' : f.room_now === false ? 'waits' : f.would_run) + '</span></td>'
              + '<td>' + esc(f.next_node || '') + '</td>'
              + '<td class="muted">' + esc(f.why || '') + '</td></tr>'; }).join('') + '</table></div>';
      h += '<div class="panel"><div class="sec">Switches</div><div class="kv">'
        + '<span>Gate test pool</span><span><label><input type="checkbox" data-a="tp-enabled"' + (pcfg.enabled ? ' checked' : '') + '> split pipeline gates\' pytest runs over the host and the nodes with room</label></span>'
        + '<span>Host fallback</span><span><label><input type="checkbox" data-a="tr-fallback"' + (cfg.host_fallback ? ' checked' : '') + '> run training on the Vera host when no node can take it</label></span>'
        + '<span>Node reserve</span><span><input id="tr-rcpu" type="number" min="0" max="12" step="0.5" value="' + esc(cfg.reserve_cpu) + '" style="width:4.5em"> cores, <input id="tr-rmem" type="number" min="0" max="64" value="' + esc(cfg.reserve_mem_gb) + '" style="width:4.5em"> GB kept for each node\'s own services</span>'
        + '<span>Threads per training job</span><span><input id="tr-threads" type="number" min="1" max="32" value="' + esc(cfg.threads || 4) + '" style="width:4.5em"></span>'
        + '<span>Wait for room</span><span><input id="tr-wait" type="number" min="0" max="21600" value="' + esc(cfg.max_wait_s) + '" style="width:6em"> s <button data-a="tr-save">save</button></span>'
        + '</div></div>';
      const pr = this._parity || {}, tot = pr.totals || {}, nss = pr.namespaces || {};
      const near = Object.keys(nss).map((k) => [k, nss[k]]).filter((x) => (x[1].vetted || 0) + (x[1].unproven || 0) > 0)
        .sort((a, b) => ((b[1].vetted || 0) + (b[1].unproven || 0)) - ((a[1].vetted || 0) + (a[1].unproven || 0))).slice(0, 12);
      h += '<div class="panel"><div class="sec">Capability parity</div>'
        + '<div class="row">' + ['safe', 'vetted', 'unproven', 'host'].map((k) => '<span class="badge ' + (k === 'host' ? 'bad' : k === 'unproven' ? 'warn' : 'ok') + '">' + k + ' ' + (tot[k] || 0) + '</span>').join(' ')
        + '<span class="sp"></span><span class="muted">' + (pr.static_at ? 'audit ' + new Date(pr.static_at * 1000).toLocaleString() : 'no static audit yet') + ' · ' + esc(pr.footprints || 0) + ' footprints · ' + esc(pr.vetted_applied || 0) + ' vetted in use'
        + ((pr.nightly_preview || {}).at ? ' · nightly preview: ' + esc(pr.nightly_preview.would_vet) + ' to vet, ' + esc(pr.nightly_preview.would_drop) + ' to drop' : '') + '</span>'
        + '<button data-a="par-audit">run audit</button><button data-a="par-preview">preview vet</button><button data-a="par-vet">vet</button></div>'
        + (near.length ? '<table><tr><th>namespace</th><th>safe</th><th>vetted</th><th>unproven</th><th>host</th></tr>'
          + near.map((x) => '<tr><td><b>' + esc(x[0]) + '</b></td>' + ['safe', 'vetted', 'unproven', 'host'].map((k) => '<td>' + (x[1][k] || '') + '</td>').join('') + '</tr>').join('') + '</table>' : '')
        + '<div class="muted" style="margin-top:4px">A capability is vetted only when its code path shows no host-only resource AND it has run at least 3 times touching nothing host-bound. Vetted capabilities run in the Extended class, which a node takes only once enabled in its roles.</div></div>';
      h += '<div class="panel"><div class="sec">Recent jobs</div><div data-keep-scroll style="max-height:320px;overflow:auto"><table><tr><th>started</th><th>job</th><th>ran on</th><th>state</th><th>time</th><th></th></tr>'
        + jobs.map((j) => '<tr><td class="muted">' + esc((j.started || '').replace('T', ' ').slice(0, 19)) + '</td>'
            + '<td>' + esc(j.label || j.fn) + '</td><td>' + esc(j.node || j.where || '') + '</td>'
            + '<td><span class="badge ' + (j.state === 'done' ? 'ok' : j.state === 'error' || j.state === 'refused' ? 'bad' : 'warn') + '">' + esc(j.state) + '</span></td>'
            + '<td>' + (j.seconds != null ? esc(j.seconds) + ' s' : '') + '</td>'
            + '<td class="muted">' + esc(j.error || j.why || '') + '</td></tr>').join('')
        + (jobs.length ? '' : '<tr><td colspan="6" class="muted">no node jobs yet</td></tr>') + '</table></div></div>';
      return h;
    }

    async _click(e) {
      const b = e.target.closest('[data-a]'); if (!b || b.tagName === 'INPUT' || b.tagName === 'SELECT') return;
      const a = b.dataset.a, k = b.dataset.k;
      if (a === 'refresh') return this.load();
      if (a === 'cm-reload') { return this.load(); }
      if (a === 'cm-pick') {
        const need = (this._root.getElementById('cm-need') || {}).value || '';
        this._cmNeed = need;
        if (!need.trim()) { this._pickRes = { error: 'describe the task first' }; return this.render(); }
        this._cmBusy = 'pick'; this.render();
        this._pickRes = await this._post('/models/pick', { need: need, top_k: 5 }); this._cmBusy = '';
        return this.render();
      }
      if (a === 'set-load') {
        this._setBusy = 'load'; this.render();
        this._settings = await this._get('/nodes/ollama/settings'); this._setBusy = ''; return this.render();
      }
      if (a === 'nt-save') {
        const v = parseInt(this._root.getElementById('nt-' + k).value, 10);
        if (!confirm('Set num_thread=' + v + ' on ' + k + '? This restarts its Ollama unit; loaded models reload.')) return;
        this._setBusy = 'nt:' + k; this.render();
        const r = await this._post('/nodes/ollama/settings/set', { instance_id: k, num_thread: v, dry_run: false });
        this._say(r.ok ? 'num_thread applied' : (r.skipped || (r.errors || []).join('; ') || r.error || 'failed'));
        this._settings = await this._get('/nodes/ollama/settings'); this._setBusy = ''; return this.render();
      }
      if (a === 'cf-preview' || a === 'cf-apply') {
        const flags = this._flagsDiff(k);
        if (!Object.keys(flags).length) return this._say('no change');
        if (a === 'cf-apply' && !confirm('Apply custom flags on ' + k + '? This restarts its Ollama unit; loaded models reload.')) return;
        if (a === 'cf-apply') { this._setBusy = 'cf:' + k; this.render(); }
        const r = await this._post('/nodes/ollama/settings/set', { instance_id: k, flags: flags, dry_run: a === 'cf-preview' });
        if (a === 'cf-preview') return this._say(r.ok ? ('would set: ' + JSON.stringify((r.plan || {}).flags || {}) + ' - ' + (r.note || '')) : ((r.errors || []).join('; ') || r.error || 'refused'));
        this._say(r.ok ? 'flags applied' : ((r.result || {}).flags && r.result.flags.rolled_back ? 'Ollama did not answer - rolled back' : (r.skipped || (r.errors || []).join('; ') || r.error || 'failed')));
        this._settings = await this._get('/nodes/ollama/settings'); this._setBusy = ''; return this.render();
      }
      if (a === 'cancel') { this._edit = null; return this.render(); }
      if (a === 'warm-toggle') { const r = await this._post('/ollama/warm/set', { enabled: !(this._warm || {}).enabled }); this._say(r.ok ? 'saved' : (r.error || 'failed')); return this.load(); }
      if (a === 'preview' || a === 'apply') {
        this._say(a === 'apply' ? 'loading...' : 'planning...');
        const r = await this._post('/ollama/warm/apply', { dry_run: a === 'preview' });
        this._say(r.error ? r.error : a === 'preview' ? ((r.actions || []).length ? (r.actions.length + ' action(s) next') : 'nothing to do') : ((r.results || []).length ? r.results.map((x) => x.action + ' ' + x.model + ' ' + (x.ok ? 'ok' : 'failed')).join(', ') : 'nothing to do'));
        return this.load();
      }
      if (a === 'node-edit') { this._edit = { kind: 'node', key: k }; return this.render(); }
      if (a === 'node-save' || a === 'node-reset') {
        const split = (id) => (this._root.getElementById(id).value || '').split(',').map((s) => s.trim()).filter(Boolean);
        const body = a === 'node-reset' ? { node: k, node_config: null } : { node: k, node_config: { models: split('ed-models'), slots: parseInt(this._root.getElementById('ed-slots').value, 10) || null, reserve: split('ed-reserve') } };
        const r = await this._post('/ollama/warm/set', body);
        this._say(r.ok ? 'saved - applies on the next pass' : (r.error || 'failed'));
        this._edit = null; return this.load();
      }
      if (a === 'sc-new') { this._edit = { kind: 'scenario', key: '' }; return this.render(); }
      if (a === 'sc-edit') { this._edit = { kind: 'scenario', key: k }; return this.render(); }
      if (a === 'sc-save') {
        let s; try { s = JSON.parse(this._root.getElementById('sc-json').value); } catch (err) { return this._say('not JSON: ' + err.message); }
        const r = await this._post('/ollama/warm/set', { scenario: s });
        this._say(r.ok ? 'scenario saved' : (r.error || 'failed')); if (r.ok) this._edit = null; return this.load();
      }
      if (a === 'sc-remove') { if (!confirm('Remove scenario ' + k + '?')) return; const r = await this._post('/ollama/warm/set', { remove_scenario: k }); this._say(r.ok ? 'removed' : (r.error || 'failed')); this._edit = null; return this.load(); }
      if (a === 'r-save') {
        const v = (id) => this._root.getElementById(id).value;
        const r = await this._post('/ollama/warm/set', { cpu_num_ctx: parseInt(v('r-ctx'), 10), spill_min_tps: parseFloat(v('r-tps')), spill_max_ctx: parseInt(v('r-maxctx'), 10) });
        this._say(r.ok ? 'saved' : (r.error || 'failed')); return this.load();
      }
      if (a === 'par-audit') { this._say('auditing (about 2 min)...'); const r = await this._post('/nodes/parity/audit', {}); this._say(r.ok ? ('audited ' + r.caps + ' caps in ' + r.seconds + ' s') : (r.error || 'failed')); return this.load(); }
      if (a === 'par-preview' || a === 'par-vet') {
        if (a === 'par-vet' && !confirm('Vet every capability the evidence clears? They run on a node only where the Extended class is enabled.')) return;
        const r = await this._post('/nodes/parity/vet', { dry_run: a === 'par-preview' });
        this._say(r.ok ? (a === 'par-preview' ? ('would vet ' + (r.would_vet || []).length + ', drop ' + (r.would_drop || []).length) : ('vetted ' + (r.vetted || []).length + ', dropped ' + (r.dropped || []).length)) : (r.error || 'failed'));
        return this.load();
      }
      if (a === 'tr-save') {
        const v = (id) => this._root.getElementById(id).value;
        const r = await this._post('/compute/config', { threads: parseInt(v('tr-threads'), 10), reserve_cpu: parseFloat(v('tr-rcpu')), reserve_mem_gb: parseFloat(v('tr-rmem')), max_wait_s: parseInt(v('tr-wait'), 10) });
        this._say(r.ok ? 'saved' : (r.error || 'failed')); return this.load();
      }
      if (a === 'nlp-timeout-save') { const r = await this._post('/nlp/config/set', { timeout_s: parseFloat(this._root.getElementById('nlp-timeout').value) }); this._say(r.ok ? 'saved' : (r.error || 'failed')); return this.load(); }
    }
    async _change(e) {
      const t = e.target; const a = t.dataset && t.dataset.a; if (!a) return;
      if (a === 'cm-fam') { this._cmFam = t.value; return this.render(); }
      if (a === 'cm-q') { this._cmQ = t.value; return this.render(); }
      if (a === 'cm-need') { this._cmNeed = t.value; return; }
      let r = null;
      if (a === 'sc-enable') r = await this._post('/ollama/warm/set', { scenario_enabled: { [t.dataset.k]: t.checked } });
      if (a === 'nlp-local') {
        if (t.checked && !confirm('Let the Vera host run NLP in-process when no node serves it?')) { t.checked = false; return; }
        r = await this._post('/nlp/config/set', { nlp_local: t.checked });
      }
      if (a === 'nlp-pin') r = await this._post('/nlp/config/set', { node: t.value });
      if (a === 'tr-fallback') {
        if (t.checked && !confirm('Let the Vera host run model training when no node worker can take it?')) { t.checked = false; return; }
        r = await this._post('/compute/config', { host_fallback: t.checked });
      }
      if (a === 'tp-enabled') r = await this._post('/evolve/test_pool/config', { enabled: t.checked });
      if (a === 'llm-nlp') {
        if (t.checked && !confirm('Turn LLM NLP on for automatic pipelines? It sends ingestion and discovery NLP to the LLMs.')) { t.checked = false; return; }
        r = await this._post('/fabric/nlp/set', { enabled: t.checked });
      }
      if (r) { this._say(r.ok === false ? (r.error || 'failed') : 'saved'); this.load(); }
    }
  }
  customElements.define('vera-node-models', VeraNodeModels);
})();
