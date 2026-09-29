/* vera-panel-bridge.js
 * ============================================================
 * Shim panels include to participate in the chat ↔ panel
 * postMessage protocol used by chat_panel.html. Include with:
 *
 *   <script src="/ui/vera-panel-bridge.js"></script>
 *
 * Once included, a panel is BOTH observable and drivable by a
 * Vera chat agent with ZERO extra code:
 *
 *   • State — the shim auto-publishes a snapshot that includes a
 *     `ui` catalog of the panel's buttons and inputs (id + label),
 *     so the agent can see what controls exist.
 *
 *   • Generic actions — the shim registers universal handlers that
 *     work on any panel:
 *         click       {id|label|selector}
 *         set_field   {id, value}
 *         set_fields  {fields:{id:value,…}}
 *         submit      {fields:{…}, click:"<button-id>"}
 *     These map straight onto the controls in the `ui` catalog, so
 *     the agent drives the real UI exactly as a human would.
 *
 *   • Named actions — every button is auto-exposed as a dispatchable
 *     action named after its onclick handler (e.g. "runQuickCycle",
 *     "startTraining"), listed in the published `panel_actions` catalog.
 *     Dispatching that name clicks the button (replaying its onclick),
 *     so for form-driven buttons: set_field the inputs, then dispatch
 *     the action. This gives EVERY panel semantic actions for free.
 *
 * Panels MAY add nicer, semantic handlers on top:
 *
 *   window.VeraPanelBridge.registerActionHandler('lan_scan', p => {…});
 *   window.VeraPanelBridge.registerStateProvider(() => ({selected_id:…}));
 *
 * A custom state provider is MERGED over the generic snapshot, so the
 * `ui` catalog is never lost. Custom action handlers override the
 * generic ones of the same name.
 *
 * Server-side agents reach all of this through the panel.dispatch
 * capability (action + payload → handler return value). The shim tags
 * each reply with the dispatcher's action_id so the chat routes it
 * back to the awaiting cap.
 *
 * • Top-level nav unification — a panel with its own internal top-level
 *   menu of sections opts in with one call, so whichever page mounted it
 *   (chat side-rail OR a main-shell top-level auto-tab) can inject those
 *   sections as real sub-menu items in ITS OWN nav instead of the panel
 *   drawing a second, redundant menu next to the outer one:
 *
 *     window.VeraPanelBridge.registerNav([{id:'test',label:'Test'}, …]);
 *     window.VeraPanelBridge.setNavActive('test');   // call on every switch
 *
 *   Published state gains `nav:{items,active}`. Selecting an injected item
 *   dispatches the generic `nav_select` action ({id}) — with no further
 *   panel code, it clicks whatever element carries a matching data-sec/
 *   -section/-view/-tab/-nav/-pane attribute (the patterns this codebase's
 *   hand-rolled section switchers already use); pass a second argument to
 *   registerNav() only if a panel's switcher doesn't use one of those.
 *
 *   Once a host confirms it's actually rendering the injected menu, it
 *   sends back `{type:'vera:panel:nav_hosted'}`, which the shim turns into
 *   a `vpb-nav-hosted` class on <html>, which hides the panel's own
 *   now-redundant menu. Every panel gets that for free from the shared
 *   /ui/vera-panel.css:
 *     html.vpb-nav-hosted [data-vera-lhm]{display:none}
 *   This is confirmation-driven, never assumed from "am I in an iframe" —
 *   a panel opened standalone, or mounted somewhere that hasn't adopted nav
 *   injection (today: the chat side-rail), never gets this class and keeps
 *   its own rail exactly as before.
 * ============================================================
 */
