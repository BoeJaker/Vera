/**
 * <vera-node-workers> - the node workers of the estate, and one-click control.
 *
 * Every Ollama node can run a Vera worker that takes tasks off the task
 * streams. This element shows each node's worker (online, idle / running,
 * the commit it runs and whether that is the commit the host runs), lets the
 * user choose which TASK CLASSES the node takes (general, NLP, CPU compute,
 * media) - applied live, the worker re-reads its roles within 30 s - and
 * provisions a worker on any Ollama node that has none, in one click.
 *
 * Data:    GET  /nodes/workers                (nodes.workers.list)
 * Actions: POST /nodes/workers/roles          (nodes.workers.roles.set)
 *          POST /nodes/workers/provision      (nodes.workers.provision)
 *          POST /nodes/workers/sync           (nodes.workers.sync)
 *          POST /nodes/workers/dispatch       (nodes.workers.dispatch - the rollout stage)
 *
 * A class with no vetted capabilities says so ("0 caps - nothing vetted yet")
 * rather than implying the node will get such work.
 *
 * Attributes: api-base (default ''), refresh (seconds, default 15)
 */
(function () {
  if (customElements.get('vera-node-workers')) return;

  const CSS = `
  :host{display:block;font:11px/1.45 var(--sans,-apple-system,system-ui,sans-serif);color:var(--fg,var(--text,#d8dde3))}
  .wrap{display:flex;flex-direction:column;gap:10px;padding:10px}
  .hdr{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .hdr .t{font:600 10px/1 var(--mono,ui-monospace,monospace);letter-spacing:.12em;text-transform:uppercase;color:var(--acc,#4a9eff)}
  .muted{color:var(--dim,#5f6975)}
  .mono{font-family:var(--mono,ui-monospace,monospace)}
  .sp{flex:1}
  button{font:inherit;font-size:10.5px;background:var(--bg2,#1a1f26);color:inherit;border:1px solid var(--border2,#2e3742);
         border-radius:4px;padding:4px 9px;cursor:pointer}
  button:hover{border-color:var(--acc,#4a9eff)}
  button[disabled]{opacity:.5;cursor:default}
  button.pri{background:var(--acc,#4a9eff);border-color:var(--acc,#4a9eff);color:var(--on-acc,#04121f);font-weight:600}
  .legend{display:flex;gap:6px;flex-wrap:wrap}
  .chip{border:1px solid var(--border2,#2e3742);border-radius:10px;padding:2px 8px;font-size:10px;white-space:nowrap}
  .chip.none{opacity:.55;border-style:dashed}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
  .card{background:var(--bg1,#14181d);border:1px solid var(--border,#232a33);border-radius:7px;padding:10px 12px;
        display:flex;flex-direction:column;gap:8px}
  .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .name{font-weight:600;font-size:12px}
  .badge{font:600 9px/1 var(--mono,ui-monospace,monospace);padding:3px 6px;border-radius:3px;border:1px solid currentColor}
  .gpu{color:var(--warn,#f5b341)} .cpu{color:var(--acc,#4a9eff)}
  .ok{color:var(--acc2,var(--ok,#28c28a))} .bad{color:var(--err,#ef5b5b)} .warn{color:var(--warn,#f5b341)}
  .cls{display:flex;gap:6px;flex-wrap:wrap}
  .cls label{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--border2,#2e3742);border-radius:4px;
             padding:3px 7px;cursor:pointer;font-size:10.5px}
  .cls label.on{border-color:var(--acc,#4a9eff)}
  .cls label.empty{opacity:.6}
  .err{font-size:10px;color:var(--err,#ef5b5b);word-break:break-word}
  .note{font-size:10px}
  .sec{font:600 9.5px/1 var(--mono,ui-monospace,monospace);letter-spacing:.1em;text-transform:uppercase;color:var(--dim,#5f6975);margin-top:4px}
  `;

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const short = (c) => (c || '').slice(0, 8) || '-';

  class VeraNodeWorkers extends HTMLElement {
    constructor() {
      super();
      this._root = this.attachShadow({ mode: 'open' });
      this._data = null;
      this._busy = {};
      this._timer = null;
      this._msg = '';
    }
    connectedCallback() {
      this._render();
      this.refresh();
      const s = Math.max(5, parseInt(this.getAttribute('refresh') || '15', 10) || 15);
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
    async refresh() {
      try { this._data = await this._call('GET', '/nodes/workers'); this._msg = ''; }
      catch (e) { this._msg = 'could not read node workers: ' + e.message; }
      this._render();
    }
    async _act(key, fn, okMsg) {
      if (this._busy[key]) return;
      this._busy[key] = true; this._render();
      try { const r = await fn(); this._msg = (r && r.ok === false) ? ('failed: ' + (r.error || 'unknown')) : (okMsg || ''); }
      catch (e) { this._msg = 'failed: ' + e.message; }
      delete this._busy[key];
      await this.refresh();
    }
    _toggle(hostId, cls, on) {
      const node = (this._data.nodes || []).find((n) => n.host_id === hostId); if (!node) return;
      const set = new Set(node.classes); on ? set.add(cls) : set.delete(cls);
      this._act('roles:' + hostId, () => this._call('POST', '/nodes/workers/roles', { host_id: hostId, classes: Array.from(set) }),
                'roles saved - the worker picks them up within 30 s');
    }

    _render() {
      const d = this._data || {};
      const classes = d.classes || [];
      const plan = d.plan || {};
      const planTxt = plan.blocked ? ('paused: ' + plan.blocked)
        : (plan.run && plan.run.length) ? ('next refresh: ' + plan.run.length + ' node(s)')
        : ((plan.up_to_date || []).length + ' up to date');
      let h = '<style>' + CSS + '</style><div class="wrap">';
      h += '<div class="hdr"><span class="t">Node workers</span>'
        + '<span class="muted">host runs <span class="mono">' + esc(short(d.host_commit)) + '</span></span>'
        + '<span class="muted">· auto-sync <b class="' + (d.sync_enabled ? 'ok' : 'warn') + '">' + (d.sync_enabled ? 'on' : 'off') + '</b> · ' + esc(planTxt) + '</span>'
        + '<span class="sp"></span>'
        + '<button data-a="sync-toggle">' + (d.sync_enabled ? 'Pause auto-sync' : 'Resume auto-sync') + '</button>'
        + '<button class="pri" data-a="sync-now"' + (this._busy.sync || d.sandbox ? ' disabled' : '') + '>' + (this._busy.sync ? 'Syncing…' : 'Sync now') + '</button></div>';
      const dp = d.dispatch || {};
      if (dp.stages) {
        const sent = Object.entries(dp.offloaded || {}).map(([k, v]) => k + ' ' + v).join(', ');
        h += '<div class="row"><span class="sec" style="margin:0">Work sent to nodes</span>'
          + '<select data-a="stage"' + (d.sandbox ? ' disabled' : '') + '>' + dp.stages.map((s) => '<option value="' + s.id + '"' + (s.id === dp.stage ? ' selected' : '') + ' title="' + esc(s.desc) + '">' + s.id + ' · ' + esc(s.label) + '</option>').join('') + '</select>'
          + '<span class="muted">' + esc((dp.stages[dp.stage] || {}).desc || '') + ' - only to an idle worker, else it runs on the host'
          + (sent ? ' · sent so far: ' + esc(sent) : '') + '</span></div>';
      }
      if (d.sandbox) h += '<div class="note warn">' + esc(d.sandbox_note) + '</div>';
      if (this._msg) h += '<div class="note ' + (this._msg.startsWith('failed') || this._msg.startsWith('could not') ? 'bad' : 'ok') + '">' + esc(this._msg) + '</div>';

      h += '<div class="legend">' + classes.map((c) => '<span class="chip' + (c.caps ? '' : ' none') + '" title="' + esc(c.desc) + '">'
        + esc(c.label) + ' · ' + (c.caps ? c.caps + ' caps' : '0 caps - nothing vetted yet') + '</span>').join('') + '</div>';

      h += '<div class="grid">';
      for (const n of (d.nodes || [])) {
        const w = n.worker || {};
        const st = !w.online ? '<b class="bad">offline</b>' : (String(w.status || '').startsWith('running') ? '<b class="warn">' + esc(w.status) + '</b>' : '<b class="ok">' + esc(w.status || 'idle') + '</b>');
        const sync = n.in_sync ? '<span class="ok">in sync</span>' : '<span class="warn">behind the host</span>';
        h += '<div class="card"><div class="row"><span class="name">' + esc(n.nodename || n.host) + '</span>'
          + '<span class="muted mono">' + esc(n.host) + '</span>'
          + '<span class="badge ' + (n.has_gpu ? 'gpu">GPU' : 'cpu">CPU') + '</span><span class="sp"></span>' + st + '</div>'
          + '<div class="row muted">worker <span class="mono">' + esc(w.id || '-') + '</span> · commit <span class="mono">' + esc(short(w.commit)) + '</span> · ' + sync + '</div>'
          + '<div class="sec">Takes' + (n.classes_default ? ' (default)' : '') + '</div><div class="cls">'
          + classes.map((c) => {
              const on = (n.classes || []).indexOf(c.key) >= 0;
              return '<label class="' + (on ? 'on' : '') + (c.caps ? '' : ' empty') + '" title="' + esc(c.desc) + '">'
                + '<input type="checkbox" data-a="cls" data-h="' + esc(n.host_id) + '" data-c="' + esc(c.key) + '"' + (on ? ' checked' : '')
                + (this._busy['roles:' + n.host_id] ? ' disabled' : '') + '>' + esc(c.label) + '</label>';
            }).join('') + '</div>'
          + (n.failures ? '<div class="err">' + n.failures + ' failed attempt(s): ' + esc(n.last_error) + '</div>' : '')
          + '<div class="row"><button data-a="prov" data-h="' + esc(n.host_id) + '"' + (this._busy['prov:' + n.host_id] || d.sandbox ? ' disabled' : '') + '>'
          + (this._busy['prov:' + n.host_id] ? 'Refreshing… (a few minutes)' : 'Refresh code') + '</button>'
          + '<button data-a="reset" data-h="' + esc(n.host_id) + '"' + (n.classes_default ? ' disabled' : '') + '>Reset roles</button></div></div>';
      }
      h += '</div>';

      const cands = d.candidates || [];
      if (cands.length) {
        h += '<div class="sec">Ollama nodes without a worker</div><div class="grid">';
        for (const c of cands) {
          h += '<div class="card"><div class="row"><span class="name">' + esc(c.label) + '</span><span class="muted mono">' + esc(c.host) + '</span>'
            + '<span class="badge ' + (c.has_gpu ? 'gpu">GPU' : 'cpu">CPU') + '</span></div>'
            + (c.ssh ? '<div class="row"><button class="pri" data-a="prov" data-h="' + esc(c.host_id) + '"' + (this._busy['prov:' + c.host_id] || d.sandbox ? ' disabled' : '') + '>'
                  + (this._busy['prov:' + c.host_id] ? 'Provisioning… (a few minutes)' : 'Provision worker') + '</button>'
                  + '<span class="muted note">starts with ' + (c.has_gpu ? 'no classes (GPU node)' : 'General + NLP') + '</span></div>'
                : '<div class="note warn">No SSH credential stored for this address - add one under Connections first.</div>')
            + '</div>';
        }
        h += '</div>';
      }
      if (!this._data && !this._msg) h += '<div class="muted">Loading…</div>';
      h += '</div>';
      this._root.innerHTML = h;
      this._root.querySelectorAll('[data-a]').forEach((el) => {
        const a = el.dataset.a, hid = el.dataset.h;
        if (a === 'cls') el.addEventListener('change', () => this._toggle(hid, el.dataset.c, el.checked));
        else if (a === 'stage') el.addEventListener('change', () => this._act('stage', () => this._call('POST', '/nodes/workers/dispatch', { stage: parseInt(el.value, 10) }), 'stage set'));
        else el.addEventListener('click', () => {
          if (a === 'sync-now') this._act('sync', () => this._call('POST', '/nodes/workers/sync', { limit: 3 }), 'sync ran');
          else if (a === 'sync-toggle') this._act('synccfg', () => this._call('POST', '/nodes/workers/sync', { dry_run: true, enabled: !d.sync_enabled }));
          else if (a === 'prov') this._act('prov:' + hid, () => this._call('POST', '/nodes/workers/provision', { host_id: hid }), 'worker installed / refreshed');
          else if (a === 'reset') this._act('roles:' + hid, () => this._call('POST', '/nodes/workers/roles', { host_id: hid, reset: true }), 'roles back to the default');
        });
      });
    }
  }
  customElements.define('vera-node-workers', VeraNodeWorkers);
})();
