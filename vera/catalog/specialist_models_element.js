/**
 * <vera-specialist-models> - the non-LLM models of the estate, per node.
 *
 * NLP (nlp_server on every node): the version each node runs against the one
 * this host would deploy - current / behind / modified / unversioned - and,
 * per task, the model and whether it is in the shared store and loaded. One
 * button brings every node running it to the host's version (dependencies are
 * reinstalled only when they changed); "Check" shows the plan first.
 * Media (the gpu_inference server): which of STT / TTS / diffusion it serves,
 * and on "Details" its SD model, LoRAs, voices and deployed version.
 * Host NER: the in-process spaCy / GLiNER the fabric's entity graph uses.
 *
 * Data:    GET  /specialist/status[?deep=true&refresh=true]   (specialist.status)
 * Actions: POST /provision/component/sync                      (provision.component.sync)
 *
 * Catalog: every curated model across the families at once - search, family /
 * task chips, per model whether it is in the shared store, loaded on how many
 * nodes and in use; export / download into the store; Hugging Face search.
 *
 * Attributes: api-base (default ''), refresh (seconds, default 30),
 *             tab (status|catalog|jobs|caches - drives the view from outside,
 *             e.g. the Models tab's submenu), tabs="external" (hide the
 *             element's own tab buttons when the page provides them)
 */
