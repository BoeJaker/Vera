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
  function _triggerNamed(name){
    if(!name) return null;
    var raw = String(name).trim(), byLabel = null;
    // Accept disambiguated forms so a specific parameterised button can be
    // targeted:  "handler:arg"  or  "handler('arg')" / "handler(arg)".
    var wantFn = raw, wantArg = null;
    var mm = raw.match(/^([a-zA-Z_$][\w$]*)\s*(?::\s*(.+)|\(\s*['"]?([^'")]*)['"]?\s*\))$/);
    if(mm){ wantFn = mm[1]; wantArg = (mm[2] !== undefined) ? mm[2] : mm[3]; if(wantArg != null) wantArg = String(wantArg).trim(); }
    var btns = document.querySelectorAll('button, .btn, .tbtn, [role="button"]');
    for(var i = 0; i < btns.length; i++){
      var b = btns[i];
      if(b.offsetParent === null) continue;                 // skip hidden
      if(b.id === raw){ b.click(); return {clicked: raw, by: 'id'}; }
      var oc = b.getAttribute('onclick') || '';
      var m = oc.match(/^\s*([a-zA-Z_$][\w$]*)\s*\(\s*(?:'([^']*)'|"([^"]*)")?/);
      if(m && m[1] === wantFn){
        var a0 = (m[2] !== undefined) ? m[2] : m[3];
        // Bare handler name matches the first button; a requested arg must match
        // that button's first string literal (so fabSection:sources is exact).
        if(wantArg == null || (a0 != null && a0 === wantArg)){
          b.click();
          return {clicked: wantFn, by: 'action', arg: (wantArg != null ? wantArg : (a0 || undefined))};
        }
      }
      if(!byLabel){
        var lbl = (b.getAttribute('title') || b.textContent || b.value || '').trim();
        if(lbl && lbl.toLowerCase() === raw.toLowerCase()) byLabel = b;
      }
    }
    if(byLabel){ byLabel.click(); return {clicked: raw, by: 'label'}; }
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
  function _applyPayloadToInputs(payload){
    if(!payload || typeof payload !== 'object') return [];
    var SYN = {command:['cmd'], cmd:['command'], cwd:['dir','path','directory'],
               dir:['cwd'], path:['cwd'], timeout:['to'], to:['timeout'],
               host:['hostname'], url:['link','href'], query:['q','sql']};
    function norm(s){ return String(s).toLowerCase().replace(/[^a-z0-9]/g, ''); }
    var inputs = Array.prototype.filter.call(
      document.querySelectorAll('input, select, textarea'),
      function(el){ return el.id && el.type !== 'hidden' && el.offsetParent !== null; });
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

  var _builtins = {
    click: _click, set_field: _setField, set_fields: _setFields, submit: _submit,
  };

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
      setTimeout(publishState, 50);
    } else if(t === 'vera:panel:query'){
      publishState();
    } else if(t === 'vera:panel:action'){
      var act = String(d.action || ''); var aid = d.action_id || ''; var payload = d.payload || {};
      if(act === '__query__'){ publishActionResult(aid, true, _buildState(), null, act); publishStateDebounced(); return; }
      var h = _actionHandlers[act] || _builtins[act] || _actionHandlers['*'];
      if(!h){
        // Auto fallback — treat `act` as a named button (onclick handler name,
        // id, label, or "handler:arg") and click it. First apply any payload to
        // the panel's visible inputs so a one-shot dispatch carrying a payload
        // (e.g. runLocal {command:"whoami"}) fills the field before clicking —
        // this gives every panel semantic, named actions without bespoke wiring.
        var applied = _applyPayloadToInputs(payload);
        var nres = _triggerNamed(act);
        if(nres){ if(applied && applied.length) nres.set_fields = applied; publishActionResult(aid, true, nres, null, act); publishStateDebounced(); return; }
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
    publishState: publishState, publishStateDebounced: publishStateDebounced,
    publishEvent: publishEvent, publishActionResult: publishActionResult,
    panelId: function(){ return _panelId; }, sessionId: function(){ return _sessionId; },
  };

  if(document.readyState === 'complete' || document.readyState === 'interactive'){ setTimeout(publishStateDebounced, 100); }
  else { document.addEventListener('DOMContentLoaded', function(){ setTimeout(publishStateDebounced, 100); }); }
})();
