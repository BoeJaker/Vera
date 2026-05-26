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

  // ── 1. Load theme CSS ──────────────────────────────────────────────────────
  if(!document.getElementById('vera-themes-css')){
    var link = document.createElement('link');
    link.id = 'vera-themes-css';
    link.rel = 'stylesheet';
    link.href = BASE + '/ui/themes.css';
    document.head.appendChild(link);
  }

  // ── 2. Apply theme + map vars across namespaces ────────────────────────────
  function applyVars(vars){
    if(!vars || typeof vars !== 'object') return;
    var root = document.documentElement;
    var k;
    // Set all theme vars directly
    for(k in vars) root.style.setProperty(k, vars[k]);

    // Map research → orchestrator namespace
    var rmap = {
      '--bg':'--bg0', '--s1':'--bg1', '--s2':'--bg2', '--s3':'--bg3',
      '--bd':'--border', '--bd2':'--border2',
      '--t1':'--text', '--t2':'--dim', '--t3':'--dim2',
      '--ac':'--acc', '--ac2':'--acc2', '--ac3':'--acc3',
      '--ac4':'--err', '--ac5':'--acc4'
    };
    for(k in rmap) if(vars[k]) root.style.setProperty(rmap[k], vars[k]);
    if(vars['--t1']) root.style.setProperty('--fg', vars['--t1']);
    if(vars['--ac2']) root.style.setProperty('--ok', vars['--ac2']);
    if(vars['--ac3']) root.style.setProperty('--warn', vars['--ac3']);

    // Map research → IDE namespace
    if(vars['--bg'])  root.style.setProperty('--bg0', vars['--bg']);
    if(vars['--s1'])  root.style.setProperty('--bg1', vars['--s1']);
    if(vars['--s2'])  root.style.setProperty('--bg2', vars['--s2']);
    if(vars['--s3']){ root.style.setProperty('--bg3', vars['--s3']); root.style.setProperty('--bg4', vars['--s3']); }
    if(vars['--t1'])  root.style.setProperty('--text0', vars['--t1']);
    if(vars['--t2'])  root.style.setProperty('--text1', vars['--t2']);
    if(vars['--t3'])  root.style.setProperty('--text2', vars['--t3']);
    if(vars['--ac'])  root.style.setProperty('--accent', vars['--ac']);
  }

  function setThemeLocal(id, vars){
    _current = id;
    document.documentElement.setAttribute('data-theme', id);

    // If vars provided, apply them directly (maps to all namespaces)
    if(vars && typeof vars === 'object' && Object.keys(vars).length > 0){
      applyVars(vars);
    } else {
      // Vars not provided — read them from computed style after data-theme was set
      // (the themes.css stylesheet defines them per [data-theme])
      var cs = getComputedStyle(document.documentElement);
      var readVars = {};
      ['--bg','--s1','--s2','--s3','--bd','--bd2',
       '--t1','--t2','--t3','--ac','--ac2','--ac3','--ac4','--ac5'
      ].forEach(function(v){
        var val = cs.getPropertyValue(v).trim();
        if(val) readVars[v] = val;
      });
      if(Object.keys(readVars).length > 0) applyVars(readVars);
    }

    // Call the panel's own setTheme if it exists (research, notebook, NLP)
    if(!_hookedSetTheme && typeof window._origSetTheme === 'function'){
      _hookedSetTheme = true;
      try{ window._origSetTheme(id, false); } catch(e){}
      _hookedSetTheme = false;
    }
  }

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
           '--t1','--t2','--t3','--ac','--ac2','--ac3','--ac4','--ac5'
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
    BASE: BASE,
  };

  // ── Auto-init ──────────────────────────────────────────────────────────────
  // Hook existing setTheme after DOM is ready
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', function(){
      hookExistingSetTheme();
      fetchAndApply();
    });
  } else {
    // Already loaded — hook now, but give panel JS a moment to define setTheme
    setTimeout(function(){
      hookExistingSetTheme();
      fetchAndApply();
    }, 100);
  }
})();