/* vera/ui/rcm.js — the ONE right-click menu, on every Vera UI (served /ui/rcm.js, window.VeraRCM).
   ───────────────────────────────────────────────────────────────────────────────────────────────────────
   The chat's context menu, made a library (owner, 2026-09-27: "the RCM of the design bleeding edge also needs extending
   to other UIs ... and the existing thermal print option integrated into the new RCM"). WHAT a right-click offers still
   comes from the one registry, /ui/menus.js (window.MENUS): the kind of thing under the pointer decides the rows. This
   file is the part every page shares: it finds what was clicked, draws the menu and the capability runner in the
   design's look, and does the actions any page can do - copy, print it on the thermal printer, open it, ask Vera.

   A page adds what only it knows:
     VeraRCM.attach({ target(el) -> [kind, name, el, extras] | null | false,   // false: leave this click alone
                      act(id, kind, name, el, extras) -> true when handled })
   Generic targets: a text selection ('text'), [data-entity]/[data-ref] (kind from data-kind or the ref's prefix),
   [data-rcm-kind] (+ data-rcm-name), a widget tile or <vera-widget>, a panel's nav button, a table row, a link, code.
   Anything else keeps the browser's own menu; Shift + right-click always does. A page with its own RCM (the chat) sets
   window.__veraRcmOwn and this one stands aside.                                                                    */
