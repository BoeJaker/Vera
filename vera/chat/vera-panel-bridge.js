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
 *   a `vpb-nav-hosted` class on <html> — a panel's OWN stylesheet uses that
 *   to hide its now-redundant internal rail, e.g.:
 *     html.vpb-nav-hosted #secNav{display:none}
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
    if(_navItems) st.nav = {items: _navItems, active: _navActiveId};
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
  // -tab/-nav/-pane attribute — the exact patterns this codebase's existing
  // hand-rolled section switchers already use (data-sec="test", or
  // data-pane="workers" as workers_ollama_panel.html/
  // agents_skills_ontologies_panel.html both use), so most panels need zero
  // extra code beyond the registerNav(items) call itself.
  function _navSelect(p){
    var id = (p || {}).id;
    if(id == null) return {ok: false, error: 'nav_select requires {id}'};
    id = String(id);
    if(_navSelectFn){
      try{ _navSelectFn(id); }catch(e){ return {ok: false, error: String(e)}; }
      _navActiveId = id; publishStateDebounced();
      return {ok: true};
    }
    var el = document.querySelector(
      '[data-sec="' + id + '"], [data-section="' + id + '"], [data-view="' + id + '"], ' +
      '[data-tab="' + id + '"], [data-nav="' + id + '"], [data-pane="' + id + '"]');
    if(!el){ var found = _findNamed(id); el = found && found.el; }
    if(!el) return {ok: false, error: 'no nav target for id: ' + id};
    el.click();
    _navActiveId = id; publishStateDebounced();
    return {ok: true};
  }

  var _builtins = {
    click: _click, set_field: _setField, set_fields: _setFields, submit: _submit,
    nav_select: _navSelect,
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
  function publishStateDebounced(){ if(_publishTimer) clearTimeout(_publishTimer); _publishTimer = setTimeout(publishState, 250); }
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
      setTimeout(publishState, 50);
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
        return {id: String(it.id), label: String(it.label || it.id)};
      });
      if(typeof selectFn === 'function') _navSelectFn = selectFn;
      publishStateDebounced();
    },
    setNavActive: function(id){ _navActiveId = String(id || ''); publishStateDebounced(); },
  };

  if(document.readyState === 'complete' || document.readyState === 'interactive'){ setTimeout(publishStateDebounced, 100); }
  else { document.addEventListener('DOMContentLoaded', function(){ setTimeout(publishStateDebounced, 100); }); }
})();
