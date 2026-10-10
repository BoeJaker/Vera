/**
 * <vera-backend-switch> - which LLM backend serves each node, and the controls to
 * change it (owner, 9 Oct 2026: in Node settings, and in both the Ollama and the
 * vLLM pages). One element, mounted in all three.
 *
 *   per node    what serves it now (Ollama | vLLM, live), the vLLM model, its
 *               thinking default; Ollama | vLLM switch; the vLLM setup it switches
 *               with (store model, names, flags, stands-in, thinking) - saved
 *               without switching, or saved and switched
 *   embeddings  Ollama | vLLM (the vLLM embedding server, vllm_embed)
 *   bulk        All -> Ollama / vLLM (embeddings follow), ticked -> Ollama / vLLM
 *
 * Every switch: a dry run, a confirm saying what each node will do, then the
 * background job (nodes.backend.switch_many), its progress shown in place.
 *
 * Data: nodes.backend.overview | config | config.set | switch_many | switch_status
 *       | embeddings. Attributes: api-base (default ''), compact (no intro line).
 * Public: el.switchNodes(ids, backend)
 */
(function () {
  if (customElements.get('vera-backend-switch')) return;

  const CSS = `
  :host{display:block;font:11px/1.45 var(--sans,-apple-system,system-ui,sans-serif);color:var(--fg,var(--text,#d8dde3))}
  .panel{background:var(--bg1,#14181d);border:1px solid var(--border,#232a33);border-radius:7px;padding:9px 11px;display:flex;flex-direction:column;gap:7px}
  .row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .t{font:600 10px/1 var(--mono,ui-monospace,monospace);letter-spacing:.12em;text-transform:uppercase;color:var(--acc,#4a9eff)}
  .muted{color:var(--dim,#5f6975)} .mono{font-family:var(--mono,ui-monospace,monospace)} .sp{flex:1}
  button,select,input{font:inherit;font-size:10.5px;background:var(--bg2,#1a1f26);color:inherit;border:1px solid var(--border2,#2e3742);border-radius:4px;padding:3px 8px}
  button{cursor:pointer} button:hover{border-color:var(--acc,#4a9eff)} button:disabled{opacity:.5;cursor:default}
  .seg{display:inline-flex} .seg button{border-radius:0} .seg button:first-child{border-radius:4px 0 0 4px} .seg button:last-child{border-radius:0 4px 4px 0;margin-left:-1px}
  .seg button.on{background:color-mix(in srgb, var(--acc,#4a9eff) 18%, transparent);border-color:var(--acc,#4a9eff);color:var(--acc,#4a9eff);font-weight:600}
  table{border-collapse:collapse;width:100%;font-size:10.5px}
  th{text-align:left;color:var(--dim,#5f6975);font-weight:600;padding:4px 8px 4px 0;border-bottom:1px solid var(--border2,#2e3742);white-space:nowrap}
  td{padding:5px 8px 5px 0;border-top:1px solid var(--border,#232a33);vertical-align:middle}
  .badge{font:600 9px/1 var(--mono,ui-monospace,monospace);padding:3px 5px;border-radius:3px;border:1px solid currentColor;white-space:nowrap}
  .ollama{color:var(--warn,#f5b341)} .vllm{color:var(--acc,#4a9eff)} .ok{color:var(--acc2,var(--ok,#28c28a))} .bad{color:var(--err,#ef5b5b)}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px;vertical-align:middle}
  .setup td{background:var(--bg0,#0d0f12)}
  .kv{display:grid;grid-template-columns:max-content 1fr;gap:5px 10px;align-items:center}
  .kv input,.kv select{width:100%;box-sizing:border-box}
  .status{font-family:var(--mono,ui-monospace,monospace);font-size:10px;white-space:pre-wrap}
  `;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const short = (m) => String(m || '').split('/').pop();
  const up = (s) => s === 'online' || s === 'degraded';

  class VeraBackendSwitch extends HTMLElement {
    constructor() {
      super();
      this._r = this.attachShadow({ mode: 'open' });
      this._r.innerHTML = '<style>' + CSS + '</style><div id="w"></div>';
      this._o = null; this._sel = new Set(); this._open = ''; this._cfg = {}; this._status = ''; this._t = null;
      this._r.addEventListener('click', (e) => this._click(e));
      this._r.addEventListener('change', (e) => this._change(e));
    }
    get base() { return this.getAttribute('api-base') || ''; }
    connectedCallback() { this.load(); this._iv = setInterval(() => { if (!document.hidden && !this._open) this.load(); }, 20000); }
    disconnectedCallback() { clearInterval(this._iv); clearTimeout(this._t); }
    async _get(p) { try { const r = await fetch(this.base + p); return r.ok ? await r.json() : null; } catch (e) { return null; } }
    async _post(p, b) {
      try { const r = await fetch(this.base + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) }); return await r.json(); }
      catch (e) { return { ok: false, error: String(e) }; }
    }
    async load() {
      this._o = await this._get('/nodes/backend/overview');
      const j = this._o && this._o.job;
      if (j && j.state === 'running') { this._jobText(j); this._poll(); }
      this.render();
    }
    _say(t) { this._status = t; const e = this._r.getElementById('st'); if (e) e.textContent = t; }

    render() {
      const o = this._o;
      const w = this._r.getElementById('w');
      if (!o) { w.innerHTML = '<div class="panel"><span class="t">LLM backend</span><span class="muted">loading...</span></div>'; return; }
      const em = o.embeddings || {};
      let h = '<div class="panel">'
        + '<div class="row"><span class="t">LLM backend</span>'
        + (this.hasAttribute('compact') ? '' : '<span class="muted">Each node serves Ollama or vLLM; it keeps its id, workers and routing either way.</span>')
        + '<span class="sp"></span><button data-a="reload">reload</button></div>'
        + '<div class="row">'
        + '<button data-a="all" data-b="ollama" title="Every node, and embeddings">All → Ollama</button>'
        + '<button data-a="all" data-b="vllm" title="Every node, and embeddings">All → vLLM</button>'
        + '<button data-a="ticked" data-b="ollama">Ticked → Ollama</button>'
        + '<button data-a="ticked" data-b="vllm">Ticked → vLLM</button>'
        + '<span class="sp"></span><span class="muted">Embeddings</span>'
        + '<span class="seg"><button data-a="emb" data-b="ollama" class="' + (em.backend !== 'vllm' ? 'on' : '') + '">Ollama</button>'
        + '<button data-a="emb" data-b="vllm" class="' + (em.backend === 'vllm' ? 'on' : '') + '"' + ((em.servers || []).length ? '' : ' disabled title="no vLLM embedding server installed (component vllm_embed)"') + '>vLLM</button></span>'
        + (em.servers || []).map((s) => '<span class="muted mono" title="vLLM embedding server"><span class="dot" style="background:' + (up(s.status) ? 'var(--acc2,#28c28a)' : 'var(--dim,#5f6975)') + '"></span>' + esc(s.id) + '</span>').join(' ')
        + '</div>'
        + '<div class="status muted" id="st">' + esc(this._status) + '</div>'
        + '<table><tr><th></th><th>Node</th><th>Serving</th><th>Model</th><th>Thinking</th><th>Backend</th><th></th></tr>';
      for (const n of o.nodes || []) {
        const v = n.vllm || {}, s = n.setup || {}, onv = n.backend === 'vllm';
        const st = onv ? v.status : n.status;
        const model = onv ? short((v.models || [])[0]) : (s.model ? '<span class="muted">next vLLM: ' + esc(short(s.model)) + '</span>' : '<span class="muted">-</span>');
        const think = onv ? (v.think_default || 'model default') : (s.think_default || '');
        h += '<tr><td><input type="checkbox" data-sel="' + esc(n.instance) + '"' + (this._sel.has(n.instance) ? ' checked' : '') + '></td>'
          + '<td><b>' + esc(n.label || n.instance) + '</b> <span class="muted mono">' + esc(n.instance) + '</span> <span class="badge ' + (n.has_gpu ? 'ollama">GPU' : 'vllm">CPU') + '</span></td>'
          + '<td><span class="badge ' + (onv ? 'vllm' : 'ollama') + '"><span class="dot" style="background:' + (up(st) ? 'var(--acc2,#28c28a)' : 'var(--err,#ef5b5b)') + '"></span>' + (onv ? 'vLLM' : 'Ollama') + '</span></td>'
          + '<td class="mono">' + (onv ? esc(model) : model)
          + ((onv && (v.stands_in_for || []).length) ? ' <span class="muted" title="names routed here">+ ' + esc(v.stands_in_for.join(', ')) + '</span>' : '') + '</td>'
          + '<td>' + (onv ? '<select data-think="' + esc(n.instance) + '">' + ['', 'on', 'off'].map((x) => '<option value="' + x + '"' + ((v.think_default || '') === x ? ' selected' : '') + '>' + (x || 'model default') + '</option>').join('') + '</select>' : '<span class="muted">' + esc(think || '-') + '</span>') + '</td>'
          + '<td><span class="seg"><button data-a="one" data-b="ollama" data-k="' + esc(n.instance) + '" class="' + (onv ? '' : 'on') + '">Ollama</button>'
          + '<button data-a="one" data-b="vllm" data-k="' + esc(n.instance) + '" class="' + (onv ? 'on' : '') + '">vLLM</button></span></td>'
          + '<td><button data-a="setup" data-k="' + esc(n.instance) + '">' + (this._open === n.instance ? 'close' : 'vLLM setup') + '</button></td></tr>';
        if (this._open === n.instance) h += this._setupRow(n);
      }
      h += '</table></div>';
      w.innerHTML = h;
    }

    _setupRow(n) {
      const c = this._cfg[n.instance];
      if (!c) return '<tr class="setup"><td></td><td colspan="6" class="muted">reading the node\'s model store...</td></tr>';
      const sg = c.suggested || {};
      const opts = (c.models || []).map((m) => '<option value="' + esc(m.path) + '"' + (m.path === sg.model ? ' selected' : '') + '>' + esc(m.name) + (m.alias ? ' (' + esc(m.alias) + ')' : '') + '</option>').join('');
      const k = esc(n.instance);
      return '<tr class="setup"><td></td><td colspan="6"><div class="kv">'
        + '<span>Model</span><span class="row"><select id="m-' + k + '" data-model="' + k + '">' + opts + '<option value="">other (type below)</option></select>'
        + '<input id="mo-' + k + '" placeholder="or a store path / HF id" value="' + ((c.models || []).some((m) => m.path === sg.model) ? '' : esc(sg.model || '')) + '" style="flex:1"></span>'
        + '<span>Answers to</span><input id="al-' + k + '" value="' + esc((sg.aliases || []).join(', ')) + '" placeholder="Ollama names this model is, e.g. qwen3.5:9b">'
        + '<span>Stands in for</span><input id="si-' + k + '" value="' + esc((sg.stands_in_for || []).join(', ')) + '" placeholder="model names vLLM cannot run here, served by this model instead">'
        + '<span>Flags</span><input id="fl-' + k + '" class="mono" value="' + esc(sg.extra_args || '') + '" placeholder="extra vllm serve flags">'
        + '<span>Thinking</span><select id="th-' + k + '">' + ['', 'on', 'off'].map((x) => '<option value="' + x + '"' + ((sg.think_default || '') === x ? ' selected' : '') + '>' + (x || 'model default') + '</option>').join('') + '</select>'
        + '<span></span><span class="row"><button data-a="save" data-k="' + k + '">Save setup</button>'
        + '<button data-a="save-switch" data-k="' + k + '">' + (n.backend === 'vllm' ? 'Save (restart on vLLM: switch to Ollama, then back)' : 'Save and switch to vLLM') + '</button>'
        + '<span class="muted">' + (c.installed === false ? esc(c.component) + ' is installed first when it switches' : '') + '</span></span>'
        + '</div></td></tr>';
    }

    _setupArgs(k) {
      const g = (id) => ((this._r.getElementById(id + k) || {}).value || '').trim();
      const list = (t) => t.split(',').map((x) => x.trim()).filter(Boolean);
      return { instance_id: k, model: g('mo-') || g('m-'), aliases: list(g('al-')), stands_in_for: list(g('si-')),
               extra_args: g('fl-'), think_default: g('th-') };
    }

    async switchNodes(ids, backend) {
      this._say('Checking...');
      const r = await this._post('/nodes/backend/switch_many', { backend, instance_ids: ids, dry_run: true });
      if (!r || !r.ok) return this._say('✗ ' + ((r && r.error) || 'no response'));
      const rows = r.nodes || [];
      const line = (x) => { const p = x.plan || {};
        return (x.ok === false ? '✗ ' : '') + x.instance + ': ' + (x.skipped || x.error || (p.action === 'switch' ? ('→ ' + p.to + (x.model ? '  ' + short(x.model) : '') + (x.install ? ' (installs ' + x.install + ' first)' : '') + (x.instance === 'embeddings' && p.why ? ' - ' + p.why : '')) : (p.why || p.action || ''))); };
      const todo = rows.filter((x) => x.ok !== false && !x.skipped && ((x.plan || {}).action === 'switch' || x.install));
      if (!todo.length) return this._say(rows.map(line).join(' · ') || 'Nothing to switch.');
      if (!confirm('Switch to ' + (backend === 'vllm' ? 'vLLM' : 'Ollama') + ':\n\n' + rows.map(line).join('\n')
        + '\n\nNodes switch one after another; a model load on a CPU node takes a few minutes.')) return this._say('');
      const g = await this._post('/nodes/backend/switch_many', { backend, instance_ids: ids, dry_run: false });
      if (!g || !g.ok) return this._say('✗ ' + ((g && g.error) || 'no response'));
      this._poll();
    }
    _jobText(j) {
      const done = (j.done || []).map((x) => (x.ok ? '✓ ' : '✗ ') + x.instance + (x.ok ? '' : ' (' + (x.error || 'failed') + ')'));
      this._say(done.concat(j.current ? ['switching ' + j.current + '...'] : []).join(' · ') + (j.state === 'running' ? '' : ' - done'));
    }
    async _poll() {
      clearTimeout(this._t);
      const r = await this._get('/nodes/backend/switch_status'); const j = r && r.job;
      if (!j) return;
      this._jobText(j);
      if (j.state === 'running') this._t = setTimeout(() => this._poll(), 4000);
      else { const s = this._status; await this.load(); this._say(s); }
    }

    async _click(e) {
      const b = e.target.closest('[data-a]'); if (!b || b.disabled) return;
      const a = b.dataset.a, k = b.dataset.k, be = b.dataset.b;
      if (a === 'reload') return this.load();
      if (a === 'all') return this.switchNodes([], be);
      if (a === 'ticked') { if (!this._sel.size) return this._say('Tick the nodes first.'); return this.switchNodes([...this._sel], be); }
      if (a === 'one') { if (b.classList.contains('on')) return; return this.switchNodes([k], be); }
      if (a === 'emb') {
        if (b.classList.contains('on')) return;
        const d = await this._post('/nodes/backend/embeddings', { backend: be, dry_run: true });
        if (!d || !d.ok) return this._say('✗ ' + ((d && (d.error || (d.plan || {}).why)) || 'no response'));
        if (!confirm('Move embeddings to ' + (be === 'vllm' ? 'the vLLM embedding server' : 'Ollama') + '?')) return;
        this._say(be === 'vllm' ? 'Starting the embedding server (about two minutes)...' : 'Moving embeddings to Ollama...');
        const r = await this._post('/nodes/backend/embeddings', { backend: be, dry_run: false });
        this._say(r && r.ok ? '✓ embeddings on ' + r.embeddings : '✗ ' + ((r && r.error) || 'failed'));
        return this.load();
      }
      if (a === 'setup') {
        if (this._open === k) { this._open = ''; return this.render(); }
        this._open = k; this.render();
        if (!this._cfg[k]) { this._cfg[k] = await this._get('/nodes/backend/config?instance_id=' + encodeURIComponent(k)) || { models: [] }; this.render(); }
        return;
      }
      if (a === 'save' || a === 'save-switch') {
        const args = this._setupArgs(k);
        if (!args.model) return this._say('Pick the model first.');
        const r = await this._post('/nodes/backend/config/set', args);
        if (!r || !r.ok) return this._say('✗ ' + ((r && r.error) || 'not saved'));
        this._cfg[k] = null; this._say('✓ setup saved for ' + k);
        if (a === 'save-switch') {
          const n = ((this._o || {}).nodes || []).find((x) => x.instance === k) || {};
          if (n.backend !== 'vllm') { this._open = ''; return this.switchNodes([k], 'vllm'); }
          this._say('✓ setup saved - switch ' + k + ' to Ollama and back to vLLM to load the new model');
        }
        return this.load();
      }
    }
    async _change(e) {
      const t = e.target;
      if (t.dataset.sel) { t.checked ? this._sel.add(t.dataset.sel) : this._sel.delete(t.dataset.sel); return; }
      if (t.dataset.think) {
        const k = t.dataset.think, n = ((this._o || {}).nodes || []).find((x) => x.instance === k) || {};
        const s = n.setup || {};
        const r = await this._post('/nodes/backend/config/set', Object.assign({}, s, { instance_id: k, model: s.model || ((n.vllm || {}).models || [])[0] || '', think_default: t.value }));
        this._say(r && r.ok ? '✓ thinking on ' + k + ': ' + (t.value || 'model default') : '✗ ' + ((r && r.error) || 'failed'));
        return this.load();
      }
      if (t.dataset.model) { const k = t.dataset.model; const c = this._cfg[k] || {}; const m = (c.models || []).find((x) => x.path === t.value);
        if (m) { const set = (id, v) => { const el = this._r.getElementById(id + k); if (el) el.value = v; };
          set('mo-', ''); set('al-', m.alias || ''); set('fl-', m.extra || ''); } }
    }
  }
  customElements.define('vera-backend-switch', VeraBackendSwitch);
})();