(function () {
  'use strict';
  if (window.VeraRCM) return;
  var CSS = [
    '.cmw{position:fixed;inset:0;z-index:2147482000;pointer-events:none}',
    '.cmw .cmbg{position:fixed;inset:0;pointer-events:auto}',
    '.cmenu{position:fixed;z-index:2147482001;min-width:216px;max-width:320px;padding:5px;pointer-events:auto;background:var(--surf,var(--s1,var(--bg1,#151719)));border-radius:var(--ui-radius,10px);box-shadow:var(--elev,0 8px 24px rgba(0,0,0,.28)),0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09)));display:flex;flex-direction:column;font-family:var(--f-ui,var(--font-ui,system-ui,sans-serif));animation:rcmIn .12s ease-out}',
    '@keyframes rcmIn{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}',
    '.cmenu .cm-h{display:flex;align-items:center;gap:7px;padding:5px 9px 7px;font-size:11.5px;color:var(--t1,var(--text,#d8dce4));font-weight:600;white-space:nowrap;overflow:hidden}',
    '.cmenu .cm-h i{width:7px;height:7px;border-radius:2px;flex-shrink:0;display:inline-block}',
    '.cmenu .cm-h b{margin-left:auto;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;color:var(--t3,var(--dim,#6b7280));font-weight:400;padding-left:10px}',
    '.cmenu .cm-h span{overflow:hidden;text-overflow:ellipsis;min-width:0}',
    '.cmenu .cm-i{display:flex;align-items:center;gap:10px;width:100%;font:inherit;font-size:11.5px;color:var(--t2,var(--dim2,#8a92a0));background:transparent;border:none;border-radius:var(--r-sm,6px);padding:6px 9px;text-align:left;cursor:pointer;white-space:nowrap}',
    '.cmenu .cm-i:hover{background:var(--surf2,var(--s2,var(--bg2,#1a1c20)));color:var(--t1,var(--text,#d8dce4))}',
    '.cmenu .cm-i i{margin-left:auto;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;color:var(--t3,var(--dim,#6b7280));font-style:normal;padding-left:12px}',
    '.cmenu .cm-i.danger{color:var(--dv6,#c96b6b)}',
    '.cmenu .cm-i.cap{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10.5px}.cmenu .cm-i.cap::before{content:"▷";font-size:8px;color:var(--ac,var(--acc,#5a9e8f))}',
    '.cmenu .cm-sep{height:1px;background:var(--bd,var(--border,rgba(255,255,255,.09)));margin:4px 6px}',
    '.crun{position:fixed;z-index:2147481999;width:330px;padding:10px 12px;border-radius:var(--ui-radius,10px);background:var(--surf,var(--s1,var(--bg1,#151719)));box-shadow:var(--elev,0 8px 24px rgba(0,0,0,.28)),0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09)));display:flex;flex-direction:column;gap:7px;font-family:var(--f-ui,var(--font-ui,system-ui,sans-serif))}',
    '.crun .crun-h{display:flex;align-items:center;gap:7px;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:11px;color:var(--ac,var(--acc,#5a9e8f))}',
    '.crun .crun-h i{width:7px;height:7px;border-radius:50%;background:var(--ac,var(--acc,#5a9e8f));display:inline-block}',
    '.crun .crun-h b{margin-left:auto;font-weight:400;font-size:10px;color:var(--t3,var(--dim,#6b7280));overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}',
    '.crun .crun-x{border:none;background:transparent;color:var(--t3,var(--dim,#6b7280));cursor:pointer;font-size:11px;padding:0 2px}',
    '.crun .crun-b{display:block;width:100%;box-sizing:border-box;min-height:64px;max-height:180px;resize:vertical;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10.5px;line-height:1.45;color:var(--t1,var(--text,#d8dce4));background:var(--bg0,var(--bg,#0e0f12));border:none;border-radius:var(--r-sm,6px);padding:7px 8px}',
    '.crun .crun-a{display:flex;align-items:center;gap:8px}',
    '.crun .crun-a .go{height:26px;padding:0 12px;border:none;border-radius:var(--r-pill,99px);background:var(--ac,var(--acc,#5a9e8f));color:var(--on-ac,var(--on-acc,#fff));font:inherit;font-size:11px;font-weight:600;cursor:pointer}',
    '.crun .crun-r{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;color:var(--t3,var(--dim,#6b7280));flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.crun .crun-out{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;color:var(--t2,var(--dim2,#8a92a0));max-height:140px;overflow:auto;white-space:pre-wrap;word-break:break-word}',
    '.rcm-toast{position:fixed;right:14px;bottom:14px;z-index:2147482002;padding:7px 12px;border-radius:var(--r-sm,7px);background:var(--surf,var(--s1,var(--bg1,#151719)));color:var(--t1,var(--text,#d8dce4));font:11.5px var(--f-ui,system-ui,sans-serif);box-shadow:var(--elev,0 6px 18px rgba(0,0,0,.3)),0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09)))}'
  ].join('\n');
  var COL = { message:'var(--ac,#5a9e8f)', text:'var(--ac,#5a9e8f)', cap:'var(--dv5,#ec4899)', file:'var(--dv1,#a78bfa)', cite:'var(--dv1,#a78bfa)', record:'var(--dv7,#fb923c)',
    dataset:'var(--dv3,#38bdf8)', memory:'var(--dv2,#5ec9a0)', code:'var(--dv7,#fb923c)', widget:'var(--dv1,#a78bfa)', panel:'var(--dv3,#38bdf8)', host:'var(--dv3,#38bdf8)', container:'var(--dv3,#38bdf8)' };
  var _binds = [], _cm = null, _crun = null, _lastX = 60, _lastY = 120;
  function css(){ if (document.getElementById('veraRcmCss')) return; var st = document.createElement('style'); st.id = 'veraRcmCss'; st.textContent = CSS; (document.head || document.documentElement).appendChild(st); }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]; }); }
  function txt(el){ return String((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim(); }
  function selText(){ try{ var s = window.getSelection(); return s ? String(s).trim() : ''; }catch(e){ return ''; } }
  function toast(m){ css(); var t = document.createElement('div'); t.className = 'rcm-toast'; t.textContent = m; document.body.appendChild(t); setTimeout(function(){ if (t.parentNode) t.parentNode.removeChild(t); }, 2400); }

  // ── what is under the pointer, on any page ──
  function generic(t){
    var q = function(s){ return t.closest ? t.closest(s) : null; }, el;
    if (q('.cmenu,.crun,input,textarea,select,[contenteditable="true"],canvas,video')) return null;   // fields and canvases keep their own
    var sel = selText();
    if (sel.length > 1){ try{ var r = window.getSelection().getRangeAt(0), c = r.commonAncestorContainer; c = c.nodeType === 1 ? c : c.parentNode;
      if (c && (c.contains(t) || t.contains(c))) return ['text', sel.slice(0, 40) + (sel.length > 40 ? '…' : ''), t, { text: sel }]; }catch(e){} }
    if ((el = q('[data-rcm-kind]'))) return [el.getAttribute('data-rcm-kind'), el.getAttribute('data-rcm-name') || txt(el).slice(0, 48), el, { text: txt(el) }];
    if ((el = q('[data-entity],[data-ref]'))){ var ref = el.getAttribute('data-entity') || el.getAttribute('data-ref') || '';
      var k = el.getAttribute('data-kind') || (ref.indexOf(':') > 0 ? ref.split(':')[0] : 'record');
      return [k, ref.indexOf(':') > 0 ? ref.split(':').slice(1).join(':') : ref, el, { ref: ref, text: txt(el) }]; }
    if ((el = q('.widget[data-wid], vera-widget'))){ var ti = el.querySelector && el.querySelector('.w-title'); return ['widget', txt(ti) || el.getAttribute('data-wid') || 'widget', el, { wid: el.getAttribute('data-wid') || '', text: txt(el) }]; }
    if ((el = q('.nav-btn'))) return ['panel', (el.getAttribute('title') || txt(el)).slice(0, 48), el, { nav: true }];
    if ((el = q('tbody tr, table tr'))){ if (el.closest('thead')) return null; var c1 = el.querySelector('td,th'); return ['record', txt(c1).slice(0, 48) || 'row', el, { text: txt(el) }]; }
    if ((el = q('a[href]'))) return ['cite', txt(el).slice(0, 48) || el.getAttribute('href'), el, { href: el.href, text: el.href }];
    if ((el = q('pre, code'))) return ['code', txt(el).slice(0, 40), el, { text: txt(el) }];
    return null;
  }
  function target(t){
    for (var i = _binds.length - 1; i >= 0; i--){ try{ var r = _binds[i].target && _binds[i].target(t); if (r === false) return null; if (r) return r; }catch(e){} }
    return generic(t);
  }

  // ── the actions any page can do ──
  function print(text){
    text = String(text || '').trim(); if (!text) return toast('nothing to print');
    toast('printing on the thermal printer…');
    fetch('/print/text', { method:'POST', headers:{ 'Content-Type':'application/json' }, body: JSON.stringify({ text: text.slice(0, 4000) }) })
      .then(function(r){ return r.json(); })
      .then(function(r){ toast((r && r.routed) ? '✓ printed' : ((r && r.escpos_b64) ? 'open Printer to finish' : ((r && r.error) || 'print failed'))); })
      .catch(function(){ toast('print failed'); });
  }
  function genericAct(id, kind, name, el, x){
    switch (id){
      case 'print': print(x.text || selText() || txt(el) || name); return true;
      case 'copy': try{ navigator.clipboard.writeText(String(x.text || x.ref || name)); toast('copied'); }catch(e){ toast('copy failed'); } return true;
      case 'open': if (x.href){ window.open(x.href, '_blank', 'noopener'); return true; } if (el && typeof el.click === 'function' && kind !== 'text'){ el.click(); return true; } return false;
      case 'ask': runner('chat.ask', { about: name, question: 'What is this, what depends on it, and what would break if it changed?', context: String(x.text || '').slice(0, 2000) }, name, _lastX, _lastY); return true;
    }
    return false;
  }
  function act(id, kind, name, el, x){
    for (var i = _binds.length - 1; i >= 0; i--){ try{ if (_binds[i].act && _binds[i].act(id, kind, name, el, x) === true) return; }catch(e){} }
    if (genericAct(id, kind, name, el, x)) return;
    toast('"' + id + '" is not wired here — the menu reads the same everywhere');
  }
  function capCall(cap, args){
    return fetch('/mcp/call', { method:'POST', headers:{ 'Content-Type':'application/json' }, body: JSON.stringify({ name: cap, arguments: args || {} }) })
      .then(function(r){ return r.json(); }).then(function(j){ return (j && j.content !== undefined) ? j.content : j; });
  }

  // ── the menu and the runner (the chat's, drawn here) ──
  function close(){ if (_cm && _cm.parentNode) _cm.parentNode.removeChild(_cm); _cm = null; }
  function closeRunner(){ if (_crun && _crun.parentNode) _crun.parentNode.removeChild(_crun); _crun = null; }
  function open(ev, kind, name, el, x){
    if (!window.MENUS) return false;
    ev.preventDefault(); ev.stopPropagation(); close(); css(); x = x || {}; _lastX = ev.clientX; _lastY = ev.clientY;
    var seen = {};
    var rows = MENUS.rows(kind, name, { noPin: !x.canPin }).filter(function(r){ var k = r.t === 'act' ? 'a:' + String(r.n || r.id) : r.t === 'cap' ? 'c:' + String(r.cap) : 's' + Math.random(); if (seen[k]) return false; seen[k] = 1; return true; });
    var col = COL[MENUS.kindOf(kind) || kind] || 'var(--t3,#6b7280)';
    var w = document.createElement('div'); w.className = 'cmw';
    var bg = document.createElement('span'); bg.className = 'cmbg'; bg.addEventListener('click', close); bg.addEventListener('contextmenu', function(e){ e.preventDefault(); close(); }); w.appendChild(bg);
    var m = document.createElement('div'); m.className = 'cmenu'; m.setAttribute('data-w', 'context menu · menu');
    m.innerHTML = '<span class="cm-h"><i style="background:' + col + '"></i><span>' + esc(name) + '</span><b>' + esc(MENUS.label(kind)) + '</b></span>';
    var sawCap = false;
    rows.forEach(function(r){
      if (r.t === 'cap' && !sawCap){ sawCap = true; var s = document.createElement('div'); s.className = 'cm-sep'; m.appendChild(s); }
      var b = document.createElement('button'); b.type = 'button'; b.className = 'cm-i' + (r.t === 'cap' ? ' cap' : '') + (r.cls ? ' ' + r.cls : '');
      b.innerHTML = (r.t === 'cap' ? esc(r.cap) : esc(r.n)) + '<i>' + esc(r.t === 'cap' ? 'stage' : (r.k || '')) + '</i>';
      b.addEventListener('click', function(e){ e.stopPropagation(); close(); if (r.t === 'cap') runner(r.cap, r.arg, name, ev.clientX, ev.clientY); else act(r.id, kind, name, el, x); });
      m.appendChild(b);
    });
    w.appendChild(m); document.body.appendChild(w); _cm = w;
    var rc = m.getBoundingClientRect();
    m.style.left = Math.max(4, Math.min(ev.clientX, window.innerWidth - rc.width - 8)) + 'px';
    m.style.top = Math.max(4, Math.min(ev.clientY, window.innerHeight - rc.height - 8)) + 'px';
    return true;
  }
  function runner(cap, arg, tgt, cx, cy){
    closeRunner(); css();
    var d = document.createElement('div'); d.className = 'crun'; d.setAttribute('data-w', 'capability runner · form');
    d.innerHTML = '<span class="crun-h"><i></i>' + esc(cap) + '<b title="' + esc(tgt || '') + '">' + esc(tgt || '') + '</b><button class="crun-x" type="button" title="Close">✕</button></span><textarea class="crun-b" spellcheck="false"></textarea><span class="crun-a"><button class="go" type="button">Send</button><span class="crun-r">staged from the menu · nothing sent yet</span></span><div class="crun-out" hidden></div>';
    d.querySelector('.crun-b').value = JSON.stringify(arg || {}, null, 2);
    d.querySelector('.crun-x').addEventListener('click', closeRunner);
    d.querySelector('.go').addEventListener('click', function(){
      var go = d.querySelector('.go'), r = d.querySelector('.crun-r'), out = d.querySelector('.crun-out'), a = {};
      try{ a = JSON.parse(d.querySelector('.crun-b').value || '{}'); }catch(e){ r.textContent = 'the arguments are not JSON: ' + e.message; return; }
      go.disabled = true; r.textContent = 'sending…'; var t0 = Date.now();
      capCall(cap, a).then(function(res){ r.textContent = (res && res.ok === false ? 'error' : '200') + ' · ' + cap + ' · ' + (Date.now() - t0) + 'ms'; out.hidden = false; out.textContent = (typeof res === 'string' ? res : JSON.stringify(res, null, 2) || '').slice(0, 4000); go.textContent = 'Sent'; })
        .catch(function(e){ r.textContent = 'failed · ' + String(e && e.message || e); })
        .then(function(){ go.disabled = false; });
    });
    document.body.appendChild(d); _crun = d;
    var rc = d.getBoundingClientRect();
    d.style.left = Math.max(4, Math.min(cx || 40, window.innerWidth - rc.width - 8)) + 'px';
    d.style.top = Math.max(4, Math.min(cy || 40, window.innerHeight - rc.height - 8)) + 'px';
  }

  document.addEventListener('contextmenu', function(ev){
    try{
      if (ev.shiftKey || window.__veraRcmOwn || !window.MENUS) return;
      var t = target(ev.target); if (!t) return;
      open(ev, t[0], t[1], t[2], t[3]);
    }catch(e){}
  }, true);
  document.addEventListener('keydown', function(ev){ if (ev.key === 'Escape'){ close(); } });

  window.VeraRCM = {
    attach: function(b){ if (b && (b.target || b.act)) _binds.push(b); return window.VeraRCM; },
    open: open, close: close, runner: runner, print: print, target: target, generic: generic, act: act
  };
})();
