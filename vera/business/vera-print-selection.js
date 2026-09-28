/* vera-print-selection.js -- select text anywhere in a Vera panel and print it.
   Fully self-contained + defensive: a failure here never affects the host panel. */
(function(){
  try{
    if(window.__veraPrintSel) return; window.__veraPrintSel = 1;
    var btn = null, lastText = '';
    function mk(){
      if(btn) return btn;
      btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = '🖨 Print';
      btn.style.cssText = 'position:fixed;z-index:2147483000;display:none;padding:5px 10px;'
        + 'font:12px/1.2 system-ui,-apple-system,sans-serif;background:#1a1f26;color:#d8dde3;'
        + 'border:1px solid #2e3742;border-radius:6px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.45)';
      btn.addEventListener('mousedown', function(e){ e.preventDefault(); });
      btn.addEventListener('click', function(e){ e.preventDefault(); send(); });
      (document.body || document.documentElement).appendChild(btn);
      return btn;
    }
    function hide(){ if(btn) btn.style.display = 'none'; }
    function reset(b){ setTimeout(function(){ hide(); b.textContent = '🖨 Print'; b.disabled = false; }, 1500); }
    function send(){
      var t = (lastText || '').trim(); if(!t) return;
      var b = mk(); b.textContent = '… printing'; b.disabled = true;
      fetch('/print/text', {method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({text: t.slice(0, 4000)})})
        .then(function(r){ return r.json(); })
        .then(function(r){
          b.textContent = (r && r.routed) ? '✓ printed'
            : ((r && r.escpos_b64) ? 'open Printer to finish' : ((r && r.error) || 'print failed'));
          reset(b);
        })
        .catch(function(){ b.textContent = 'print failed'; reset(b); });
    }
    function onSel(){
      try{
        // the right-click menu carries Print now (owner, 2026-09-27: "the existing thermal print option integrated into the new RCM")
        if(window.VeraRCM || window.__veraRcmOwn){ hide(); return; }
        var sel = window.getSelection(); var t = sel ? String(sel) : '';
        if(!t || t.trim().length < 2 || t.length > 6000){ hide(); return; }
        lastText = t;
        var rc = sel.getRangeAt(0).getBoundingClientRect();
        if(!rc || (rc.width === 0 && rc.height === 0)){ hide(); return; }
        var b = mk(); b.style.display = 'block';
        var top = rc.top - 32; if(top < 4) top = rc.bottom + 6;
        var vw = window.innerWidth || document.documentElement.clientWidth || 600;
        var left = Math.min(Math.max(4, rc.left), vw - 92);
        b.style.top = top + 'px'; b.style.left = left + 'px';
      }catch(e){ hide(); }
    }
    document.addEventListener('mouseup', function(){ setTimeout(onSel, 10); });
    document.addEventListener('keyup', function(e){ if(e.shiftKey || e.ctrlKey || e.metaKey) setTimeout(onSel, 10); });
    document.addEventListener('scroll', hide, true);
    document.addEventListener('mousedown', function(e){ if(btn && e.target !== btn) hide(); });
  }catch(e){ /* never break the host panel */ }
})();
