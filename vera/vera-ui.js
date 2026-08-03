/**
 * vera-ui.js v2 — Vera Universal UI Integration
 * ================================================
 * Include in any panel:  <script src="/ui/vera-ui.js"></script>
 *
 * How it works:
 *   1. Loads /ui/themes.css (all theme CSS definitions)
 *   2. Fetches active theme from /ui/theme and applies it
 *   3. Hooks into the panel's EXISTING setTheme() if present — does NOT replace it
 *   4. When the panel's own theme picker is used, broadcasts the change via /ui/theme/set
 *   5. Listens for cross-panel theme changes via parent postMessage + MutationObserver
 *   6. Maps theme vars to all three namespaces (research, orchestrator, IDE)
 *
 * Standalone panels keep working exactly as before — vera-ui.js is additive.
 */
(function(){
  'use strict';
  var BASE = window.location.origin;
  var _current = '';
  var _hookedSetTheme = false;

  // Relative luminance of a #hex colour (0=black … 1=white); non-hex → 0.5.
  function _lum(hex){
    if(typeof hex !== 'string') return 0.5;
    var h = hex.trim().replace('#','');
    if(h.length === 3) h = h.replace(/(.)/g,'$1$1');
    if(h.length < 6) return 0.5;
    var r = parseInt(h.slice(0,2),16)/255,
        g = parseInt(h.slice(2,4),16)/255,
        b = parseInt(h.slice(4,6),16)/255;
    function lin(c){ return c<=0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); }
    if(isNaN(r)||isNaN(g)||isNaN(b)) return 0.5;
    return 0.2126*lin(r) + 0.7152*lin(g) + 0.0722*lin(b);
  }
  // Pick dark/light ink for text sitting on an accent-coloured surface.
  function _deriveOnAccent(accent){
    if(!accent) return '';
    return _lum(accent) > 0.45 ? '#0c0d10' : '#f6f4ef';
  }

  // ── 0. Theme cache ─────────────────────────────────────────────────────────
  // The last-applied theme + its resolved CSS vars are cached in localStorage
  // (shared across every same-origin panel iframe). This lets each panel paint
  // the correct theme *synchronously* on load — before the /ui/theme fetch — so
  // panels never flash their hard-coded dark defaults while the request is in
  // flight. The fetch in section 4 stays as a background reconcile.
  var CACHE_KEY = 'vera:ui:theme';
  var CACHE_VARS_KEY = 'vera:ui:themeVars';
  // Coherence stamp: which theme id the cached vars belong to. The id and the
  // vars are separate keys, and several paths used to update the id WITHOUT
  // re-resolving vars — every page then booted data-theme="ash" (light) while
  // replaying the previous dark theme's inline vars over it, i.e. "black
  // background on ash" everywhere. Vars are now only replayed when the stamp
  // matches the id; otherwise the [data-theme] stylesheet wins.
  var CACHE_VARS_FOR_KEY = 'vera:ui:themeVarsFor';

  function _readCache(){
    try{
      var id = localStorage.getItem(CACHE_KEY);
      if(!id) return null;
      var raw = localStorage.getItem(CACHE_VARS_KEY);
      var vfor = localStorage.getItem(CACHE_VARS_FOR_KEY);
      return { theme: id, vars: (raw && vfor === id) ? JSON.parse(raw) : null };
    }catch(e){ return null; }
  }
  function _writeCache(id, vars){
    try{
      localStorage.setItem(CACHE_KEY, id);
      if(vars && typeof vars === 'object' && Object.keys(vars).length){
        localStorage.setItem(CACHE_VARS_KEY, JSON.stringify(vars));
        localStorage.setItem(CACHE_VARS_FOR_KEY, id);
      } else if(localStorage.getItem(CACHE_VARS_FOR_KEY) !== id){
        // id moved without resolved vars — drop the stale set so no page
        // paints the old theme's colours under the new id.
        localStorage.removeItem(CACHE_VARS_KEY);
        localStorage.removeItem(CACHE_VARS_FOR_KEY);
      }
    }catch(e){}
  }

  // ── 0b. UI scale (global zoom) ─────────────────────────────────────────────
  // A single magnification factor applied to every frame independently via CSS
  // `zoom` on <html>. It scales layout (font, spacing, controls) so the whole UI
  // grows/shrinks to taste — no per-stylesheet rem refactor needed.
  //
  // Why per-frame (not just the shell) is correct: panels are embedded as
  // width/height:100% iframes. Under an ancestor's CSS zoom a percentage-sized
  // box keeps its *device* size (its CSS-px size divides by the zoom, then paints
  // back ×zoom), so the shell's zoom scales its own chrome but NOT the panel
  // content. Each panel therefore applies the same factor to itself and magnifies
  // its content exactly once — no double-scaling.
  //
  // The value lives in localStorage (shared by every same-origin frame), so one
  // setting drives them all and the `storage` event live-syncs already-open
  // frames. Server persistence (/ui/scale) seeds fresh browsers.
  var SCALE_KEY = 'vera:ui:scale';
  var SCALE_MIN = 0.6, SCALE_MAX = 2.0, SCALE_STEP = 0.1, SCALE_DEFAULT = 1.0;

  function _clampScale(s){
    s = parseFloat(s);
    if(!isFinite(s) || s <= 0) return SCALE_DEFAULT;
    return Math.max(SCALE_MIN, Math.min(SCALE_MAX, Math.round(s*100)/100));
  }
  function _readScale(){
    try{ var v = localStorage.getItem(SCALE_KEY); return v!=null ? _clampScale(v) : SCALE_DEFAULT; }
    catch(e){ return SCALE_DEFAULT; }
  }
  // Paint only — no persistence/broadcast. `zoom` reflows at the scaled size
  // (unlike transform:scale, which repaints and leaves scrollbars/overflow
  // wrong). Supported in every current engine (Firefox 126+).
  //
  // The counter-scale is the important part: native browser zoom shrinks the
  // layout viewport so `100vh`/`100vw` still fit, but CSS `zoom` does NOT — a
  // full-height layout would overflow and the frame would grow a spurious
  // scrollbar. So we (a) counter-size <html> to viewport/scale (it renders back
  // to exactly the viewport after zoom), and (b) publish `--ui-scale` so any
  // `height:calc(100vh / var(--ui-scale,1))` box in the page (the shell body,
  // and any panel that opts in) counter-scales itself the same way. Panels that
  // still use raw `100vh` just get a normal inner scrollbar when enlarged —
  // graceful, and exactly what zooming a page does.
  function _paintScale(s){
    s = _clampScale(s);
    var d = document.documentElement;
    try{
      if(s === 1){
        d.style.zoom = '';
        d.style.width = '';
        d.style.height = '';
        d.style.removeProperty('--ui-scale');
      } else {
        d.style.setProperty('--ui-scale', String(s));
        d.style.zoom = String(s);
        d.style.width  = 'calc(100vw / ' + s + ')';
        d.style.height = 'calc(100vh / ' + s + ')';
      }
    }catch(e){}
  }
  // Set + persist + broadcast. Returns the clamped value actually applied.
  function setScale(s){
    s = _clampScale(s);
    _paintScale(s);
    try{ localStorage.setItem(SCALE_KEY, String(s)); }catch(e){}
    // Cross-session / cross-device persistence — fire and forget.
    fetch(BASE + '/ui/scale/set', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({scale: s})
    }).catch(function(){});
    // Same-origin siblings pick this up via the `storage` event; also relay by
    // postMessage (parent + our own children) so the shell's iframe tree updates
    // even if a browser throttles storage events.
    try{ window.parent.postMessage({type:'vera:scale', scale: s}, '*'); }catch(e){}
    try{
      var fr = document.querySelectorAll('iframe');
      for(var i=0;i<fr.length;i++){
        try{ fr[i].contentWindow.postMessage({type:'vera:scale', scale: s}, '*'); }catch(e2){}
      }
    }catch(e3){}
    return s;
  }

  // ── 1. Load theme CSS ──────────────────────────────────────────────────────
  if(!document.getElementById('vera-themes-css')){
    var link = document.createElement('link');
    link.id = 'vera-themes-css';
    link.rel = 'stylesheet';
    link.href = BASE + '/ui/themes.css';
    document.head.appendChild(link);
  }

  // ── 1b. Load the configurable loading animation ────────────────────────────
  // Auto-upgrades any .vera-loading overlay to the configured animation
  // (default: an evolving node/edge graph). Additive — panels that never show a
  // loading overlay are unaffected.
  if(!window.VeraLoader && !document.getElementById('vera-loader-js')){
    var lsc = document.createElement('script');
    lsc.id = 'vera-loader-js';
    lsc.src = BASE + '/ui/vera-loader.js';
    lsc.async = true;
    document.head.appendChild(lsc);
  }

  // ── 2. Apply theme + map vars across namespaces ────────────────────────────
  // Returns the full set of properties actually applied (raw research vars plus
  // every mapped orchestrator/IDE alias). setThemeLocal caches this so the
  // in-panel head snippet can repaint from a single flat object with no mapping
  // logic of its own.
  function applyVars(vars){
    if(!vars || typeof vars !== 'object') return null;
    var root = document.documentElement;
    var out = {};
    function set(key, val){ root.style.setProperty(key, val); out[key] = val; }
    var k;
    // Set all theme vars directly
    for(k in vars) set(k, vars[k]);

    // Map research → orchestrator namespace
    var rmap = {
      '--bg':'--bg0', '--s1':'--bg1', '--s2':'--bg2', '--s3':'--bg3',
      '--bd':'--border', '--bd2':'--border2',
      '--t1':'--text', '--t2':'--dim', '--t3':'--dim2',
      '--ac':'--acc', '--ac2':'--acc2', '--ac3':'--acc3',
      '--ac4':'--err', '--ac5':'--acc4',
      '--on-ac':'--on-acc'
    };
    for(k in rmap) if(vars[k]) set(rmap[k], vars[k]);
    if(vars['--t1']) set('--fg', vars['--t1']);
    if(vars['--ac2']) set('--ok', vars['--ac2']);
    if(vars['--ac3']) set('--warn', vars['--ac3']);

    // Text-on-accent: the readable ink painted on accent-coloured surfaces
    // (active tabs, .primary buttons, "on" chips). Themes ship an explicit
    // --on-ac; if an older/cached theme omits it, derive one that contrasts
    // the accent so those controls never render as ink-on-ink. Publish under
    // both the alias (--on-accent) and the mapped orchestrator var.
    var onAc = vars['--on-ac'] || _deriveOnAccent(vars['--ac']);
    if(onAc){ set('--on-acc', onAc); set('--on-accent', onAc); }

    // Fill remaining namespace gaps so every panel repaints fully on a theme
    // switch (Telegram family: --fg2/--purple; warm family: --gpu).
    // --fg1/--fg3 are the primary/muted text vars used by the dream + several
    // other panels; without mapping them, those panels keep dark-theme text in
    // light mode (unreadable buttons/text). Map them from --t1/--t3.
    if(vars['--t1']) set('--fg1', vars['--t1']);
    if(vars['--t3']) set('--fg3', vars['--t3']);
    if(vars['--t2']) set('--fg2', vars['--t2']);
    if(vars['--ac5']) set('--purple', vars['--ac5']);
    if(vars['--ac3']) set('--gpu', vars['--ac3']);

    // Map research → IDE namespace
    if(vars['--bg'])  set('--bg0', vars['--bg']);
    if(vars['--s1'])  set('--bg1', vars['--s1']);
    if(vars['--s2'])  set('--bg2', vars['--s2']);
    if(vars['--s3']){ set('--bg3', vars['--s3']); set('--bg4', vars['--s3']); }
    if(vars['--t1'])  set('--text0', vars['--t1']);
    if(vars['--t2'])  set('--text1', vars['--t2']);
    if(vars['--t3'])  set('--text2', vars['--t3']);
    if(vars['--ac'])  set('--accent', vars['--ac']);
    return out;
  }

  function setThemeLocal(id, vars){
    _current = id;
    document.documentElement.setAttribute('data-theme', id);

    // If vars provided, apply them directly (maps to all namespaces). applyVars
    // returns the *full* applied set (raw + mapped) — that's what we cache.
    var effective = null;
    if(vars && typeof vars === 'object' && Object.keys(vars).length > 0){
      effective = applyVars(vars);
    } else {
      // Vars not provided — read them from computed style after data-theme was set
      // (the themes.css stylesheet defines them per [data-theme])
      var cs = getComputedStyle(document.documentElement);
      var readVars = {};
      ['--bg','--s1','--s2','--s3','--bd','--bd2',
       '--t1','--t2','--t3','--ac','--ac2','--ac3','--ac4','--ac5','--on-ac'
      ].forEach(function(v){
        var val = cs.getPropertyValue(v).trim();
        if(val) readVars[v] = val;
      });
      if(Object.keys(readVars).length > 0){ effective = applyVars(readVars); }
    }

    // Persist the fully-mapped set so the next panel/iframe (and the next page
    // load) can paint this theme instantly — the in-panel head snippet just
    // replays these key/values, no /ui/theme round-trip on the critical path.
    _writeCache(id, effective);

    // Call the panel's own setTheme if it exists (research, notebook, NLP)
    if(!_hookedSetTheme && typeof window._origSetTheme === 'function'){
      _hookedSetTheme = true;
      try{ window._origSetTheme(id, false); } catch(e){}
      _hookedSetTheme = false;
    }
  }

  // ── 2b. Instant paint from cache ───────────────────────────────────────────
  // Runs synchronously the moment this script is parsed — before DOMContentLoaded
  // and before the /ui/theme fetch — so the panel adopts the last-known theme
  // with zero network latency. Applies inline vars directly, so it doesn't even
  // wait on themes.css to download. No cache (first-ever load) → no-op, and the
  // fetch below fills it in exactly as before.
  (function applyCachedTheme(){
    var c = _readCache();
    if(c && c.theme){
      _current = c.theme;
      document.documentElement.setAttribute('data-theme', c.theme);
      if(c.vars) applyVars(c.vars);
    }
  })();

  // Paint the cached UI scale synchronously too, so the panel never flashes at
  // 100% before the setting applies.
  (function applyCachedScale(){ _paintScale(_readScale()); })();

  // ── 3. Hook into existing setTheme ─────────────────────────────────────────
  // If the panel already has setTheme(), wrap it so changes broadcast to the API.
  // We do this after DOMContentLoaded to ensure the panel's JS has loaded.
  function hookExistingSetTheme(){
    if(typeof window.setTheme === 'function' && !window.setTheme._veraHooked){
      window._origSetTheme = window.setTheme;
      window.setTheme = function(t, save){
        // Call the original — this handles localStorage, UI updates, CodeMirror, etc.
        window._origSetTheme(t, save);
        _current = t;
        // Broadcast to other panels via the API (fire-and-forget)
        fetch(BASE + '/ui/theme/set', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({theme: t})
        }).catch(function(){});
        // Notify parent
        try{ window.parent.postMessage({type:'vera:theme', theme:t}, '*'); }catch(e){}
      };
      window.setTheme._veraHooked = true;
    }
  }

  // ── 4. Fetch active theme from API ─────────────────────────────────────────
  function fetchAndApply(retryCount){
    retryCount = retryCount || 0;
    fetch(BASE + '/ui/theme').then(function(r){ return r.json(); }).then(function(data){
      if(data && data.theme){
        // The cache already painted this theme synchronously; only reconcile if
        // the server's active theme has actually changed since, to avoid a
        // redundant repaint (and re-running the panel's own setTheme).
        if(data.theme === _current &&
           document.documentElement.getAttribute('data-theme') === data.theme){
          // Same theme already painted — re-map+cache the server vars (cheap and
          // visually a no-op if unchanged; picks up edited custom-theme vars).
          if(data.vars) _writeCache(data.theme, applyVars(data.vars));
          return;
        }
        setThemeLocal(data.theme, data.vars);
      }
    }).catch(function(){
      // Backend not ready — retry with backoff (max 6 attempts)
      if(retryCount < 6){
        setTimeout(function(){ fetchAndApply(retryCount + 1); },
                   retryCount === 0 ? 500 : 2000);
      }
    });
  }

  // ── 5. Listen for cross-panel theme changes ────────────────────────────────
  // postMessage from parent or sibling iframes
  window.addEventListener('message', function(e){
    if(e.data && e.data.type === 'vera:theme' && e.data.theme){
      setThemeLocal(e.data.theme, e.data.vars);
    }
    // Also handle WS event forwarded as a message (some panels relay WS events)
    if(e.data && e.data.type === 'vera_event' && e.data.event &&
       e.data.event.type === 'ui.theme.changed'){
      setThemeLocal(e.data.event.theme, e.data.event.vars);
    }
    // UI scale relayed from a sibling/parent frame.
    if(e.data && e.data.type === 'vera:scale' && typeof e.data.scale !== 'undefined'){
      _paintScale(e.data.scale);
    }
    // Scale changed via the capability (agent / another client) — apply + cache.
    if(e.data && e.data.type === 'vera_event' && e.data.event &&
       e.data.event.type === 'ui.scale.changed'){
      var sv = _clampScale(e.data.event.scale);
      _paintScale(sv);
      try{ localStorage.setItem(SCALE_KEY, String(sv)); }catch(e2){}
    }
  });

  // ── 5b. Live-sync scale across same-origin frames + keyboard shortcuts ──────
  // The `storage` event fires in every OTHER same-origin frame when any frame
  // changes localStorage — the natural broadcast channel for a shared setting.
  window.addEventListener('storage', function(e){
    if(e.key === SCALE_KEY){ _paintScale(e.newValue!=null ? e.newValue : SCALE_DEFAULT); }
  });
  // Ctrl/Cmd+Alt with =/-/0 adjusts UI scale (Alt keeps native browser zoom on
  // plain Ctrl +/-/0 free).
  window.addEventListener('keydown', function(e){
    if(!(e.ctrlKey || e.metaKey) || !e.altKey || e.shiftKey) return;
    var k = e.key;
    if(k === '=' || k === '+'){ e.preventDefault(); setScale(_readScale() + SCALE_STEP); }
    else if(k === '-' || k === '_'){ e.preventDefault(); setScale(_readScale() - SCALE_STEP); }
    else if(k === '0'){ e.preventDefault(); setScale(SCALE_DEFAULT); }
  });

  // MutationObserver on parent frame's data-theme attribute
  try{
    var parentRoot = window.parent && window.parent.document ? window.parent.document.documentElement : null;
    if(parentRoot && parentRoot !== document.documentElement){
      new MutationObserver(function(){
        var theme = parentRoot.getAttribute('data-theme');
        if(theme && theme !== _current){
          // Read vars from parent's computed style
          var cs = getComputedStyle(parentRoot);
          var vars = {};
          ['--bg','--s1','--s2','--s3','--bd','--bd2',
           '--t1','--t2','--t3','--ac','--ac2','--ac3','--ac4','--ac5','--on-ac'
          ].forEach(function(v){
            var val = cs.getPropertyValue(v).trim();
            if(val) vars[v] = val;
          });
          setThemeLocal(theme, Object.keys(vars).length ? vars : null);
        }
      }).observe(parentRoot, {attributes:true, attributeFilter:['data-theme']});
    }
  }catch(e){/* cross-origin */}

  // ── 6. Inject theme picker ─────────────────────────────────────────────────
  function injectPicker(containerId){
    var container = document.getElementById(containerId);
    if(!container) return;
    fetch(BASE + '/ui/themes').then(function(r){return r.json()}).then(function(data){
      var themes = data.themes || {};
      var html = '';
      for(var tid in themes){
        var t = themes[tid];
        var active = tid === _current;
        html += '<div data-t="' + tid + '" onclick="veraUI.setTheme(\'' + tid + '\')" ' +
          'style="display:flex;align-items:center;gap:7px;padding:3px 6px;border-radius:5px;cursor:pointer;' +
          'transition:.1s;' + (active ? 'background:var(--bd,rgba(255,255,255,.07))' : '') + '">' +
          '<div style="width:14px;height:14px;border-radius:50%;background:' + (t.accent||'#888') +
          ';border:2px solid ' + (active ? 'var(--t1,#fff)' : 'transparent') + '"></div>' +
          '<span style="font-size:11px;color:' + (active ? 'var(--t1,#fff)' : 'var(--t2,#888)') + '">' +
          (t.label||tid) + '</span></div>';
      }
      container.innerHTML = html;
    }).catch(function(){});
  }

  // ── 6b. Reusable "UI size" control ─────────────────────────────────────────
  // −  100%  +  ↺  — used by the standalone floating picker (the shell builds an
  // equivalent in its own theme menu). Buttons stopPropagation so clicking them
  // never dismisses the containing menu.
  function _makeScaleControl(){
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;padding:5px 6px 8px;margin-bottom:5px;'+
      'border-bottom:1px solid var(--bd2,var(--border2,rgba(128,128,128,.3)))';
    var lbl = document.createElement('span');
    lbl.textContent = 'UI size';
    lbl.style.cssText = 'flex:1;color:var(--t2,var(--dim,#999))';
    function mk(txt, title){
      var b = document.createElement('button');
      b.type = 'button'; b.textContent = txt; b.title = title;
      b.style.cssText = 'width:22px;height:22px;padding:0;line-height:1;border-radius:5px;cursor:pointer;'+
        'border:1px solid var(--bd2,var(--border2,rgba(128,128,128,.4)));'+
        'background:var(--s2,var(--bg2,#222));color:var(--t1,var(--text,#ddd));font-size:13px';
      return b;
    }
    var minus = mk('−','Smaller (Ctrl+Alt+−)');
    var val   = document.createElement('span');
    val.style.cssText = 'min-width:36px;text-align:center;color:var(--t1,var(--text,#ddd));font-variant-numeric:tabular-nums';
    var plus  = mk('+','Larger (Ctrl+Alt+=)');
    var reset = mk('↺','Reset to 100% (Ctrl+Alt+0)');
    function refresh(){ val.textContent = Math.round(_readScale()*100) + '%'; }
    minus.onclick = function(e){ e.stopPropagation(); setScale(_readScale()-SCALE_STEP); refresh(); };
    plus.onclick  = function(e){ e.stopPropagation(); setScale(_readScale()+SCALE_STEP); refresh(); };
    reset.onclick = function(e){ e.stopPropagation(); setScale(SCALE_DEFAULT); refresh(); };
    refresh();
    row.appendChild(lbl); row.appendChild(minus); row.appendChild(val);
    row.appendChild(plus); row.appendChild(reset);
    return row;
  }

  // ── 7. Standalone floating theme picker ─────────────────────────────────────
  // A panel viewed on its own (top-level document, not inside the shell's tab
  // iframe) has no theme selector of its own. Inject a small floating control
  // so any panel is themeable when displayed alone. Suppressed when embedded
  // in the shell (window.top !== window) or when the page already ships a
  // theme UI (main shell #themeBtn, UI-builder #themeButtons/#themePicker).
  function injectFloatingPicker(){
    try{ if(window.self !== window.top) return; }catch(e){ return; } // embedded
    if(window.__veraNoFloatingPicker) return;
    if(document.getElementById('themeBtn') ||
       document.getElementById('themeButtons') ||
       document.getElementById('themePicker') ||
       document.getElementById('veraFloatTheme')) return;

    var wrap = document.createElement('div');
    wrap.id = 'veraFloatTheme';
    wrap.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483000;'+
      'font-family:var(--sans,system-ui,sans-serif);font-size:11px';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.title = 'Theme';
    btn.textContent = '🎨';
    btn.style.cssText = 'width:30px;height:30px;border-radius:50%;cursor:pointer;'+
      'border:1px solid var(--bd2,var(--border2,rgba(128,128,128,.4)));'+
      'background:var(--s2,var(--bg2,#222));color:var(--t1,var(--text,#ddd));'+
      'box-shadow:0 3px 12px rgba(0,0,0,.4);line-height:1;font-size:15px;'+
      'display:flex;align-items:center;justify-content:center;padding:0';
    var menu = document.createElement('div');
    menu.style.cssText = 'display:none;position:absolute;right:0;bottom:38px;'+
      'min-width:150px;max-height:60vh;overflow-y:auto;padding:5px;'+
      'background:var(--s1,var(--bg1,#16181d));'+
      'border:1px solid var(--bd2,var(--border2,rgba(128,128,128,.4)));'+
      'border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.5)';
    wrap.appendChild(menu); wrap.appendChild(btn);

    function renderMenu(){
      fetch(BASE + '/ui/themes').then(function(r){return r.json();}).then(function(data){
        var themes = (data && data.themes) || {};
        menu.innerHTML = '';
        menu.appendChild(_makeScaleControl());
        Object.keys(themes).forEach(function(tid){
          var t = themes[tid];
          var active = tid === _current;
          var row = document.createElement('div');
          row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:5px 8px;'+
            'border-radius:5px;cursor:pointer;white-space:nowrap;'+
            (active ? 'background:var(--bd,rgba(128,128,128,.14))' : '');
          row.onmouseover = function(){ if(!active) row.style.background='var(--s3,var(--bg3,rgba(128,128,128,.1)))'; };
          row.onmouseout  = function(){ if(!active) row.style.background=''; };
          row.onclick = function(){ window.veraUI.setTheme(tid); menu.style.display='none'; };
          row.innerHTML =
            '<span style="width:12px;height:12px;border-radius:50%;flex-shrink:0;'+
            'background:'+(t.accent||'#888')+';border:1.5px solid '+(active?'var(--t1,#fff)':'transparent')+'"></span>'+
            '<span style="color:'+(active?'var(--t1,var(--text,#fff))':'var(--t2,var(--dim,#999))')+'">'+
            (t.label||tid)+'</span>';
          menu.appendChild(row);
        });
      }).catch(function(){});
    }
    btn.onclick = function(e){
      e.stopPropagation();
      if(menu.style.display === 'block'){ menu.style.display='none'; return; }
      renderMenu(); menu.style.display='block';
    };
    document.addEventListener('click', function(){ menu.style.display='none'; });

    (document.body || document.documentElement).appendChild(wrap);
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  window.veraUI = {
    setTheme: function(id){
      // Always call the API so the change is broadcast and we get vars back
      fetch(BASE + '/ui/theme/set', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({theme: id})
      }).then(function(r){ return r.json(); }).then(function(data){
        if(data && data.theme) setThemeLocal(data.theme, data.vars);
        // Also call panel's own setTheme for localStorage/CodeMirror
        if(typeof window._origSetTheme === 'function'){
          try{ window._origSetTheme(data.theme || id, false); }catch(e){}
        } else if(typeof window.setTheme === 'function' && !window.setTheme._veraHooked){
          try{ window.setTheme(data.theme || id, false); }catch(e){}
        }
      }).catch(function(){
        // API unavailable — apply locally
        setThemeLocal(id, null);
      });
      // Notify parent
      try{ window.parent.postMessage({type:'vera:theme', theme:id}, '*'); }catch(e){}
    },
    getTheme: function(){ return _current; },
    applyTheme: setThemeLocal,
    applyVars: applyVars,
    injectPicker: injectPicker,
    injectFloatingPicker: injectFloatingPicker,
    onAccent: _deriveOnAccent,
    // UI scale (global zoom)
    setScale: setScale,
    getScale: _readScale,
    nudgeScale: function(d){ return setScale(_readScale() + (d||0)); },
    applyScale: _paintScale,
    makeScaleControl: _makeScaleControl,
    SCALE_MIN: SCALE_MIN, SCALE_MAX: SCALE_MAX, SCALE_STEP: SCALE_STEP,
    BASE: BASE,
  };

  // ── Auto-init ──────────────────────────────────────────────────────────────
  // Hook existing setTheme after DOM is ready
  function _init(){
    hookExistingSetTheme();
    fetchAndApply();
    fetchAndApplyScale();
    injectFloatingPicker();
  }

  // Seed the UI scale from the server ONLY when this browser has no explicit
  // local choice yet — so a per-device override (different monitor sizes) is
  // never stomped by the shared server value on reload.
  function fetchAndApplyScale(){
    var hasLocal = false;
    try{ hasLocal = localStorage.getItem(SCALE_KEY) != null; }catch(e){}
    if(hasLocal) return;
    fetch(BASE + '/ui/scale').then(function(r){ return r.json(); }).then(function(d){
      if(d && typeof d.scale !== 'undefined'){
        var s = _clampScale(d.scale);
        _paintScale(s);
        try{ localStorage.setItem(SCALE_KEY, String(s)); }catch(e){}
      }
    }).catch(function(){});
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    // Already loaded — hook now, but give panel JS a moment to define setTheme
    setTimeout(_init, 100);
  }
})();