/* vera-select.js — the one way to pick from a list: search, filter, tick,
 * chips; free text only where the field says so.
 *
 *   <script src="/ui/vera-select.js"></script>
 *
 * Progressive: the panel keeps its <input> (or <textarea>) and its handlers.
 * Add one attribute and the control appears in front of it, writing the
 * chosen values back as the field always held them (comma-separated by
 * default), firing `input` and `change` like a typed edit would.
 *
 *   <input id="tk-caps" data-select="caps">                       many capabilities
 *   <input id="set-model" data-select="models" data-select-single>  one model
 *   <input id="f-tags" data-select="tags" data-select-new
 *          data-select-url="/exec/ssh/hosts" data-select-path="hosts[].tags[]">
 *                                          the tags that exist, plus new ones
 *   <input data-select="static" data-select-options="a|b|c">
 *
 * Attributes: data-select=<source> · data-select-single · data-select-new
 * (typed values allowed) · data-select-sep="," (or " ", "\n") · data-select-
 * prefix="dream.stage." (only values under it) · data-select-url + data-
 * select-path (a generic union: "hosts[].tags[]", "features[].id") · data-
 * select-options ("a|b" or a JSON array) · data-select-placeholder.
 *
 * Sources: caps (/mcp/tools, grouped by prefix) · models (/agents/models, hint
 * = which nodes hold it) · logins (machines with an SSH login, value = login
 * id) · machines (every machine, value = its entity ref) · docker-hosts ·
 * instances (inference nodes) · agents (/agents/list) · skills
 * (/fabric/skills) · features (/foundry/features) · tags/static/url.
 *
 * veraSelect.enhance(el, opts) does the same from code; veraSelect.scan(root)
 * enhances every [data-select] under root (run on load and on DOM changes, so
 * rows a panel renders later get it too); veraSelect.source(name, loader)
 * registers a source. */
