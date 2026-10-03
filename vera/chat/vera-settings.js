/* ═══════════════════════════════════════════════════════════════════════════
 * vera-settings.js — ONE settings page for every panel (owner, 2026-10-03:
 * "that settings menu is the cannon settings menu style for all panels").
 *
 * The chat's Settings page is the canon: a full page over the panel - a header
 * (title, crumb, Esc, ✕), an index on the left (search, the panel's group, a
 * row per card) and the cards on the right in a grid. This draws the same page
 * for any panel, from the panel's own settings section:
 *
 *   <button class="nav-btn" data-section="settings"
 *           data-vera-settings="#sect-settings">Settings</button>
 *
 * Clicking that item - or the harness picking it from the docked menu (it
 * clicks the item) - opens the page instead of the panel's in-page section.
 * The section's first heading is the page section's title, a lead paragraph
 * under it its line, and every other top-level block one card, titled by its
 * own first heading / label. The blocks are MOVED, never copied: every field
 * keeps its id and its handlers, and goes back where it was when the page
 * closes (Esc, ✕). Ctrl/⌘ + , opens it.
 *
 * The values are the chat's (#settingsPage in chat_panel.html; the test
 * test_lhm_shared_settings keeps them equal); colours fall back through the
 * names other panels' themes use.
 * ═══════════════════════════════════════════════════════════════════════ */
