/* vera-estate.js — the one estate list, the one way to pick a machine, and the
 * one way to run something that changes the estate.
 *
 *   <script src="/ui/vera-estate.js"></script>
 *
 *   veraEstate.machines()            -> every machine (estate.machines), cached 30 s
 *   veraEstate.logins()              -> the machines that have an SSH login
 *   veraEstate.pick({filter, title}) -> a picker over the same list; resolves to a machine
 *   veraEstate.fillSelect(sel, {...})-> a <select> of machines, values = ssh_host_id
 *   veraEstate.ref(machine)          -> its entity reference (guest:145 / host:<id>)
 *   veraEstate.plan(path, args, title, opts)  -> dry run, show the plan, confirm, run
 *   veraEstate.confirmRun(path, args, text)   -> confirm, run (for actions without a dry run)
 *
 * Seven panes each grew their own list of hosts — the SSH store, the enrol
 * store, Proxmox, Docker, the mesh, the directory, nodes.list. estate.machines
 * is the join of all of them; this is how a panel reaches it without learning
 * any other panel's data model. Every write goes through the same flow the
 * Storage controls set: the exact commands and warnings first, then the run.
 */
(function(){
  if (window.veraEstate) return;
  var BASE = (window.__VERA_BASE__ || (window.veraUI && window.veraUI.BASE) || '').replace(/\/$/, '');
  var CACHE = {at: 0, rows: null, inflight: null};
  var TTL = 30000;

  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function post(path, body){
    return fetch(BASE + path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})})
      .then(function(r){ return r.json().catch(function(){ return {error:'HTTP ' + r.status}; }); });
  }
  function ref(m){
    if (!m) return '';
    if (m.kind === 'guest' && m.vmid != null) return 'guest:' + m.vmid;
    return m.ssh_host_id ? ('host:' + m.ssh_host_id) : (m.id ? 'host:' + m.id : '');
  }
  function machines(refresh){
    if (!refresh && CACHE.rows && Date.now() - CACHE.at < TTL) return Promise.resolve(CACHE.rows);
    if (CACHE.inflight) return CACHE.inflight;
    CACHE.inflight = fetch(BASE + '/estate/machines', {cache:'no-store'}).then(function(r){ return r.json(); })
      .then(function(j){ CACHE.rows = (j && j.machines) || []; CACHE.at = Date.now(); CACHE.inflight = null; return CACHE.rows; })
      .catch(function(e){ CACHE.inflight = null; throw e; });
    return CACHE.inflight;
  }
  function logins(refresh){ return machines(refresh).then(function(rows){ return rows.filter(function(m){ return m.ssh_host_id; }); }); }
  var KIND = {'proxmox-node':'node', guest:'guest', host:'host', 'docker-host':'Docker'};
  function label(m){
    var where = m.kind === 'guest' && m.vmid != null ? ((m.type === 'qemu' ? 'VM ' : 'CT ') + m.vmid) : (KIND[m.kind] || m.kind || '');
    return (m.label || m.addr || '?') + (where ? ' · ' + where : '') + (m.addr ? ' · ' + m.addr : '') + (m.status ? ' · ' + m.status : '');
  }
  // A <select> whose options are machines with a login; value = ssh_host_id.
  function fillSelect(sel, opts){
    opts = opts || {};
    return logins(opts.refresh).then(function(rows){
      var f = opts.filter ? rows.filter(opts.filter) : rows;
      var h = opts.blank != null ? '<option value="">' + esc(opts.blank) + '</option>' : '';
      h += f.map(function(m){ return '<option value="' + esc(m.ssh_host_id) + '"' + (m.ssh_host_id === opts.selected ? ' selected' : '') + '>' + esc(label(m)) + '</option>'; }).join('');
      if (opts.selected && !f.some(function(m){ return m.ssh_host_id === opts.selected; })) h = '<option value="' + esc(opts.selected) + '" selected>' + esc(opts.selected) + ' (not in the estate list)</option>' + h;
      sel.innerHTML = h || '<option value="">no machines with a login</option>';
      return f;
    });
  }

  // ── the picker ─────────────────────────────────────────────────────────────
  var CSS = '#vera-pick{position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center}'
    + '#vera-pick .box{width:min(560px,94vw);max-height:80vh;display:flex;flex-direction:column;background:var(--bg1,var(--s1,#14181d));color:var(--text,var(--t1,#d8dde3));border:1px solid var(--border,var(--bd,#232a33));border-radius:8px;font:12px/1.5 system-ui,sans-serif;box-shadow:0 12px 40px rgba(0,0,0,.4)}'
    + '#vera-pick .hd{padding:10px 12px;border-bottom:1px solid var(--border,#232a33);display:flex;gap:8px;align-items:center}'
    + '#vera-pick input{flex:1;background:var(--bg2,#1a1f26);border:1px solid var(--border,#232a33);color:inherit;border-radius:5px;padding:5px 8px;font:inherit}'
    + '#vera-pick .ls{overflow:auto;padding:6px}'
    + '#vera-pick .it{display:flex;gap:10px;align-items:baseline;padding:6px 8px;border-radius:5px;cursor:pointer}'
    + '#vera-pick .it:hover,#vera-pick .it.on{background:var(--bg2,#1a1f26)}'
    + '#vera-pick .it b{min-width:140px}#vera-pick .it .d{color:var(--dim,#5f6975);font-size:11px}'
    + '#vera-pick .x{background:none;border:1px solid var(--border,#232a33);color:inherit;border-radius:4px;padding:2px 8px;cursor:pointer;font:inherit}';
  function pick(opts){
    opts = opts || {};
    return machines().then(function(rows){
      var list = opts.filter ? rows.filter(opts.filter) : rows;
      return new Promise(function(resolve){
        if (!document.getElementById('vera-pick-css')) { var st = document.createElement('style'); st.id = 'vera-pick-css'; st.textContent = CSS; document.head.appendChild(st); }
        var wrap = document.createElement('div'); wrap.id = 'vera-pick'; wrap.setAttribute('role', 'dialog');
        wrap.innerHTML = '<div class="box"><div class="hd"><b>' + esc(opts.title || 'Pick a machine') + '</b><input id="vera-pick-q" placeholder="filter by name, address, kind…"><button class="x" type="button">✕</button></div><div class="ls" id="vera-pick-ls"></div></div>';
        document.body.appendChild(wrap);
        var q = wrap.querySelector('#vera-pick-q'), ls = wrap.querySelector('#vera-pick-ls');
        function close(v){ wrap.remove(); document.removeEventListener('keydown', key); resolve(v || null); }
        function key(e){ if (e.key === 'Escape') close(null); }
        function draw(){
          var t = (q.value || '').toLowerCase();
          var f = list.filter(function(m){ return !t || label(m).toLowerCase().indexOf(t) >= 0; });
          ls.innerHTML = f.length ? f.map(function(m, i){ return '<div class="it" data-i="' + list.indexOf(m) + '" tabindex="0"><b>' + esc(m.label || m.addr) + '</b><span class="d">' + esc(label(m).replace((m.label || m.addr) + ' · ', '')) + '</span></div>'; }).join('') : '<div class="it d">no machine matches</div>';
        }
        ls.addEventListener('click', function(e){ var it = e.target.closest('.it[data-i]'); if (it) close(list[+it.dataset.i]); });
        ls.addEventListener('keydown', function(e){ var it = e.target.closest('.it[data-i]'); if (it && e.key === 'Enter') close(list[+it.dataset.i]); });
        wrap.querySelector('.x').addEventListener('click', function(){ close(null); });
        wrap.addEventListener('click', function(e){ if (e.target === wrap) close(null); });
        document.addEventListener('keydown', key);
        q.addEventListener('input', draw); draw(); q.focus();
      });
    });
  }

  // ── the one flow for changing the estate ───────────────────────────────────
  // plan(): for capabilities that answer a dry run with {plan:{commands,warnings,before,after}}.
  function plan(path, args, title, opts){
    opts = opts || {};
    var p = path.charAt(0) === '/' ? path : '/' + path.replace(/\./g, '/');
    var base = Object.assign({}, args || {});
    return post(p, base).then(function(dry){
      var pl = dry.plan || {};
      if (dry.error && !(pl.commands && pl.commands.length)) { alert(dry.error); return {ok:false, error: dry.error}; }
      var text = (title || path) + '\n\n' + (opts.where ? 'Will run on ' + opts.where + ':' : 'Will run:') + '\n  ' + (pl.commands || []).join('\n  ')
        + ((pl.before || pl.after) ? '\n\nLayout: ' + (pl.before || '') + ' → ' + (pl.after || '') : '')
        + ((pl.warnings || []).length ? '\n\nMind:\n  • ' + pl.warnings.join('\n  • ') : '') + '\n\nRun it?';
      if (!confirm(text)) return {ok:false, cancelled:true};
      var conf = Object.assign({}, base, {confirm: true});
      if (opts.typed) { var typed = prompt('Type ' + opts.typed.label + ' to confirm: ' + opts.typed.value); if (typed !== opts.typed.value) { alert('Not run: the name did not match.'); return {ok:false, cancelled:true}; } conf[opts.typed.field] = typed; }
      return post(p, conf).then(function(r){
        if (r.error || r.ok === false) alert((r.error || 'It did not succeed.') + (r.stderr ? '\n' + String(r.stderr).slice(-300) : ''));
        return r;
      });
    });
  }
  // confirmRun(): for actions without a dry run - one honest confirmation, then the call.
  function confirmRun(path, args, text){
    var p = path.charAt(0) === '/' ? path : '/' + path.replace(/\./g, '/');
    if (!confirm(text)) return Promise.resolve({ok:false, cancelled:true});
    return post(p, args || {}).then(function(r){ if (r.error) alert(r.error); return r; });
  }
  function chip(r, text){ return r ? '<span data-entity="' + esc(r) + '" style="cursor:pointer;border-bottom:1px dotted currentColor" title="Everything the estate knows about this">' + esc(text) + '</span>' : esc(text); }

  window.veraEstate = {machines: machines, logins: logins, pick: pick, fillSelect: fillSelect, ref: ref,
                       label: label, plan: plan, confirmRun: confirmRun, chip: chip, BASE: BASE};
})();
