/* vera-entity-drawer.js — one slide-over for one thing, from any page.
 *
 *   <script src="/ui/vera-entity-drawer.js"></script>
 *   veraEntityDrawer.open('guest:145');
 *
 * Asks estate.entity.resolve for the record and draws it: title, facts, the
 * five registration planes as chips, the entities it touches, and where to
 * go next. Every link goes through veraUI.openEntity, so the page hosting the
 * drawer never has to know where another kind of thing lives. Styled from the
 * host page's --bg, --text and --border variables so it sits inside any panel.
 */
(function(){
  if (window.veraEntityDrawer) return;
  var BASE = (window.__VERA_BASE__ || (window.veraUI && window.veraUI.BASE) || '').replace(/\/$/, '');
  var CSS = [
    '#vera-ed{position:fixed;top:0;right:0;height:100%;width:min(440px,100%);z-index:9000;',
    '  background:var(--bg1,var(--s1,#14181d));color:var(--text,var(--t1,#d8dde3));border-left:1px solid var(--border,var(--bd,#232a33));',
    '  box-shadow:-12px 0 30px rgba(0,0,0,.35);display:flex;flex-direction:column;font:12px/1.5 system-ui,sans-serif;',
    '  transform:translateX(100%);transition:transform .18s ease-out}',
    '#vera-ed.on{transform:none}',
    '@media (prefers-reduced-motion:reduce){#vera-ed{transition:none}}',
    '#vera-ed .hd{padding:12px 14px;border-bottom:1px solid var(--border,#232a33);display:flex;gap:10px;align-items:flex-start}',
    '#vera-ed .hd b{font-size:14px;display:block}',
    '#vera-ed .hd .sub{color:var(--dim,var(--t3,#5f6975));font-size:11px}',
    '#vera-ed .x{margin-left:auto;background:none;border:1px solid var(--border,#232a33);color:inherit;border-radius:4px;padding:2px 8px;cursor:pointer;font:inherit}',
    '#vera-ed .x:focus-visible{outline:2px solid var(--acc,#4a9eff)}',
    '#vera-ed .bd{overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:14px}',
    '#vera-ed h4{margin:0 0 6px;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim,#5f6975);font-weight:600}',
    '#vera-ed .kv{display:grid;grid-template-columns:110px 1fr;gap:4px 10px;font-size:11px}',
    '#vera-ed .kv span:first-child{color:var(--dim,#5f6975)}',
    '#vera-ed .planes{display:flex;flex-wrap:wrap;gap:6px}',
    '#vera-ed .pl{border:1px solid var(--border,#232a33);border-radius:14px;padding:3px 9px;font-size:10px;background:var(--bg2,#1a1f26);cursor:default}',
    '#vera-ed .pl.yes{border-color:var(--ok,#28c28a);color:var(--ok,#28c28a)}',
    '#vera-ed .pl.no{border-color:var(--err,#ef5b5b);color:var(--err,#ef5b5b)}',
    '#vera-ed .pl.unknown{border-color:var(--warn,#f5b341);color:var(--warn,#f5b341)}',
    '#vera-ed .pl.na{opacity:.55}',
    '#vera-ed .pl[data-ref]{cursor:pointer}',
    '#vera-ed .rel{display:flex;flex-direction:column;gap:4px}',
    '#vera-ed .rel a,#vera-ed .lnk a{display:flex;gap:8px;align-items:baseline;color:inherit;text-decoration:none;padding:5px 8px;border:1px solid var(--border,#232a33);border-radius:5px;background:var(--bg2,#1a1f26)}',
    '#vera-ed .rel a:hover,#vera-ed .lnk a:hover{border-color:var(--acc,#4a9eff)}',
    '#vera-ed .rel .n{font-size:10px;color:var(--dim,#5f6975);min-width:70px}',
    '#vera-ed .rel .d{margin-left:auto;font-size:10px;color:var(--dim,#5f6975);font-family:ui-monospace,monospace}',
    '#vera-ed .lnk{display:flex;flex-direction:column;gap:4px}',
    '#vera-ed .err{color:var(--err,#ef5b5b)}',
    '#vera-ed .note{color:var(--dim,#5f6975);font-size:10px}'
  ].join('\n');

  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  var PLANE_LABEL = {ssh:'SSH login', directory:'Directory', mesh:'Mesh', certificate:'Certificate', backup:'Backup'};
  var el = null, current = '';

  function ensure(){
    if (el) return el;
    var st = document.createElement('style'); st.id = 'vera-ed-css'; st.textContent = CSS; document.head.appendChild(st);
    el = document.createElement('aside'); el.id = 'vera-ed'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', 'Estate entity');
    el.innerHTML = '<div class="hd"><div><b id="vera-ed-t"></b><div class="sub" id="vera-ed-s"></div></div><button class="x" type="button" aria-label="Close">✕</button></div><div class="bd" id="vera-ed-b"></div>';
    el.querySelector('.x').addEventListener('click', close);
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && el.classList.contains('on')) close(); });
    document.body.appendChild(el);
    return el;
  }
  function close(){ if (el) el.classList.remove('on'); current = ''; }
  function go(ref){ if (window.veraUI && window.veraUI.openEntity) { window.veraUI.openEntity(ref); close(); } }
  function goPane(pane, sub, ref){
    var target = {type:'vera:estate:open', pane: pane, sub: sub || '', entity: ref || ''};
    if (window.parent && window.parent !== window) { try { window.parent.postMessage(target, '*'); close(); return; } catch(e){} }
    location.href = BASE + '/ui/panels/workers-ollama?pane=' + encodeURIComponent(pane) + (sub ? '&sub=' + encodeURIComponent(sub) : '') + (ref ? '&entity=' + encodeURIComponent(ref) : '');
  }

  function render(rec){
    var b = document.getElementById('vera-ed-b');
    document.getElementById('vera-ed-t').textContent = rec.title || rec.id || rec.ref || '';
    document.getElementById('vera-ed-s').textContent = rec.subtitle || rec.noun || '';
    if (!rec.found) {
      b.innerHTML = '<div class="err">' + esc(rec.error || 'Nothing found.') + '</div>';
      return;
    }
    var h = '';
    if (rec.facts && rec.facts.length) {
      h += '<section><h4>Facts</h4><div class="kv">' + rec.facts.map(function(f){ return '<span>' + esc(f.label) + '</span><span>' + esc(f.value) + '</span>'; }).join('') + '</div></section>';
    }
    var planes = rec.planes || {}, keys = Object.keys(planes);
    if (keys.length) {
      h += '<section><h4>Registered in</h4><div class="planes">' + keys.map(function(k){
        var p = planes[k] || {}, st = p.state === 'n/a' ? 'na' : (p.state || 'unknown');
        return '<span class="pl ' + st + '"' + (p.ref ? ' data-ref="' + esc(p.ref) + '" tabindex="0"' : '') + ' title="' + esc(p.detail || '') + '">' + esc(PLANE_LABEL[k] || k) + ': ' + esc(p.state) + '</span>';
      }).join('') + '</div>' + keys.map(function(k){ var p = planes[k] || {}; return p.detail ? '<div class="note">' + esc(PLANE_LABEL[k] || k) + ' — ' + esc(p.detail) + '</div>' : ''; }).join('') + '</section>';
    }
    if (rec.related && rec.related.length) {
      h += '<section><h4>Touches</h4><div class="rel">' + rec.related.map(function(r){
        return '<a href="#" data-ref="' + esc(r.ref) + '"><span class="n">' + esc(r.noun || '') + '</span><span>' + esc(r.label) + '</span>' + (r.detail ? '<span class="d">' + esc(r.detail) + '</span>' : '') + '</a>';
      }).join('') + '</div></section>';
    }
    if (rec.links && rec.links.length) {
      h += '<section><h4>Go to</h4><div class="lnk">' + rec.links.map(function(l){
        return '<a href="#"' + (l.pane ? ' data-pane="' + esc(l.pane) + '" data-sub="' + esc(l.sub || '') + '"' : '') + (l.ref ? ' data-ref="' + esc(l.ref) + '"' : '') + '>' + esc(l.label) + ' ›</a>';
      }).join('') + '</div></section>';
    }
    var errs = Object.keys(rec.errors || {});
    if (errs.length) h += '<div class="note">Not read: ' + esc(errs.join(', ')) + '</div>';
    b.innerHTML = h;
    b.querySelectorAll('[data-ref],[data-pane]').forEach(function(a){
      a.addEventListener('click', function(e){
        e.preventDefault();
        if (a.dataset.pane) return goPane(a.dataset.pane, a.dataset.sub, a.dataset.ref);
        if (a.dataset.ref) { if (a.classList.contains('pl') || a.closest('.rel')) open(a.dataset.ref); else go(a.dataset.ref); }
      });
      a.addEventListener('keydown', function(e){ if (e.key === 'Enter') a.click(); });
    });
  }

  function open(ref){
    ensure();
    ref = String(ref || '');
    current = ref;
    el.classList.add('on');
    document.getElementById('vera-ed-t').textContent = ref;
    document.getElementById('vera-ed-s').textContent = '';
    document.getElementById('vera-ed-b').innerHTML = '<div class="note">Looking up ' + esc(ref) + '…</div>';
    fetch(BASE + '/estate/entity/resolve?ref=' + encodeURIComponent(ref), {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(rec){ if (current === ref) render(rec || {found:false, error:'no answer'}); })
      .catch(function(e){ if (current === ref) render({found:false, error:String(e)}); });
  }

  window.veraEntityDrawer = {open: open, close: close};
  // Any element with data-entity opens the drawer: <span data-entity="guest:145">…</span>
  document.addEventListener('click', function(e){
    var t = e.target && e.target.closest && e.target.closest('[data-entity]');
    if (t) { e.preventDefault(); open(t.getAttribute('data-entity')); }
  });
})();