(function(){
  if(window.VeraSettings) return;
  var BG = 'var(--bg, var(--bg0, #0e0f12))', T1 = 'var(--t1, var(--text, var(--fg, #d8dce4)))', T2 = 'var(--t2, var(--dim2, var(--fg2, #8a92a0)))',
      T3 = 'var(--t3, var(--dim, #6b7280))', BD = 'var(--bd, var(--border, var(--line, rgba(255,255,255,.09))))',
      SURF = 'var(--surf, var(--bg1, var(--s1, #14171c)))', SURF2 = 'var(--surf2, var(--bg2, var(--s2, #1a1f26)))',
      CARD = 'var(--s2, var(--bg2, #15181d))', ACC = 'var(--ac, var(--acc, var(--accent, #5a9e8f)))', ELEV = 'var(--elev, 0 1px 2px rgba(0,0,0,.18))';
  var CSS = [
    '.vs-page{position:fixed;top:0;right:0;bottom:0;left:0;z-index:5200;display:flex;flex-direction:column;background:' + BG + ';color:' + T1 + ';font-family:var(--f-ui,var(--sans,system-ui,sans-serif))}',
    '.vs-page .vs-hd{display:flex;align-items:center;gap:14px;height:52px;padding:0 22px;border-bottom:1px solid ' + BD + ';flex-shrink:0}',
    '.vs-page .vs-hd h1{margin:0;font-size:19px;font-weight:600;letter-spacing:-.01em}',
    '.vs-page .vs-hd .crumb{font-size:10.5px;color:' + T2 + '}',
    '.vs-page .vs-hd .kbd{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;color:' + T3 + ';border:1px solid ' + BD + ';border-radius:var(--r-sm,6px);padding:2px 7px}',
    '.vs-page .vs-hd .x{width:28px;height:28px;border:none;border-radius:var(--r-sm,6px);background:transparent;color:' + T3 + ';font:inherit;font-size:13px;cursor:pointer}',
    '.vs-page .vs-hd .x:hover{color:' + T1 + ';background:' + SURF2 + '}',
    '.vs-page .vs-hd .sp{flex:1}',
    '.vs-page .vs-bd{flex:1;min-height:0;display:grid;grid-template-columns:232px 1fr;gap:14px;padding:14px 22px 18px}',
    '.vs-page .vs-idx{background:' + SURF + ';border-radius:var(--ui-radius,10px);box-shadow:' + ELEV + ';display:flex;flex-direction:column;min-height:0;overflow:hidden}',
    '.vs-page .vs-idx-hd{padding:12px 12px 6px}',
    '.vs-page .vs-srch{display:flex;align-items:center;gap:7px;height:28px;padding:0 10px;border-radius:var(--r-sm,6px);background:' + BG + ';box-shadow:inset 0 0 0 1px ' + BD + ';color:' + T3 + ';font-size:10.5px}',
    '.vs-page .vs-srch input{flex:1;min-width:0;border:none;background:transparent;font:inherit;color:' + T1 + ';outline:none}',
    '.vs-page .vs-idx-bd{overflow:auto;padding:4px 8px 12px}',
    '.vs-page .vs-grp{display:flex;align-items:center;gap:6px;padding:10px 6px 4px;font-size:var(--label-size,9.5px);text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:' + T2 + '}',
    '.vs-page .vs-grp b{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-weight:400;color:' + T3 + '}',
    '.vs-page .vs-irow{display:flex;align-items:center;gap:8px;height:28px;padding:0 8px;border-radius:var(--r-sm,6px);font-size:11.5px;color:' + T2 + ';cursor:pointer}',
    '.vs-page .vs-irow:hover{color:' + T1 + ';background:' + SURF2 + '}',
    '.vs-page .vs-irow.on{color:' + T1 + ';background:var(--fill,color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent));box-shadow:inset 2px 0 0 ' + ACC + '}',
    '.vs-page .vs-irow .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.vs-page .vs-cn{min-height:0;overflow:auto;background:' + SURF + ';border-radius:var(--ui-radius,10px);box-shadow:' + ELEV + ';padding:6px 18px 18px}',
    '.vs-page .vs-sec-h{display:flex;align-items:baseline;gap:12px;padding:12px 0 10px;border-bottom:1px solid ' + BD + ';margin-bottom:10px}',
    '.vs-page .vs-sec-h h3{margin:0;font-size:15px;font-weight:600}',
    '.vs-page .vs-sec-h .lbl{font-size:10.5px;color:' + T2 + '}',
    '.vs-page .vs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;align-items:start;padding-bottom:12px}',
    '.vs-page .vs-card{background:' + CARD + ';border-radius:var(--ui-radius,10px);box-shadow:' + ELEV + ';padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;min-width:0;font-size:11.5px;position:relative;transition:box-shadow .2s}',
    '.vs-page .vs-card.on{box-shadow:' + ELEV + ',0 0 0 1.5px ' + ACC + '}',
    '.vs-page .vs-card[hidden],.vs-page .vs-irow[hidden]{display:none}',
    '.vs-page .vs-card > *{max-width:100%}',
    '.vs-page .vs-card > .card{background:transparent;border:0;box-shadow:none;padding:0;margin:0}',
    '.vs-page .vs-card select,.vs-page .vs-card input[type="text"],.vs-page .vs-card input[type="number"],.vs-page .vs-card input[type="url"],.vs-page .vs-card input[type="password"],.vs-page .vs-card textarea{font:inherit;font-size:11px;color:' + T1 + ';background:' + BG + ';border:1px solid ' + BD + ';border-radius:var(--r-sm,6px);padding:3px 7px}',
    '.vs-page .vs-empty{padding:16px 4px;font-size:11px;color:' + T3 + '}',
    '@media (prefers-reduced-motion:reduce){.vs-page .vs-card{transition:none}}'
  ].join('\n');

  function _css(){ if(document.getElementById('vera-settings-css')) return; var st = document.createElement('style'); st.id = 'vera-settings-css'; st.textContent = CSS; (document.head || document.documentElement).appendChild(st); }
  function _el(tag, cls, text){ var e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e; }
  function _txt(e){ return String((e && e.textContent) || '').replace(/\s+/g, ' ').trim(); }
  function _panelName(){ return String(document.title || 'This panel').replace(/^\s*Vera\s*[—–\-·|:/]\s*/i, '').trim() || 'This panel'; }

  /* a settings section, read: {title, lead, cards:[element]} - the first heading is the title, a paragraph straight
     under it the lead, every other top-level block a card */
  function sectionParts(sec){
    var title = '', lead = '', cards = [], kids = Array.prototype.slice.call(sec.children || []);
    kids.forEach(function(k){
      var tag = String(k.tagName || '').toUpperCase();
      if(tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEMPLATE') return;
      if(!title && /^H[1-4]$/.test(tag)){ title = _txt(k); return; }
      // the line under the title: a paragraph, or the first block that holds text but nothing to set (an explanation)
      if(title && !lead && !cards.length && (tag === 'P' || (!_fields(k) && !_hasButton(k) && _txt(k).length && _txt(k).length < 320))){ lead = _txt(k); k.setAttribute('data-vs-lead', ''); return; }
      cards.push(k);
    });
    return { title: title, lead: lead, cards: cards };
  }
  function _fields(k){ return !!(k.querySelector && k.querySelector('input:not([type=hidden]),select,textarea')) || /^(INPUT|SELECT|TEXTAREA)$/.test(String(k.tagName || '').toUpperCase()); }
  function _hasButton(k){ return !!(k.querySelector && k.querySelector('button')) || String(k.tagName || '').toUpperCase() === 'BUTTON'; }
  /* a card's heading: a real one, else a short first line over more content (a block whose fields its page draws later)  */
  function _head(k){
    var h = k.querySelector && k.querySelector('h1,h2,h3,h4,h5,legend,.card-title,.sec,.title'); if(h) return h;
    var n0 = k.firstChild; while(n0 && n0.nodeType === 3 && !/\S/.test(n0.textContent)) n0 = n0.nextSibling;   /* it must OPEN the card, not sit in a sentence */
    var f = k.firstElementChild; if(f && f === n0 && k.children.length > 1 && !/^(INPUT|SELECT|TEXTAREA|BUTTON|TABLE)$/.test(f.tagName) && _txt(f).length && _txt(f).length <= 60 && !(f.querySelector && f.querySelector('input,select,textarea,button'))) return f;
    return null;
  }
  /* a card's name for the index: its own heading / legend, else its first field's label, else that field's placeholder,
     aria-label or name - never a link or a bold word inside a sentence. '' = the card gets no row (an explanation, a row
     of buttons): it is still shown, just not listed */
  function cardTitle(k){
    var h = _head(k); if(h && _txt(h)) return _txt(h).slice(0, 48);
    if(!_fields(k)) return '';
    var lb = k.querySelector('label'); if(lb && _txt(lb)) return _txt(lb).slice(0, 48);
    var f = k.querySelector('input:not([type=hidden]),select,textarea') || k;
    return String(f.getAttribute && (f.getAttribute('placeholder') || f.getAttribute('aria-label') || f.getAttribute('name') || f.id) || 'Settings').replace(/[-_]+/g, ' ').trim().slice(0, 48);
  }

  var _page = null, _cur = null;
  function isOpen(){ return !!_cur; }
  function open(trigger){
    var sel = trigger && trigger.getAttribute && trigger.getAttribute('data-vera-settings');
    var sec = sel ? document.querySelector(sel) : null;
    if(!sec) return false;
    close(); _css();
    var parts = sectionParts(sec), moved = [];
    var page = _el('div', 'vs-page'); page.setAttribute('data-w', 'settings · page'); page.setAttribute('role', 'dialog'); page.setAttribute('aria-label', 'Settings');
    var hd = _el('header', 'vs-hd'); hd.appendChild(_el('h1', '', 'Settings')); hd.appendChild(_el('span', 'crumb', _panelName()));
    hd.appendChild(_el('div', 'sp')); hd.appendChild(_el('span', 'kbd', 'Esc'));
    var x = _el('button', 'x', '✕'); x.type = 'button'; x.title = 'Close (Esc) - every field keeps its value'; x.addEventListener('click', function(){ close(); }); hd.appendChild(x);
    page.appendChild(hd);
    var bd = _el('div', 'vs-bd');
    var idx = _el('nav', 'vs-idx'), ih = _el('div', 'vs-idx-hd'), srch = _el('label', 'vs-srch', '⌕'), q = _el('input'); q.type = 'search'; q.placeholder = 'Find a setting'; srch.appendChild(q); ih.appendChild(srch); idx.appendChild(ih);
    var ib = _el('div', 'vs-idx-bd'); var grp = _el('div', 'vs-grp'); grp.appendChild(document.createTextNode(_panelName() + ' ')); var grpCt = _el('b', '', ''); grp.appendChild(grpCt); ib.appendChild(grp);
    idx.appendChild(ib); bd.appendChild(idx);
    var cn = _el('main', 'vs-cn'), sh = _el('div', 'vs-sec-h'); sh.appendChild(_el('h3', '', parts.title || (trigger.getAttribute('data-label') || _txt(trigger) || 'Settings')));   /* no heading: the item's own name */ if(parts.lead) sh.appendChild(_el('span', 'lbl', parts.lead)); cn.appendChild(sh);
    var grid = _el('div', 'vs-grid'); cn.appendChild(grid); bd.appendChild(cn); page.appendChild(bd);
    var rows = [];
    parts.cards.forEach(function(k, i){
      var mark = document.createComment('vera-settings'); k.parentNode.insertBefore(mark, k);
      var card = _el('div', 'vs-card'); card.appendChild(k); grid.appendChild(card); moved.push({ el: k, mark: mark });
      var nm = cardTitle(k); if(!nm){ rows.push({ row: null, card: card }); return; }   // shown, not listed
      var row = _el('div', 'vs-irow'); row.appendChild(_el('span', 'nm', nm));
      row.addEventListener('click', function(){ rows.forEach(function(r){ if(r.row) r.row.classList.toggle('on', r.row === row); r.card.classList.toggle('on', r.row === row); }); try{ card.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }catch(e){ card.scrollIntoView(); } });
      ib.appendChild(row); rows.push({ row: row, card: card });
    });
    grpCt.textContent = String(rows.filter(function(r){ return r.row; }).length);
    if(!parts.cards.length) grid.appendChild(_el('div', 'vs-empty', 'This panel has no settings to show.'));
    q.addEventListener('input', function(){ var v = q.value.trim().toLowerCase(); rows.forEach(function(r){ var hit = !v || _txt(r.card).toLowerCase().indexOf(v) >= 0 || (r.row && _txt(r.row).toLowerCase().indexOf(v) >= 0); if(r.row) r.row.hidden = !hit; r.card.hidden = !hit; }); });
    document.body.appendChild(page);
    _page = page; _cur = { trigger: trigger, sec: sec, moved: moved };
    try{ q.focus(); }catch(e){}
    return true;
  }
  // every block goes back where it was, in its order - with whatever was typed into it
  function close(){
    if(!_cur) return false;
    _cur.moved.forEach(function(m){ if(m.mark.parentNode){ m.mark.parentNode.insertBefore(m.el, m.mark); m.mark.parentNode.removeChild(m.mark); } });
    if(_page && _page.parentNode) _page.parentNode.removeChild(_page);
    _page = null; _cur = null; return true;
  }
  // the item opens the page, before the panel's own handler can show its in-page section (capture, on the document)
  document.addEventListener('click', function(ev){
    var t = ev.target && ev.target.closest ? ev.target.closest('[data-vera-settings]') : null;
    if(!t) return;
    ev.preventDefault(); ev.stopPropagation();
    open(t);
  }, true);
  document.addEventListener('keydown', function(ev){
    if(ev.key === 'Escape' && _cur){ ev.preventDefault(); close(); return; }
    if((ev.ctrlKey || ev.metaKey) && ev.key === ','){ var t = document.querySelector('[data-vera-settings]'); if(t){ ev.preventDefault(); if(_cur) close(); else open(t); } }
  }, true);

  window.VeraSettings = { open: open, close: close, isOpen: isOpen, sectionParts: sectionParts, cardTitle: cardTitle, css: _css };
})();
