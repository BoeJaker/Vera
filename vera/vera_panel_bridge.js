/* vera-panel-bridge.js
 * ============================================================
 * Tiny shim panels can include to participate in the chat ↔
 * panel postMessage protocol introduced in chat_panel.html.
 *
 * Drop this in a panel HTML file with:
 *   <script src="/ui/vera-panel-bridge.js"></script>
 *
 * Then either let the default behaviour run (it scans the DOM
 * and emits a generic state snapshot) or register your own:
 *
 *   window.VeraPanelBridge.registerStateProvider(() => ({
 *     selected_id: currentSelection,
 *     filter:      document.getElementById('search').value,
 *     row_count:   visibleRows.length,
 *   }));
 *
 *   window.VeraPanelBridge.registerActionHandler('select', (payload) => {
 *     selectRow(payload.id);
 *     return { selected: payload.id };   // returned to caller via the bridge
 *   });
 *
 *   // Async handlers also work — return a Promise:
 *   window.VeraPanelBridge.registerActionHandler('search', async (p) => {
 *     const r = await fetch('/api/search?q='+encodeURIComponent(p.q));
 *     return await r.json();
 *   });
 *
 * Server-side agents reach this through the panel.dispatch capability
 * (action + payload → handler return value). The shim takes care of
 * tagging the reply with the dispatcher's action_id so the chat can
 * route it back to the awaiting cap.
 *
 * The shim publishes state on a debounce so rapid changes don't
 * spam the chat with messages.
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

  function _safeDOMState(){
    // Generic fallback for panels that don't register a provider:
    // grab visible text near focused/selected elements + tab state.
    var st = {
      url:   location.href,
      title: document.title,
      hash:  location.hash,
    };
    try{
      var focused = document.activeElement;
      if(focused && focused !== document.body && focused.id){
        st.focused_id = focused.id;
      }
      // Find anything with .active / .on / .selected so we can hint at
      // what the user is looking at without the panel author having to
      // wire anything up.
      var actives = document.querySelectorAll('.active, .on.rtab, .selected, [aria-selected="true"]');
      if(actives.length){
        st.active = Array.prototype.slice.call(actives, 0, 6).map(function(el){
          return (el.id || el.textContent || el.tagName).toString().slice(0, 80);
        });
      }
      // Title-bar / heading clues
      var h = document.querySelector('h1, h2, .panel-title, [data-panel-title]');
      if(h && h.textContent) st.heading = h.textContent.trim().slice(0, 120);
    }catch(e){}
    return st;
  }

  function _buildState(){
    if(_stateProvider){
      try{
        var s = _stateProvider();
        return (s && typeof s === 'object') ? s : {};
      }catch(e){
        return {error: String(e)};
      }
    }
    return _safeDOMState();
  }

  function publishState(){
    var s = _buildState();
    // Skip if nothing changed (cheap stringify diff)
    try{
      var sig = JSON.stringify(s);
      if(sig === _lastState) return;
      _lastState = sig;
    }catch(e){}
    try{
      window.parent.postMessage({
        type:      'vera:panel:state',
        panel_id:  _panelId,
        session_id:_sessionId,
        state:     s,
      }, '*');
    }catch(e){}
  }

  function publishStateDebounced(){
    if(_publishTimer) clearTimeout(_publishTimer);
    _publishTimer = setTimeout(publishState, 250);
  }

  function publishEvent(name, payload){
    try{
      window.parent.postMessage({
        type:     'vera:panel:event',
        panel_id: _panelId,
        event:    name,
        payload:  payload || {},
      }, '*');
    }catch(e){}
  }

  // Reply to a server-dispatched action. action_id correlates the
  // request on the chat side; without it, the reply is dropped.
  function publishActionResult(action_id, ok, result, error, action){
    if(!action_id) return;
    try{
      window.parent.postMessage({
        type:      'vera:panel:action_result',
        panel_id:  _panelId,
        action_id: action_id,
        action:    action || '',
        ok:        !!ok,
        result:    (result === undefined ? null : result),
        error:     error || null,
      }, '*');
    }catch(e){}
  }

  // Listen for chat → panel messages
  window.addEventListener('message', function(ev){
    var d = ev.data;
    if(!d || typeof d !== 'object') return;
    var t = d.type || '';
    if(t === 'vera:panel:init'){
      _panelId  = d.panel_id  || _panelId;
      _sessionId = d.session_id || _sessionId;
      // Send an initial snapshot so the chat doesn't have to wait for
      // user activity.
      setTimeout(publishState, 50);
    } else if(t === 'vera:panel:query'){
      publishState();
    } else if(t === 'vera:panel:action'){
      var act = String(d.action || '');
      var aid = d.action_id || '';
      var payload = d.payload || {};

      // Built-in __query__ handler — returns the current state. Lets
      // panel.query work without the panel author registering anything.
      if(act === '__query__'){
        publishActionResult(aid, true, _buildState(), null, act);
        publishStateDebounced();
        return;
      }

      var h = _actionHandlers[act] || _actionHandlers['*'];
      if(!h){
        publishEvent('action_unhandled', {action: act});
        publishActionResult(aid, false, null, 'no handler for action: '+act, act);
        return;
      }
      var ret;
      try{
        ret = h(payload, act);
      }catch(e){
        publishEvent('action_error', {action: act, error: String(e)});
        publishActionResult(aid, false, null, String(e), act);
        return;
      }
      // If the handler returned a Promise, wait for it before acking.
      if(ret && typeof ret.then === 'function'){
        ret.then(function(v){
          publishActionResult(aid, true, v === undefined ? null : v, null, act);
          publishStateDebounced();
        }, function(e){
          publishActionResult(aid, false, null, String(e), act);
        });
      } else {
        publishActionResult(aid, true, ret === undefined ? null : ret, null, act);
        publishStateDebounced();
      }
    }
  });

  // Auto-publish on common DOM signals so panels that don't wire
  // anything up still get reasonable freshness.
  ['click','change','input'].forEach(function(t){
    document.addEventListener(t, publishStateDebounced, {passive:true, capture:true});
  });
  // Periodic safety net (every 30s) so stale state eventually heals
  setInterval(publishStateDebounced, 30000);

  window.VeraPanelBridge = {
    registerStateProvider: function(fn){ _stateProvider = fn; publishStateDebounced(); },
    registerActionHandler: function(name, fn){ _actionHandlers[String(name)] = fn; },
    publishState:          publishState,
    publishStateDebounced: publishStateDebounced,
    publishEvent:          publishEvent,
    publishActionResult:   publishActionResult,
    panelId:               function(){ return _panelId; },
    sessionId:             function(){ return _sessionId; },
  };

  // First snapshot once DOM is settled (panels often initialise after
  // their own DOMContentLoaded handler runs)
  if(document.readyState === 'complete' || document.readyState === 'interactive'){
    setTimeout(publishStateDebounced, 100);
  } else {
    document.addEventListener('DOMContentLoaded', function(){
      setTimeout(publishStateDebounced, 100);
    });
  }
})();