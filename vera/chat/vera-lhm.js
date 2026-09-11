/* ═══════════════════════════════════════════════════════════════════════════
 * vera-lhm.js — the ONE left-hand menu (UI redesign, Notes/40 §2 + §9; the
 * Canvas / ChatMenu / Harness boards).
 *
 * A quick-menu LHM is a 46px icon RAIL of menus beside a 272px DETAIL column:
 * a HEADER (title · meta · ✎), the menu's TAB STRIP, its panes, and one CTA.
 * ☰ at the top of the rail swaps the detail column to the TOP-LEVEL LIST —
 * every menu, and the panels open right now — IN PLACE; picking a row swaps
 * back. Every part is a widget: rail, header, tab strip, list, CTA carry
 * data-w="label · form" so edit mode (✎) can outline and name them, and a
 * later slice can give each a record.
 *
 * The same code serves two hosts:
 *   • a page that OWNS the menu (the chat): VeraLHM.mount({host, menus, …})
 *     builds the rail and the header around the page's existing tab strip and
 *     panes — nothing is re-implemented, nothing is removed; the existing
 *     tabs keep their handlers, they are only grouped under the rail's menus.
 *   • a page that HOSTS another page's menu (the harness with the chat open):
 *     VeraLHM.absorb(host, spec, pick) renders the same rail + tab strip from
 *     the spec the owner published, and pick() sends the choice back. The
 *     owner is told it is hosted (vera:panel:nav_hosted) and hides its own
 *     rail — one LHM, not two.
 *
 * Owner ↔ host protocol = the existing panel bridge messages (see
 * vera-panel-bridge.js): the owner publishes vera:panel:state with
 * state.nav = {items, active, lhm:spec}; the host answers nav_hosted /
 * nav_unhosted and dispatches vera:panel:action {action:'nav_select',
 * payload:{id}} with id = '<menu>' or '<menu>/<tab>'. The owner publishes
 * ONLY when it is embedded (window.parent !== window): a standalone chat
 * would otherwise receive its own message on the listener it keeps for the
 * panels IT hosts.
 * ═══════════════════════════════════════════════════════════════════════ */
