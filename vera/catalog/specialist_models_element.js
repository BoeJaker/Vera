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
 * Attributes: api-base (default ''), refresh (seconds, default 30)
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
      this._data = null; this._plan = null; this._busy = {}; this._msg = ''; this._deep = false; this._timer = null;
    }
    connectedCallback() {
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
      this._render();
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
      let h = '<style>' + CSS + '</style><div class="wrap">';
      h += '<div class="hdr"><span class="t">Specialist models</span>'
        + '<span class="muted">NLP ' + (sm.nlp_current || 0) + '/' + (sm.nlp_nodes || 0) + ' nodes current'
        + (sm.nlp_missing_models ? ' · <span class="warn">' + sm.nlp_missing_models + ' missing models</span>' : '')
        + ' · media ' + (sm.media_online || 0) + '/' + (sm.media_nodes || 0) + ' online</span><span class="sp"></span>'
        + '<button data-a="deep">' + (this._deep ? 'Hide details' : 'Details') + '</button>'
        + '<button data-a="reprobe"' + (this._busy.reprobe ? ' disabled' : '') + '>Re-probe</button>'
        + '<button data-a="plan"' + (this._busy.plan ? ' disabled' : '') + '>' + (this._busy.plan ? 'Checking nodes…' : 'Check versions') + '</button></div>';
      if (d.sandbox) h += '<div class="note warn">This is a dev sandbox: updating nodes from here is refused - use the host.</div>';
      if (this._msg) h += '<div class="note ' + (busyMsg ? 'bad' : 'ok') + '">' + esc(this._msg) + '</div>';
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
          + (c ? '<div class="row muted">deployed ' + state(c.state) + (c.version ? ' <span class="mono">' + esc(c.version) + '</span>' : '')
              + (c.note ? ' · ' + esc(c.note) : '') + (c.error ? ' <span class="bad">' + esc(c.error) + '</span>' : '') + '</div>' : '')
          + '</div>';
      }
      if (!(media.nodes || []).length && this._data) h += '<div class="muted">No media node registered.</div>';
      h += '</div>';

      // Host NER
      h += '<div class="sec">Entity NER on the host (fabric entity graph)</div>'
        + '<div class="card"><div class="row">backend <b class="mono">' + esc(ner.backend || '-') + '</b>'
        + ' · spaCy ' + yes(ner.spacy_installed, 'installed', 'not installed') + ' <span class="mono muted">' + esc(ner.spacy_model) + '</span>'
        + ' · GLiNER ' + yes(ner.gliner_installed, 'installed', 'not installed') + ' <span class="mono muted">' + esc(ner.gliner_model) + '</span></div>'
        + '<div class="muted note">Runs in-process on the host, not on the nodes. Switch or install via fabric.entity_graph.ner / ner_install.</div></div>';

      if (!this._data && !this._msg) h += '<div class="muted">Loading…</div>';
      h += '</div>';
      this._root.innerHTML = h;
      this._root.querySelectorAll('[data-a]').forEach((el) => {
        const a = el.dataset.a;
        el.addEventListener('click', () => {
          if (a === 'deep') { this._deep = !this._deep; this.refresh(); }
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