(function(){
  if(window.VeraPanelBridge) return;   // idempotent

  var _stateProvider = null;
  var _actionHandlers = {};
  var _panelId = '';
  var _sessionId = '';
  var _publishTimer = null;
  var _lastState = null;
  // ── Internal top-level nav (opt-in) — see registerNav() below ──────────
  var _navItems = null;      // [{id,label}] once a panel registers, else null
  var _navActiveId = '';
  var _navSelectFn = null;

  // ── A PANEL'S MENU AS THE CHAT'S (owner, 2026-09-27: "can each internal ui panel drop into its lhm menu like the chat").
  // The items a panel registered are published as a full LHM spec too (state.nav.lhm), so the harness draws the panel's
  // menu IN its LHM - a rail of icons, the open one's list beside it - exactly as it draws the chat's. A panel whose items
  // come in named groups (the Estate: Overview, Machines, ...) gets a rail icon per group and that group's items as the
  // list; a flat one gets a rail icon per item and the whole list beside it. Menu ids are '\u00a7g<n>' (a group) and
  // '\u00a7i<n>' (an item); a pick is '<menu>' or '<menu>/<item id>', resolved back to an item id by _navResolve.

  // ── NESTED PANELS (owner, 2026-09-27: "the comms and estate storage menus and any other deep LHMs needs fully absorbing
  // into the new chat/harness ui based unified LHM system"). A panel that shows other panels in frames - Comms (its
  // Calendar, Email, Telegram ...), the Estate (its Storage, its map) - hears the menu each child publishes (their
  // bridges post to this page, their parent). The child that is SHOWN has its sections listed under the item that shows
  // it (depth 1 in the docked menu), its current section lit; a pick on one is sent down to the child as its own
  // nav_select; while this menu is docked the child is told it is hosted, so its own sidebar folds away; and when this
  // page has no top bar of its own, the shown child's bar is offered up in its place. ──
  var _kids = [], _hostedUp = false, _hdrKid = null;
  // only a panel IN the harness nests: never the harness itself (it hosts every panel) nor the chat (its side panel is its own)
  function _nestOn(){ try{ return window.parent && window.parent !== window && !document.documentElement.hasAttribute('data-harness'); }catch(e){ return false; } }
  function _kidOf(win){
    for(var i = 0; i < _kids.length; i++) if(_kids[i].win === win) return _kids[i];
    var fr = null; try{ var fs = document.querySelectorAll('iframe'); for(var j = 0; j < fs.length; j++) if(fs[j].contentWindow === win){ fr = fs[j]; break; } }catch(e){}
    if(!fr) return null; var k = { win: win, frame: fr, nav: null, hdr: null }; _kids.push(k); return k;
  }
  function _kidShown(k){ try{ return !!(k.frame && k.frame.isConnected && (k.frame.offsetWidth || k.frame.offsetHeight)); }catch(e){ return false; } }
  function _activeKid(){ for(var i = _kids.length - 1; i >= 0; i--){ var k = _kids[i]; if(_kidShown(k) && k.nav && k.nav.items && k.nav.items.length) return k; } return null; }
  // ── A PAGE'S OWN SUB-SECTIONS (owner, 2026-09-28: "the estate ui's observe menu is missing the perf section"). A section
  // that switches between views in the page itself (Observe: Events / Perf) marks each tab data-vera-sub="<id>"; while its
  // strip is shown the docked menu lists them under the open item ('s:<id>'), the current one lit, and a pick clicks the tab.
  // A strip in a bar the harness absorbed still counts as shown (the bar is folded away because the harness holds it). ──
  function _subShown(el){ for(var n = el; n && n !== document.body; n = n.parentElement){ if(n.hidden) return false; var cs = getComputedStyle(n);
      if(cs.visibility === 'hidden') return false; if(cs.display === 'none' && !n.hasAttribute('data-vpb-hdr-bar')) return false; } return !!el; }
  function _subLabel(el){ var own = ''; Array.prototype.forEach.call(el.childNodes, function(c){ if(c.nodeType === 3) own += c.textContent; });
    return String(el.getAttribute('data-label') || own.trim() || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40); }
  function _pageSubs(){ try{
    var els = Array.prototype.slice.call(document.querySelectorAll('[data-vera-sub]')).filter(_subShown).slice(0, 24); if(!els.length) return null;
    var on = els.filter(function(el){ return /\b(active|on|selected)\b/.test(String(el.className || '')) || el.getAttribute('aria-selected') === 'true'; })[0];
    return { items: els.map(function(el){ return { id: String(el.getAttribute('data-vera-sub')), label: _subLabel(el), el: el }; }), active: on ? String(on.getAttribute('data-vera-sub')) : '' };
  }catch(e){ return null; } }
  function _kidsHosted(on){ _hostedUp = !!on; _kids.forEach(function(k){ try{ k.win.postMessage({ type: on ? 'vera:panel:nav_hosted' : 'vera:panel:nav_unhosted' }, '*'); }catch(e){} }); }
  window.addEventListener('message', function(ev){
    if(!_nestOn()) return;
    var d = ev.data; if(!d || typeof d !== 'object' || !ev.source || ev.source === window) return;
    try{ if(ev.source === window.parent) return; }catch(e){}
    if(d.type !== 'vera:panel:state' && d.type !== 'vera:hdr:offer') return;
    var k = _kidOf(ev.source); if(!k) return;
    if(d.type === 'vera:panel:state'){
      var nv = d.state && d.state.nav; k.nav = (nv && Array.isArray(nv.items)) ? { items: nv.items.slice(0, 60).map(function(it){ return { id: String(it.id), label: String(it.label || it.id).replace(/\s+/g, ' ').trim().slice(0, 48) }; }), active: String(nv.active || '') } : null;
      if(k.nav && nv.lhm && Array.isArray(nv.lhm.widgets)) k.nav.widgets = nv.lhm.widgets.slice(0, 8).map(String);   // the child's menu widgets ride up with its sections
      try{ ev.source.postMessage({ type: (_hostedUp || document.documentElement.classList.contains('vpb-nav-hosted')) ? 'vera:panel:nav_hosted' : 'vera:panel:nav_unhosted' }, '*'); }catch(e){}
      publishStateDebounced();
    } else { k.hdr = d; _hdrRelay(); }
  });
  // what is shown changes as this page switches its own item: look again, cheaply
  setInterval(function(){ if(!_nestOn()) return; var k = _activeKid(); var ps = (typeof _pageSubs === 'function') ? _pageSubs() : null; var sig = (k ? k.frame.getAttribute('src') + '|' + k.nav.active : '') + '|' + (ps ? ps.items.map(function(it){ return it.id; }).join(',') + '>' + ps.active : ''); if(sig !== _kidSig){ _kidSig = sig; publishStateDebounced(); _hdrRelay(); } }, 1000);
  var _kidSig = '';
  function _hdrRelay(){
    if(typeof _hdrBar !== 'undefined' && _hdrBar) return;   // this page has its own bar
    var k = null; for(var i = _kids.length - 1; i >= 0; i--){ if(_kidShown(_kids[i]) && _kids[i].hdr){ k = _kids[i]; break; } }
    var had = _hdrKid; _hdrKid = k;
    if(!k){ if(had){ try{ window.parent.postMessage({ type: 'vera:hdr:offer', title: document.title || '', groups: [] }, '*'); }catch(e){} } return; }
    try{ window.parent.postMessage({ type: 'vera:hdr:offer', title: k.hdr.title || document.title || '', groups: k.hdr.groups || [] }, '*'); }catch(e){}
  }
  // ── ANY PAGE'S MENU DOCKS (owner, 2026-09-27: "the research ui lhm needs integrating into the unified lhm and im sure
  // there are more"). A page marks its menu data-vera-lhm - whatever its markup, a sidebar, an icon rail, a strip of view
  // tabs - and the shell's nav code (/ui/vera-panel.js) publishes it; a page that does not load that code has it brought
  // here. Docked, the marked menu folds away, firmly: a page's own #nav{display:...} would otherwise outrank the rule. ──
  function _adoptLhm(){
    try{
      if(!document.querySelector('[data-vera-lhm]')) return;
      if(!document.getElementById('vpb-lhm-css')){ var st = document.createElement('style'); st.id = 'vpb-lhm-css';
        st.textContent = 'html.vpb-nav-hosted [data-vera-lhm]:not(.vp-has-content){display:none!important}';
        (document.head || document.documentElement).appendChild(st); }
      if(!window.veraPanel && !document.querySelector('script[src$="/ui/vera-panel.js"]')){ var s = document.createElement('script'); s.src = '/ui/vera-panel.js'; (document.head || document.documentElement).appendChild(s); }
    }catch(e){}
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _adoptLhm); else _adoptLhm();

  var _navLhmOpts = null;
  // ── A PANEL'S MENU WIDGETS (owner, 2026-09-27: "any lhm items that can be made into widgets ... like the calendar controls
  // and even the calendar from the comms ui itself - and the different parts of it like the schedule view"). A page names
  // the widgets its docked menu carries on any element: data-lhm-widgets="cal:controls cal:month@m" - template ids, @size
  // optional. They ride in the lhm spec (spec.widgets); a nesting page passes its shown child's up with the child's sections.
  function _navWidgets(){ try{ var el = document.querySelector('[data-lhm-widgets]'); if(!el) return [];
    return String(el.getAttribute('data-lhm-widgets') || '').split(/[\s,]+/).filter(function(s){ return /^[\w.:-]+(@(xs|s|m|l|xl))?$/.test(s); }).slice(0, 8); }catch(e){ return []; } }
  function _navIcon(it){ var t = String(it.icon || '').trim(); if(t) return t; return (String(it.label || it.id || '').trim().charAt(0) || '\u2022').toUpperCase(); }
  function _navLhm(){
    if(!_navItems || !_navItems.length || (_navLhmOpts && _navLhmOpts.lhm === false)) return null;
    // the panel's name: its page title carries the product's name first ("Vera — Estate"), which the menu's head does not need
    var title = String((_navLhmOpts && _navLhmOpts.title) || document.title || '').replace(/^\s*Vera\s*[\u2014\u2013\-\u00b7|:]\s*/i, '').trim();
    var groups = [], at = {};
    _navItems.forEach(function(it){ var g = it.group || ''; if(!(g in at)){ at[g] = groups.length; groups.push({ name: g, items: [] }); } groups[at[g]].items.push(it); });
    var act = { menu: '', tab: '' }, menus;
    if(groups.filter(function(g){ return g.name; }).length >= 2){
      menus = groups.map(function(g, gi){ var id = '\u00a7g' + gi; g.items.forEach(function(it){ if(it.id === _navActiveId){ act.menu = id; act.tab = it.id; } });
        return { id: id, icon: _navIcon(g.items[0]), label: g.name || 'More', title: g.name || 'More', tabs: g.items.map(function(it){ return { id: it.id, label: it.label }; }) }; });
    } else {
      var all = _navItems.map(function(it){ return { id: it.id, label: it.label }; });
      menus = _navItems.map(function(it, i){ var id = '\u00a7i' + i; if(it.id === _navActiveId){ act.menu = id; act.tab = it.id; }
        return { id: id, icon: _navIcon(it), label: it.label, title: title || 'Sections', tabs: all }; });
    }
    if(!act.menu && menus.length) act.menu = menus[0].id;
    var kid = (typeof _activeKid === 'function') ? _activeKid() : null, ps = (act.menu && typeof _pageSubs === 'function') ? _pageSubs() : null;
    if((kid || ps) && act.menu){
      // the page's own sub-sections, and a framed child's sections under the one that shows it (or under the item, as before)
      var kidSub = kid ? kid.nav.items.map(function(it){ return { id: 'c:' + it.id, label: String(it.label || it.id), depth: ps ? 2 : 1 }; }) : [], sub = kidSub;
      if(ps){ sub = []; ps.items.forEach(function(it){ sub.push({ id: 's:' + it.id, label: it.label, depth: 1 }); if(it.id === ps.active) sub = sub.concat(kidSub); }); if(!ps.active) sub = sub.concat(kidSub); }
      menus.forEach(function(m){ if(m.id !== act.menu) return; var at = -1; m.tabs.forEach(function(tb, i){ if(tb.id === _navActiveId) at = i; });
        m.tabs = m.tabs.slice(0, at + 1).concat(sub, m.tabs.slice(at + 1)); });
      if(kid && kid.nav.active) act.tab = 'c:' + kid.nav.active; else if(ps && ps.active) act.tab = 's:' + ps.active;
    }
    var wd = _navWidgets(); if(kid && kid.nav.widgets) kid.nav.widgets.forEach(function(w){ if(wd.indexOf(w) < 0) wd.push(w); });
    return { title: title, active: act, menus: menus, open: [], widgets: wd };
  }
  function _navResolve(id){
    var s = String(id);
    if(s.charAt(0) !== '\u00a7') return s;                          // an item id, as before
    var slash = s.indexOf('/'); if(slash > 0) return s.slice(slash + 1);   // '<menu>/<item>': the item
    var spec = _navLhm(); if(!spec) return s;
    var m = spec.menus.filter(function(x){ return x.id === s; })[0]; if(!m) return s;
    if(s.charAt(1) === 'i'){ var it = _navItems[+s.slice(2)]; return it ? it.id : s; }   // a flat menu's icon: its item
    if(m.tabs.some(function(t){ return t.id === _navActiveId; })) return _navActiveId;   // a group's icon: stay if already in it
    return m.tabs.length ? m.tabs[0].id : s;                                                // ... else its first item
  }

  // ── DOM helpers ───────────────────────────────────────────────────────
  function _elById(id){ return id ? document.getElementById(id) : null; }

  // Catalogue the panel's interactive controls so the agent knows what it
  // can click / fill — without the panel author wiring anything up.
  function _uiCatalog(){
    var out = {buttons: [], inputs: []};
    try{
      var seen = {};
      var btns = document.querySelectorAll('button, .btn, .tbtn, [role="button"]');
      Array.prototype.forEach.call(btns, function(b){
        if(b.offsetParent === null) return;                 // hidden
        var id = b.id || '';
        var label = (b.getAttribute('title') || b.textContent || b.value || '')
                      .trim().replace(/\s+/g, ' ').slice(0, 48);
        if(!id && !label) return;
        var key = id || label;
        if(seen[key]) return; seen[key] = 1;
        var entry = id ? {id: id, label: label} : {label: label};
        // Capture the leading onclick handler name so we can expose it as a
        // dispatchable, named action (e.g. "runQuickCycle", "startTraining").
        var oc = b.getAttribute('onclick') || '';
        var m = oc.match(/^\s*([a-zA-Z_$][\w$]*)\s*\(\s*(?:'([^']*)'|"([^"]*)")?/);
        if(m){
          entry.action = m[1];
          // Capture the first string-literal argument (e.g. fabSection('sources'))
          // so buttons sharing a handler can be disambiguated and dispatched
          // individually rather than all collapsing to the bare handler name.
          var a0 = (m[2] !== undefined) ? m[2] : m[3];
          if(a0) entry.arg = a0;
        }
        out.buttons.push(entry);
      });
      out.buttons = out.buttons.slice(0, 40);

      var ins = document.querySelectorAll('input, select, textarea');
      Array.prototype.forEach.call(ins, function(el){
        if(!el.id || el.type === 'hidden') return;
        if(el.offsetParent === null) return;
        var info = {id: el.id, type: (el.tagName === 'SELECT' ? 'select' : (el.type || 'text'))};
        var label = (el.getAttribute('placeholder') || el.getAttribute('aria-label') || el.name || '').slice(0, 48);
        if(label) info.label = label;
        if(el.type === 'checkbox'){ info.checked = !!el.checked; }
        else { var v = el.value || ''; if(v) info.value = String(v).slice(0, 60); }
        if(el.tagName === 'SELECT'){
          info.options = Array.prototype.slice.call(el.options, 0, 12).map(function(o){ return o.value; });
        }
        out.inputs.push(info);
      });
      out.inputs = out.inputs.slice(0, 50);
    }catch(e){}
    return out;
  }

  function _safeDOMState(){
    var st = {url: location.href, title: document.title, hash: location.hash};
    // Explicit opt-in only — see registerNav()'s own doc for why this isn't
    // auto-derived from generic "active" class detection the way st.active
    // above is: panels mark their current section with every class under the
    // sun (.on, .active, .selected, .current...), so a guess would either
    // miss real panels or misfire on unrelated "active" elements that have
    // nothing to do with top-level section nav.
    if(_navItems){ st.nav = {items: _navItems, active: _navActiveId}; var _lhm = _navLhm(); if(_lhm) st.nav.lhm = _lhm; }
    // a page whose menu is VeraLHM's (the chat) publishes that menu itself; this snapshot is a second state from the same
    // frame, and the host reads a state WITHOUT nav as "no menu" - so it carries the menu's own nav, or it drops it
    else { try{ var _own = (window.VeraLHM && typeof window.VeraLHM.navState === 'function') ? window.VeraLHM.navState() : null; if(_own) st.nav = _own; }catch(e){} }
    try{
      var focused = document.activeElement;
      if(focused && focused !== document.body && focused.id) st.focused_id = focused.id;
      var actives = document.querySelectorAll('.active, .on.rtab, .selected, [aria-selected="true"]');
      if(actives.length){
        st.active = Array.prototype.slice.call(actives, 0, 6).map(function(el){
          return (el.id || el.textContent || el.tagName).toString().slice(0, 80);
        });
      }
      var h = document.querySelector('h1, h2, .panel-title, [data-panel-title]');
      if(h && h.textContent) st.heading = h.textContent.trim().slice(0, 120);
    }catch(e){}
    var cat = _uiCatalog();
    st.ui = cat;
    // Auto-derive a named-action catalog from the panel's buttons so EVERY
    // panel exposes semantic actions (named after each button's handler, with
    // its label as the description) — dispatchable by name with no bespoke
    // wiring. Panels that register a custom provider (exec, netmap) overlay
    // their own curated panel_actions on top of this.
    var acts = {}, counts = {};
    cat.buttons.forEach(function(b){ if(b.action) counts[b.action] = (counts[b.action] || 0) + 1; });
    cat.buttons.forEach(function(b){
      if(!b.action) return;
      // When several buttons share a handler but differ by a string arg
      // (e.g. fabSection('datasets') vs fabSection('sources')), expose each as
      // a distinct "handler:arg" action so the agent can target the right one
      // instead of every dispatch collapsing onto the first button.
      var key = (counts[b.action] > 1 && b.arg) ? (b.action + ':' + b.arg) : b.action;
      if(!acts[key]) acts[key] = b.label || key;
    });
    if(Object.keys(acts).length) st.panel_actions = acts;
    return st;
  }

  // Click the button that corresponds to a named action — matched by its
  // onclick handler name, then id, then visible label. Clicking replays the
  // button's exact onclick (including any fixed args), so form-driven buttons
  // work after the agent fills inputs via set_field. Returns null if no match
  // so the caller can report the action as unhandled.
  // Locate the button matching a named action WITHOUT clicking it, so the caller
  // can scope a payload fill to the button's container before firing. Returns
  // {el, meta} or null. Matching order mirrors _triggerNamed: id → onclick
  // handler(:arg) → visible label.
  function _findNamed(name){
    if(!name) return null;
    var raw = String(name).trim(), byLabel = null;
    var wantFn = raw, wantArg = null;
    var mm = raw.match(/^([a-zA-Z_$][\w$]*)\s*(?::\s*(.+)|\(\s*['"]?([^'")]*)['"]?\s*\))$/);
    if(mm){ wantFn = mm[1]; wantArg = (mm[2] !== undefined) ? mm[2] : mm[3]; if(wantArg != null) wantArg = String(wantArg).trim(); }
    var btns = document.querySelectorAll('button, .btn, .tbtn, [role="button"]');
    for(var i = 0; i < btns.length; i++){
      var b = btns[i];
      if(b.offsetParent === null) continue;                 // skip hidden
      if(b.id === raw){ return {el: b, meta: {clicked: raw, by: 'id'}}; }
      var oc = b.getAttribute('onclick') || '';
      var m = oc.match(/^\s*([a-zA-Z_$][\w$]*)\s*\(\s*(?:'([^']*)'|"([^"]*)")?/);
      if(m && m[1] === wantFn){
        var a0 = (m[2] !== undefined) ? m[2] : m[3];
        if(wantArg == null || (a0 != null && a0 === wantArg)){
          return {el: b, meta: {clicked: wantFn, by: 'action', arg: (wantArg != null ? wantArg : (a0 || undefined))}};
        }
      }
      if(!byLabel){
        var lbl = (b.getAttribute('title') || b.textContent || b.value || '').trim();
        if(lbl && lbl.toLowerCase() === raw.toLowerCase()) byLabel = b;
      }
    }
    if(byLabel){ return {el: byLabel, meta: {clicked: raw, by: 'label'}}; }
    return null;
  }

  function _triggerNamed(name){
    var f = _findNamed(name);
    if(!f) return null;
    f.el.click();
    return f.meta;
  }

  // The nearest logical container of a control — a <form>, a section
  // (.sec/.section/.card/.pane/.tab-pane or an id like fsec-*), or the panel
  // root. Used to scope payload→input mapping so a one-shot dispatch on a busy
  // multi-section panel fills the RIGHT form, not a same-named input elsewhere.
  function _containerOf(el){
    var n = el;
    while(n && n !== document.body){
      if(n.tagName === 'FORM') return n;
      var cl = n.className && n.className.baseVal !== undefined ? n.className.baseVal : (n.className || '');
      cl = String(cl);
      if(/\b(sec|section|card|pane|panel|tab-pane|fqtab|fsec)\b/.test(cl)) return n;
      if(n.id && /^(fsec-|sec-|tab-|pane-)/.test(n.id)) return n;
      n = n.parentElement;
    }
    return null;
  }

  function _buildState(){
    var base = _safeDOMState();
    if(_stateProvider){
      try{
        var s = _stateProvider();
        if(s && typeof s === 'object') return Object.assign(base, s);  // custom overlays generic
      }catch(e){ base.provider_error = String(e); }
    }
    return base;
  }

  // ── Generic action handlers (work on ANY panel) ───────────────────────
  function _click(p){
    p = p || {};
    var el = _elById(p.id);
    if(!el && p.selector){ try{ el = document.querySelector(p.selector); }catch(e){} }
    if(!el && p.label){
      var want = String(p.label).toLowerCase();
      var cands = document.querySelectorAll('button, .btn, .tbtn, [role="button"]');
      for(var i = 0; i < cands.length; i++){
        var t = (cands[i].textContent || cands[i].title || cands[i].value || '').trim().toLowerCase();
        if(t && t.indexOf(want) >= 0){ el = cands[i]; break; }
      }
    }
    if(!el) return {ok: false, error: 'element not found: ' + (p.id || p.selector || p.label || '?')};
    el.click();
    return {clicked: (p.id || p.selector || p.label)};
  }

  function _setField(p){
    p = p || {};
    if(!p.id) return {ok: false, error: 'id required'};
    var el = _elById(p.id);
    if(!el) return {ok: false, error: 'field not found: ' + p.id};
    if(el.type === 'checkbox') el.checked = !!p.value;
    else el.value = String(p.value === undefined || p.value === null ? '' : p.value);
    try{ el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }catch(e){}
    return {set: p.id, value: (el.type === 'checkbox' ? el.checked : el.value)};
  }

  function _setFields(p){
    p = p || {}; var f = p.fields || {}; var done = [];
    for(var k in f){ if(!f.hasOwnProperty(k)) continue; _setField({id: k, value: f[k]}); done.push(k); }
    return {set: done};
  }

  function _submit(p){
    p = p || {}; var f = p.fields || {};
    for(var k in f){ if(f.hasOwnProperty(k)) _setField({id: k, value: f[k]}); }
    if(p.click || p.button) return _click({id: (p.click || p.button), label: p.label});
    return {set: Object.keys(f)};
  }

  // Best-effort: map a payload's keys onto the panel's VISIBLE inputs so a
  // one-shot named dispatch carrying a payload (e.g. runLocal {command:"…"})
  // fills the form before the button is clicked. Only applies a key when it
  // resolves to exactly one visible input (exact id, then synonym-aware
  // substring) — ambiguous keys are skipped rather than guessed. Returns the
  // ids that were set.
  function _applyPayloadToInputs(payload, scope){
    if(!payload || typeof payload !== 'object') return [];
    var SYN = {command:['cmd'], cmd:['command'], cwd:['dir','path','directory'],
               dir:['cwd'], path:['cwd'], timeout:['to'], to:['timeout'],
               host:['hostname'], url:['link','href'], query:['q','sql','text','keywords'],
               text:['query','q'], keywords:['query','q','text']};
    function norm(s){ return String(s).toLowerCase().replace(/[^a-z0-9]/g, ''); }
    // Scope the candidate inputs to a container when given (e.g. the section that
    // holds the button we're about to click) so a key like "query" maps to THIS
    // form's field, not a same-named input in a different section. Fall back to
    // the whole document when the scope holds no usable inputs.
    var root = (scope && scope.querySelectorAll) ? scope : document;
    var inputs = Array.prototype.filter.call(
      root.querySelectorAll('input, select, textarea'),
      function(el){ return el.id && el.type !== 'hidden' && el.offsetParent !== null; });
    if(!inputs.length && root !== document){
      inputs = Array.prototype.filter.call(
        document.querySelectorAll('input, select, textarea'),
        function(el){ return el.id && el.type !== 'hidden' && el.offsetParent !== null; });
    }
    var applied = [];
    Object.keys(payload).forEach(function(key){
      var val = payload[key];
      if(val === null || val === undefined || typeof val === 'object') return;
      var nkey = norm(key);
      var terms = [nkey].concat((SYN[key.toLowerCase()] || []).map(norm));
      var exact = null, hits = [];
      inputs.forEach(function(el){
        var nid = norm(el.id);
        if(nid === nkey){ exact = el; return; }
        for(var i = 0; i < terms.length; i++){
          if(terms[i] && (nid.indexOf(terms[i]) >= 0 || terms[i].indexOf(nid) >= 0)){ hits.push(el); break; }
        }
      });
      var target = exact || (hits.length === 1 ? hits[0] : null);
      if(!target) return;
      if(target.type === 'checkbox') target.checked = !!val;
      else target.value = String(val);
      try{ target.dispatchEvent(new Event('input', {bubbles: true})); target.dispatchEvent(new Event('change', {bubbles: true})); }catch(e){}
      applied.push(target.id);
    });
    return applied;
  }

  // Select a section by the id the panel gave registerNav(). Prefers the
  // panel's own select callback when it registered one; otherwise falls back
  // to clicking whatever element carries a matching data-sec/-section/-view/
  // -tab/-nav/-pane/-s attribute — the exact patterns this codebase's
  // existing hand-rolled section switchers already use (data-sec="test",
  // data-pane="workers" as workers_ollama_panel.html/
  // agents_skills_ontologies_panel.html both use, data-s="x" as
  // ui_builder_panel.html uses), so most panels need zero extra code beyond
  // the registerNav(items) call itself — and the canonical vera-panel.js
  // #sidebar[data-vera-lhm] shell doesn't even need that: it calls
  // registerNav()/setNavActive() generically for every panel using it.
  var _navPend = 0;
  function _navPick(p){
    var id = (p || {}).id;
    if(id == null) return {ok: false, error: 'nav_select requires {id}'};
    id = _navResolve(String(id));
    _navPend++;   // a newer pick cancels a chain still waiting
    var steps = id.split('>');
    if(steps.length > 1 && steps.slice(1).every(function(x){ return /^[cs]:/.test(x); })){
      var r0 = _navSelect({id: steps[0]});
      if(r0 && r0.ok) steps.shift();
      else if(!/^[cs]:/.test(steps[0])) return r0;   // an item that is not there: nothing below it can be either
      _navChain(steps, _navPend, 0); return {ok: true, pending: steps.length};
    }
    var r = _navSelect({id: id});
    if(r && r.ok === false && /^[cs]:/.test(id)){ _navChain([id], _navPend, 1); r.pending = 1; }
    return r;
  }
  function _navChain(steps, tok, n){
    if(!steps.length || tok !== _navPend) return;
    setTimeout(function(){
      if(tok !== _navPend) return;
      var r = _navSelect({id: steps[0]});
      if(r && r.ok){ _navChain(steps.slice(1), tok, 0); return; }
      if(n < 60) _navChain(steps, tok, n + 1);
    }, n ? 400 : 300);
  }
  /* one step: an item, a page sub-section (s:) or a shown child's section (c:) */
  function _navSelect(p){
    var id = (p || {}).id;
    if(id == null) return {ok: false, error: 'nav_select requires {id}'};
    id = _navResolve(String(id));
    if(id.indexOf('c:') === 0){ var kid = _activeKid(); if(!kid) return {ok: false, error: 'no nested panel is shown'};
      if(kid.nav && kid.nav.items && !kid.nav.items.some(function(it){ return it.id === id.slice(2); })) return {ok: false, error: 'the shown nested panel has no ' + id.slice(2)};
      try{ kid.win.postMessage({type: 'vera:panel:action', action: 'nav_select', action_id: 'nest-' + Date.now(), payload: {id: id.slice(2)}}, '*'); }catch(e){}
      kid.nav.active = id.slice(2); publishStateDebounced(); return {ok: true}; }
    if(id.indexOf('s:') === 0){ var ps = _pageSubs(), hit = ps && ps.items.filter(function(it){ return it.id === id.slice(2); })[0];
      if(!hit) return {ok: false, error: 'no sub-section ' + id.slice(2) + ' is shown'}; hit.el.click(); publishStateDebounced(); return {ok: true}; }
    if(_navSelectFn){
      try{ _navSelectFn(id); }catch(e){ return {ok: false, error: String(e)}; }
      _navActiveId = id; publishStateDebounced();
      return {ok: true};
    }
    // One selector list would leave the choice to document order, which picks
    // the WRONG element as soon as two attributes carry the same value:
    // workers_ollama_panel.html marks every Estate item `data-view="estate"`
    // (the view that OWNS the item) and the map pane `data-pane="estate"`, so
    // selecting the map matched the "Overview" section heading and clicking it
    // did nothing — the Map menu item was dead while every other one worked
    // (18 Sep 2026). So try the attributes one at a time, in order of how
    // specifically each names a target. `data-view` goes last: in this codebase
    // it usually groups items rather than naming one.
    var ATTRS = ['data-sec', 'data-section', 'data-tab', 'data-nav', 'data-pane',
                 'data-s', 'data-go', 'data-k', 'data-view'];
    var el = null;
    for(var ai = 0; ai < ATTRS.length && !el; ai++){
      el = document.querySelector('[' + ATTRS[ai] + '="' + id + '"]');
    }
    if(!el){ var found = _findNamed(id); el = found && found.el; }
    if(!el) return {ok: false, error: 'no nav target for id: ' + id};
    el.click();
    _navActiveId = id; publishStateDebounced();
    return {ok: true};
  }

  var _builtins = {
    click: _click, set_field: _setField, set_fields: _setFields, submit: _submit,
    nav_select: _navPick,
  };

  // ── Live cap-activity feed ────────────────────────────────────────────
  // The chat forwards every server-side cap the agent runs (call/ok/error,
  // scoped to this panel's cap groups) as vera:panel:cap_activity messages.
  // With zero panel code, a small overlay feed appears showing the agent's
  // actions live. Panels MAY additionally register native mirrors:
  //   VeraPanelBridge.registerCapActivityHandler('fabric.query', fn)
  //   VeraPanelBridge.registerCapActivityHandler('fabric.*', fn)   // group
  //   VeraPanelBridge.registerCapActivityHandler('*', fn)          // all
  // fn receives {phase:'call'|'ok'|'error', cap, group, trace_id, args?,
  // preview?, error?, elapsed_ms?} — e.g. re-run the query in the panel UI,
  // refresh a list after a mutation, or jump to the relevant section.
  var _capActivityHandlers = [];   // [{pattern, fn}]
  var _feedBox = null, _feedList = null, _feedCollapsed = false;
  var _feedRows = {};              // trace_id+cap → row el (call→ok/error resolution)

  function _feedEnsure(){
    if(_feedBox) return;
    var css = 'position:fixed;right:10px;bottom:10px;z-index:99999;max-width:360px;' +
              'font:11px/1.4 ui-monospace,Menlo,monospace;color:var(--text,#ddd);' +
              'background:var(--bg2,rgba(28,28,30,.96));border:1px solid var(--border,#444);' +
              'border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.35);overflow:hidden';
    _feedBox = document.createElement('div');
    _feedBox.id = 'vpbCapFeed';
    _feedBox.setAttribute('style', css);
    var hd = document.createElement('div');
    hd.setAttribute('style', 'display:flex;align-items:center;gap:6px;padding:5px 9px;' +
      'font-weight:700;font-size:10px;letter-spacing:.05em;cursor:pointer;' +
      'border-bottom:1px solid var(--border,#444);color:var(--acc,#5a9e8f)');
    hd.innerHTML = '<span>⚡ AGENT ACTIVITY</span>' +
      '<span style="flex:1"></span><span id="vpbCapFeedTgl" style="opacity:.7">–</span>';
    hd.onclick = function(){
      _feedCollapsed = !_feedCollapsed;
      _feedList.style.display = _feedCollapsed ? 'none' : '';
      var t = document.getElementById('vpbCapFeedTgl');
      if(t) t.textContent = _feedCollapsed ? '+' : '–';
    };
    _feedList = document.createElement('div');
    _feedList.setAttribute('style', 'display:flex;flex-direction:column;gap:2px;' +
      'padding:5px 8px;max-height:180px;overflow-y:auto');
    _feedBox.appendChild(hd); _feedBox.appendChild(_feedList);
    (document.body || document.documentElement).appendChild(_feedBox);
  }

  function _feedTrim(){
    while(_feedList && _feedList.children.length > 6){
      var last = _feedList.lastChild;
      for(var k in _feedRows){ if(_feedRows[k] === last) delete _feedRows[k]; }
      _feedList.removeChild(last);
    }
  }

  function _fmtArgs(args){
    if(!args || typeof args !== 'object') return '';
    var parts = [];
    for(var k in args){
      if(!args.hasOwnProperty(k)) continue;
      parts.push(k + '=' + String(args[k]).slice(0, 32));
      if(parts.length >= 3) break;
    }
    return parts.join(' ');
  }

  function _feedShow(a){
    try{
      _feedEnsure();
      var key = (a.trace_id || '') + '|' + (a.cap || '');
      var row = _feedRows[key];
      if(a.phase === 'call' || !row){
        row = document.createElement('div');
        row.setAttribute('style', 'display:flex;gap:6px;align-items:baseline;' +
          'white-space:nowrap;overflow:hidden;text-overflow:ellipsis');
        _feedList.insertBefore(row, _feedList.firstChild);
        _feedRows[key] = row;
        _feedTrim();
      }
      var icon = a.phase === 'ok' ? '<span style="color:var(--ok,#6db87a)">✓</span>'
               : a.phase === 'error' ? '<span style="color:var(--err,#c96b6b)">✗</span>'
               : '<span style="color:var(--warn,#c9a35a)">▸</span>';
      var tail = a.phase === 'ok' ? ((a.elapsed_ms != null ? (a.elapsed_ms/1000).toFixed(1)+'s ' : '') +
                                     String(a.preview || '').slice(0, 46))
               : a.phase === 'error' ? String(a.error || 'failed').slice(0, 60)
               : _fmtArgs(a.args);
      row.innerHTML = icon + ' <b>' + String(a.cap || '')
        .replace(/</g, '&lt;') + '</b> <span style="opacity:.65">' +
        String(tail).replace(/</g, '&lt;') + '</span>';
      // Rows fade out on their own so the feed disappears when the agent goes idle.
      if(row._vpbTimer) clearTimeout(row._vpbTimer);
      row._vpbTimer = setTimeout(function(){
        try{
          if(row.parentNode) row.parentNode.removeChild(row);
          delete _feedRows[key];
          if(_feedList && !_feedList.children.length && _feedBox){
            _feedBox.parentNode.removeChild(_feedBox);
            _feedBox = null; _feedList = null; _feedRows = {};
          }
        }catch(e){}
      }, 45000);
    }catch(e){}
  }

  var _capArgsMemo = {};   // trace_id|cap → args from the 'call' phase
  function _onCapActivity(a){
    if(!a || !a.cap) return;
    // Only the 'call' phase carries args; remember them so ok/error handlers
    // (which mirror completed calls) still see what the cap was invoked with.
    var memoKey = (a.trace_id || '') + '|' + a.cap;
    if(a.phase === 'call' && a.args){
      if(Object.keys(_capArgsMemo).length > 40) _capArgsMemo = {};
      _capArgsMemo[memoKey] = a.args;
    } else if(a.args == null && _capArgsMemo[memoKey]){
      a.args = _capArgsMemo[memoKey];
    }
    if(a.phase !== 'call') delete _capArgsMemo[memoKey];
    _feedShow(a);
    var grp = String(a.cap).split('.')[0];
    for(var i = 0; i < _capActivityHandlers.length; i++){
      var h = _capActivityHandlers[i];
      var p = h.pattern;
      var hit = (p === '*') || (p === a.cap) ||
                (p.slice(-2) === '.*' && p.slice(0, -2) === grp);
      if(!hit) continue;
      try{ h.fn(a); }catch(e){}
    }
  }

  // ── postMessage plumbing ──────────────────────────────────────────────
  function publishState(){
    var s = _buildState();
    try{ var sig = JSON.stringify(s); if(sig === _lastState) return; _lastState = sig; }catch(e){}
    try{ window.parent.postMessage({type: 'vera:panel:state', panel_id: _panelId, session_id: _sessionId, state: s}, '*'); }catch(e){}
  }
  // 40ms, not the 250ms this used to be — mainly coalesces bursts of rapid
  // DOM mutations (the click/change/input listeners below), not a
  // deliberate throttle; the injected-nav round trip (registerNav ->
  // publish -> host -> nav_hosted reply) was visibly slow to settle and
  // every ms here is on that critical path.
  function publishStateDebounced(){ if(_publishTimer) clearTimeout(_publishTimer); _publishTimer = setTimeout(publishState, 40); }
  function publishEvent(name, payload){
    try{ window.parent.postMessage({type: 'vera:panel:event', panel_id: _panelId, event: name, payload: payload || {}}, '*'); }catch(e){}
  }
  function publishActionResult(action_id, ok, result, error, action){
    if(!action_id) return;
    try{
      window.parent.postMessage({type: 'vera:panel:action_result', panel_id: _panelId, action_id: action_id,
        action: action || '', ok: !!ok, result: (result === undefined ? null : result), error: error || null}, '*');
    }catch(e){}
  }

  window.addEventListener('message', function(ev){
    var d = ev.data; if(!d || typeof d !== 'object') return;
    var t = d.type || '';
    if(t === 'vera:panel:init'){
      _panelId = d.panel_id || _panelId; _sessionId = d.session_id || _sessionId;
      // Force the next publish through even if the state BODY hasn't
      // changed since the last one. Panels that call registerNav()/
      // registerStateProvider() at script-parse time (before this init
      // ever arrives, since init only fires on the host's iframe 'load'
      // event, well after inline scripts already ran) publish once with
      // panel_id still '' — a host that key its cache by panel_id (as the
      // main-shell nav-injection listener does) correctly ignores that
      // unaddressed copy. publishState()'s own dedupe then sees the SAME
      // state body on this next call and silently drops it too, so the
      // correctly-tagged copy — the only one any panel_id-keyed listener
      // can actually use — never goes out at all without this reset.
      _lastState = null;
      publishState();
    } else if(t === 'vera:panel:nav_hosted'){
      // The host confirms it received our registerNav() items and is
      // rendering them itself (only sent back once it actually saw a
      // non-empty `nav` in our published state — see capability_
      // orchestration.html's _lhmNavSync wiring) — i.e. its own outer menu
      // now covers what this panel's internal rail was for. A panel opened
      // standalone (or in a host that hasn't adopted nav injection, like
      // today's chat side-rail) never receives this, so its own rail stays
      // visible there — this is never assumed, only confirmed by the host.
      document.documentElement.classList.add('vpb-nav-hosted');
      _kidsHosted(true);
    } else if(t === 'vera:panel:nav_unhosted'){
      // The inverse — the host's own top-level menu just stopped reliably
      // covering these sections (its "keep inner nav visible while the main
      // menu is auto-hiding" opt-in, or the host isn't hosting this panel's
      // nav at all right now), so un-hide this panel's own rail again.
      document.documentElement.classList.remove('vpb-nav-hosted');
      _kidsHosted(false);
    } else if(t === 'vera:panel:query'){
      // Explicit freshness ping — bypass the changed-state dedupe. Without
      // this, an idle panel whose state hasn't changed republishes NOTHING,
      // the chat's snapshot ages past its 5-minute staleness guard, and
      // panel.query starts returning "no panel mounted" while the panel is
      // visibly on screen.
      _lastState = null;
      publishState();
    } else if(t === 'vera:panel:cap_activity'){
      _onCapActivity(d.activity || {});
    } else if(t === 'vera:panel:action'){
      var act = String(d.action || ''); var aid = d.action_id || ''; var payload = d.payload || {};
      if(act === '__query__'){ publishActionResult(aid, true, _buildState(), null, act); publishStateDebounced(); return; }
      // Generic introspection: return the full control catalog + action list so
      // an agent can discover what a bespoke-less panel can do before driving it.
      if(act === 'describe' || act === '__describe__'){
        var s = _buildState();
        publishActionResult(aid, true, {ui: s.ui, panel_actions: s.panel_actions || {}, active: s.active, heading: s.heading}, null, act);
        return;
      }
      var h = _actionHandlers[act] || _builtins[act] || _actionHandlers['*'];
      if(!h){
        // Auto fallback — treat `act` as a named button (onclick handler name,
        // id, label, or "handler:arg"). Locate it FIRST, then apply any payload
        // to inputs scoped to that button's container so the one-shot dispatch
        // fills the right form before clicking — this gives every panel
        // semantic, named actions without bespoke wiring.
        var found = _findNamed(act);
        if(found){
          var applied = _applyPayloadToInputs(payload, _containerOf(found.el));
          found.el.click();
          if(applied && applied.length) found.meta.set_fields = applied;
          publishActionResult(aid, true, found.meta, null, act); publishStateDebounced(); return;
        }
        publishEvent('action_unhandled', {action: act}); publishActionResult(aid, false, null, 'no handler for action: ' + act, act); return;
      }
      var ret;
      try{ ret = h(payload, act); }
      catch(e){ publishEvent('action_error', {action: act, error: String(e)}); publishActionResult(aid, false, null, String(e), act); return; }
      if(ret && typeof ret.then === 'function'){
        ret.then(function(v){ publishActionResult(aid, true, v === undefined ? null : v, null, act); publishStateDebounced(); },
                 function(e){ publishActionResult(aid, false, null, String(e), act); });
      } else { publishActionResult(aid, true, ret === undefined ? null : ret, null, act); publishStateDebounced(); }
    }
  });

  ['click', 'change', 'input'].forEach(function(t){ document.addEventListener(t, publishStateDebounced, {passive: true, capture: true}); });
  setInterval(publishStateDebounced, 30000);


  // ── THE PANEL'S TOP BAR, IN THE HARNESS'S (owner, 2026-09-27: "i need all ui panels top bars to absorb into the harness
  // top bar like the chat ui does"). The chat offers its own bar; every other panel's is found and offered here, over the
  // same protocol: its controls go up as proxies (vera:hdr:offer), the harness says when it holds them (vera:hdr:absorbed)
  // and the bar folds away, and a press on a proxy is a press on the panel's own control (vera:hdr:act). A panel names its
  // bar with data-vera-topbar (="keep" keeps it in the panel); otherwise the usual names count only when the element IS the
  // page's top bar - at the top, across most of the width, holding controls - so a toolbar inside a pane is never taken.
  /* the names a page's top bar goes by - a sweep of 58 panels found Stack Monitor's .pane-tb, Research's #toolbar, Perf's
     and the Gallery's .bar, a header inside the page's wrapper - still only at the top, across the page, holding controls */
  var _HDR_SEL = '#topbar, #topBar, .topbar, .top-bar, .panel-topbar, body > header, .hdr, .header, .tb, .pane-tb, #tb, #toolbar, .toolbar, .bar, header, .page-head, .panel-head';
  var _hdrBar = null, _hdrSig = '', _hdrT = null, _hdrHid = 0, _hdrMO = null;
  /* NOTHING IN THE BAR IS LOST (owner, 2026-09-28: "the issue that happened with the perf ui... have any other parts of
     uis been swallowed/hidden by mistake?"). The bar folds away while the harness holds it, so whatever it holds that is
     not offered is gone: the Estate's Observe tabs (<span class="j-tab" onclick>) and the Notebook's logo link were. What
     is offered is every CLICKABLE thing in the bar - a control, a link, a tab (role=tab, data-vera-sub), anything with an
     onclick or a tabindex, and anything the page draws with a pointer cursor (a click bound in script) - never a part of
     one (a wrapper that holds a control gives way to the control; what sits inside a control is the control). */
  var _HDR_CTL = 'button, select, input, textarea, a[href], [onclick], [role=tab], [role=button], [role=link], [role=menuitem], [role=switch], [role=checkbox], [role=radio], [role=option], [data-vera-sub], summary, [tabindex]:not([tabindex="-1"])';
  function _hdrNative(el){ return /^(BUTTON|SELECT|INPUT|TEXTAREA)$/.test(el.tagName); }
  function _hdrCtls(bar){
    var out = [];
    Array.prototype.forEach.call(bar.querySelectorAll('*'), function(el){
      var tag = el.tagName; if(tag === 'OPTION' || tag === 'OPTGROUP' || tag === 'SCRIPT' || tag === 'STYLE') return;
      if(tag === 'INPUT' && String(el.type || '').toLowerCase() === 'hidden') return;
      var hit = false; try{ hit = el.matches(_HDR_CTL); }catch(e){}
      if(!hit){ try{ hit = getComputedStyle(el).cursor === 'pointer' && (!el.parentElement || getComputedStyle(el.parentElement).cursor !== 'pointer'); }catch(e){} }
      if(!hit) return;
      for(var n = el.parentElement; n && n !== bar; n = n.parentElement){ if(_hdrNative(n) || (n.tagName === 'A' && n.hasAttribute('href'))) return; }
      out.push(el);
    });
    out = out.filter(function(el){ if(_hdrNative(el)) return true; for(var i = 0; i < out.length; i++){ if(out[i] !== el && el.contains(out[i])) return false; } return true; });
    return out.filter(function(el){ return _hdrShownIn(el, bar); });
  }
  function _hdrOn(el){ return /\b(on|active|selected|current)\b/.test(String(el.className && el.className.baseVal != null ? el.className.baseVal : el.className || '')) || el.getAttribute('aria-pressed') === 'true' || el.getAttribute('aria-selected') === 'true' || (el.getAttribute('aria-current') || 'false') !== 'false'; }
  function _hdrLabel(el, t){ return (_hdrText(el) || t || el.getAttribute('data-label') || '\u00b7').slice(0, 24); }
  /* the bar SHOWN: a page of several panes keeps one bar per pane, and the absorbed one must be the shown pane's. Its own
     display does not count (it is folded away while the harness holds it); its pane's does. */
  function _hdrBarShown(b){
    if(!b || !document.contains(b) || b.hidden) return false;
    for(var n = b.parentElement; n && n !== document.documentElement; n = n.parentElement){ if(n.hidden) return false; var cs = getComputedStyle(n); if(cs.display === 'none' || cs.visibility === 'hidden') return false; }
    return true;
  }
  function _hdrDrop(){ if(_hdrBar){ try{ _hdrBar.removeAttribute('data-vpb-hdr-bar'); }catch(e){} } if(_hdrMO){ try{ _hdrMO.disconnect(); }catch(e){} _hdrMO = null; } _hdrBar = null; _hdrSig = ''; }
  function _hdrCheck(){
    if(_hdrBar && _hdrBarShown(_hdrBar)){ _hdrOffer(); return; }
    var had = !!_hdrBar; _hdrDrop(); _hdrFind();
    if(!_hdrBar && had){ try{ window.parent.postMessage({ type: 'vera:hdr:offer', title: document.title || '', groups: [] }, '*'); }catch(e){} _hdrKid = null; if(typeof _hdrRelay === 'function') _hdrRelay(); }
  }
  function _hdrEmbedded(){ try{ return !!(window.parent && window.parent !== window); }catch(e){ return false; } }
  function _hdrFindBar(){
    var named = document.querySelector('[data-vera-topbar]');
    if(named) return named.getAttribute('data-vera-topbar') === 'keep' ? null : named;
    var c = document.querySelectorAll(_HDR_SEL), W = window.innerWidth || document.documentElement.clientWidth || 0;
    for(var i = 0; i < c.length; i++){
      var r = c[i].getBoundingClientRect();
      if(r.width < 1 || r.top > 90 || r.height < 18 || r.height > 96 || r.width < W * 0.4) continue;
      if(!c[i].querySelector('button, select, input')) continue;
      return c[i];
    }
    return null;
  }
  // shown by its OWN rules: the bar itself is folded away while the harness holds it, and that must not count
  function _hdrShownIn(el, bar){
    for(var n = el; n && n !== bar.parentNode; n = n.parentElement){
      if(n.hidden) return false; var cs = getComputedStyle(n);
      if(n !== bar && cs.display === 'none') return false; if(cs.visibility === 'hidden') return false;
    }
    return true;
  }
  function _hdrText(el){ return String((el && el.textContent) || '').replace(/\s+/g, ' ').trim(); }
  function _hdrItems(bar){
    var groups = [], gi = 0;
    var ttl = bar.querySelector('.ttl, .title, #sec-title, .panel-title, h1, h2, h3');
    var all = _hdrCtls(bar);
    /* a title that is itself clickable is offered as what it is (a link, a tab) - not twice */
    if(ttl && _hdrText(ttl) && !all.some(function(el){ return el === ttl || el.contains(ttl); })) groups.push({ grp: 'title', prio: 1, items: [{ hid: 'title', kind: 'text', label: _hdrText(ttl).slice(0, 60), title: _hdrText(ttl).slice(0, 160) }] });
    Array.prototype.forEach.call(bar.children, function(ch){
      var ctl = all.filter(function(el){ return el === ch || ch.contains(el); });
      if(!ctl.length) return;
      gi++;
      var g = { grp: 'g' + gi, prio: 2 + Math.min(gi, 7), title: String(ch.title || '').slice(0, 80), items: [] };
      ctl.slice(0, 40).forEach(function(el){
        var id = el.getAttribute('data-vpb-hid'); if(!id){ id = 'p' + (++_hdrHid); el.setAttribute('data-vpb-hid', id); }
        var tag = el.tagName, ty = String(el.type || '').toLowerCase(), t = String(el.title || el.getAttribute('aria-label') || '').slice(0, 160);
        if(tag === 'SELECT') g.items.push({ hid: id, kind: 'select', value: el.value, title: t, options: Array.prototype.slice.call(el.options, 0, 80).map(function(o){ return [o.value, _hdrText(o).slice(0, 40)]; }) });
        else if(tag === 'INPUT' && (ty === 'checkbox' || ty === 'radio')){ var lb = el.closest('label'); g.items.push({ hid: id, kind: 'btn', label: (_hdrText(lb) || el.name || t || 'toggle').slice(0, 24), title: t, on: !!el.checked }); }
        else if(tag === 'INPUT' && /^(|text|search|number|url|email|tel|password)$/.test(ty)) g.items.push({ hid: id, kind: 'input', type: ty === 'number' ? 'number' : 'search', value: String(el.value || '').slice(0, 200), placeholder: String(el.placeholder || t || '').slice(0, 60), title: t });
        else if(tag === 'TEXTAREA') g.items.push({ hid: id, kind: 'input', type: 'search', value: String(el.value || '').slice(0, 200), placeholder: String(el.placeholder || t || '').slice(0, 60), title: t });
        /* a date, a time, a colour, a slider: the same kind of input in the harness's bar */
        else if(tag === 'INPUT' && /^(date|time|datetime-local|month|week|color|range)$/.test(ty)) g.items.push({ hid: id, kind: 'input', type: ty, value: String(el.value || '').slice(0, 200), min: el.min || '', max: el.max || '', step: el.step || '', placeholder: '', title: t || ty });
        else if(tag === 'INPUT') g.items.push({ hid: id, kind: 'btn', label: (String(el.value || '') || t || ty || '\u00b7').slice(0, 24), title: t });   /* submit, reset, button, file */
        else if(tag === 'BUTTON') g.items.push({ hid: id, kind: 'btn', label: _hdrLabel(el, t), title: t, on: _hdrOn(el) });
        /* everything else that is clicked: a tab (lit when it is the current one), a link, an element with a click of its own */
        else { var tab = el.matches('[role=tab], [data-vera-sub]') || /(^|[\s_-])(tab|j-tab|seg|chip)([\s_-]|$)/i.test(String(el.className && el.className.baseVal != null ? el.className.baseVal : el.className || ''));
          var lnk = tag === 'A' && el.hasAttribute('href');
          g.items.push({ hid: id, kind: 'btn', label: _hdrLabel(el, t), title: t || (lnk ? String(el.getAttribute('href') || '').slice(0, 160) : ''), on: _hdrOn(el), tab: !!tab, link: !!lnk }); }
      });
      if(g.items.length) groups.push(g);
    });
    return groups;
  }
  function _hdrOffer(force){
    if(!_hdrBar || !document.contains(_hdrBar)) return;
    var groups = _hdrItems(_hdrBar), sig = JSON.stringify(groups);
    if(!force && sig === _hdrSig) return; _hdrSig = sig;
    try{ window.parent.postMessage({ type: 'vera:hdr:offer', title: document.title || '', groups: groups }, '*'); }catch(e){}
  }
  function _hdrSoon(){ if(_hdrT) return; _hdrT = setTimeout(function(){ _hdrT = null; _hdrOffer(); }, 160); }
  function _hdrFind(){
    if(_hdrBar && document.contains(_hdrBar)) return;
    var b = _hdrFindBar(); if(!b) return;
    _hdrBar = b; b.setAttribute('data-vpb-hdr-bar', '');
    try{ _hdrMO = new MutationObserver(_hdrSoon); _hdrMO.observe(b, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['class', 'style', 'hidden', 'title', 'disabled', 'aria-selected', 'aria-pressed', 'aria-current', 'href'] }); }catch(e){}
    b.addEventListener('change', _hdrSoon, true); b.addEventListener('input', _hdrSoon, true);
    _hdrOffer(true);
  }
  function _hdrStart(){
    // the chat speaks for its own bar (it marks itself data-harness); the harness itself is never embedded
    if(!_hdrEmbedded() || document.documentElement.hasAttribute('data-harness')) return;
    try{ var st = document.createElement('style'); st.textContent = 'html.vpb-hdr-absorbed [data-vpb-hdr-bar]{display:none!important}'; (document.head || document.documentElement).appendChild(st); }catch(e){}
    window.addEventListener('message', function(ev){
      var d = ev.data; if(!d || typeof d !== 'object' || ev.source !== window.parent) return;
      if(!_hdrBar && _hdrKid && (d.type === 'vera:hdr:absorbed' || d.type === 'vera:hdr:act')){ try{ _hdrKid.win.postMessage(d, '*'); }catch(e){} return; }
      if(d.type === 'vera:hdr:absorbed'){ document.documentElement.classList.toggle('vpb-hdr-absorbed', !!d.on); if(d.on) _hdrOffer(true); return; }
      if(d.type !== 'vera:hdr:act' || !_hdrBar) return;
      var el = _hdrBar.querySelector('[data-vpb-hid="' + String(d.hid || '').replace(/["\\]/g, '') + '"]'); if(!el) return;
      var v = d.value;
      if(el.tagName === 'SELECT'){ el.value = String(v == null ? '' : v); el.dispatchEvent(new Event('change', { bubbles: true })); }
      else if(el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) el.click();
      else if(el.tagName === 'INPUT'){
        var val = (v && typeof v === 'object') ? v.value : v; el.value = String(val == null ? '' : val);
        el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
        if(v && typeof v === 'object' && v.enter) ['keydown', 'keypress', 'keyup'].forEach(function(k){ el.dispatchEvent(new KeyboardEvent(k, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true })); });
      }
      else el.click();
      _hdrSoon();
    });
    // a panel in a tab not yet shown has no size to measure: look again until its bar is found, then keep it current
    _hdrFind();
    setInterval(_hdrCheck, 2500);
    var _hdrCT = null; document.addEventListener('click', function(){ clearTimeout(_hdrCT); _hdrCT = setTimeout(_hdrCheck, 220); }, true);
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _hdrStart); else setTimeout(_hdrStart, 0);

  window.VeraPanelBridge = {
    registerStateProvider: function(fn){ _stateProvider = fn; publishStateDebounced(); },
    registerActionHandler: function(name, fn){ _actionHandlers[String(name)] = fn; },
    registerCapActivityHandler: function(pattern, fn){
      if(pattern && typeof fn === 'function')
        _capActivityHandlers.push({pattern: String(pattern), fn: fn});
    },
    publishState: publishState, publishStateDebounced: publishStateDebounced,
    publishEvent: publishEvent, publishActionResult: publishActionResult,
    panelId: function(){ return _panelId; }, sessionId: function(){ return _sessionId; },

    // ── Top-level nav unification ──────────────────────────────────────
    // A panel that has its own internal top-level menu of sections (a "Test
    // / Improve / Watch / ..." style row, whatever the panel's own idiom is)
    // opts in with ONE call:
    //   VeraPanelBridge.registerNav([{id:'test',label:'Test'}, ...]);
    // The outer shell (whichever page mounted this panel — the chat side-
    // rail or a top-level auto-tab) can then inject these as real sub-menu
    // items in ITS OWN top-level menu instead of the panel drawing a second,
    // redundant nav next to the outer one. Selecting an injected item posts
    // the generic 'nav_select' action back here (see _navSelect above),
    // which — with no further panel code — clicks whatever element carries
    // a matching data-sec/-section/-view/-tab/-nav attribute. Pass a second
    // `selectFn` argument only if a panel's switcher doesn't use one of
    // those attributes; call setNavActive(id) whenever the panel's OWN UI
    // changes section on its own (a direct click, a hash change, ...) so the
    // outer shell's injected menu highlights the right item even when the
    // switch didn't originate from an injected click.
    registerNav: function(items, selectFn){
      _navItems = (items || []).map(function(it){
        return {id: String(it.id), label: String(it.label || it.id).replace(/\s+/g, ' ').trim().slice(0, 48), group: it.group ? String(it.group) : '', icon: it.icon ? String(it.icon) : ''};
      });
      if(typeof selectFn === 'function') _navSelectFn = selectFn;
      publishStateDebounced();
    },
    setNavActive: function(id){ _navActiveId = String(id || ''); publishStateDebounced(); },
  };

  if(document.readyState === 'complete' || document.readyState === 'interactive'){ setTimeout(publishStateDebounced, 100); }
  else { document.addEventListener('DOMContentLoaded', function(){ setTimeout(publishStateDebounced, 100); }); }
})();

/* Vera: load the select-anywhere -> thermal print helper (isolated, best-effort) */
/* Vera: the right-click menu on every panel - the registry and its runtime (isolated, best-effort; a page with its own
   menu sets window.__veraRcmOwn and the runtime stands aside) */
try{ (function(){ if(window.__veraRcmLoad) return; window.__veraRcmLoad = 1;
  var h = document.head || document.documentElement;
  if(!window.MENUS){ var m = document.createElement('script'); m.src = '/ui/menus.js'; m.async = false; h.appendChild(m); }
  if(!window.VeraRCM){ var r = document.createElement('script'); r.src = '/ui/rcm.js'; r.async = false; h.appendChild(r); }
})(); }catch(e){}
try{ (function(){ if(window.__veraPrintSelLoad) return; window.__veraPrintSelLoad = 1;
  var s = document.createElement('script'); s.src = '/ui/vera-print-selection.js'; s.async = true;
  (document.head || document.documentElement).appendChild(s);
})(); }catch(e){}