(function(){
  if(window.VeraLHM) return;   // idempotent

  var CSS = [
    /* the rail */
    '.lhm-rail{width:46px;flex:0 0 46px;display:flex;flex-direction:column;align-items:center;gap:2px;padding:6px 0;background:var(--bg1);border-right:1px solid var(--border);box-sizing:border-box;overflow:hidden}',
    'html[data-blocks="off"] .lhm-rail{background:transparent;border-right-color:transparent}',
    '.lhm-rail .lhm-ico{position:relative;width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:var(--r-sm,7px);color:var(--dim2);font-size:15px;line-height:1;cursor:pointer;user-select:none;border:1px solid transparent;flex-shrink:0}',
    '.lhm-rail .lhm-ico:hover{color:var(--text);background:var(--bg2)}',
    '.lhm-rail .lhm-ico.on{color:var(--acc);background:var(--bg2);border-color:var(--border)}',
    '.lhm-rail .lhm-ico.top{font-size:16px;margin-bottom:4px}',
    '.lhm-rail .lhm-ico.top.on{color:var(--text)}',
    '.lhm-rail .lhm-ico .lhm-badge{position:absolute;top:-3px;right:-3px;min-width:14px;height:14px;padding:0 3px;border-radius:7px;background:var(--acc);color:var(--on-acc,#fff);font-family:var(--mono);font-size:8px;font-weight:700;line-height:14px;text-align:center;box-sizing:border-box}',
    '.lhm-rail .lhm-sp{flex:1}',
    /* the detail column: whatever the owner already had, wrapped */
    '.lhm-det{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}',
    '.lhm-hd{display:flex;align-items:baseline;gap:6px;padding:8px 9px 5px;border-bottom:1px solid var(--border);flex-shrink:0}',
    'html[data-blocks="off"] .lhm-hd{border-bottom-color:transparent}',
    '.lhm-hd h2{margin:0;font-family:var(--f-disp,var(--sans));font-size:12.5px;font-weight:600;color:var(--text);letter-spacing:-.1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-hd .lhm-meta{flex:1;min-width:0;font-family:var(--mono);font-size:9px;color:var(--dim2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-hd .lhm-edit{flex:0 0 auto;background:transparent;border:1px solid transparent;border-radius:var(--r-sm,4px);color:var(--dim2);font-size:11px;line-height:1;padding:2px 5px;cursor:pointer}',
    '.lhm-hd .lhm-edit:hover{color:var(--text);border-color:var(--border)}',
    '.lhm-hd .lhm-edit.on{color:var(--acc);border-color:var(--acc)}',
    /* the tab strip the owner already had: only the current menu\'s tabs show */
    '.lhm-det .ctab.lhm-off{display:none!important}',
    /* the top-level list */
    '.lhm-top{display:none;flex-direction:column;gap:2px;padding:6px;overflow-y:auto;flex:1;min-height:0}',
    '.lhm-topmode .lhm-top{display:flex}',
    '.lhm-topmode .lhm-det > :not(.lhm-hd):not(.lhm-top){display:none!important}',
    '.lhm-top .lhm-sec{font-family:var(--mono);font-size:8px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);padding:6px 4px 3px}',
    '.lhm-top .lhm-row{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:var(--r-sm,6px);cursor:pointer;color:var(--text);font-size:11px;border:1px solid transparent}',
    '.lhm-top .lhm-row:hover{background:var(--bg2);border-color:var(--border)}',
    '.lhm-top .lhm-row.on{color:var(--acc)}',
    '.lhm-top .lhm-row .lhm-ri{width:18px;text-align:center;color:var(--dim2);font-size:13px;flex-shrink:0}',
    '.lhm-top .lhm-row .lhm-rn{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.lhm-top .lhm-row .lhm-rm{font-family:var(--mono);font-size:8.5px;color:var(--dim2);white-space:nowrap}',
    '.lhm-top .lhm-row .lhm-rx{flex:0 0 auto;background:transparent;border:none;color:var(--dim2);font-size:11px;cursor:pointer;padding:0 2px}',
    '.lhm-top .lhm-row .lhm-rx:hover{color:var(--err)}',
    '.lhm-top .lhm-empty{padding:4px 8px;font-size:10px;color:var(--dim)}',
    /* the CTA */
    '.lhm-cta{flex-shrink:0;margin:6px 7px 7px;padding:6px 10px;border:1px solid var(--border);border-radius:var(--r-sm,6px);background:var(--bg2);color:var(--text);font-family:var(--sans);font-size:10.5px;text-align:left;cursor:pointer}',
    '.lhm-cta:hover{border-color:var(--acc);color:var(--acc)}',
    '.lhm-topmode .lhm-cta{display:none!important}',
    /* every part is a widget: edit mode outlines and names them; ⚙ opens the part's record, ⧉ saves it as a template */
    '.lhm-wbar{display:none;position:absolute;top:2px;right:4px;z-index:6;gap:2px}',
    '.lhm-editing .lhm-wbar{display:flex}',
    '.lhm-wbar button{width:18px;height:18px;border:1px solid var(--border);border-radius:var(--r-sm,4px);background:var(--bg1);color:var(--dim2);font-size:10px;line-height:1;cursor:pointer;padding:0}',
    '.lhm-wbar button:hover{color:var(--acc);border-color:var(--acc)}',
    '.lhm-wcfg{display:none;flex-direction:column;gap:6px;padding:8px;overflow-y:auto;flex:1;min-height:0}',
    '.lhm-wcfgmode .lhm-wcfg{display:flex}',
    '.lhm-wcfgmode .lhm-det > :not(.lhm-hd):not(.lhm-wcfg){display:none!important}',
    '.lhm-wcfg .lhm-wr{display:grid;grid-template-columns:64px 1fr;border-bottom:1px solid var(--border);font-size:10.5px}',
    '.lhm-wcfg .lhm-wr .k{font-family:var(--mono);font-size:8px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);padding:5px 0}',
    '.lhm-wcfg .lhm-wr .v{padding:4px 0 4px 6px;line-height:1.45;word-break:break-word;color:var(--text)}',
    '.lhm-wcfg .lhm-wr .v code{font-family:var(--mono);font-size:9.5px;color:var(--acc)}',
    '.lhm-wcfg .lhm-wacts{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}',
    '.lhm-wcfg .lhm-wacts button{font-size:10px;padding:3px 8px;border:1px solid var(--border);border-radius:var(--r-sm,5px);background:var(--bg2);color:var(--text);cursor:pointer}',
    '.lhm-wcfg .lhm-wacts button:hover{border-color:var(--acc);color:var(--acc)}',
    '.lhm-wcfg .lhm-wnote{font-family:var(--mono);font-size:8.5px;color:var(--dim2)}',
    '.lhm-editing [data-w]{outline:1px dashed var(--acc);outline-offset:-1px;position:relative}',
    '.lhm-editing [data-w]::before{content:attr(data-w);position:absolute;top:0;left:0;z-index:5;font-family:var(--mono);font-size:8px;line-height:1;padding:2px 4px;background:var(--acc);color:var(--on-acc,#fff);border-radius:0 0 4px 0;pointer-events:none;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}',
    /* hosted elsewhere (the harness draws the rail + tab strip): the owner keeps header, panes and CTA */
    'html.vpb-nav-hosted .lhm-rail,html.vpb-nav-hosted .lhm-det .ctx-tab-bar{display:none!important}',
    /* an absorbed menu in a host */
    '.lhm-absorbed{display:flex;flex-direction:row;min-height:0;flex:1}',
    '.lhm-absorbed .lhm-tabs{flex:1;display:flex;flex-direction:column;gap:1px;padding:6px 4px;min-width:0;overflow-y:auto}',
    '.lhm-absorbed .lhm-tab{padding:6px 8px;border-radius:var(--r-sm,5px);font-family:var(--mono);font-size:9.5px;letter-spacing:.3px;color:var(--dim2);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-left:2px solid transparent}',
    '.lhm-absorbed .lhm-tab:hover{color:var(--text);background:var(--bg2)}',
    '.lhm-absorbed .lhm-tab.on{color:var(--acc);border-left-color:var(--acc);background:var(--bg2)}',
    '.lhm-absorbed .lhm-tabs .lhm-ttl{font-family:var(--sans);font-size:11px;font-weight:600;color:var(--text);padding:4px 8px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
  ].join('\n');

  function _css(doc){
    doc = doc || document;
    if(doc.getElementById('vera-lhm-css')) return;
    var s = doc.createElement('style'); s.id = 'vera-lhm-css'; s.textContent = CSS;
    (doc.head || doc.documentElement).appendChild(s);
  }
  function _el(tag, cls, text){ var e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e; }
  function _esc(s){ return String(s == null ? '' : s); }

  // ── the owner side ─────────────────────────────────────────────────────
  var _cfg = null, _host = null, _rail = null, _det = null, _hd = null, _top = null, _cta = null;
  var _active = '', _activeTab = '', _topMode = false, _editing = false;
  var _pid = '', _embedded = false, _hosted = false, _picking = false;   // _picking: the click is ours, not the user's
  var _wcfg = null, _wcfgOpen = false;   // the record sheet

  // the tab strip's elements, keyed by the tab id the owner gave the menu
  function _tabEl(id){
    if(!_cfg || !_cfg.tabEl) return null;
    try{ return _cfg.tabEl(id); }catch(e){ return null; }
  }
  function _menu(id){ return (_cfg && _cfg.menus || []).filter(function(m){ return m.id === id; })[0] || null; }
  function _menuOfTab(tabId){ return (_cfg && _cfg.menus || []).filter(function(m){ return (m.tabs || []).some(function(t){ return t.id === tabId; }); })[0] || null; }

  function _renderRail(){
    if(!_rail) return;
    _rail.innerHTML = '';
    var top = _el('div', 'lhm-ico top' + (_topMode ? ' on' : ''), '☰');
    top.title = 'Everything — every menu, and what is open now';
    var openN = _openNow().length; if(openN){ var b = _el('span', 'lhm-badge', String(openN)); top.appendChild(b); }
    top.addEventListener('click', function(){ toggleTop(); });
    _rail.appendChild(top);
    (_cfg.menus || []).forEach(function(m){
      var ico = _el('div', 'lhm-ico' + (m.id === _active && !_topMode ? ' on' : ''), m.icon || '•');
      ico.title = m.label + (m.tabs && m.tabs.length > 1 ? ' — ' + m.tabs.map(function(t){ return t.label; }).join(' · ') : '');
      ico.setAttribute('data-menu', m.id);
      var badge = 0; try{ badge = m.badge ? +m.badge() : 0; }catch(e){}
      if(badge){ ico.appendChild(_el('span', 'lhm-badge', String(badge))); }
      ico.addEventListener('click', function(){ pick(m.id); });
      _rail.appendChild(ico);
    });
    _rail.appendChild(_el('div', 'lhm-sp'));
  }
  function _renderHeader(){
    if(!_hd) return;
    var m = _menu(_active);
    var h2 = _hd.querySelector('h2'), meta = _hd.querySelector('.lhm-meta'), ed = _hd.querySelector('.lhm-edit');
    if(_topMode){ h2.textContent = _cfg.title || 'Vera'; meta.textContent = (_cfg.menus || []).length + ' menus · ' + _openNow().length + ' open'; }
    else { h2.textContent = m ? (m.title || m.label) : ''; var s = ''; try{ s = m && m.meta ? String(m.meta() || '') : ''; }catch(e){} meta.textContent = s; }
    ed.classList.toggle('on', _editing);
  }
  function _renderTabs(){
    // only the current menu's tabs show in the owner's strip; the others stay in the DOM with their handlers
    var m = _menu(_active); var ids = {};
    if(m) (m.tabs || []).forEach(function(t){ ids[t.id] = 1; });
    (_cfg.menus || []).forEach(function(mm){ (mm.tabs || []).forEach(function(t){ var el = _tabEl(t.id); if(el) el.classList.toggle('lhm-off', !ids[t.id]); }); });
  }
  function _renderCta(){
    if(!_cta) return;
    var m = _menu(_active);
    if(!m || !m.cta){ _cta.style.display = 'none'; return; }
    _cta.style.display = ''; _cta.textContent = m.cta.label || '→';
  }
  function _openNow(){
    var out = [];
    try{ if(_cfg && _cfg.openNow) out = _cfg.openNow() || []; }catch(e){}
    return out;
  }
  function _renderTop(){
    if(!_top) return;
    _top.innerHTML = '';
    _top.appendChild(_el('div', 'lhm-sec', 'Open now · ' + _openNow().length + ' · one set, one bridge'));
    var open = _openNow();
    if(!open.length) _top.appendChild(_el('div', 'lhm-empty', 'Nothing open beside the chat. A menu, the aide or you can open a panel here.'));
    open.forEach(function(o){
      var r = _el('div', 'lhm-row');
      r.appendChild(_el('span', 'lhm-ri', o.icon || '▭'));
      r.appendChild(_el('span', 'lhm-rn', o.label || o.id));
      r.appendChild(_el('span', 'lhm-rm', [o.origin || '', o.placement || ''].filter(Boolean).join(' · ')));
      if(o.close){ var x = _el('button', 'lhm-rx', '✕'); x.title = 'Close'; x.addEventListener('click', function(ev){ ev.stopPropagation(); try{ o.close(); }catch(e){} render(); }); r.appendChild(x); }
      if(o.focus) r.addEventListener('click', function(){ try{ o.focus(); }catch(e){} });
      _top.appendChild(r);
    });
    _top.appendChild(_el('div', 'lhm-sec', 'Menus · ' + (_cfg.menus || []).length));
    (_cfg.menus || []).forEach(function(m){
      var r = _el('div', 'lhm-row' + (m.id === _active ? ' on' : ''));
      r.appendChild(_el('span', 'lhm-ri', m.icon || '•'));
      r.appendChild(_el('span', 'lhm-rn', m.label));
      r.appendChild(_el('span', 'lhm-rm', (m.tabs || []).map(function(t){ return t.label; }).join(' · ')));
      r.addEventListener('click', function(){ pick(m.id); });
      _top.appendChild(r);
    });
    if(_cfg.topExtra){ try{ _cfg.topExtra(_top); }catch(e){} }
  }
  function render(){
    if(!_cfg) return;
    if(_host) _host.classList.toggle('lhm-topmode', _topMode);
    if(_host) _host.classList.toggle('lhm-editing', _editing);
    _renderRail(); _renderHeader(); _renderTabs(); _renderCta(); _renderTop();
    if(_editing) _wireBars();
    if(!_editing && _wcfgOpen) closeRecord();
    _publish();
  }

  // pick('<menu>') opens the menu on its remembered / first tab; pick('<menu>/<tab>') or pick('<tab>') a tab
  function pick(id, opts){
    id = String(id || ''); opts = opts || {};
    var menuId = id, tabId = '';
    if(id.indexOf('/') >= 0){ menuId = id.split('/')[0]; tabId = id.split('/')[1]; }
    var m = _menu(menuId);
    if(!m){ var mt = _menuOfTab(id); if(mt){ m = mt; menuId = mt.id; tabId = id; } }
    if(!m) return false;
    if(!tabId) tabId = (m._last && (m.tabs || []).some(function(t){ return t.id === m._last; })) ? m._last : ((m.tabs || [])[0] || {}).id || '';
    _active = menuId; _activeTab = tabId; m._last = tabId; _topMode = false;
    var el = _tabEl(tabId);
    if(el && !opts.silent){ _picking = true; try{ el.click(); }catch(e){} _picking = false; }
    if(_cfg.onPick){ try{ _cfg.onPick(menuId, tabId); }catch(e){} }
    render();
    return true;
  }
  // the owner's own UI switched tab (a direct click) — keep the rail and the header in step
  function setActiveTab(tabId){
    var m = _menuOfTab(tabId); if(!m) return;
    _active = m.id; _activeTab = tabId; m._last = tabId; _topMode = false; render();
  }
  function toggleTop(on){ _topMode = (on == null) ? !_topMode : !!on; render(); }
  function toggleEdit(on){ _editing = (on == null) ? !_editing : !!on; if(!_editing) closeRecord(); render(); }

  // ── every part's record: the widget registry's template behind it ──────
  function _base(){ try{ return (_cfg && _cfg.base) || window._veraBase || location.origin; }catch(e){ return ''; } }
  function _tplOf(el){ return el ? (el.getAttribute('data-tpl') || '') : ''; }
  function _wireBars(){
    if(!_host) return;
    var parts = _host.querySelectorAll('[data-w]');
    Array.prototype.forEach.call(parts, function(el){
      if(el.querySelector(':scope > .lhm-wbar')) return;
      var bar = _el('div', 'lhm-wbar');
      var cfgB = _el('button', '', '⚙'); cfgB.title = 'This widget\'s record';
      cfgB.addEventListener('click', function(ev){ ev.stopPropagation(); openRecord(_tplOf(el), el.getAttribute('data-w')); });
      var saveB = _el('button', '', '⧉'); saveB.title = 'Save as a template of your own';
      saveB.addEventListener('click', function(ev){ ev.stopPropagation(); saveAsTemplate(_tplOf(el), el.getAttribute('data-w')); });
      bar.appendChild(cfgB); bar.appendChild(saveB);
      if(getComputedStyle(el).position === 'static') el.style.position = 'relative';
      el.appendChild(bar);
    });
  }
  function _row(k, v){ var r = _el('div', 'lhm-wr'); r.appendChild(_el('span', 'k', k)); var vv = _el('span', 'v'); vv.innerHTML = v; r.appendChild(vv); return r; }
  function _escH(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]; }); }
  function openRecord(tplId, label){
    if(!_wcfg) return;
    _wcfg.innerHTML = ''; _wcfgOpen = true; _host.classList.add('lhm-wcfgmode');
    var hd = _el('div', 'lhm-wnote', (label || 'widget') + (tplId ? ' · ' + tplId : ' · no record yet')); _wcfg.appendChild(hd);
    var closeRow = _el('div', 'lhm-wacts'); var x = _el('button', '', '✕ close'); x.addEventListener('click', closeRecord); closeRow.appendChild(x); _wcfg.appendChild(closeRow);
    if(!tplId){ _wcfg.appendChild(_el('div', 'lhm-wnote', 'This part has no template in the registry yet — ⧉ saves it as one.')); return; }
    fetch(_base() + '/ui/widgets/template?id=' + encodeURIComponent(tplId)).then(function(r){ return r.json(); }).then(function(r){
      if(!r || !r.ok){ _wcfg.appendChild(_el('div', 'lhm-wnote', (r && r.error) || 'registry unavailable')); return; }
      var t = r.template, reads = t.reads || {}, draw = t.draw || {};
      _wcfg.appendChild(_row('template', _escH(t.id) + ' · v' + (t.version || 1)));
      _wcfg.appendChild(_row('form', _escH(t.form)));
      _wcfg.appendChild(_row('reads', reads.cap ? '<code>' + _escH(reads.cap) + '</code>' + (reads.args && Object.keys(reads.args).length ? ' ' + _escH(JSON.stringify(reads.args)) : '') + (reads.note ? ' · ' + _escH(reads.note) : '') : '—'));
      _wcfg.appendChild(_row('frame', _escH(t.frame || '—')));
      _wcfg.appendChild(_row('draw', _escH(draw.form || t.form) + ' · size ' + _escH(draw.size || 'M')));
      _wcfg.appendChild(_row('can', _escH((t.can || []).join(' · ') || '—')));
      _wcfg.appendChild(_row('placed', _escH((t.placements || []).map(function(p){ return p.where + (p.count > 1 ? ' ×' + p.count : ''); }).join(' · ') || '—')));
      var acts = _el('div', 'lhm-wacts');
      var sv = _el('button', '', '⧉ Save as my template'); sv.addEventListener('click', function(){ saveAsTemplate(tplId, label); }); acts.appendChild(sv);
      ['dashboard', 'canvas', 'LHM'].forEach(function(w){ var b = _el('button', '', '+ ' + w); b.title = 'Place into ' + w; b.addEventListener('click', function(){ placeInto(tplId, w); }); acts.appendChild(b); });
      _wcfg.appendChild(acts);
    }).catch(function(){ _wcfg.appendChild(_el('div', 'lhm-wnote', 'registry unavailable')); });
  }
  function closeRecord(){ _wcfgOpen = false; if(_host) _host.classList.remove('lhm-wcfgmode'); }
  function saveAsTemplate(tplId, label){
    var name = ''; try{ name = window.prompt('Name for your template', (label || 'widget').split(' · ')[0] + ' (mine)') || ''; }catch(e){}
    if(!name) return;
    var go = function(t){
      var copy = Object.assign({}, t || {}, { id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64), name: name,
        source: { origin: 'you', from: 'the chat LHM', from_builtin: tplId || '', panel: (t && t.source && t.source.panel) || '' } });
      if(!copy.form){ copy.form = ((label || '').split(' · ')[1] || 'list').trim(); }
      if(!copy.reads) copy.reads = { cap: '', args: {} };
      copy.placed = (t && t.placements ? t.placements.map(function(p){ return p.where; }) : ['LHM']); delete copy.placements; delete copy.instances;
      return fetch(_base() + '/ui/widgets/templates/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template: copy, force: !tplId }) })
        .then(function(r){ return r.json(); }).then(function(r){ if(_wcfg && _wcfgOpen) _wcfg.appendChild(_el('div', 'lhm-wnote', r && r.ok ? 'saved ' + r.template.id + ' · v' + r.template.version : 'save failed: ' + ((r && (r.error || (r.problems || []).join('; '))) || '?'))); });
    };
    if(tplId) fetch(_base() + '/ui/widgets/template?id=' + encodeURIComponent(tplId)).then(function(r){ return r.json(); }).then(function(r){ return go(r && r.ok ? r.template : null); }).catch(function(){ go(null); });
    else go(null);
  }
  function placeInto(tplId, where){
    var sid = ''; try{ sid = _cfg.sessionId ? String(_cfg.sessionId() || '') : ''; }catch(e){}
    fetch(_base() + '/ui/widgets/instantiate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: tplId, where: where, host: where === 'dashboard' ? 'main' : '', session_id: sid }) })
      .then(function(r){ return r.json(); }).then(function(r){ if(_wcfg && _wcfgOpen) _wcfg.appendChild(_el('div', 'lhm-wnote', r && r.ok ? 'placed into ' + where + ' · ' + r.instance.id : 'place failed: ' + ((r && r.error) || '?'))); });
  }

  function spec(){
    if(!_cfg) return null;
    return {
      title: _cfg.title || '', active: { menu: _active, tab: _activeTab, top: _topMode },
      menus: (_cfg.menus || []).map(function(m){ var b = 0; try{ b = m.badge ? +m.badge() : 0; }catch(e){} return { id: m.id, icon: m.icon || '•', label: m.label, title: m.title || m.label, badge: b, tabs: (m.tabs || []).map(function(t){ return { id: t.id, label: t.label }; }) }; }),
      open: _openNow().map(function(o){ return { id: o.id, label: o.label, icon: o.icon || '', origin: o.origin || '', placement: o.placement || '' }; })
    };
  }

  // ── owner ↔ host (the panel bridge's messages) ─────────────────────────
  var _lastSig = '';
  function _publish(force){
    if(!_embedded || !_cfg) return;
    var s = spec(); if(!s) return;
    var nav = { items: s.menus.map(function(m){ return { id: m.id, label: m.label }; }), active: s.active.menu, lhm: s };
    var sig = JSON.stringify(nav) + '|' + _pid;
    if(!force && sig === _lastSig) return; _lastSig = sig;
    try{ window.parent.postMessage({ type: 'vera:panel:state', panel_id: _pid, session_id: _cfg.sessionId ? String(_cfg.sessionId() || '') : '', state: { nav: nav, lhm: true } }, '*'); }catch(e){}
  }
  function _onMessage(ev){
    var d = ev.data; if(!d || typeof d !== 'object') return;
    var t = d.type || '';
    if(t === 'vera:panel:init'){ if(d.panel_id) _pid = String(d.panel_id); _publish(true); }
    else if(t === 'vera:panel:nav_hosted'){ _hosted = true; document.documentElement.classList.add('vpb-nav-hosted'); }
    else if(t === 'vera:panel:nav_unhosted'){ _hosted = false; document.documentElement.classList.remove('vpb-nav-hosted'); }
    else if(t === 'vera:panel:action' && d.action === 'nav_select'){
      var id = d.payload && d.payload.id != null ? String(d.payload.id) : '';
      var ok = id === '☰' ? (toggleTop(true), true) : pick(id);
      try{ window.parent.postMessage({ type: 'vera:panel:action_result', panel_id: _pid, action_id: d.action_id || '', action: 'nav_select', ok: !!ok, result: ok ? { menu: _active, tab: _activeTab } : null, error: ok ? null : 'no menu or tab ' + id }, '*'); }catch(e){}
    }
  }

  function mount(cfg){
    if(!cfg || !cfg.host) throw new Error('VeraLHM.mount: host required');
    _cfg = cfg; _host = cfg.host; _css();
    _embedded = (function(){ try{ return window.parent && window.parent !== window; }catch(e){ return true; } })();
    // wrap what the owner already has into the detail column, put the rail before it
    if(!_host.querySelector(':scope > .lhm-det')){
      _det = _el('div', 'lhm-det');
      while(_host.firstChild) _det.appendChild(_host.firstChild);
      _host.appendChild(_det);
    } else _det = _host.querySelector(':scope > .lhm-det');
    _rail = _el('div', 'lhm-rail'); _rail.setAttribute('data-w', 'rail · ' + (cfg.menus || []).length + ' icons · order · badges'); _rail.setAttribute('data-tpl', 'lhm:rail');
    _host.insertBefore(_rail, _det);
    _hd = _el('div', 'lhm-hd'); _hd.setAttribute('data-w', 'menu header · header'); _hd.setAttribute('data-tpl', 'lhm:header');
    _hd.appendChild(_el('h2', '', '')); _hd.appendChild(_el('span', 'lhm-meta mono', ''));
    var ed = _el('button', 'lhm-edit', '✎'); ed.title = 'Edit this menu — every part is a widget'; ed.addEventListener('click', function(){ toggleEdit(); }); _hd.appendChild(ed);
    _det.insertBefore(_hd, _det.firstChild);
    var strip = cfg.tabBar ? _det.querySelector(cfg.tabBar) : null;
    if(strip && !strip.getAttribute('data-w')) strip.setAttribute('data-w', 'tabs · strip');
    _top = _el('div', 'lhm-top'); _top.setAttribute('data-w', 'top list · list');
    _det.insertBefore(_top, _hd.nextSibling);
    _wcfg = _el('div', 'lhm-wcfg'); _wcfg.setAttribute('data-w', 'widget record · sheet');   // the record sheet, made before it is placed
    _det.insertBefore(_wcfg, _top.nextSibling);
    _cta = _el('button', 'lhm-cta'); _cta.setAttribute('data-w', 'cta · button'); _cta.setAttribute('data-tpl', 'lhm:cta');
    _cta.addEventListener('click', function(){ var m = _menu(_active); if(m && m.cta && m.cta.run){ try{ m.cta.run(); }catch(e){} } });
    _det.appendChild(_cta);
    (cfg.panes || []).forEach(function(p){ var el = document.getElementById(p.id); if(!el) return; if(!el.getAttribute('data-w')) el.setAttribute('data-w', p.w || (p.id + ' · list')); if(p.tpl) el.setAttribute('data-tpl', p.tpl); });
    window.addEventListener('message', _onMessage);
    // the owner's tab strip may be clicked directly: follow it
    if(strip) strip.addEventListener('click', function(ev){ if(_picking) return; var t = ev.target && ev.target.closest ? ev.target.closest('.ctab') : null; if(!t || !cfg.tabIdOf) return; var id = ''; try{ id = cfg.tabIdOf(t) || ''; }catch(e){} if(id) setTimeout(function(){ setActiveTab(id); }, 0); });
    _host.classList.add('lhm-host');
    var first = cfg.initial || ((cfg.menus || [])[0] || {}).id;
    if(first) pick(first, { silent: !!cfg.silentInitial });
    return { pick: pick, render: render, spec: spec, toggleTop: toggleTop, toggleEdit: toggleEdit };
  }

  // ── the host side: draw another page's menu from its spec ──────────────
  // host: an element to fill; spec: what the owner published (state.nav.lhm); pick(id): send it back
  function absorb(host, spec, pickFn, opts){
    if(!host || !spec) return null;
    opts = opts || {}; _css(host.ownerDocument);
    host.innerHTML = '';
    var wrap = _el('div', 'lhm-absorbed');
    var rail = _el('div', 'lhm-rail'); rail.setAttribute('data-w', 'rail · absorbed · ' + (spec.menus || []).length + ' icons');
    var top = _el('div', 'lhm-ico top' + (opts.topOn ? ' on' : ''), '☰'); top.title = opts.topTitle || 'This page\'s own menu';
    top.addEventListener('click', function(){ if(opts.onTop) opts.onTop(); });
    rail.appendChild(top);
    var act = spec.active || {};
    (spec.menus || []).forEach(function(m){
      var ico = _el('div', 'lhm-ico' + (m.id === act.menu ? ' on' : ''), m.icon || '•'); ico.title = m.label;
      if(m.badge) ico.appendChild(_el('span', 'lhm-badge', String(m.badge)));
      ico.addEventListener('click', function(){ pickFn(m.id); });
      rail.appendChild(ico);
    });
    rail.appendChild(_el('div', 'lhm-sp'));
    wrap.appendChild(rail);
    var tabs = _el('div', 'lhm-tabs'); tabs.setAttribute('data-w', 'tabs · absorbed');
    var cur = (spec.menus || []).filter(function(m){ return m.id === act.menu; })[0];
    if(cur){
      tabs.appendChild(_el('div', 'lhm-ttl', cur.title || cur.label));
      (cur.tabs || []).forEach(function(t){
        var e = _el('div', 'lhm-tab' + (t.id === act.tab ? ' on' : ''), t.label);
        e.addEventListener('click', function(){ pickFn(cur.id + '/' + t.id); });
        tabs.appendChild(e);
      });
    }
    wrap.appendChild(tabs);
    host.appendChild(wrap);
    return wrap;
  }

  window.VeraLHM = { mount: mount, pick: pick, setActiveTab: setActiveTab, toggleTop: toggleTop, toggleEdit: toggleEdit, render: render, spec: spec, absorb: absorb, css: _css,
    openRecord: openRecord, closeRecord: closeRecord, saveAsTemplate: saveAsTemplate, placeInto: placeInto,
    get active(){ return { menu: _active, tab: _activeTab, top: _topMode, editing: _editing, hosted: _hosted, embedded: _embedded }; } };
})();
