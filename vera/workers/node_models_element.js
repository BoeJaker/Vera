/**
 * <vera-node-models> - the models the estate keeps ready, and the NLP it runs,
 * in one pane.
 *
 * Warm slots  per node: its slots (GPU 1, CPU 2 - the embedder rides beside
 *             them), the models planned there and where the plan came from,
 *             what is resident now (pinned or expiring), what was dropped and
 *             why. Edit a node's models; apply now.
 * Workloads   the scenarios that take the slots over while a job type runs
 *             hot (e.g. coders into every slot) - their demand, state, edit.
 * Routing     the CPU window planned models share, and the warm-spill limits.
 * NLP         where nlp.* runs (placement, the node chosen and why, versions),
 *             the host-NLP switch, the node pin, and the LLM-NLP master switch.
 * Catalog     the specialist (non-LLM) model catalog, per node - the
 *             <vera-specialist-models> element, embedded.
 *
 * Data: ollama.warm.status|set|apply, nlp.nodes, nlp.config.get|set,
 *       fabric.nlp.get|set, specialist.status (via the embedded element).
 * Attributes: api-base (default ''), refresh (seconds, default 15)
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
      this._msg = '';
    }
    get base() { return this.getAttribute('api-base') || ''; }
    connectedCallback() {
      this._root.innerHTML = '<style>' + CSS + '</style><div class="wrap" id="w"></div>';
      this._root.addEventListener('click', (e) => this._click(e));
      this._root.addEventListener('change', (e) => this._change(e));
      this.load();
      const n = Math.max(5, parseInt(this.getAttribute('refresh') || '15', 10));
      this._t = setInterval(() => { if (!this._edit && !document.hidden) this.load(); }, n * 1000);
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
    async load() {
      const [w, n, c, l] = await Promise.all([
        this._get('/ollama/warm/status'), this._get('/nlp/nodes'),
        this._get('/nlp/config'), this._get('/fabric/nlp/config')]);
      this._warm = w; this._nlp = n; this._ncfg = c; this._llmnlp = l;
      this.render();
    }
    _say(m) { this._msg = m; const el = this._root.getElementById('msg'); if (el) el.textContent = m; }

    render() {
      const w = this._warm || {};
      const el = this._root.getElementById('w');
      if (!el) return;
      const catalogHtml = this._root.getElementById('cat') ? null : '<div class="panel"><div class="sec">Catalog - specialist (non-LLM) models</div><vera-specialist-models id="cat"></vera-specialist-models></div>';
      const top = '<div class="hdr"><span class="t">Models &amp; NLP</span>'
        + '<button data-a="warm-toggle" class="' + (w.enabled ? 'on' : '') + '" title="Keep each node\'s planned models loaded">' + (w.enabled ? 'warm slots on' : 'warm slots off') + '</button>'
        + (w.census_busy ? '<span class="badge warn" title="scenarios stay off and the GPU is left alone while a census goal runs">census running</span>' : '')
        + '<span class="sp"></span><span class="muted msg" id="msg">' + esc(this._msg) + '</span>'
        + '<button data-a="preview">preview</button><button data-a="apply" title="Load what is missing now (one model per node)">apply now</button><button data-a="refresh">refresh</button></div>';
      const body = top + this._nodesHtml(w) + this._scenariosHtml(w) + this._routingHtml(w) + this._nlpHtml();
      // keep the embedded catalog element alive across re-renders
      let cat = this._root.getElementById('cat');
      const holder = cat ? cat.parentElement : null;
      if (holder) holder.remove();
      el.innerHTML = body;
      if (holder) el.appendChild(holder);
      else {
        el.insertAdjacentHTML('beforeend', catalogHtml);
        if (!customElements.get('vera-specialist-models') && !document.querySelector('script[data-spm]')) {
          const s = document.createElement('script'); s.src = this.base + '/ui/elements/specialist_models.js'; s.dataset.spm = '1';
          document.head.appendChild(s);
        }
      }
    }

    _nodesHtml(w) {
      const nodes = w.nodes || [];
      if (!nodes.length) return '<div class="muted">no nodes</div>';
      const cards = nodes.map((n) => {
        const res = n.resident || [];
        const isRes = (m) => res.find((r) => String(r.model).replace(/:latest$/, '') === String(m).replace(/:latest$/, ''));
        const clsBadge = n.class === 'gpu' ? '<span class="badge gpu">GPU</span>' : n.class === 'cpu_sibling' ? '<span class="badge cpu" title="the CPU-only Ollama beside a GPU node">CPU sibling</span>' : '<span class="badge cpu">CPU</span>';
        const slots = (n.planned || []).map((p) => {
          const r = isRes(p.model);
          const state = r ? (r.pinned ? '<span class="ok" title="kept loaded until the plan drops it">pinned</span>' : '<span class="warn">lapses in ' + dur(r.expires_in_s) + '</span>') : '<span class="warn">not loaded</span>';
          return '<div class="slot ' + (r ? 'warm' : 'cold') + '"><span class="mono">' + esc(p.model) + '</span><span class="muted">' + (p.num_ctx ? esc(p.num_ctx) + ' ctx' : '') + '</span><span class="sp"></span>' + state + '</div>';
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
      return '<div class="sec">Warm slots</div><div class="grid">' + cards + '</div>' + acts + last;
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

    _nlpHtml() {
      const n = this._nlp || {}; const c = (this._ncfg || {}).config || {}; const l = this._llmnlp || {};
      const nodes = (n.nodes || []).map((x) => {
        const ver = (x.component || {}).version || '';
        return '<tr><td class="mono">' + esc(x.node_id) + (x.node_id === n.node ? ' <span class="badge ok">next</span>' : '') + '</td>'
          + '<td class="mono">' + esc(ver) + ((x.component || {}).intact === false ? ' <span class="bad">edited</span>' : '') + '</td>'
          + '<td>' + esc(x.runners != null ? x.runners : '') + '</td>'
          + '<td>' + (x.mem_available_mb != null ? esc(Math.round(x.mem_available_mb / 1024)) + ' GB free' : '') + '</td>'
          + '<td>' + esc((x.load || [])[0] != null ? x.load[0] : '') + '</td></tr>';
      }).join('');
      const pins = ['<option value="">choose by signals</option>'].concat((n.candidates || []).map((id) => '<option value="' + esc(id) + '"' + (c.node === id ? ' selected' : '') + '>' + esc(id) + '</option>')).join('');
      return '<div class="panel"><div class="sec">NLP</div>'
        + '<div class="kv"><span class="muted">runs</span><span><b>' + esc(n.where || '?') + '</b> &middot; ' + esc(n.reason || '') + '</span>'
        + '<span class="muted">next call</span><span class="mono">' + esc(n.why || '-') + '</span>'
        + '<span class="muted">host may run NLP</span><span><input type="checkbox" data-a="nlp-local"' + (c.nlp_local ? ' checked' : '') + '> <span class="muted">off = never on the host, even with no node</span></span>'
        + '<span class="muted">pin to node</span><span><select data-a="nlp-pin">' + pins + '</select> timeout <input id="nlp-timeout" type="number" style="width:60px" value="' + esc(c.timeout_s) + '"> s <button data-a="nlp-timeout-save">save</button></span>'
        + '<span class="muted">LLM NLP in pipelines</span><span><input type="checkbox" data-a="llm-nlp"' + (l.enabled ? ' checked' : '') + '> <span class="muted">off = ingestion, discovery and loops use regex/spaCy/node NER only</span></span></div>'
        + '<table><tr><th>node</th><th>nlp_server</th><th>runners</th><th>memory</th><th>load</th></tr>' + (nodes || '<tr><td colspan="5" class="muted">no node answers</td></tr>') + '</table></div>';
    }

    async _click(e) {
      const b = e.target.closest('[data-a]'); if (!b || b.tagName === 'INPUT' || b.tagName === 'SELECT') return;
      const a = b.dataset.a, k = b.dataset.k;
      if (a === 'refresh') return this.load();
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
      if (a === 'nlp-timeout-save') { const r = await this._post('/nlp/config/set', { timeout_s: parseFloat(this._root.getElementById('nlp-timeout').value) }); this._say(r.ok ? 'saved' : (r.error || 'failed')); return this.load(); }
    }
    async _change(e) {
      const t = e.target; const a = t.dataset && t.dataset.a; if (!a) return;
      let r = null;
      if (a === 'sc-enable') r = await this._post('/ollama/warm/set', { scenario_enabled: { [t.dataset.k]: t.checked } });
      if (a === 'nlp-local') {
        if (t.checked && !confirm('Let the Vera host run NLP in-process when no node serves it?')) { t.checked = false; return; }
        r = await this._post('/nlp/config/set', { nlp_local: t.checked });
      }
      if (a === 'nlp-pin') r = await this._post('/nlp/config/set', { node: t.value });
      if (a === 'llm-nlp') {
        if (t.checked && !confirm('Turn LLM NLP on for automatic pipelines? It sends ingestion and discovery NLP to the LLMs.')) { t.checked = false; return; }
        r = await this._post('/fabric/nlp/set', { enabled: t.checked });
      }
      if (r) { this._say(r.ok === false ? (r.error || 'failed') : 'saved'); this.load(); }
    }
  }
  customElements.define('vera-node-models', VeraNodeModels);
})();