(function () {
  if (customElements.get('vera-specialist-models')) return;

  const CSS = `
  :host{display:block;font:11px/1.45 var(--sans,-apple-system,system-ui,sans-serif);color:var(--fg,var(--text,#d8dde3))}
  .wrap{display:flex;flex-direction:column;gap:10px;padding:10px}
  .hdr{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .t{font:600 10px/1 var(--mono,ui-monospace,monospace);letter-spacing:.12em;text-transform:uppercase;color:var(--acc,#4a9eff)}
  .muted{color:var(--dim,#5f6975)}
  .mono{font-family:var(--mono,ui-monospace,monospace)}
  .sp{flex:1}
  button{font:inherit;font-size:10.5px;background:var(--bg2,#1a1f26);color:inherit;border:1px solid var(--border2,#2e3742);
         border-radius:4px;padding:4px 9px;cursor:pointer}
  button:hover{border-color:var(--acc,#4a9eff)}
  button[disabled]{opacity:.5;cursor:default}
  button.pri{background:var(--acc,#4a9eff);border-color:var(--acc,#4a9eff);color:var(--on-acc,#04121f);font-weight:600}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
  .card{background:var(--bg1,#14181d);border:1px solid var(--border,#232a33);border-radius:7px;padding:10px 12px;
        display:flex;flex-direction:column;gap:6px}
  .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .name{font-weight:600;font-size:12px}
  .badge{font:600 9px/1 var(--mono,ui-monospace,monospace);padding:3px 6px;border-radius:3px;border:1px solid currentColor}
  .ok{color:var(--acc2,var(--ok,#28c28a))} .bad{color:var(--err,#ef5b5b)} .warn{color:var(--warn,#f5b341)}
  .sec{font:600 9.5px/1 var(--mono,ui-monospace,monospace);letter-spacing:.1em;text-transform:uppercase;color:var(--dim,#5f6975);margin-top:6px}
  table{border-collapse:collapse;width:100%;font-size:10.5px}
  td{padding:2px 6px 2px 0;vertical-align:top;border-top:1px solid var(--border,#232a33)}
  td.m{font-family:var(--mono,ui-monospace,monospace);word-break:break-all}
  .note{font-size:10px}
  input.q{flex:1;min-width:200px;font:inherit;background:var(--bg2,#1a1f26);color:inherit;border:1px solid var(--border2,#2e3742);border-radius:4px;padding:5px 8px}
  .chip{font:inherit;font-size:10.5px;padding:3px 9px;border-radius:12px}
  .chip.on{border-color:var(--acc,#4a9eff);color:var(--acc,#4a9eff)}
  tr.fam td{padding-top:10px;border-top:none;font:600 9.5px/1 var(--mono,ui-monospace,monospace);letter-spacing:.1em;text-transform:uppercase;color:var(--dim,#5f6975)}
  a.hf{color:inherit;text-decoration:none;border-bottom:1px dotted var(--dim,#5f6975)} a.hf:hover{color:var(--acc,#4a9eff)}
  .plan div{font-size:10.5px}
  `;

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const STATE = { current: ['ok', 'current'], behind: ['warn', 'behind the host'], modified: ['bad', 'edited on the node'],
                  unversioned: ['warn', 'unversioned (deployed before versions)'], absent: ['muted', 'not deployed'] };
  const state = (s) => { const v = STATE[s] || ['muted', s || 'unknown']; return '<b class="' + v[0] + '">' + esc(v[1]) + '</b>'; };
  const yes = (b, t, f) => b ? '<span class="ok">' + (t || 'yes') + '</span>' : '<span class="bad">' + (f || 'no') + '</span>';

  class VeraSpecialistModels extends HTMLElement {
    constructor() {
      super();
      this._root = this.attachShadow({ mode: 'open' });
      this._root.innerHTML = '<style>' + CSS + '</style><div id="host"></div>';
      this._last = ''; this._auto = false;
      this._data = null; this._plan = null; this._busy = {}; this._msg = ''; this._deep = false; this._timer = null;
      this._tab = 'status';
      this._cat = { fam: 'all', task: '', text: '', query: '', data: null, hf: null };
      this._jobs = null; this._job = null; this._caches = null;
    }
    async _loadTab() {
      try {
        if (this._tab === 'catalog') {
          // every family at once - the browse view filters client-side
          if (!this._cat.data || this._auto) this._cat.data = await this._call('GET', '/specialist/catalog');
        } else if (this._tab === 'jobs') {
          this._jobs = await this._call('GET', '/specialist/jobs');
          if (this._job) this._job = await this._call('GET', '/specialist/jobs?job_id=' + encodeURIComponent(this._job.id));
        } else if (this._tab === 'caches' && !this._caches) {
          this._caches = await this._call('GET', '/specialist/node_models');
        }
      } catch (e) { this._msg = 'could not load: ' + e.message; }
      this._render();
    }
    static get observedAttributes() { return ['tab']; }
    attributeChangedCallback(name, _old, val) {
      if (name === 'tab' && val && val !== this._tab && ['status', 'catalog', 'jobs', 'caches'].indexOf(val) >= 0) {
        this._tab = val; this._msg = ''; if (val === 'caches') this._caches = null;
        this._render(); this._loadTab();
      }
    }
    connectedCallback() {
      const t = this.getAttribute('tab');
      if (t && ['status', 'catalog', 'jobs', 'caches'].indexOf(t) >= 0) this._tab = t;
      this._render(); this.refresh();
      const s = Math.max(10, parseInt(this.getAttribute('refresh') || '30', 10) || 30);
      this._timer = setInterval(() => { if (!Object.keys(this._busy).length) this.refresh(); }, s * 1000);
    }
    disconnectedCallback() { clearInterval(this._timer); }
    get _base() { return this.getAttribute('api-base') || ''; }

    async _call(method, path, body) {
      const r = await fetch(this._base + path, {
        method, headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined, credentials: 'same-origin' });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((j && (j.detail || j.error)) || ('HTTP ' + r.status));
      return j;
    }
    async refresh(force) {
      const q = [];
      if (this._deep) q.push('deep=true');
      if (force) q.push('refresh=true');
      try { this._data = await this._call('GET', '/specialist/status' + (q.length ? '?' + q.join('&') : '')); }
      catch (e) { this._msg = 'could not read specialist models: ' + e.message; }
      this._auto = true;
      try {
        if (this._tab === 'jobs') await this._loadTab();
        else this._render();               // the catalog keeps what it loaded
      } finally { this._auto = false; }
    }

    /* Repaint without the flicker (2026-10-01): stylesheet set once, identical
       output not repainted, scroll kept, an automatic refresh waits while a
       field has focus. */
    _paint(h) {
      const host = this._root.getElementById('host');
      if (!host || h === this._last) return false;
      const a = this._root.activeElement;
      if (this._auto && a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return false;
      const se = document.scrollingElement, py = se ? se.scrollTop : 0;
      const keep = Array.prototype.map.call(host.querySelectorAll('pre'), (x) => x.scrollTop);
      host.innerHTML = h;
      this._last = h;
      Array.prototype.forEach.call(host.querySelectorAll('pre'), (x, i) => { if (keep[i]) x.scrollTop = keep[i]; });
      if (se && se.scrollTop !== py) se.scrollTop = py;
      return true;
    }
    async _act(key, fn, okMsg) {
      if (this._busy[key]) return;
      this._busy[key] = true; this._render();
      try { const r = await fn(); this._msg = (r && r.ok === false) ? ('failed: ' + (r.error || 'unknown')) : (okMsg || ''); }
      catch (e) { this._msg = 'failed: ' + e.message; }
      delete this._busy[key];
      await this.refresh(true);
    }

    _renderPlan() {
      const p = this._plan; if (!p) return '';
      const todo = (p.plan || []).filter((x) => x.action === 'deploy');
      let h = '<div class="card plan"><div class="row"><span class="name">Update plan - nlp_server</span>'
        + '<span class="muted">host version <span class="mono">' + esc(p.host_version) + '</span></span><span class="sp"></span>'
        + '<button class="pri" data-a="sync-run"' + (!todo.length || this._busy.sync || (this._data || {}).sandbox ? ' disabled' : '') + '>'
        + (this._busy.sync ? 'Updating… (minutes per node)' : 'Update ' + todo.length + ' node(s)') + '</button>'
        + '<button data-a="plan-close">Close</button></div>';
      for (const x of (p.plan || [])) {
        h += '<div><span class="mono">' + esc(x.host) + '</span> - ' + (x.action === 'deploy'
          ? '<b class="warn">redeploy</b>' + (x.install_deps ? ' + reinstall deps' : ' (code only)')
          : '<span class="muted">leave</span>') + ' <span class="muted">' + esc(x.why) + '</span></div>';
      }
      const res = p.results || {};
      for (const hid of Object.keys(res)) {
        h += '<div>' + esc(hid) + ': ' + (res[hid].ok ? '<span class="ok">done ' + esc(res[hid].version) + '</span>'
          : '<span class="bad">' + esc(res[hid].error) + '</span>') + '</div>';
      }
      return h + '</div>';
    }

    _render() {
      const d = this._data || {};
      const nlp = d.nlp || {}, media = d.media || {}, ner = d.host_ner || {}, sm = d.summary || {};
      const busyMsg = this._msg && (this._msg.startsWith('failed') || this._msg.startsWith('could not'));
      let h = '<div class="wrap">';
      h += '<div class="hdr"><span class="t">Specialist models</span>'
        + '<span class="muted">NLP ' + (sm.nlp_current || 0) + '/' + (sm.nlp_nodes || 0) + ' nodes current'
        + (sm.nlp_missing_models ? ' · <span class="warn">' + sm.nlp_missing_models + ' missing models</span>' : '')
        + ' · media ' + (sm.media_online || 0) + '/' + (sm.media_nodes || 0) + ' online'
        + ' · builder ' + (d.builder ? (d.builder.ok ? '<span class="ok">up</span>' + (d.builder.free_gb != null ? ' (' + d.builder.free_gb + ' GB free)' : '')
            + ((d.builder.running || []).length ? ' <span class="warn">1 job running</span>' : '') : '<span class="bad">down</span>') : '-')
        + '</span><span class="sp"></span>'
        + (this.getAttribute('tabs') === 'external' ? '' : ['catalog', 'status', 'jobs', 'caches'].map((t) => '<button data-a="tab" data-t="' + t + '"' + (this._tab === t ? ' class="pri"' : '') + '>'
            + { status: 'Nodes', catalog: 'Catalog', jobs: 'Builds', caches: 'Node caches' }[t] + '</button>').join('')) + '</div>';
      if (d.sandbox) h += '<div class="note warn">This is a dev sandbox: updating nodes and installing models from here is refused - use the host.</div>';
      if (this._msg) h += '<div class="note ' + (busyMsg ? 'bad' : 'ok') + '">' + esc(this._msg) + '</div>';
      if (this._tab !== 'status') {
        h += this._tab === 'catalog' ? this._renderCatalog() : this._tab === 'jobs' ? this._renderJobs() : this._renderCaches();
        h += '</div>';
        if (this._paint(h)) this._wire();
        return;
      }
      h += '<div class="row"><span class="sp"></span><button data-a="deep">' + (this._deep ? 'Hide details' : 'Details') + '</button>'
        + '<button data-a="reprobe"' + (this._busy.reprobe ? ' disabled' : '') + '>Re-probe</button>'
        + '<button data-a="plan"' + (this._busy.plan ? ' disabled' : '') + '>' + (this._busy.plan ? 'Checking nodes…' : 'Check versions') + '</button></div>';
      h += this._renderPlan();

      // NLP
      const pl = nlp.placement || {};
      h += '<div class="sec">NLP - nlp_server · host version <span class="mono">' + esc((d.versions || {}).nlp_server || '-') + '</span>'
        + ' · next call → ' + esc(pl.node || pl.where || '-') + '</div>';
      if (nlp.error) h += '<div class="note bad">' + esc(nlp.error) + '</div>';
      h += '<div class="grid">';
      for (const n of (nlp.nodes || [])) {
        h += '<div class="card"><div class="row"><span class="name">' + esc(n.node_id) + '</span>'
          + '<span class="muted mono">' + esc(n.url) + '</span><span class="sp"></span>' + state(n.state) + '</div>'
          + '<div class="row muted">version <span class="mono">' + esc(n.version || '-') + '</span> · ' + esc(n.threads || '?') + ' threads'
          + ((n.changed || []).length ? ' · differs: <span class="mono">' + esc(n.changed.join(', ')) + '</span>' : '') + '</div>'
          + '<table>' + Object.keys(n.tasks || {}).map((t) => {
              const r = n.tasks[t];
              return '<tr><td>' + esc(t) + '</td><td class="m">' + esc(r.model) + '</td><td>' + yes(r.present, 'in store', 'missing')
                + '</td><td>' + (r.loaded ? '<span class="ok">loaded</span>' : '<span class="muted">cold</span>') + '</td></tr>';
            }).join('') + '</table></div>';
      }
      if (!(nlp.nodes || []).length && this._data) h += '<div class="muted">No node answers on the NLP port.</div>';
      h += '</div>';

      // Media
      h += '<div class="sec">Media - gpu_inference · host version <span class="mono">' + esc((d.versions || {}).gpu_inference || '-') + '</span></div><div class="grid">';
      for (const m of (media.nodes || [])) {
        const s = m.serves || {}, c = m.component;
        h += '<div class="card"><div class="row"><span class="name">' + esc(m.label) + '</span><span class="muted mono">' + esc(m.url) + '</span><span class="sp"></span>'
          + (m.status === 'online' ? '<b class="ok">online</b>' : '<b class="' + (m.status === 'offline' ? 'bad' : 'muted') + '">' + esc(m.status) + '</b>') + '</div>'
          + '<div class="row">' + ['stt', 'tts', 'imagegen'].map((k) => '<span class="badge ' + ((m.services || []).indexOf(k) >= 0 ? 'ok' : 'muted') + '">' + k.toUpperCase() + '</span>').join('')
          + (m.in_use ? '<span class="warn">' + m.in_use + ' in use</span>' : '') + '</div>'
          + '<div class="muted">' + ['tts_engine', 'device', 'gpu', 'sd_model', 'loras', 'voices'].filter((k) => s[k] != null && s[k] !== '')
              .map((k) => esc(k.replace('_', ' ')) + ' <span class="mono">' + esc(typeof s[k] === 'object' ? JSON.stringify(s[k]) : s[k]) + '</span>').join(' · ') + '</div>'
          + (s.image_tiers ? '<div class="muted">image tiers: ' + esc(s.image_tiers.join(', ')) + '</div>' : '')
          + (s.model_store ? '<div class="muted">from the shared store: ' + (Object.keys(s.model_store).filter((k) => k !== 'store' && s.model_store[k]).map(esc).join(', ') || '<span class="warn">nothing</span>')
              + (s.model_store.store ? '' : ' <span class="bad">(store not mounted)</span>') + '</div>'
            : '<div class="muted">model sources: not reported (server predates the store)</div>')
          + (c ? '<div class="row muted">deployed ' + state(c.state) + (c.version ? ' <span class="mono">' + esc(c.version) + '</span>' : '')
              + (c.note ? ' · ' + esc(c.note) : '') + (c.error ? ' <span class="bad">' + esc(c.error) + '</span>' : '') + '</div>' : '')
          + '</div>';
      }
      if (!(media.nodes || []).length && this._data) h += '<div class="muted">No media node registered.</div>';
      h += '</div>';

      // Host NER
      const onNodes = String(ner.backend || '').indexOf('node') === 0;
      h += '<div class="sec">Entity NER (fabric entity graph)</div>'
        + '<div class="card"><div class="row">backend <b class="mono">' + esc(ner.backend || '-') + '</b>'
        + (onNodes ? ' <span class="badge ok">on the nodes</span>' : ' <span class="badge warn">in-process on the host</span>') + '</div>'
        + '<div class="muted note">' + (onNodes ? 'GLiNER (or the OntoNotes NER) served by the node NLP servers above; the host loads no model.'
            : 'The host runs it itself - the nodes are not answering nlp.ner. fabric.entity_graph.ner switches it.') + '</div></div>';

      if (!this._data && !this._msg) h += '<div class="muted">Loading…</div>';
      h += '</div>';
      if (this._paint(h)) this._wire();
    }

    _renderCatalog() {
      const c = this._cat, d = c.data || {}, fams = d.families || {};
      const sandbox = (this._data || {}).sandbox;
      const all = d.entries || [];
      if (d.store_error) return '<div class="note warn">store not readable: ' + esc(d.store_error) + '</div>';
      if (!c.data) return '<div class="muted">Loading the catalog...</div>';
      // loaded-on-N: what the node NLP servers report per task
      const nodes = ((this._data || {}).nlp || {}).nodes || [];
      const loadedOn = (e) => e.family !== 'nlp' ? null : nodes.filter((n) => {
        const t = (n.tasks || {})[e.task] || {}; return t.model === e.model && t.loaded; }).length;
      const count = (f) => all.filter((e) => f === 'all' || e.family === f).length;
      let h = '<div class="row"><input class="q" data-a="cat-text" placeholder="filter by name, task or note" value="' + esc(c.text) + '">'
        + '<span class="muted">' + all.length + ' curated models · ' + all.filter((e) => e.built).length + ' in the store</span></div>';
      h += '<div class="row">' + ['all'].concat(Object.keys(fams)).map((f) => '<button class="chip' + (c.fam === f ? ' on' : '') + '" data-a="cat-fam" data-f="' + f + '">'
          + esc(f === 'all' ? 'All' : ((fams[f] || {}).label || f)) + ' <span class="muted">' + count(f) + '</span></button>').join('') + '</div>';
      if (c.fam === 'nlp') {
        const tasks = Array.from(new Set(all.filter((e) => e.family === 'nlp').map((e) => e.task))).sort();
        h += '<div class="row">' + [''].concat(tasks).map((t) => '<button class="chip' + (c.task === t ? ' on' : '') + '" data-a="cat-task" data-t="' + esc(t) + '">'
            + esc(t || 'every task') + '</button>').join('') + '</div>';
      }
      const shown = all.filter((e) => (c.fam === 'all' || e.family === c.fam) && (!c.task || e.task === c.task));
      const order = (e) => (e.in_use ? 0 : e.built ? 1 : 2);
      const groups = {};
      shown.forEach((e) => { (groups[e.family] = groups[e.family] || []).push(e); });
      const row = (e, hf) => {
        const n = hf ? null : loadedOn(e);
        const text = [e.model, e.task, e.family, e.note].join(' ').toLowerCase();
        return '<tr data-text="' + esc(text) + '"><td class="m"><a class="hf" target="_blank" rel="noopener" href="https://huggingface.co/' + esc(e.model) + '">' + esc(e.model) + '</a>'
          + '<div class="muted">' + esc(e.note || '') + '</div></td>'
          + '<td>' + esc(e.task || '') + '</td>'
          + '<td>' + (e.in_use ? '<span class="badge ok">in use</span> ' : '') + (e.built ? '<span class="badge ok">in store</span> ' : '')
          + (n ? '<span class="badge ok" title="loaded on ' + n + ' NLP node(s)">loaded ' + n + '/' + nodes.length + '</span> ' : '')
          + (hf ? (e.curated ? '<span class="badge">curated</span>' : '<span class="badge warn">unvetted</span>') + ' <span class="muted">' + esc(e.downloads || 0) + ' dl</span>' : '') + '</td>'
          + '<td><button data-a="install" data-e="' + esc(e.id || '') + '" data-hf="' + (hf ? '1' : '') + '" data-m="' + esc(e.model) + '" data-fam="' + esc(e.family || c.fam) + '" data-task="' + esc(e.task || c.task || '') + '"'
          + (sandbox || this._busy['inst:' + (e.id || e.model)] || (e.built && !hf) ? ' disabled' : '') + '>'
          + (this._busy['inst:' + (e.id || e.model)] ? 'Queuing...' : e.built && !hf ? 'In store' : ((e.family || c.fam) === 'nlp' ? 'Export' : 'Download')) + '</button></td></tr>';
      };
      h += '<table><tr><th style="text-align:left">model</th><th style="text-align:left">task</th><th style="text-align:left">status</th><th></th></tr>'
        + Object.keys(groups).map((f) => '<tr class="fam"><td colspan="4">' + esc((fams[f] || {}).label || f) + ' <span class="muted">- ' + esc((fams[f] || {}).serves || '') + '</span></td></tr>'
            + groups[f].sort((a, b) => order(a) - order(b)).map((e) => row(e, false)).join('')).join('')
        + (shown.length ? '' : '<tr><td colspan="4" class="muted">nothing curated here</td></tr>') + '</table>';
      // Hugging Face: for one family (and, for NLP, one task)
      const canHf = ['nlp', 'sd', 'gliner'].indexOf(c.fam) >= 0 && !(c.fam === 'nlp' && (!c.task || c.task === 'rerank'));
      if (canHf) {
        h += '<div class="sec">More on Hugging Face</div><div class="row"><input class="q" data-a="cat-q" value="' + esc(c.query) + '" placeholder="search ' + esc(c.fam === 'nlp' ? c.task + ' models' : c.fam) + '">'
          + '<button data-a="cat-search"' + (this._busy.hf ? ' disabled' : '') + '>' + (this._busy.hf ? 'Searching...' : 'Search') + '</button></div>';
        if (c.hf && c.hf.error) h += '<div class="note bad">' + esc(c.hf.error) + '</div>';
        if (c.hf && c.hf.results) h += '<div class="muted note">Unvetted picks: the build reports whether the model exports and loads. Putting a model in the store does not switch a node to it.</div>'
          + '<table>' + c.hf.results.map((x) => row(Object.assign({ family: c.fam, task: c.task }, x), true)).join('') + '</table>';
      } else if (c.fam === 'nlp') {
        h += '<div class="muted note">Pick one task to search Hugging Face for more.</div>';
      }
      return h;
    }

    _applyCatFilter() {
      const q = (this._cat.text || '').trim().toLowerCase();
      this._root.querySelectorAll('tr[data-text]').forEach((tr) => { tr.style.display = !q || tr.dataset.text.indexOf(q) >= 0 ? '' : 'none'; });
    }

    _renderJobs() {
      const jobs = (this._jobs || {}).jobs;
      if (this._jobs && this._jobs.ok === false) return '<div class="note bad">' + esc(this._jobs.error) + '</div>';
      if (!jobs) return '<div class="muted">Loading…</div>';
      let h = '<table>' + (jobs.length ? '' : '<tr><td class="muted">No builds since the builder started.</td></tr>') + jobs.map((j) => {
        const cls = { done: 'ok', failed: 'bad', running: 'warn', queued: 'muted' }[j.state] || 'muted';
        const dur = j.started_at ? Math.round(((j.ended_at || Date.now() / 1000) - j.started_at)) + 's' : '';
        return '<tr><td><b class="' + cls + '">' + esc(j.state) + '</b></td><td class="m">' + esc(j.model) + (j.task ? ' (' + esc(j.task) + ')' : '')
          + '<div class="muted">' + esc(j.kind) + ' · ' + esc(j.family) + (dur ? ' · ' + dur : '') + '</div>'
          + (j.error ? '<div class="bad">' + esc(j.error) + '</div>' : '') + '</td><td><button data-a="job" data-j="' + esc(j.id) + '">Log</button></td></tr>';
      }).join('') + '</table>';
      if (this._job) h += '<div class="card"><div class="row"><span class="name">' + esc(this._job.model) + '</span><span class="muted">' + esc(this._job.state) + '</span><span class="sp"></span><button data-a="job-close">Close</button></div>'
        + '<pre style="margin:0;max-height:320px;overflow:auto;font:10px/1.35 var(--mono,ui-monospace,monospace);white-space:pre-wrap">' + esc((this._job.log || []).join('\n')) + '</pre></div>';
      return h;
    }

    _renderCaches() {
      const d = this._caches;
      if (!d) return '<div class="muted">Reading each node\'s caches over SSH…</div>';
      if (d.ok === false) return '<div class="note bad">' + esc(d.error) + '</div>';
      let h = '<div class="muted note">Models in each node\'s own caches, outside the shared store. Copies across user caches each take their own disk.</div><div class="grid">';
      for (const n of (d.nodes || [])) {
        h += '<div class="card"><div class="row"><span class="name">' + esc(n.host) + '</span><span class="sp"></span><span class="muted">' + Math.round((n.total_mb || 0) / 102.4) / 10 + ' GB</span></div>'
          + (n.error ? '<div class="note bad">' + esc(n.error) + '</div>' : '')
          + '<table>' + (n.models || []).map((m) => '<tr><td>' + esc(m.kind) + '</td><td class="m">' + esc(m.model) + '</td><td>' + (m.size_mb >= 1024 ? (Math.round(m.size_mb / 102.4) / 10) + ' GB' : m.size_mb + ' MB')
            + '</td><td class="' + (m.paths.length > 1 ? 'warn' : 'muted') + '" title="' + esc(m.paths.join('\n')) + '">' + m.paths.length + ' cop' + (m.paths.length > 1 ? 'ies' : 'y') + '</td></tr>').join('')
          + ((n.models || []).length ? '' : '<tr><td class="muted">nothing in the known caches</td></tr>') + '</table></div>';
      }
      return h + '</div>';
    }

    _wire() {
      if (this._tab === 'catalog') this._applyCatFilter();      // a repaint shows every row again
      this._root.querySelectorAll('[data-a]').forEach((el) => {
        const a = el.dataset.a;
        if (a === 'cat-fam') { el.addEventListener('click', () => { this._cat.fam = el.dataset.f; this._cat.task = ''; this._cat.hf = null; this._render(); this._applyCatFilter(); }); return; }
        if (a === 'cat-task') { el.addEventListener('click', () => { this._cat.task = el.dataset.t; this._cat.hf = null; this._render(); this._applyCatFilter(); }); return; }
        // filtering hides rows in place: no repaint, so the field keeps focus
        if (a === 'cat-text') { el.addEventListener('input', () => { this._cat.text = el.value; this._applyCatFilter(); }); return; }
        if (a === 'cat-q') { el.addEventListener('input', () => { this._cat.query = el.value; }); el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') this._root.querySelector('[data-a="cat-search"]').click(); }); return; }
        el.addEventListener('click', () => {
          if (a === 'tab') { this._tab = el.dataset.t; this._msg = ''; if (this._tab === 'caches') this._caches = null; this._render(); this._loadTab(); }
          else if (a === 'cat-search') {
            const c = this._cat;
            this._busy.hf = true; this._render();
            const q = ['family=' + encodeURIComponent(c.fam), 'hf=true', 'query=' + encodeURIComponent(c.query)];
            if (c.fam === 'nlp') q.push('task=' + encodeURIComponent(c.task));
            this._call('GET', '/specialist/catalog?' + q.join('&')).then((r) => { c.hf = r.hf || { error: r.error }; })
              .catch((e) => { c.hf = { error: e.message }; }).finally(() => { delete this._busy.hf; this._render(); });
          }
          else if (a === 'install') {
            const id = el.dataset.e || el.dataset.m, fam = el.dataset.fam;
            const body = el.dataset.hf ? { family: fam, model: el.dataset.m, task: fam === 'nlp' ? el.dataset.task : '' } : { entry: el.dataset.e };
            this._busy['inst:' + id] = true; this._render();
            this._call('POST', '/specialist/install', body).then((r) => {
              this._msg = r.ok === false ? 'failed: ' + r.error : 'queued on the builder - see Builds';
            }).catch((e) => { this._msg = 'failed: ' + e.message; }).finally(() => { delete this._busy['inst:' + id]; this._cat.data = null; this._loadTab(); });
          }
          else if (a === 'job') { this._call('GET', '/specialist/jobs?job_id=' + encodeURIComponent(el.dataset.j)).then((r) => { this._job = r; this._render(); }).catch((e) => { this._msg = 'failed: ' + e.message; this._render(); }); }
          else if (a === 'job-close') { this._job = null; this._render(); }
          else if (a === 'deep') { this._deep = !this._deep; this.refresh(); }
          else if (a === 'reprobe') this._act('reprobe', async () => ({}), 'nodes re-probed');
          else if (a === 'plan') this._act('plan', async () => { this._plan = await this._call('POST', '/provision/component/sync', { component: 'nlp_server', dry_run: true }); return this._plan; });
          else if (a === 'plan-close') { this._plan = null; this._render(); }
          else if (a === 'sync-run') this._act('sync', async () => { this._plan = await this._call('POST', '/provision/component/sync', { component: 'nlp_server', dry_run: false }); return this._plan; }, 'nodes updated');
        });
      });
    }
  }
  customElements.define('vera-specialist-models', VeraSpecialistModels);
})();