(function(){
  if (window.veraSelect) return;
  var BASE = (window.__VERA_BASE__ || (window.veraUI && window.veraUI.BASE) || '').replace(/\/$/, '');
  var CSS = [
    '.vs-host{position:relative;display:flex;flex-direction:column;min-width:0}',
    '.vs-box{display:flex;flex-wrap:wrap;gap:4px;align-items:center;min-height:26px;padding:3px 6px;border:1px solid var(--border2,var(--border,#333));border-radius:var(--ui-radius,5px);background:var(--bg1,var(--bg,#111));cursor:text;font:inherit;color:var(--text,var(--fg,#ddd))}',
    '.vs-box:focus-within{border-color:var(--acc,#4a9eff)}',
    '.vs-chip{display:inline-flex;align-items:center;gap:4px;padding:1px 6px;border-radius:10px;background:var(--bg3,#222);border:1px solid var(--border,#333);font-size:11px;max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.vs-chip.new{border-style:dashed}',
    '.vs-chip .x{cursor:pointer;color:var(--dim2,#888);font-size:12px;line-height:1}.vs-chip .x:hover{color:var(--err,#e55)}',
    '.vs-q{flex:1;min-width:70px;border:none;outline:none;background:transparent;color:inherit;font:inherit;font-size:11.5px;padding:2px 0}',
    '.vs-caret{color:var(--dim,#666);font-size:9px;padding:0 2px;cursor:pointer}',
    '.vs-pop{position:absolute;left:0;right:0;top:100%;z-index:60;margin-top:3px;max-height:280px;overflow:auto;background:var(--bg2,#181818);border:1px solid var(--border2,#333);border-radius:var(--ui-radius,5px);box-shadow:0 8px 24px rgba(0,0,0,.35);display:none}',
    '.vs-host.open .vs-pop{display:block}',
    '.vs-grp{padding:5px 8px 2px;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--dim,#666);position:sticky;top:0;background:var(--bg2,#181818)}',
    '.vs-it{display:flex;align-items:center;gap:7px;padding:4px 8px;font-size:11.5px;cursor:pointer;min-width:0}',
    '.vs-it:hover,.vs-it.hi{background:var(--bg3,#222)}',
    '.vs-it .cb{width:12px;height:12px;border:1px solid var(--border2,#444);border-radius:3px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;font-size:9px;color:var(--on-acc,#000)}',
    '.vs-it.on .cb{background:var(--acc,#4a9eff);border-color:var(--acc,#4a9eff)}',
    '.vs-it .l{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.vs-it .h{color:var(--dim2,#888);font-size:10px;white-space:nowrap;max-width:45%;overflow:hidden;text-overflow:ellipsis}',
    '.vs-it.add .l{color:var(--acc,#4a9eff)}',
    '.vs-none{padding:8px;color:var(--dim,#666);font-size:11px}',
    '.vs-foot{display:flex;gap:8px;padding:4px 8px;border-top:1px solid var(--border,#333);font-size:10px;color:var(--dim2,#888)}',
    '.vs-foot span{cursor:pointer}.vs-foot span:hover{color:var(--text,var(--fg,#ddd))}'
  ].join('\n');
  function css(){ if (document.getElementById('vera-select-css')) return; var st = document.createElement('style'); st.id = 'vera-select-css'; st.textContent = CSS; document.head.appendChild(st); }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function get(path){ return fetch(BASE + path, {cache: 'no-store'}).then(function(r){ return r.json(); }); }
  function post(path, body){ return fetch(BASE + path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})}).then(function(r){ return r.json(); }); }
  function uniq(a){ var s = {}; return a.filter(function(x){ if (x == null || x === '' || s[x]) return false; s[x] = 1; return true; }); }

  // ── sources: name -> Promise<[{value,label,hint,group}]>, cached 60 s ─────
  var SOURCES = {}, CACHE = {};
  function source(name, loader){ SOURCES[name] = loader; }
  function items(name, el){
    var key = name + '|' + (el ? (el.getAttribute('data-select-url') || '') + '|' + (el.getAttribute('data-select-path') || '') + '|' + (el.getAttribute('data-select-options') || '') : '');
    var c = CACHE[key];
    if (c && Date.now() - c.at < 60000) return c.p;
    var loader = SOURCES[name];
    var p = (loader ? Promise.resolve().then(function(){ return loader(el); }) : Promise.resolve([]))
      .then(function(list){ return (list || []).map(function(x){ return typeof x === 'string' ? {value: x, label: x} : x; }).filter(function(x){ return x && x.value != null && x.value !== ''; }); })
      .catch(function(){ return []; });
    CACHE[key] = {at: Date.now(), p: p};
    return p;
  }
  // "hosts[].tags[]" / "features[].id" over a JSON body -> flat list of strings
  function pluck(obj, path){
    var out = [];
    function walk(o, parts){
      if (o == null) return;
      if (!parts.length) { if (Array.isArray(o)) o.forEach(function(x){ walk(x, []); }); else out.push(o); return; }
      var p = parts[0], rest = parts.slice(1);
      if (p === '[]') { (Array.isArray(o) ? o : []).forEach(function(x){ walk(x, rest); }); return; }
      if (p === '*') { Object.keys(o).forEach(function(k){ walk(o[k], rest); }); return; }
      walk(o[p], rest);
    }
    var parts = [];
    String(path || '').split('.').forEach(function(seg){
      while (/\[\]$/.test(seg)) { seg = seg.slice(0, -2); parts.push(seg); parts.push('[]'); seg = ''; }
      if (seg) parts.push(seg);
    });
    walk(obj, parts.filter(Boolean));
    return out;
  }
  source('caps', function(){ return get('/mcp/tools').then(function(t){ return (Array.isArray(t) ? t : (t.tools || [])).map(function(x){ var n = x.name || x; return {value: n, label: n, hint: (x.description || '').slice(0, 70), group: String(n).split('.')[0]}; }); }); });
  source('models', function(){ return get('/agents/models').then(function(r){ var by = {}; var inst = r.instances || {}; Object.keys(inst).forEach(function(id){ ((inst[id] || {}).models || []).forEach(function(m){ (by[m] = by[m] || []).push(id); }); }); return Object.keys(by).sort().map(function(m){ return {value: m, label: m, hint: by[m].join(', '), group: /embed/.test(m) ? 'embedding' : 'chat'}; }); }); });
  source('logins', function(){ return window.veraEstate ? veraEstate.logins().then(function(rows){ return rows.map(function(m){ return {value: m.ssh_host_id, label: m.label || m.addr, hint: veraEstate.label(m).replace(/^[^·]*· ?/, ''), group: m.kind}; }); }) : []; });
  source('machines', function(){ return window.veraEstate ? veraEstate.machines().then(function(rows){ return rows.map(function(m){ return {value: veraEstate.ref(m), label: m.label || m.addr, hint: m.addr || '', group: m.kind}; }); }) : []; });
  source('docker-hosts', function(){ return window.veraEstate ? veraEstate.machines().then(function(rows){ return rows.filter(function(m){ return m.docker_host_id; }).map(function(m){ return {value: m.docker_host_id, label: m.label || m.docker_host_id, hint: m.addr || '', group: 'Docker host'}; }); }) : []; });
  source('instances', function(){ return get('/agents/models').then(function(r){ var inst = r.instances || {}; return Object.keys(inst).map(function(id){ var i = inst[id] || {}; return {value: id, label: i.label || id, hint: (i.gpu ? 'gpu · ' : '') + ((i.models || []).length + ' models'), group: 'inference node'}; }); }); });
  source('agents', function(){ return get('/agents/list').then(function(r){ return (r.agents || []).map(function(a){ return {value: a.name || a.id, label: a.label || a.name, hint: (a.description || '').slice(0, 60), group: a.role || ''}; }); }); });
  source('skills', function(){ return get('/fabric/skills').then(function(r){ return (r.skills || []).map(function(s){ return {value: s.id || s.name, label: s.name || s.id, hint: (s.description || '').slice(0, 60)}; }); }); });
  source('features', function(){ return get('/foundry/features').then(function(r){ return (r.features || []).map(function(f){ return {value: f.id, label: f.label || f.id, hint: (f.desc || f.status || '').slice(0, 60), group: f.status || ''}; }); }); });
  source('static', function(el){ var raw = el.getAttribute('data-select-options') || ''; var list; try { list = JSON.parse(raw); } catch (e) { list = raw.split('|'); } return list.map(function(x){ return typeof x === 'string' ? {value: x.trim(), label: x.trim()} : x; }); });
  source('url', function(el){ var url = el.getAttribute('data-select-url'), path = el.getAttribute('data-select-path') || ''; if (!url) return []; var p = /^POST /.test(url) ? post(url.slice(5)) : get(url); return p.then(function(r){ return uniq(pluck(r, path).map(String)).sort().map(function(v){ return {value: v, label: v}; }); }); });
  source('tags', function(el){ return SOURCES.url(el); });

  // ── the control ──────────────────────────────────────────────────────────
  function enhance(el, opts){
    if (!el || el._veraSelect) return el && el._veraSelect;
    css();
    opts = opts || {};
    var A = function(n){ return el.getAttribute('data-select-' + n); };
    var name = opts.source || el.getAttribute('data-select') || 'static';
    var multiple = opts.multiple != null ? opts.multiple : (el.tagName === 'SELECT' ? el.multiple : !el.hasAttribute('data-select-single'));
    var allowNew = opts.allowNew != null ? opts.allowNew : el.hasAttribute('data-select-new');
    var sep = opts.sep || A('sep') || ',';
    if (sep === '\\n') sep = '\n';
    var prefix = opts.prefix || A('prefix') || '';
    var placeholder = opts.placeholder || A('placeholder') || el.getAttribute('placeholder') || (multiple ? 'pick or search…' : 'pick…');
    var selected = [], all = [], loaded = false, hi = -1;

    var host = document.createElement('div'); host.className = 'vs-host';
    var box = document.createElement('div'); box.className = 'vs-box';
    var q = document.createElement('input'); q.className = 'vs-q'; q.placeholder = placeholder; q.setAttribute('autocomplete', 'off');
    var caret = document.createElement('span'); caret.className = 'vs-caret'; caret.textContent = '▾';
    var pop = document.createElement('div'); pop.className = 'vs-pop';
    box.appendChild(q); box.appendChild(caret); host.appendChild(box); host.appendChild(pop);
    // the host takes the field's place in its row: same flex, width and margins
    ['flex', 'flexGrow', 'flexBasis', 'width', 'minWidth', 'maxWidth', 'marginLeft', 'marginRight', 'marginTop', 'marginBottom'].forEach(function(p){ if (el.style[p]) host.style[p] = el.style[p]; });
    if (!host.style.width && !host.style.flex && el.tagName !== 'SELECT') host.style.width = el.offsetWidth ? Math.max(el.offsetWidth, 160) + 'px' : '';
    el.parentNode.insertBefore(host, el); host.appendChild(el);
    el.style.display = 'none';
    if (el.id) host.setAttribute('data-for', el.id);

    // read what the field holds; write what was picked, the way the field held it
    function read(){
      if (el.tagName === 'SELECT') return Array.prototype.filter.call(el.options, function(o){ return o.selected; }).map(function(o){ return o.value; });
      var v = el.value || '';
      return uniq(v.split(sep === ' ' ? /[\s,]+/ : sep === '\n' ? /\r?\n/ : new RegExp('\\s*' + sep.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*')).map(function(s){ return s.trim(); }));
    }
    function write(fire){
      if (el.tagName === 'SELECT') {
        Array.prototype.forEach.call(el.options, function(o){ o.selected = selected.indexOf(o.value) >= 0; });
        selected.forEach(function(v){ if (!Array.prototype.some.call(el.options, function(o){ return o.value === v; })) { var o = document.createElement('option'); o.value = v; o.textContent = v; o.selected = true; el.appendChild(o); } });
      } else {
        el.value = selected.join(sep === ',' ? ', ' : sep);
      }
      if (fire !== false) { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }
      if (opts.onChange) opts.onChange(selected.slice());
    }
    function labelOf(v){ var it = all.filter(function(x){ return x.value === v; })[0]; return it ? it.label : v; }
    function chips(){
      Array.prototype.slice.call(box.querySelectorAll('.vs-chip')).forEach(function(c){ c.remove(); });
      selected.forEach(function(v){
        var known = all.some(function(x){ return x.value === v; });
        var c = document.createElement('span'); c.className = 'vs-chip' + (loaded && !known ? ' new' : ''); c.title = v;
        c.innerHTML = esc(labelOf(v)) + ' <span class="x" title="Remove">×</span>';
        c.querySelector('.x').addEventListener('click', function(e){ e.stopPropagation(); selected = selected.filter(function(x){ return x !== v; }); write(); chips(); list(); });
        box.insertBefore(c, q);
      });
      q.placeholder = selected.length ? '' : placeholder;
    }
    function visible(){
      var f = (q.value || '').trim().toLowerCase();
      return all.filter(function(x){ return (!prefix || String(x.value).indexOf(prefix) === 0) && (!f || String(x.value).toLowerCase().indexOf(f) >= 0 || String(x.label).toLowerCase().indexOf(f) >= 0 || String(x.hint || '').toLowerCase().indexOf(f) >= 0); });
    }
    function list(){
      if (!loaded) { pop.innerHTML = '<div class="vs-none">loading…</div>'; return; }
      var vis = visible(), f = (q.value || '').trim();
      var h = '', lastGroup = null, i = 0;
      vis.slice(0, 400).forEach(function(x){
        if (x.group != null && x.group !== lastGroup) { h += '<div class="vs-grp">' + esc(x.group || 'other') + '</div>'; lastGroup = x.group; }
        var on = selected.indexOf(x.value) >= 0;
        h += '<div class="vs-it' + (on ? ' on' : '') + (i === hi ? ' hi' : '') + '" data-v="' + esc(x.value) + '"><span class="cb">' + (on ? '✓' : '') + '</span><span class="l" title="' + esc(x.value) + '">' + esc(x.label) + '</span>' + (x.hint ? '<span class="h" title="' + esc(x.hint) + '">' + esc(x.hint) + '</span>' : '') + '</div>';
        i++;
      });
      if (f && allowNew && !vis.some(function(x){ return x.value === f; })) h += '<div class="vs-it add" data-add="' + esc(f) + '"><span class="cb">+</span><span class="l">Use “' + esc(f) + '”</span></div>';
      if (!h) h = '<div class="vs-none">' + (all.length ? 'nothing matches' : (allowNew ? 'type a value and press Enter' : 'nothing to pick from')) + '</div>';
      if (multiple && vis.length > 1) h += '<div class="vs-foot"><span data-all>select all shown</span><span data-none>clear</span><span style="margin-left:auto">' + vis.length + ' of ' + all.length + '</span></div>';
      pop.innerHTML = h;
    }
    function pick(v){
      if (multiple) { if (selected.indexOf(v) >= 0) selected = selected.filter(function(x){ return x !== v; }); else selected.push(v); }
      else { selected = [v]; close(); }
      q.value = ''; write(); chips(); list();
    }
    function open(){
      host.classList.add('open'); hi = -1;
      if (!loaded) { items(name, el).then(function(l){ all = l; loaded = true; chips(); list(); }); }
      list();
      document.addEventListener('mousedown', outside);
    }
    function close(){ host.classList.remove('open'); document.removeEventListener('mousedown', outside); }
    function outside(e){ if (!host.contains(e.target)) close(); }
    pop.addEventListener('mousedown', function(e){ e.preventDefault(); });
    pop.addEventListener('click', function(e){
      var it = e.target.closest('.vs-it'); if (it) { pick(it.getAttribute('data-add') != null ? it.getAttribute('data-add') : it.getAttribute('data-v')); q.focus(); return; }
      if (e.target.hasAttribute('data-all')) { visible().forEach(function(x){ if (selected.indexOf(x.value) < 0) selected.push(x.value); }); write(); chips(); list(); }
      if (e.target.hasAttribute('data-none')) { selected = []; write(); chips(); list(); }
    });
    box.addEventListener('click', function(){ q.focus(); open(); });
    q.addEventListener('focus', open);
    q.addEventListener('input', function(){ hi = -1; list(); });
    q.addEventListener('keydown', function(e){
      var rows = pop.querySelectorAll('.vs-it');
      if (e.key === 'ArrowDown') { hi = Math.min(rows.length - 1, hi + 1); list(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { hi = Math.max(0, hi - 1); list(); e.preventDefault(); }
      else if (e.key === 'Enter') { e.preventDefault(); var r = pop.querySelectorAll('.vs-it')[hi >= 0 ? hi : 0]; if (r) pick(r.getAttribute('data-add') != null ? r.getAttribute('data-add') : r.getAttribute('data-v')); else if (allowNew && q.value.trim()) pick(q.value.trim()); }
      else if (e.key === 'Escape') { close(); }
      else if (e.key === 'Backspace' && !q.value && selected.length) { selected.pop(); write(); chips(); list(); }
    });
    // the field can still be set from code: mirror it
    var sync = function(){ if (document.activeElement === q) return; var now = read(); if (now.join('') !== selected.join('')) { selected = now; chips(); } };
    el.addEventListener('vera:select:sync', sync);
    var desc = Object.getOwnPropertyDescriptor(el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value');
    if (desc && desc.set && el.tagName !== 'SELECT') {
      Object.defineProperty(el, 'value', {get: function(){ return desc.get.call(el); }, set: function(v){ desc.set.call(el, v); sync(); }, configurable: true});
    }
    selected = read(); chips();
    items(name, el).then(function(l){ all = l; loaded = true; chips(); });
    el._veraSelect = {host: host, get: function(){ return selected.slice(); }, set: function(v){ selected = uniq(Array.isArray(v) ? v : [v]); write(); chips(); }, refresh: function(){ loaded = false; delete CACHE[name + '|']; items(name, el).then(function(l){ all = l; loaded = true; chips(); list(); }); }};
    return el._veraSelect;
  }
  function scan(root){ Array.prototype.forEach.call((root || document).querySelectorAll('[data-select]'), function(el){ if (!el._veraSelect) enhance(el); }); }
  var pending = null;
  function watch(){
    if (!window.MutationObserver || !document.body) return;
    new MutationObserver(function(){ if (pending) return; pending = setTimeout(function(){ pending = null; scan(); }, 120); }).observe(document.body, {childList: true, subtree: true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function(){ scan(); watch(); }); else { scan(); watch(); }
  window.veraSelect = {enhance: enhance, scan: scan, source: source, items: items, pluck: pluck};
})();
